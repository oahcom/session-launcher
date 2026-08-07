#!/usr/bin/env python3
"""单元测试: core.py 生命周期核心函数"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

import pytest

# 需要测试的函数
from core import (
    start, stop, status, send, output, health_check,
    _validate_role_name,
    _resolve_ws_paths,
)


class TestCoreLifecycle:
    """core.py 生命周期函数测试"""

    def setup_method(self):
        """每个测试前的隔离环境"""
        self.temp_dir = tempfile.mkdtemp(prefix="ccs_test_")
        os.environ["CCS_WORKSPACES_ROOT"] = self.temp_dir
        os.environ["TMUX_TMPDIR"] = os.path.join(self.temp_dir, "tmux")
        os.makedirs(os.environ["TMUX_TMPDIR"], exist_ok=True)

    def teardown_method(self):
        """清理"""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch("core._is_alive")
    @patch("core._check_memory_before_launch")
    @patch("core.workspace_create")
    @patch("core.get_role")
    @patch("core.inject_role_knowledge_into_workspace")
    @patch("core._write_mcp_settings")
    @patch("core._write_instance_claude_md")
    @patch("core._register_instance_in_workspace")
    @patch("core.subprocess.run")
    @patch("core._tmux_send")
    @patch("core.time.sleep")
    def test_start_creates_session(
        self, mock_sleep, mock_tmux_send, mock_subprocess_run,
        mock_register_instance, mock_write_md, mock_write_mcp,
        mock_inject, mock_get_role, mock_workspace_create,
        mock_check_memory, mock_is_alive
    ):
        """start() 创建新 tmux session"""
        mock_is_alive.return_value = False
        mock_check_memory.return_value = None
        mock_workspace_create.return_value = {"success": True, "action": "created"}
        mock_get_role.return_value = {"name": "qa", "produce": ["test_report"], "consume": ["task_spec"]}
        mock_inject.return_value = None
        mock_write_mcp.return_value = None
        
        # subprocess.run for tmux new-session
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""
        mock_subprocess_run.return_value = mock_result

        result = start("qa", title="QA测试", detach=True)

        assert result["success"] is True
        assert result["role"] == "qa"
        mock_is_alive.assert_called_once()
        mock_check_memory.assert_called_once_with("qa")
        mock_workspace_create.assert_called_once()
        mock_subprocess_run.assert_called()  # tmux new-session called

    @patch("core._is_alive")
    def test_start_existing_session_returns_false(self, mock_is_alive):
        """start() 会话已存在返回 success=True (已运行)"""
        mock_is_alive.return_value = True

        result = start("qa", detach=True)

        assert result["success"] is True
        assert result["role"] == "qa"
        assert "tmux_session" in result
        # Should not call subprocess for new session

    @patch("core._is_alive")
    @patch("core._check_memory_before_launch")
    def test_start_memory_guard_fails(self, mock_check_memory, mock_is_alive):
        """start() 内存不足返回错误"""
        mock_is_alive.return_value = False
        mock_check_memory.return_value = "内存不足: 可用 100MB < 需要 500MB"

        result = start("qa", detach=True)

        assert result["success"] is False
        assert "内存不足" in result["error"]

    @patch("core._is_alive")
    @patch("core._tmux_kill")
    @patch("core.cleanup_stale_sentinels")
    def test_stop_kills_session(self, mock_cleanup, mock_tmux_kill, mock_is_alive):
        """stop() 杀掉 tmux session"""
        mock_is_alive.return_value = True
        mock_tmux_kill.return_value = None
        mock_cleanup.return_value = None

        result = stop("qa", instance_id=0)

        assert result["success"] is True
        mock_is_alive.assert_called_once()
        mock_tmux_kill.assert_called_once()

    @patch("core._is_alive")
    @patch("core._tmux_kill")
    @patch("core.delete_sentinel")
    def test_stop_nonexistent_session(self, mock_delete_sentinel, mock_tmux_kill, mock_is_alive):
        """stop() 会话不存在返回 success=False (非幂等)"""
        mock_is_alive.return_value = False
        mock_tmux_kill.return_value = None
        mock_delete_sentinel.return_value = None

        result = stop("qa", instance_id=0)

        assert result["success"] is False
        assert "CCS 不存在" in result.get("error", "")

    @patch("core._is_alive")
    @patch("core.list_sentinels")
    def test_status_running(self, mock_list_sentinels, mock_is_alive):
        """status() 运行中返回正确状态"""
        mock_is_alive.return_value = True
        from unittest.mock import MagicMock
        sentinel = MagicMock()
        sentinel.role = "qa"
        sentinel.instance_id = 0
        sentinel.title = "test"
        sentinel.pid = 12345
        sentinel.tmux_session = "ccs-qa"
        mock_list_sentinels.return_value = [sentinel]

        result = status()

        assert any(r["role"] == "qa" and r["alive"] for r in result)

    @patch("core._is_alive")
    @patch("core.list_sentinels")
    def test_status_not_running(self, mock_list_sentinels, mock_is_alive):
        """status() 未运行返回正确状态"""
        mock_is_alive.return_value = False
        from unittest.mock import MagicMock
        sentinel = MagicMock()
        sentinel.role = "qa"
        sentinel.instance_id = 0
        sentinel.title = "test"
        sentinel.pid = None
        sentinel.tmux_session = "ccs-qa"
        mock_list_sentinels.return_value = [sentinel]

        result = status()

        assert any(r["role"] == "qa" and not r["alive"] for r in result)

    @patch("core._is_alive")
    @patch("core._tmux_send")
    def test_send_message(self, mock_tmux_send, mock_is_alive):
        """send() 发送消息到 tmux"""
        mock_is_alive.return_value = True
        mock_tmux_send.return_value = None

        # source="cli" 跳过 gatekeeper 三源验证
        result = send("qa", "test message", source="cli", instance_id=0)

        assert result["success"] is True
        mock_tmux_send.assert_called_once()
        # Check message contains the test message
        args, _ = mock_tmux_send.call_args
        assert "test message" in args[1]

    @patch("core._tmux_output")
    def test_output_captures_tmux(self, mock_tmux_output):
        """output() 捕获 tmux pane 内容"""
        mock_tmux_output.return_value = "some output\nwith prompt ❯"

        result = output("qa", tail=10, instance_id=0)

        assert "some output" in result
        assert "prompt" in result

    @patch("core._is_alive")
    @patch("core._find_claude_pid")
    @patch("core.read_sentinel")
    def test_health_check_healthy(self, mock_read_sentinel, mock_find_pid, mock_is_alive):
        """health_check() 健康状态"""
        mock_is_alive.return_value = True
        mock_find_pid.return_value = 12345
        from unittest.mock import MagicMock
        sentinel = MagicMock()
        sentinel.role = "qa"
        sentinel.instance_id = 0
        sentinel.title = "test"
        sentinel.pid = 12345
        sentinel.tmux_session = "ccs-qa"
        sentinel.partners = []
        sentinel.bus_track = None
        sentinel.health.last_bus_msg_age = 30.0
        sentinel.health.watchdog_ok = True
        sentinel.health.restart_count = 0
        sentinel.started_at = 1000.0
        mock_read_sentinel.return_value = sentinel

        result = health_check("qa", instance_id=0)

        assert result["qa"]["alive"] is True
        assert result["qa"]["role"] == "qa"

    @patch("core._is_alive")
    @patch("core.read_sentinel")
    def test_health_check_unhealthy(self, mock_read_sentinel, mock_is_alive):
        """health_check() 不健康状态"""
        mock_is_alive.return_value = False
        from unittest.mock import MagicMock
        sentinel = MagicMock()
        sentinel.role = "qa"
        sentinel.instance_id = 0
        sentinel.title = "test"
        sentinel.pid = None
        sentinel.tmux_session = "ccs-qa"
        sentinel.partners = []
        sentinel.bus_track = None
        sentinel.health.last_bus_msg_age = 0.0
        sentinel.health.watchdog_ok = False
        sentinel.health.restart_count = 0
        sentinel.started_at = 0.0
        mock_read_sentinel.return_value = sentinel

        result = health_check("qa", instance_id=0)

        assert result["qa"]["alive"] is False
        assert result["qa"]["role"] == "qa"


class TestCoreUtilities:
    """core.py 工具函数测试"""

    def test_validate_role_name_valid(self):
        """_validate_role_name() 合法角色名"""
        assert _validate_role_name("qa") is True
        assert _validate_role_name("engineer") is True
        assert _validate_role_name("verifier") is True
        assert _validate_role_name("qa_01") is True

    def test_validate_role_name_invalid(self):
        """_validate_role_name() 非法角色名"""
        assert _validate_role_name("") is False
        assert _validate_role_name("qa!") is False
        assert _validate_role_name("qa ") is False

    def test_resolve_ws_paths(self):
        """_resolve_ws_paths() 解析工作空间路径"""
        paths = _resolve_ws_paths("qa")
        assert all("ccs-workspaces" in str(p) for p in paths)


class TestCoreEdgeCases:
    """core.py 边界情况测试"""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp(prefix="ccs_test_")
        os.environ["CCS_WORKSPACES_ROOT"] = self.temp_dir

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch("core._is_alive")
    @patch("core._check_memory_before_launch")
    @patch("core.workspace_create")
    @patch("core.get_role")
    @patch("core.inject_role_knowledge_into_workspace")
    @patch("core._write_mcp_settings")
    @patch("core.subprocess.run")
    @patch("core._tmux_send")
    @patch("core.time.sleep")
    def test_start_ondemand_mode(self, mock_sleep, mock_tmux_send, mock_subprocess_run,
                                  mock_write_mcp, mock_inject, mock_get_role,
                                  mock_workspace_create, mock_check_memory, mock_is_alive):
        """start() ondemand 模式不启动 tmux"""
        mock_is_alive.return_value = False
        mock_check_memory.return_value = None
        mock_get_role.return_value = {"name": "qa", "drive": "ondemand"}
        
        result = start("qa", drive="ondemand", detach=True)

        assert result["success"] is True
        assert result["mode"] == "ondemand"
        # Should not call tmux new-session
        mock_subprocess_run.assert_not_called()

    @patch("core._is_alive")
    @patch("core._check_memory_before_launch")
    @patch("core.workspace_create")
    @patch("core.get_role")
    @patch("core.inject_role_knowledge_into_workspace")
    @patch("core._write_mcp_settings")
    @patch("core.subprocess.run")
    @patch("core._tmux_send")
    @patch("core.time.sleep")
    def test_start_with_custom_workspace(self, mock_sleep, mock_tmux_send, mock_subprocess_run,
                                          mock_write_mcp, mock_inject, mock_get_role,
                                          mock_workspace_create, mock_check_memory, mock_is_alive):
        """start() 自定义 workspace 参数"""
        mock_is_alive.return_value = False
        mock_check_memory.return_value = None
        mock_workspace_create.return_value = {"success": True, "action": "created"}
        mock_get_role.return_value = {"name": "qa", "drive": "loop"}
        
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_subprocess_run.return_value = mock_result

        result = start("qa", workspace="myws", detach=True)

        assert result["success"] is True
        # workspace_create should be called with custom name
        mock_workspace_create.assert_called_with("myws")

    @patch("core._is_alive")
    @patch("core._tmux_send")
    def test_send_empty_message(self, mock_tmux_send, mock_is_alive):
        """send() 空消息处理"""
        mock_is_alive.return_value = True
        mock_tmux_send.return_value = None

        result = send("qa", "", source="cli", instance_id=0)

        assert result["success"] is True
        mock_tmux_send.assert_called_once()

    @patch("core._tmux_output")
    def test_output_large_tail(self, mock_tmux_output):
        """output() 大 tail 值"""
        mock_tmux_output.return_value = "x" * 10000

        result = output("qa", tail=10000, instance_id=0)

        assert isinstance(result, str)
        assert len(result) == 10000


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
