#!/usr/bin/env python3
"""
test_partner_client.py — 跨角色协作核心模块测试。

运行: python3 -m pytest tests/test_partner_client.py -v
"""

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock

# 确保 src 在路径中
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_THIS_DIR), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


# ── Layer 2: Status (resolve) ──────────────────────────────


@patch("partner_client.read_sentinel")
@patch("partner_client.is_ccs_running")
@patch("partner_client._find_claude_pid")
def test_resolve_alive(mock_find_pid, mock_is_running, mock_read_sentinel):
    """角色在线时 resolve 返回 alive=True。"""
    from partner_client import PartnerClient
    from sentinel import CcsSentinel, CcsHealth
    sentinel = CcsSentinel(
        role="pg", title="PG", pid=12345,
        lifecycle="infinite",
        health=CcsHealth(last_bus_msg_age=10.0),
    )
    mock_read_sentinel.return_value = sentinel
    mock_is_running.return_value = True
    mock_find_pid.return_value = 12345  # 哨兵 pid 一致

    pc = PartnerClient("qa")
    status = pc.resolve("pg")
    assert status["alive"] is True
    assert status["pid"] == 12345
    assert status["lifecycle"] == "infinite"


@patch("partner_client.read_sentinel")
@patch("partner_client.is_ccs_running")
def test_resolve_dead(mock_is_running, mock_read_sentinel):
    """角色离线时 resolve 返回 alive=False。"""
    from partner_client import PartnerClient
    from sentinel import CcsSentinel, CcsHealth
    sentinel = CcsSentinel(
        role="pg", title="PG", pid=0,
        lifecycle="ondemand",
        health=CcsHealth(),
    )
    mock_read_sentinel.return_value = sentinel
    mock_is_running.return_value = False

    pc = PartnerClient("qa")
    status = pc.resolve("pg")
    assert status["alive"] is False
    assert status["lifecycle"] == "ondemand"


@patch("partner_client.read_sentinel")
@patch("partner_client.is_ccs_running")
def test_resolve_no_sentinel(mock_is_running, mock_read_sentinel):
    """无哨兵文件时 resolve 返回基础信息。"""
    from partner_client import PartnerClient
    mock_read_sentinel.return_value = None
    mock_is_running.return_value = False

    pc = PartnerClient("qa")
    status = pc.resolve("unknown_role")
    assert status["alive"] is False
    assert status["lifecycle"] == "unknown"


# ── Layer 3: Wake 权限 ─────────────────────────────────────


def test_check_wake_permission_qa_to_pg():
    """QA 可以唤醒 PG。"""
    from partner_client import PartnerClient
    pc = PartnerClient("qa")
    assert pc.check_wake_permission("pg") is True


def test_check_wake_permission_pg_to_qa():
    """PG 不可以唤醒 QA。"""
    from partner_client import PartnerClient
    pc = PartnerClient("pg")
    assert pc.check_wake_permission("qa") is False


def test_check_wake_permission_lr_to_all():
    """LR 全局唤醒权限。"""
    from partner_client import PartnerClient
    pc = PartnerClient("lr")
    assert pc.check_wake_permission("pg") is True
    assert pc.check_wake_permission("qa") is True
    assert pc.check_wake_permission("reviewer") is True
    assert pc.check_wake_permission("coordinator") is True


def test_check_wake_permission_coordinator_to_all():
    """Coordinator 全局唤醒权限。"""
    from partner_client import PartnerClient
    pc = PartnerClient("coordinator")
    assert pc.check_wake_permission("pg") is True
    assert pc.check_wake_permission("qa") is True


def test_check_wake_permission_reviewer_to_qa():
    """Reviewer 可以唤醒 QA。"""
    from partner_client import PartnerClient
    pc = PartnerClient("reviewer")
    assert pc.check_wake_permission("qa") is True


def test_check_wake_permission_reviewer_to_pm():
    """Reviewer 不可以唤醒 PM（不在矩阵中）。"""
    from partner_client import PartnerClient
    pc = PartnerClient("reviewer")
    assert pc.check_wake_permission("pm") is False


# ── wake() 权限检查 ─────────────────────────────────────────


