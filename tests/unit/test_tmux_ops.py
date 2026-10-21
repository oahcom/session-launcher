"""test_tmux_ops.py — tmux_ops 核心函数测试覆盖。"""
import sys

# conftest.py 已设置 sys.path
from tmux_ops import make_tmux_name, parse_tmux_name, _role_is_busy

# ── parse_tmux_name 正反向一致性 ──

class TestParseTmuxName:
    """parse_tmux_name(make_tmux_name(role, id)) == (role, id) round-trip."""

    def test_round_trip_id0(self):
        assert parse_tmux_name(make_tmux_name("pm", 0)) == ("pm", 0)

    def test_round_trip_id1(self):
        assert parse_tmux_name(make_tmux_name("engineer", 3)) == ("engineer", 3)

    def test_round_trip_id16(self):
        assert parse_tmux_name(make_tmux_name("writer", 16)) == ("writer", 16)

    def test_round_trip_hyphen_role(self):
        assert parse_tmux_name(make_tmux_name("code-reviewer", 2)) == ("code-reviewer", 2)

    def test_round_trip_underscore_role(self):
        assert parse_tmux_name(make_tmux_name("qa_engineer", 5)) == ("qa_engineer", 5)

    def test_round_trip_cdx_prefix(self):
        """cdx- prefix is also valid (codex sessions)."""
        name = "cdx-coderunner-7"
        assert parse_tmux_name(name) == ("coderunner", 7)

    def test_non_ccs_prefix_returns_empty(self):
        assert parse_tmux_name("other-something") == ("", 0)

    def test_ccs_plain_no_id(self):
        assert parse_tmux_name("ccs-pm") == ("pm", 0)

    def test_ccs_with_ambiguous_digits(self):
        """ccs-role-12abc is treated as role='role-12abc', id=0 (digits must be at end)."""
        assert parse_tmux_name("ccs-role-12abc") == ("role-12abc", 0)

    def test_ccs_digits_only(self):
        """ccs-42 parses as role='42', id=0 (no dash before digits)."""
        assert parse_tmux_name("ccs-42") == ("42", 0)

# ── make_tmux_name 特殊字符 ──

class TestMakeTmuxName:
    def test_default_prefix(self):
        assert make_tmux_name("pm") == "ccs-pm"

    def test_instance_id_prefix(self):
        assert make_tmux_name("pm", 3) == "ccs-pm-3"

    def test_instance_id_zero_no_suffix(self):
        """id=0 应该无后缀（向后兼容）。"""
        assert make_tmux_name("engineer", 0) == "ccs-engineer"

    def test_empty_role(self):
        """空 role 仍然生成合法 tmux 名。"""
        assert make_tmux_name("") == "ccs-"
        assert make_tmux_name("", 2) == "ccs--2"

    def test_role_with_hyphens(self):
        assert make_tmux_name("code-reviewer", 1) == "ccs-code-reviewer-1"

    def test_role_with_underscores(self):
        assert make_tmux_name("qa_engineer", 5) == "ccs-qa_engineer-5"

    def test_role_with_digits(self):
        assert make_tmux_name("role1", 2) == "ccs-role1-2"

    def test_instance_999(self):
        assert make_tmux_name("dev", 999) == "ccs-dev-999"

# ── _tmux_send 不得中断生成 ──

_IDLE_PANE = "history\n" + "─"*40 + "\n❯ \n" + "─"*40 + "\n  ⏵⏵ bypass permissions on\n"


