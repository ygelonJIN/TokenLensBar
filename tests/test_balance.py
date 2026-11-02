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

        # 10. OpenCode：控制台页面里抽额度窗口与 Zen 余额
        go_html = (
            '<script>self.__next_f.push([1,"20:{"rollingUsage":$R[31]={status:"ok",'
            'resetInSec:8660,usagePercent:25},"weeklyUsage":$R[32]={status:"ok",'
            'resetInSec:45200,usagePercent:50},"monthlyUsage":$R[33]={status:"ok",'
            'resetInSec:900000,usagePercent:10}"])</script>')

        windows = tl.opencode_windows(go_html)
        passed &= check("抽到三个额度窗口", sorted(windows), ["monthly", "rolling", "weekly"])
        passed &= check("rolling 百分比", windows["rolling"]["usage_percent"], 25.0)
        passed &= check("monthly 重置倒计时", windows["monthly"]["reset_in"], 900000.0)

        items = tl.opencode_go_items(go_html, {"window": "monthly"})
        passed &= check("月度剩余额度 = 60×(1-10%)", items[0]["total"], 54.0)
        passed &= check("OpenCode 用美元", items[0]["currency"], "USD")
        items = tl.opencode_go_items(go_html, {"window": "rolling"})
        passed &= check("滚动剩余额度 = 12×(1-25%)", items[0]["total"], 9.0)
        items = tl.opencode_go_items(go_html, {"window": "weekly"})
        passed &= check("每周剩余额度 = 30×(1-50%)", items[0]["total"], 15.0)

        try:
            tl.opencode_go_items(go_html, {"window": "hourly"})
            bad_window = ""
        except tl.BalanceError as exc:
            bad_window = str(exc)
        passed &= check("窗口名不认识时报错", "hourly" in bad_window, True)

        zen_html = '<div data-x=\'{"balance":-1392000000,"reloadAmount":20}\'></div>'
        zen = tl.opencode_zen_items(zen_html)
        passed &= check("Zen 余额按 1e-8 换算", zen[0]["total"], 13.92)

        try:
            tl.opencode_zen_items("<html>nothing</html>")
            no_balance = ""
        except tl.BalanceError as exc:
            no_balance = str(exc)
        passed &= check("页面没有余额字段时报错", "余额字段" in no_balance, True)

        # 10b. workspace 探测：从 /workspace 的重定向里认 wrk_
        class FakeOpener:
            def __init__(self, handler):
                self.handler = handler
                self.urls = []

            def open(self, request, timeout=0):
                self.urls.append(request.full_url)
                return self.handler(request)

        class FakeHtml:
            def __init__(self, text):
                self.text = text

            def read(self, _size):
                return self.text.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        seen = {}

        def fake_handler(request):
            if request.full_url.endswith("/workspace"):
                raise urllib.error.HTTPError(
                    request.full_url, 302, "Found",
                    {"Location": "https://opencode.ai/workspace/wrk_01ABC23DEF456/go"}, None)
            seen["cookie"] = request.get_header("Cookie")
            return FakeHtml(go_html)

        original_opener = tl._OPENCODE_OPENER
        tl._OPENCODE_OPENER = FakeOpener(fake_handler)
        try:
            auto = tl.fetch_opencode_balance({"type": "opencode"}, "Fe26.2**abc")
        finally:
            tl._OPENCODE_OPENER = original_opener
        passed &= check("自动探测到 workspace", auto[0]["total"], 54.0)
        passed &= check("Cookie 头补上 auth=", seen["cookie"], "auth=Fe26.2**abc")

        # 10c. 会话失效：3xx 到登录页
        def login_handler(request):
            raise urllib.error.HTTPError(
                request.full_url, 302, "Found",
                {"Location": "https://opencode.ai/console/login?next=%2Fworkspace"}, None)

        tl._OPENCODE_OPENER = FakeOpener(login_handler)
        try:
            try:
                tl.fetch_opencode_balance({"type": "opencode",
                                           "workspace_id": "wrk_01ABC23DEF456"},
                                          "auth=stale")
                login_error = ""
            except tl.BalanceError as exc:
                login_error = str(exc)
        finally:
            tl._OPENCODE_OPENER = original_opener
        passed &= check("登录页重定向提示重新登录", "重新登录" in login_error, True)

        # 10d. 余额刷新：opencode 没有 Key 也能靠浏览器登录态取数
        write_json(config, {
            "refresh_seconds": 600,
            "providers": [{"id": "opencode-go", "type": "opencode",
                           "secret": "opencode_cookie", "enabled": True,
                           "window": "monthly"}],
        })
        write_json(secrets, {})
        opencode_seen = {}

        def opencode_fetch(entry, key):
            opencode_seen["entry"] = entry
            return [{"currency": "USD", "total": 42.0, "granted": None,
                     "topped_up": None, "available": True}]

        result = tl.refresh_balances(db, force=True, config_path=config,
                                     secrets_path=secrets,
                                     fetcher=opencode_fetch, now=3000)
        passed &= check("opencode 无 Key 也刷新", result["refreshed"], ["opencode-go"])
        passed &= check("默认名字是 OpenCode Go",
                        opencode_seen["entry"]["name"], "OpenCode Go")
        payload = tl.balance_payload(db, config_path=config, now=3000)
        passed &= check("OpenCode 金额", payload["providers"][0]["items"][0]["total"], 42.0)
        passed &= check("OpenCode 币种", payload["providers"][0]["items"][0]["currency"], "USD")

        # 10f. 浏览器代查：不读 Cookie 文件，直接把页面拉回来
        def fake_browser(js, needle, timeout=15.0):
            if js is not tl.OPENCODE_SAFARI_JS or needle != "opencode.ai":
                return "1+1"
            return json.dumps({
                "goStatus": 200, "goURL": "/console/api/go/status",
                "go": json.dumps({"rolling": {"usagePercent": 12.5},
                                  "weekly": {"usagePercent": 40},
                                  "monthly": {"usagePercent": 10}}),
                "billStatus": 200, "bill": "{}",
                "text": "5-hour window | 0% used | Weekly | 0% used | "
                        "Monthly | 0% used | $0.00 | available credit",
            })

        original_browser = tl._run_browser_js
        original_read = tl.read_safari_opencode_cookie

        def no_cookie(paths=None):
            raise tl.CookieError("没有权限读 Safari Cookie")

        tl._run_browser_js = fake_browser
        tl.read_safari_opencode_cookie = no_cookie
        try:
            via = tl._fetch_opencode_items({"type": "opencode", "window": "monthly",
                                            "via_browser": True,
                                            "auto_cookie": True}, "")
        finally:
            tl._run_browser_js = original_browser
            tl.read_safari_opencode_cookie = original_read
        passed &= check("代查优先且不碰 Cookie 文件", via[0]["total"], 54.0)

        # 10f2. 真实接口结构：access.meters 里是 micro-cents，直接算剩余
        real_go = {"product": "go", "access": {"meters": {
            "fiveHour": {"limitMicroCents": "1200000000", "usedMicroCents": "0"},
            "week": {"limitMicroCents": "3000000000", "usedMicroCents": "0"},
            "month": {"limitMicroCents": "6000000000", "usedMicroCents": "1500000000"},
        }}}
        meters = tl._opencode_meters(real_go)
        passed &= check("micro-cents 换成美元", meters["monthly"]["limit"], 60.0)
        passed &= check("已用金额", meters["monthly"]["used"], 15.0)
        passed &= check("剩余金额", meters["monthly"]["remaining"], 45.0)
        payload_real = {"go_json": real_go, "go_text": "", "text": ""}
        passed &= check("按接口金额算剩余（不再乘百分比）",
                        tl.opencode_items_from_payload(
                            payload_real, {"window": "monthly"})[0]["total"], 45.0)
        try:
            tl.opencode_items_from_payload(
                {"go_json": {"access": {"meters": {
                    "month": {"limitMicroCents": "6000000000"}}}},
                 "go_text": "", "text": ""}, {"window": "weekly"})
            missing_window = ""
        except tl.BalanceError as exc:
            missing_window = str(exc)
        passed &= check("没这个窗口时报清楚", "weekly" in missing_window, True)

        # 10g. 没开标签页：退回 Cookie 路线，报错里两路原因都在
        def no_tab(js, needle, timeout=15.0):
            return "ERR:NO_TAB"

        tl._run_browser_js = no_tab
        tl.read_safari_opencode_cookie = no_cookie
        try:
            try:
                tl._fetch_opencode_items({"type": "opencode", "window": "monthly",
                                          "via_browser": True,
                                          "auto_cookie": True}, "")
                both_notes = ""
            except tl.BalanceError as exc:
                both_notes = str(exc)
        finally:
            tl._run_browser_js = original_browser
            tl.read_safari_opencode_cookie = original_read
        passed &= check("代查没标签页时报出来", "标签页" in both_notes, True)
        passed &= check("Cookie 文件失败也报出来", "没有权限" in both_notes, True)

        # 10h. 标签页在但没登录：直接说清要登录，别绕 Cookie
        def logged_out(js, needle, timeout=15.0):
            return "ERR:LOGIN"

        tl._run_browser_js = logged_out
        tl.read_safari_opencode_cookie = no_cookie
        try:
            try:
                tl._fetch_opencode_items({"type": "opencode", "window": "monthly",
                                          "via_browser": True,
                                          "auto_cookie": True}, "")
                login_note = ""
            except tl.BalanceError as exc:
                login_note = str(exc)
        finally:
            tl._run_browser_js = original_browser
            tl.read_safari_opencode_cookie = original_read
        passed &= check("没登录时提示去登录", "没登录" in login_note, True)

        # 10e. 有 provider 就对不上时要分开算，别把 opencode 的用量算给 DeepSeek
        deepseek_entry = {"id": "deepseek", "type": "deepseek", "name": "DeepSeek"}
        passed &= check(
            "opencode-go 的 deepseek 模型不算给 DeepSeek",
            tl._event_matches_provider({"provider": "opencode-go",
                                        "model": "deepseek-flash"}, deepseek_entry), False)
        passed &= check(
            "deepseek 自己的用量照算",
            tl._event_matches_provider({"provider": "deepseek",
                                        "model": "deepseek-flash"}, deepseek_entry), True)
        passed &= check(
            "opencode 记账认得自己",
            tl._event_matches_provider({"provider": "opencode-go",
                                        "model": "deepseek-flash"},
                                       {"id": "opencode-go", "type": "opencode",
                                        "name": "OpenCode Go"}), True)

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
