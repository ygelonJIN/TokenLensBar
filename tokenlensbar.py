#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TokenLensBar — 本地 token 用量采集器

设计原则
  1. 不碰 API Key，不发任何网络请求。
  2. 不依赖模型厂商，不认识"厂商"这个概念。
  3. 不绑定 Agent，只绑定"本地用量记录的数据格式"。
  4. 增量扫描：只重读变化的文件；事件按稳定 ID 去重，重复扫描不会重复计数。

用法
  python3 tokenlensbar.py scan                # 采集（可放进 cron / launchd）
  python3 tokenlensbar.py report --days 7     # 看报表
  python3 tokenlensbar.py report --by agent
  python3 tokenlensbar.py sources             # 看发现了哪些数据源

新增一个 Agent 的支持 = 写一个 Adapter 子类 + 注册到 ADAPTERS，约 20 行。
"""

import argparse
import json
import math
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

DEFAULT_DB = Path(os.environ.get("TOKENLENSBAR_DB", "~/.tokenlensbar/usage.db")).expanduser()
PRICES_PATH = Path(os.environ.get("TOKENLENSBAR_PRICES", "~/.tokenlensbar/prices.json")).expanduser()
BALANCES_PATH = Path(os.environ.get("TOKENLENSBAR_BALANCES", "~/.tokenlensbar/balances.json")).expanduser()
SECRETS_PATH = Path(os.environ.get("TOKENLENSBAR_SECRETS", "~/.tokenlensbar/secrets.json")).expanduser()
DEEPSEEK_BALANCE_URL = os.environ.get(
    "TOKENLENSBAR_DEEPSEEK_BALANCE_URL", "https://api.deepseek.com/user/balance")
# 小米 MiMo 没有 API Key 余额接口，只有控制台会话接口（Cookie 鉴权）。
MIMO_BALANCE_URL = os.environ.get(
    "TOKENLENSBAR_MIMO_BALANCE_URL", "https://platform.xiaomimimo.com/api/v1/balance")

MTOK = 1_000_000.0


# --------------------------------------------------------------------------
# 基础设施
# --------------------------------------------------------------------------

def home(*parts):
    return Path(os.path.expanduser("~")).joinpath(*parts)


DEFAULT_OFF_PEAK = {"timezone": "Asia/Shanghai",
                    "peak_windows": [["09:00", "12:00"], ["14:00", "18:00"]],
                    "weekdays": [1, 2, 3, 4, 5],
                    "holidays": []}
RATE_KEYS = ("input_cache_hit", "input_cache_miss", "output", "cache_write")


def _hhmm(text):
    hour, minute = str(text).split(":")
    return int(hour) * 60 + int(minute)


def _date_to_epoch(text):
    if not text:
        return 0.0
    try:
        return datetime.strptime(str(text), "%Y-%m-%d").timestamp()
    except ValueError:
        return 0.0


def _rates(entry):
    out = {}
    for key in RATE_KEYS:
        value = entry.get(key)
        if value is not None:
            out[key] = float(value)
    return out


def _normalize_versions(spec):
    """一个型号可以有多段价格（涨价/降价），按生效日期取对应那段。"""
    versions = []
    if isinstance(spec.get("effective"), list):
        for item in spec["effective"]:
            if not isinstance(item, dict):
                continue
            versions.append({
                "from": _date_to_epoch(item.get("from")),
                "peak": _rates(item.get("peak") or item),
                "off_peak": _rates(item.get("off_peak") or item),
            })
    else:
        versions.append({
            "from": 0.0,
            "peak": _rates(spec.get("peak") or spec),
            "off_peak": _rates(spec.get("off_peak") or spec),
        })
    versions.sort(key=lambda v: v["from"])
    return versions


def load_prices(path=None):
    """读取单价表并归一化。

    {
      "currency": "CNY", "symbol": "\u00a5",
      "off_peak": {"timezone": "Asia/Shanghai", "windows": [["00:30", "08:30"]]},
      "models": {
        "mimo-v2.6-pro": {"match": ["mimo-v2.6-pro"],
                          "input_cache_hit": 0.025, "input_cache_miss": 3.0, "output": 6.0},
        "deepseek-v4-pro": {"match": ["DeepSeek-V4-Pro"],
                            "peak": {...}, "off_peak": {...}}
      }
    }
    """
    path = Path(path or PRICES_PATH)
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}

    prices = {
        "currency": raw.get("currency") or "USD",
        "symbol": raw.get("symbol") or "$",
        "off_peak": raw.get("off_peak") or DEFAULT_OFF_PEAK,
        # 这些 Agent 的记录只有"最后一次活动时间"，分不清峰谷 —— 一律按峰时算（宁高不低）
        "peak_only_agents": [str(a).lower() for a in (raw.get("peak_only_agents") or [])],
        "models": {},
        "subscription": set(),
    }
    for name, spec in (raw.get("models") or {}).items():
        if not isinstance(spec, dict):
            continue
        aliases = spec.get("match") or [name]
        prices["models"][name] = {
            "match": [str(a).lower().replace("_", "-") for a in aliases],
            "versions": _normalize_versions(spec),
        }
        if spec.get("billing") == "subscription":
            prices["subscription"].add(name)
    return prices


def _match_score(model, spec):
    needle = (model or "").lower().replace("_", "-")
    if not needle:
        return 0
    for index, alias in enumerate(spec["match"]):
        if needle == alias:
            return 1000 - index
        if needle.startswith(alias) or alias.startswith(needle):
            return 500 - index
        if alias in needle:
            return 100 - index
    return 0


def resolve_rate(model, ts, prices):
    """返回 (配置里的型号名, 该时间点生效的费率) 或 (None, None)。"""
    best_score, best_name, best_version = 0, None, None
    for name, spec in prices["models"].items():
        score = _match_score(model, spec)
        if score <= best_score:
            continue
        version = spec["versions"][0] if spec["versions"] else None
        for candidate in spec["versions"]:
            if candidate["from"] <= ts:
                version = candidate
        if version is None:
            continue
        best_score, best_name, best_version = score, name, version
    return best_name, best_version


def model_label(model, ts, prices):
    """展示用型号名：价格表认得的用表里的正式名，认不得就保留日志原名。"""
    raw = model or "(未知)"
    if not prices:
        return raw
    name, _version = resolve_rate(model or "", ts, prices)
    return name or raw


def is_off_peak(ts, prices):
    """这条请求发生时是不是空闲时段。

    两种写法：
      peak_windows  只给出「高峰」区间（工作日 + 时段），其余一律算空闲；
                    配合 weekdays / holidays，周末和法定节假日全天都是空闲。
      windows       老写法：直接给出「空闲」区间。

    判定按事件自己的时间戳，所以历史账单不会因为"现在几点"而变。
    """
    config = (prices or {}).get("off_peak") or {}
    peak_windows = config.get("peak_windows")
    windows = config.get("windows") or []
    if peak_windows is None and not windows:
        return False
    try:
        zone = ZoneInfo(config.get("timezone") or "Asia/Shanghai")
    except Exception:
        return False
    local = datetime.fromtimestamp(ts, zone)
    minutes = local.hour * 60 + local.minute

    if peak_windows is not None:
        weekdays = config.get("weekdays") or [1, 2, 3, 4, 5]
        holidays = set(config.get("holidays") or [])
        if local.isoweekday() not in weekdays:
            return True                                    # 周末
        if local.strftime("%Y-%m-%d") in holidays:
            return True                                    # 法定节假日
        for window in peak_windows:
            try:
                start, end = _hhmm(window[0]), _hhmm(window[1])
            except (ValueError, IndexError, TypeError):
                continue
            if start <= end and start <= minutes < end:
                return False
            if start > end and (minutes >= start or minutes < end):  # 跨午夜
                return False
        return True                                        # 工作日但不在高峰时段

    for window in windows:
        try:
            start, end = _hhmm(window[0]), _hhmm(window[1])
        except (ValueError, IndexError, TypeError):
            continue
        if start <= end:
            if start <= minutes < end:
                return True
        elif minutes >= start or minutes < end:  # 跨午夜
            return True
    return False


def event_cost(event, prices, with_band=False):
    """按'缓存命中 / 未命中 + 峰谷'算一次请求的钱。返回 (金额, 命中的型号名)。

    with_band=True 时多返回一个 "peak" / "off_peak"，用于面板拆峰谷金额。
    """
    if not prices:
        return (0.0, None, "peak") if with_band else (0.0, None)
    ts = float(event.get("ts") or 0)
    name, version = resolve_rate(event.get("model") or "", ts, prices)
    if version is None:
        return (0.0, None, "peak") if with_band else (0.0, None)

    rates = version["peak"]
    band = "peak"
    agent = (event.get("agent") or "").lower()
    peak_only = any(agent == name or agent.startswith(name)
                    for name in (prices.get("peak_only_agents") or []))
    if version["off_peak"] and not peak_only and is_off_peak(ts, prices):
        rates = version["off_peak"]
        band = "off_peak"

    cache_hit = float(event.get("cache_read") or 0)
    raw_input = float(event.get("input") or 0)
    if event.get("cached_in_input", 1):
        cache_miss = max(0.0, raw_input - cache_hit)
    else:
        cache_miss = raw_input

    amount = (
        cache_hit * rates.get("input_cache_hit", 0.0)
        + cache_miss * rates.get("input_cache_miss", 0.0)
        + float(event.get("output") or 0) * rates.get("output", 0.0)
        + float(event.get("cache_write") or 0) * rates.get("cache_write", 0.0)
    )
    if with_band:
        return amount / MTOK, name, band
    return amount / MTOK, name


def as_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def iso_to_epoch(text):
    if not text:
        return 0.0
    try:
        cleaned = text.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned).timestamp()
    except ValueError:
        return 0.0


# launchd 给的 PATH 只有 /usr/bin:/bin:/usr/sbin:/sbin，Homebrew 的 zstd
# 不在里面。以前 scan 找不到 zstd 就把 DSH 会话当成“正在写入”跳过，
# 于是定时采集和面板的「立即采集」永远读不到 DSH 的用量。
ZSTD_SEARCH_PATHS = (
    "/opt/homebrew/bin/zstd",
    "/usr/local/bin/zstd",
    "/opt/local/bin/zstd",
    "/usr/bin/zstd",
)


def find_zstd():
    """先按 PATH 找 zstd，再查常见的 Homebrew / MacPorts / 系统路径。"""
    override = os.environ.get("TOKENLENSBAR_ZSTD")
    if override:
        return override if os.access(override, os.X_OK) else None
    found = shutil.which("zstd")
    if found:
        return found
    for candidate in ZSTD_SEARCH_PATHS:
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def zstd_decompress(path):
    """返回解压后的文本；本机没有 zstd 命令、或一个字节都读不出来时返回 None。

    文件正在被追加写入时，zstd 会因为末尾帧不完整而返回非 0，但前面已经解出来的
    部分是有效数据。所以只要 stdout 有内容就照常返回，让调用方自己跳过被截断的
    最后一行（json 解析失败会自动跳过）。返回 None 才是真的没法读。
    """
    binary = find_zstd()
    if binary is None:
        return None
    try:
        proc = subprocess.run(
            [binary, "-dc", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError:
        return None
    text = proc.stdout.decode("utf-8", "replace")
    if proc.returncode != 0 and not text.strip():
        return None
    return text


def iter_jsonl(text):
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


def make_event(event_id, ts, agent, model, usage, provider=None, cost=0.0,
               cached_in_input=True):
    def pick(*names):
        for name in names:
            if name in usage and usage[name] is not None:
                return as_int(usage[name])
        return 0

    inp = pick("input_tokens", "inputTokens", "prompt_tokens")
    out = pick("output_tokens", "outputTokens", "completion_tokens")
    cache_read = pick("cached_input_tokens", "cache_read_tokens", "cacheReadTokens")
    cache_write = pick("cache_write_input_tokens", "cache_write_tokens", "cacheWriteTokens")
    reasoning = pick("reasoning_output_tokens", "reasoning_tokens", "reasoningTokens")
    total = pick("total_tokens", "totalTokens") or (inp + out)
    return {
        "id": event_id,
        "ts": ts,
        "agent": agent,
        "provider": provider or "",
        "model": model or "",
        "input": inp,
        "output": out,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "reasoning": reasoning,
        "total": total,
        "cost_usd": float(cost or 0.0),
        # 缓存命中是否已经包含在 input 里。Codex/DSH 是包含（total = input + output），
        # Hermes 是单独计费的两列。搞错会让成本翻倍。
        "cached_in_input": 1 if cached_in_input else 0,
    }


# --------------------------------------------------------------------------
# Adapter：一个 Adapter 认领一种"本地用量记录格式"
# --------------------------------------------------------------------------

class SourceBusy(Exception):
    """源文件当前读不出来（比如正在写入 / 压缩流被截断）。

    抛出它的文件本轮不写进 files 表，下一轮扫描会重试——
    绝不能让"没读到"被当成"读完了"，否则这个文件会永久漏采。
    """


class Adapter:
    name = "?"
    # delta    = 每条记录是"一次请求的增量"，可安全追加
    # snapshot = 每条记录是"累计值快照"，每次全量覆盖
    mode = "delta"
    # True = 每次扫描都重读，不走 (size, mtime) 快路径。
    # 整文件重写的源（zstd 会话、sqlite 快照）必须用它：
    # 写入落在 -wal 或扫描正好撞上写入时，size/mtime 会停在半截。
    always_scan = False

    def discover(self):
        """返回本机上存在的候选文件路径。"""
        return []

    def parse(self, path):
        """解析一个文件，产出事件 dict 的生成器。"""
        return []
        yield


class CodexAdapter(Adapter):
    """Codex / rollout 日志：JSONL，事件 token_usage_record。

    注意：payload 里同时有 usage（单次增量）、turn_token_usage（本回合累计）、
    thread_token_usage（整条会话累计）。只能用 usage，否则会重复计数。
    """

    name = "codex"

    def discover(self):
        for root in (home(".codex", "sessions"), home(".codex", "archived_sessions")):
            if root.is_dir():
                for path in root.rglob("*.jsonl"):
                    yield path

    def parse(self, path):
        model = ""
        try:
            handle = open(path, "r", encoding="utf-8", errors="replace")
        except OSError:
            return
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                payload = obj.get("payload") or {}
                if not model and isinstance(payload.get("model"), str):
                    model = payload["model"]
                if obj.get("type") != "token_usage_record":
                    continue
                usage = payload.get("usage")
                if not isinstance(usage, dict):
                    continue
                rid = payload.get("response_id") or "%s:%s" % (
                    payload.get("session_id"), obj.get("ordinal"))
                yield make_event(
                    event_id="codex:%s" % rid,
                    ts=iso_to_epoch(obj.get("timestamp")),
                    agent=self.name,
                    model=payload.get("model") or model,
                    usage=usage,
                    cached_in_input=True,
                )


class DshAdapter(Adapter):
    """DeepSeek Harness：zstd 压缩 JSONL 会话。

    用量在 assistant/message 的 data.usage；型号在 model/selection。
    """

    name = "dsh"
    # DSH 每步都会整文件重写 zstd 会话。扫描正好撞上写入时，解出来的是半截流，
    # 而 (size, mtime) 会被记成"已处理"，之后再也不重读——所以这个源永远重扫。
    always_scan = True

    def discover(self):
        root = home(".dsh", "sessions")
        if root.is_dir():
            for path in root.rglob("*.jsonl.zstd"):
                yield path

    def parse(self, path):
        text = zstd_decompress(path)
        if text is None:
            raise SourceBusy(path)
        session_id = ""
        provider = ""
        model = ""
        step_started = {}
        for obj in iter_jsonl(text):
            kind = obj.get("type")
            if kind == "session":
                session_id = obj.get("id") or ""
            elif kind == "step/start":
                # 峰的档位按"请求发出"的时刻算，不是按答完的时刻。
                data = obj.get("data") or {}
                turn = data.get("turn")
                step = data.get("step")
                started = obj.get("time")
                if turn is not None and step is not None and started:
                    step_started[(turn, step)] = started / 1000.0
            elif kind == "model/selection":
                data = obj.get("data") or {}
                provider = (data.get("provider") or "").replace("-account", "")
                model = data.get("model") or model
            elif kind == "request/context":
                # 有的会话没有 model/selection，模型只在 request/context 里
                data = obj.get("data") or {}
                provider = (data.get("provider") or "").replace("-account", "") or provider
                model = data.get("model") or model
            elif kind == "request/header":
                config = ((obj.get("data") or {}).get("header") or {}).get("config") or {}
                provider = (config.get("provider") or "").replace("-account", "") or provider
                model = config.get("model") or model
            elif kind == "assistant/message":
                data = obj.get("data") or {}
                usage = data.get("usage")
                if not isinstance(usage, dict):
                    continue
                # DSH 的 inputTokens 和 cacheReadTokens 是并列的（实测存在 cache > input），
                # 所以归一化成"输入包含缓存"，并且不传 total_tokens，让它按 input+output 重算。
                normalized = {
                    "input_tokens": as_int(usage.get("inputTokens")) + as_int(usage.get("cacheReadTokens")),
                    "output_tokens": usage.get("outputTokens"),
                    "cache_read_tokens": usage.get("cacheReadTokens"),
                    "cache_write_tokens": usage.get("cacheWriteTokens"),
                }
                yield make_event(
                    event_id="dsh:%s:%s:%s" % (session_id, data.get("turn"), data.get("step")),
                    ts=step_started.get((data.get("turn"), data.get("step")))
                    or (obj.get("time") or 0) / 1000.0,
                    agent=self.name,
                    provider=provider,
                    model=model,
                    usage=normalized,
                    cached_in_input=True,
                )


class HermesAdapter(Adapter):
    """Hermes：状态库里的 session_model_usage，按会话+模型累计。"""

    name = "hermes"
    mode = "snapshot"
    # 写入落在 state.db-wal 上，state.db 的 mtime 可能几小时不动。
    # 走快路径就会一直用几小时前的快照，所以这个源永远重读。
    always_scan = True

    def discover(self):
        for name in ("state.db", "state.db-wal"):
            path = home(".hermes", name)
            if path.exists():
                yield path

    def parse(self, path):
        if path.name.endswith("-wal"):
            return
        try:
            con = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=1.0)
        except sqlite3.Error:
            return
        try:
            rows = con.execute(
                "select session_id, model, billing_provider, task, input_tokens, output_tokens,"
                " cache_read_tokens, cache_write_tokens, reasoning_tokens,"
                " estimated_cost_usd, coalesce(last_seen, first_seen, 0)"
                " from session_model_usage"
            )
        except sqlite3.Error:
            con.close()
            return
        for row in rows:
            (session_id, model, provider, task, inp, out, cr, cw, rsn, cost, last_seen) = row
            yield make_event(
                event_id="hermes:%s:%s:%s:%s" % (session_id, model, provider or "", task or ""),
                ts=float(last_seen or 0),
                agent=self.name,
                provider=provider,
                model=model,
                usage={
                    # Hermes 的 input 不含缓存命中，这里归一化成"输入包含缓存"，
                    # 这样所有来源都是同一套语义：cache_read ⊆ input。
                    "input_tokens": as_int(inp) + as_int(cr),
                    "output_tokens": out,
                    "cache_read_tokens": cr,
                    "cache_write_tokens": cw,
                    "reasoning_tokens": rsn,
                },
                cost=cost,
                cached_in_input=True,
            )
        con.close()


REMOTES_PATH = Path(os.environ.get("TOKENLENSBAR_REMOTES", "~/.tokenlensbar/remotes.json")).expanduser()

# 远端只需要 python3 + 自己的 ~/.hermes/state.db，不用装任何东西
REMOTE_QUERY = r"""
import json, os, sqlite3, sys

