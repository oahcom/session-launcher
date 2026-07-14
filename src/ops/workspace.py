"""ops/workspace.py — 工作空间管理（从 core.py 提取）"""

import time
from pathlib import Path
from typing import Optional

from tmux_ops import _find_claude_pid, _is_alive
from role_manager import _WS_MARKER_START, _WS_MARKER_END, _validate_role_name
from ops.sentinel import CcsSentinel, write_sentinel

# ── 常量 ──
_SESSION_ROLES_ROOT = Path.home() / "hermes-session-roles"
_WS_ROOT = Path.home() / "ccs-workspaces"
_TMUX_PREFIX = "ccs-"


def register(role: str, tmux_name: str, title: str = "") -> dict:
    """注册已运行的 CCS 进程（用于恢复未注册的 session）"""
    tmux_name = tmux_name if tmux_name else f"{_TMUX_PREFIX}{role}"
    pid = _find_claude_pid(tmux_name)
    s = CcsSentinel(
        role=role, title=title or role, tmux_session=tmux_name,
        pid=pid, started_at=time.time(), lifecycle="infinite",
    )
    write_sentinel(s)
    return {"success": True, "role": role, "tmux_session": tmux_name, "pid": pid}


def workspace_create(name: str) -> dict:
    """为角色创建工作空间（目录 + CLAUDE.md + 指南）。"""
    ws_path = _WS_ROOT / name
    created = not ws_path.exists()
    ws_path.mkdir(parents=True, exist_ok=True)
    # 目录被外部删除后重建 -> 重置 created 标记
    if created and ws_path.exists():
        created = True

    claude_md = ws_path / "CLAUDE.md"
    guide_path = ws_path / "WORKSPACE_GUIDE.md"

    if not claude_md.exists():
        sys_block = f"""{_WS_MARKER_START}
> 本 workspace 属于 {name}，由 CCS Launcher 自动管理。
> 非必要请不要修改目录结构。

## 工作流提示
- 每个任务完成后更新 CLAUDE.md
- 使用 bus 跨角色通信（cat=task / cat=code_fix …）
- 用 `ccs send <角色> 消息` 调用其他角色
{_WS_MARKER_END}
"""
        claude_md.write_text(sys_block, encoding="utf-8")

    if not guide_path.exists():
        guide_path.write_text(f"# {name} — 工作空间指南\n\n", encoding="utf-8")

    return {"success": True, "action": "created" if created else "updated",
            "path": str(ws_path)}


def workspace_list() -> list[dict]:
    """列出所有工作空间及其活动状态。"""
    workspaces = []
    for d in sorted(_WS_ROOT.iterdir()):
        if not d.is_dir():
            continue
        role = d.name
        tmux_name = f"{_TMUX_PREFIX}{role}"
        alive = _is_alive(tmux_name)
        workspaces.append({"role": role, "path": str(d), "alive": alive})
    return workspaces
