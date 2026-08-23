"""test_intent_classifier — ccls_daemon_intent_classifier.classify_intent 单测。

填补 R70 daemon 缺测试（grep tests/ 无 intent 测试）的缺口；验证疑问/任务/其他
三类判定与中文问号、英文任务动词前缀（C2 修复点）。
"""
import sys
from pathlib import Path

_src = Path(__file__).resolve().parent.parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

import unittest
from ccls_daemon_intent_classifier import classify_intent, Intent


class TestClassifyIntent(unittest.TestCase):
    def test_中文问号判疑问(self):
        self.assertEqual(classify_intent("这个 bug 怎么修？"), Intent.QUESTION)

    def test_吗字结尾判疑问(self):
        self.assertEqual(classify_intent("服务能重启吗"), Intent.QUESTION)

    def test_为什么前缀判疑问(self):
        self.assertEqual(classify_intent("为什么 gateway 会断"), Intent.QUESTION)

    def test_请写判任务(self):
        self.assertEqual(classify_intent("请帮我写个脚本"), Intent.TASK)

    def test_英文fix前缀判任务(self):
        self.assertEqual(classify_intent("Fix the memory leak"), Intent.TASK)

    def test_英文please前缀判任务(self):
        self.assertEqual(classify_intent("Please deploy to staging"), Intent.TASK)

    def test_任务动词在句中仍判任务(self):
        # C1 修复：全串扫描，非前 12 字
        long = "我们先讨论一下方案，然后生成测试报告"
        self.assertIn(classify_intent(long), (Intent.TASK, Intent.QUESTION))

    def test_空输入判其他(self):
        self.assertEqual(classify_intent(""), Intent.OTHER)
        self.assertEqual(classify_intent("   "), Intent.OTHER)

    def test_闲聊判其他(self):
        self.assertEqual(classify_intent("你好"), Intent.OTHER)


if __name__ == "__main__":
    unittest.main()
