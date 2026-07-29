#!/usr/bin/env python3
"""单元测试: routing/partner.py — 异常处理路径全覆盖

路径索引:
  P1  confirm_delivery      task 不存在 / _get_task 异常
  P2  confirm_delivery      _check_bus_notification 异常
  P3  confirm_delivery      降级阶梯 / 超时
  P4  _get_task             SQLite 异常
  P5  _check_bus_notification  subprocess/JSON 异常
  P6  resolve               workflow DB / _output 异常
  P7  _write_bus            subprocess 异常
  P8  wake                  权限不足
  P9  force_send            目标离线 + auto_wake 分支
"""
import json
import sys
import tempfile
from pathlib import Path
from subprocess import TimeoutExpired
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))
sys.path.insert(1, str(Path(__file__).resolve().parent.parent.parent / "src" / "ops"))

import pytest


# ══════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_workflow_db(tmp_path):
    """创建临时 workflows.db 并写入一条 task 记录。"""
    import sqlite3
    db_path = tmp_path / ".hermes" / "state" / "workflows.db"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, status TEXT)")
    conn.execute("INSERT INTO tasks (task_id, status) VALUES ('task_001', 'created')")
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def mock_paths(mock_workflow_db):
    """patch paths.WORKFLOW_DB 指向临时 DB。"""
    import routing.partner as _partner
    with patch.object(_partner, "WORKFLOW_DB", mock_workflow_db):
        yield


@pytest.fixture
def mock_sentinel():
    """返回一个模拟哨兵对象。"""
    from ops.sentinel import CcsSentinel, CcsHealth
    health = CcsHealth(watchdog_ok=True, last_bus_msg_age=5.0, restart_count=0)
    sentinel = CcsSentinel(
        role="engineer", instance_id=0, pid=12345,
        lifecycle="running", started_at=1000.0,
        partners=[], bus_track=[],
        health=health,
    )
    return sentinel


# ══════════════════════════════════════════════════════════════════
# P1: confirm_delivery — task 不存在
# ══════════════════════════════════════════════════════════════════

class TestConfirmDeliveryTaskNotFound:
    """P1: confirm_delivery 在 task 不存在时的路径"""

    def test_task_not_found_returns_immediate_failure(self, mock_paths):
        """P1a: task_id 不在 workflow DB → 立即返回 confirmed=False"""
        from routing.partner import PartnerClient
        pc = PartnerClient("tester")
        result = pc.confirm_delivery("nonexistent_task", "engineer", timeout=5)
        assert result["confirmed"] is False
        assert "不存在" in result["reason"]
        assert result["elapsed_sec"] == 0.0
        assert result["task"] is None

    def test_get_task_returns_none_when_db_missing(self):
        """P1b: DB 文件缺失 → _get_task 返回 None"""
        from routing.partner import PartnerClient
        import routing.partner as _partner
        fake_db = Path("/tmp/nonexistent_workflow_xyz.db")
        with patch.object(_partner, "WORKFLOW_DB", fake_db):
            pc = PartnerClient("tester")
            task = pc._get_task("task_001")
        assert task is None

    def test_get_task_returns_none_on_corrupt_db(self):
        """P1c: DB 损坏 → _get_task 返回 None（不抛异常）"""
        from routing.partner import PartnerClient
        import routing.partner as _partner
        fake_db = Path("/tmp/_test_corrupt_partner.db")
        fake_db.write_text("not a sqlite database")
        with patch.object(_partner, "WORKFLOW_DB", fake_db):
            pc = PartnerClient("tester")
            task = pc._get_task("task_001")
        assert task is None
        fake_db.unlink(missing_ok=True)


# ══════════════════════════════════════════════════════════════════
# P2: confirm_delivery — _check_bus_notification 异常
# ══════════════════════════════════════════════════════════════════

class TestConfirmDeliveryBusNotifException:
    """P2: _check_bus_notification 异常不影响主流程"""

    def test_bus_notif_exception_returns_false(self, mock_paths):
        """P2a: subprocess 抛异常 → 返回 False（不中断主循环）"""
        from routing.partner import PartnerClient
        with patch.object(PartnerClient, "_check_bus_notification",
                          side_effect=RuntimeError("unexpected error")):
            with patch.object(PartnerClient, "_get_task",
                              return_value={"task_id": "task_001", "status": "created"}):
                with patch("routing.partner.is_ccs_running", return_value=True):
                    with patch("routing.partner.time.sleep"):
                        with patch("routing.partner.time.time", side_effect=[0, 300]):
                            pc = PartnerClient("tester")
                            result = pc.confirm_delivery("task_001", "engineer", timeout=1)
        assert result["confirmed"] is False


