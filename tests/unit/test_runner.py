#!/usr/bin/env python3
"""单元测试: ops/runner.py — 沉默吞异常路径覆盖

路径索引:
  S1  dashboard subprocess 失败 → fallback 消息
  S2  dashboard ast.literal_eval 失败 → fallback 消息
  S3  _start_feed_listener _connect socket 异常 → 重连
  S4  _start_feed_listener recv 异常 → 断开重连
  S5  _start_feed_listener socket close 异常 → 不冒泡
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from subprocess import TimeoutExpired

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))
sys.path.insert(1, str(Path(__file__).resolve().parent.parent.parent / "src" / "ops"))

import pytest

from ops.runner import dashboard, _start_feed_listener


@pytest.fixture(autouse=True)
def _cleanup_threads():
    """各测试前清理残留线程引用，隔离测试。"""
    if hasattr(_start_feed_listener, "_threads"):
        _start_feed_listener._threads.clear()
    yield


class TestDashboard:
    """S1/S2: dashboard() 沉默吞异常路径"""

    def test_subprocess_failure_returns_fallback_message(self):
        """S1: subprocess.run 抛出异常 → 返回包含 fallback 消息的仪表板。"""
        with patch("ops.runner.subprocess.run", side_effect=FileNotFoundError("no python")):
            result = dashboard()

        assert "pipeline router 不可达" in result
        assert "CCS 生态健康仪表板" in result

    def test_subprocess_timeout_returns_fallback(self):
        """S1: timeout 异常 → 返回 fallback 消息。"""
        with patch("ops.runner.subprocess.run", side_effect=TimeoutExpired("cmd", 10)):
            result = dashboard()

        assert "pipeline router 不可达" in result

    def test_corrupted_routing_output_returns_fallback(self):
        """S2: ast.literal_eval 解析失败 → fallback。"""
        mock_r = MagicMock()
        mock_r.returncode = 0
        mock_r.stdout = "not a valid python literal!!!\n"
        mock_r.stderr = ""

        with patch("ops.runner.subprocess.run", return_value=mock_r):
            result = dashboard()

        assert "pipeline router 不可达" in result

    def test_malformed_routing_output_returns_fallback(self):
        """S2: 标准输出非 dict 结构 → fallback。"""
        mock_r = MagicMock()
        mock_r.returncode = 0
        mock_r.stdout = "[1, 2, 3]\n"
        mock_r.stderr = ""

        with patch("ops.runner.subprocess.run", return_value=mock_r):
            result = dashboard()

        assert "pipeline router 不可达" in result

    def test_healthy_dashboard_contains_basic_sections(self):
        """正常路径：仪表板包含必要章节。"""
        with patch("ops.runner.subprocess.run", return_value=MagicMock(returncode=1)):
            result = dashboard()

        assert "CCS 生态健康仪表板" in result
        assert "CCS 哨兵" in result

    def test_subprocess_nonzero_exit_still_shows_dashboard(self):
        """S1: returncode != 0 仍显示仪表板（不进入路由解析块）。"""
        mock_r = MagicMock()
        mock_r.returncode = 1
        mock_r.stdout = ""

        with patch("ops.runner.subprocess.run", return_value=mock_r):
            result = dashboard()

        assert "pipeline router 不可达" not in result
        assert "CCS 生态健康仪表板" in result


class TestStartFeedListener:
    """S3/S4/S5: _start_feed_listener 异常路径"""

    def _assert_one_daemon_thread(self):
        """验证 _start_feed_listener 启动了一个守护线程且未冒泡异常。"""
        threads = getattr(_start_feed_listener, "_threads", [])
        assert len(threads) == 1
        assert threads[0].daemon is True

    def test_connect_socket_exception_does_not_raise(self):
        """S3: socket.connect 异常 → 静默重连，不冒泡到外层。"""
        mock_sock = MagicMock()
        mock_sock.connect.side_effect = ConnectionRefusedError

        with patch("socket.socket", return_value=mock_sock):
            _start_feed_listener("test_role", "test_cat")

        self._assert_one_daemon_thread()

    def test_recv_loop_exception_does_not_raise(self):
        """S4: recv 阶段异常 → 内部 break → 重连，不冒泡。"""
        mock_sock = MagicMock()
        mock_sock.connect.side_effect = lambda *a: None
        mock_sock.recv.side_effect = OSError("connection reset")

        with patch("socket.socket", return_value=mock_sock):
            _start_feed_listener("test_role", "test_cat")

        self._assert_one_daemon_thread()

    def test_recv_empty_data_triggers_reconnect(self):
        """S4: recv 返回空数据 → break → 重连循环。"""
        mock_sock = MagicMock()
        mock_sock.connect.side_effect = lambda *a: None
        mock_sock.recv.return_value = b""

        with patch("socket.socket", return_value=mock_sock):
            _start_feed_listener("test_role", "test_cat")

        self._assert_one_daemon_thread()

    def test_socket_close_exception_is_silent(self):
        """S5: socket.close 异常 → 只写 debug log，不冒泡。"""
        mock_sock = MagicMock()
        mock_sock.connect.side_effect = lambda *a: None
        mock_sock.recv.side_effect = [OSError("recv failed"), StopIteration]
        mock_sock.close.side_effect = OSError("close failed")

        with patch("socket.socket", return_value=mock_sock):
            with patch("ops.runner.log.debug") as mock_debug:
                _start_feed_listener("test_role", "test_cat")

        mock_debug.assert_any_call("feed socket close failed", exc_info=True)
        self._assert_one_daemon_thread()

    def test_corrupt_json_in_recv_skips_message(self):
        """S4: 收到损坏 JSON 行 → 跳过，不崩溃。"""
        mock_sock = MagicMock()
        mock_sock.connect.side_effect = lambda *a: None
        mock_sock.recv.side_effect = [
            b"{corrupt!!!}\n",
            b"\n",
            b"",
        ]

        with patch("socket.socket", return_value=mock_sock):
            _start_feed_listener("test_role", "test_cat")

        self._assert_one_daemon_thread()

    def test_message_with_wrong_cat_ignored(self):
        """正常路径：收到不同 cat 的消息不转发。"""
        mock_sock = MagicMock()
        mock_sock.connect.side_effect = lambda *a: None

        msg = json.dumps({"cmd": "MESSAGE", "cat": "other_cat", "data": "test"})
        mock_sock.recv.side_effect = [
            (msg + "\n").encode(),
            b"",
        ]

        with patch("socket.socket", return_value=mock_sock):
            with patch("tmux_ops._tmux_send") as mock_tmux_send:
                _start_feed_listener("test_role", "test_cat")

        mock_tmux_send.assert_not_called()
        self._assert_one_daemon_thread()

    def test_matching_message_forwards_to_tmux(self):
        """正常路径：匹配 cat 的消息转发到 tmux。"""
        mock_sock = MagicMock()
        mock_sock.connect.side_effect = lambda *a: None

        msg = json.dumps({"cmd": "MESSAGE", "cat": "test_cat", "data": "hello"})
        mock_sock.recv.side_effect = [
            (msg + "\n").encode(),
            b"",
        ]

        with patch("socket.socket", return_value=mock_sock):
            with patch("tmux_ops._tmux_send") as mock_tmux_send:
                _start_feed_listener("test_role", "test_cat")

        mock_tmux_send.assert_called_once()
        args, _ = mock_tmux_send.call_args
        assert args[0] == "ccs-test_role"
        sent_data = json.loads(args[1])
        assert sent_data["data"] == "hello"
        self._assert_one_daemon_thread()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
