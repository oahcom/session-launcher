#!/usr/bin/env python3
"""单元测试: ops/tracker.py — 轮次追踪 + 死锁检测

路径索引:
  P1  _bus_read_latest   bus 有数据 → 返回 Fact
  P2  _bus_read_latest   bus 为空 → 返回 None
  P3  _bus_read_latest   Blackboard 抛异常 → 返回 None
  P4  _bus_write         正常写入 → Blackboard.write 被调用
  P5  _bus_write         写入异常 → 不崩溃，记 warning
  P6  _audit_monitor     写入格式正确 + 默认 src
  P7  _run               bus 无数据 → 跳过，不触发 update_health
  P8  _run               消息未超时 → 跳过
  P9  _run               消息超时 + 自己是作者 → 跳过
  P10 _run               消息超时 + 伙伴存活 + 提醒间隔 > 30min → 发提醒
  P11 _run               消息超时 + 伙伴死亡 → 仅审计
  P12 start_tracker      创建 daemon=False 线程并启动
"""
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))
sys.path.insert(1, str(Path(__file__).resolve().parent.parent.parent / "src" / "ops"))

import pytest

from ops.tracker import (
    _bus_read_latest,
    _bus_write,
    _audit_monitor,
    _run,
    start_tracker,
)


# ── helpers ──

def _fact(ts: float = 1000.0, src: str = "other_role", cat: str = "work"):
    """构造一个 mock Fact 对象（字段与 bus_protocol.Fact 对齐）。"""
    return SimpleNamespace(ts=ts, src=src, cat=cat)


def _sleep_breaker(max_calls: int):
    """返回 fake sleep：前 max_calls-1 次正常，第 max_calls 次 raise SystemExit。"""
    calls = [0]
    def fake_sleep(interval):
        calls[0] += 1
        if calls[0] >= max_calls:
            raise SystemExit
    return fake_sleep


def _tmux_ok():
    """让 _run 中的 tmux has-session 返回成功（returncode=0）。"""
    mock_mod = MagicMock()
    mock_mod.make_tmux_name.return_value = "ccs-partner-0"
    return mock_mod


def _tmux_fail():
    """让 _run 中的 tmux has-session 返回失败（returncode=1）。"""
    mock_mod = MagicMock()
    mock_mod.make_tmux_name.return_value = "ccs-partner-0"
    return mock_mod


# ── P1-P3: _bus_read_latest ──

class TestBusReadLatest:

    @patch("bus_protocol.Blackboard")
    def test_bus_read_latest_success(self, mock_cls):
        """P1: bus 有数据 → 返回第一个 Fact。"""
        fact = _fact()
        mock_cls.return_value.read.return_value = [fact]
        assert _bus_read_latest("work") is fact

    @patch("bus_protocol.Blackboard")
    def test_bus_read_latest_empty(self, mock_cls):
        """P2: bus 为空 → 返回 None。"""
        mock_cls.return_value.read.return_value = []
        assert _bus_read_latest("work") is None

    @patch("bus_protocol.Blackboard", side_effect=RuntimeError("db locked"))
    def test_bus_read_latest_exception(self, _mock_cls):
        """P3: Blackboard 抛异常 → 返回 None，不崩溃。"""
        assert _bus_read_latest("work") is None


# ── P4-P5: _bus_write ──

class TestBusWrite:

    @patch("bus_protocol.Blackboard")
    def test_bus_write_success(self, mock_cls):
        """P4: 正常写入 → Blackboard.write 被调用，参数正确。"""
        _bus_write("work", "test msg", src="tracker")
        mock_cls.return_value.write.assert_called_once_with(
            "work", "test msg", src="tracker"
        )

    @patch("bus_protocol.Blackboard", side_effect=RuntimeError("db locked"))
    def test_bus_write_exception(self, _mock_cls):
        """P5: 写入异常 → 不崩溃（warning 被记录）。"""
        _bus_write("work", "msg")  # should not raise


# ── P6: _audit_monitor ──

class TestAuditMonitor:

    @patch("ops.tracker._bus_write")
    def test_audit_monitor_format(self, mock_write):
        """P6: 审计写入格式为 "决策: {decision} → {detail}"。"""
        _audit_monitor("死锁检测", "detail text", src="tracker")
        mock_write.assert_called_once_with(
            "monitor_audit",
            "决策: 死锁检测 → detail text",
            src="tracker",
        )

    @patch("ops.tracker._bus_write")
    def test_audit_monitor_default_src(self, mock_write):
        """P6b: src 为空时默认用 "tracker"。"""
        _audit_monitor("test", "d")
        mock_write.assert_called_once_with(
            "monitor_audit", "决策: test → d", src="tracker"
        )


# ── P7-P11: _run 主循环路径 ──

