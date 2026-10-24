#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tokenlens — 本地 token 用量采集器

设计原则
  1. 不碰 API Key，不发任何网络请求。
  2. 不依赖模型厂商，不认识"厂商"这个概念。
  3. 不绑定 Agent，只绑定"本地用量记录的数据格式"。
  4. 增量扫描：只重读变化的文件；事件按稳定 ID 去重，重复扫描不会重复计数。

用法
  python3 tokenlens.py scan                # 采集（可放进 cron / launchd）
  python3 tokenlens.py report --days 7     # 看报表
  python3 tokenlens.py report --by agent
  python3 tokenlens.py sources             # 看发现了哪些数据源

新增一个 Agent 的支持 = 写一个 Adapter 子类 + 注册到 ADAPTERS，约 20 行。
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

DEFAULT_DB = Path(os.environ.get("TOKENLENS_DB", "~/.tokenlens/usage.db")).expanduser()
PRICES_PATH = Path(os.environ.get("TOKENLENS_PRICES", "~/.tokenlens/prices.json")).expanduser()

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


def event_cost(event, prices):
    """按'缓存命中 / 未命中 + 峰谷'算一次请求的钱。返回 (金额, 命中的型号名)。"""
    if not prices:
        return 0.0, None
    ts = float(event.get("ts") or 0)
    name, version = resolve_rate(event.get("model") or "", ts, prices)
    if version is None:
        return 0.0, None

    rates = version["peak"]
    if version["off_peak"] and is_off_peak(ts, prices):
        rates = version["off_peak"]

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


def zstd_decompress(path):
    """返回解压后的文本；本机没有 zstd 命令、或一个字节都读不出来时返回 None。

    文件正在被追加写入时，zstd 会因为末尾帧不完整而返回非 0，但前面已经解出来的
    部分是有效数据。所以只要 stdout 有内容就照常返回，让调用方自己跳过被截断的
    最后一行（json 解析失败会自动跳过）。返回 None 才是真的没法读。
    """
    try:
        proc = subprocess.run(
            ["zstd", "-dc", str(path)],
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
        for obj in iter_jsonl(text):
            kind = obj.get("type")
            if kind == "session":
                session_id = obj.get("id") or ""
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
                    ts=(obj.get("time") or 0) / 1000.0,
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
                " estimated_cost_usd, last_seen from session_model_usage"
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


REMOTES_PATH = Path(os.environ.get("TOKENLENS_REMOTES", "~/.tokenlens/remotes.json")).expanduser()

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
            " reasoning_tokens, estimated_cost_usd, last_seen"
            " from session_model_usage").fetchall()
    elif "sessions" in tables:
        # 老版 Hermes：用量只在 sessions 表里，按会话一条
        cols = {row[1] for row in con.execute("pragma table_info(sessions)")}
        stamp = "coalesce(ended_at, started_at)" if "ended_at" in cols else "started_at"
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
    "insert or ignore into events"
    " (id, ts, agent, provider, model, input, output, cache_read, cache_write,"
    "  reasoning, total, cost_usd, cached_in_input, origin)"
    " values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def insert_event(con, event, origin):
    con.execute(INSERT_SQL, (
        event["id"], event["ts"], event["agent"], event["provider"], event["model"],
        event["input"], event["output"], event["cache_read"], event["cache_write"],
        event["reasoning"], event["total"], event["cost_usd"],
        event["cached_in_input"], origin,
    ))


def scan(db_path, verbose=False):
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
        "model": row[3],
        "input": row[4],
        "output": row[5],
        "cache_read": row[6],
        "cache_write": row[7],
        "cached_in_input": row[11] if len(row) > 11 else 1,
    }


def row_cost(row, prices):
    """返回 (金额, 命中的型号名, 是否来自来源自带成本)。"""
    # Hermes 自带的 estimated_cost_usd 是美元，和人民币单价表混用会算错，
    # 所以默认一律用本地单价表重算；需要时在 prices.json 里打开 use_source_cost。
    stored = row[10] or 0.0
    if stored > 0 and prices.get("use_source_cost"):
        return stored, "(来源自带)", True
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
        model = row[3] or "(未知)"
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
        key = (agent_label(row[1]), row[3] or "(未知)")
        add(cells.setdefault(key, zero_bucket()), row, cost)
    out = []
    for (agent, model), bucket in cells.items():
        item = dict(bucket)
        item["agent"] = agent
        item["model"] = model
        out.append(item)
    out.sort(key=lambda item: -item["total"])
    return out


def daily_series(rows, since):
    series = {}
    for row in rows:
        if row[0] < since:
            continue
        day = datetime.fromtimestamp(row[0]).strftime("%Y-%m-%d")
        bucket = series.setdefault(day, {"tokens": 0, "count": 0})
        bucket["tokens"] += row[9]
        bucket["count"] += 1
    return [dict(date=day, **value) for day, value in sorted(series.items())]