path = os.path.expanduser(sys.argv[1])
out = {"mode": None, "rows": [], "error": None}
try:
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=2.0)
    tables = {row[0] for row in con.execute(
        "select name from sqlite_master where type='table'")}

    if "session_model_usage" in tables:
        # 新版 Hermes：按会话+模型累计
        out["mode"] = "model_usage"
        out["rows"] = con.execute(
            "select session_id, model, billing_provider, task, input_tokens,"
            " output_tokens, cache_read_tokens, cache_write_tokens,"
            " reasoning_tokens, estimated_cost_usd,"
            " coalesce(last_seen, first_seen, 0)"
            " from session_model_usage").fetchall()
    elif "sessions" in tables:
        # 老版 Hermes：用量只在 sessions 表里，按会话一条。
        # 会话没正常结束时，用最后一条消息时间当结束时间；仍没有才退回 started_at。
        cols = {row[1] for row in con.execute("pragma table_info(sessions)")}
        last_msg = ("(select max(timestamp) from messages m"
                    " where m.session_id = sessions.id)") if "messages" in tables else "null"
        if "ended_at" in cols:
            stamp = "coalesce(ended_at, %s, started_at)" % last_msg
        else:
            stamp = "coalesce(%s, started_at)" % last_msg
        provider = "billing_provider" if "billing_provider" in cols else "''"
        out["mode"] = "sessions"
        out["rows"] = con.execute(
            "select id, model, %s, '', input_tokens, output_tokens,"
            " cache_read_tokens, cache_write_tokens, reasoning_tokens,"
            " estimated_cost_usd, %s from sessions"
            " where input_tokens > 0 or output_tokens > 0 or cache_read_tokens > 0"
            % (provider, stamp)).fetchall()
    else:
        out["error"] = "远端既没有 session_model_usage 也没有 sessions 表"
    con.close()
except Exception as exc:
    out["error"] = str(exc)

print(json.dumps(out))
"""


class HermesRemoteAdapter(Adapter):
    """SSH 到另一台机器读 Hermes 的状态库（比如树莓派）。"""

    mode = "snapshot"
    virtual = True

    def __init__(self, name, ssh_target, db_path, timeout=30):
        self.name = "hermes-%s" % name
        self.ssh_target = ssh_target
        self.db_path = db_path
        self.timeout = timeout
        self.last_mode = None
        self.last_error = None
        AGENT_LABELS[self.name] = "Hermes (%s)" % name

    def origin_key(self):
        return "remote:%s" % self.name

    def discover(self):
        return []

    def pull(self):
        """返回 (成功?, 事件列表)。连不上时返回 False，避免把上次的数据删掉。"""
        command = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=5",
            "-o", "StrictHostKeyChecking=accept-new",
            self.ssh_target,
            "python3", "-", self.db_path,
        ]
        try:
            proc = subprocess.run(
                command, input=REMOTE_QUERY.encode("utf-8"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=self.timeout, check=False)
        except (OSError, subprocess.SubprocessError):
            return False, []
        if proc.returncode != 0:
            return False, []
        try:
            payload = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
        except ValueError:
            return False, []
        if payload.get("error"):
            self.last_error = payload["error"]
            return False, []
        self.last_mode = payload.get("mode")

        events = []
        for row in payload.get("rows") or []:
            (session_id, model, provider, task, inp, out, cr, cw, rsn,
             cost, last_seen) = row
            events.append(make_event(
                event_id="%s:%s:%s:%s:%s" % (self.name, session_id, model,
                                             provider or "", task or ""),
                ts=float(last_seen or 0),
                agent=self.name,
                provider=provider,
                model=model,
                usage={
                    "input_tokens": as_int(inp) + as_int(cr),
                    "output_tokens": out,
                    "cache_read_tokens": cr,
                    "cache_write_tokens": cw,
                    "reasoning_tokens": rsn,
                },
                cost=cost,
                cached_in_input=True,
            ))
        return True, events


def build_remote_adapters():
    if not REMOTES_PATH.exists():
        return []
    try:
        with open(REMOTES_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    out = []
    for item in (data.get("remotes") or []):
        if not isinstance(item, dict) or not item.get("ssh"):
            continue
        name = item.get("name") or str(item["ssh"]).split("@")[0]
        out.append(HermesRemoteAdapter(
            name=name,
            ssh_target=item["ssh"],
            db_path=item.get("db") or "~/.hermes/state.db",
            timeout=int(item.get("timeout") or 30),
        ))
    return out


# 数据库里存的是内部 key（dsh / codex / hermes），展示时再换成正式名字。
# 这样改显示名不会动到历史事件的 id，也不会导致重复计数。
AGENT_LABELS = {
    "dsh": "DeepSeek Harness",
    "codex": "Codex",
    "hermes": "Hermes (MacBook)",
}


def agent_label(name):
    name = name or "(未知)"
    return AGENT_LABELS.get(name, name)


ADAPTERS = [CodexAdapter(), DshAdapter(), HermesAdapter()] + build_remote_adapters()


# --------------------------------------------------------------------------
# 存储
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          TEXT PRIMARY KEY,
    ts          REAL NOT NULL,
    agent       TEXT NOT NULL,
    provider    TEXT DEFAULT '',
    model       TEXT DEFAULT '',
    input       INTEGER DEFAULT 0,
    output      INTEGER DEFAULT 0,
    cache_read  INTEGER DEFAULT 0,
    cache_write INTEGER DEFAULT 0,
    reasoning   INTEGER DEFAULT 0,
    total       INTEGER DEFAULT 0,
    cost_usd    REAL DEFAULT 0,
    cached_in_input INTEGER DEFAULT 1,
    origin      TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_model ON events(model);
CREATE TABLE IF NOT EXISTS files (
    path   TEXT PRIMARY KEY,
    agent  TEXT,
    size   INTEGER,
    mtime  REAL,
    offset INTEGER DEFAULT 0,
    events INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS balances (
    provider   TEXT NOT NULL,
    currency   TEXT NOT NULL,
    total      REAL NOT NULL DEFAULT 0,
    granted    REAL,
    topped_up  REAL,
    available  INTEGER NOT NULL DEFAULT 1,
    fetched_at REAL NOT NULL,
    PRIMARY KEY (provider, currency)
);
CREATE TABLE IF NOT EXISTS balance_state (
    provider     TEXT PRIMARY KEY,
    last_attempt REAL,
    last_success REAL,
    error        TEXT
);
CREATE TABLE IF NOT EXISTS balance_samples (
    provider   TEXT NOT NULL,
    currency   TEXT NOT NULL,
    ts         REAL NOT NULL,
    total      REAL NOT NULL,
    granted    REAL,
    topped_up  REAL,
    available  INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (provider, currency, ts)
);
CREATE INDEX IF NOT EXISTS idx_balance_samples_ts ON balance_samples(ts);
CREATE TABLE IF NOT EXISTS manual_entries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    day        TEXT NOT NULL,
    created_at REAL NOT NULL,
    amount     REAL,
    tokens     INTEGER,
    agent      TEXT,
    model      TEXT
);
CREATE INDEX IF NOT EXISTS idx_manual_entries_day ON manual_entries(day);
"""


