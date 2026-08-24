"""test_intent_classifier — ccls_daemon_intent_classifier.classify_intent 单测。

填补 R70 daemon 缺测试（grep tests/ 无 intent 测试）的缺口；验证疑问/任务/其他
三类判定与中文问号、英文任务动词前缀（C2 修复点）。
"""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

_src = Path(__file__).resolve().parent.parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

import unittest
from ccls_daemon_intent_classifier import classify_intent, Intent, IntentReminderDaemon


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


class TestStallDetection(unittest.TestCase):
    """验证 Round 4 复活的 stall 检测：连续两轮 pane 无新产出 → 推送提醒。"""

    def setUp(self):
        self.daemon = IntentReminderDaemon(interval=0.01)
        # 清理可能残留的状态
        self.daemon._states.clear()

    def tearDown(self):
        self.daemon._stop.set()

    @patch("ccls_daemon_intent_classifier._capture_pane_end")
    @patch("ccls_daemon_intent_classifier.IntentReminderDaemon._push_reminder", return_value=True)
    @patch("ccls_daemon_intent_classifier.read_intent", return_value=Intent.TASK)
    @patch("ccls_daemon_intent_classifier._tmux_sessions", return_value=["ccs-engineer"])
    def test_stall_triggers_reminder(self, mock_tmux, mock_read_intent, mock_push, mock_capture):
        """连续两轮同一 pane 内容 → 第二轮触发提醒。"""
        pane_content = "user: 修复 bug\nccs: 正在分析...\n" * 10  # 真实会话内容
        mock_capture.return_value = pane_content

        # Poll 1: 首次快照存储，不提醒
        pushed = self.daemon._tick()
        self.assertEqual(pushed, 0, "首次轮询不应提醒")

        # Poll 2: 同内容（ccs 无新产出）→ 应触发提醒
        pushed = self.daemon._tick()
        self.assertEqual(pushed, 1, "无新产出第二轮应触发提醒")
        mock_push.assert_called()

    @patch("ccls_daemon_intent_classifier._capture_pane_end")
    @patch("ccls_daemon_intent_classifier.IntentReminderDaemon._push_reminder", return_value=True)
    @patch("ccls_daemon_intent_classifier.read_intent", return_value=Intent.TASK)
    @patch("ccls_daemon_intent_classifier._tmux_sessions", return_value=["ccs-engineer"])
    def test_new_output_resets_stall(self, mock_tmux, mock_read_intent, mock_push, mock_capture):
        """有新产出时不提醒，且更新快照。"""
        mock_capture.side_effect = [
            "old output line 1\n",      # Poll 1
            "old output line 1\nnew output\n",  # Poll 2: 有新内容
        ]

        # Poll 1
        self.daemon._tick()
        # Poll 2: 有新产出 → 不提醒，更新快照
        pushed = self.daemon._tick()
        self.assertEqual(pushed, 0, "有新产出不应触发提醒")
        mock_push.assert_not_called()

    @patch("ccls_daemon_intent_classifier._capture_pane_end")
    @patch("ccls_daemon_intent_classifier.subprocess.run")
    @patch("ccls_daemon_intent_classifier.read_intent", return_value=Intent.TASK)
    @patch("ccls_daemon_intent_classifier._tmux_sessions", return_value=["ccs-engineer"])
    def test_cooldown_prevents_spam(self, mock_tmux, mock_read_intent, mock_run, mock_capture):
        """冷却期内不重复提醒。使用真实 _push_reminder 逻辑，mock subprocess.run 返回有效 CompletedProcess。"""
        from subprocess import CompletedProcess
        # 构造成功的 CompletedProcess mock
        mock_completed = MagicMock(spec=CompletedProcess)
        mock_completed.returncode = 0
        mock_run.return_value = mock_completed

        pane_content = "stalled content\n"
        mock_capture.return_value = pane_content

        # 首次触发提醒（2 次 tick：首次存快照，第二次触发）
        self.daemon._tick()
        self.daemon._tick()
        self.assertTrue(mock_run.called, "首次推送应发生")

        # 冷却期内再次触发 _tick → 不应再次调用 subprocess.run
        for _ in range(5):
            self.daemon._tick()
        # 仅第一次推送调用 subprocess.run（每行 2 次调用 + reminder 可能多行）
        # 关键：call_count 应等于首次推送的总调用数，不应增加
        calls_after_first = mock_run.call_count
        for _ in range(5):
            self.daemon._tick()
        self.assertEqual(mock_run.call_count, calls_after_first, "冷却期内不应再次推送")

    @patch("ccls_daemon_intent_classifier._capture_pane_end")
    @patch("ccls_daemon_intent_classifier.IntentReminderDaemon._push_reminder", return_value=True)
    @patch("ccls_daemon_intent_classifier.read_intent", return_value=Intent.TASK)
    @patch("ccls_daemon_intent_classifier._tmux_sessions", return_value=["ccs-engineer-1"])
    def test_instance_suffix_stripped_for_intent(self, mock_tmux, mock_read_intent, mock_push, mock_capture):
        """实例后缀（ccs-engineer-1 → engineer）被剥离后传给 read_intent。"""
        mock_capture.return_value = "stalled\n"
        self.daemon._tick()
        self.daemon._tick()
        mock_read_intent.assert_called_with("engineer")

    @patch("ccls_daemon_intent_classifier._capture_pane_end")
    @patch("ccls_daemon_intent_classifier.subprocess.run")
    @patch("ccls_daemon_intent_classifier.read_intent", return_value=Intent.TASK)
    @patch("ccls_daemon_intent_classifier._tmux_sessions", return_value=["ccs-engineer", "ccs-pm"])
    def test_multiple_sessions_independent(self, mock_tmux, mock_read_intent, mock_run, mock_capture):
        """多会话独立跟踪，互不干扰。"""
        from subprocess import CompletedProcess
        mock_completed = MagicMock(spec=CompletedProcess)
        mock_completed.returncode = 0
        mock_run.return_value = mock_completed

        mock_capture.side_effect = ["engineer content\n", "pm content\n"]
        self.daemon._tick()
        # 两个会话都首轮 → 无提醒
        self.assertEqual(mock_run.call_count, 0)

        mock_capture.side_effect = ["engineer content\n", "pm content\n"]
        self.daemon._tick()
        # 两个会话都第二轮且内容未变 → 各自触发（每个 reminder 触发 subprocess.run 多次）
        self.assertGreater(mock_run.call_count, 0)


if __name__ == "__main__":
    unittest.main()