class TestTmuxSendDoesNotInterrupt:
    """投递消息不得中断角色正在跑的推理。

    C-c 在 Claude CLI 生成中等价于「中断当前推理」：实测 lr 6 分钟内被打断 7 次，
    transcript 留下 [Request interrupted by user]，正在执行的 Bash 调用被拒。
    清输入行的正确按键是 C-u（kill-line），它不触碰生成状态。
    """

    def test_never_sends_ctrl_c(self):
        import tmux_ops
        from unittest.mock import patch
        with patch("tmux_ops.subprocess.run") as m, \
             patch("tmux_ops._tmux_output", return_value=_IDLE_PANE):
            m.return_value.returncode = 0
            tmux_ops._tmux_send("ccs-probe", "hello")
        sent = [c.args[0] for c in m.call_args_list]
        assert not any("C-c" in argv for argv in sent), \
            f"投递路径出现 C-c，会中断角色推理: {sent}"

    def test_clears_input_line_with_ctrl_u(self):
        import tmux_ops
        from unittest.mock import patch
        with patch("tmux_ops.subprocess.run") as m, \
             patch("tmux_ops._tmux_output", return_value=_IDLE_PANE):
            m.return_value.returncode = 0
            tmux_ops._tmux_send("ccs-probe", "hello")
        sent = [c.args[0] for c in m.call_args_list]
        assert any("C-u" in argv for argv in sent), \
            f"未清输入行，上一条未提交的残留会粘在新消息前: {sent}"

    def test_message_sent_literal(self):
        """-l 必须存在，否则 payload 内的按键名会被 tmux 解释执行。"""
        import tmux_ops
        from unittest.mock import patch
        with patch("tmux_ops.subprocess.run") as m, \
             patch("tmux_ops._tmux_output", return_value=_IDLE_PANE):
            m.return_value.returncode = 0
            tmux_ops._tmux_send("ccs-probe", "q/goal C-c")
        literal = [c.args[0] for c in m.call_args_list if "-l" in c.args[0]]
        assert literal, "缺少 send-keys -l，payload 中的按键名会被解释"
        assert any("q/goal C-c" in argv for argv in literal), \
            "payload 未按字面量发送"


# ── 忙检测 ──

class TestRoleIsBusy:
    """_role_is_busy 只在生成中返回 True。"""

    def test_returns_true_when_status_bar_shows_interrupt_hint(self):
        import tmux_ops
        from unittest.mock import patch
        pane = ("  ⏵⏵ bypass permissions on (shift+tab to cycle)"
                " · esc to interrupt · ← 11 agents")
        with patch("tmux_ops._tmux_output", return_value=pane):
            assert tmux_ops._role_is_busy("ccs-probe") is True

    def test_idle_pane_with_leftover_spinner_is_not_busy(self):
        """空闲 pane 会残留已完成的 spinner 行，不能据此判忙。

        实测 lr 空闲时 pane 仍留 "✻ Sautéed for 3m 2s"，状态栏已无
        "esc to interrupt"。若把 spinner 文案当忙标志，空闲角色会被误判为忙，
        投递路径永久跳过清行。
        """
        import tmux_ops
        from unittest.mock import patch
        pane = ("✻ Sautéed for 3m 2s\n"
                "────────────\n❯ \n────────────\n"
                "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← 11 agents")
        with patch("tmux_ops._tmux_output", return_value=pane):
            assert tmux_ops._role_is_busy("ccs-probe") is False

    def test_retrying_attempt_counts_as_busy(self):
        """429 重试期状态栏只有 "Retrying attempt"、没有 esc 提示，但一样吞按键。"""
        import tmux_ops
        from unittest.mock import patch
        pane = ("  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← 11 agents\n"
                "  Retrying attempt 3/5 in 8s…")
        with patch("tmux_ops._tmux_output", return_value=pane):
            assert tmux_ops._role_is_busy("ccs-probe") is True

    def test_missing_session_is_not_busy(self):
        """采集失败不得抛异常（_tmux_output 返回错误串）。"""
        import tmux_ops
        from unittest.mock import patch
        with patch("tmux_ops._tmux_output", return_value="[错误] timeout"):
            assert tmux_ops._role_is_busy("ccs-probe") is False


# ── 启动前的内存闸门 ──

