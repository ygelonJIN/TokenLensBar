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
import tokenlens as tl  # noqa: E402


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

    # 5. 未定价模型会被点名
    prices = tl.load_prices(write_prices({"models": {}}))
    rows = [(epoch("2026-10-24 12:00"), "codex", "p", "brand-new-model",
             1000, 100, 500, 0, 0, 1100, 0.0, 1)]
    agg = tl.aggregate(rows, prices, 0)
    passed &= check("未定价模型进 unpriced 列表", "brand-new-model" in agg["unpriced"], True)
    passed &= check("未定价不影响 token 统计", agg["totals"]["total"], 1100)

    print()
    print("全部通过" if passed else "有失败项")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
