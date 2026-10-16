"""test_intent_classifier ─ ccls_daemon_intent_classifier 單元測試。

驗證：(1) 意圖分類三類判定；(2) 請求驅動觸發——僅在角色存在未對齊用戶請求
時重複提醒，無請求零注入；(3) 單次請求預算上限與新請求重置。
"""
import time
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


class TestRequestDrivenTrigger(unittest.TestCase):
    """驗證觸發源為「該角色存在未對齊用戶請求」，而非 pane 無新產出。

    缺陷實證（2026-10-16）：原判據 `text == st.last_snippet` 對 infinite 常駐
    角色等價於「角色空閒」，而其空閒是設計正常態 → 注入提醒 → 角色回「待機」→
    pane 變化 → 預算重置 → 再注入，自激循環（pm 250 輪 / coordinator 193 輪 /
    engineer 175 輪）。改用請求驅動後，無請求即零注入。
    """

    def setUp(self):
        self.daemon = IntentReminderDaemon(interval=0.01)
        self.daemon._states.clear()

    def tearDown(self):
        self.daemon._stop.set()

    def _run(self, ticks, injects, traces):
        self.daemon._read_request_traces = staticmethod(lambda: traces)
        with patch("ccls_daemon_intent_classifier._tmux_sessions",
                   return_value=["ccs-engineer"]), \
             patch("ccls_daemon_intent_classifier._inject_text",
                   side_effect=lambda s, t: injects.append(t)), \
             patch("ccls_daemon_intent_classifier.subprocess.run"), \
             patch("ccls_daemon_intent_classifier.time.monotonic",
                   side_effect=range(0, 10 ** 7, 100)):
            for _ in range(ticks):
                self.daemon._tick()

    def test_no_user_request_injects_nothing(self):
        """無用戶請求 → 零注入（角色空閒是正常態，不得當停滯轟擊）。"""
        injects = []
        self._run(50, injects, {})
        self.assertEqual(injects, [], "無用戶請求時不得注入任何內容")

    def test_expired_request_injects_nothing(self):
        """請求已過 TTL → 不再重複提醒。"""
        injects = []
        stale = time.time() - IntentReminderDaemon._REQUEST_TTL - 100
        self._run(50, injects, {"engineer": stale})
        self.assertEqual(injects, [], "過期請求不應再提醒")

    def test_open_request_triggers_reminders_capped(self):
        """存在未對齊請求 → 重複提醒，且不超過單次請求預算。"""
        injects = []
        self._run(50, injects, {"engineer": time.time()})
        self.assertEqual(len(injects), IntentReminderDaemon._MAX_REMINDERS,
                         f"應提醒 {IntentReminderDaemon._MAX_REMINDERS} 次，"
                         f"實際 {len(injects)}")

    def test_reminder_uses_user_intent_wording(self):
        """前提已由 bus 痕跡確證 → 用用戶要求的原始措辭，不再用「停滯自檢」框架。"""
        injects = []
        self._run(50, injects, {"engineer": time.time()})
        self.assertTrue(injects, "應有提醒")
        body = injects[0]
        self.assertIn("你收到的是任务", body)
        from user_intent_reminder import _CORE_QUESTIONS
        self.assertIn(_CORE_QUESTIONS[0], body)
        self.assertIn(_CORE_QUESTIONS[1], body)

    def test_new_request_resets_budget(self):
        """新一輪用戶請求到達 → 預算重置，可再次提醒。"""
        injects = []
        base = time.time()
        self._run(50, injects, {"engineer": base})
        self.assertEqual(len(injects), IntentReminderDaemon._MAX_REMINDERS)
        self._run(50, injects, {"engineer": base + 60})  # 新請求
        self.assertEqual(len(injects), IntentReminderDaemon._MAX_REMINDERS * 2,
                         "新請求後應可再次提醒（預算重置）")

    def test_multiple_sessions_independent(self):
        """多會話獨立跟蹤，互不干擾。"""
        d = self.daemon
        d._read_request_traces = staticmethod(lambda: {})
        with patch("ccls_daemon_intent_classifier._tmux_sessions",
                   return_value=["ccs-engineer", "ccs-pm"]), \
             patch("ccls_daemon_intent_classifier._inject_text") as inject:
            d._tick()
            inject.assert_not_called()


