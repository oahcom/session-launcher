"""test_intent_classifier ─ ccls_daemon_intent_classifier 單元測試。

驗證：(1) 意圖分類三類判定；(2) 停滞檢測連續兩輪無新產出→提醒；
(3) 品質回歸：停滞自檢提醒不得注入虛假「用户直接指令」前提，bus 痕跡入 monitor_audit。
"""
import unittest
from subprocess import CompletedProcess
from unittest.mock import patch, MagicMock

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
    """驗證停滞檢測：連續兩輪 pane 無新產出 → 第二輪推送停滞自檢提醒。

    註：daemon 不再分類 ccs 輸出（D3），僅憑 pane 快照是否變化判定停滞；
    意圖判定(QUESTION/TASK)歸屬 user→CCS 的 ccs.send→inject_reminder 路徑，
    daemon 不負意圖判定責任。
    """

    def setUp(self):
        self.daemon = IntentReminderDaemon(interval=0.01)
        self.daemon._states.clear()

    def tearDown(self):
        self.daemon._stop.set()

    @patch("ccls_daemon_intent_classifier._capture_pane_end")
    @patch("ccls_daemon_intent_classifier.IntentReminderDaemon._push_reminder", return_value=True)
    @patch("ccls_daemon_intent_classifier._tmux_sessions", return_value=["ccs-engineer"])
    def test_stall_triggers_reminder(self, mock_tmux, mock_push, mock_capture):
        """連續兩輪同一 pane 内容 → 第二輪觸發提醒。"""
        mock_capture.return_value = "user: 修复 bug\nccs: 正在分析...\n" * 10

        pushed = self.daemon._tick()
        self.assertEqual(pushed, 0, "首次輪詢不應提醒（僅存快照）")

        pushed = self.daemon._tick()
        self.assertEqual(pushed, 1, "無新產出第二輪應觸發提醒")
        mock_push.assert_called()

    @patch("ccls_daemon_intent_classifier._capture_pane_end")
    @patch("ccls_daemon_intent_classifier.IntentReminderDaemon._push_reminder", return_value=True)
    @patch("ccls_daemon_intent_classifier._tmux_sessions", return_value=["ccs-engineer"])
    def test_new_output_resets_stall(self, mock_tmux, mock_push, mock_capture):
        """有新產出時不提醒，並更新快照。"""
        mock_capture.side_effect = [
            "old output line 1\n",
            "old output line 1\nold output\n",
        ]
        self.daemon._tick()
        pushed = self.daemon._tick()
        self.assertEqual(pushed, 0, "有新產出不應觸發提醒")
        mock_push.assert_not_called()

    @patch("ccls_daemon_intent_classifier._tmux_send")
    @patch("ccls_daemon_intent_classifier._capture_pane_end")
    @patch("ccls_daemon_intent_classifier.subprocess.run")
    @patch("ccls_daemon_intent_classifier._tmux_sessions", return_value=["ccs-engineer"])
    def test_cooldown_prevents_spam(self, mock_tmux, mock_run, mock_capture, mock_send):
        """冷卻期內不重複推送。

        注入走 _tmux_send 而非 subprocess.run（后者仅供 bus trace 等使用），
        故以 _tmux_send 调用次数判定推送。
        """
        mock_run.return_value = MagicMock(spec=CompletedProcess, returncode=0)
        mock_capture.return_value = "stalled content\n"

        self.daemon._tick()
        self.daemon._tick()
        self.assertTrue(mock_send.called, "首次推送應發生")
        calls_after_first = mock_send.call_count
        for _ in range(5):
            self.daemon._tick()
        self.assertEqual(mock_send.call_count, calls_after_first, "冷卻期內不應再次推送")

    @patch("ccls_daemon_intent_classifier._capture_pane_end")
    @patch("ccls_daemon_intent_classifier.subprocess.run")
    @patch("ccls_daemon_intent_classifier._tmux_sessions", return_value=["ccs-engineer", "ccs-pm"])
    def test_multiple_sessions_independent(self, mock_tmux, mock_run, mock_capture):
        """多會話獨立跟蹤，互不干擾。"""
        mock_run.return_value = MagicMock(spec=CompletedProcess, returncode=0)
        mock_capture.side_effect = ["engineer content\n", "pm content\n"]
        self.daemon._tick()
        self.assertEqual(mock_run.call_count, 0)

        mock_capture.side_effect = ["engineer content\n", "pm content\n"]
        self.daemon._tick()
        self.assertGreater(mock_run.call_count, 0)


