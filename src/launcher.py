#!/usr/bin/env python3
"""
launcher.py — 兼容层（所有函数委托给 core.py）

旧版代码从 launcher 导入的函数/常量全部在此重新导出。
新版代码应直接从 core / ccs 导入。
"""
import sys
from pathlib import Path

# ── 确保本项目模块可导入 ──
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

# ── 全部从 core.py 重新导出 ──
# 函数
from core import (
    load_roles, get_role, _invalidate_role_cache,
    check_signal, has_work, auto_schedule,
    _forbidden_list, check_wake_permission,
    _build_role_prompt, _action_templates,
    inject_role_knowledge_into_workspace, inject_prompt_into_claudemd,
    clear_injected_prompt, _ensure_bus_aliases_in_bashrc,
    _find_claude_pid, _find_codex_pid,
    _is_alive, _tmux_send, _tmux_output, _tmux_kill,
    force_start_ccs, wake_ccs,
    write_lifecycle_sentinel, check_ondemand_timeout, cleanup_stale_sentinels,
    start_codex_session, run_codex_task, cdx_status,
    _start_feed_listener,
    register, workspace_create, workspace_list,
)
# 核心操作（不同名兼容）
from core import (
    start as start_ccs,
    stop as stop_ccs,
    status as ccs_status,
    send as send_to_ccs,
    output as ccs_capture_output,
    health_check,
)

# 常量
from core import (
    TMUX_PREFIX as CCS_TMUX_PREFIX,
    CODEX_TMUX_PREFIX, CODEX_SENTINEL_DIR,
    BUS_CLIENT, SESSION_ROLES_ROOT,
    _FORBIDDEN_MAP, _FORBIDDEN_DISPLAY,
    _WAKE_PERMISSION_MAP,
    _WS_MARKER_START, _WS_MARKER_END,
    SESSION_MARKER_START, SESSION_MARKER_END,
    _CLAUDE_MD as CLAUDE_MD,
    _LIFECYCLE_SENTINEL_DIR as LIFECYCLE_SENTINEL_DIR,
)

# 哨兵目录兼容名
from sentinel import SENTINEL_DIR as CCS_SENTINEL_DIR

# 旧名兼容
exec_codex = run_codex_task


def ccs_capture_output_raw(tmux_name: str, tail: int = 10) -> str:
    """从 tmux pane 捕获原始输出（旧版兼容）。"""
    from core import _tmux_output
    return _tmux_output(tmux_name, tail=tail)


def is_ccs_running(role_name: str) -> bool:
    """检查 CCS 是否在运行（兼容层，委托给 core._is_alive）。"""
    from core import _is_alive
    return _is_alive(f"ccs-{role_name}")


# ── 旧版 CLI 入口 ──
# 新版 CLI 已在 ccs.py，这里保留仅用于向后兼容

def main():
    """委托给 ccs.py 的 main()。"""
    from ccs import main as _ccs_main
    _ccs_main()