class TestRoleOf(unittest.TestCase):
    """_role_of：tmux 會話名 → 角色名，必須保留角色名自身的連字號。

    缺陷實證（2026-10-16 Codex 審查 P2）：原用 `role.rsplit('-', 1)[0]`，
    對角色名含連字號的會話（生產中的 ccs-monitor / codex-dev）會砍掉真名 →
    與痕跡標題永不相等 → 靜默零注入（無錯誤、無日誌）。
    """

    def test_plain_role(self):
        self.assertEqual(IntentReminderDaemon._role_of("ccs-engineer"), "engineer")

    def test_instance_suffix_stripped(self):
        """實例後綴（純數字）應被剝離，歸併到同一角色。"""
        self.assertEqual(IntentReminderDaemon._role_of("ccs-pm-2"), "pm")

    def test_hyphenated_role_kept(self):
        """角色名自帶連字號時必須完整保留。"""
        self.assertEqual(IntentReminderDaemon._role_of("ccs-ccs-monitor"),
                         "ccs-monitor")
        self.assertEqual(IntentReminderDaemon._role_of("ccs-codex-dev"),
                         "codex-dev")


class TestRequestLookup(unittest.TestCase):
    """_read_request_traces：一次讀回全部角色的「最近一次用戶請求時刻」。"""

    def _facts(self, entries):
        return {"facts": [{"title": f"[user_intent_reminder] 已注入意图提醒 → {r}",
                           "timestamp": ts} for r, ts in entries]}

    @patch("ccls_daemon_intent_classifier.subprocess.run")
    def test_maps_all_roles(self, mock_run):
        import json
        mock_run.return_value = MagicMock(
            spec=CompletedProcess, returncode=0,
            stdout=json.dumps(self._facts([("engineer", 111.0), ("qa", 222.0)])))
        self.assertEqual(IntentReminderDaemon._read_request_traces(),
                         {"engineer": 111.0, "qa": 222.0})

    @patch("ccls_daemon_intent_classifier.subprocess.run")
    def test_keeps_latest_per_role(self, mock_run):
        """同角色多條痕跡 → 取最新（舊請求不應覆蓋新請求）。"""
        import json
        mock_run.return_value = MagicMock(
            spec=CompletedProcess, returncode=0,
            stdout=json.dumps(self._facts([("qa", 222.0), ("qa", 999.0)])))
        self.assertEqual(IntentReminderDaemon._read_request_traces(), {"qa": 999.0})

    @patch("ccls_daemon_intent_classifier.subprocess.run")
    def test_ignores_non_trace_facts(self, mock_run):
        """非痕跡條目（其他 code_fix）不得被當成請求。"""
        import json
        mock_run.return_value = MagicMock(
            spec=CompletedProcess, returncode=0,
            stdout=json.dumps({"facts": [
                {"title": "[other] 修復完成", "timestamp": 1.0}]}))
        self.assertEqual(IntentReminderDaemon._read_request_traces(), {})

    @patch("ccls_daemon_intent_classifier.subprocess.run")
    def test_returns_empty_when_no_trace(self, mock_run):
        """無痕跡 → {}（寧可不提醒，也不憑推測注入）。"""
        import json
        mock_run.return_value = MagicMock(
            spec=CompletedProcess, returncode=0, stdout=json.dumps({"facts": []}))
        self.assertEqual(IntentReminderDaemon._read_request_traces(), {})

    @patch("ccls_daemon_intent_classifier.subprocess.run",
           side_effect=OSError("bus 不可用"))
    def test_bus_failure_returns_empty(self, mock_run):
        """bus 故障 → {}，不拋錯、不誤報。"""
        self.assertEqual(IntentReminderDaemon._read_request_traces(), {})

    @patch("ccls_daemon_intent_classifier.subprocess.run")
    def test_bad_timestamp_skipped(self, mock_run):
        """timestamp 非數值 → 跳過該條，不讓髒數據炸整批。"""
        import json
        mock_run.return_value = MagicMock(
            spec=CompletedProcess, returncode=0,
            stdout=json.dumps({"facts": [
                {"title": "[user_intent_reminder] 已注入意图提醒 → qa",
                 "timestamp": "not-a-number"}]}))
        self.assertEqual(IntentReminderDaemon._read_request_traces(), {})