# ══════════════════════════════════════════════════════════════════
# P3: confirm_delivery — 降级阶梯 / 超时
# ══════════════════════════════════════════════════════════════════

class TestConfirmDeliveryDegradation:
    """P3: 确认交付降级阶梯路径"""

    def test_timeout_when_task_never_consumed(self, mock_paths):
        """P3a: 超时 → confirmed=False"""
        from routing.partner import PartnerClient
        with patch("routing.partner.is_ccs_running", return_value=True):
            with patch.object(PartnerClient, "_check_bus_notification", return_value=False):
                with patch.object(PartnerClient, "_get_task",
                                  return_value={"task_id": "task_001", "status": "created"}):
                    with patch("routing.partner.time.sleep"):
                        with patch("routing.partner.time.time", side_effect=[0, 300]):
                            pc = PartnerClient("tester")
                            result = pc.confirm_delivery("task_001", "engineer", timeout=1)
        assert result["confirmed"] is False
        assert "超时" in result["reason"]

    def test_timeout_alive_writes_architecture_bus(self, mock_paths):
        """P3b: 超时且 target alive → 写入 bus architecture 升级"""
        from routing.partner import PartnerClient
        with patch("routing.partner.is_ccs_running", return_value=True):
            with patch.object(PartnerClient, "_check_bus_notification", return_value=False):
                with patch.object(PartnerClient, "_get_task",
                                  return_value={"task_id": "task_001", "status": "created"}):
                    with patch.object(PartnerClient, "_write_bus") as mock_write:
                        with patch("routing.partner.time.sleep"):
                            with patch("routing.partner.time.time", side_effect=[0, 300]):
                                pc = PartnerClient("tester")
                                result = pc.confirm_delivery("task_001", "engineer", timeout=1)
        assert result["confirmed"] is False
        # 超时后 final block 写入 architecture bus
        architecture_calls = [c for c in mock_write.call_args_list if c[0][0] == "architecture"]
        assert len(architecture_calls) == 1

    def test_degradation_at_120s_triggers_notify(self, mock_paths):
        """P3c: 120s → 发 workflow bus 提醒
        time.time 调用: start_ts(1) + while_cond(1) + body(1) per iteration
        side_effect: [0, 0, 0, 130, 130, 300]
                    start_ts  iter1     iter2     exit
        """
        from routing.partner import PartnerClient
        with patch("routing.partner.is_ccs_running", return_value=False):
            with patch.object(PartnerClient, "_check_bus_notification", return_value=False):
                with patch.object(PartnerClient, "_get_task",
                                  return_value={"task_id": "task_001", "status": "created"}):
                    with patch.object(PartnerClient, "_write_bus") as mock_write:
                        with patch("routing.partner.time.sleep"):
                            with patch("routing.partner.time.time",
                                       side_effect=[0, 0, 0, 130, 130, 300]):
                                pc = PartnerClient("tester")
                                pc.confirm_delivery("task_001", "engineer", timeout=300)
        workflow_calls = [c for c in mock_write.call_args_list if c[0][0] == "workflow"]
        assert len(workflow_calls) >= 1

    def test_degradation_at_240s_triggers_wake(self, mock_paths):
        """P3d: 240s + not alive → 自动唤醒"""
        from routing.partner import PartnerClient
        with patch("routing.partner.is_ccs_running", return_value=False):
            with patch.object(PartnerClient, "_check_bus_notification", return_value=False):
                with patch.object(PartnerClient, "_get_task",
                                  return_value={"task_id": "task_001", "status": "created"}):
                    with patch.object(PartnerClient, "_write_bus"):
                        with patch.object(PartnerClient, "wake") as mock_wake:
                            with patch("routing.partner.time.sleep"):
                                with patch("routing.partner.time.time",
                                           side_effect=[0, 0, 0, 250, 250, 300]):
                                    pc = PartnerClient("tester")
                                    pc.confirm_delivery("task_001", "engineer", timeout=300)
        assert mock_wake.called

    def test_60s_check_alive_no_extra_action(self, mock_paths):
        """P3e: 60s 分支检查 alive 但不额外动作"""
        from routing.partner import PartnerClient
        with patch("routing.partner.is_ccs_running", return_value=True):
            with patch.object(PartnerClient, "_check_bus_notification", return_value=False):
                with patch.object(PartnerClient, "_get_task",
                                  return_value={"task_id": "task_001", "status": "created"}):
                    with patch.object(PartnerClient, "_write_bus"):
                        with patch("routing.partner.time.sleep"):
                            with patch("routing.partner.time.time",
                                       side_effect=[0, 0, 0, 65, 65, 300]):
                                pc = PartnerClient("tester")
                                result = pc.confirm_delivery("task_001", "engineer", timeout=300)
        assert result["confirmed"] is False