def connect(db_path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.executescript(SCHEMA)
    # 老库升级：补 cached_in_input 这一列
    cols = [row[1] for row in con.execute("pragma table_info(events)")]
    if "cached_in_input" not in cols:
        con.execute("alter table events add column cached_in_input integer default 1")
        con.commit()
    # 老库里 Hermes 的行是"输入不含缓存"，就地归一化（幂等：改完标记就置 1）
    pending = con.execute(
        "select count(*) from events where cached_in_input = 0").fetchone()[0]
    if pending:
        con.execute("update events set input = input + cache_read,"
                    " total = total + cache_read where cached_in_input = 0")
        con.execute("update events set cached_in_input = 1 where cached_in_input = 0")
        con.commit()
    return con


INSERT_SQL = (
    "insert into events"
    " (id, ts, agent, provider, model, input, output, cache_read, cache_write,"
    "  reasoning, total, cost_usd, cached_in_input, origin)"
    " values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    # 峰谷口径改成按 step/start 计费后，重扫要能改写已有行的 ts；
    # 其它字段保持原样，避免把手工修正过的内容覆盖掉。
    " on conflict(id) do update set ts=excluded.ts"
    " where excluded.ts <> events.ts"
)


def insert_event(con, event, origin):
    con.execute(INSERT_SQL, (
        event["id"], event["ts"], event["agent"], event["provider"], event["model"],
        event["input"], event["output"], event["cache_read"], event["cache_write"],
        event["reasoning"], event["total"], event["cost_usd"],
        event["cached_in_input"], origin,
    ))


def scan(db_path, verbose=False, force_balance=False):
    con = connect(db_path)
    stats = {}
    for adapter in ADAPTERS:
        if getattr(adapter, "virtual", False):
            ok, events = adapter.pull()
            if not ok:
                # 连不上就保留上次抓到的数据，不能因为一次断网把历史清空
                if verbose:
                    print("  ! %s 连不上（%s），保留上次数据"
                          % (agent_label(adapter.name), adapter.ssh_target))
                stats[adapter.name] = (1, 0, 0)
                continue
            origin = adapter.origin_key()
            con.execute("delete from events where origin=?", (origin,))
            before = con.total_changes
            for event in events:
                insert_event(con, event, origin)
            inserted = con.total_changes - before
            con.execute(
                "insert or replace into files (path, agent, size, mtime, offset, events)"
                " values (?,?,?,?,?,?)",
                (origin, adapter.name, 0, time.time(), 0, inserted),
            )
            con.commit()
            stats[adapter.name] = (1, 1, inserted)
            if verbose:
                print("  + %-6s %s → %d 条" % (adapter.name, adapter.ssh_target, inserted))
            continue

        found = parsed = inserted = 0
        for path in sorted(set(adapter.discover())):
            if adapter.mode == "snapshot" and path.name.endswith("-wal"):
                continue
            found += 1
            try:
                st = path.stat()
            except OSError:
                continue
            row = con.execute("select size, mtime from files where path=?", (str(path),)).fetchone()
            if (not adapter.always_scan and row
                    and row[0] == st.st_size and row[1] == st.st_mtime):
                continue
            # 先把事件全部解出来，再动数据库：解析失败时什么都不能改
            try:
                events = list(adapter.parse(path))
            except SourceBusy:
                if verbose:
                    print("  ~ %-6s %s（正在写入，下一轮重试）" % (adapter.name, path.name))
                continue
            parsed += 1
            if adapter.mode == "snapshot":
                con.execute("delete from events where origin=?", (str(path),))
            before = con.total_changes
            for event in events:
                insert_event(con, event, str(path))
            inserted += con.total_changes - before
            con.execute(
                "insert or replace into files (path, agent, size, mtime, offset, events)"
                " values (?,?,?,?,?,?)",
                (str(path), adapter.name, st.st_size, st.st_mtime, 0, inserted),
            )
            if verbose:
                print("  + %-6s %s" % (adapter.name, path))
        con.commit()
        stats[adapter.name] = (found, parsed, inserted)
    con.close()
    # 余额是可选功能；配置缺失时这里会立刻返回，不发请求。
    try:
        refresh_balances(db_path, force=force_balance, verbose=verbose)
    except Exception:
        if verbose:
            print("  ! 余额刷新异常，保留上次缓存")
    return stats


# --------------------------------------------------------------------------
# 报表
# --------------------------------------------------------------------------

def display_width(text):
    width = 0
    for ch in text:
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def pad(text, width, align="left"):
    text = str(text)
    gap = max(0, width - display_width(text))
    if align == "right":
        return " " * gap + text
    return text + " " * gap


def human(num):
    """中文单位：1 亿 = 10^8，1 万 = 10^4。不写 B/M/K，避免歧义。"""
    num = float(num)
    if num >= 100_000_000:
        return "%.2f亿" % (num / 100_000_000.0)
    if num >= 10_000:
        return "%.1f万" % (num / 10_000.0)
    return str(int(round(num)))


def fmt_ts(epoch):
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


def fetch_rows(db_path, since):
    con = connect(db_path)
    rows = con.execute(
        "select ts, agent, provider, model, input, output, cache_read, cache_write,"
        " reasoning, total, cost_usd, cached_in_input from events where ts >= ? order by ts",
        (since,),
    ).fetchall()
    sources = con.execute("select agent, count(*) from files group by agent").fetchall()
    con.close()
    return rows, sources


def row_event(row):
    return {
        "ts": row[0],
        "agent": row[1],
        "model": row[3],
        "input": row[4],
        "output": row[5],
        "cache_read": row[6],
        "cache_write": row[7],
        "cached_in_input": row[11] if len(row) > 11 else 1,
    }


def row_cost(row, prices, with_band=False):
    """返回 (金额, 命中的型号名, 是否来自来源自带成本)。

    with_band=True 时再返回 "peak" / "off_peak"，供面板拆分峰谷金额。
    """
    # Hermes 自带的 estimated_cost_usd 是美元，和人民币单价表混用会算错，
    # 所以默认一律用本地单价表重算；需要时在 prices.json 里打开 use_source_cost。
    stored = row[10] or 0.0
    if stored > 0 and prices.get("use_source_cost"):
        return (stored, "(来源自带)", True, "peak") if with_band else (stored, "(来源自带)", True)
    if with_band:
        amount, name, band = event_cost(row_event(row), prices, with_band=True)
        return amount, name, False, band
    amount, name = event_cost(row_event(row), prices)
    return amount, name, False


def zero_bucket():
    return {"total": 0, "input": 0, "output": 0, "cache_read": 0,
            "cache_write": 0, "count": 0, "cost": 0.0}


def add(bucket, row, cost):
    bucket["total"] += row[9]
    bucket["input"] += row[4]
    bucket["output"] += row[5]
    bucket["cache_read"] += row[6]
    bucket["cache_write"] += row[7]
    bucket["count"] += 1
    bucket["cost"] += cost
    return bucket


def aggregate(rows, prices, since=0.0):
    """一次遍历，得到总量 + 多维分组 + 未定价型号。"""
    totals = zero_bucket()
    by_model, by_agent, by_provider, by_day = {}, {}, {}, {}
    unpriced = set()

    for row in rows:
        if row[0] < since:
            continue
        cost, matched, stored = row_cost(row, prices)
        model = model_label(row[3], row[0], prices)
        agent = agent_label(row[1])
        provider = row[2] or "(未记录)"
        day = datetime.fromtimestamp(row[0]).strftime("%Y-%m-%d")

        add(totals, row, cost)
        add(by_model.setdefault(model, zero_bucket()), row, cost)
        add(by_agent.setdefault(agent, zero_bucket()), row, cost)
        add(by_provider.setdefault(provider, zero_bucket()), row, cost)

        bucket = by_day.setdefault(day, zero_bucket())
        add(bucket, row, cost)

        if not stored and matched is None and (row[4] or row[5]):
            unpriced.add(model)

    return {
        "totals": totals,
        "by_model": by_model,
        "by_agent": by_agent,
        "by_provider": by_provider,
        "by_day": by_day,
        "unpriced": sorted(unpriced),
    }


def breakdown(rows, prices, since):
    """Agent × 模型 交叉明细：谁、用了哪个模型、多少 token、多少钱。"""
    cells = {}
    for row in rows:
        if row[0] < since:
            continue
        cost, _, _ = row_cost(row, prices)
        key = (agent_label(row[1]), model_label(row[3], row[0], prices))
        add(cells.setdefault(key, zero_bucket()), row, cost)
    out = []
    for (agent, model), bucket in cells.items():
        item = dict(bucket)
        item["agent"] = agent
        item["model"] = model
        out.append(item)
    out.sort(key=lambda item: -item["total"])
    return out


def start_of_day(ts):
    return datetime.fromtimestamp(ts).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def daily_series(rows, since, prices=None, now=None):
    """按天汇总：token、次数、金额；记录之间的空档补 0，时间轴不会被压缩。"""
    prices = prices if prices is not None else load_prices()
    now = time.time() if now is None else now
    series = {}
    for row in rows:
        if row[0] < since:
            continue
        day = datetime.fromtimestamp(row[0]).strftime("%Y-%m-%d")
        bucket = series.setdefault(day, zero_bucket())
        cost, _, _ = row_cost(row, prices)
        add(bucket, row, cost)
    if not series:
        return []

    first = datetime.strptime(min(series), "%Y-%m-%d").date()
    last = max(datetime.fromtimestamp(now).date(),
               datetime.strptime(max(series), "%Y-%m-%d").date())
    out = []
    day = first
    while day <= last:
        key = day.strftime("%Y-%m-%d")
        bucket = series.get(key)
        out.append({
            "date": key,
            "tokens": bucket["total"] if bucket else 0,
            "count": bucket["count"] if bucket else 0,
            "cost": round(bucket["cost"], 6) if bucket else 0.0,
        })
        day += timedelta(days=1)
    return out


def heatmap_series(rows, since, prices=None):
    """7×24 热力图：按计价时区把事件落到「周几 / 几点」，其余返回给前端补格。"""
    prices = prices if prices is not None else load_prices()
    try:
        zone = ZoneInfo((prices.get("off_peak") or {}).get("timezone") or "Asia/Shanghai")
    except Exception:
        zone = None
    cells = {}
    for row in rows:
        if row[0] < since:
            continue
        local = datetime.fromtimestamp(row[0], zone) if zone else datetime.fromtimestamp(row[0])
        key = (local.isoweekday() - 1, local.hour)
        bucket = cells.setdefault(key, zero_bucket())
        cost, _, _ = row_cost(row, prices)
        add(bucket, row, cost)
    return [
        {"day": day, "hour": hour, "tokens": bucket["total"],
         "cost": round(bucket["cost"], 6), "count": bucket["count"]}
        for (day, hour), bucket in sorted(cells.items())
    ]


def outside_cells(db_path, window_cells, prices):
    """窗口内有数据之外的（Agent, 模型）组合：历史还有多少，最后一次是什么时候。"""
    if not db_path.exists():
        return []
    all_rows, _ = fetch_rows(db_path, 0.0)
    pairs = {(cell["agent"], cell["model"]) for cell in window_cells}
    last = {}
    for row in all_rows:
        key = (agent_label(row[1]), model_label(row[3], row[0], prices))
        if row[0] > last.get(key, 0.0):
            last[key] = row[0]
    out = []
    for cell in breakdown(all_rows, prices, 0.0):
        key = (cell["agent"], cell["model"])
        if key in pairs:
            continue
        out.append({"agent": key[0], "model": key[1], "tokens": cell["total"],
                    "cost": round(cell["cost"], 6), "events": cell["count"],
                    "last": last.get(key, 0.0)})
    out.sort(key=lambda item: -item["tokens"])
    return out


def models_seen(rows, prices=None):
    """本机所有出现过的模型 + 出现在哪些 Agent + 是否已定价。"""
    seen = {}
    for row in rows:
        model = model_label(row[3], row[0], prices)
        item = seen.setdefault(model, {"model": model, "tokens": 0, "agents": set()})
        item["tokens"] += row[9]
        item["agents"].add(agent_label(row[1]))
    return seen


def money(value, symbol="$"):
    if value <= 0:
        return symbol + "0"
    if value < 0.01:
        return symbol + "%.3f" % value
    if value >= 100:
        return symbol + "%.0f" % value
    return symbol + "%.2f" % value


def money_exact(value, symbol="¥"):
    """固定两位小数，负数写成 -¥2.00（手工补记/合计用）。"""
    sign = "-" if value < 0 else ""
    return "%s%s%.2f" % (sign, symbol, abs(value))


def report(db_path, days, group_by):
    if not db_path.exists():
        print("还没有数据，先运行：python3 tokenlensbar.py scan")
        return
    since = 0.0 if days <= 0 else time.time() - days * 86400
    rows, sources = fetch_rows(db_path, since)
    prices = load_prices()
    symbol = prices.get("symbol") or "$"

    if not rows:
        print("最近 %d 天没有记录。" % days)
        return

    agg = aggregate(rows, prices, since)
    totals = agg["totals"]
    groups = {"model": agg["by_model"], "agent": agg["by_agent"],
              "provider": agg["by_provider"]}[group_by]

    print()
    print("TokenLensBar · 本地用量报告")
    window = "全部历史" if days <= 0 else "最近 %d 天" % days
    print("  窗口     %s（%s → %s）" % (window, fmt_ts(rows[0][0]), fmt_ts(rows[-1][0])))
    print("  事件数   %s" % format(len(rows), ","))
    print("  总 token %s" % human(totals["total"]))
    print("    ├ 输入     %s（其中缓存命中 %s，命中率 %.1f%%）" % (
        human(totals["input"]), human(totals["cache_read"]),
        100.0 * totals["cache_read"] / totals["input"] if totals["input"] else 0.0))
    print("    ├ 输出     %s" % human(totals["output"]))
    print("    └ 缓存写   %s" % human(totals["cache_write"]))

    label = {"model": "模型", "agent": "Agent", "provider": "厂商"}[group_by]
    header = [label, "总量", "输入", "输出", "占比", "次数", "成本"]
    widths = [max(display_width(h), 10) for h in header]

    body = []
    grand = totals["total"] or 1
    for key, bucket in sorted(groups.items(), key=lambda kv: -kv[1]["total"]):
        cells = [
            key,
            human(bucket["total"]),
            human(bucket["input"]),
            human(bucket["output"]),
            "%.1f%%" % (100.0 * bucket["total"] / grand),
            format(bucket["count"], ","),
            money(bucket["cost"], symbol),
        ]
        body.append(cells)
        for i, cell in enumerate(cells):
            widths[i] = max(widths[i], display_width(cell))

    print()
    print("  按%s" % label)
    print("  " + "  ".join(pad(h, widths[i]) for i, h in enumerate(header)))
    print("  " + "  ".join("-" * widths[i] for i in range(len(header))))
    for cells in body:
        print("  " + "  ".join(
            pad(cell, widths[i], "right" if i else "left") for i, cell in enumerate(cells)))

    if sources:
        print()
        print("  数据源   " + "  ".join("%s(%d files)" % (a, n) for a, n in sources))
    print()
    if prices:
        print("  单价表   %s（%d 个型号，%s）" % (
            PRICES_PATH.name, len(prices.get("models") or {}), prices.get("currency")))
    else:
        print("  单价表   未配置，成本按 0 计。放到 %s 即可自动生效。" % PRICES_PATH)
    if agg["unpriced"]:
        print("  ⚠ 未定价 %s" % "、".join(agg["unpriced"]))
        print("      这些模型的 token 已计入总量，但成本算不出来。补到 %s 即可。" % PRICES_PATH)
    print()


def daily_filled(items, days, now=None):
    """最近 N 天，缺的日子补 0；token 和金额一起返回。"""
    buckets = {}
    for item in items:
        day = datetime.fromtimestamp(item["ts"]).strftime("%Y-%m-%d")
        bucket = buckets.setdefault(day, {"tokens": 0.0, "cost": 0.0})
        bucket["tokens"] += item["total"]
        bucket["cost"] += item["cost"]
    today = datetime.fromtimestamp(time.time() if now is None else now).replace(
        hour=0, minute=0, second=0, microsecond=0)
    out = []
    for offset in range(days - 1, -1, -1):
        day = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        bucket = buckets.get(day, {"tokens": 0.0, "cost": 0.0})
        out.append({"date": day, "tokens": bucket["tokens"],
                    "cost": round(bucket["cost"], 6)})
    return out


def enrich(rows, prices):
    """一次遍历把每条事件的成本算好（峰谷 + 单价），后面只做加总。"""
    out = []
    for row in rows:
        cost, matched, stored = row_cost(row, prices)
        out.append({
            "ts": row[0],
            "agent": agent_label(row[1]),
            "provider": row[2] or "(未记录)",
            "model": model_label(row[3], row[0], prices),
            "input": row[4] or 0,
            "output": row[5] or 0,
            "cache_read": row[6] or 0,
            "total": row[9] or 0,
            "cost": cost,
            "unpriced": (not stored and matched is None and (row[4] or row[5])),
        })
    return out


# --------------------------------------------------------------------------
# 日期工具
# --------------------------------------------------------------------------

def _day_key(value):
    """把 YYYY-MM-DD 归一化；不合法返回 None。"""
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def status_json(db_path, prices=None, now=None, config_path=None):
    """给菜单栏用的结构化摘要。成本在这里即时算，不落库。"""
    prices = prices if prices is not None else load_prices()
    symbol = prices.get("symbol") or "$"
    if not db_path.exists():
        return {"ok": False, "symbol": symbol, "reason": "no-database"}

    now = time.time() if now is None else now
    # 面板要显示"全部历史"，所以这里取全量（几千行，一次遍历几十毫秒）
    rows, _ = fetch_rows(db_path, 0)
    if not rows:
        return {"ok": False, "symbol": symbol, "reason": "no-events"}

    items = enrich(rows, prices)
    today_start = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0,
                                                      microsecond=0).timestamp()
    week_start = now - 7 * 86400
    month_start = now - 30 * 86400

    def window(since):
        tokens = cost = 0.0
        count = 0
        for item in items:
            if item["ts"] < since:
                continue
            tokens += item["total"]
            cost += item["cost"]
            count += 1
        return {"tokens": tokens, "cost": round(cost, 6), "events": count}

    windows = {
        "today": window(today_start),
        "week": window(week_start),
        "month": window(month_start),
    }

    def group(since, key):
        buckets = {}
        for item in items:
            if item["ts"] < since:
                continue
            name = item[key]
            bucket = buckets.setdefault(name, {"tokens": 0.0, "cost": 0.0})
            bucket["tokens"] += item["total"]
            bucket["cost"] += item["cost"]
        return buckets

    subscription_models = prices.get("subscription") or set()
    agent_models = {}
    for item in items:
        agent_models.setdefault(item["agent"], set()).add(item["model"])

    def agent_is_subscription(name):
        used = agent_models.get(name) or set()
        return bool(used) and all(model in subscription_models for model in used)

    all_models = group(0, "model")
    all_agents = group(0, "agent")
    all_total = sum(b["tokens"] for b in all_models.values()) or 1
    all_cost = sum(b["cost"] for b in all_models.values())
    today_models = group(today_start, "model")
    today_agents = group(today_start, "agent")
    today_total = sum(b["tokens"] for b in today_models.values()) or 1

    unpriced = sorted({item["model"] for item in items
                       if item["unpriced"] and item["ts"] >= week_start})
    week_input = sum(item["input"] for item in items if item["ts"] >= week_start)
    week_cache = sum(item["cache_read"] for item in items if item["ts"] >= week_start)

    try:
        balances = balance_payload(db_path, config_path=config_path)
    except Exception:
        balances = {"configured": False, "refresh_seconds": 600,
                    "providers": [], "error": "余额读取失败"}
    try:
        spend = today_spend(db_path, prices, items=items, now=now,
                            config_path=config_path)
    except Exception as exc:
        spend = {"configured": False, "day": _local_day(now), "currencies": [],
                 "manual": {"amount": 0, "tokens": 0, "entries": []},
                 "tokens": 0, "error": str(exc)}

    return {
        "ok": True,
        "symbol": symbol,
        "currency": prices.get("currency") or "USD",
        "windows": windows,
        "top_today": [
            {"model": name, "tokens": b["tokens"], "cost": round(b["cost"], 6)}
            for name, b in sorted(today_models.items(), key=lambda kv: -kv[1]["tokens"])
        ],
        # 面板「今日」页签用：今天真正用过的模型 / Agent，口径和「全部」一致
        "models_today": [
            {"model": name, "tokens": b["tokens"], "cost": round(b["cost"], 6),
             "share": round(b["tokens"] / today_total, 4),
             "subscription": name in subscription_models}
            for name, b in sorted(today_models.items(), key=lambda kv: -kv[1]["tokens"])
        ],
        "agents_today": [
            {"agent": name, "tokens": b["tokens"], "cost": round(b["cost"], 6),
             "share": round(b["tokens"] / today_total, 4),
             "subscription": agent_is_subscription(name)}
            for name, b in sorted(today_agents.items(), key=lambda kv: -kv[1]["tokens"])
        ],
        "daily": daily_filled(items, 7, now=now),
        "models": [
            {"model": name, "tokens": b["tokens"], "cost": round(b["cost"], 6),
             "share": round(b["tokens"] / all_total, 4),
             "subscription": name in subscription_models}
            for name, b in sorted(all_models.items(), key=lambda kv: -kv[1]["tokens"])
        ],
        "agents": [
            {"agent": name, "tokens": b["tokens"], "cost": round(b["cost"], 6),
             "share": round(b["tokens"] / all_total, 4),
             "subscription": agent_is_subscription(name)}
            for name, b in sorted(all_agents.items(), key=lambda kv: -kv[1]["tokens"])
        ],
        "cache_rate": round(min(1.0, week_cache / week_input) if week_input else 0.0, 4),
        "unpriced": unpriced,
        "all_tokens": all_total,
        "all_events": len(items),
        "all_cost": round(all_cost, 6),
        "today_spend": spend,
        "last_event": rows[-1][0],
        "priced": bool(prices),
        "balances": balances,
    }


