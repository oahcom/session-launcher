"""test_ccs_socket.py — CCSClient 连接池和消息收发测试覆盖。"""
import asyncio
import json
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "src")




class TestCCSClient:
    """mock unix socket 测试连接池、重连、消息收发。"""

    @patch("ccs_socket.asyncio.open_unix_connection")
    @patch("ccs_socket.CCSClient._send", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_connect_success(self, mock_send, mock_open):
        from ccs_socket import CCSClient

        reader = AsyncMock()
        reader.readline = AsyncMock(return_value=json.dumps({"event": "subscribed"}).encode() + b"\n")
        writer = MagicMock()
        mock_open.return_value = (reader, writer)

        cli = CCSClient("test_role")
        ok = await cli.connect(max_retries=0)
        assert ok is True
        assert cli.reader == reader
        assert cli.writer == writer

    @patch("ccs_socket.asyncio.open_unix_connection")
    @pytest.mark.asyncio
    async def test_connect_retry_on_failure(self, mock_open):
        from ccs_socket import CCSClient

        mock_open.side_effect = ConnectionRefusedError("refused")
        cli = CCSClient("retry_role")
        ok = await cli.connect(max_retries=2)
        assert ok is False
        # 应重试多次
        assert mock_open.call_count >= 2

    @patch("ccs_socket.asyncio.open_unix_connection")
    @patch("ccs_socket.CCSClient._send", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_send_to_calls_publish(self, mock_send, mock_open):
        from ccs_socket import CCSClient

        reader = AsyncMock()
        reader.readline = AsyncMock(return_value=json.dumps({"event": "subscribed"}).encode() + b"\n")
        writer = MagicMock()
        mock_open.return_value = (reader, writer)

        cli = CCSClient("alice")
        await cli.connect(max_retries=0)
        mock_send.reset_mock()

        await cli.send_to("bob", "hello")
        expected = {"cmd": "PUBLISH", "to": "ccs-bob", "msg": {"text": "hello", "type": "chat", "_from_role": "alice"}}
        mock_send.assert_awaited_once_with(expected)

    @patch("ccs_socket.asyncio.open_unix_connection")
    @patch("ccs_socket.CCSClient._send", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_pool_reuse_connection(self, mock_send, mock_open):
        from ccs_socket import CCSClient

        reader = AsyncMock()
        reader.readline = AsyncMock(return_value=json.dumps({"event": "subscribed"}).encode() + b"\n")
        writer = MagicMock()
        writer.is_closing.return_value = False
        mock_open.return_value = (reader, writer)

        cli1 = CCSClient("pool_role")
        await cli1.connect(max_retries=0)
        assert mock_open.call_count == 1

        # 第二次 connect 应复用池中连接，不调用 open_unix_connection
        cli2 = CCSClient("pool_role")
        await cli2.connect(max_retries=0)
        assert mock_open.call_count == 1, "should reuse pooled connection"

    @patch("ccs_socket.asyncio.open_unix_connection")
    @patch("ccs_socket.CCSClient._send", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_pool_evict_after_idle_close(self, mock_send, mock_open):
        from ccs_socket import CCSClient

        reader = AsyncMock()
        reader.readline = AsyncMock(return_value=json.dumps({"event": "subscribed"}).encode() + b"\n")
        writer = MagicMock()
        writer.is_closing.return_value = True  # 模拟已关闭
        mock_open.return_value = (reader, writer)

        cli1 = CCSClient("evict_role")
        await cli1.connect(max_retries=0)

        # 模拟第二个 reader/writer（新连接）
        reader2 = AsyncMock()
        reader2.readline = AsyncMock(return_value=json.dumps({"event": "subscribed"}).encode() + b"\n")
        writer2 = MagicMock()
        mock_open.return_value = (reader2, writer2)

        cli2 = CCSClient("evict_role")
        await cli2.connect(max_retries=0)
        # writer.is_closing()=True → 池中连接被丢弃，应新建
        # ponytail: call_count=2 表示旧的被淘汰
        assert mock_open.call_count == 2

    @patch("ccs_socket.asyncio.open_unix_connection")
    @patch("ccs_socket.CCSClient._send", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_connect_unexpected_response(self, mock_send, mock_open):
        from ccs_socket import CCSClient

        reader = AsyncMock()
        reader.readline = AsyncMock(return_value=json.dumps({"event": "error", "msg": "bad"}).encode() + b"\n")
        writer = MagicMock()
        mock_open.return_value = (reader, writer)

        cli = CCSClient("bad_role")
        ok = await cli.connect(max_retries=0)
        assert ok is False

    @patch("ccs_socket.asyncio.open_unix_connection")
    @patch("ccs_socket.CCSClient._send", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_listen_receives_callback(self, mock_send, mock_open):
        from ccs_socket import CCSClient

        msg_payload = json.dumps({"event": "message", "msg": {"text": "hi"}}).encode() + b"\n"
        reader = AsyncMock()
        reader.readline = AsyncMock(side_effect=[
            json.dumps({"event": "subscribed"}).encode() + b"\n",
            msg_payload,
            b"",  # EOF
        ])
        writer = MagicMock()
        mock_open.return_value = (reader, writer)

        cli = CCSClient("listener")
        await cli.connect(max_retries=0)
        received = []
        await cli.listen(cb=lambda m: received.append(m))
        assert len(received) == 1
        assert received[0]["text"] == "hi"

    @patch("ccs_socket.asyncio.open_unix_connection")
    @patch("ccs_socket.CCSClient._send", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_close_cleans_pool(self, mock_send, mock_open):
        from ccs_socket import CCSClient

        reader = AsyncMock()
        reader.readline = AsyncMock(return_value=json.dumps({"event": "subscribed"}).encode() + b"\n")
        writer = MagicMock()
        mock_open.return_value = (reader, writer)

        cli = CCSClient("close_role")
        await cli.connect(max_retries=0)
        assert "close_role" in CCSClient._pools
        await cli.close()
        assert "close_role" not in CCSClient._pools
        assert cli.writer is None

    @patch("ccs_socket.asyncio.open_unix_connection")
    @patch("ccs_socket.CCSClient._send", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_connect_timeout_subbed(self, mock_send, mock_open):
        """SUBSCRIBE 响应超时触发重试。"""
        from ccs_socket import CCSClient

        reader = AsyncMock()
        reader.readline = AsyncMock(side_effect=asyncio.TimeoutError("timeout"))
        writer = MagicMock()
        mock_open.return_value = (reader, writer)

        cli = CCSClient("timeout_role")
        ok = await cli.connect(max_retries=1)
        assert ok is False
        # 1 try + 1 retry = 2 calls
        assert mock_open.call_count == 2


class TestCCSStreamer:
    """CCSStreamer 流式内容捕获测试。"""

    @patch("ccs_socket.subprocess.run")
    def test_start_captures_delta(self, mock_run):
        from ccs_socket import CCSStreamer

        # 第一次 poll: 全部内容
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "line1\nline2\n"

        streamer = CCSStreamer("test")
        received = []
        streamer.start(cb=lambda s: received.append(s))
        time.sleep(0.7)  # wait for one poll cycle
        streamer.stop()

        assert len(received) >= 1

    @patch("ccs_socket.subprocess.run")
    def test_start_sends_delta_only(self, mock_run):
        from ccs_socket import CCSStreamer

        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "base content\n"
        streamer = CCSStreamer("delta")
        received = []
        streamer.start(cb=lambda s: received.append(s))
        time.sleep(0.1)
        # 第二次: 新内容追加
        mock_run.return_value.stdout = "base content\nnew line\n"
        time.sleep(0.7)
        streamer.stop()

        assert any("new line" in s for s in received)

    @patch("ccs_socket.subprocess.run")
    def test_stop_ends_poll(self, mock_run):
        from ccs_socket import CCSStreamer

        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "data\n"
        streamer = CCSStreamer("stop")
        streamer.start(cb=lambda s: None)
        streamer.stop()
        assert streamer._running is False


if __name__ == "__main__":
    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-v"]))
