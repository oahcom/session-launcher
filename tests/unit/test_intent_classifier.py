"""test_intent_classifier ─ ccls_daemon_intent_classifier 單元測試。

驗證：(1) 意圖分類三類判定；(2) 請求驅動觸發——僅在角色存在未對齊用戶請求
時重複提醒，無請求零注入；(3) 單次請求預算上限與新請求重置。
"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch, MagicMock

import ccls_daemon_intent_classifier as _mod
from ccls_daemon_intent_classifier import classify_intent, Intent, IntentReminderDaemon


class _HermeticStateFile(unittest.TestCase):
    """把 STATE_FILE 重定向到临时目录，避免测试写生产预算文件。

    缺陷實證（2026-10-17 三方審查 P1）：`_tick` 推送成功後 `_save_states` 直接寫
    `~/.hermes/run/intent_reminder_state.json`。未打樁的用例跑一次就用測試態
    覆蓋 daemon 的真實預算（實測殘留 last_remind_ts: 300，只可能來自測試 mock 的
    monotonic 序列）—— daemon 被 systemd 重拉後讀到它，即復現「冷卻恆真→靜默停發」。
    同類缺陷本項目修過（test_core 寫生產 workflows.db），此處不得重蹈。
    """
    _state_patch = None
    _state_dir = None

    def setUp(self):
        super().setUp()
        self._state_dir = tempfile.TemporaryDirectory()
        self._state_patch = patch.object(
            _mod, "STATE_FILE", Path(self._state_dir.name) / "intent_reminder_state.json")
        self._state_patch.start()

    def tearDown(self):
        self._state_patch.stop()
        self._state_dir.cleanup()
        super().tearDown()


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


class TestRequestDrivenTrigger(_HermeticStateFile):
    """驗證觸發源為「該角色存在未對齊用戶請求」，而非 pane 無新產出。

    缺陷實證（2026-10-16）：原判據 `text == st.last_snippet` 對 infinite 常駐
    角色等價於「角色空閒」，而其空閒是設計正常態 → 注入提醒 → 角色回「待機」→
    pane 變化 → 預算重置 → 再注入，自激循環（pm 250 輪 / coordinator 193 輪 /
    engineer 175 輪）。改用請求驅動後，無請求即零注入。
    """

    def setUp(self):
        super().setUp()  # 激活 _HermeticStateFile 的状态隔离
        self.daemon = IntentReminderDaemon(interval=0.01)
        self.daemon._states.clear()

    def tearDown(self):
        self.daemon._stop.set()
        super().tearDown()  # 停止 STATE_FILE patch + 清理临时目录

    def _run(self, ticks, injects, traces):
        self.daemon._read_request_traces = staticmethod(lambda: traces)
        with patch("ccls_daemon_intent_classifier._tmux_sessions",
                   return_value=["ccs-engineer"]), \
             patch("ccls_daemon_intent_classifier._inject_text",
                   side_effect=lambda s, t: (injects.append(t), True)[1]), \
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
        self._run(50, injects, {"engineer": (stale, None)})
        self.assertEqual(injects, [], "過期請求不應再提醒")

    def test_open_request_triggers_reminders_capped(self):
        """存在未對齊請求 → 重複提醒，且不超過單次請求預算。"""
        injects = []
        self._run(50, injects, {"engineer": (time.time(), None)})
        self.assertEqual(len(injects), IntentReminderDaemon._MAX_REMINDERS,
                         f"應提醒 {IntentReminderDaemon._MAX_REMINDERS} 次，"
                         f"實際 {len(injects)}")

    def test_cap_constant_matches_class_constant(self):
        """_MAX_REMINDERS_CAP（模块级，供 _load_states clamp）必须与
        IntentReminderDaemon._MAX_REMINDERS 同值。

        两者分居类内外，改一处忘另一处会让 clamp 边界与运行时预算脱钩：
        cap 偏小 → 重载后预算被提前掐断；偏大 → 脏条目能领到超出契约的额度。
        """
        self.assertEqual(
            _mod._MAX_REMINDERS_CAP, IntentReminderDaemon._MAX_REMINDERS,
            "模組級 cap 與類內 _MAX_REMINDERS 已漂移")

    def test_corrupt_entry_fails_closed_not_open(self):
        """單條髒數據 → 按「已耗盡」處理，不得退回滿額預算。

        缺陷實證（2026-10-17 三方審查 P1）：原實現遇解析失敗直接 continue，
        該會話在內存中不存在 → 下輪 setdefault 新建 → remind_count=0 →
        重新領滿 3 次提醒，與「落盤防重複提醒」的初衷正好相反（fail-open）。
        """
        with tempfile.TemporaryDirectory() as d:
            with patch.object(_mod, "STATE_FILE", Path(d) / "state.json"):
                _mod.STATE_FILE.write_text(json.dumps({
                    "ccs-qa": {"request_ts": 1.0, "remind_count": "not-a-number"},
                }), encoding="utf-8")
                states = _mod._load_states()
        self.assertIn("ccs-qa", states, "臟條目不得被直接丟棄（否則退回滿額預算）")
        self.assertEqual(states["ccs-qa"].remind_count, _mod._MAX_REMINDERS_CAP,
                         "臟條目應按已耗盡處理（fail-closed）")

    def test_out_of_range_count_is_clamped(self):
        """remind_count 越界（負數/超大）→ clamp 到 [0, cap]。"""
        with tempfile.TemporaryDirectory() as d:
            with patch.object(_mod, "STATE_FILE", Path(d) / "state.json"):
                _mod.STATE_FILE.write_text(json.dumps({
                    "ccs-a": {"request_ts": 1.0, "remind_count": -5},
                    "ccs-b": {"request_ts": 1.0, "remind_count": 9999},
                }), encoding="utf-8")
                states = _mod._load_states()
        self.assertEqual(states["ccs-a"].remind_count, 0, "負數應 clamp 到 0")
        self.assertEqual(states["ccs-b"].remind_count, _mod._MAX_REMINDERS_CAP,
                         "超大值應 clamp 到 cap")


    def test_busy_role_defers_reminder_without_spending_budget(self):
        """角色生成中 → 不注入、不計預算（下輪重試）。

        缺陷實證（2026-10-17）：Claude Code 生成期間收到的 send-keys 進入
        queued messages（pane 底部 "Press up to edit queued messages"），角色
        只在當前回合結束後才讀。實測 qa 忙碌 12 分鐘期間 3 條提醒全卡隊列，
        而預算已耗盡轉靜默 —— 提醒全部在角色忙時發出、全部在角色閒時失效，
        與「提醒它自檢」的時機需求恰好相反。修復後忙時延後且不扣預算。
        """
        injects = []
        with patch("ccls_daemon_intent_classifier._role_is_busy", return_value=True):
            self._run(50, injects, {"engineer": (time.time(), None)})
        self.assertEqual(injects, [], "角色生成中不應注入（會卡進 queued messages）")
        st = self.daemon._states["ccs-engineer"]
        self.assertEqual(st.remind_count, 0,
                         "忙時延後不得消耗預算，否則角色閒下來時已無預算可提醒")

    def test_idle_role_still_gets_reminders(self):
        """反向場景：角色空閒時照常提醒（修復不得把提醒一併廢掉）。"""
        injects = []
        with patch("ccls_daemon_intent_classifier._role_is_busy", return_value=False):
            self._run(50, injects, {"engineer": (time.time(), None)})
        self.assertEqual(len(injects), IntentReminderDaemon._MAX_REMINDERS,
                         "空閒角色應照常收到提醒")

    def test_reminder_uses_user_intent_wording(self):
        """前提已由 bus 痕跡確證 → 用用戶要求的原始措辭，不再用「停滯自檢」框架。"""
        injects = []
        self._run(50, injects, {"engineer": (time.time(), None)})
        self.assertTrue(injects, "應有提醒")
        body = injects[0]
        self.assertIn("你收到的是任务", body)
        from user_intent_reminder import _CORE_QUESTIONS
        self.assertIn(_CORE_QUESTIONS[0], body)
        self.assertIn(_CORE_QUESTIONS[1], body)

    def test_question_request_uses_question_wording(self):
        """疑問請求的重複提醒必須用疑問措辭，不得降級成任務措辭。"""
        injects = []
        self._run(50, injects, {"engineer": (time.time(), Intent.QUESTION)})
        self.assertTrue(injects, "應有提醒")
        self.assertIn("你收到的是疑问", injects[0],
                      "疑問請求不得使用任務措辭")

    def test_missing_intent_degrades_to_task_wording(self):
        """痕跡無 intent 字段（舊痕跡/字段變動）→ 降級 TASK，即修復前行為。"""
        injects = []
        self._run(50, injects, {"engineer": (time.time(), None)})
        self.assertTrue(injects, "應有提醒")
        self.assertIn("你收到的是任务", injects[0])

    def test_new_request_resets_budget(self):
        """新一輪用戶請求到達 → 預算重置，可再次提醒。"""
        injects = []
        base = time.time()
        self._run(50, injects, {"engineer": (base, None)})
        self.assertEqual(len(injects), IntentReminderDaemon._MAX_REMINDERS)
        self._run(50, injects, {"engineer": (base + 60, None)})  # 新請求
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
                         {"engineer": (111.0, None), "qa": (222.0, None)})

    @patch("ccls_daemon_intent_classifier.subprocess.run")
    def test_keeps_latest_per_role(self, mock_run):
        """同角色多條痕跡 → 取最新（舊請求不應覆蓋新請求）。"""
        import json
        mock_run.return_value = MagicMock(
            spec=CompletedProcess, returncode=0,
            stdout=json.dumps(self._facts([("qa", 222.0), ("qa", 999.0)])))
        self.assertEqual(IntentReminderDaemon._read_request_traces(), {"qa": (999.0, None)})

    @patch("ccls_daemon_intent_classifier.subprocess.run")
    def test_parses_intent_from_evidence(self, mock_run):
        """evidence intent= 字段 → Intent（疑问/任务/其他），供重复提醒选用措辞。

        缺陷實證：重複提醒曾硬编码 Intent.TASK，疑問被按任務措辭提醒
        （「先確保嚴格按用戶指令交付」對一個純提問是錯的前提）。
        """
        import json
        mock_run.return_value = MagicMock(
            spec=CompletedProcess, returncode=0,
            stdout=json.dumps({"facts": [
                {"title": "[user_intent_reminder] 已注入意图提醒 → qa",
                 "timestamp": 111.0,
                 "evidence": "source=user role=qa intent=question"},
                {"title": "[user_intent_reminder] 已注入意图提醒 → pm",
                 "timestamp": 222.0,
                 "evidence": "source=user role=pm intent=task"}]}
            ))
        self.assertEqual(
            IntentReminderDaemon._read_request_traces(),
            {"qa": (111.0, Intent.QUESTION), "pm": (222.0, Intent.TASK)})

    @patch("ccls_daemon_intent_classifier.subprocess.run")
    def test_unknown_intent_value_degrades_to_none(self, mock_run):
        """intent 取值非法（數據漂移/字段改義）→ None，由調用方降級 TASK。

        不得讓髒值炸整輪解析（會使所有角色靜默零提醒）。
        """
        import json
        mock_run.return_value = MagicMock(
            spec=CompletedProcess, returncode=0,
            stdout=json.dumps({"facts": [
                {"title": "[user_intent_reminder] 已注入意图提醒 → qa",
                 "timestamp": 111.0, "evidence": "source=user role=qa intent=wut"}]}
            ))
        self.assertEqual(IntentReminderDaemon._read_request_traces(),
                         {"qa": (111.0, None)})

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


class TestDeliveryFailureHandling(_HermeticStateFile):
    """投遞失敗必須被識別，不得計為成功。

    缺陷實證（2026-10-16 獨立審查 D-2 P1）：`_tmux_send` 重試耗盡時
    **return False 而非拋異常**（tmux_ops.py:220/270，docstring 明寫「調用方
    必須檢查返回值」）。舊寫法丟棄返回值 → 投遞全失敗也計成功 → 預算空燒、
    bus 留下「已提醒」假痕跡、daemon 側無失敗信號。
    """

    def setUp(self):
        super().setUp()  # 激活 _HermeticStateFile 的状态隔离
        self.daemon = IntentReminderDaemon(interval=0.01)
        self.daemon._states.clear()
        self._fixed_ts = time.time()
        self.daemon._read_request_traces = staticmethod(
            lambda: {"engineer": (self._fixed_ts, None)})
        # 本組用例只考「投遞失敗處理」，忙檢測是另一維（見
        # TestRequestDrivenTrigger 的 busy 用例）。不打樁會讀到真 tmux 狀態：
        # engineer 真忙 → _push_reminder 提前 return → 投遞根本未嘗試 →
        # mock_inject.called 恆 False，測試空轉而非測到目標行為。
        self._busy_patch = patch(
            "ccls_daemon_intent_classifier._role_is_busy", return_value=False)
        self._busy_patch.start()

    def tearDown(self):
        self._busy_patch.stop()
        self.daemon._stop.set()
        super().tearDown()  # 停止 STATE_FILE patch + 清理临时目录

    @patch("ccls_daemon_intent_classifier._tmux_sessions",
           return_value=["ccs-engineer"])
    def test_delivery_failure_not_counted_as_success(self, mock_tmux):
        """投遞非明確成功（False/None/0/""）→ 不得累加預算、不得寫 bus 假痕跡。

        判據用 `is not True`：_tmux_send 歷史上有過「超限後靜默返回 None」的版本，
        只判 `is False` 會在那種實現下重新靜默失效。
        """
        for bad in (False, None, 0, ""):
            with self.subTest(returned=bad):
                self.daemon._states.clear()
                with patch("ccls_daemon_intent_classifier._inject_text",
                           return_value=bad) as mock_inject, \
                     patch("ccls_daemon_intent_classifier.subprocess.run") as mock_run, \
                     patch("ccls_daemon_intent_classifier.time.monotonic",
                           return_value=self.daemon._REMIND_COOLDOWN + 1):
                    pushed = self.daemon._tick()
                self.assertTrue(mock_inject.called, "應已嘗試投遞（否則用例空轉）")
                self.assertEqual(pushed, 0, f"投遞返回 {bad!r} 不得計入 pushed")
                self.assertEqual(
                    self.daemon._states["ccs-engineer"].remind_count, 0,
                    f"投遞返回 {bad!r} 不得消耗預算（否則永久靜默）")
                mock_run.assert_not_called()  # 不得寫 monitor_audit 假痕跡

    @patch("ccls_daemon_intent_classifier._tmux_sessions",
           return_value=["ccs-engineer"])
    @patch("ccls_daemon_intent_classifier._inject_text", return_value=True)
    def test_delivery_success_counted(self, mock_inject, mock_tmux):
        """投遞成功 → 正常累加預算。"""
        with patch("ccls_daemon_intent_classifier.subprocess.run"), \
             patch("ccls_daemon_intent_classifier.time.monotonic",
                   return_value=self.daemon._REMIND_COOLDOWN + 1):
            pushed = self.daemon._tick()
        self.assertEqual(pushed, 1)
        self.assertEqual(self.daemon._states["ccs-engineer"].remind_count, 1)


class TestTmuxFlakeResilience(_HermeticStateFile):
    """tmux 枚舉單輪為空 = 抖動，不得清空狀態（否則預算被重置）。

    缺陷實證（2026-10-16 獨立審查 D-4 P2）：`_tmux_sessions` 超時/抖動時返回 []，
    舊寫法該輪刪除全部 _states → 下一輪 setdefault 令 remind_count 歸零 →
    同一用戶請求再拿滿額預算（實測單次抖動使注入翻倍 3→6）。
    對齊架構紅線「3 次連續失敗才標 down」：單次失敗不算。
    """

    def test_empty_session_list_does_not_reset_budget(self):
        FIXED_TS = time.time()  # 固定值：time.time() 每次都变会被判成"新请求"
        d = IntentReminderDaemon(interval=0.01)
        d._states.clear()
        d._read_request_traces = staticmethod(lambda: {"engineer": (FIXED_TS, None)})
        injects = []
        with patch("ccls_daemon_intent_classifier._tmux_sessions",
                   return_value=["ccs-engineer"]), \
             patch("ccls_daemon_intent_classifier._inject_text",
                   side_effect=lambda s, t: (injects.append(t), True)[1]), \
             patch("ccls_daemon_intent_classifier.subprocess.run"), \
             patch("ccls_daemon_intent_classifier.time.monotonic",
                   side_effect=range(0, 10 ** 7, 100)):
            for _ in range(50):
                d._tick()  # 耗盡預算
            self.assertEqual(len(injects), IntentReminderDaemon._MAX_REMINDERS)
            # 單輪枚舉失敗（抖動）
            with patch("ccls_daemon_intent_classifier._tmux_sessions",
                       return_value=[]):
                d._tick()
            for _ in range(50):
                d._tick()  # 恢復
        self.assertEqual(len(injects), IntentReminderDaemon._MAX_REMINDERS,
                         f"單輪抖動不得重置預算，實際注入 {len(injects)} 次")


class TestReminderQuality(_HermeticStateFile):
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


class TestEmptyPollReclaimsEventually(_HermeticStateFile):
    """連續空枚舉超過閾值 → 認定會話真退出，照常回收狀態。

    回歸（2026-10-16 二輪審查 P1-3）：上一輪的抖動守衛 `if not live: return`
    對抖動是對的，但把「全部 ccs-* 真的都退出了」也一起吞了 → _states 永久泄漏，
    且空枚舉不打日誌，監控側無法區分「抖動」與「daemon 卡死」。
    修法對齊架構紅線「3 次連續失敗才標 down」：連續空才回收，且每輪留日誌。
    """

    def setUp(self):
        super().setUp()  # 激活 _HermeticStateFile 的状态隔离
        self.daemon = IntentReminderDaemon(interval=0.01)

    def tearDown(self):
        self.daemon._stop.set()
        super().tearDown()  # 停止 STATE_FILE patch + 清理临时目录

    def test_single_empty_poll_keeps_state(self):
        """單輪空（抖動）→ 狀態保留，預算不被重置。"""
        d = self.daemon
        d._states.clear()
        import ccls_daemon_intent_classifier as m
        d._states["ccs-engineer"] = m._SessionState(remind_count=2)
        with patch("ccls_daemon_intent_classifier._tmux_sessions", return_value=[]):
            d._tick()
        self.assertIn("ccs-engineer", d._states, "單輪抖動不得回收狀態")
        self.assertEqual(d._states["ccs-engineer"].remind_count, 2,
                         "單輪抖動不得重置預算")

    def test_sustained_empty_polls_reclaim_state(self):
        """連續空枚舉達閾值 → 狀態被回收（會話真的都沒了）。"""
        d = self.daemon
        d._states.clear()
        import ccls_daemon_intent_classifier as m
        d._states["ccs-engineer"] = m._SessionState()
        with patch("ccls_daemon_intent_classifier._tmux_sessions", return_value=[]), \
             patch("sys.stderr"):
            for _ in range(d._EMPTY_POLL_THRESHOLD):
                d._tick()
        self.assertNotIn("ccs-engineer", d._states,
                         f"連續 {d._EMPTY_POLL_THRESHOLD} 輪空應回收狀態（否則永久泄漏）")

    def test_recovery_resets_empty_counter(self):
        """抖動恢復後計數歸零——下次單輪空不應被當作連續。"""
        d = self.daemon
        d._states.clear()
        import ccls_daemon_intent_classifier as m
        d._states["ccs-engineer"] = m._SessionState()
        with patch("sys.stderr"):
            with patch("ccls_daemon_intent_classifier._tmux_sessions", return_value=[]):
                d._tick()
                d._tick()  # 連續 2 輪（未達閾值 3）
            with patch("ccls_daemon_intent_classifier._tmux_sessions",
                       return_value=["ccs-engineer"]):
                d._tick()  # 恢復
            with patch("ccls_daemon_intent_classifier._tmux_sessions", return_value=[]):
                d._tick()  # 再空一輪
        self.assertIn("ccs-engineer", d._states,
                      "恢復後計數應歸零，單輪空不回收")


class TestStatePersistence(_HermeticStateFile):
    """預算落盤的跨重啟語義與髒數據韌性。

    缺陷實證（2026-10-17 三方審查）：
    (1) last_remind_ts 是 time.monotonic()（自開機計時）卻落盤 → 重啟後 monotonic
        歸零，舊值（上次開機時長）使冷卻判據 `now - last < 30` 恆真 → 該 session
        提醒靜默停發數天，且新請求只重置預算不重置冷卻，救不回。
    (2) 單條髒數據（remind_count=null/dict）拋 TypeError 被外層 except 吞掉 →
        整表預算清空 → 所有角色重新獲得滿額預算（本文件反覆防的"預算空燒"）。
    """

    def test_monotonic_cooldown_not_restored_across_restart(self):
        """重啟後不得繼承 monotonic 冷卻值（否則冷卻恆真→靜默停發）。"""
        _mod.STATE_FILE.write_text(json.dumps({
            "ccs-qa": {"request_ts": 123.0, "last_remind_ts": 999999.0,
                       "remind_count": 2}}), encoding="utf-8")
        st = _mod._load_states()["ccs-qa"]
        self.assertEqual(st.last_remind_ts, 0.0,
                         "monotonic 冷卻值不可跨重啟繼承（會導致永久靜默）")
        self.assertEqual(st.remind_count, 2, "已用預算應保留（防重啟刷預算）")
        self.assertEqual(st.request_ts, 123.0)

    def test_save_omits_monotonic_cooldown(self):
        """落盤內容不含 last_remind_ts（寫了也無意義且有害）。"""
        _mod._save_states({"ccs-qa": _mod._SessionState(
            request_ts=1.0, last_remind_ts=888.0, remind_count=1)})
        raw = json.loads(_mod.STATE_FILE.read_text(encoding="utf-8"))
        self.assertNotIn("last_remind_ts", raw["ccs-qa"],
                         "monotonic 值不得落盤")

    def test_one_dirty_entry_does_not_clear_whole_table(self):
        """單條髒數據不得清空整表預算，且不得退回滿額預算。

        原斷言為 assertNotIn("bad", st)（直接丟棄臟條目）。2026-10-17 三方審查
        指出這條路徑是 fail-open 的：條目被丟 → 內存中不存在該會話 → 下輪
        setdefault 新建 _SessionState → remind_count=0 → 重新領滿 3 次提醒，
        與落盤防重複提醒的初衷正好相反。改為按 cap 保守處理（fail-closed）：
        最壞少提醒一次，不會多提醒。"good" 不因鄰條臟數據而丟失的原意不變。
        """
        _mod.STATE_FILE.write_text(json.dumps({
            "good": {"request_ts": 1.0, "remind_count": 1},
            "bad": {"request_ts": None, "remind_count": None},
        }), encoding="utf-8")
        st = _mod._load_states()
        self.assertIn("good", st, "合法條目不得因鄰條臟數據而丟失")
        self.assertEqual(st["good"].remind_count, 1, "合法條目的預算應原樣保留")
        self.assertEqual(st["bad"].remind_count, _mod._MAX_REMINDERS_CAP,
                         "臟條目應按已耗盡處理（fail-closed），而非丟棄後重獲滿額")