class TestRun:

    @patch("ops.tracker.time.sleep", side_effect=SystemExit)
    @patch("ops.tracker.update_health")
    @patch("ops.tracker._bus_read_latest")
    def test_run_bus_empty_skips(self, mock_read, mock_health, _mock_sleep):
        """P7: bus 无数据 → 跳过，不调用 update_health。"""
        mock_read.return_value = None
        with pytest.raises(SystemExit):
            _run("role_a", "work", 300, 10, ["role_b"], instance_id=0)
        mock_health.assert_not_called()

    @patch("ops.tracker.time.sleep")
    @patch("ops.tracker.update_health")
    @patch("ops.tracker._bus_read_latest")
    def test_run_not_timed_out_skips(self, mock_read, mock_health, mock_sleep):
        """P8: 消息年龄 < timeout → 跳过。"""
        now = time.time()
        mock_read.return_value = _fact(ts=now - 50)  # age≈50 < timeout=300
        mock_sleep.side_effect = _sleep_breaker(2)
        with pytest.raises(SystemExit):
            _run("role_a", "work", 300, 10, ["role_b"], instance_id=0)
        mock_health.assert_called_once()

    @patch("ops.tracker.time.sleep")
    @patch("ops.tracker.update_health")
    @patch("ops.tracker._bus_read_latest")
    def test_run_self_authored_skips(self, mock_read, mock_health, mock_sleep):
        """P9: 消息超时 + 自己是作者 → 跳过（不发提醒）。"""
        now = time.time()
        mock_read.return_value = _fact(ts=now - 500, src="role_a")  # age=500 > 300
        mock_sleep.side_effect = _sleep_breaker(2)
        with pytest.raises(SystemExit):
            _run("role_a", "work", 300, 10, ["role_b"], instance_id=0)
        mock_health.assert_called_once()

    @patch("ops.tracker._audit_monitor")
    @patch("ops.tracker._bus_write")
    @patch("ops.tracker.update_health")
    @patch("ops.tracker._bus_read_latest")
    @patch("ops.tracker.time")
    def test_run_partner_alive_reminder(self, mock_time, mock_read, mock_health,
                                        mock_bwrite, mock_audit):
        """P10: 超时 + 伙伴存活 + 首次提醒 → 发提醒 + 审计。"""
        now = 1000000.0
        mock_time.time.return_value = now
        mock_time.sleep.side_effect = _sleep_breaker(2)
        mock_read.return_value = _fact(ts=now - 500, src="other_role")

        mock_tmux = _tmux_ok()
        mock_sub = MagicMock()
        mock_sub.run.return_value = MagicMock(returncode=0)

        with pytest.raises(SystemExit):
            with patch.dict("sys.modules", {"tmux_ops": mock_tmux, "subprocess": mock_sub}):
                _run("role_a", "work", 300, 10, ["role_b"], instance_id=0)

        mock_health.assert_called_once()
        mock_bwrite.assert_called_once()
        mock_audit.assert_called_once()
        # 审计包含 "死锁检测"
        assert mock_audit.call_args[0][0] == "死锁检测"

    @patch("ops.tracker._audit_monitor")
    @patch("ops.tracker._bus_write")
    @patch("ops.tracker.update_health")
    @patch("ops.tracker._bus_read_latest")
    @patch("ops.tracker.time")
    def test_run_partner_dead_audit_only(self, mock_time, mock_read, mock_health,
                                         mock_bwrite, mock_audit):
        """P11: 超时 + 伙伴死亡 → 仅审计，不发提醒。"""
        now = 1000000.0
        mock_time.time.return_value = now
        mock_time.sleep.side_effect = _sleep_breaker(2)
        mock_read.return_value = _fact(ts=now - 500, src="other_role")

        # tmux import will fail (no tmux_ops module) → partner_alive stays False
        with pytest.raises(SystemExit):
            _run("role_a", "work", 300, 10, ["role_b"], instance_id=0)

        mock_bwrite.assert_not_called()
        mock_audit.assert_called_once()
        assert "伙伴死亡" in mock_audit.call_args[0][0]


# ── P12: start_tracker ──

class TestStartTracker:

    @patch("ops.tracker.threading.Thread")
    def test_start_tracker_creates_thread(self, mock_thread_cls):
        """P12: start_tracker 创建 daemon=False 线程并 start。"""
        mock_thread = MagicMock()
        mock_thread_cls.return_value = mock_thread

        result = start_tracker(
            "role_a", "work", timeout_sec=300,
            interval=10, partners=["role_b"], instance_id=1
        )

        mock_thread_cls.assert_called_once()
        kwargs = mock_thread_cls.call_args
        assert kwargs[1]["daemon"] is False
        assert "role_a" in kwargs[1]["name"]
        assert "work" in kwargs[1]["name"]
        assert "1" in kwargs[1]["name"]
        args = kwargs[1]["args"]
        assert args[0] == "role_a"
        assert args[1] == "work"
        assert args[2] == 300
        assert args[3] == 10
        assert args[4] == ["role_b"]
        assert args[5] == 1
        mock_thread.start.assert_called_once()
        assert result is mock_thread