class TestSingleBusReadPerTick(unittest.TestCase):
    """每輪只讀一次 bus（回歸：曾每 session 各讀一次 → N+1 子進程）。

    缺陷實證（2026-10-16 Codex 審查 P2）：實測單次 read 79ms，10 會話 × 每 10s
    = 每分鐘 ~4.7s CPU，且每次構造 Blackboard 觸發全表 FTS 回填。
    """

    @patch("ccls_daemon_intent_classifier.subprocess.run")
    @patch("ccls_daemon_intent_classifier._inject_text")
    @patch("ccls_daemon_intent_classifier._tmux_sessions")
    def test_bus_read_once_for_many_sessions(self, mock_tmux, mock_inject, mock_run):
        import json
        mock_tmux.return_value = ["ccs-a", "ccs-b", "ccs-c", "ccs-d"]
        mock_run.return_value = MagicMock(
            spec=CompletedProcess, returncode=0, stdout=json.dumps({"facts": []}))
        d = IntentReminderDaemon(interval=0.01)
        d._tick()
        self.assertEqual(mock_run.call_count, 1,
                         f"4 個會話應只讀 1 次 bus，實際 {mock_run.call_count} 次")


class TestReminderQuality(unittest.TestCase):
    """bus 痕跡入 monitor_audit，不污染 code_fix。"""

    @patch("ccls_daemon_intent_classifier.subprocess.run")
    def test_bus_trace_writes_monitor_audit_not_code_fix(self, mock_run):
        mock_run.return_value = MagicMock(spec=CompletedProcess, returncode=0)
        d = IntentReminderDaemon(interval=0.01)
        d._bus_trace("ccs-maintainer")
        self.assertTrue(mock_run.called, "bus trace 應調用 bus_client")
        args = mock_run.call_args[0][0]
        self.assertIn("monitor_audit", args, "痕跡應寫入 monitor_audit")
        self.assertNotIn("code_fix", args, "不得污染 code_fix")


class TestInjectTextAsSingleMessage(unittest.TestCase):
    """多行提醒必須作為**一條**消息注入，不得逐行提交。

    回歸：曾用 `for line: send-keys -l line; send-keys Enter`，每行獨立成
    消息（實測 coordinator 收到 22 字符的裸首行），角色為每行各起一個回合應答。
    """

    @patch("ccls_daemon_intent_classifier._tmux_send")
    def test_multiline_text_injected_once(self, mock_send):
        from ccls_daemon_intent_classifier import _inject_text
        _inject_text("ccs-probe", "L1\nL2\nL3")
        self.assertEqual(mock_send.call_count, 1,
                         "多行文本應一次投遞，不應逐行發送")

    @patch("ccls_daemon_intent_classifier._tmux_send")
    def test_text_passed_through_unaltered(self, mock_send):
        from ccls_daemon_intent_classifier import _inject_text
        text = "L1\nL2\nL3"
        _inject_text("ccs-probe", text)
        self.assertEqual(mock_send.call_args[0][1], text,
                         "文本應原樣傳遞，不得按行拆分或重組")

    @patch("ccls_daemon_intent_classifier._tmux_send")
    def test_push_reminder_sends_whole_body(self, mock_send):
        """_push_reminder 主體走單次注入。"""
        d = IntentReminderDaemon(interval=0.01)
        d._push_reminder("ccs-probe", round_no=1)
        self.assertEqual(mock_send.call_count, 1, "整條提醒應一次投遞")
        body = mock_send.call_args[0][1]
        self.assertIn("重复自检", body)
        self.assertGreater(len(body.split("\n")), 1,
                           "提醒本身應是多行，用於驗證整條投遞")

    def test_tmux_send_not_degraded_stub(self):
        """_tmux_send 必須來自 tmux_ops，不得是降級樁（否則注入靜默失敗）。"""
        import ccls_daemon_intent_classifier as m
        self.assertEqual(getattr(m._tmux_send, "__module__", ""), "tmux_ops",
                         "tmux_ops 加載失敗，注入降級為拋異常樁")


if __name__ == "__main__":
    unittest.main()
