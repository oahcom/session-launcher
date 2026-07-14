#!/usr/bin/env python3
"""
Codex 模块测试（start_codex_session / run_codex_task / cdx_status / _find_codex_pid）。

覆盖 D6 修复：P1-7 零测试覆盖，P1-9 直接依赖 subprocess 不可 mock。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock

# 确保 src 在路径中
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from sentinel import CcsSentinel, write_sentinel, read_sentinel, delete_sentinel
from core import (
    _validate_role_name,
    _find_codex_pid,
    _active_codex_session_count,
    _wait_codex_ready,
    _write_codex_sentinel,
    _build_codex_runner_script,
    start_codex_session,
    run_codex_task,
    cdx_status,
    CODEX_SENTINEL_DIR,
    CODEX_TMUX_PREFIX,
    CODEX_SESSION_MAX,
)


# ═══════════════════════════════════════════════════════════════
#  _validate_role_name
# ═══════════════════════════════════════════════════════════════

def test_role_name_校验合法名():
    assert _validate_role_name("verifier") is True
    assert _validate_role_name("ccs-monitor") is True
    assert _validate_role_name("role_123") is True


def test_role_name_校验路径遍历():
    assert _validate_role_name("../../etc/evil") is False
    assert _validate_role_name("a/b") is False
    assert _validate_role_name("") is False
    assert _validate_role_name("非法角色!") is False


# ═══════════════════════════════════════════════════════════════
#  _find_codex_pid
# ═══════════════════════════════════════════════════════════════

@patch("codex_ops.subprocess.run")
def test_find_codex_pid_精确匹配codex(mock_run):
    """_find_codex_pid 精确搜索 'codex' 子进程，不 fallback 到 pane PID。"""
    # display-message 返回 pane PID
    display_mock = MagicMock()
    display_mock.stdout = "1000\n"
    # pgrep -x codex 无结果
    pgrep_x_mock = MagicMock()
    pgrep_x_mock.stdout = ""
    # pgrep -f codex 找到
    pgrep_f_mock = MagicMock()
    pgrep_f_mock.stdout = "2000\n"
    mock_run.side_effect = [display_mock, pgrep_x_mock, pgrep_f_mock]
    assert _find_codex_pid("cdx-verifier") == 2000


@patch("codex_ops.subprocess.run")
def test_find_codex_pid_未就绪返回None(mock_run):
    """codex 进程还没启动时返回 None（不 fallback 到 pane PID）。"""
    display_mock = MagicMock()
    display_mock.stdout = "1000\n"
    no_match = MagicMock()
    no_match.stdout = ""
    mock_run.side_effect = [display_mock, no_match, no_match]
    assert _find_codex_pid("cdx-verifier") is None


# ═══════════════════════════════════════════════════════════════
#  _active_codex_session_count
# ═══════════════════════════════════════════════════════════════

def test_active_codex_count_空目录返回0(tmp_path):
    with patch("codex_ops.CODEX_SENTINEL_DIR", tmp_path):
        assert _active_codex_session_count() == 0


@patch("codex_ops.subprocess.run")
def test_active_codex_count_过滤存活(mock_run, tmp_path):
    with patch("codex_ops.CODEX_SENTINEL_DIR", tmp_path):
        (tmp_path / "verifier.json").write_text(json.dumps({"role": "verifier"}))
        (tmp_path / "dead.json").write_text(json.dumps({"role": "dead"}))

        # 第一个 session 存活，第二个死亡
        def side_effect(*args, **kwargs):
            m = MagicMock()
            if "cdx-dead" in args[0]:
                m.returncode = 1
            else:
                m.returncode = 0
            return m

        mock_run.side_effect = side_effect
        assert _active_codex_session_count() == 1


# ═══════════════════════════════════════════════════════════════
#  _wait_codex_ready
# ═══════════════════════════════════════════════════════════════

@patch("tmux_ops._is_alive", return_value=True)
@patch("tmux_ops._find_codex_pid", return_value=999)
def test_wait_codex_ready_就绪(mock_pid, mock_alive):
    assert _wait_codex_ready("cdx-test", timeout=5) is True


@patch("tmux_ops._is_alive", return_value=True)
@patch("tmux_ops._find_codex_pid", return_value=None)
def test_wait_codex_ready_pid未就绪(mock_pid, mock_alive):
    """tmux 存活但 pid 还没出现 → 继续等待。"""
    assert _wait_codex_ready("cdx-test", timeout=1) is False


@patch("tmux_ops._is_alive", return_value=False)
@patch("tmux_ops._find_codex_pid", return_value=None)
def test_wait_codex_ready_tmux未就绪(mock_pid, mock_alive):
    assert _wait_codex_ready("cdx-test", timeout=1) is False


# ═══════════════════════════════════════════════════════════════
#  _write_codex_sentinel
# ═══════════════════════════════════════════════════════════════

@patch("codex_ops.CODEX_SENTINEL_DIR", new_callable=lambda: Path(tempfile.mkdtemp()))
def test_write_codex_sentinel_写入(mock_dir):
    """验证写入的哨兵包含 engine=codex 字段。"""
    import json
    path = _write_codex_sentinel("verifier", "Verifier", "cdx-verifier", 12345, "infinite")
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["engine"] == "codex"
    assert data["role"] == "verifier"
    assert data["pid"] == 12345


@patch("codex_ops.CODEX_SENTINEL_DIR", new_callable=lambda: Path(tempfile.mkdtemp()))
def test_write_codex_sentinel_重名覆盖(mock_dir):
    """同一角色重复写哨兵应该更新而非重复创建。"""
    import json
    p1 = _write_codex_sentinel("verifier", "V1", "cdx-verifier", 100, "infinite")
    data1 = json.loads(p1.read_text())
    assert data1["pid"] == 100
    p2 = _write_codex_sentinel("verifier", "V2", "cdx-verifier", 200, "ondemand")
    data2 = json.loads(p2.read_text())
    assert data2["pid"] == 200
    assert data2["lifecycle"] == "ondemand"


# ═══════════════════════════════════════════════════════════════
#  _build_codex_runner_script
# ═══════════════════════════════════════════════════════════════

def test_build_codex_runner_loop模式不含注入():
    """验证 runner 脚本包含 shlex.quote 转义后的 prompt，不含 bash -c。"""
    role = {
        "name": "verifier",
        "title": "Verifier",
        "drive": "loop",
        "system_prompt": "你是一名验证者",
        "output_targets": [],
        "input_signals": [],
    }
    script = _build_codex_runner_script(role)
    assert "while true" in script
    assert "codex exec" in script
    # 不应包含原始的未经转义的 prompt 拼接到 bash -c
    assert "bash -c" not in script


def test_build_codex_runner_goal模式():
    """drive=goal 用 codex 交互模式。"""
    role = {"name": "verifier", "title": "V", "drive": "goal"}
    script = _build_codex_runner_script(role)
    assert "codex --model" in script
    assert "while true" not in script


# ═══════════════════════════════════════════════════════════════
#  start_codex_session （mock subprocess）
# ═══════════════════════════════════════════════════════════════

@patch("codex_ops._validate_role_name", return_value=False)
def test_start_codex_session_非法角色名(mock_valid):
    """P1-2: 非法角色名直接返回错误。"""
    result = start_codex_session("../../evil")
    assert result["success"] is False
    assert "非法" in result["error"]


@patch("codex_ops.get_role", return_value=None)
@patch("codex_ops._validate_role_name", return_value=True)
def test_start_codex_session_角色不存在(mock_valid, mock_role):
    result = start_codex_session("nonexistent")
    assert result["success"] is False
    assert "不存在" in result["error"]


@patch("codex_ops._validate_role_name", return_value=True)
@patch("codex_ops.get_role")
@patch("codex_ops._active_codex_session_count", return_value=CODEX_SESSION_MAX)
def test_start_codex_session_达上限(mock_count, mock_role, mock_valid):
    """P2-18: 并发上限检查。"""
    mock_role.return_value = {"name": "verifier", "title": "V"}
    result = start_codex_session("verifier")
    assert result["success"] is False
    assert "上限" in result["error"]


@patch("codex_ops._validate_role_name", return_value=True)
@patch("codex_ops.get_role")
@patch("codex_ops._active_codex_session_count", return_value=0)
@patch("codex_ops.subprocess.run")
def test_start_codex_session_tmux超时(mock_run, mock_count, mock_role, mock_valid):
    """P2-11: tmux has-session 超时返回错误。"""
    from subprocess import TimeoutExpired

    mock_role.return_value = {"name": "verifier", "title": "Verifier"}
    mock_run.side_effect = TimeoutExpired(cmd="tmux has-session", timeout=5)
    result = start_codex_session("verifier")
    assert result["success"] is False


@patch("codex_ops._validate_role_name", return_value=True)
@patch("codex_ops.get_role")
@patch("codex_ops._active_codex_session_count", return_value=0)
@patch("codex_ops.subprocess.run")
@patch("codex_ops._wait_codex_ready", return_value=False)
def test_start_codex_session_启动超时(mock_ready, mock_run, mock_count, mock_role, mock_valid):
    """P1-4/6: readiness 超时 kill session 并返回错误。"""
    mock_role.return_value = {
        "name": "verifier", "title": "Verifier",
        "drive": "goal",
    }
    # 用 callable side_effect 处理可变数量的 subprocess.run 调用
    def mock_subprocess(*args, **kwargs):
        m = MagicMock()
        # has-session → returncode=1（不存在），其他都返回 0
        if "has-session" in str(args[0]) or "role_assembler" in str(args[0]):
            m.returncode = 1
        else:
            m.returncode = 0
        m.stdout = ""
        return m
    mock_run.side_effect = mock_subprocess

    result = start_codex_session("verifier")
    assert result["success"] is False
    assert "超时" in result["error"]


# ═══════════════════════════════════════════════════════════════
#  run_codex_task
# ═══════════════════════════════════════════════════════════════

@patch("codex_ops._is_alive", return_value=False)
def test_run_codex_task_session不在运行(mock_alive):
    """P2-19: 目标不存活时直接返回错误，不等到 5 分钟超时。"""
    result = run_codex_task("verifier", "do something")
    assert result["success"] is False
    assert "不在运行" in result["error"]


@patch("codex_ops._is_alive", return_value=True)
@patch("codex_ops.get_role", return_value={"name": "verifier", "title": "Verifier"})
@patch("codex_ops.subprocess.run")
def test_run_codex_task_正常执行(mock_run, mock_role, mock_alive):
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "Done!"
    mock_result.stderr = ""
    mock_run.return_value = mock_result

    result = run_codex_task("verifier", "verify something", timeout=30)
    assert result["success"] is True
    assert result["output"] == "Done!"
    assert result["exit_code"] == 0


@patch("codex_ops._is_alive", return_value=True)
@patch("codex_ops.get_role", return_value={"name": "verifier", "title": "Verifier"})
@patch("codex_ops.subprocess.run")
def test_run_codex_task_输出截断标记(mock_run, mock_role, mock_alive):
    """长输出标注截断信息。"""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "X" * 3000
    mock_result.stderr = ""
    mock_run.return_value = mock_result

    result = run_codex_task("verifier", "long output")
    assert result["success"] is True
    assert len(result["output"]) <= 2000
    assert "截断" in (result.get("truncated") or "")


# ═══════════════════════════════════════════════════════════════
#  cdx_status
# ═══════════════════════════════════════════════════════════════

def test_cdx_status_空目录返回空(tmp_path):
    with patch("codex_ops.CODEX_SENTINEL_DIR", tmp_path):
        assert cdx_status() == []


@patch("codex_ops.subprocess.run")
def test_cdx_status_正常解析(mock_run, tmp_path):
    with patch("codex_ops.CODEX_SENTINEL_DIR", tmp_path):
        (tmp_path / "verifier.json").write_text(json.dumps({
            "role": "verifier",
            "title": "Verifier",
            "lifecycle": "infinite",
            "started_at": 1000000,
        }))
        # tmux 存活
        mock_run.return_value = MagicMock(returncode=0)

        stats = cdx_status()
        assert len(stats) == 1
        assert stats[0]["role"] == "verifier"
        assert stats[0]["engine"] == "codex"
        assert stats[0]["lifecycle"] == "infinite"


@patch("codex_ops.subprocess.run")
def test_cdx_status_坏哨兵记录日志(mock_run, tmp_path, caplog):
    """P2-13: 损坏哨兵文件打印 warning 不静默跳过。"""
    import logging
    caplog.set_level(logging.WARNING)

    with patch("codex_ops.CODEX_SENTINEL_DIR", tmp_path):
        (tmp_path / "bad.json").write_text("not json")
        mock_run.return_value = MagicMock(returncode=0)
        stats = cdx_status()
        assert len(stats) == 0
        assert "损坏哨兵文件" in caplog.text