# ══════════════════════════════════════════════════════════════════
# P4: _get_task — SQLite 异常
# ══════════════════════════════════════════════════════════════════

class TestGetTaskExceptions:
    """P4: _get_task 各种异常路径"""

    def test_sqlite_connect_fails_returns_none(self):
        """P4a: sqlite3.connect 失败 → 返回 None"""
        from routing.partner import PartnerClient
        import routing.partner as _partner
        fake_db = Path("/dev/null")
        with patch.object(_partner, "WORKFLOW_DB", fake_db):
            pc = PartnerClient("tester")
            result = pc._get_task("task_001")
        assert result is None

    def test_sqlite_execute_fails_returns_none(self):
        """P4b: execute 报错 → 返回 None"""
        from routing.partner import PartnerClient
        import routing.partner as _partner
        import sqlite3
        tmpdir = tempfile.mkdtemp()
        db_path = Path(tmpdir) / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE tasks (task_id TEXT)")
        conn.execute("INSERT INTO tasks VALUES ('task_001')")
        conn.commit()
        conn.close()
        with patch.object(_partner, "WORKFLOW_DB", db_path):
            pc = PartnerClient("tester")
            result = pc._get_task("task_001")
        assert result is not None
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════
# P5: _check_bus_notification — subprocess/JSON 异常
# ══════════════════════════════════════════════════════════════════

class TestCheckBusNotificationExceptions:
    """P5: _check_bus_notification 异常防护"""

    def test_subprocess_timeout_returns_false(self):
        """P5a: subprocess timeout → False"""
        from routing.partner import PartnerClient
        with patch("routing.partner.subprocess.run",
                   side_effect=TimeoutExpired("bus_client.py", 10)):
            pc = PartnerClient("tester")
            result = pc._check_bus_notification("engineer", "task_001")
        assert result is False

    def test_nonzero_returncode_returns_false(self):
        """P5b: returncode != 0 → False"""
        from routing.partner import PartnerClient
        mock = MagicMock()
        mock.returncode = 1
        with patch("routing.partner.subprocess.run", return_value=mock):
            pc = PartnerClient("tester")
            result = pc._check_bus_notification("engineer", "task_001")
        assert result is False

    def test_invalid_json_returns_false(self):
        """P5c: stdout 非合法 JSON → False"""
        from routing.partner import PartnerClient
        mock = MagicMock()
        mock.returncode = 0
        mock.stdout = "not json at all"
        with patch("routing.partner.subprocess.run", return_value=mock):
            pc = PartnerClient("tester")
            result = pc._check_bus_notification("engineer", "task_001")
        assert result is False

    def test_fact_with_task_id_in_text_returns_true(self):
        """P5d: bus fact text 匹配 task_id → True"""
        from routing.partner import PartnerClient
        mock = MagicMock()
        mock.returncode = 0
        mock.stdout = json.dumps([{"text": "engineer 已接单 task_001", "id": 1}])
        with patch("routing.partner.subprocess.run", return_value=mock):
            pc = PartnerClient("tester")
            result = pc._check_bus_notification("engineer", "task_001")
        assert result is True

    def test_fact_with_task_id_in_evidence_returns_true(self):
        """P5e: bus fact evidence 匹配 task_id → True"""
        from routing.partner import PartnerClient
        mock = MagicMock()
        mock.returncode = 0
        mock.stdout = json.dumps([{"text": "some msg", "e": "task=task_001", "id": 2}])
        with patch("routing.partner.subprocess.run", return_value=mock):
            pc = PartnerClient("tester")
            result = pc._check_bus_notification("engineer", "task_001")
        assert result is True

    def test_any_exception_returns_false(self):
        """P5f: 任意 Exception → 返回 False（不解锁主流程）"""
        from routing.partner import PartnerClient
        with patch("routing.partner.subprocess.run",
                   side_effect=RuntimeError("crash")):
            pc = PartnerClient("tester")
            result = pc._check_bus_notification("engineer", "task_001")
        assert result is False