class TestMemoryShortage:
    """主判据是 pipeline cgroup 的 memory.current/high，宿主机 swap/avail 只作兜底。

    两条现场互斥、都得钉住：
    2026-10-16 swap_free=0 但 avail 6.1GB 也起不来（撞 cgroup high → 不可中断睡眠）；
    2026-10-21 swap_free=0/avail=2291MB 却正常起来（cgroup 有余量），当时旧判据
    挡住了 archivist、角色停摆 10 分钟。
    """

    def _meminfo(self, avail_mb, swap_total_mb, swap_free_mb):
        return (f"MemAvailable: {avail_mb*1024} kB\n"
                f"SwapTotal: {swap_total_mb*1024} kB\n"
                f"SwapFree: {swap_free_mb*1024} kB\n")

    def _check(self, avail_mb, swap_total_mb, swap_free_mb, cgroup=None):
        """cgroup=(current_bytes, high_bytes)；None = cgroup 文件不可见。"""
        import tmux_ops
        from unittest.mock import patch
        _mem = self._meminfo(avail_mb, swap_total_mb, swap_free_mb)

        def fake_read(self_path):
            name = str(self_path)
            if name.endswith("memory.current") or name.endswith("memory.high"):
                if cgroup is None:
                    raise OSError("cgroup absent")
                return str(cgroup[0] if name.endswith("memory.current") else cgroup[1])
            return _mem

        with patch("pathlib.Path.read_text", new=fake_read):
            return tmux_ops._memory_shortage("probe")

    def test_full_swap_with_ram_free_is_allowed(self):
        assert self._check(avail_mb=2291, swap_total_mb=8191, swap_free_mb=0) is None

    def test_full_swap_and_low_ram_is_blocked(self):
        err = self._check(avail_mb=300, swap_total_mb=8191, swap_free_mb=0)
        assert err and "swap" in err

    def test_low_ram_alone_is_blocked(self):
        assert self._check(avail_mb=300, swap_total_mb=0, swap_free_mb=0) is not None

    def test_healthy_is_allowed(self):
        assert self._check(avail_mb=4000, swap_total_mb=8191, swap_free_mb=4000,
                           cgroup=(5000 * 2**20, 6144 * 2**20)) is None

    def test_swap_boundary_127_blocks_128_allows(self):
        """avail 取 1010MB：刚好在 _MEM_FREE_MIN_MB(1000) 之上、_SWAP_LOW_AVAIL_MB
        (1024) 之下，这样唯一能拦的就只有 swap 那一条分支。"""
        assert self._check(avail_mb=1010, swap_total_mb=8191, swap_free_mb=127,
                           cgroup=(5000 * 2**20, 6144 * 2**20)) is not None
        assert self._check(avail_mb=1010, swap_total_mb=8191, swap_free_mb=128,
                           cgroup=(5000 * 2**20, 6144 * 2**20)) is None

    def test_cgroup_high_blocks_with_plenty_of_host_ram(self):
        """2026-10-16 的形状：宿主机还富余，但 cgroup 撞 high → 必须拦。"""
        err = self._check(avail_mb=6969, swap_total_mb=8191, swap_free_mb=4169,
                          cgroup=(6144 * 2**20 * 96 // 100, 6144 * 2**20))
        assert err and "cgroup" in err

    def test_cgroup_below_threshold_allowed(self):
        assert self._check(avail_mb=6969, swap_total_mb=8191, swap_free_mb=4169,
                           cgroup=(5000 * 2**20, 6144 * 2**20)) is None

    def test_cgroup_unreadable_falls_back_to_host_checks(self):
        """cgroup 不可见时不抛异常，退回宿主机判据（本例宿主机宽松 → 放行）。"""
        assert self._check(avail_mb=6969, swap_total_mb=8191, swap_free_mb=4169) is None


# ── --restart-idle 重启判据 ──

class TestRestartIfStale:
    """只在「进程比 prompt 旧 + pane 空闲 + 确实是 claude」三条同时成立时才 kill。"""

    def test_stale_idle_claude_is_restarted(self, tmp_path):
        import tmux_ops
        from unittest.mock import patch
        md = tmp_path / "CLAUDE.md"
        md.write_text("x", encoding="utf-8")
        with patch("tmux_ops._is_alive", return_value=True), \
             patch("tmux_ops._role_is_busy", return_value=False), \
             patch("tmux_ops._tmux_output", return_value="❯ "), \
             patch("tmux_ops._find_claude_pid", return_value=4242), \
             patch("tmux_ops._proc_start_time", return_value=0.0), \
             patch("pathlib.Path.read_text", return_value="claude\n"), \
             patch("tmux_ops.os.kill") as kill:
            assert tmux_ops._restart_if_stale("ccs-probe", md) is True
        kill.assert_called_once_with(4242, 9)

    def test_busy_role_is_not_killed(self, tmp_path):
        import tmux_ops
        from unittest.mock import patch
        md = tmp_path / "CLAUDE.md"
        md.write_text("x", encoding="utf-8")
        with patch("tmux_ops._is_alive", return_value=True), \
             patch("tmux_ops._role_is_busy", return_value=True), \
             patch("tmux_ops.os.kill") as kill:
            assert tmux_ops._restart_if_stale("ccs-probe", md) is False
        kill.assert_not_called()

    def test_fresh_process_is_not_killed(self, tmp_path):
        import tmux_ops
        from unittest.mock import patch
        md = tmp_path / "CLAUDE.md"
        md.write_text("x", encoding="utf-8")
        with patch("tmux_ops._is_alive", return_value=True), \
             patch("tmux_ops._role_is_busy", return_value=False), \
             patch("tmux_ops._find_claude_pid", return_value=4242), \
             patch("tmux_ops._proc_start_time", return_value=10 ** 12), \
             patch("tmux_ops.os.kill") as kill:
            assert tmux_ops._restart_if_stale("ccs-probe", md) is False
        kill.assert_not_called()

    def test_capture_failure_is_not_killed(self, tmp_path):
        import tmux_ops
        from unittest.mock import patch
        md = tmp_path / "CLAUDE.md"
        md.write_text("x", encoding="utf-8")
        with patch("tmux_ops._is_alive", return_value=True), \
             patch("tmux_ops._role_is_busy", return_value=False), \
             patch("tmux_ops._tmux_output", return_value=""), \
             patch("tmux_ops.os.kill") as kill:
            assert tmux_ops._restart_if_stale("ccs-probe", md) is False
        kill.assert_not_called()

    def test_wrapper_pid_is_not_killed(self, tmp_path):
        """_find_claude_pid 兜底可能回落到 bash wrapper：杀它会连 tmux session 一起没。"""
        import tmux_ops
        from unittest.mock import patch
        md = tmp_path / "CLAUDE.md"
        md.write_text("x", encoding="utf-8")
        with patch("tmux_ops._is_alive", return_value=True), \
             patch("tmux_ops._role_is_busy", return_value=False), \
             patch("tmux_ops._find_claude_pid", return_value=4242), \
             patch("tmux_ops._proc_start_time", return_value=0.0), \
             patch("pathlib.Path.read_text", return_value="bash\n"), \
             patch("tmux_ops.os.kill") as kill:
            assert tmux_ops._restart_if_stale("ccs-probe", md) is False
        kill.assert_not_called()


# ── 忙时投递策略 ──

class TestTmuxSendBusyHandling:
    """忙时拒投（不碰 pane），由调用方留 bus 下轮重推。"""

    def test_busy_role_is_not_injected_at_all(self):
        """忙时不发 C-u、不发正文、不发 Enter——一次 tmux 按键都不该发生。

        旧策略「忙时跳过 C-u 但照发」基于「消息进队列等回合结束再读」的
        假设。实测 2026-10-20 closer 在 9m58s 推理中途收到注入后立即转去
        执行，原审计结论丢失。忙时必须完全不碰 pane。
        """
        import tmux_ops
        from unittest.mock import patch
        with patch("tmux_ops._role_is_busy", return_value=True), \
             patch("tmux_ops.subprocess.run") as m:
            m.return_value.returncode = 0
            assert tmux_ops._tmux_send("ccs-probe", "hello") == "busy"
        assert m.call_args_list == [], \
            f"忙时不得有任何 tmux 按键: {[c.args[0] for c in m.call_args_list]}"

    def test_idle_role_clears_input_line(self):
        import tmux_ops
        from unittest.mock import patch
        with patch("tmux_ops._role_is_busy", return_value=False), \
             patch("tmux_ops._tmux_output", return_value=_IDLE_PANE), \
             patch("tmux_ops.subprocess.run") as m:
            m.return_value.returncode = 0
            tmux_ops._tmux_send("ccs-probe", "hello")
        sent = [c.args[0] for c in m.call_args_list]
        assert any("C-u" in argv for argv in sent), \
            f"空闲时未清输入行，残留会粘在新消息前: {sent}"



if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))


