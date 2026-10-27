#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定价逻辑的回归测试：python3 tests/test_pricing.py

改完 prices.json 或定价代码后跑一遍，确认没算错。
"""

import json
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tokenlensbar as tl  # noqa: E402


def write_prices(data):
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(data, handle, ensure_ascii=False)
    handle.close()
    return handle.name


def epoch(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M").timestamp()


def event(model, ts, input_tokens, cache_read, output_tokens):
    return {
        "model": model,
        "ts": ts,
        "input": input_tokens,
        "output": output_tokens,
        "cache_read": cache_read,
        "cache_write": 0,
        "cached_in_input": 1,
    }


def check(name, actual, expected, tolerance=1e-9):
    ok = abs(actual - expected) <= tolerance
    print("%s %s" % ("✓" if ok else "✗", name))
    if not ok:
        print("   期望 %.6f，实际 %.6f" % (expected, actual))
    return ok


def check_text(name, actual, expected):
    ok = actual == expected
    print("%s %s" % ("✓" if ok else "✗", name))
    if not ok:
        print("   期望 %r，实际 %r" % (expected, actual))
    return ok


def main():
    passed = True

    # 1. 缓存命中/未命中分开计价
    prices = tl.load_prices(write_prices({
        "currency": "CNY", "symbol": "¥",
        "models": {"m": {"match": ["m"], "input_cache_hit": 0.1,
                         "input_cache_miss": 2.0, "output": 8.0}},
    }))
    # 100万输入，其中 80万命中 → 20万未命中 + 10万输出
    cost, _ = tl.event_cost(event("m", epoch("2026-10-24 14:00"), 1_000_000, 800_000, 100_000), prices)
    passed &= check("缓存命中按命中价、其余按未命中价",
                    cost, 0.8 * 0.1 + 0.2 * 2.0 + 0.1 * 8.0)

    # 2. 峰谷时段：北京时间 00:30–08:30 算空闲
    prices = tl.load_prices(write_prices({
        "currency": "CNY", "symbol": "¥",
        "off_peak": {"timezone": "Asia/Shanghai", "windows": [["00:30", "08:30"]]},
        "models": {"m": {"match": ["m"],
                         "peak": {"input_cache_hit": 0.04, "input_cache_miss": 2.0, "output": 8.0},
                         "off_peak": {"input_cache_hit": 0.02, "input_cache_miss": 1.0, "output": 4.0}}},
    }))
    night = tl.event_cost(event("m", epoch("2026-10-24 03:00"), 1_000_000, 0, 1_000_000), prices)[0]
    day = tl.event_cost(event("m", epoch("2026-10-24 14:00"), 1_000_000, 0, 1_000_000), prices)[0]
    passed &= check("凌晨 3 点走空闲价", night, 1.0 + 4.0)
    passed &= check("下午 2 点走高峰价", day, 2.0 + 8.0)
    passed &= check("空闲确实更便宜", 1.0 if night < day else 0.0, 1.0)

    # 3. 多段价格：按事件发生时间取对应那段
    prices = tl.load_prices(write_prices({
        "currency": "CNY", "symbol": "¥",
        "models": {"m": {
            "match": ["m"],
            "effective": [
                {"from": "2026-10-01", "input_cache_hit": 1.0, "input_cache_miss": 10.0, "output": 20.0},
                {"from": "2026-10-20", "input_cache_hit": 0.5, "input_cache_miss": 5.0, "output": 10.0},
            ],
        }},
    }))
    old = tl.event_cost(event("m", epoch("2026-10-10 12:00"), 1_000_000, 0, 0), prices)[0]
    new = tl.event_cost(event("m", epoch("2026-10-22 12:00"), 1_000_000, 0, 0), prices)[0]
    passed &= check("旧价格生效期内用旧价", old, 10.0)
    passed &= check("新价格生效后自动切新价", new, 5.0)

    # 4. 型号别名与前缀匹配
    prices = tl.load_prices(write_prices({
        "currency": "CNY", "symbol": "¥",
        "models": {"mimo-v2.6-pro": {"match": ["mimo-v2.6-pro"],
                                     "input_cache_hit": 1, "input_cache_miss": 1, "output": 1}},
    }))
    passed &= check("带后缀的型号也能认出来",
                    tl.resolve_rate("mimo-v2.6-pro-20261001", epoch("2026-10-24 12:00"), prices)[0]
                    is not None, True)
    passed &= check("完全不认识的型号不瞎猜",
                    tl.resolve_rate("unknown-model", epoch("2026-10-24 12:00"), prices)[0] is None,
                    True)

    # 4c. 同一前缀的两个型号（pro / pro-ultraspeed）不能串价
    prices = tl.load_prices(write_prices({
        "currency": "CNY", "symbol": "¥",
        "models": {
            "mimo-v2.6-pro": {"match": ["mimo-v2.6-pro"],
                              "input_cache_hit": 0.025, "input_cache_miss": 3.0, "output": 6.0},
            "mimo-v2.6-pro-ultraspeed": {"match": ["mimo-v2.6-pro-ultraspeed"],
                                         "input_cache_hit": 0.25, "input_cache_miss": 30.0,
                                         "output": 60.0},
            "mimo-v2.6-flash": {"match": ["mimo-v2.6-flash"],
                                "input_cache_hit": 0.02, "input_cache_miss": 1.0, "output": 2.0},
        },
    }))
    now = epoch("2026-10-24 12:00")
    passed &= check_text("pro 认成 pro",
                         tl.model_label("MiMo-V2.6-Pro", now, prices), "mimo-v2.6-pro")
    passed &= check_text("ultraspeed 不会被 pro 抢走",
                         tl.model_label("mimo-v2.6-pro-ultraspeed", now, prices),
                         "mimo-v2.6-pro-ultraspeed")
    passed &= check_text("带日期的 ultraspeed 也归 ultraspeed",
                         tl.model_label("mimo-v2.6-pro-ultraspeed-20261001", now, prices),
                         "mimo-v2.6-pro-ultraspeed")
    passed &= check_text("带日期的 pro 仍归 pro",
                         tl.model_label("mimo-v2.6-pro-20261001", now, prices), "mimo-v2.6-pro")
    passed &= check_text("flash 是独立型号",
                         tl.model_label("mimo-v2.6-flash", now, prices), "mimo-v2.6-flash")
    passed &= check("flash 单价对得上",
                    tl.event_cost(event("mimo-v2.6-flash", now, 1_000_000, 0, 0), prices)[0], 1.0)

    # 4b. 展示用正式名：日志里的别名归到价格表里的型号名
    prices = tl.load_prices(write_prices({
        "currency": "CNY", "symbol": "¥",
        "models": {"deepseek-v4.1-flash": {
            "match": ["deepseek-flash", "DeepSeek-V4.1-Flash"],
            "input_cache_hit": 0.04, "input_cache_miss": 2.0, "output": 8.0}},
    }))
    passed &= check_text("日志原名映射到正式名",
                         tl.model_label("deepseek-flash", epoch("2026-10-24 12:00"), prices),
                         "deepseek-v4.1-flash")
    passed &= check_text("认不得的型号保留原名",
                         tl.model_label("unknown-model", epoch("2026-10-24 12:00"), prices),
                         "unknown-model")
    rows = [
        (epoch("2026-10-24 12:00"), "dsh", "deepseek", "deepseek-flash",
         1000, 100, 500, 0, 0, 1100, 0.0, 1),
        (epoch("2026-10-24 12:01"), "dsh", "deepseek", "DeepSeek-V4.1-Flash",
         2000, 200, 1000, 0, 0, 2200, 0.0, 1),
    ]
    agg = tl.aggregate(rows, prices, 0)
    passed &= check_text("两个别名合并成一行正式名",
                         list(agg["by_model"]), ["deepseek-v4.1-flash"])
    passed &= check_text("models_seen 也用正式名",
                         list(tl.models_seen(rows, prices)), ["deepseek-v4.1-flash"])
    passed &= check_text("面板 enrich 用正式名",
                         [item["model"] for item in tl.enrich(rows, prices)],
                         ["deepseek-v4.1-flash", "deepseek-v4.1-flash"])
    passed &= check_text("breakdown 用正式名",
                         [cell["model"] for cell in tl.breakdown(rows, prices, 0)],
                         ["deepseek-v4.1-flash"])

    # 5. 未定价模型会被点名
    prices = tl.load_prices(write_prices({"models": {}}))
    rows = [(epoch("2026-10-24 12:00"), "codex", "p", "brand-new-model",
             1000, 100, 500, 0, 0, 1100, 0.0, 1)]
    agg = tl.aggregate(rows, prices, 0)
    passed &= check("未定价模型进 unpriced 列表", "brand-new-model" in agg["unpriced"], True)
    passed &= check("未定价不影响 token 统计", agg["totals"]["total"], 1100)

    # 9. 时间不准的来源整体按峰价（峰值 * 1，不走空闲价）
    prices = tl.load_prices(write_prices({
        "currency": "CNY", "symbol": "\u00a5",
        "off_peak": {"timezone": "Asia/Shanghai",
                     "peak_windows": [["09:00", "12:00"], ["14:00", "18:00"]],
                     "weekdays": [1, 2, 3, 4, 5], "holidays": []},
        "peak_only_agents": ["hermes"],
        "models": {"m": {"match": ["m"],
                         "peak": {"input_cache_hit": 0.04, "input_cache_miss": 2.0, "output": 8.0},
                         "off_peak": {"input_cache_hit": 0.02, "input_cache_miss": 1.0, "output": 4.0}}},
    }))

    def by_agent(agent, text):
        ev = event("m", epoch(text), 1_000_000, 0, 1_000_000)
        ev["agent"] = agent
        return tl.event_cost(ev, prices)[0]

    passed &= check("周六 hermes 也按峰价", by_agent("hermes", "2026-10-24 10:00"), 2.0 + 8.0)
    passed &= check("周六 hermes-pi 也按峰价", by_agent("hermes-pi", "2026-10-24 10:00"), 2.0 + 8.0)
    passed &= check("同一时刻 codex 仍走空闲价", by_agent("codex", "2026-10-24 10:00"), 1.0 + 4.0)

    # 8. 工作日峰时 / 周末全天空闲 / 节假日全天空闲
    prices = tl.load_prices(write_prices({
        "currency": "CNY", "symbol": "\u00a5",
        "off_peak": {"timezone": "Asia/Shanghai",
                     "peak_windows": [["09:00", "12:00"], ["14:00", "18:00"]],
                     "weekdays": [1, 2, 3, 4, 5],
                     "holidays": ["2026-11-03"]},
        "models": {"m": {"match": ["m"],
                         "peak": {"input_cache_hit": 0.04, "input_cache_miss": 2.0, "output": 8.0},
                         "off_peak": {"input_cache_hit": 0.02, "input_cache_miss": 1.0, "output": 4.0}}},
    }))

    def unit(text):
        return tl.event_cost(event("m", epoch(text), 1_000_000, 0, 0), prices)[0]

    passed &= check("周一 10:00 走高峰价", unit("2026-11-02 10:00"), 2.0)
    passed &= check("周一 15:00 走高峰价", unit("2026-11-02 15:00"), 2.0)
    passed &= check("周一 12:30 午休走空闲价", unit("2026-11-02 12:30"), 1.0)
    passed &= check("周一 08:59 还没到峰时", unit("2026-11-02 08:59"), 1.0)
    passed &= check("周六全天空闲", unit("2026-10-24 10:00"), 1.0)
    passed &= check("节假日全天空闲", unit("2026-11-03 10:00"), 1.0)

    # 6. 压缩流被截断时，前面解出来的部分仍然可用
    #    （DSH 是"追加帧"格式：最后一帧写了一半，前面几帧还是完整的）
    work = tempfile.mkdtemp()
    import subprocess
    import pathlib

    def pack(name, start, count):
        plain = os.path.join(work, name + ".jsonl")
        with open(plain, "w", encoding="utf-8") as handle:
            for i in range(start, start + count):
                handle.write(json.dumps({"i": i, "pad": "x" * 200}) + "\n")
        out = os.path.join(work, name + ".zstd")
        subprocess.run(["zstd", "-q", "-f", plain, "-o", out], check=True)
        with open(out, "rb") as handle:
            return handle.read()

    first = pack("p1", 0, 25)
    second = pack("p2", 25, 25)
    packed = os.path.join(work, "a.zstd")
    with open(packed, "wb") as handle:
        handle.write(first + second[: len(second) // 2])  # 模拟"正在写入"的半截文件
    text = tl.zstd_decompress(packed)
    passed &= check("半截 zstd 仍能解出前面完整的内容",
                    len([line for line in (text or "").splitlines() if line.strip()]), 25)

    # 7. 读不出来的文件不能被记成"已处理"（否则永久漏采）
    class Flaky(tl.Adapter):
        name = "flaky"
        always_scan = True
        busy = True

        def __init__(self, path):
            self.path = path

        def discover(self):
            return [pathlib.Path(self.path)]

        def parse(self, path):
            if self.busy:
                raise tl.SourceBusy(path)
            yield tl.make_event("flaky:1", epoch("2026-10-24 12:00"), "flaky",
                                "m", {"input_tokens": 1000, "output_tokens": 100})

    source = os.path.join(work, "b.json")
    with open(source, "w", encoding="utf-8") as handle:
        handle.write("{}")
    db = pathlib.Path(work) / "t.db"
    adapter = Flaky(source)
    saved = tl.ADAPTERS
    tl.ADAPTERS = [adapter]
    try:
        tl.scan(db)
        con = tl.connect(db)
        passed &= check("读取失败时不写 files 表", con.execute(
            "select count(*) from files where path=?", (source,)).fetchone()[0], 0)
        adapter.busy = False
        tl.scan(db)
        passed &= check("下一轮重试后能补上", con.execute(
            "select count(*) from events where agent='flaky'").fetchone()[0], 1)
        con.close()
    finally:
        tl.ADAPTERS = saved

    print()
    print("全部通过" if passed else "有失败项")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
