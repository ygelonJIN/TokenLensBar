#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MiMo Cookie 自动续期的回归测试：python3 tests/test_cookie.py

不联网、不碰真实 Safari：Cookie 文件是按二进制布局合成的，
secrets.json / usage.db 都写在临时目录里。
"""

import json
import os
import sqlite3
import stat
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tokenlensbar as tl  # noqa: E402


def check(name, actual, expected):
    ok = actual == expected
    print("%s %s" % ("✓" if ok else "✗", name))
    if not ok:
        print("   期望 %r，实际 %r" % (expected, actual))
    return ok


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def sample_items(total=88.0):
    return [{"currency": "CNY", "total": total, "granted": None,
             "topped_up": None, "available": True}]


# ---------------------------------------------------------------------------
# 合成 Cookies.binarycookies（布局与解析器的自校验规则一致）
# ---------------------------------------------------------------------------

def make_record(endian, domain, name, path, value, ts=700000000.0):
    strs = [domain.encode(), name.encode(), path.encode(), value.encode()]
    offsets, cursor = [], 48
    for item in strs:
        offsets.append(cursor)
        cursor += len(item) + 1
    rec = struct.pack(endian + "IIIIIIII", cursor + 4, 0,
                      offsets[0], offsets[1], offsets[2], offsets[3], 0, 0)
    rec += struct.pack(endian + "dd", ts, ts + 100)
    rec += b"".join(item + b"\x00" for item in strs)
    return rec + b"\x00" * 4


def make_page(endian, records):
    header = 4 + 4 * len(records)
    offsets, cursor = [], header
    for rec in records:
        offsets.append(cursor)
        cursor += len(rec)
    return (struct.pack(endian + "I", len(records))
            + b"".join(struct.pack(endian + "I", off) for off in offsets)
            + b"".join(records))


def make_file(endian="<", pages=(), count_off=4, table="sizes", trailing=b""):
    """table：'sizes'（页长度）或 'offsets'（页偏移）；count_off：页数字段的位置。"""
    body = b"".join(pages)
    head_len = count_off + 4 + 4 * len(pages)
    if table == "sizes":
        raw = b"".join(struct.pack(endian + "I", len(page)) for page in pages)
    else:
        cursor, offsets = head_len, []
        for page in pages:
            offsets.append(cursor)
            cursor += len(page)
        raw = b"".join(struct.pack(endian + "I", off) for off in offsets)
    return b"cook" + b"\x00" * (count_off - 4) + struct.pack(
        endian + "I", len(pages)) + raw + body + trailing


def mimo_records(endian="<", token="tok-abc", user="4242"):
    return [
        make_record(endian, "platform.xiaomimimo.com",
                    "api-platform_serviceToken", "/", token),
        make_record(endian, ".xiaomimimo.com", "userId", "/", user),
    ]


def main():
    passed = True
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # 1. 双页解析 + 域过滤：别的域同名 Cookie、无关字段都不能混进来
        page_a = make_page("<", mimo_records("<"))
        page_b = make_page("<", [
            make_record("<", "other.com", "api-platform_serviceToken", "/", "evil"),
            make_record("<", ".xiaomimimo.com", "other_field", "/", "zzz"),
        ])
        blob = tmp / "cookies.binarycookies"
        blob.write_bytes(make_file("<", pages=[page_a, page_b]))
        header = tl.read_safari_mimo_cookie(paths=[str(blob)])
        passed &= check("解析出两个字段", header,
                        "api-platform_serviceToken=tok-abc; userId=4242")
        passed &= check("别的域不混入", "evil" in header, False)

        # 2. 布局变体自动探测：大端 + 页数字段在偏移 8
        blob2 = tmp / "cookies2.binarycookies"
        blob2.write_bytes(make_file(">", count_off=8,
                                    pages=[make_page(">", mimo_records(">", "be11", "777"))]))
        header2 = tl.read_safari_mimo_cookie(paths=[str(blob2)])
        passed &= check("大端布局也认得", header2,
                        "api-platform_serviceToken=be11; userId=777")

        # 2b. 其它文件头布局：页数@12、页后有余量、页表存的是页偏移
        for label, kwargs in (("页数@12+尾部余量", {"count_off": 12,
                                              "trailing": b"\x00" * 8}),
                              ("页表是页偏移", {"table": "offsets"})):
            blob_x = tmp / ("cookies-%d.binarycookies" % len(label))
            blob_x.write_bytes(make_file(
                "<", pages=[make_page("<", mimo_records("<", "v1", "2"))], **kwargs))
            header_x = tl.read_safari_mimo_cookie(paths=[str(blob_x)])
            passed &= check("布局变体：%s" % label, header_x,
                            "api-platform_serviceToken=v1; userId=2")

        # 2c. 页数可以超过 256（真实文件见到过 275 页）
        many = [make_page("<", mimo_records("<", "big-file", "3"))] + [
            make_page("<", [make_record("<", "other.com", "k", "/", "v")])] * 274
        blob_many = tmp / "cookies-many.binarycookies"
        blob_many.write_bytes(make_file("<", pages=many))
        passed &= check("页数 275 也能解析",
                        tl.read_safari_mimo_cookie(paths=[str(blob_many)]),
                        "api-platform_serviceToken=big-file; userId=3")

        # 2d. 混合字节序：文件头/页计数大端，偏移表和记录小端
        recs_m = mimo_records("<", "mix-tok", "5")
        head_m = 4 + 4 * len(recs_m)
        offsets_m, cursor_m = [], head_m
        for rec in recs_m:
            offsets_m.append(cursor_m)
            cursor_m += len(rec)
        page_m = (struct.pack(">I", len(recs_m))
                  + b"".join(struct.pack("<I", off) for off in offsets_m)
                  + b"".join(recs_m))
        blob_m = tmp / "cookies-mixed.binarycookies"
        blob_m.write_bytes(make_file(">", pages=[page_m]))
        passed &= check("混合字节序也认得",
                        tl.read_safari_mimo_cookie(paths=[str(blob_m)]),
                        "api-platform_serviceToken=mix-tok; userId=5")

        # 2e. 页间有对齐填充（页长取整到 2 字节，87 字节缺口就是这么来的）
        page_p = make_page("<", mimo_records("<", "pad-tok", "6"))
        if len(page_p) % 2 == 0:
            page_p += b"\x00"
        filler_p = make_page("<", [make_record("<", "other.com", "k", "/", "v")])
        table_p, body_p = [], b""
        for page in (page_p, filler_p):
            table_p.append(len(page))
            body_p += page
            if len(page) % 2:
                body_p += b"\x00"
        blob_p = tmp / "cookies-padded.binarycookies"
        blob_p.write_bytes(b"cook" + struct.pack(">I", 2)
                           + b"".join(struct.pack(">I", n) for n in table_p) + body_p)
        passed &= check("页间有对齐填充也认得",
                        tl.read_safari_mimo_cookie(paths=[str(blob_p)]),
                        "api-platform_serviceToken=pad-tok; userId=6")

        # 2f. 结构探针零泄露：输出里不能出现任何值
        probe_text = "\n".join(tl._bc_probe(make_page(
            "<", mimo_records("<", "probe-tok", "8"))))
        passed &= check("探针不含值", "probe-tok" in probe_text, False)
        passed &= check("探针给出串段位置", "串段（起点,长度）" in probe_text, True)

        # 2g. 真实 Safari 版式（实测）：文件头大端；页头 = 未知 + 计数@4 + 偏移表@8；
        # 记录 = 大小@0 + 偏移字段在 +16..+28（相对记录起点）+ 字符串从 +55 起
        def wild_record(domain, name, path, value):
            strings = [domain.encode(), name.encode(), path.encode(), value.encode()]
            rels, cursor = [], 55
            for item in strings:
                rels.append(cursor)
                cursor += len(item) + 1
            rec = struct.pack("<I", cursor + 4) + b"\x00" * 12
            rec += struct.pack("<IIII", *rels) + b"\x00" * 23
            return rec + b"".join(item + b"\x00" for item in strings)

        def wild_page(records):
            count = len(records)
            head = 8 + 4 * count
            first_off = (head + 7) // 8 * 8
            offsets, cursor = [], first_off
            for rec in records:
                offsets.append(cursor)
                cursor += len(rec)
            return (b"\x00\x00\x01\x00" + struct.pack("<I", count)
                    + b"".join(struct.pack("<I", off) for off in offsets)
                    + b"\x00" * (first_off - head) + b"".join(records))

        blob_w = tmp / "cookies-wild.binarycookies"
        blob_w.write_bytes(make_file(">", pages=[
            wild_page([
                wild_record(".xiaomimimo.com",
                            "api-platform_serviceToken", "/", "wild-tok"),
                wild_record(".xiaomimimo.com", "userId", "/", "42"),
            ]),
            wild_page([wild_record("other.com", "k2", "/", "v2")]),
        ]))
        passed &= check("真实 Safari 版式也认得",
                        tl.read_safari_mimo_cookie(paths=[str(blob_w)]),
                        "api-platform_serviceToken=wild-tok; userId=42")

        # 2h. 真实脏数据：空值、非 ASCII、坏记录混排，不拖垮解析
        def wild_record_raw(domain, name, path, value_bytes):
            strings = [domain.encode(), name.encode(), path.encode(), value_bytes]
            rels, cursor = [], 55
            for item in strings:
                rels.append(cursor)
                cursor += len(item) + 1
            rec = struct.pack("<I", cursor + 4) + b"\x00" * 12
            rec += struct.pack("<IIII", *rels) + b"\x00" * 23
            return rec + b"".join(item + b"\x00" for item in strings)

        bad_rec = wild_record_raw("bad.com", "ctrl", "/", b"\x01\x02")
        blob_d = tmp / "cookies-dirty.binarycookies"
        blob_d.write_bytes(make_file(">", pages=[
            wild_page([
                wild_record_raw("other.com", "empty_val", "/", b""),
                wild_record_raw("other.com", "uni_val", "/", "中文值".encode()),
                wild_record_raw(".xiaomimimo.com",
                                "api-platform_serviceToken", "/", b"dirty-tok"),
                wild_record_raw(".xiaomimimo.com", "userId", "/", b"11"),
                bad_rec,
            ]),
            wild_page([bad_rec, bad_rec]),
        ]))
        passed &= check("脏数据混排也认得",
                        tl.read_safari_mimo_cookie(paths=[str(blob_d)]),
                        "api-platform_serviceToken=dirty-tok; userId=11")

        # 2i. 前两段字符串顺序是（名,域）的版式：顺序无关识别
        blob_s = tmp / "cookies-swapped.binarycookies"
        blob_s.write_bytes(make_file(">", pages=[wild_page([
            wild_record_raw("api-platform_serviceToken", ".xiaomimimo.com",
                            "/", b"swap-tok"),
            wild_record_raw("userId", ".xiaomimimo.com", "/", b"77"),
        ])]))
        passed &= check("名字在前的版式也认得",
                        tl.read_safari_mimo_cookie(paths=[str(blob_s)]),
                        "api-platform_serviceToken=swap-tok; userId=77")

        # 2j. 域清单只列域名和 Cookie 名，不带值
        summary = "\n".join(tl._bc_domain_summary(tl._bc_records(blob_s.read_bytes())))
        passed &= check("域清单列出 mimo 域", "xiaomimimo.com" in summary, True)
        passed &= check("域清单不带值", "swap-tok" in summary, False)

        # 2k. SQLite 形态的 Cookie 库（WebKit 分库）
        db_cookie = tmp / "Cookies.sqlite"
        con = sqlite3.connect(str(db_cookie))
        con.execute("create table cookies (host text, name text, value text)")
        con.execute("insert into cookies values"
                    " ('.xiaomimimo.com', 'api-platform_serviceToken', 'sql-tok')")
        con.execute("insert into cookies values ('.xiaomimimo.com', 'userId', '88')")
        con.execute("insert into cookies values ('other.com', 'x', 'y')")
        con.commit()
        con.close()
        passed &= check("SQLite 库也认得",
                        tl.read_safari_mimo_cookie(paths=[str(db_cookie)]),
                        "api-platform_serviceToken=sql-tok; userId=88")

        # 2l. 自动发现分库文件（新版 Safari 把 Cookie 分库放）
        root = tmp / "fake-lib"
        deep = root / "A" / "B"
        deep.mkdir(parents=True)
        (deep / "Cookies.binarycookies").write_bytes(blob_s.read_bytes())
        (root / "notes.txt").write_text("x", encoding="utf-8")
        names = [p.name for p in tl._cookie_file_candidates(extra_roots=[root])]
        passed &= check("发现嵌套 Cookie 文件", "Cookies.binarycookies" in names, True)
        passed &= check("无关文件不收", "notes.txt" in names, False)

        # 2m. 深度扫描定位“名字不叫 cookie”的库：只报路径和命中串名
        (deep / "weirdname.dat").write_bytes(b"xx api-platform_serviceToken yy")
        hits, scanned = tl._scan_needles([root], (b"api-platform_serviceToken",))
        hit_names = [p.name for p, _ in hits]
        passed &= check("深度扫描找到怪名文件", "weirdname.dat" in hit_names, True)
        passed &= check("深度扫描标明命中串",
                        any(matched == ["api-platform_serviceToken"]
                            for p, matched in hits if p.name == "weirdname.dat"),
                        True)
        passed &= check("深度扫描覆盖到文件", scanned >= 3, True)

        # 3. 同名多条按时间取新：ts 大的赢
        blob3 = tmp / "cookies3.binarycookies"
        blob3.write_bytes(make_file("<", pages=[make_page("<", [
            make_record("<", "platform.xiaomimimo.com",
                        "api-platform_serviceToken", "/", "old-tok", ts=600000000.0),
            make_record("<", "platform.xiaomimimo.com",
                        "api-platform_serviceToken", "/", "new-tok", ts=700000000.0),
            make_record("<", ".xiaomimimo.com", "userId", "/", "1"),
        ])]))
        header3 = tl.read_safari_mimo_cookie(paths=[str(blob3)])
        passed &= check("同名取最新", "new-tok" in header3, True)

        # 4. 缺 userId / 损坏 / 文件不存在 → CookieError，且不泄露值
        blob4 = tmp / "cookies4.binarycookies"
        blob4.write_bytes(make_file("<", pages=[make_page("<", [
            make_record("<", "platform.xiaomimimo.com",
                        "api-platform_serviceToken", "/", "secret-tok"),
        ])]))
        for label, path in (("缺字段", blob4),
                            ("损坏文件", tmp / "broken.binarycookies"),
                            ("文件不存在", tmp / "nope.binarycookies")):
            if label == "损坏文件":
                path.write_bytes(b"xxxx" + b"\x00" * 40)
            try:
                tl.read_safari_mimo_cookie(paths=[str(path)])
                message = ""
            except tl.CookieError as exc:
                message = str(exc)
            passed &= check("%s 报 CookieError" % label, bool(message), True)
            passed &= check("%s 不泄露值" % label,
                            ("secret-tok" in message) or ("tok-abc" in message), False)

        # 5. 无权限 → 指引去开完全磁盘访问权限
        locked = tmp / "locked.binarycookies"
        locked.write_bytes(blob.read_bytes())
        os.chmod(locked, 0)
        try:
            try:
                tl.read_safari_mimo_cookie(paths=[str(locked)])
                message = ""
            except tl.CookieError as exc:
                message = str(exc)
            passed &= check("无权限时指路 FDA", "完全磁盘访问" in message, True)
        finally:
            os.chmod(locked, 0o600)

        # 6. validate：粘贴的 Cookie 头清洗 + 两个字段都要在
        passed &= check("去掉 Cookie: 前缀",
                        tl.validate_mimo_cookie("Cookie: api-platform_serviceToken=a; userId=1"),
                        "api-platform_serviceToken=a; userId=1")
        try:
            tl.validate_mimo_cookie("api-platform_serviceToken=a")
            message = ""
        except tl.BalanceError as exc:
            message = str(exc)
        passed &= check("缺 userId 要报错", "userId" in message, True)

        # 7. save_mimo_cookie：写回指定键、保留其它键、600 权限
        secrets = tmp / "secrets.json"
        write_json(secrets, {"deepseek": "sk-keep"})
        tl.save_mimo_cookie("api-platform_serviceToken=n; userId=2",
                            secrets_path=secrets)
        stored = json.loads(secrets.read_text(encoding="utf-8"))
        passed &= check("保留其它键", stored["deepseek"], "sk-keep")
        passed &= check("写入新 Cookie", stored["mimo_cookie"],
                        "api-platform_serviceToken=n; userId=2")
        passed &= check("权限 600",
                        stat.S_IMODE(os.stat(secrets).st_mode), 0o600)
        passed &= check("没有残留临时文件", (tmp / "secrets.json.tmp").exists(), False)

        # 8. refresh_mimo_cookie：读 → 校验 → 写回，全链路
        fresh = tmp / "fresh.binarycookies"
        fresh.write_bytes(make_file("<", pages=[
            make_page("<", mimo_records("<", "safari-tok", "9"))]))
        result = tl.refresh_mimo_cookie(secrets_path=secrets,
                                        secret_name="mimo_cookie",
                                        paths=[str(fresh)])
        stored = json.loads(secrets.read_text(encoding="utf-8"))
        passed &= check("续期返回新值", result,
                        "api-platform_serviceToken=safari-tok; userId=9")
        passed &= check("续期写回 secrets", stored["mimo_cookie"], result)

        # 9. refresh_balances：401 → 自动换 Cookie → 重试成功
        config = tmp / "balances.json"
        write_json(config, {
            "refresh_seconds": 600,
            "providers": [{"id": "mimo", "type": "mimo_cookie",
                           "name": "Xiaomi MiMo", "enabled": True,
                           "secret": "mimo_cookie"}],
        })
        db = tmp / "usage.db"
        write_json(secrets, {"mimo_cookie": "api-platform_serviceToken=old; userId=1"})
        calls = []

        def flaky(entry, key):
            calls.append(key)
            if len(calls) == 1:
                raise tl.BalanceAuthError("Cookie 已失效或缺少 userId，请重新登录后粘贴")
            return sample_items(88.0)

        refreshed = []

        def refresher():
            refreshed.append(1)
            return "api-platform_serviceToken=new; userId=2"

        result = tl.refresh_balances(db, force=True, config_path=config,
                                     secrets_path=secrets, fetcher=flaky,
                                     cookie_refresher=refresher, now=1000)
        passed &= check("自动换新后重试成功", result["refreshed"], ["mimo"])
        passed &= check("重试用的是新 Cookie", calls,
                        ["api-platform_serviceToken=old; userId=1",
                         "api-platform_serviceToken=new; userId=2"])
        passed &= check("自动续期只调一次", refreshed, [1])
        payload = tl.balance_payload(db, config_path=config, now=1000)
        passed &= check("余额已更新", payload["providers"][0]["items"][0]["total"], 88.0)
        passed &= check("错误清空", payload["providers"][0]["error"], None)

        # 10. 自动换新失败：保留旧值，错误里指手动兜底
        def always_auth(entry, key):
            raise tl.BalanceAuthError("Cookie 已失效或缺少 userId，请重新登录后粘贴")

        def broken_refresher():
            raise tl.CookieError("没权限读 Safari Cookie")

        result = tl.refresh_balances(db, force=True, config_path=config,
                                     secrets_path=secrets, fetcher=always_auth,
                                     cookie_refresher=broken_refresher, now=2000)
        message = result["errors"][0][1]
        passed &= check("换新失败要报原因", "自动换新 Cookie 失败" in message, True)
        passed &= check("指了手动兜底命令", "cookie --from-clipboard" in message, True)
        payload = tl.balance_payload(db, config_path=config, now=2000)
        passed &= check("失败保留旧值", payload["providers"][0]["items"][0]["total"], 88.0)

        # 11. 换新成功但接口还是拒：说明的不是换新本身的问题
        calls = []

        def stubborn(entry, key):
            calls.append(key)
            raise tl.BalanceAuthError("Cookie 已失效或缺少 userId，请重新登录后粘贴")

        result = tl.refresh_balances(db, force=True, config_path=config,
                                     secrets_path=secrets, fetcher=stubborn,
                                     cookie_refresher=refresher, now=3000)
        passed &= check("换新后仍失败要有说明",
                        "自动换新 Cookie 后仍失败" in result["errors"][0][1], True)
        passed &= check("换新后确实又请求了一次", len(calls), 2)

        # 12. auto_cookie=false：完全不碰浏览器，行为回到纯手动
        write_json(config, {
            "refresh_seconds": 600,
            "providers": [{"id": "mimo", "type": "mimo_cookie",
                           "name": "Xiaomi MiMo", "enabled": True,
                           "secret": "mimo_cookie", "auto_cookie": False}],
        })
        counter = []

        def counting_refresher():
            counter.append(1)
            return "x"

        result = tl.refresh_balances(db, force=True, config_path=config,
                                     secrets_path=secrets, fetcher=always_auth,
                                     cookie_refresher=counting_refresher, now=4000)
        passed &= check("auto_cookie=false 不续期", counter, [])
        passed &= check("auto_cookie=false 不带兜底提示",
                        "自动换新" in result["errors"][0][1], False)

        # 13. DeepSeek 的 401 不走换 Cookie 那条路
        write_json(config, {
            "refresh_seconds": 600,
            "providers": [{"id": "deepseek", "type": "deepseek",
                           "name": "DeepSeek", "enabled": True}],
        })
        write_json(secrets, {"deepseek": "sk-test-not-real"})

        def ds_auth(entry, key):
            raise tl.BalanceAuthError("Key 无效或没有权限")

        counter_ds = []

        def ds_refresher():
            counter_ds.append(1)
            return "x"

        result = tl.refresh_balances(db, force=True, config_path=config,
                                     secrets_path=secrets, fetcher=ds_auth,
                                     cookie_refresher=ds_refresher, now=5000)
        passed &= check("非 MiMo 不触发续期", counter_ds, [])
        passed &= check("非 MiMo 错误原样", result["errors"][0][1], "Key 无效或没有权限")

        # 14. 401 抛的是 BalanceAuthError（BalanceError 的子类）
        passed &= check("BalanceAuthError 是 BalanceError 子类",
                        issubclass(tl.BalanceAuthError, tl.BalanceError), True)

        # 15. 浏览器代查：让已登录的 Safari 标签页直接查余额（osascript 是假的）
        class FakeProc:
            def __init__(self, out=b"", err=b"", code=0):
                self.stdout, self.stderr, self.returncode = out, err, code

        real_run = tl.subprocess.run
        osascript_result = [FakeProc()]

        def fake_run(cmd, **kwargs):
            if cmd[0].endswith("pgrep"):
                return FakeProc(code=0)
            return osascript_result[0]

        tl.subprocess.run = fake_run
        try:
            osascript_result[0] = FakeProc(
                out=b"200\n{\"data\":[{\"balance\": 6.6, \"currency\": \"CNY\"}]}")
            items = tl.fetch_mimo_balance_via_safari()
            passed &= check("代查解析余额", items[0]["total"], 6.6)

            osascript_result[0] = FakeProc(out=b"401\n{}")
            caught = None
            try:
                tl.fetch_mimo_balance_via_safari()
            except tl.BalanceError as exc:
                caught = exc
            passed &= check("代查 401 走鉴权错误",
                            isinstance(caught, tl.BalanceAuthError), True)

            osascript_result[0] = FakeProc(out=b"ERR:NO_TAB")
            try:
                tl.fetch_mimo_balance_via_safari()
                message = ""
            except tl.BalanceError as exc:
                message = str(exc)
            passed &= check("没标签页有明确提示", "标签页" in message, True)

            osascript_result[0] = FakeProc(
                code=1, err="不允许发送 Apple 事件。(-1743)".encode())
            try:
                tl.fetch_mimo_balance_via_safari()
                message = ""
            except tl.BalanceError as exc:
                message = str(exc)
            passed &= check("自动化被拒时指路系统设置", "自动化" in message, True)

            # 浏览器代查开着时，没配 Cookie 也能刷新
            write_json(config, {
                "refresh_seconds": 600,
                "providers": [{"id": "mimo", "type": "mimo_cookie",
                               "name": "Xiaomi MiMo", "enabled": True,
                               "via_browser": True}],
            })
            write_json(secrets, {})
            osascript_result[0] = FakeProc(out=b"200\n{\"data\":[{\"balance\": 7.7}]}")
            result = tl.refresh_balances(db, force=True, config_path=config,
                                         secrets_path=secrets, now=6000)
            passed &= check("无 Cookie 也能靠代查刷新", result["refreshed"], ["mimo"])
            payload = tl.balance_payload(db, config_path=config, now=6000)
            passed &= check("代查余额进面板",
                            payload["providers"][0]["items"][0]["total"], 7.7)
        finally:
            tl.subprocess.run = real_run

        # 16. 两路都失败时，原因合并；via_browser=false 不碰浏览器
        real_browser = tl.fetch_mimo_balance_via_safari
        real_cookie = tl.fetch_mimo_balance
        counter_browser = []

        def fake_browser(*args, **kwargs):
            counter_browser.append(1)
            raise tl.BalanceError("代查挂了")

        def fake_cookie(*args, **kwargs):
            raise tl.BalanceError("Cookie 挂了")

        tl.fetch_mimo_balance_via_safari = fake_browser
        tl.fetch_mimo_balance = fake_cookie
        try:
            try:
                tl._fetch_mimo_items({"type": "mimo_cookie", "via_browser": True}, "")
                message = ""
            except tl.BalanceError as exc:
                message = str(exc)
            passed &= check("两路失败原因都带上",
                            "Cookie 挂了" in message and "代查挂了" in message, True)

            # Cookie 路 401 时，代查的失败原因也不能被盖掉
            def fake_cookie_auth(*args, **kwargs):
                raise tl.BalanceAuthError("Cookie 已失效")

            tl.fetch_mimo_balance = fake_cookie_auth
            caught = None
            try:
                tl._fetch_mimo_items({"type": "mimo_cookie", "via_browser": True}, "")
            except tl.BalanceError as exc:
                caught = exc
            passed &= check("401 仍走鉴权重试语义",
                            isinstance(caught, tl.BalanceAuthError), True)
            passed &= check("401 也带出代查原因",
                            "代查挂了" in str(caught or ""), True)
            tl.fetch_mimo_balance = fake_cookie
            baseline = len(counter_browser)
            try:
                tl._fetch_mimo_items({"type": "mimo_cookie", "via_browser": False}, "")
                message = ""
            except tl.BalanceError as exc:
                message = str(exc)
            passed &= check("关掉代查就不碰浏览器",
                            len(counter_browser) == baseline, True)
        finally:
            tl.fetch_mimo_balance_via_safari = real_browser
            tl.fetch_mimo_balance = real_cookie

    print()
    print("全部通过" if passed else "有失败项")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
