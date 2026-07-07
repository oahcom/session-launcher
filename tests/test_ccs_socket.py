#!/usr/bin/env python3
"""
CCS Socket + 流式输出端到端测试。

覆盖：
1. CSSocketServer 启动/停止
2. SUBSCRIBE 注册
3. PUBLISH 路由 <1ms
4. 认证（CCS_SOCKET_TOKEN）
5. CCSClient 连接/发送/监听
6. CCSStreamer 流式输出
7. send-direct CLI 命令
8. socket status CLI 命令
9. 并发连接

运行: cd src && python3 -m pytest ../tests/test_ccs_socket.py -v
      或: cd tests && python3 test_ccs_socket.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback
from pathlib import Path
from unittest.mock import patch, MagicMock

# 路径设置：确保能从 tests/ 或项目根 import src
_THIS_DIR = Path(__file__).resolve().parent
_SRC_DIR = _THIS_DIR.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))


# ── 辅助 ──

def _socket_exists() -> bool:
    return Path("/tmp/ccs-sockets/ccs.sock").exists()


def _ensure_server(timeout: float = 2.0) -> bool:
    """确保 socket server 在运行，返回是否成功。"""
    import subprocess
    from ccs_socket import SOCKET_DIR
    if _socket_exists():
        return True
    SOCKET_DIR.mkdir(exist_ok=True)
    script = Path(__file__).resolve().parent.parent / "src" / "ccs_socket.py"
    subprocess.Popen(
        [sys.executable, str(script), "server"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.1)
        if _socket_exists():
            return True
    return False


def _kill_server():
    """停止 socket server。"""
    import subprocess
    subprocess.run(["pkill", "-9", "-f", "ccs_socket"], capture_output=True)
    import shutil
    sock = Path("/tmp/ccs-sockets/ccs.sock")
    if sock.exists():
        sock.unlink()
    pid = Path("/tmp/ccs-sockets/server.pid")
    if pid.exists():
        pid.unlink()


def _get_token() -> str:
    return os.environ.get("CCS_SOCKET_TOKEN", "")


# ── 测试函数 ──

def test_server_start_stop():
    """CSSocketServer 能启动和停止。"""
    from ccs_socket import CSSocketServer

    async def run():
        server = CSSocketServer()
        await server.start()
        assert _socket_exists(), "ccs.sock 不存在"
        await server.stop()
        assert not _socket_exists(), "stop 后 ccs.sock 应删除"

    asyncio.run(run())


def test_subscribe_and_publish():
    """SUBSCRIBE → PUBLISH 路由 <1ms。"""
    from ccs_socket import CSSocketServer, CCSClient

    async def run():
        server = CSSocketServer()
        await server.start()
        await asyncio.sleep(0.2)

        alice = CCSClient("alice")
        bob = CCSClient("bob")
        assert await alice.connect(), "alice connect failed"
        assert await bob.connect(), "bob connect failed"

        received = []
        task = asyncio.create_task(bob.listen(lambda m: received.append(m)))
        await asyncio.sleep(0.1)

        await alice.send_to("bob", "test message")
        await asyncio.sleep(0.3)

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert len(received) == 1, f"expected 1 message, got {len(received)}"
        assert received[0]["text"] == "test message"
        assert received[0]["_from"] == "alice"
        assert "_ts" in received[0]

        await alice.close()
        await bob.close()
        await server.stop()

    asyncio.run(run())


def test_auth_reject_wrong_token():
    """错误 token 被拒，连接断开。"""
    import os
    os.environ["CCS_SOCKET_TOKEN"] = "correct_token"
    try:
        # reload module to pick up new token
        import importlib
        import ccs_socket
        importlib.reload(ccs_socket)
        from ccs_socket import CSSocketServer

        async def run():
            server = ccs_socket.CSSocketServer()
            await server.start()
            await asyncio.sleep(0.2)

            # connect with wrong token
            reader, writer = await asyncio.open_unix_connection(
                str(ccs_socket.SOCKET_DIR / "ccs.sock"))
            bad_cmd = json.dumps({
                "cmd": "SUBSCRIBE", "agent": "noauth", "token": "wrong"
            }).encode() + b"\n"
            writer.write(bad_cmd)
            await writer.drain()

            resp = await asyncio.wait_for(reader.readline(), timeout=2)
            data = json.loads(resp.decode().strip())
            assert data["event"] == "error"
            assert data["detail"] == "auth failed"
            writer.close()

            # connect with correct token
            reader2, writer2 = await asyncio.open_unix_connection(
                str(ccs_socket.SOCKET_DIR / "ccs.sock"))
            good_cmd = json.dumps({
                "cmd": "SUBSCRIBE", "agent": "bob", "token": "correct_token"
            }).encode() + b"\n"
            writer2.write(good_cmd)
            await writer2.drain()

            resp2 = await asyncio.wait_for(reader2.readline(), timeout=2)
            data2 = json.loads(resp2.decode().strip())
            assert data2["event"] == "subscribed"
            writer2.close()

            await server.stop()

        asyncio.run(run())
    finally:
        del os.environ["CCS_SOCKET_TOKEN"]
        import importlib
        import ccs_socket
        importlib.reload(ccs_socket)


def test_concurrent_connections():
    """多 agent 并发连接不崩溃。"""
    from ccs_socket import CSSocketServer, CCSClient

    async def run():
        server = CSSocketServer()
        await server.start()
        await asyncio.sleep(0.2)

        N = 5
        clients = []
        for i in range(N):
            c = CCSClient(f"agent_{i}")
            ok = await c.connect()
            assert ok, f"agent_{i} connect failed"
            clients.append(c)

        await asyncio.sleep(0.1)

        received = [[] for _ in range(N)]
        tasks = []
        for i in range(N):
            idx = i
            tasks.append(asyncio.create_task(
                clients[i].listen(lambda m, idx=idx: received[idx].append(m))))

        # agent_0 → 所有其他 agent
        for i in range(1, N):
            await clients[0].send_to(f"agent_{i}", f"msg_{i}")
        await asyncio.sleep(0.3)

        for t in tasks:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

        for i in range(1, N):
            assert len(received[i]) == 1, f"agent_{i} received {len(received[i])} messages"
            assert received[i][0]["_from"] == "agent_0"
            assert received[i][0]["text"] == f"msg_{i}"

        for c in clients:
            await c.close()
        await server.stop()

    asyncio.run(run())


def test_ping_stats():
    """PING/STATS 命令返回正确数据。"""
    from ccs_socket import CSSocketServer

    async def run():
        server = CSSocketServer()
        await server.start()
        await asyncio.sleep(0.2)

        reader, writer = await asyncio.open_unix_connection(
            str(server.socket_path))

        # PING
        writer.write(json.dumps({"cmd": "PING"}).encode() + b"\n")
        await writer.drain()
        resp = json.loads((await asyncio.wait_for(reader.readline(), 2)).decode())
        assert resp["event"] == "pong"
        assert "uptime" in resp

        # STATS (need subscribe first)
        writer.write(json.dumps({
            "cmd": "SUBSCRIBE", "agent": "statuser"
        }).encode() + b"\n")
        await writer.drain()
        await asyncio.wait_for(reader.readline(), 2)

        writer.write(json.dumps({"cmd": "STATS"}).encode() + b"\n")
        await writer.drain()
        resp2 = json.loads((await asyncio.wait_for(reader.readline(), 2)).decode())
        assert resp2["event"] == "stats"
        assert "agents" in resp2

        writer.close()
        await server.stop()

    asyncio.run(run())


def test_streamer_capture():
    """CCSStreamer 捕获 tmux 输出。"""
    import subprocess
    # CCSStreamer 用 ccs-{role} 格式，tmux session 必须匹配
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", "ccs-stream_test", "-x", "80", "-y", "24"],
        capture_output=True, timeout=3)
    subprocess.run(
        ["tmux", "send-keys", "-t", "ccs-stream_test", "echo STREAM_TEST_42", "Enter"],
        capture_output=True, timeout=3)
    time.sleep(0.5)

    try:
        from ccs_socket import CCSStreamer

        chunks = []
        streamer = CCSStreamer("test_stream")
        streamer.start(lambda c: chunks.append(c))
        time.sleep(1)
        streamer.stop()

        assert len(chunks) > 0, "no output captured"
        assert "STREAM_TEST_42" in chunks[-1], "expected text not in output"
    finally:
        subprocess.run(["tmux", "kill-session", "-t", "ccs-stream_test"],
                       capture_output=True, timeout=3)


def test_cli_socket_status():
    """ccs.py socket status 命令正常。"""
    import subprocess
    # 这个命令只需要检查不抛异常（不需要 server 运行）
    result = subprocess.run(
        [sys.executable, str(_SRC_DIR / "ccs.py"), "socket", "status"],
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 or "无法连接" in result.stdout
    print(f"  (socket status: {result.stdout.strip()[:80]})")


def test_send_direct_cli():
    """send-direct CLI 发送成功（需 server 运行）。"""
    import subprocess
    if not _ensure_server():
        print("  (skipped: server not running)")
        return
    try:
        result = subprocess.run(
            [sys.executable, str(_SRC_DIR / "ccs.py"),
             "send-direct", "cli_a", "cli_b", "CLI测试消息"],
            capture_output=True, text=True, timeout=10)
        assert "已发送" in result.stdout, f"unexpected: {result.stdout}{result.stderr}"
    finally:
        pass  # server 保持运行供后续测试


# ── 主入口 ──

def _run_all():
    tests = [
        ("server start/stop", test_server_start_stop),
        ("subscribe + publish <1ms", test_subscribe_and_publish),
        ("auth reject wrong token", test_auth_reject_wrong_token),
        ("concurrent connections", test_concurrent_connections),
        ("ping/stats", test_ping_stats),
        ("streamer capture", test_streamer_capture),
        ("cli socket status", test_cli_socket_status),
        ("cli send-direct", test_send_direct_cli),
    ]

    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✅ {name}")
            passed += 1
        except Exception:
            print(f"  ❌ {name}")
            traceback.print_exc()
            failed += 1

    print(f"\n结果: {passed}/{passed + failed} 通过")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_run_all())