# ══════════════════════════════════════════════════════════════════
# P6: resolve — workflow DB / _output 异常
# ══════════════════════════════════════════════════════════════════

class TestResolveExceptions:
    """P6: resolve 方法异常路径"""

    def test_workflow_db_query_exception_logged(self, mock_sentinel):
        """P6a: workflow DB 查询异常 → logger.debug + 继续返回"""
        from routing.partner import PartnerClient
        import routing.partner as _partner
        with patch("routing.partner.read_sentinel", return_value=mock_sentinel):
            with patch("routing.partner.is_ccs_running", return_value=True):
                with patch("routing.partner._find_pid", return_value=12345):
                    with patch("routing.partner._output", return_value="last line"):
                        with patch.object(_partner, "WORKFLOW_DB",
                                          Path("/nonexistent/db/path")):
                            pc = PartnerClient("tester")
                            result = pc.resolve("engineer")
        assert result["role"] == "engineer"
        assert result["alive"] is True
        assert result["pending_tasks"] == 0
        assert result["current_task_id"] is None

    def test_tmux_output_exception_returns_neg_one(self, mock_sentinel):
        """P6b: _output 异常 → last_active_sec 保持 -1.0"""
        from routing.partner import PartnerClient
        with patch("routing.partner.read_sentinel", return_value=mock_sentinel):
            with patch("routing.partner.is_ccs_running", return_value=True):
                with patch("routing.partner._find_pid", return_value=12345):
                    with patch("routing.partner._output",
                               side_effect=RuntimeError("tmux dead")):
                        pc = PartnerClient("tester")
                        result = pc.resolve("engineer")
        assert result["last_active_sec"] == -1.0

    def test_sentinel_none_still_returns_basic_info(self):
        """P6c: 哨兵不存在 → 基本字段使用默认值"""
        from routing.partner import PartnerClient
        with patch("routing.partner.read_sentinel", return_value=None):
            with patch("routing.partner.is_ccs_running", return_value=False):
                pc = PartnerClient("tester")
                result = pc.resolve("ghost_role")
        assert result["role"] == "ghost_role"
        assert result["alive"] is False
        assert result["pid"] is None
        assert result["lifecycle"] == "unknown"


# ══════════════════════════════════════════════════════════════════
# P7: _write_bus — subprocess 异常
# ══════════════════════════════════════════════════════════════════

class TestWriteBusExceptions:
    """P7: _write_bus 异常不抛到调用层"""

    def test_subprocess_timeout_swallowed(self):
        """P7a: subprocess timeout → logger.debug + 不抛异常"""
        from routing.partner import PartnerClient
        with patch("routing.partner.subprocess.run",
                   side_effect=TimeoutExpired("bus_client.py", 15)):
            pc = PartnerClient("tester")
            pc._write_bus("workflow", "test title", "test evidence")

    def test_subprocess_crash_swallowed(self):
        """P7b: subprocess RuntimeError → 不抛异常"""
        from routing.partner import PartnerClient
        with patch("routing.partner.subprocess.run",
                   side_effect=RuntimeError("bus_client.py not found")):
            pc = PartnerClient("tester")
            pc._write_bus("workflow", "test title", "test evidence")

    def test_bus_client_path_invalid(self):
        """P7c: BUS_CLIENT 路径不存在 → 静默吞掉"""
        from routing.partner import PartnerClient
        import routing.partner as _partner
        with patch.object(_partner, "_bus_client",
                          return_value=Path("/nonexistent/bus_client.py")):
            pc = PartnerClient("tester")
            pc._write_bus("workflow", "test title", "test evidence")