def status(db_path, days_list=(1, 7, 30), now=None):
    """一行式摘要，给脚本用。"""
    prices = load_prices()
    symbol = prices.get("symbol") or "$"
    now = time.time() if now is None else now
    since = now - max(days_list) * 86400
    rows, _ = fetch_rows(db_path, since) if db_path.exists() else ([], [])
    out = []
    for days in days_list:
        start = now - days * 86400
        agg = aggregate(rows, prices, start)
        out.append({
            "days": days,
            "tokens": agg["totals"]["total"],
            "cost": round(agg["totals"]["cost"], 6),
            "events": agg["totals"]["count"],
            "unpriced": agg["unpriced"],
        })
    _ = symbol
    return out


# --------------------------------------------------------------------------
# 账户余额（可选）
# --------------------------------------------------------------------------
#
# 默认不联网：只有 balances.json 里启用 provider，并且 secrets.json 里有 Key，
# 才会在 scan 时请求对应厂商的余额接口。status --json 只读缓存，不碰网络。

class BalanceError(Exception):
    pass


class BalanceAuthError(BalanceError):
    """鉴权失败（401/403）。MiMo Cookie 过期走这里，可自动换新 Cookie 重试一次。"""


def _is_placeholder_key(value):
    text = str(value or "").strip()
    upper = text.upper()
    return (not text or upper.startswith("PASTE_") or "YOUR_" in upper
            or upper.startswith("SK-在这里") or "换成" in upper)


def load_balances_config(path=None):
    path = Path(path or BALANCES_PATH)
    if not path.exists():
        return {"configured": False, "refresh_seconds": 600,
                "providers": [], "error": None}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"configured": False, "refresh_seconds": 600,
                "providers": [], "error": "balances.json 读取失败"}
    if not isinstance(raw, dict):
        return {"configured": False, "refresh_seconds": 600,
                "providers": [], "error": "balances.json 格式不对"}
    try:
        refresh = int(raw.get("refresh_seconds") or 600)
    except (TypeError, ValueError):
        refresh = 600
    refresh = max(60, min(refresh, 86400))
    providers = []
    for entry in raw.get("providers") or []:
        if not isinstance(entry, dict) or entry.get("enabled") is False:
            continue
        ptype = str(entry.get("type") or entry.get("id") or "").strip()
        pid = str(entry.get("id") or ptype).strip()
        if not pid or not ptype:
            continue
        default_names = {"deepseek": "DeepSeek", "mimo": "Xiaomi MiMo",
                         "mimo_cookie": "Xiaomi MiMo"}
        providers.append({
            "id": pid,
            "type": ptype,
            "name": str(entry.get("name") or default_names.get(ptype, pid)),
            "url": str(entry.get("url") or "").strip(),
            "secret": str(entry.get("secret") or "").strip(),
            "enabled": entry.get("enabled", True) is not False,
            "auto_cookie": entry.get("auto_cookie", True) is not False,
            "via_browser": entry.get("via_browser", True) is not False,
        })
    return {"configured": bool(providers), "refresh_seconds": refresh,
            "providers": providers, "error": None}


def load_secrets(path=None):
    path = Path(path or SECRETS_PATH)
    if not path.exists():
        return {}, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, "secrets.json 读取失败"
    if not isinstance(data, dict):
        return {}, "secrets.json 格式不对"
    out = {}
    for name, value in data.items():
        if isinstance(value, str) and not _is_placeholder_key(value):
            out[str(name)] = value.strip()
    return out, None


