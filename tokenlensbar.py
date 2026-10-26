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
            raise BalanceError(auth_error)
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


def fetch_mimo_balance(entry, cookie, timeout=6.0):
    """小米 MiMo 账户余额。控制台接口只认 Cookie，不认 API Key。

    需要两个值：api-platform_serviceToken（Cookie）和 userId（控制台地址里的
    ?userId=...，服务端要它，但控制台页面上不一定把它显示成 Cookie）。
    """
    header = _cookie_header(cookie)
    if not header:
        raise BalanceError("未配置 Cookie")
    if "serviceToken" not in header:
        raise BalanceError("Cookie 里缺少 api-platform_serviceToken")
    if "userId" not in header:
        raise BalanceError("Cookie 里缺少 userId（Cookies 表里的 userId，或控制台地址 ?userId=...）")
    url = entry.get("url") or MIMO_BALANCE_URL
    data = _http_json(url, cookie=header, timeout=timeout,
                      auth_error="Cookie 已失效或缺少 userId，请重新登录后粘贴")
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


def _provider_key(secrets, entry):
    key = secrets.get(entry.get("secret") or entry["id"])
    if not key and entry["type"] == "deepseek":
        key = os.environ.get("DEEPSEEK_API_KEY")
    if not key and entry["type"] in ("mimo", "mimo_cookie"):
        key = os.environ.get("MIMO_COOKIE")
    return (key or "").strip()


def refresh_balances(db_path, force=False, verbose=False, config_path=None,
                     secrets_path=None, fetcher=None, now=None):
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
        if not key:
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

        try:
            if fetcher is not None:
                items = fetcher(entry, key)
            elif entry["type"] == "deepseek":
                items = fetch_deepseek_balance(entry, key)
            elif entry["type"] in ("mimo", "mimo_cookie"):
                items = fetch_mimo_balance(entry, key)
            else:
                raise BalanceError("暂不支持 %s" % entry["type"])
            if not items:
                raise BalanceError("没有返回余额")
        except BalanceError as exc:
            message = str(exc)
            con.execute(
                "insert or replace into balance_state"
                " (provider, last_attempt, last_success, error) values (?,?,?,?)",
                (provider, now, last_success, message))
            con.commit()
            result["errors"].append((provider, message))
            if verbose:
                print("  ! %s 余额：%s" % (entry["name"], message))
            continue
        except Exception:
            message = "刷新异常"
            con.execute(
                "insert or replace into balance_state"
                " (provider, last_attempt, last_success, error) values (?,?,?,?)",
                (provider, now, last_success, message))
            con.commit()
            result["errors"].append((provider, message))
            if verbose:
                print("  ! %s 余额：刷新异常" % entry["name"])
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