class TestReminderQuality(unittest.TestCase):
    """回归：停滞自检提醒必须诚实，不得注入虚假「用户直接指令」前提；bus 痕迹入 monitor_audit。"""

    def test_stagnation_reminder_has_no_false_user_premise(self):
        d = IntentReminderDaemon(interval=0.01)
        txt = d._stagnation_reminder(2)
        self.assertNotIn("你收到的是用户直接指令", txt)
        self.assertNotIn("直接指令", txt)
        from user_intent_reminder import _CORE_QUESTIONS
        self.assertIn(_CORE_QUESTIONS[0], txt)
        self.assertIn(_CORE_QUESTIONS[1], txt)
        self.assertIn("停滞自检", txt)

    @patch("ccls_daemon_intent_classifier.subprocess.run")
    def test_bus_trace_writes_monitor_audit_not_code_fix(self, mock_run):
        mock_run.return_value = MagicMock(spec=CompletedProcess, returncode=0)
        d = IntentReminderDaemon(interval=0.01)
        d._bus_trace("ccs-maintainer")
        self.assertTrue(mock_run.called, "bus trace 应调用 bus_client")
        args = mock_run.call_args[0][0]
        self.assertIn("monitor_audit", args, "痕迹应写入 monitor_audit")
        self.assertNotIn("code_fix", args, "不得再污染 code_fix")


class TestInjectTextAsSingleMessage(unittest.TestCase):
    """多行提醒必须作为**一条**消息注入，不得逐行提交。

    回归：曾用 `for line: send-keys -l line; send-keys Enter`，每行独立成
    消息（实测 coordinator 收到 22 字符的裸首行
    「【停滞自检 · 每轮必对齐】第1次重复自检：」），角色为每行各起一个回合应答。
    """

    @patch("ccls_daemon_intent_classifier._tmux_send")
    def test_multiline_text_injected_once(self, mock_send):
        from ccls_daemon_intent_classifier import _inject_text
        _inject_text("ccs-probe", "L1\nL2\nL3")
        self.assertEqual(mock_send.call_count, 1,
                         "多行文本应一次投递，不应逐行发送")

    @patch("ccls_daemon_intent_classifier._tmux_send")
    def test_text_passed_through_unaltered(self, mock_send):
        from ccls_daemon_intent_classifier import _inject_text
        text = "L1\nL2\nL3"
        _inject_text("ccs-probe", text)
        self.assertEqual(mock_send.call_args[0][1], text,
                         "文本应原样传递，不得按行拆分或重组")

    @patch("ccls_daemon_intent_classifier._tmux_send")
    def test_push_reminder_sends_whole_body(self, mock_send):
        """_push_reminder 主体走单次注入（含越界摘要分支）。"""
        d = IntentReminderDaemon(interval=0.01)
        with patch.object(d, "_recent_violations_summary", return_value=""):
            d._push_reminder("ccs-probe", round_no=1)
        self.assertEqual(mock_send.call_count, 1, "整条提醒应一次投递")
        body = mock_send.call_args[0][1]
        self.assertIn("停滞自检", body)
        self.assertGreater(len(body.split("\n")), 1,
                           "提醒本身应是多行，用于验证整条投递")

    def test_tmux_send_not_degraded_stub(self):
        """_tmux_send 必须来自 tmux_ops，不得是降级桩（否则注入静默失败）。"""
        import ccls_daemon_intent_classifier as m
        self.assertEqual(getattr(m._tmux_send, "__module__", ""), "tmux_ops",
                         "tmux_ops 加载失败，注入降级为抛异常桩")


if __name__ == "__main__":
    unittest.main()
