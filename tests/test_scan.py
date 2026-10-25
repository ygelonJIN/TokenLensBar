#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫描链路的回归测试：python3 tests/test_scan.py

重点是模拟 launchd 的窄 PATH（/usr/bin:/bin:/usr/sbin:/sbin）：
Homebrew 装的 zstd 不在 PATH 里时也必须找得到，否则 DSH 会话会被当成
“正在写入”永久跳过，今日用量一直是 0。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tokenlensbar as tl  # noqa: E402

NARROW_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def check(name, actual, expected):
    ok = actual == expected
    print("%s %s" % ("✓" if ok else "✗", name))
    if not ok:
        print("   期望 %r，实际 %r" % (expected, actual))
    return ok


def fixture_records():
    return [
        {"type": "session", "id": "s1"},
        {"type": "model/selection",
         "data": {"provider": "deepseek-account", "model": "deepseek-flash"}},
        {"type": "assistant/message", "time": 1792891704250,
         "data": {"turn": 1, "step": 1,
                  "usage": {"inputTokens": 687, "outputTokens": 217,
                            "cacheReadTokens": 8320, "cacheWriteTokens": 0,
                            "totalTokens": 9224}}},
    ]


def main():
    passed = True
    binary = tl.find_zstd()
    if binary is None:
        print("! 本机没装 zstd，跳过压缩相关测试")
        return 0

    saved_path = os.environ.get("PATH")
    saved_override = os.environ.pop("TOKENLENSBAR_ZSTD", None)
    work = Path(tempfile.mkdtemp(prefix="tlb-scan-"))
    try:
        # 1. launchd 的 PATH 里没有 Homebrew，仍然要能定位到 zstd
        os.environ["PATH"] = NARROW_PATH
        passed &= check("窄 PATH 下仍能找到 zstd", tl.find_zstd() is not None, True)

        # 2. 显式指定了不存在的路径时，不要偷偷换成别的二进制
        os.environ["TOKENLENSBAR_ZSTD"] = str(work / "missing-zstd")
        passed &= check("显式路径无效时不回退", tl.find_zstd(), None)
        os.environ.pop("TOKENLENSBAR_ZSTD", None)

        # 3. 用窄 PATH 跑完整的 DSH 解析：解压 + 归一化 usage
        source = work / "session.v4.jsonl"
        source.write_text(
            "\n".join(json.dumps(obj, ensure_ascii=False) for obj in fixture_records()) + "\n",
            encoding="utf-8")
        compressed = work / "session.v4.jsonl.zstd"
        subprocess.run([binary, "-q", "-f", "-o", str(compressed), str(source)],
                       check=True, stdout=subprocess.DEVNULL)

        text = tl.zstd_decompress(compressed)
        passed &= check("窄 PATH 下能解压会话", bool(text and '"assistant/message"' in text), True)

        events = list(tl.DshAdapter().parse(compressed))
        passed &= check("DSH 解析出一条事件", len(events), 1)
        if events:
            event = events[0]
            passed &= check("事件 id", event["id"], "dsh:s1:1:1")
            passed &= check("模型", event["model"], "deepseek-flash")
            passed &= check("厂商", event["provider"], "deepseek")
            # inputTokens 与 cacheReadTokens 并列，归一化成 input 包含缓存
            passed &= check("输入包含缓存", event["input"], 9007)
            passed &= check("缓存命中", event["cache_read"], 8320)
            passed &= check("输出", event["output"], 217)
            passed &= check("总量", event["total"], 9224)
    finally:
        if saved_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = saved_path
        if saved_override is not None:
            os.environ["TOKENLENSBAR_ZSTD"] = saved_override
        shutil.rmtree(work, ignore_errors=True)

    print()
    print("全部通过" if passed else "有失败项")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