def outside_cells(db_path, window_cells, prices):
    """窗口内有数据之外的（Agent, 模型）组合：历史还有多少，最后一次是什么时候。"""
    if not db_path.exists():
        return []
    all_rows, _ = fetch_rows(db_path, 0.0)
    pairs = {(cell["agent"], cell["model"]) for cell in window_cells}
    last = {}
    for row in all_rows:
        key = (agent_label(row[1]), row[3] or "(未知)")
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


def models_seen(rows):
    """本机所有出现过的模型 + 出现在哪些 Agent + 是否已定价。"""
    seen = {}
    for row in rows:
        model = row[3] or "(未知)"
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


def report(db_path, days, group_by):
    if not db_path.exists():
        print("还没有数据，先运行：python3 tokenlens.py scan")
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
    print("tokenlens · 本地用量报告")
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


def daily_filled(items, days):
    """最近 N 天，缺的日子补 0；token 和金额一起返回。"""
    buckets = {}
    for item in items:
        day = datetime.fromtimestamp(item["ts"]).strftime("%Y-%m-%d")
        bucket = buckets.setdefault(day, {"tokens": 0.0, "cost": 0.0})
        bucket["tokens"] += item["total"]
        bucket["cost"] += item["cost"]
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
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
            "model": row[3] or "(未知)",
            "input": row[4] or 0,
            "output": row[5] or 0,
            "cache_read": row[6] or 0,
            "total": row[9] or 0,
            "cost": cost,
            "unpriced": (not stored and matched is None and (row[4] or row[5])),
        })
    return out


def status_json(db_path, prices=None):
    """给菜单栏用的结构化摘要。成本在这里即时算，不落库。"""
    prices = prices if prices is not None else load_prices()
    symbol = prices.get("symbol") or "$"
    if not db_path.exists():
        return {"ok": False, "symbol": symbol, "reason": "no-database"}

    now = time.time()
    # 面板要显示"全部历史"，所以这里取全量（几千行，一次遍历几十毫秒）
    rows, _ = fetch_rows(db_path, 0)
    if not rows:
        return {"ok": False, "symbol": symbol, "reason": "no-events"}

    items = enrich(rows, prices)
    today_start = datetime.now().replace(hour=0, minute=0, second=0,
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
    today_models = group(today_start, "model")
    today_agents = group(today_start, "agent")
    today_total = sum(b["tokens"] for b in today_models.values()) or 1

    unpriced = sorted({item["model"] for item in items
                       if item["unpriced"] and item["ts"] >= week_start})
    week_input = sum(item["input"] for item in items if item["ts"] >= week_start)
    week_cache = sum(item["cache_read"] for item in items if item["ts"] >= week_start)

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
        "daily": daily_filled(items, 7),
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
        "last_event": rows[-1][0],
        "priced": bool(prices),
    }


def status(db_path, days_list=(1, 7, 30)):
    """一行式摘要，给脚本用。"""
    prices = load_prices()
    symbol = prices.get("symbol") or "$"
    since = time.time() - max(days_list) * 86400
    rows, _ = fetch_rows(db_path, since) if db_path.exists() else ([], [])
    out = []
    now = time.time()
    for days in days_list:
        agg = aggregate(rows, prices, now - days * 86400)
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
# CLI
# --------------------------------------------------------------------------

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
        print("还没有数据，先运行：python3 tokenlens.py scan")
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
        print("还没有数据，先运行：python3 tokenlens.py scan")
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
        print("还没有数据，先运行：python3 tokenlens.py scan")
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


def api_payload(db_path, days):
    prices = load_prices()
    symbol = prices.get("symbol") or "$"
    since = time.time() - days * 86400
    rows, sources = fetch_rows(db_path, since) if db_path.exists() else ([], [])
    agg = aggregate(rows, prices, since)
    totals = agg["totals"]
    seen = models_seen(rows)

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
        "daily": daily_series(rows, since),
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
    print("  tokenlens 看板：%s" % url)
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
    parser = argparse.ArgumentParser(prog="tokenlens", description="本地 token 用量采集器")
    sub = parser.add_subparsers(dest="command")

    scan_parser = sub.add_parser("scan", help="采集一次")
    scan_parser.add_argument("-v", "--verbose", action="store_true")

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

    serve_parser = sub.add_parser("serve", help="启动本地看板")
    serve_parser.add_argument("--port", type=int, default=8787)
    serve_parser.add_argument("--no-browser", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "scan":
        stats = scan(DEFAULT_DB, verbose=args.verbose)
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
    elif args.command == "status":
        if args.json:
            print(json.dumps(status_json(DEFAULT_DB), ensure_ascii=False))
        else:
            data = status(DEFAULT_DB)
            symbol = (load_prices() or {}).get("symbol") or "$"
            print("  ".join(
                "近%d天 %s tokens %s%.2f" % (d["days"], human(d["tokens"]), symbol, d["cost"])
                for d in data
            ))
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
