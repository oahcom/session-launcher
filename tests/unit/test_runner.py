#!/usr/bin/env python3
"""单元测试: ops/runner.py — 沉默吞异常路径覆盖

路径索引:
  S3  _start_feed_listener _connect socket 异常 → 重连
  S4  _start_feed_listener recv 异常 → 断开重连
  S5  _start_feed_listener socket close 异常 → 不冒泡
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))
sys.path.insert(1, str(Path(__file__).resolve().parent.parent.parent / "src" / "ops"))

import pytest

from ops.runner import _start_feed_listener


@pytest.fixture(autouse=True)
def _cleanup_threads():
    """各测试前清理残留线程引用，隔离测试。"""
    if hasattr(_start_feed_listener, "_threads"):
        _start_feed_listener._threads.clear()
    yield


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