# ══════════════════════════════════════════════════════════════════
# P8: wake — 权限不足
# ══════════════════════════════════════════════════════════════════

class TestWakePermission:
    """P8: wake 权限检查路径"""

    def test_wake_permission_denied(self):
        """P8a: 权限不足 → 返回 error"""
        from routing.partner import PartnerClient
        with patch("routing.partner._check_wake", return_value=False):
            pc = PartnerClient("intern")
            result = pc.wake("engineer")
        assert result["success"] is False
        assert "权限不足" in result["error"]

    def test_wake_force_skip_permission_check(self):
        """P8b: force=True → 跳过权限检查"""
        from routing.partner import PartnerClient
        with patch("routing.partner._check_wake", return_value=False):
            with patch("routing.partner._wake_ccs",
                       return_value={"success": True}):
                pc = PartnerClient("intern")
                result = pc.wake("engineer", force=True)
        assert result["success"] is True

    def test_wake_permission_granted(self):
        """P8c: 有唤醒权限 → 调用 _wake_ccs"""
        from routing.partner import PartnerClient
        with patch("routing.partner._check_wake", return_value=True):
            with patch("routing.partner._wake_ccs",
                       return_value={"success": True, "action": "started"}):
                pc = PartnerClient("coordinator")
                result = pc.wake("engineer", context="test ctx")
        assert result["success"] is True
        assert result["action"] == "started"


# ══════════════════════════════════════════════════════════════════
# P9: force_send — 目标不在线 + auto_wake 分支
# ══════════════════════════════════════════════════════════════════

class TestForceSendExceptions:
    """P9: force_send 异常路径"""

    def test_offline_no_auto_wake(self):
        """P9a: 目标离线 + auto_wake=False → 返回 error"""
        from routing.partner import PartnerClient
        with patch("routing.partner.is_ccs_running", return_value=False):
            pc = PartnerClient("tester")
            result = pc.force_send("engineer", "hello", auto_wake=False)
        assert result["success"] is False
        assert "不在线" in result["error"]

    def test_offline_auto_wake_no_permission(self):
        """P9b: 离线 + auto_wake 但权限不足 → 返回 error"""
        from routing.partner import PartnerClient
        with patch("routing.partner.is_ccs_running", return_value=False):
            with patch.object(PartnerClient, "check_wake_permission",
                              return_value=False):
                pc = PartnerClient("intern")
                result = pc.force_send("engineer", "hello", auto_wake=True)
        assert result["success"] is False
        assert "权限不足" in result["error"]

    def test_offline_wake_failed(self):
        """P9c: 离线 + 权限够但 wake 失败 → 返回 wake 结果"""
        from routing.partner import PartnerClient
        wake_fail = {"success": False, "error": "start failed"}
        with patch("routing.partner.is_ccs_running", return_value=False):
            with patch.object(PartnerClient, "check_wake_permission",
                              return_value=True):
                with patch.object(PartnerClient, "wake", return_value=wake_fail):
                    pc = PartnerClient("tester")
                    result = pc.force_send("engineer", "hello", auto_wake=True)
        assert result["success"] is False
        assert "start failed" in result["error"]

    def test_offline_wake_then_send_success(self):
        """P9d: 离线 → wake → 等待就绪 → send 成功"""
        from routing.partner import PartnerClient
        wake_ok = {"success": True, "action": "started"}
        alive_returns = [False] + [True] * 10
        with patch("routing.partner.is_ccs_running",
                   side_effect=alive_returns):
            with patch.object(PartnerClient, "check_wake_permission",
                              return_value=True):
                with patch.object(PartnerClient, "wake", return_value=wake_ok):
                    with patch("routing.partner._send",
                               return_value={"success": True, "sent_chars": 5}):
                        pc = PartnerClient("tester")
                        result = pc.force_send("engineer", "hello", auto_wake=True)
        assert result["success"] is True

    def test_alive_sends_directly(self):
        """P9e: 目标在线 → 直接 send"""
        from routing.partner import PartnerClient
        with patch("routing.partner.is_ccs_running", return_value=True):
            with patch("routing.partner._send",
                       return_value={"success": True, "sent_chars": 5}):
                pc = PartnerClient("tester")
                result = pc.force_send("engineer", "hello", auto_wake=True)
        assert result["success"] is True
        assert result["sent_chars"] == 5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