@patch("partner_client.check_wake_permission")
@patch("partner_client.wake_ccs")
def test_wake_with_permission(mock_wake_ccs, mock_check_perm):
    """有权限时 wake 调用 wake_ccs。"""
    from partner_client import PartnerClient
    mock_check_perm.return_value = True
    mock_wake_ccs.return_value = {"success": True, "role": "pg"}

    pc = PartnerClient("qa")
    result = pc.wake("pg", context="bug #123")
    assert result["success"] is True
    mock_wake_ccs.assert_called_once_with("pg", context="bug #123", by_role="qa")


@patch("partner_client.check_wake_permission")
@patch("partner_client.wake_ccs")
def test_wake_without_permission(mock_wake_ccs, mock_check_perm):
    """无权限时 wake 返回错误。"""
    from partner_client import PartnerClient
    mock_check_perm.return_value = False

    pc = PartnerClient("pg")
    result = pc.wake("qa", context="bug #123")
    assert result["success"] is False
    assert "权限不足" in result["error"]
    mock_wake_ccs.assert_not_called()


# ── force_start_ccs 内部权限检查 ────────────────────────────


@patch("partner_client.check_wake_permission")
@patch("partner_client.is_ccs_running")
def test_force_start_ccs_permission_denied(mock_is_running, mock_check_perm):
    """force_start_ccs 内部权限检查拦截 PG 启动 QA。"""
    from launcher import force_start_ccs
    mock_is_running.return_value = False
    mock_check_perm.return_value = False  # 由 launcher.check_wake_permission 返回

    # 直接 mock check_wake_permission 在 launcher 层面
    with patch("launcher.check_wake_permission", return_value=False):
        result = force_start_ccs("qa", by_role="pg")
        assert result["success"] is False
        assert "权限不足" in result["error"]


@patch("partner_client.is_ccs_running")
def test_force_start_ccs_authz_internal_check(mock_is_running):
    """force_start_ccs 内部独立检查权限，不依赖调用方前置检查。"""
    from launcher import force_start_ccs, check_wake_permission

    # PG 无权启动 QA
    assert check_wake_permission("pg", "qa") is False
    mock_is_running.return_value = False
    result = force_start_ccs("qa", by_role="pg")
    assert result["success"] is False


# ── 禁区 FORBIDDEN_MAP 枚举格式 ────────────────────────────


def test_forbidden_map_is_list():
    """_FORBIDDEN_MAP 值改为 list[str]，不是 str。"""
    from launcher import _FORBIDDEN_MAP
    for role, items in _FORBIDDEN_MAP.items():
        assert isinstance(items, list), f"{role} 的禁区值不是 list"
        for item in items:
            assert isinstance(item, str), f"{role} 的禁区项 {item} 不是 str"


def test_forbidden_map_enum_english():
    """_FORBIDDEN_MAP 使用英文枚举标识。"""
    from launcher import _FORBIDDEN_MAP
    known_enums = {
        "run_tests", "edit_config", "deploy", "start_ccs",
        "edit_persona_json", "write_code", "write_other_workspace",
    }
    for role, items in _FORBIDDEN_MAP.items():
        for item in items:
            assert item in known_enums, (
                f"{role} 的禁区项 {item} 不是已知枚举"
            )


def test_forbidden_list_display():
    """_forbidden_list 返回中文显示。"""
    from launcher import _forbidden_list
    result = _forbidden_list("qa")
    assert "写代码" in result
    assert "改配置" in result
    assert "部署" in result
    assert "start_ccs" not in result  # 中文显示，不是英文


# ── confirm_delivery 双信号 ────────────────────────────────


@patch("partner_client.PartnerClient._get_task")
@patch("partner_client.PartnerClient._check_bus_notification")
def test_confirm_delivery_task_not_created(mock_bus, mock_get_task):
    """信号①：task.status != 'created' → 确认成功。"""
    from partner_client import PartnerClient
    mock_get_task.return_value = {
        "task_id": "task_001",
        "status": "in_progress",
        "title": "test",
    }
    mock_bus.return_value = False

    pc = PartnerClient("qa")
    result = pc.confirm_delivery("task_001", "pg", timeout=30)
    assert result["confirmed"] is True
    assert "elapsed_sec" in result


