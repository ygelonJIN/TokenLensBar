#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把六组脚本式回归挂到 unittest 上：python3 -m unittest discover tests

其他几组（test_pricing / test_panel / test_spend / test_scan / test_balance / test_cookie）
都是带 main() 的独立脚本，不是 TestCase，`unittest discover` 一条也收不到。
这里逐个子进程跑一遍，把退出码变成一个真正的测试用例。
改完代码跑这一条就够；单组调试仍然可以 python3 tests/xxx.py。
"""

import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GROUPS = ["test_pricing", "test_panel", "test_spend",
          "test_scan", "test_balance", "test_cookie"]


class ScriptSuites(unittest.TestCase):
    def test_scripts(self):
        for name in GROUPS:
            with self.subTest(group=name):
                result = subprocess.run(
                    [sys.executable, os.path.join(HERE, name + ".py")],
                    capture_output=True, text=True, timeout=300)
                self.assertEqual(
                    result.returncode, 0,
                    msg="%s 失败：\n%s\n%s" % (name, result.stdout[-2000:], result.stderr[-2000:]))


if __name__ == "__main__":
    unittest.main()
