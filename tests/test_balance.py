#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""余额功能的回归测试：python3 tests/test_balance.py

不联网：DeepSeek / 小米 MiMo 的 HTTP 响应和 refresh 的 fetcher 都是假的。
"""

import json
import os
import sys
import tempfile
import urllib.error
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


def sample_items(total=110.0, available=True):
    return [{"currency": "CNY", "total": total, "granted": 10.0,
             "topped_up": total - 10.0, "available": available}]


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.request = None

    def read(self, _size):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def main():
    passed = True
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db = tmp / "usage.db"
        config = tmp / "balances.json"
        secrets = tmp / "secrets.json"
        write_json(config, {
            "refresh_seconds": 600,
            "providers": [{"id": "deepseek", "type": "deepseek",
                           "name": "DeepSeek", "enabled": True}],
        })

        # 1. 没有配置时完全不碰网络
        nobody = {"called": 0}

        def never(entry, key):
            nobody["called"] += 1
            return sample_items()

        result = tl.refresh_balances(db, force=True, config_path=tmp / "missing.json",
                                     secrets_path=secrets, fetcher=never)
        passed &= check("没有 balances.json 时不联网", nobody["called"], 0)
        passed &= check("没有配置时 configured=False", result["configured"], False)

        # 2. 有配置但没有 Key：只写错误状态
        result = tl.refresh_balances(db, force=True, config_path=config,
                                     secrets_path=secrets, fetcher=never)
        passed &= check("没有 Key 时不联网", nobody["called"], 0)
        passed &= check("没有 Key 时报错", result["errors"][0][1], "未配置 Key")
        payload = tl.balance_payload(db, config_path=config)
        passed &= check("payload 带上错误", payload["providers"][0]["error"], "未配置 Key")

        # 3. 正常刷新并缓存
        write_json(secrets, {"deepseek": "sk-test-not-real"})
        calls = []

        def ok(entry, key):
            calls.append((entry["id"], key))
            return sample_items()

        result = tl.refresh_balances(db, force=True, config_path=config,
                                     secrets_path=secrets, fetcher=ok, now=1000)
        passed &= check("刷新成功", result["refreshed"], ["deepseek"])
        passed &= check("请求用的是整串 Key", calls[0][1], "sk-test-not-real")
        payload = tl.balance_payload(db, config_path=config, now=1000)
        item = payload["providers"][0]["items"][0]
        passed &= check("余额数值正确", item["total"], 110.0)
        passed &= check("币种正确", item["currency"], "CNY")
        passed &= check("刷新成功后错误清空", payload["providers"][0]["error"], None)
        con = tl.connect(db)
        sample_count = con.execute(
            "select count(*) from balance_samples where provider='deepseek'").fetchone()[0]
        con.close()
        passed &= check("刷新同时写入余额历史样本", sample_count, 1)

        # 4. 刷新间隔内不重复请求；force 才请求
        result = tl.refresh_balances(db, force=False, config_path=config,
                                     secrets_path=secrets, fetcher=ok, now=1001)
        passed &= check("间隔内跳过", result["skipped"], ["deepseek"])
        passed &= check("间隔内没有额外请求", len(calls), 1)
        result = tl.refresh_balances(db, force=True, config_path=config,
                                     secrets_path=secrets, fetcher=ok, now=1002)
        passed &= check("force 会刷新", result["refreshed"], ["deepseek"])
        passed &= check("force 多请求一次", len(calls), 2)

        # 5. 失败时保留上次余额
        def fail(entry, key):
            raise tl.BalanceError("Key 无效或没有权限")

        result = tl.refresh_balances(db, force=True, config_path=config,
                                     secrets_path=secrets, fetcher=fail, now=1003)
        passed &= check("失败写错误", result["errors"][0][1], "Key 无效或没有权限")
        payload = tl.balance_payload(db, config_path=config, now=1003)
        passed &= check("失败时保留旧余额", payload["providers"][0]["items"][0]["total"], 110.0)
        passed &= check("失败时 payload 带错误", payload["providers"][0]["error"], "Key 无效或没有权限")

        # 6. 占位符 Key 视为未配置
        write_json(secrets, {"deepseek": "PASTE_YOUR_DEEPSEEK_API_KEY_HERE"})
        loaded, _ = tl.load_secrets(secrets)
        passed &= check("占位符 Key 不当作真 Key", loaded, {})

        # 7. 解析 DeepSeek 返回（含多币种和不可用）
        response = {
            "is_available": True,
            "balance_infos": [
                {"currency": "CNY", "total_balance": "110.00",
                 "granted_balance": "10.00", "topped_up_balance": "100.00"},
                {"currency": "USD", "total_balance": 5.5,
                 "granted_balance": "0", "topped_up_balance": "5.5"},
            ],
        }
        original_open = tl.urllib.request.urlopen
        seen_request = {}

        def fake_open(request, timeout=0):
            seen_request["auth"] = request.get_header("Authorization")
            return FakeResponse(response)

        tl.urllib.request.urlopen = fake_open
        try:
            items = tl.fetch_deepseek_balance({"type": "deepseek"}, "sk-test-not-real")
        finally:
            tl.urllib.request.urlopen = original_open
        passed &= check("解析出两种币种", len(items), 2)
        passed &= check("CNY 总额", items[0]["total"], 110.0)
        passed &= check("USD 总额", items[1]["total"], 5.5)
        passed &= check("请求头是 Bearer", seen_request["auth"], "Bearer sk-test-not-real")

        # 7b. 空 balance_infos 按 0 元处理
        original_open = tl.urllib.request.urlopen
        tl.urllib.request.urlopen = lambda request, timeout=0: FakeResponse(
            {"is_available": False, "balance_infos": []})
        try:
            empty = tl.fetch_deepseek_balance({"type": "deepseek"}, "sk-test-not-real")
        finally:
            tl.urllib.request.urlopen = original_open
        passed &= check("空余额返回 0 元", empty[0]["total"], 0.0)
        passed &= check("空余额标记不可用", empty[0]["available"], False)

        # 9. 小米 MiMo：Cookie 鉴权（没有 API Key 余额接口）
        write_json(config, {
            "refresh_seconds": 600,
            "providers": [{"id": "mimo", "type": "mimo_cookie",
                           "secret": "mimo_cookie", "enabled": True}],
        })
        write_json(secrets, {"mimo_cookie": "api-platform_serviceToken=tok; userId=42"})
        mimo_seen = {}

        def mimo_fetch(entry, key):
            mimo_seen["cookie"] = key
            return sample_items(total=88.5)

        result = tl.refresh_balances(db, force=True, config_path=config,
                                     secrets_path=secrets, fetcher=mimo_fetch, now=2000)
        passed &= check("MiMo 读的是 Cookie", mimo_seen["cookie"],
                        "api-platform_serviceToken=tok; userId=42")
        payload = tl.balance_payload(db, config_path=config, now=2000)
        passed &= check("MiMo 默认名字", payload["providers"][0]["name"], "Xiaomi MiMo")
        passed &= check("MiMo 金额", payload["providers"][0]["items"][0]["total"], 88.5)

        # 9b. 解析控制台返回，并核对请求头
        seen = {}

        def fake_mimo_open(request, timeout=0):
            seen["cookie"] = request.get_header("Cookie")
            return FakeResponse({"data": {"currency": "CNY", "balance": "88.50",
                                          "availableBalance": "88.50"}})

        original_open = tl.urllib.request.urlopen
        tl.urllib.request.urlopen = fake_mimo_open
        try:
            items = tl.fetch_mimo_balance(
                {"type": "mimo_cookie"},
                "Cookie: api-platform_serviceToken=tok; userId=42")
        finally:
            tl.urllib.request.urlopen = original_open
        passed &= check("MiMo 余额数值", items[0]["total"], 88.5)
        passed &= check("MiMo 币种", items[0]["currency"], "CNY")
        passed &= check("MiMo 发 Cookie 头", seen["cookie"],
                        "api-platform_serviceToken=tok; userId=42")

        # 9c. 401 和缺字段的报错要说人话
        def fake_mimo_401(request, timeout=0):
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", None, None)

        original_open = tl.urllib.request.urlopen
        tl.urllib.request.urlopen = fake_mimo_401
        try:
            try:
                tl.fetch_mimo_balance({"type": "mimo_cookie"},
                                      "api-platform_serviceToken=bad; userId=42")
                mimo_401 = ""
            except tl.BalanceError as exc:
                mimo_401 = str(exc)
        finally:
            tl.urllib.request.urlopen = original_open
        passed &= check("MiMo 401 提示重新登录", "重新登录" in mimo_401, True)

        try:
            tl.fetch_mimo_balance({"type": "mimo_cookie"}, "userId=42")
            mimo_missing = ""
        except tl.BalanceError as exc:
            mimo_missing = str(exc)
        passed &= check("MiMo 缺 serviceToken 报错", "serviceToken" in mimo_missing, True)

        try:
            tl.fetch_mimo_balance({"type": "mimo_cookie"},
                                  "api-platform_serviceToken=tok")
            mimo_no_uid = ""
        except tl.BalanceError as exc:
            mimo_no_uid = str(exc)
        passed &= check("MiMo 缺 userId 报错", "userId" in mimo_no_uid, True)

        # 8. 401 不泄露 Key
        def fake_401(request, timeout=0):
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", None, None)

        tl.urllib.request.urlopen = fake_401
        try:
            try:
                tl.fetch_deepseek_balance({"type": "deepseek"}, "sk-super-secret")
                message = ""
            except tl.BalanceError as exc:
                message = str(exc)
        finally:
            tl.urllib.request.urlopen = original_open
        passed &= check("401 转成 Key 无效", message, "Key 无效或没有权限")
        passed &= check("错误信息不含 Key", "sk-super-secret" in message, False)

    print()
    print("全部通过" if passed else "有失败项")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