def test_confirm_delivery_skip_start():
    """反例：created → completed 跳过 in_progress，仍确认。"""
    from partner_client import PartnerClient

    with patch.object(PartnerClient, "_get_task") as mock_get_task:
        mock_get_task.return_value = {
            "task_id": "task_001",
            "status": "completed",  # 跳过 in_progress
            "title": "test",
        }
        pc = PartnerClient("qa")
        result = pc.confirm_delivery("task_001", "pg", timeout=30)
        assert result["confirmed"] is True


# ── force_send 权限检查 ────────────────────────────────────


@patch("partner_client.is_ccs_running")
def test_force_send_alive(mock_is_running):
    """force_send 目标在线时直接发消息。"""
    from partner_client import PartnerClient

    mock_is_running.return_value = True

    with patch("partner_client.send") as mock_send:
        mock_send.return_value = {"success": True}
        pc = PartnerClient("qa")
        result = pc.force_send("pg", "hello", auto_wake=True)
        assert result["success"] is True
        mock_send.assert_called_once_with("pg", "hello")


@patch("partner_client.is_ccs_running")
def test_force_send_dead_no_auto_wake(mock_is_running):
    """force_send 目标离线 + auto_wake=False → 报错。"""
    from partner_client import PartnerClient
    mock_is_running.return_value = False

    pc = PartnerClient("qa")
    result = pc.force_send("pg", "hello", auto_wake=False)
    assert result["success"] is False
    assert "不在线" in result["error"]


# ── render_step(handoff) 测试 ──────────────────────────────


def test_render_step_handoff():
    """render_step(handoff) 输出只有摘要，不含 CLI 命令。"""
    from task_utils import render_step
    step = {
        "id": "s1",
        "type": "handoff",
        "title": "提交 bug 给 PG",
        "target_role": "pg",
        "confirm_timeout": 300,
    }
    output = render_step(step)
    assert "已将任务交由 pg 处理" in output
    assert "等待接单确认中" in output
    assert "python" not in output  # 不含 CLI 命令


def test_render_step_single():
    """render_step(single) 输出 prompt_template。"""
    from task_utils import render_step
    step = {
        "id": "s1",
        "type": "single",
        "title": "PG 修复代码",
    }
    output = render_step(step)
    assert "PG 修复代码" in output


# ── STEP_TYPES 常量 ───────────────────────────────────────


def test_step_types():
    """STEP_TYPES 包含三种类型。"""
    from task_utils import STEP_TYPES
    assert "single" in STEP_TYPES
    assert "handoff" in STEP_TYPES
    assert "review" in STEP_TYPES


# ── 唤醒 ondemand 角色（不区分退出类型） ───────────────────


@patch("partner_client.check_wake_permission")
def test_wake_ondemand_role(mock_check_perm):
    """wake_ccs 唤醒 ondemand 角色走标准 force_start_ccs。"""
    from launcher import force_start_ccs, is_ccs_running

    mock_check_perm.return_value = True
    with patch("core._is_alive", return_value=False):
        with patch("core.start") as mock_start:
            mock_start.return_value = {"success": True, "role": "pg"}
            # 直接调 force_start_ccs
            result = force_start_ccs("pg", by_role="qa")
            assert result["success"] is True


# ── 边界情况 ───────────────────────────────────────────────


def test_forbidden_display_all_keys():
    """_FORBIDDEN_DISPLAY 覆盖所有 _FORBIDDEN_MAP 中的枚举。"""
    from launcher import _FORBIDDEN_MAP, _FORBIDDEN_DISPLAY

    all_enums = set()
    for items in _FORBIDDEN_MAP.values():
        all_enums.update(items)
    missing = all_enums - set(_FORBIDDEN_DISPLAY.keys())
    assert not missing, f"缺少中文显示的枚举: {missing}"


def test_wake_permission_symmetry():
    """权限矩阵不对称性检查：至少存在一个不对称对。"""
    from launcher import check_wake_permission
    # QA → PG 应该通，PG → QA 应该不通
    assert check_wake_permission("qa", "pg") is True
    assert check_wake_permission("pg", "qa") is False
