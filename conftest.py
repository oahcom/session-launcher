"""conftest — 将 src/ 添加到 sys.path 以便测试导入。"""
import os
import sys

import pytest

_SRC = os.path.join(os.path.dirname(__file__), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


@pytest.fixture(autouse=True)
def _no_production_bus(monkeypatch, tmp_path):
    """测试不得写生产 bus：core.send 的痕迹会驱动真实角色收到提醒。

    2026-10-21 实测：test_core_lifecycle 用 source="cli" 调 send("qa","test message")，
    痕迹落进真实 bus，活体 qa 被注入 22 次「原始请求：test message」。
    钉 paths + core + ccls_daemon_intent_classifier 三处（各自快照 BUS_CLIENT）。
    events.signals / feed_listener 也快照该常量，但 tests/ 无触达；触达时加进来。
    """
    stub = tmp_path / "bus_client.py"
    stub.write_text('#!/usr/bin/env python3\n"""丢弃 bus 写入的测试桩。"""\n',
                    encoding="utf-8")
    stub.chmod(0o755)
    import paths
    monkeypatch.setattr(paths, "BUS_CLIENT", str(stub), raising=False)
    try:
        import core
    except ImportError:  # 非 core 相关测试不必加载整条依赖链
        return
    monkeypatch.setattr(core, "BUS_CLIENT", str(stub), raising=False)
    try:
        import ccls_daemon_intent_classifier as _daemon
    except ImportError:
        return
    monkeypatch.setattr(_daemon, "BUS_CLIENT", str(stub), raising=False)


@pytest.fixture(autouse=True)
def _no_production_side_effects(monkeypatch, tmp_path):
    """测试不得杀生产进程、不得改生产计数。

    2026-10-22 审计实测：core.stop("qa") 走 _kill_feed_listener_by_pidfile —— 读生产
    ~/.hermes/run/feed_listeners/ccs-qa.pid 后 os.kill(SIGTERM) 掉活着的
    feed_listener（pid 330050）并 unlink 该 pid 文件；core._save_send_fail_streak
    写生产 send_fail_streak.json（红线 3「三连败」的计数）。
    """
    try:
        import core
    except ImportError:
        return
    monkeypatch.setattr(core, "_kill_feed_listener_by_pidfile", lambda *a, **k: None,
                        raising=False)
    # 起进程同样要断：core.start() → _start_feed_subprocess() 真 Popen feed_listener，
    # 会连生产 bus、往真实 tmux 会话注入消息，并把生产 pid 文件覆盖成测试进程 pid
    # （2026-10-22 审计实测：跑一次 unit 测试多一个常驻 listener，ccs stop qa 会杀错进程）。
    monkeypatch.setattr(core, "_start_feed_subprocess", lambda *a, **k: None,
                        raising=False)
    monkeypatch.setattr(core, "_SEND_FAIL_STREAK_FILE", tmp_path / "send_fail_streak.json",
                        raising=False)