# ── 投递后确认（滞留自愈） ──

class TestSubmittedProbe:
    """_submitted 只看输入框边界内的文本，已提交的消息不得误判为滞留。"""

    def _pane(self, composer_text):
        rule = "─" * 40
        return (f"❯ earlier message\n"
                f"  thought abt it\n{rule}\n❯ {composer_text}\n{rule}\n"
                f"  ⏵⏵ bypass permissions on\n")

    def test_stranded_text_is_detected(self):
        import tmux_ops
        from unittest.mock import patch
        with patch("tmux_ops._tmux_output", return_value=self._pane("hello world")):
            assert tmux_ops._submitted("ccs-probe", "hello world") is False

    def test_submitted_text_not_confused_with_history(self):
        """已提交的消息滚到横线之上，同样带 ❯ 前缀——不得据此判滞留。"""
        import tmux_ops
        from unittest.mock import patch
        with patch("tmux_ops._tmux_output", return_value=self._pane("")):
            assert tmux_ops._submitted("ccs-probe", "earlier message") is True

    def test_unreachable_pane_does_not_stall(self):
        import tmux_ops
        from unittest.mock import patch
        with patch("tmux_ops._tmux_output", return_value="[错误] timeout"):
            assert tmux_ops._submitted("ccs-probe", "hello") is True

    def test_multiline_probe_uses_tail_when_head_scrolled_out(self):
        """输入框只显示多行消息的尾部：首行滚出视口时仍须判为滞留。

        实测 ccs-archivist：启动消息首行不可见，仅探测首行 → 恒不命中 →
        滞留被当成已提交，消息在输入框躺 25 分钟。
        """
        import tmux_ops
        from unittest.mock import patch
        message = "## 归档管理员 — 已就绪\n驱动模式: ondemand\n3. 执行第一条 eval_criteria"
        pane = self._pane("  启动自检:\n  3. 执行第一条 eval_criteria")
        with patch("tmux_ops._tmux_output", return_value=pane):
            assert tmux_ops._submitted("ccs-probe", message) is False


class TestComposerBoundary:
    """_composer_text 只取横线之间的输入框区域。"""

    def test_takes_text_between_last_two_rules(self):
        import tmux_ops
        rule = "─" * 40
        pane = f"history line\n{rule}\n❯ hello\n{rule}\nfooter\n{rule}\n❯ draft\n{rule}"
        assert "draft" in tmux_ops._composer_text(pane)
        assert "hello" not in tmux_ops._composer_text(pane)

    def test_no_boundary_yields_none(self):
        """None = TUI 未画出输入框（须继续补 Enter），与"输入框为空"的空串不同。"""
        import tmux_ops
        assert tmux_ops._composer_text("no rules here") is None
