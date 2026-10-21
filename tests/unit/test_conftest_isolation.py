"""R122 回归：测试执行不得写生产 bus（写入 = 活体角色收到「原始请求：test message」）。"""
from unittest.mock import MagicMock, patch

import pytest


def test_bus_client_points_to_test_stub(tmp_path):
    """conftest autouse 夹具必须钉住 core 与 paths 两处常量。"""
    import core
    import paths

    assert str(tmp_path) in core.BUS_CLIENT, "core 未被隔离 → send 仍写生产 bus"
    assert str(tmp_path) in paths.BUS_CLIENT, "paths 未被隔离 → 其他写入者漏网"


def test_send_writes_trace_to_stub_not_production_bus(tmp_path):
    """send() 的痕迹命令必须指向测试桩（core 模块级 import 的 BUS_CLIENT 也要补钉）。"""
    import core

    run = MagicMock(return_value=MagicMock(stdout="{}", returncode=0))
    with patch("core._is_alive", return_value=True), \
            patch("core._tmux_send", return_value=True), \
            patch("core.subprocess.run", run):
        assert core.send("qa", "test message", source="cli", instance_id=0)["success"]

    bus_cmds = [c.args[0] for c in run.call_args_list if "write" in c.args[0]]
    assert bus_cmds, "send 应写痕迹（本测试只关心写到哪里）"
    assert all(str(tmp_path) in cmd[0] for cmd in bus_cmds), f"痕迹写到了生产路径: {bus_cmds}"