def _balance_number(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _http_json(url, key=None, cookie=None, timeout=6.0,
               auth_error="Key 无效或没有权限"):
    headers = {
        "Accept": "application/json",
        "User-Agent": "TokenLensBar/1.0",
    }
    if key:
        headers["Authorization"] = "Bearer " + key
    if cookie:
        headers["Cookie"] = cookie
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(2_000_000)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise BalanceAuthError(auth_error)
        if exc.code == 429:
            raise BalanceError("请求过于频繁")
        raise BalanceError("HTTP %s" % exc.code)
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        text = str(reason).lower()
        if isinstance(reason, TimeoutError) or "timed out" in text or "timeout" in text:
            raise BalanceError("网络超时")
        raise BalanceError("网络错误")
    except TimeoutError:
        raise BalanceError("网络超时")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BalanceError("返回格式异常")


def fetch_deepseek_balance(entry, key, timeout=6.0):
    url = entry.get("url") or DEEPSEEK_BALANCE_URL
    data = _http_json(url, key, timeout=timeout)
    if not isinstance(data, dict):
        raise BalanceError("返回格式异常")
    infos = data.get("balance_infos")
    if not isinstance(infos, list):
        raise BalanceError("返回里没有 balance_infos")
    available = bool(data.get("is_available", True))
    if not infos:
        # 没有余额时有的账号会返回空数组；按 0 元处理，而不是当成解析失败。
        return [{"currency": "CNY", "total": 0.0, "granted": None,
                 "topped_up": None, "available": available}]
    items = []
    for info in infos:
        if not isinstance(info, dict):
            continue
        total = _balance_number(info.get("total_balance"))
        if total is None:
            continue
        currency = str(info.get("currency") or "CNY").strip().upper()
        items.append({
            "currency": currency,
            "total": total,
            "granted": _balance_number(info.get("granted_balance")),
            "topped_up": _balance_number(info.get("topped_up_balance")),
            "available": available,
        })
    if not items:
        raise BalanceError("返回里没有余额数据")
    return items


def _cookie_header(value):
    """把粘贴的 Cookie 头收拾干净：去掉 `Cookie:` 前缀和换行。"""
    text = " ".join(str(value or "").split())
    if text.lower().startswith("cookie:"):
        text = text[len("cookie:"):].strip()
    return text


def validate_mimo_cookie(value):
    """把 Cookie 头收拾干净并检查两个字段齐全；只返回值，不打印。"""
    header = _cookie_header(value)
    if not header:
        raise BalanceError("未配置 Cookie")
    if "serviceToken" not in header:
        raise BalanceError("Cookie 里缺少 api-platform_serviceToken")
    if "userId" not in header:
        raise BalanceError("Cookie 里缺少 userId（Cookies 表里的 userId，或控制台地址 ?userId=...）")
    return header


def _mimo_balance_from_payload(data):
    """MiMo 余额接口的返回结构（代查和 Cookie 两条路共用）。"""
    if not isinstance(data, dict):
        raise BalanceError("返回格式异常")
    payload = data.get("data")
    if isinstance(payload, list):
        payload = payload[0] if payload else None
    if not isinstance(payload, dict):
        message = data.get("message") or data.get("msg")
        if message:
            raise BalanceError(str(message))
        code = data.get("code")
        if code not in (None, 0, 200, "0", "200"):
            raise BalanceError("HTTP %s" % code)
        raise BalanceError("返回里没有余额数据")
    total = _balance_number(payload.get("balance"))
    if total is None:
        total = _balance_number(payload.get("availableBalance"))
    if total is None:
        total = _balance_number(payload.get("available_balance"))
    if total is None:
        raise BalanceError("返回里没有余额数据")
    currency = str(payload.get("currency") or "CNY").strip().upper()
    return [{"currency": currency, "total": total, "granted": None,
             "topped_up": None, "available": True}]


def fetch_mimo_balance(entry, cookie, timeout=6.0):
    """小米 MiMo 账户余额。控制台接口只认 Cookie，不认 API Key。

    需要两个值：api-platform_serviceToken（Cookie）和 userId（控制台地址里的
    ?userId=...，服务端要它，但控制台页面上不一定把它显示成 Cookie）。
    """
    header = validate_mimo_cookie(cookie)
    url = entry.get("url") or MIMO_BALANCE_URL
    data = _http_json(url, cookie=header, timeout=timeout,
                      auth_error="Cookie 已失效或缺少 userId，请重新登录后粘贴")
    return _mimo_balance_from_payload(data)


# --------------------------------------------------------------------------
# MiMo 余额的“浏览器代查”：让已登录的 Safari 标签页直接调余额接口
#
# 新版 Safari 的会话 Cookie 只活在内存里（Cookies.binarycookies 里没有它），
# 从磁盘拿不到；于是换思路——不取 Cookie，用 osascript 让 Safari 在自己的
# 已登录页面里发 XHR，会话自动带上，我们只要结果。
# 前提：Safari 开着 MiMo 控制台标签页；开发菜单勾选「允许 Apple 事件中的
# JavaScript」；系统设置里允许本程序自动化控制 Safari（首次会弹框）。
# --------------------------------------------------------------------------

MIMO_SAFARI_JS = (
    "(function(){try{var x=new XMLHttpRequest();"
    "x.open('GET','/api/v1/balance',false);x.withCredentials=true;x.send();"
    "return x.status+String.fromCharCode(10)+x.responseText;}"
    "catch(e){return 'ERR:JS '+String(e);}})()"
)
MIMO_SAFARI_SCRIPT = (
    'tell application "Safari"\n'
    '  repeat with w in windows\n'
    '    repeat with t in tabs of w\n'
    '      if (URL of t contains "xiaomimimo") then\n'
    '        return do JavaScript "%s" in t\n'
    '      end if\n'
    '    end repeat\n'
    '  end repeat\n'
    '  return "ERR:NO_TAB"\n'
    'end tell' % MIMO_SAFARI_JS
)
MIMO_SAFARI_PROBE = (
    'tell application "Safari"\n'
    '  repeat with w in windows\n'
    '    repeat with t in tabs of w\n'
    '      if (URL of t contains "xiaomimimo") then\n'
    '        return do JavaScript "1+1" in t\n'
    '      end if\n'
    '    end repeat\n'
    '  end repeat\n'
    '  return "ERR:NO_TAB"\n'
    'end tell'
)


def _safari_error_hint(err):
    text = str(err or "").strip()
    if "Apple 事件" in text or "Apple events" in text or "-1743" in text:
        return ("Safari 拒绝自动化：系统设置 → 隐私与安全性 → 自动化，"
                "允许本程序控制 Safari")
    if "JavaScript" in text or "do JavaScript" in text or "-1719" in text:
        return ("Safari 未开 JS 授权：设置 → 高级 → 显示网页开发者功能，"
                "再在「开发」菜单勾选「允许 Apple 事件中的 JavaScript」")
    tail = text.splitlines()[-1] if text else ""
    return "Safari 代查失败（%s）" % tail if tail else "Safari 代查失败"


def _run_safari_js(script, timeout=10.0):
    """在 Safari 的 MiMo 标签页里执行 JS；Safari 没开、没标签页都给明确错误。"""
    try:
        pgrep = subprocess.run(["/usr/bin/pgrep", "-x", "Safari"],
                               capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pgrep = None
    if pgrep is None or pgrep.returncode != 0:
        raise BalanceError("Safari 没在运行（浏览器代查需要 Safari 开着控制台标签页）")
    try:
        proc = subprocess.run(["/usr/bin/osascript", "-e", script],
                              capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise BalanceError("Safari 代查超时")
    except OSError:
        raise BalanceError("Safari 代查失败（osascript 不可用）")
    text = proc.stdout.decode("utf-8", "replace").strip()
    if proc.returncode != 0 or not text:
        raise BalanceError(_safari_error_hint(proc.stderr.decode("utf-8", "replace")))
    return text


def fetch_mimo_balance_via_safari(timeout=10.0):
    """浏览器代查余额：不取 Cookie，直接让已登录的 Safari 页面请求余额接口。"""
    text = _run_safari_js(MIMO_SAFARI_SCRIPT, timeout=timeout)
    if text.startswith("ERR:NO_TAB"):
        raise BalanceError("Safari 里没有打开 MiMo 控制台标签页（开着就能代查）")
    if text.startswith("ERR:JS"):
        raise BalanceError(_safari_error_hint(text[6:].strip()))
    status, _, body = text.partition("\n")
    try:
        code = int(status.strip())
    except ValueError:
        raise BalanceError("Safari 代查返回异常")
    if code in (401, 403):
        raise BalanceAuthError("Safari 里的登录会话已失效，请重新登录控制台")
    if code != 200:
        raise BalanceError("HTTP %s" % code)
    try:
        data = json.loads(body)
    except ValueError:
        raise BalanceError("返回格式异常")
    return _mimo_balance_from_payload(data)


def _fetch_mimo_items(entry, key):
    """MiMo 取数顺序：浏览器代查优先，Cookie 接口兜底；失败把两路原因都带上。"""
    notes = []
    if entry.get("via_browser", True):
        try:
            return fetch_mimo_balance_via_safari()
        except BalanceError as exc:
            notes.append(str(exc))
    try:
        return fetch_mimo_balance(entry, key)
    except BalanceAuthError as exc:
        # 保留鉴权语义（上层会换 Cookie 重试），但把代查失败的原因一起带上，
        # 免得真正的病灶被 401 的报错盖住。
        if notes:
            raise BalanceAuthError("%s；%s" % (exc, "；".join(notes)))
        raise
    except BalanceError as exc:
        if notes:
            raise BalanceError("%s；%s" % (exc, "；".join(notes)))
        raise


# --------------------------------------------------------------------------
# MiMo Cookie 自动续期：从 Safari 的 Cookie 库读新会话，写回 secrets.json
#
# Safari 的 Cookies.binarycookies 是二进制但不加密：魔数 "cook" + 若干页；
# 页内是记录偏移表 + 记录本体；记录里有四个字符串偏移（域/名/路径/值）和两个日期。
# 各 Safari 版本的字段布局有过细微差异，所以解析全程自校验：页大小之和、
# 偏移边界、字符串可打印、日期区间。认不出来就报 CookieError，走手动兜底。
# 这里只取 xiaomimimo 域指定的两个字段，值不打印、不写日志。
# --------------------------------------------------------------------------

MIMO_COOKIE_SECRET = "mimo_cookie"
MIMO_COOKIE_DOMAIN = "xiaomimimo.com"
MIMO_COOKIE_NAMES = ("api-platform_serviceToken", "userId")
SAFARI_COOKIE_FILES = (
    "~/Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies",
    "~/Library/Cookies/Cookies.binarycookies",
)
APPLE_EPOCH = 978307200  # Apple 的 2001-01-01 纪元换算到 Unix 纪元
_BC_DATE_FIELDS = ((32, 40), (40, 48), (40, 52), (32, 48))


class CookieError(Exception):
    """Cookie 自动续期失败。消息里只有原因，不带任何凭据值。"""


def _bc_cstr(buf, base, rel, limit):
    """记录内 base+rel 处的 NUL 结尾字符串。空串合法、UTF-8 高位字节合法
    （真实 Cookie 都有），只拒控制字符和越界。"""
    start = base + rel
    if rel <= 0 or start >= limit:
        return None
    end = buf.find(b"\x00", start, limit)
    if end < 0:
        return None
    raw = buf[start:end]
    if any(c < 0x20 or c == 0x7F for c in raw):
        return None
    return raw.decode("utf-8", "replace")


def _bc_date(page, off, limit, endian):
    for a, b in _BC_DATE_FIELDS:
        if off + b + 8 > limit:
            continue
        first_date = struct.unpack_from(endian + "d", page, off + a)[0]
        second_date = struct.unpack_from(endian + "d", page, off + b)[0]
        if 0 < first_date < 4102444800 and 0 < second_date < 4102444800:
            return max(first_date, second_date) + APPLE_EPOCH
    return None


def _bc_record(page, off):
    """记录本体：数字头（大小 + 四个字符串偏移）+ 四段 NUL 字符串（域/名/路径/值）。
    字节序和偏移字段位置可能有版本差异（真实文件见到 +8 和 +16 两种），
    靠“字符串全可打印 + 偏移落在记录内”验证。"""
    if off + 32 > len(page):
        return None
    for endian in ("<", ">"):
        size = struct.unpack_from(endian + "I", page, off)[0]
        limit = off + size if 32 <= size <= len(page) - off else len(page)
        for first in (8, 12, 16):
            fields = struct.unpack_from(endian + "IIII", page, off + first)
            strings = [_bc_cstr(page, off, rel, limit) for rel in fields]
            if any(text is None for text in strings):
                continue
            stamp = _bc_date(page, off, limit, endian)
            if stamp is None:
                stamp = _bc_date(page, off, limit, ">" if endian == "<" else "<")
            return {"strings": strings, "ts": stamp}
    return None


def _bc_page_cookies(page):
    """页头 = 未知字段 + 计数 + 记录偏移表；计数可能在 0 或 4，字节序也可能
    和文件头不一致（文件头大端、页内小端是老规矩），四种组合都试。"""
    for count_off in (0, 4):
        for count_endian in ("<", ">"):
            count = struct.unpack_from(count_endian + "I", page, count_off)[0]
            if not 1 <= count <= 4096:
                continue
            table_off = count_off + 4
            if table_off + 4 * count > len(page):
                continue
            for off_endian in ("<", ">"):
                offsets = struct.unpack_from(
                    off_endian + "%dI" % count, page, table_off)
                if any(off < 0 or off + 32 > len(page) for off in offsets):
                    continue
                records = []
                for off in offsets:
                    rec = _bc_record(page, off)
                    if rec is not None:
                        records.append(rec)
                # 真实 Cookie 里有空值/非 ASCII 的记录，个别认不出不拖垮整页
                if records and len(records) * 2 >= count:
                    return records
    raise CookieError("Cookie 页结构不认识")


def _bc_page_candidates(data):
    """文件头布局在 Safari 版本间有差异：页数位置、字节序、页表是长度还是偏移、
    页后有没有余量，都可能不同。这里产出所有说得通的分页方案，
    记录级自校验（_bc_page_cookies）会把错误方案全部挡掉。"""
    if len(data) < 16 or data[:4] != b"cook":
        raise CookieError("不是 Safari 的 Cookie 文件")
    out, seen = [], set()

    def emit(key, pages):
        if pages and key not in seen:
            seen.add(key)
            out.append(pages)

    for endian in ("<", ">"):
        for count_off in (4, 8, 12):
            count = struct.unpack_from(endian + "I", data, count_off)[0]
            if not 1 <= count <= 4096:
                continue
            table_off = count_off + 4
            end = table_off + 4 * count
            if end > len(data):
                continue
            table = struct.unpack_from(endian + "%dI" % count, data, table_off)
            tail = len(data) - end
            if all(0 < n <= len(data) for n in table):
                # ① 页表是长度。页可能首尾相接，也可能按 2/4/8 字节对齐、页间留填充；
                # 页后允许少量余量。切歪的方案过不了记录级校验。
                for stride in (1, 2, 4, 8):
                    cursor, pages = end, []
                    for index, n in enumerate(table):
                        pages.append(data[cursor:cursor + n])
                        step = n if index == len(table) - 1 else -(-n // stride) * stride
                        cursor += step
                    if all(len(page) == n for page, n in zip(pages, table)) and (
                            0 <= len(data) - cursor <= 4096):
                        emit((endian, count_off, "sizes+pad%d" % stride), pages)
            # ③ 页表是页偏移（首项正好是表尾，逐项递增）
            if (all(0 < n <= len(data) for n in table) and list(table) == sorted(table)
                    and table[0] == end and len(set(table)) == count):
                bounds = list(table) + [len(data)]
                pages = [data[a:b] for a, b in zip(bounds, bounds[1:])]
                if all(pages):
                    emit((endian, count_off, "offsets"), pages)
    return out


def _bc_probe(page):
    """页内结构探针：只输出数字。数字区 = 第一段可打印串之前的字节（hex）；
    串段只给（起点, 长度），不给内容——长度足够判断字段布局，且零泄露。"""
    runs = []
    i = 0
    while i < min(len(page), 400):
        if 0x20 <= page[i] <= 0x7E:
            j = i
            while j < len(page) and 0x20 <= page[j] <= 0x7E:
                j += 1
            if j < len(page) and page[j] == 0 and j - i >= 1:
                runs.append((i, j - i))
                i = j + 1
                continue
        i += 1
    head_end = runs[0][0] if runs else min(len(page), 64)
    head = page[:min(head_end, 64)]
    return ["  数字区 %d 字节：%s" % (len(head), head.hex()),
            "  串段（起点,长度）：%s" % (
                " ".join("(%d,%d)" % item for item in runs[:12]) or "（无可打印串）")]


def _bc_describe(data):
    """解析失败时的结构诊断：只含字节数和页表数字，不含任何 Cookie 值。"""
    lines = ["文件 %d 字节，头 8 字节 %s" % (len(data), data[:8].hex())]
    if data[:4] != b"cook":
        return lines
    for endian in ("<", ">"):
        for count_off in (4, 8, 12):
            count = struct.unpack_from(endian + "I", data, count_off)[0]
            if not 1 <= count <= 4096:
                continue
            end = count_off + 4 + 4 * count
            if end > len(data):
                continue
            table = struct.unpack_from(endian + "%dI" % count, data, count_off + 4)
            lines.append("  %s 页数在 %d：%d 页，页表和 %d，表后 %d 字节" % (
                "小端" if endian == "<" else "大端", count_off,
                count, sum(table), len(data) - end))
    # 再看最像的那个分页方案的页内结构（纯数字：hex 只到第一段字符串为止）
    try:
        candidates = _bc_page_candidates(data)
    except CookieError:
        return lines
    if candidates:
        for index, page in enumerate(candidates[0][:2]):
            lines.append("  页 %d（%d 字节）：" % (index, len(page)))
            lines.extend(_bc_probe(page))
    return lines


def _mimo_host_ok(domain):
    host = domain.strip().lower()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0].split(":", 1)[0].lstrip(".")
    return host == MIMO_COOKIE_DOMAIN or host.endswith("." + MIMO_COOKIE_DOMAIN)


def _cookie_roots():
    """Safari/WebKit 可能存 Cookie 的目录根。"""
    roots = []
    library = Path(os.path.expanduser("~/Library"))
    for folder in (library / "Containers", library / "Group Containers"):
        try:
            for entry in folder.iterdir():
                name = entry.name.lower()
                if "safari" in name or "webkit" in name:
                    roots.append(entry)
        except OSError:
            pass
    roots += [library / "WebKit", library / "Cookies", library / "Safari"]
    return roots


def _scan_needles(roots, needles, max_depth=10, max_size=30 * 1024 * 1024):
    """在给定目录里逐个文件找字节串：只报路径和命中的串名，不输出内容。
    用来定位名字不叫 cookie 的 Cookie 库。"""
    hits, scanned = [], 0
    for root in roots:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d.lower() not in ("caches", "cache", "mediacache")]
            if dirpath.count(os.sep) - str(root).count(os.sep) > max_depth:
                dirnames[:] = []
                continue
            for filename in filenames:
                path = Path(dirpath) / filename
                try:
                    if path.stat().st_size > max_size:
                        continue
                    data = path.read_bytes()
                except OSError:
                    continue
                scanned += 1
                matched = [needle.decode() for needle in needles if needle in data]
                if matched:
                    hits.append((path, matched))
    return hits, scanned


def _cookie_file_candidates(extra_roots=None):
    """找出可能存 Cookie 的文件：静态路径 + Safari/WebKit 相关目录下名字带 cookie 的
    文件（新版 Safari 会把 Cookie 分库放在 WebKit 容器 / 配置文件目录里）。"""
    found, seen = [], set()

    def add(path):
        path = Path(path)
        if path.is_file() and str(path) not in seen:
            seen.add(str(path))
            found.append(path)

    for raw in SAFARI_COOKIE_FILES:
        add(Path(raw).expanduser())
    env = os.environ.get("TOKENLENSBAR_SAFARI_COOKIES")
    if env:
        add(Path(env).expanduser())
    roots = [Path(raw).expanduser() for raw in (extra_roots or ())]
    if extra_roots is None:
        roots += _cookie_roots()
    for root in roots:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            if dirpath.count(os.sep) - str(root).count(os.sep) > 5:
                dirnames[:] = []
                continue
            for filename in filenames:
                if "cookie" in filename.lower():
                    add(Path(dirpath) / filename)
    return found


def _sqlite_cookie_records(path):
    """WebKit 系的 Cookie 也可能在 SQLite 里（一般是 域/名/值 三列）。
    返回和二进制库一样的记录形状；不碰网络，只读。"""
    records = []
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        tables = [row[0] for row in con.execute(
            "select name from sqlite_master where type='table'")]
        for table in tables:
            quoted = '"%s"' % table.replace('"', '""')
            cols = {row[1].lower() for row in con.execute(
                "pragma table_info(%s)" % quoted)}
            name_col = next((c for c in ("name", "key") if c in cols), None)
            value_col = next((c for c in ("value", "val") if c in cols), None)
            host_col = next((c for c in ("host", "domain", "host_key", "origin")
                             if c in cols), None)
            if not (name_col and value_col):
                continue
            query = "select %s, %s, %s from %s" % (
                '"%s"' % host_col if host_col else "''",
                '"%s"' % name_col, '"%s"' % value_col, quoted)
            for host, name, value in con.execute(query):
                records.append({"strings": [str(host or ""), str(name or ""),
                                            "/", str(value or "")], "ts": None})
    finally:
        con.close()
    return records


def _cookie_records(data, path):
    """按文件头认格式：SQLite 库或二进制 Cookie 库。"""
    if data[:16].startswith(b"SQLite format 3"):
        return _sqlite_cookie_records(path)
    return _bc_records(data)


def _mimo_picks(records):
    """从记录里挑 MiMo 的两个字段；前两段字符串里名字和域名的顺序随版式变，顺序无关。"""
    picks = {}
    for rec in records:
        strings = rec["strings"]
        head = strings[:2]
        name = next((text for text in head if text in MIMO_COOKIE_NAMES), None)
        if name is None or not any(_mimo_host_ok(text) for text in head):
            continue
        value = strings[3]
        old = picks.get(name)
        if old is None or (rec["ts"] or -1) >= (old[0] or -1):
            picks[name] = (rec["ts"], value)
    return picks


def read_safari_mimo_cookie(paths=None):
    """从各 Cookie 库里拼出 MiMo Cookie 头。值只出现在返回值里。"""
    candidates = list(paths) if paths else _cookie_file_candidates()
    problems = []
    best = None
    for path in candidates:
        path = Path(path).expanduser()
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            continue
        except PermissionError:
            raise CookieError(
                "没有权限读 Safari Cookie：系统设置 → 隐私与安全性 → 完全磁盘访问权限，"
                "给运行它的程序（终端或 TokenLensBar）授权后重试")
        except OSError as exc:
            problems.append("%s：%s" % (path.name, exc))
            continue
        try:
            fields = _mimo_picks(_cookie_records(data, path))
        except (CookieError, sqlite3.Error) as exc:
            problems.append("%s：%s" % (path.name, exc))
            continue
        missing = [n for n in MIMO_COOKIE_NAMES if n not in fields]
        if missing:
            problems.append("%s：缺 %s" % (path.name, "、".join(missing)))
            continue
        header = "api-platform_serviceToken=%s; userId=%s" % (
            fields["api-platform_serviceToken"][1], fields["userId"][1])
        stamp = fields["api-platform_serviceToken"][0] or 0
        if best is None or stamp >= best[0]:
            best = (stamp, header)
    if best is not None:
        return best[1]
    raise CookieError("没找到 %s 的登录会话%s" % (
        MIMO_COOKIE_DOMAIN, "（%s）" % "；".join(problems) if problems else
        "（可能已退出登录，请重新登录控制台）"))


def _bc_records(data):
    """解析整个二进制 Cookie 库的所有记录。个别怪页/怪记录跳过，取认出记录最多的布局。"""
    best = []
    for pages in _bc_page_candidates(data):
        got = []
        for page in pages:
            try:
                got.extend(_bc_page_cookies(page))
            except CookieError:
                continue  # 个别怪页不拖垮整个文件
        if len(got) > len(best):
            best = got
    return best


def _mimo_cookie_fields(data):
    """解析一份二进制 Cookie 库，返回 {字段名: (时间戳, 值)}，只收 MiMo 的两个字段。"""
    records = _bc_records(data)
    if not records:
        raise CookieError("Cookie 结构不认识（Safari 改版？）")
    return _mimo_picks(records)


def _bc_domain_summary(records):
    """只列域名和 Cookie 名（都不是秘密），用来判断“这文件里到底有没有 MiMo 会话”。"""
    domains, mimo_names = [], []
    for rec in records:
        head = rec["strings"][:2]
        for text in head:
            if "." in text and "://" not in text and len(text) < 80 and text not in domains:
                domains.append(text)
        if any(_mimo_host_ok(text) for text in head):
            for text in head:
                if not _mimo_host_ok(text) and text not in mimo_names:
                    mimo_names.append(text)
    lines = ["  文件里的域（%d 个，只列前 12）：%s" % (
        len(domains), "、".join(domains[:12]) or "（无）")]
    if mimo_names:
        lines.append("  mimo 相关 Cookie 名：%s" % "、".join(mimo_names[:12]))
    return lines


def mimo_cookie_secret_name(config_path=None):
    config = load_balances_config(config_path)
    for entry in config["providers"]:
        if entry.get("type") in ("mimo", "mimo_cookie"):
            return entry.get("secret") or entry["id"]
    return MIMO_COOKIE_SECRET


def save_mimo_cookie(header, secrets_path=None, secret_name=None):
    """把 Cookie 写回 secrets.json：原子替换、600 权限、不动其它键。不打印值。"""
    path = Path(secrets_path or SECRETS_PATH)
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise CookieError("%s 读不了，先修好再自动更新" % path.name)
        if not isinstance(data, dict):
            raise CookieError("%s 格式不对，先修好再自动更新" % path.name)
    data[secret_name or MIMO_COOKIE_SECRET] = header
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def refresh_mimo_cookie(secrets_path=None, secret_name=None, paths=None):
    """自动续期：Safari 取新 Cookie → 校验 → 写回 secrets.json，返回新 Cookie 头。"""
    header = validate_mimo_cookie(read_safari_mimo_cookie(paths))
    save_mimo_cookie(header, secrets_path, secret_name)
    return header


def _provider_key(secrets, entry):
    key = secrets.get(entry.get("secret") or entry["id"])
    if not key and entry["type"] == "deepseek":
        key = os.environ.get("DEEPSEEK_API_KEY")
    if not key and entry["type"] in ("mimo", "mimo_cookie"):
        key = os.environ.get("MIMO_COOKIE")
    return (key or "").strip()


def refresh_balances(db_path, force=False, verbose=False, config_path=None,
                     secrets_path=None, fetcher=None, now=None,
                     cookie_refresher=None):
    """按需刷新余额。配置缺失/没 Key 时只写错误状态，不发请求。"""
    config = load_balances_config(config_path)
    result = {"configured": config["configured"], "refreshed": [],
              "skipped": [], "errors": []}
    if not config["configured"]:
        return result

    secrets, secret_error = load_secrets(secrets_path)
    con = connect(db_path)
    now = time.time() if now is None else now
    # 升级到“余额历史”版本时，balances 表里已有最近一次余额；先补成样本，
    # 这样今天第一次刷新就能和它算出差额，而不是从 0 开始。
    for row in con.execute(
            "select provider, currency, total, granted, topped_up, available, fetched_at"
            " from balances"):
        if row[6] and row[5] and row[2] is not None:
            con.execute(
                "insert or ignore into balance_samples"
                " (provider, currency, ts, total, granted, topped_up, available)"
                " values (?,?,?,?,?,?,1)",
                (row[0], row[1], row[6], row[2], row[3], row[4]))
    con.commit()
    for entry in config["providers"]:
        provider = entry["id"]
        state = con.execute(
            "select last_attempt, last_success, error from balance_state where provider=?",
            (provider,)).fetchone()
        last_attempt = (state[0] or 0) if state else 0
        last_success = state[1] if state else None
        if not force and last_attempt and now - last_attempt < config["refresh_seconds"]:
            result["skipped"].append(provider)
            continue

        key = _provider_key(secrets, entry)
        # 浏览器代查不需要 Key；开着代查时没配 Cookie 也照常取数。
        browser_ready = (entry["type"] in ("mimo", "mimo_cookie")
                         and entry.get("via_browser", True))
        if not key and not browser_ready:
            missing = ("未配置 Cookie" if entry["type"] in ("mimo", "mimo_cookie")
                       else "未配置 Key")
            message = secret_error or missing
            con.execute(
                "insert or replace into balance_state"
                " (provider, last_attempt, last_success, error) values (?,?,?,?)",
                (provider, now, last_success, message))
            con.commit()
            result["errors"].append((provider, message))
            if verbose:
                print("  ! %s 余额：%s" % (entry["name"], message))
            continue

        def fetch_items(entry_key):
            if fetcher is not None:
                got = fetcher(entry, entry_key)
            elif entry["type"] == "deepseek":
                got = fetch_deepseek_balance(entry, entry_key)
            elif entry["type"] in ("mimo", "mimo_cookie"):
                got = _fetch_mimo_items(entry, entry_key)
            else:
                raise BalanceError("暂不支持 %s" % entry["type"])
            if not got:
                raise BalanceError("没有返回余额")
            return got

        items, message = None, None
        try:
            items = fetch_items(key)
        except BalanceAuthError as exc:
            # MiMo 的 Cookie 是短效会话，过期是常态：自动换新 Cookie 重试一次。
            # 自动换新失败不影响面板——保留旧值，把原因记进 balance_state。
            if (entry["type"] not in ("mimo", "mimo_cookie")
                    or not entry.get("auto_cookie", True)):
                message = str(exc)
            else:
                refresher = cookie_refresher or (
                    lambda: refresh_mimo_cookie(
                        secrets_path=secrets_path,
                        secret_name=entry.get("secret") or MIMO_COOKIE_SECRET))
                try:
                    fresh = refresher()
                except Exception as ref_exc:
                    message = ("%s；自动换新 Cookie 失败（%s）。手动兜底："
                               "python3 tokenlensbar.py cookie --from-clipboard"
                               % (exc, ref_exc))
                else:
                    try:
                        items = fetch_items(fresh)
                    except Exception as exc2:
                        message = "自动换新 Cookie 后仍失败：%s" % exc2
        except BalanceError as exc:
            message = str(exc)
        except Exception:
            message = "刷新异常"
        if items is None:
            con.execute(
                "insert or replace into balance_state"
                " (provider, last_attempt, last_success, error) values (?,?,?,?)",
                (provider, now, last_success, message))
            con.commit()
            result["errors"].append((provider, message))
            if verbose:
                print("  ! %s 余额：%s" % (entry["name"], message))
            continue

        con.execute("delete from balances where provider=?", (provider,))
        for item in items:
            currency = item.get("currency") or "CNY"
            total = _balance_number(item.get("total"))
            available = bool(item.get("available", True))
            con.execute(
                "insert or replace into balances"
                " (provider, currency, total, granted, topped_up, available, fetched_at)"
                " values (?,?,?,?,?,?,?)",
                (provider, currency, float(total or 0),
                 item.get("granted"), item.get("topped_up"),
                 1 if available else 0, now))
            # 只有“确实拿到了余额”的样本才进入历史；接口不可用/空数不能当成 0，
            # 否则第二天的余额差会凭空多出一大笔“消费”。
            if available and total is not None:
                con.execute(
                    "insert or replace into balance_samples"
                    " (provider, currency, ts, total, granted, topped_up, available)"
                    " values (?,?,?,?,?,?,1)",
                    (provider, currency, now, float(total),
                     item.get("granted"), item.get("topped_up")))
        con.execute(
            "insert or replace into balance_state"
            " (provider, last_attempt, last_success, error) values (?,?,?,?)",
            (provider, now, now, None))
        con.commit()
        result["refreshed"].append(provider)
        if verbose:
            print("  ✓ %s 余额已更新" % entry["name"])
    con.close()
    return result


def balance_payload(db_path, config_path=None, now=None):
    """给菜单栏用的余额缓存。只读 SQLite，不发网络请求。"""
    config = load_balances_config(config_path)
    if not config["configured"]:
        return {"configured": False, "refresh_seconds": config["refresh_seconds"],
                "providers": []}
    con = connect(db_path)
    now = time.time() if now is None else now
    providers = []
    for entry in config["providers"]:
        rows = con.execute(
            "select currency, total, granted, topped_up, available, fetched_at"
            " from balances where provider=? order by currency", (entry["id"],)).fetchall()
        state = con.execute(
            "select last_attempt, last_success, error from balance_state where provider=?",
            (entry["id"],)).fetchone()
        fetched_at = max((row[5] for row in rows), default=None)
        if fetched_at is None and state:
            fetched_at = state[1]
        error = state[2] if state else None
        providers.append({
            "id": entry["id"],
            "name": entry["name"],
            "type": entry["type"],
            "items": [{
                "currency": row[0],
                "total": row[1],
                "granted": row[2],
                "topped_up": row[3],
                "available": bool(row[4]),
            } for row in rows],
            "fetched_at": fetched_at,
            "last_attempt": state[0] if state else None,
            "error": error,
            "stale": bool(fetched_at and now - fetched_at > config["refresh_seconds"] * 2),
        })
    con.close()
    return {"configured": True, "refresh_seconds": config["refresh_seconds"],
            "providers": providers}


# --------------------------------------------------------------------------
# 今日消费：余额下降 + 跨天会话归因 + 手动补记
# --------------------------------------------------------------------------

def _local_day(ts=None):
    return datetime.fromtimestamp(time.time() if ts is None else ts).strftime("%Y-%m-%d")


def _day_bounds(day):
    start = datetime.strptime(day, "%Y-%m-%d").timestamp()
    return start, start + 86400


def _currency_symbol(currency):
    code = (currency or "CNY").upper()
    return {"CNY": "¥", "USD": "$"}.get(code, code + " ")


def add_manual_entry(amount=None, tokens=None, agent=None, model=None,
                     day=None, db_path=None, now=None):
    """补记一条手动消费/Token。金额和 Token 至少填一个。"""
    day = _day_key(day) if day else _local_day(now)
    if day is None:
        raise ValueError("日期格式应为 YYYY-MM-DD")
    amount_value = None
    if amount is not None and str(amount).strip() != "":
        try:
            amount_value = float(amount)
        except (TypeError, ValueError):
            raise ValueError("金额必须是数字")
        if not math.isfinite(amount_value):
            raise ValueError("金额必须是有限数字")
    tokens_value = None
    if tokens is not None and str(tokens).strip() != "":
        try:
            tokens_value = int(tokens)
        except (TypeError, ValueError):
            raise ValueError("Token 必须是整数")
        if tokens_value < 0:
            raise ValueError("Token 不能是负数")
    if amount_value is None and tokens_value is None:
        raise ValueError("金额和 Token 至少填一个")
    agent = (str(agent).strip() if agent else "") or None
    model = (str(model).strip() if model else "") or None
    con = connect(db_path or DEFAULT_DB)
    cur = con.execute(
        "insert into manual_entries (day, created_at, amount, tokens, agent, model)"
        " values (?,?,?,?,?,?)",
        (day, time.time() if now is None else now, amount_value,
         tokens_value, agent, model))
    con.commit()
    entry_id = cur.lastrowid
    con.close()
    return entry_id


def manual_entries_for(db_path, day=None, now=None):
    day = _day_key(day) if day else _local_day(now)
    if day is None:
        return []
    con = connect(db_path)
    rows = con.execute(
        "select id, created_at, amount, tokens, agent, model"
        " from manual_entries where day=? order by id", (day,)).fetchall()
    con.close()
    return [{
        "id": row[0],
        "created_at": row[1],
        "amount": row[2],
        "tokens": row[3],
        "agent": row[4],
        "model": row[5],
    } for row in rows]


def delete_manual_entry(entry_id, db_path=None):
    con = connect(db_path or DEFAULT_DB)
    cur = con.execute("delete from manual_entries where id=?", (int(entry_id),))
    con.commit()
    deleted = cur.rowcount
    con.close()
    return deleted > 0


def _provider_aliases(entry):
    aliases = {
        str(entry.get("id") or "").lower(),
        str(entry.get("type") or "").lower(),
        str(entry.get("name") or "").lower(),
    }
    ptype = str(entry.get("type") or "").lower()
    if ptype == "deepseek":
        aliases.add("deepseek")
    if ptype in ("mimo", "mimo_cookie"):
        aliases.update({"xiaomi", "mimo", "xiaomi mimo"})
    return {item for item in aliases if item}


def _event_matches_provider(item, entry):
    aliases = _provider_aliases(entry)
    provider = (item.get("provider") or "").lower()
    if provider and provider not in ("(未记录)", "unknown", "unknown-provider"):
        for alias in aliases:
            if alias == provider or alias in provider or provider in alias:
                return True
    model = (item.get("model") or "").lower()
    if "deepseek" in aliases and "deepseek" in model:
        return True
    if "mimo" in aliases and ("mimo" in model or "xiaomi" in model):
        return True
    return False


def _gap_weights(items, entry, gap_start, gap_end):
    """跨天缺口里的消费按“会话结束日”归到日历日。

    Hermes 的事件时间已经是会话/任务的 last_seen（远端老库是 ended_at），
    所以这里不需要再拆会话；每个会话整段算到它结束的那一天。
    """
    weights = {}
    for item in items or []:
        ts = item.get("ts") or 0
        if not (gap_start < ts <= gap_end):
            continue
        if not _event_matches_provider(item, entry):
            continue
        day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        weight = item.get("cost") or 0.0
        if weight <= 0:
            weight = float(item.get("total") or 1.0)
        weights[day] = weights.get(day, 0.0) + weight
    return weights


def today_spend(db_path, prices=None, items=None, now=None, config_path=None):
    """今日消费：余额下降为金额，跨天缺口按日志的会话结束日归因。

    余额栏本身不改；这里只给「今日消费」提供结构化数据。
    """
    prices = prices if prices is not None else load_prices()
    now = time.time() if now is None else now
    day = _local_day(now)
    day_start, day_end = _day_bounds(day)
    day_end = min(day_end, now + 1.0)
    primary_currency = (prices.get("currency") or "CNY").upper()
    config = load_balances_config(config_path)
    entries = manual_entries_for(db_path, day, now=now) if db_path.exists() else []
    manual_amount = sum(float(row["amount"] or 0) for row in entries)
    manual_tokens = sum(int(row["tokens"] or 0) for row in entries)

    series = {}
    if config["configured"] and db_path.exists():
        con = connect(db_path)
        for entry in config["providers"]:
            rows = con.execute(
                "select currency, ts, total from balance_samples"
                " where provider=? and available=1 and ts>=? and ts<=?"
                " order by currency, ts",
                (entry["id"], day_start, day_end)).fetchall()
            for currency, ts, total in rows:
                series.setdefault((entry["id"], currency), []).append((ts, total))
        # 每个 provider 即使今天还没样本，也要出现在结果里
        for entry in config["providers"]:
            existing = {currency for (pid, currency) in series if pid == entry["id"]}
            latest = con.execute(
                "select distinct currency from balances where provider=?", (entry["id"],)).fetchall()
            for (currency,) in latest:
                if currency not in existing:
                    series[(entry["id"], currency)] = []
        con.close()

    currency_buckets = {}
    provider_info = {entry["id"]: entry for entry in config["providers"]}
    for (pid, currency), samples in series.items():
        entry = provider_info.get(pid) or {"id": pid, "name": pid, "type": pid}
        bucket = currency_buckets.setdefault(currency, {
            "currency": (currency or "CNY").upper(),
            "symbol": _currency_symbol(currency),
            "automatic": None,
            "manual": 0.0,
            "total": 0.0,
            "providers": [],
        })
        spent = None
        start_ts = None
        gap_share = 0.0
        if samples:
            samples.sort(key=lambda item: item[0])
            start_ts = samples[0][0]
            spent = 0.0
            for previous, current in zip(samples, samples[1:]):
                spent += max(0.0, previous[1] - current[1])
            con = connect(db_path)
            previous = con.execute(
                "select ts, total from balance_samples"
                " where provider=? and currency=? and available=1 and ts<?"
                " order by ts desc limit 1", (pid, currency, day_start)).fetchone()
            con.close()
            if previous and previous[1] > samples[0][1]:
                gap_drop = previous[1] - samples[0][1]
                weights = _gap_weights(items, entry, previous[0], samples[0][0])
                total_weight = sum(weights.values())
                if total_weight > 0:
                    gap_share = gap_drop * weights.get(day, 0.0) / total_weight
                    spent += gap_share
        bucket["providers"].append({
            "id": pid,
            "name": entry.get("name") or pid,
            "currency": bucket["currency"],
            "spent": None if spent is None else round(spent, 6),
            "start_ts": start_ts,
            "samples": len(samples),
            "gap": round(gap_share, 6),
        })
        if spent is not None:
            bucket["automatic"] = round((bucket["automatic"] or 0.0) + spent, 6)
        bucket["total"] = round((bucket["automatic"] or 0.0), 6)

    # 手动补记统一记到主币种（通常是 CNY）
    if entries or manual_amount:
        bucket = currency_buckets.setdefault(primary_currency, {
            "currency": primary_currency,
            "symbol": _currency_symbol(primary_currency),
            "automatic": None,
            "manual": 0.0,
            "total": 0.0,
            "providers": [],
        })
        bucket["manual"] = round(manual_amount, 6)
        bucket["total"] = round((bucket["automatic"] or 0.0) + manual_amount, 6)

    currencies = sorted(currency_buckets.values(),
                        key=lambda item: (item["currency"] != primary_currency,
                                          item["currency"]))
    return {
        "configured": bool(config["configured"]),
        "day": day,
        "currencies": currencies,
        "manual": {
            "amount": round(manual_amount, 6),
            "tokens": manual_tokens,
            "entries": entries,
        },
        "tokens": manual_tokens,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _cookie_check():
    """只读体检：配置、Safari 可读性、找到的字段名、余额状态。不显示任何值。"""
    status = 0
    config = load_balances_config()
    entries = [e for e in config["providers"]
               if e.get("type") in ("mimo", "mimo_cookie")]
    if not entries:
        print("balances.json 里没有 MiMo 这条 provider。")
        return 1
    entry = entries[0]
    print("余额配置：%s（enabled=%s，auto_cookie=%s，via_browser=%s）" % (
        entry["name"], entry.get("enabled", True) is not False,
        entry.get("auto_cookie", True) is not False,
        entry.get("via_browser", True) is not False))
    if entry.get("via_browser", True):
        try:
            probe = _run_safari_js(MIMO_SAFARI_PROBE, timeout=8)
        except BalanceError as exc:
            probe = "ERR:JS %s" % exc
        if probe.startswith("ERR:NO_TAB"):
            print("Safari 代查：没找到 MiMo 控制台标签页")
            status = 1
        elif probe.startswith("ERR:JS"):
            print("Safari 代查：%s" % probe[6:].strip())
            status = 1
        else:
            print("Safari 代查：正常（控制台标签页在、JS 授权在）")
    try:
        stored = json.loads(Path(SECRETS_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    name = entry.get("secret") or entry["id"]
    have_key = isinstance(stored, dict) and bool(stored.get(name))
    print("secrets.json：%s（值不显示）" % ("已配置" if have_key else "未配置"))
    if not have_key:
        status = 1
    candidates = _cookie_file_candidates()
    if not candidates:
        print("没发现任何 Cookie 文件。")
        return 1
    irrelevant, got_all = 0, False
    for path in candidates:
        try:
            blob = path.read_bytes()
        except PermissionError:
            print("Cookie 库：%s —— 无权限读（需要完全磁盘访问权限）" % path)
            status = 1
            continue
        except OSError as exc:
            print("Cookie 库：%s —— 读不了（%s）" % (path, exc))
            status = 1
            continue
        has_needle = (b"xiaomimimo" in blob
                      or b"api-platform_serviceToken" in blob)
        try:
            records = _cookie_records(blob, path)
        except (CookieError, sqlite3.Error) as exc:
            if has_needle:
                print("Cookie 库：%s —— %s（原始字节里有 MiMo 字样，格式待适配）" % (
                    path, exc))
                for line in _bc_describe(blob):
                    print("  %s" % line)
                status = 1
            else:
                irrelevant += 1
            continue
        fields = _mimo_picks(records)
        found = "、".join(n for n in MIMO_COOKIE_NAMES if n in fields)
        missing = "、".join(n for n in MIMO_COOKIE_NAMES if n not in fields)
        if not found and not has_needle:
            irrelevant += 1
            continue
        print("Cookie 库：%s —— %d 条记录，找到 %s%s" % (
            path, len(records), found or "（没有 MiMo 字段）",
            "，缺 %s" % missing if missing else ""))
        for needle in (b"xiaomimimo", b"api-platform_serviceToken"):
            print("  原始字节里含 %s：%s" % (
                needle.decode(), "是" if needle in blob else "否"))
        if missing:
            status = 1
            for line in _bc_domain_summary(records):
                print(line)
        else:
            got_all = True
    if irrelevant:
        print("另有 %d 个 Cookie 文件与 MiMo 无关（已略过）" % irrelevant)
    if not got_all:
        status = 1
        print("深度扫描（只报路径和命中的串名，不报内容）：")
        hits, scanned = _scan_needles(
            _cookie_roots(), (b"xiaomimimo", b"api-platform_serviceToken"))
        print("  扫了 %d 个文件，命中 %d 处：" % (scanned, len(hits)))
        for path, matched in hits[:30]:
            print("  含 %s：%s" % ("、".join(matched), path))
        if len(hits) > 30:
            print("  …… 另有 %d 处未列出" % (len(hits) - 30))
        shown_dirs = set()
        for path, matched in hits:
            parent = path.parent
            if "websitedata" not in str(parent).lower() or parent in shown_dirs:
                continue
            if len(shown_dirs) >= 4:
                break
            shown_dirs.add(parent)
            listing = []
            try:
                for child in sorted(parent.iterdir()):
                    try:
                        size = child.stat().st_size
                        listing.append("%s(%d)" % (child.name, size)
                                       if child.is_file() else "%s(目录)" % child.name)
                    except OSError:
                        listing.append("%s(不可读)" % child.name)
            except OSError:
                continue
            print("  站点目录 %s 里有：%s" % (parent.name[:20] + "…",
                                          "、".join(listing)))
    try:
        for provider in balance_payload(DEFAULT_DB)["providers"]:
            if provider["id"] == entry["id"]:
                stamp = (datetime.fromtimestamp(provider["fetched_at"])
                         .strftime("%m-%d %H:%M") if provider["fetched_at"]
                         else "从未成功")
                print("余额状态：上次成功 %s%s" % (
                    stamp, "；错误：%s" % provider["error"] if provider["error"] else ""))
    except Exception:
        pass
    return status


def cmd_cookie(args):
    """MiMo 余额 Cookie：默认从 Safari 自动续期，--from-clipboard 剪贴板兜底。"""
    if getattr(args, "check", False):
        return _cookie_check()
    try:
        if getattr(args, "from_clipboard", False):
            proc = subprocess.run(["pbpaste"], capture_output=True, check=False)
            header = validate_mimo_cookie(proc.stdout.decode("utf-8", "replace"))
            save_mimo_cookie(header,
                             secret_name=mimo_cookie_secret_name())
        else:
            header = refresh_mimo_cookie(
                secret_name=mimo_cookie_secret_name())
    except (CookieError, BalanceError) as exc:
        print("Cookie 更新失败：%s" % exc)
        return 1
    print("MiMo Cookie 已更新（内容不显示）。点「立即采集」或 "
          "python3 tokenlensbar.py balance --refresh 生效。")
    return 0


def cmd_manual(args):
    """手动补记：金额、Token、Agent、Model 都可选，但金额和 Token 至少一个。"""
    action = getattr(args, "manual_action", None) or "list"
    day = getattr(args, "date", None)
    json_output = getattr(args, "json", False)
    if day and _day_key(day) is None:
        print("日期格式应为 YYYY-MM-DD，例如 2026-10-25")
        return 2

    if action == "add":
        try:
            entry_id = add_manual_entry(
                amount=args.amount, tokens=args.tokens,
                agent=args.agent, model=args.model, day=day)
        except (OSError, ValueError) as exc:
            print("补记失败：%s" % exc)
            return 1
        print("已补记 #%d" % entry_id)
        return 0

    if action == "delete":
        try:
            deleted = delete_manual_entry(args.id)
        except (OSError, ValueError) as exc:
            print("删除失败：%s" % exc)
            return 1
        print("已删除 #%d" % args.id if deleted else "没有这条记录")
        return 0

    entries = manual_entries_for(DEFAULT_DB, day)
    if json_output:
        print(json.dumps(entries, ensure_ascii=False))
        return 0
    if not entries:
        print("今天还没有手动补记。用法：tokenlensbar.py manual add --amount 2")
        return 0
    symbol = (load_prices() or {}).get("symbol") or "¥"
    print()
    for entry in entries:
        parts = []
        if entry["amount"] is not None:
            parts.append(money_exact(entry["amount"], symbol))
        if entry["tokens"] is not None:
            parts.append("%s tokens" % format(entry["tokens"], ","))
        if entry["agent"]:
            parts.append(entry["agent"])
        if entry["model"]:
            parts.append(entry["model"])
        print("  #%-4d %s" % (entry["id"], "  ".join(parts)))
    print()
    return 0


def cmd_sources(_args):
    print()
    for adapter in ADAPTERS:
        if getattr(adapter, "virtual", False):
            print("%s  [%s]  ssh %s" % (agent_label(adapter.name), adapter.mode,
                                        adapter.ssh_target))
            continue
        paths = sorted(set(adapter.discover()))
        print("%s  [%s]  %d 个文件" % (agent_label(adapter.name), adapter.mode, len(paths)))
        for path in paths[:3]:
            print("    %s" % path)
        if len(paths) > 3:
            print("    … 另外 %d 个" % (len(paths) - 3))
    print()
    print("采集库：%s" % DEFAULT_DB)
    print()


def load_all_rows(db_path):
    since = time.time() - 3650 * 86400
    rows, sources = fetch_rows(db_path, since)
    return rows, sources


def cmd_models(_args):
    if not DEFAULT_DB.exists():
        print("还没有数据，先运行：python3 tokenlensbar.py scan")
        return
    rows, _ = load_all_rows(DEFAULT_DB)
    prices = load_prices()
    seen = models_seen(rows)
    print()
    print("  本机出现过的模型（全部历史，不只最近）")
    print()
    header = ["模型", "总量", "出现在", "定价"]
    widths = [max(display_width(h), 6) for h in header]
    body = []
    for model, item in sorted(seen.items(), key=lambda kv: -kv[1]["tokens"]):
        name, _version = resolve_rate(model, time.time(), prices)
        cells = [model, human(item["tokens"]), "、".join(sorted(item["agents"])),
                 name or "未定价"]
        body.append(cells)
        for i, cell in enumerate(cells):
            widths[i] = max(widths[i], display_width(cell))
    print("  " + "  ".join(pad(h, widths[i]) for i, h in enumerate(header)))
    print("  " + "  ".join("-" * widths[i] for i in range(len(header))))
    for cells in body:
        print("  " + "  ".join(pad(c, widths[i], "right" if i == 1 else "left")
                               for i, c in enumerate(cells)))
    print()


def cmd_reset(args):
    """清掉某个来源的数据和文件游标，下次 scan 会重新解析。"""
    if not DEFAULT_DB.exists():
        print("还没有数据。")
        return
    con = connect(DEFAULT_DB)
    agents = [args.agent] if args.agent else [a.name for a in ADAPTERS]
    for name in agents:
        events = con.execute("select count(*) from events where agent=?", (name,)).fetchone()[0]
        con.execute("delete from events where agent=?", (name,))
        con.execute("delete from files where agent=?", (name,))
        print("已清除 %s：%d 条事件（下次 scan 会重扫）" % (name, events))
    con.commit()
    con.close()


def cmd_health(_args):
    """体检：数据源还在不在、有没有可能静默停止解析。"""
    if not DEFAULT_DB.exists():
        print("还没有数据，先运行：python3 tokenlensbar.py scan")
        return
    con = connect(DEFAULT_DB)
    stats = {row[0]: (row[1], row[2]) for row in con.execute(
        "select agent, max(ts), count(*) from events group by agent")}
    con.close()

    now = time.time()
    print()
    print("  数据源体检")
    print()
    header = ["来源", "文件", "事件", "最后一条", "状态"]
    widths = [max(display_width(h), 6) for h in header]
    body = []
    problems = []

    for adapter in ADAPTERS:
        virtual = getattr(adapter, "virtual", False)
        paths = [] if virtual else sorted(set(adapter.discover()))
        last_ts, count = stats.get(adapter.name, (0.0, 0))
        if virtual:
            if count == 0:
                state = "没拉到数据"
                problems.append("%s：一条都没拉到（检查 ssh 免密和远端 ~/.hermes/state.db）"
                                % agent_label(adapter.name))
            else:
                age = (now - last_ts) / 86400.0
                state = "正常" if age <= 30 else "%.0f 天无新数据" % age
                if age > 30:
                    problems.append("%s：最后一条在 %.0f 天前" % (agent_label(adapter.name), age))
            cells = [agent_label(adapter.name), "ssh", format(count, ","),
                     fmt_ts(last_ts) if last_ts else "—", state]
            body.append(cells)
            for i, cell in enumerate(cells):
                widths[i] = max(widths[i], display_width(cell))
            continue
        if not paths:
            state = "未发现数据源"
            problems.append("%s：一个文件都没找到（没装？路径变了？）" % adapter.name)
        elif count == 0:
            state = "有文件但零事件"
            problems.append("%s：%d 个文件存在，却一条都没解析出来 —— 格式大概率变了"
                            % (adapter.name, len(paths)))
        else:
            age = (now - last_ts) / 86400.0
            if age > 30:
                state = "%.0f 天无新数据" % age
                problems.append("%s：最后一条在 %.0f 天前 —— 可能已停用，也可能格式变了"
                                % (adapter.name, age))
            else:
                state = "正常"
        cells = [agent_label(adapter.name), str(len(paths)), format(count, ","),
                 fmt_ts(last_ts) if last_ts else "—", state]
        body.append(cells)
        for i, cell in enumerate(cells):
            widths[i] = max(widths[i], display_width(cell))

    print("  " + "  ".join(pad(h, widths[i]) for i, h in enumerate(header)))
    print("  " + "  ".join("-" * widths[i] for i in range(len(header))))
    for cells in body:
        print("  " + "  ".join(pad(c, widths[i], "right" if i in (1, 2) else "left")
                               for i, c in enumerate(cells)))
    print()

    if problems:
        for item in problems:
            print("  ⚠ %s" % item)
    else:
        print("  全部正常：每个 Adapter 都能找到源，且最近都有新事件。")
    print()


def cmd_breakdown(args):
    if not DEFAULT_DB.exists():
        print("还没有数据，先运行：python3 tokenlensbar.py scan")
        return
    since = 0.0 if args.days <= 0 else time.time() - args.days * 86400
    rows, _ = fetch_rows(DEFAULT_DB, since)
    prices = load_prices()
    symbol = prices.get("symbol") or "$"
    cells = breakdown(rows, prices, since)
    if not cells:
        print("最近 %d 天没有记录。" % args.days)
        return

    window = "全部历史" if args.days <= 0 else "最近 %d 天" % args.days
    print()
    print("  Agent × 模型（%s）" % window)
    print()
    header = ["Agent", "模型", "总量", "输入", "输出", "成本"]
    widths = [max(display_width(h), 6) for h in header]
    body = []
    for cell in cells:
        row = [cell["agent"], cell["model"], human(cell["total"]), human(cell["input"]),
               human(cell["output"]), money(cell["cost"], symbol)]
        body.append(row)
        for i, value in enumerate(row):
            widths[i] = max(widths[i], display_width(value))
    print("  " + "  ".join(pad(h, widths[i]) for i, h in enumerate(header)))
    print("  " + "  ".join("-" * widths[i] for i in range(len(header))))
    for row in body:
        print("  " + "  ".join(pad(v, widths[i], "right" if i >= 2 else "left")
                               for i, v in enumerate(row)))
    print()

    if args.days > 0:
        older = outside_cells(DEFAULT_DB, cells, prices)
        if older:
            print("  窗口外还有 %d 项（加 --days 0 看全部）：" % len(older))
            for item in older:
                print("    %-8s %-22s %8s  %s  %s" % (
                    item["agent"], item["model"], human(item["tokens"]),
                    money(item["cost"], symbol),
                    datetime.fromtimestamp(item["last"]).strftime("%Y-%m-%d")))
            print()


def api_payload(db_path, days, now=None, prices=None):
    prices = prices if prices is not None else load_prices()
    symbol = prices.get("symbol") or "$"
    now = time.time() if now is None else now
    since = start_of_day(now - (days - 1) * 86400)
    rows, sources = fetch_rows(db_path, since) if db_path.exists() else ([], [])
    agg = aggregate(rows, prices, since)
    totals = agg["totals"]
    seen = models_seen(rows, prices)
    peak_cost = off_peak_cost = 0.0
    for row in rows:
        if row[0] < since:
            continue
        cost, _, _, band = row_cost(row, prices, with_band=True)
        if band == "off_peak":
            off_peak_cost += cost
        else:
            peak_cost += cost

    models = []
    for model, bucket in sorted(agg["by_model"].items(), key=lambda kv: -kv[1]["total"]):
        models.append({
            "model": model,
            "tokens": bucket["total"],
            "input": bucket["input"],
            "output": bucket["output"],
            "cache_read": bucket["cache_read"],
            "events": bucket["count"],
            "cost": round(bucket["cost"], 6),
            "agents": sorted(seen.get(model, {}).get("agents", [])),
        })

    return {
        "ok": bool(rows),
        "days": days,
        "symbol": symbol,
        "currency": prices.get("currency") or "USD",
        "priced": bool(prices),
        "totals": {
            "tokens": totals["total"],
            "input": totals["input"],
            "output": totals["output"],
            "cache_read": totals["cache_read"],
            "events": totals["count"],
            "cost": round(totals["cost"], 6),
        },
        "models": models,
        "agents": [
            {"agent": name, **{k: (round(v, 6) if k == "cost" else v) for k, v in bucket.items()}}
            for name, bucket in sorted(agg["by_agent"].items(), key=lambda kv: -kv[1]["total"])
        ],
        "matrix": breakdown(rows, prices, since),
        "outside": outside_cells(db_path, breakdown(rows, prices, since), prices),
        "daily": daily_series(rows, since, prices, now),
        "heatmap": heatmap_series(rows, since, prices),
        "bands": {"peak": round(peak_cost, 6), "off_peak": round(off_peak_cost, 6)},
        "unpriced": agg["unpriced"],
        "sources": [{"agent": name, "files": count} for name, count in sources],
    }


def serve(db_path, port, open_browser):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    root = Path(__file__).resolve().parent / "web"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, content_type="application/json; charset=utf-8"):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                page = root / "index.html"
                if not page.exists():
                    self._send(500, "web/index.html 不存在")
                    return
                self._send(200, page.read_bytes(), "text/html; charset=utf-8")
                return
            if parsed.path == "/api/summary":
                query = parse_qs(parsed.query)
                try:
                    days = int(query.get("days", ["30"])[0])
                except ValueError:
                    days = 30
                days = max(1, min(days, 3650))
                payload = api_payload(db_path, days)
                self._send(200, json.dumps(payload, ensure_ascii=False))
                return
            self._send(404, json.dumps({"error": "not found"}))

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = "http://127.0.0.1:%d/" % port
    print()
    print("  TokenLensBar 看板：%s" % url)
    print("  只监听 127.0.0.1，数据不出本机。Ctrl+C 退出。")
    print()
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止。")
    finally:
        server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="tokenlensbar", description="本地 token 用量采集器")
    sub = parser.add_subparsers(dest="command")

    scan_parser = sub.add_parser("scan", help="采集一次")
    scan_parser.add_argument("-v", "--verbose", action="store_true")
    scan_parser.add_argument("--force-balance", action="store_true",
                             help="忽略余额缓存间隔，立即刷新余额")

    report_parser = sub.add_parser("report", help="看报表")
    report_parser.add_argument("--days", type=int, default=7, help="0 = 全部历史")
    report_parser.add_argument("--by", choices=["model", "agent", "provider"], default="model")

    sub.add_parser("sources", help="列出发现的数据源")
    sub.add_parser("models", help="列出所有见过的模型及其定价状态")
    sub.add_parser("health", help="体检数据源是否还在、是否静默停止解析")

    reset_parser = sub.add_parser("reset", help="清掉某个来源的数据，下次扫描重算")
    reset_parser.add_argument("--agent", help="例如 dsh / codex / hermes；不填则清全部")

    breakdown_parser = sub.add_parser("breakdown", help="Agent × 模型 交叉明细")
    breakdown_parser.add_argument("--days", type=int, default=30,
                                  help="0 = 全部历史")

    status_parser = sub.add_parser("status", help="一行式摘要")
    status_parser.add_argument("--json", action="store_true")

    manual_parser = sub.add_parser("manual", help="手动补记金额/Token/Agent/Model")
    manual_sub = manual_parser.add_subparsers(dest="manual_action")
    manual_add = manual_sub.add_parser("add", help="新增一条补记")
    manual_add.add_argument("--amount", type=float, help="金额，可为负数")
    manual_add.add_argument("--tokens", type=int, help="Token 数")
    manual_add.add_argument("--agent", help="Agent 名称，可空")
    manual_add.add_argument("--model", help="模型名称，可空")
    manual_add.add_argument("--date", help="日期 YYYY-MM-DD，默认今天")
    manual_list = manual_sub.add_parser("list", help="查看补记")
    manual_list.add_argument("--date", help="日期 YYYY-MM-DD，默认今天")
    manual_list.add_argument("--json", action="store_true")
    manual_delete = manual_sub.add_parser("delete", help="删除一条补记")
    manual_delete.add_argument("id", type=int)

    balance_parser = sub.add_parser("balance", help="查看账户余额")
    balance_parser.add_argument("--refresh", action="store_true", help="立即请求余额接口")
    balance_parser.add_argument("--today", action="store_true", help="查看今日消费")
    balance_parser.add_argument("--json", action="store_true")

    cookie_parser = sub.add_parser(
        "cookie", help="更新 MiMo 余额 Cookie（自动读 Safari；--from-clipboard 兜底）")
    cookie_parser.add_argument("--from-clipboard", action="store_true",
                               help="从剪贴板读 Cookie 写入（自动读不到时的手动兜底）")
    cookie_parser.add_argument("--check", action="store_true",
                               help="只体检状态，不改任何东西")

    serve_parser = sub.add_parser("serve", help="启动本地看板")
    serve_parser.add_argument("--port", type=int, default=8787)
    serve_parser.add_argument("--no-browser", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "scan":
        stats = scan(DEFAULT_DB, verbose=args.verbose, force_balance=args.force_balance)
        total = 0
        parts = []
        for name, (found, parsed, inserted) in stats.items():
            total += inserted
            parts.append("%s %d/%d→%d" % (name, parsed, found, inserted))
        print("扫描完成：新增 %d 条事件  [%s]" % (total, "，".join(parts)))
        if DEFAULT_DB.exists():
            _rows, _ = load_all_rows(DEFAULT_DB)
            new_models = aggregate(_rows, load_prices() if load_prices() else {},
                                   time.time() - 86400)["unpriced"]
            if new_models:
                print("⚠ 有未定价模型：%s（补到 %s）" % ("、".join(new_models), PRICES_PATH))
    elif args.command == "report":
        report(DEFAULT_DB, args.days, args.by)
    elif args.command == "balance":
        if args.refresh:
            try:
                result = refresh_balances(DEFAULT_DB, force=True, verbose=True)
            except Exception as exc:
                print("余额刷新失败：%s" % exc)
                return 1
            if not result["configured"]:
                print("余额查询未启用：先配置 %s" % BALANCES_PATH)
        if args.today:
            prices = load_prices()
            rows, _ = fetch_rows(DEFAULT_DB, 0) if DEFAULT_DB.exists() else ([], [])
            items = enrich(rows, prices)
            spend = today_spend(DEFAULT_DB, prices, items=items)
            if args.json:
                print(json.dumps(spend, ensure_ascii=False))
                return 0
            print()
            for bucket in spend["currencies"]:
                print("  %s" % money_exact(bucket["total"], bucket["symbol"]))
                for provider in bucket["providers"]:
                    if provider["spent"] is not None:
                        started = (datetime.fromtimestamp(provider["start_ts"])
                                   .strftime("%H:%M") if provider["start_ts"] else "—")
                        print("    %-12s %s%.2f（%s 起）" % (
                            provider["name"], bucket["symbol"],
                            provider["spent"], started))
                    else:
                        print("    %-12s —（今天还没采到余额）" % provider["name"])
                if bucket["manual"]:
                    print("    手动补记     %s%.2f" % (bucket["symbol"], bucket["manual"]))
            if not spend["currencies"]:
                print("  今天还没有消费数据。")
            print()
            return 0
        try:
            payload = balance_payload(DEFAULT_DB)
        except Exception as exc:
            print("余额读取失败：%s" % exc)
            return 1
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        elif not payload["configured"]:
            print("余额查询未启用。参考 %s 和 %s" % (BALANCES_PATH, SECRETS_PATH))
        else:
            for provider in payload["providers"]:
                if provider["items"]:
                    for item in provider["items"]:
                        print("%s  %s %.2f%s" % (
                            provider["name"], item["currency"], item["total"],
                            "" if item.get("available", True) else "  (不可用)"))
                else:
                    print("%s  %s" % (provider["name"], provider["error"] or "读取中…"))
    elif args.command == "cookie":
        return cmd_cookie(args)
    elif args.command == "status":
        if args.json:
            print(json.dumps(status_json(DEFAULT_DB), ensure_ascii=False))
        else:
            data = status(DEFAULT_DB)
            symbol = (load_prices() or {}).get("symbol") or "$"
            print("  ".join(
                "近%d天 %s tokens %s%.2f" % (
                    d["days"], human(d["tokens"]), symbol, d["cost"])
                for d in data
            ))
    elif args.command == "manual":
        return cmd_manual(args)
    elif args.command == "models":
        cmd_models(args)
    elif args.command == "health":
        cmd_health(args)
    elif args.command == "reset":
        cmd_reset(args)
    elif args.command == "breakdown":
        cmd_breakdown(args)
    elif args.command == "serve":
        serve(DEFAULT_DB, args.port, not args.no_browser)
    elif args.command == "sources":
        cmd_sources(args)
    else:
        parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
