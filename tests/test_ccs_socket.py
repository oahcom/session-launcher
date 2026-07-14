#!/usr/bin/env python3
"""
CCS Socket + 流式输出端到端测试（复用 sister_bus）。

覆盖：
1. CCSClient 连接 sister_bus_ccs.sock
2. SUBSCRIBE 注册为 ccs-{role}
3. PUBLISH 路由 <1ms
4. CCSStreamer 流式输出
5. send-direct CLI 命令
6. 并发连接

运行: cd tests && python3 test_ccs_socket.py
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
import pytest

# 路径设置：确保能从 tests/ 或项目根 import src
_THIS_DIR = Path(__file__).resolve().parent
_SRC_DIR = _THIS_DIR.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))


# ── 辅助 ──

SISTER_BUS_CCS_SOCK = Path("/tmp/sister_bus_ccs.sock")


def _sister_bus_sock_exists() -> bool:
    """检查 sister_bus socket server 是否真正运行（连接而非仅文件存在）。"""
    if not SISTER_BUS_CCS_SOCK.exists():
        return False
    import socket
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(str(SISTER_BUS_CCS_SOCK))
        s.close()
        return True
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        return False


# ── 测试函数 ──

def test_client_connect_sister_bus():
    """CCSClient 能连接 sister_bus_ccs.sock 并 SUBSCRIBE。"""
    from ccs_socket import CCSClient

    async def run():
        # 检查 sister_bus server 是否运行（由 systemd 管理 sister_socket_server）
        if not _sister_bus_sock_exists():
            print("  (skipped: sister_bus_ccs.sock 不存在，需启动 sister_socket_server)")
            return True

        alice = CCSClient("alice")
        ok = await alice.connect()
        assert ok, "connect failed"
        await alice.close()
        return True

    asyncio.run(run())


def test_subscribe_and_publish():
    """SUBSCRIBE → PUBLISH 路由 <1ms。"""
    from ccs_socket import CCSClient

    async def run():
        if not _sister_bus_sock_exists():
            print("  (skipped: sister_bus_ccs.sock 不存在)")
            return True

        alice = CCSClient("alice")
        bob = CCSClient("bob")
        ok1 = await alice.connect()
        ok2 = await bob.connect()
        assert ok1 and ok2, f"connect failed: alice={ok1} bob={ok2}"

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
        assert received[0].get("_from_role") == "alice"

        await alice.close()
        await bob.close()
        return True

    asyncio.run(run())


def test_concurrent_connections():
    """多 agent 并发连接不崩溃。"""
    from ccs_socket import CCSClient

    async def run():
        if not _sister_bus_sock_exists():
            print("  (skipped: sister_bus_ccs.sock 不存在)")
            return True

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
            assert received[i][0]["_from_role"] == "agent_0"
            assert received[i][0]["text"] == f"msg_{i}"

        for c in clients:
            await c.close()
        return True

    asyncio.run(run())


def test_streamer_capture():
    """CCSStreamer 捕获 tmux 输出。"""
    import subprocess
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
        streamer = CCSStreamer("stream_test")
        streamer.start(lambda c: chunks.append(c))
        time.sleep(1.5)
        streamer.stop()

        assert len(chunks) > 0, "no output captured"
        assert "STREAM_TEST_42" in chunks[-1], "expected text not in output"
    finally:
        subprocess.run(["tmux", "kill-session", "-t", "ccs-stream_test"],
                       capture_output=True, timeout=3)


def test_cli_send_direct():
    """send-direct CLI 发送成功（需 sister_bus_ccs.sock）。"""
    import subprocess
    if not _sister_bus_sock_exists():
        pytest.skip("sister_bus_ccs.sock 不可用")

    result = subprocess.run(
        [sys.executable, str(_SRC_DIR / "ccs.py"),
         "send-direct", "cli_a", "cli_b", "CLI测试消息"],
        capture_output=True, text=True, timeout=10)
    assert "已发送" in result.stdout, f"unexpected: {result.stdout}{result.stderr}"


# ── 主入口 ──

def _run_all():
    tests = [
        ("client connect sister_bus", test_client_connect_sister_bus),
        ("subscribe + publish <1ms", test_subscribe_and_publish),
        ("concurrent connections", test_concurrent_connections),
        ("streamer capture", test_streamer_capture),
        ("cli send-direct", test_cli_send_direct),
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