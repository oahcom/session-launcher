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
    钉 paths + core 两处（实际写入路径）。events.signals / ccls_daemon 各自
    快照 BUS_CLIENT，当前无测试触达；将来触达时把它们加进本夹具。
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
