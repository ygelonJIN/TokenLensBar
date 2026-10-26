#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""面板口径的回归测试：python3 tests/test_panel.py

改 api_payload / daily_series / heatmap_series / 峰谷拆分后跑一遍，
确认「按天」「按时段」「峰谷」三处的金额都和总计对得上。
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


def row(ts, agent="dsh", model="m", input_tokens=1_000_000, output_tokens=0, cache_read=0):
    return (ts, agent, "p", model, input_tokens, output_tokens, cache_read,
            0, 0, input_tokens + output_tokens, 0.0, 1)


def prices_with(extra=None):
    data = {
        "currency": "CNY", "symbol": "¥",
        "off_peak": {
            "timezone": "Asia/Shanghai",
            "peak_windows": [["09:00", "12:00"], ["14:00", "18:00"]],
        },
        "models": {
            "m": {
                "match": ["m"],
                "peak": {"input_cache_hit": 0.1, "input_cache_miss": 2.0, "output": 8.0},
                "off_peak": {"input_cache_hit": 0.05, "input_cache_miss": 1.0, "output": 4.0},
            },
        },
    }
    data.update(extra or {})
    return tl.load_prices(write_prices(data))


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
    prices = prices_with()

    # 1. 按天：中间断档要补 0，且金额合计不变
    now = epoch("2026-10-26 12:00")
    since = tl.start_of_day(epoch("2026-10-22 10:00"))
    rows = [row(epoch("2026-10-22 10:00")), row(epoch("2026-10-25 22:00"))]
    series = tl.daily_series(rows, since, prices, now)
    passed &= check_text("补零后的日期连续", [d["date"] for d in series],
                         ["2026-10-22", "2026-10-23", "2026-10-24", "2026-10-25", "2026-10-26"])
    passed &= check_text("断档日 token 为 0", series[1]["tokens"], 0)
    passed &= check_text("断档日金额为 0", series[1]["cost"], 0.0)
    passed &= check("按天金额合计 = 逐条计价合计",
                    sum(d["cost"] for d in series),
                    sum(tl.row_cost(r, prices)[0] for r in rows))

    # 2. 峰谷拆分：峰时按峰价，谷时按谷价，两者相加等于总额
    peak_row = row(epoch("2026-10-21 10:00"))   # 周三上午
    off_row = row(epoch("2026-10-21 22:00"))    # 周三夜间
    peak_cost, _, _, peak_band = tl.row_cost(peak_row, prices, with_band=True)
    off_cost, _, _, off_band = tl.row_cost(off_row, prices, with_band=True)
    passed &= check_text("峰时事件标记为 peak", peak_band, "peak")
    passed &= check_text("谷时事件标记为 off_peak", off_band, "off_peak")
    passed &= check("峰时金额用峰价", peak_cost, 2.0)
    passed &= check("谷时金额用谷价", off_cost, 1.0)

    # 3. peak_only_agents：分不清峰谷的来源整体按高峰价
    forced = prices_with({"peak_only_agents": ["hermes"]})
    forced_cost, _, _, forced_band = tl.row_cost(
        row(epoch("2026-10-21 22:00"), agent="hermes"), forced, with_band=True)
    passed &= check_text("peak_only 来源整段算峰", forced_band, "peak")
    passed &= check("peak_only 来源按峰价计费", forced_cost, 2.0)

    # 4. 7×24：事件落到计价时区的周几/几点
    heat_since = tl.start_of_day(epoch("2026-10-21 00:00"))
    cells = tl.heatmap_series([peak_row, off_row], heat_since, prices)
    by_key = {(c["day"], c["hour"]): c for c in cells}
    passed &= check_text("周三 10 点落在热力图 (2, 10)", (2, 10) in by_key, True)
    passed &= check_text("周三 22 点落在热力图 (2, 22)", (2, 22) in by_key, True)
    passed &= check("热力图金额合计 = 逐条计价合计",
                    sum(c["cost"] for c in cells),
                    sum(tl.row_cost(r, prices)[0] for r in [peak_row, off_row]))

    print()
    print("全部通过" if passed else "有失败项")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
