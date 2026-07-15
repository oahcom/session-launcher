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

def _make_sys_block(name: str) -> str:
    """生成 <name> 对应的 WORKSPACE_SYS marker 内容块。"""
    return (
        f"{_WS_MARKER_START}\n"
        f"> 本 workspace 属于 {name}，由 CCS Launcher 自动管理。\n"
        "> 非必要请不要修改目录结构。\n"
        "\n"
        "## 工作流提示\n"
        "- 每个任务完成后更新 CLAUDE.md\n"
        "- 使用 bus 跨角色通信（cat=task / cat=code_fix …）\n"
        "- 用 `ccs send <角色> 消息` 调用其他角色\n"
        f"{_WS_MARKER_END}\n"
    )

def workspace_create(name: str) -> dict:
    """为角色创建工作空间（目录 + CLAUDE.md + 指南）。

    若 CLAUDE.md 已存在，会替换最后一组 WORKSPACE_SYS marker 内容，
    避免多次调用产生重复 block（T-C1 修复）。
    """
    ws_path = _WS_ROOT / name
    created = not ws_path.exists()
    ws_path.mkdir(parents=True, exist_ok=True)
    if created and ws_path.exists():
        created = True

    claude_md = ws_path / "CLAUDE.md"
    guide_path = ws_path / "WORKSPACE_GUIDE.md"
    sys_block = _make_sys_block(name)

    if not claude_md.exists():
        claude_md.write_text(sys_block, encoding="utf-8")
    else:
        # T-C1: 替换最后一组 marker，避免重复 block
        content = claude_md.read_text(encoding="utf-8")
        if _WS_MARKER_START in content and _WS_MARKER_END in content:
            start = content.rindex(_WS_MARKER_START)
            try:
                end = content.index(_WS_MARKER_END, start) + len(_WS_MARKER_END)
            except ValueError:
                end = len(content)
            new_content = content[:start] + sys_block + content[end:]
        elif _WS_MARKER_START in content:
            start = content.rindex(_WS_MARKER_START)
            new_content = content[:start] + sys_block
        else:
            new_content = content.rstrip() + "\n\n" + sys_block
        claude_md.write_text(new_content, encoding="utf-8")
        created = False  # 更新而非新建

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
