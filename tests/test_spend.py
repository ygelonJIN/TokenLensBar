#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""今日消费（余额下降 + 跨天会话归因 + 手动补记）回归测试。

运行：python3 tests/test_spend.py
不联网：余额样本和事件都直接写进临时 SQLite。
"""

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tokenlensbar as tl  # noqa: E402


def check(name, actual, expected, tolerance=1e-9):
    if isinstance(expected, float):
        ok = abs(actual - expected) <= tolerance
    else:
        ok = actual == expected
    print("%s %s" % ("✓" if ok else "✗", name))
    if not ok:
        print("   期望 %r，实际 %r" % (expected, actual))
    return ok


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def make_fixture(tmp, samples, events):
    case = Path(tempfile.mkdtemp(dir=str(tmp)))
    db = case / "usage.db"
    prices_path = write_json(case / "prices.json", {
        "currency": "CNY", "symbol": "¥",
        "models": {
            "deepseek-v4-pro": {
                "match": ["deepseek-v4-pro", "m"],
                "input_cache_hit": 1.0,
                "input_cache_miss": 1.0,
                "output": 1.0,
            },
        },
    })
    config = write_json(case / "balances.json", {
        "providers": [
            {"id": "deepseek", "type": "deepseek", "name": "DeepSeek", "enabled": True},
        ],
    })
    prices = tl.load_prices(prices_path)
    con = tl.connect(db)
    for provider, currency, ts, total in samples:
        con.execute(
            "insert into balance_samples"
            " (provider, currency, ts, total, available) values (?,?,?,?,1)",
            (provider, currency, ts, total))
    for index, event in enumerate(events):
        tl.insert_event(con, {
            "id": "e%d" % index,
            "ts": event["ts"],
            "agent": event.get("agent", "hermes"),
            "provider": event.get("provider", "deepseek"),
            "model": event.get("model", "deepseek-v4-pro"),
            "input": event.get("input", 1_000_000),
            "output": 0,
            "cache_read": 0,
            "cache_write": 0,
            "reasoning": 0,
            "total": event.get("input", 1_000_000),
            "cost_usd": 0.0,
            "cached_in_input": 1,
        }, "test")
    # 最新余额，用于余额栏（不参与今日消费计算）
    con.execute(
        "insert or replace into balances"
        " (provider, currency, total, available, fetched_at) values (?,?,?,?,?)",
        ("deepseek", "CNY", samples[-1][3] if samples else 0, 1, samples[-1][2] if samples else 0))
    con.commit()
    con.close()
    return db, prices, config


def main():
    passed = True
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        now = datetime(2026, 10, 25, 10, 0).timestamp()
        day_start = datetime(2026, 10, 25, 8, 0).timestamp()
        day9 = datetime(2026, 10, 25, 9, 0).timestamp()
        y23 = datetime(2026, 10, 24, 23, 0).timestamp()
        y2330 = datetime(2026, 10, 24, 23, 30).timestamp()
        t2 = datetime(2026, 10, 25, 2, 0).timestamp()

        # 1. 跨天缺口按会话结束日归因：23:30 归昨天，2:00 归今天
        db, prices, config = make_fixture(
            tmp,
            [("deepseek", "CNY", y23, 100.0),
             ("deepseek", "CNY", day_start, 95.0),
             ("deepseek", "CNY", day9, 94.0)],
            [{"ts": y2330, "input": 1_500_000},
             {"ts": t2, "input": 3_000_000}],
        )
        spend = tl.today_spend(db, prices, items=tl.enrich(tl.fetch_rows(db, 0)[0], prices),
                               now=now, config_path=config)
        cny = spend["currencies"][0]
        # 8:00→9:00 降 1；缺口 5 按 1.5:3.0 分，今天 3.333333
        passed &= check("今日消费 = 当天下降 + 2:00 会话份额",
                        cny["providers"][0]["spent"], 4.333333, 1e-6)
        passed &= check("起点是今天第一条样本", cny["providers"][0]["start_ts"], day_start)
        passed &= check("没有跨夜提示字段", "gap_notice" in spend, False)
        passed &= check("余额栏数据仍存在", tl.balance_payload(db, config_path=config)["configured"], True)

        # 2. 只有 2:00 会话：整笔缺口算今天
        db, prices, config = make_fixture(
            tmp,
            [("deepseek", "CNY", y23, 100.0),
             ("deepseek", "CNY", day_start, 95.0),
             ("deepseek", "CNY", day9, 94.0)],
            [{"ts": t2, "input": 5_000_000}],
        )
        spend = tl.today_spend(db, prices, items=tl.enrich(tl.fetch_rows(db, 0)[0], prices),
                               now=now, config_path=config)
        passed &= check("2:00 消费整笔归今天", spend["currencies"][0]["providers"][0]["spent"], 6.0)

        # 3. 只有昨天 23:30：缺口不算今天
        db, prices, config = make_fixture(
            tmp,
            [("deepseek", "CNY", y23, 100.0),
             ("deepseek", "CNY", day_start, 95.0),
             ("deepseek", "CNY", day9, 94.0)],
            [{"ts": y2330, "input": 5_000_000}],
        )
        spend = tl.today_spend(db, prices, items=tl.enrich(tl.fetch_rows(db, 0)[0], prices),
                               now=now, config_path=config)
        passed &= check("昨天会话不算今天", spend["currencies"][0]["providers"][0]["spent"], 1.0)

        # 4. 充值：上涨不冲减消费，也不显示充值
        db, prices, config = make_fixture(
            tmp,
            [("deepseek", "CNY", y23, 100.0),
             ("deepseek", "CNY", day_start, 120.0),
             ("deepseek", "CNY", day9, 118.0)],
            [],
        )
        spend = tl.today_spend(db, prices, items=None, now=now, config_path=config)
        passed &= check("充值后消费仍是当天下降", spend["currencies"][0]["providers"][0]["spent"], 2.0)
        passed &= check("不记录充值字段", "topup" in json.dumps(spend, ensure_ascii=False), False)

        # 5. 今天还没有样本
        db, prices, config = make_fixture(
            tmp,
            [("deepseek", "CNY", y23, 100.0)],
            [],
        )
        spend = tl.today_spend(db, prices, items=None, now=now, config_path=config)
        passed &= check("今天没样本时 spent=None", spend["currencies"][0]["providers"][0]["spent"], None)

        # 6. 手动补记：金额、Token、Agent、Model 都可选
        db, prices, config = make_fixture(
            tmp,
            [("deepseek", "CNY", day_start, 95.0), ("deepseek", "CNY", day9, 94.0)],
            [{"ts": day9, "input": 1_000_000}],
        )
        entry_id = tl.add_manual_entry(amount=2, tokens=1000, agent="Foo",
                                       model="bar", db_path=db, now=now)
        passed &= check("手动补记返回 id", entry_id, 1)
        tl.add_manual_entry(tokens=500, agent="Baz", db_path=db, now=now)
        entries = tl.manual_entries_for(db, now=now)
        passed &= check("手动补记两条", len(entries), 2)
        passed &= check("Agent/Model 可空", entries[1]["model"], None)
        spend = tl.today_spend(db, prices, items=None, now=now, config_path=config)
        passed &= check("手动金额加到今日消费", spend["currencies"][0]["total"], 3.0)
        passed &= check("手动 Token 单独统计", spend["manual"]["tokens"], 1500)
        passed &= check("手动条目带 Agent", spend["manual"]["entries"][0]["agent"], "Foo")

        # 7. 手动补记至少要有金额或 Token
        try:
            tl.add_manual_entry(agent="OnlyAgent", db_path=db, now=now)
            failed = False
        except ValueError:
            failed = True
        passed &= check("四个都空时拒绝", failed, True)

        # 8. status_json 输出 today_spend；余额栏仍是当前余额
        payload = tl.status_json(db, prices, now=now, config_path=config)
        passed &= check("status_json 带 today_spend", "today_spend" in payload, True)
        passed &= check("status_json 今日合计", payload["today_spend"]["currencies"][0]["total"], 3.0)
        passed &= check("余额栏仍返回当前余额",
                        payload["balances"]["providers"][0]["items"][0]["total"], 94.0)

        # 9. 命令行补记
        saved = tl.DEFAULT_DB
        tl.DEFAULT_DB = db
        try:
            import contextlib
            import io
            code = tl.main(["manual", "add", "--amount", "1", "--agent", "CLI",
                            "--model", "m"])
            passed &= check("manual CLI 退出码", code, 0)
            entries = tl.manual_entries_for(db, now=now)
            passed &= check("manual CLI 写入", entries[-1]["agent"], "CLI")
            with contextlib.redirect_stdout(io.StringIO()):
                code = tl.main(["manual", "delete", str(entries[-1]["id"])])
            passed &= check("manual CLI 删除", code, 0)
        finally:
            tl.DEFAULT_DB = saved

    print()
    print("全部通过" if passed else "有失败项")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
