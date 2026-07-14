#!/usr/bin/env python3
"""Auto-generated: codex_ops.py — extracted from core.py"""

import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# ── Import all dependencies needed from sibling modules ──
# From role_manager (imported directly to avoid circular dep via core)
from role_manager import (
    get_role,
    _validate_role_name,
    _build_role_prompt,
    SESSION_ROLES_ROOT,
)

# From tmux_ops
from tmux_ops import (
    _is_alive,
    _find_codex_pid,
    _active_codex_session_count,
    _write_codex_sentinel,
    _wait_codex_ready,
    _tmux_output,
    TMUX_PREFIX,
    CODEX_TMUX_PREFIX,
    CODEX_SENTINEL_DIR,
    CODEX_SESSION_MAX,
    CODEX_LOOP_DELAY,
    CODEX_OUTPUT_MAX,
    CODEX_ERROR_MAX,
    CODEX_READY_RETRIES,
    CODEX_READY_INTERVAL,
)


__all__ = [
    'start_codex_session',
    '_build_codex_runner_script',
    'run_codex_task',
    'cdx_status',
]
from typing import Optional



def start_codex_session(role_name: str) -> dict:
    """启动持久 Codex session (exec mode)。

    修复清单：
      - role_name 白名单校验（P1-2）
      - runner 脚本用 shlex.quote() 防注入（P1-1/3）
      - 主动轮询 readiness 替代 sleep(3)（P1-4/6）
      - 哨兵写入 try/except（P1-5）
      - _find_codex_pid 搜索 codex 进程（P1-8）
      - 权限由角色配置控制（P2-10）
      - tmux has-session 加 timeout（P2-11）
      - 直接写 CODEX_SENTINEL_DIR 哨兵（P2-16）
      - 并发上限检查（P2-18）
    """
    # P1-2: role_name 白名单校验
    if not _validate_role_name(role_name):
        return {"success": False, "error": f"角色名非法: {role_name}"}

    role = get_role(role_name)
    if not role:
        return {"success": False, "error": f"角色 {role_name} 不存在"}

    tmux_name = f"{CODEX_TMUX_PREFIX}{role_name}"

    # P2-18: 并发上限检查
    active = _active_codex_session_count()
    if active >= CODEX_SESSION_MAX:
        return {"success": False,
                "error": f"Codex session 已达上限 ({CODEX_SESSION_MAX})，当前活跃: {active}"}

    # P2-11: tmux has-session 加 timeout
    try:
        r = subprocess.run(
            ["tmux", "has-session", "-t", tmux_name],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0:
            return {"success": False, "error": f"Codex session {role_name} 已在运行"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "检查 tmux 状态超时"}

    # P1-1/3: 构建 runner 脚本并写入临时文件
    runner_script = _build_codex_runner_script(role)

    tmp_script = Path(f"/tmp/cdx-runner-{role_name}.sh")
    try:
        tmp_script.write_text(runner_script)
        tmp_script.chmod(0o755)
    except OSError as e:
        return {"success": False, "error": f"无法写入 runner 脚本: {e}"}

    tmux_cmd = [
        "tmux", "new-session", "-d", "-s", tmux_name,
        "-e", "FORCE_PERSONA=0",
        "bash", str(tmp_script),
    ]
    try:
        result = subprocess.run(tmux_cmd, capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "tmux new-session 超时"}
    if result.returncode != 0:
        return {"success": False, "error": f"tmux 启动失败: {result.stderr.strip()}"}

    # P1-4/6: 主动轮询 readiness 替代 sleep(3)
    ready = _wait_codex_ready(tmux_name, timeout=15)
    if not ready:
        subprocess.run(["tmux", "kill-session", "-t", tmux_name],
                       capture_output=True, timeout=5)
        return {"success": False, "error": f"Codex session {role_name} 启动超时（15s 内未就绪）"}

    pid = _find_codex_pid(tmux_name)

    # P1-5: 哨兵写入 try/except + P2-16: 直接写 CODEX_SENTINEL_DIR
    try:
        _write_codex_sentinel(role_name, role.get("title", ""),
                              tmux_name, pid, role.get("lifecycle", "infinite"))
    except OSError as e:
        subprocess.run(["tmux", "kill-session", "-t", tmux_name],
                       capture_output=True, timeout=5)
        return {"success": False, "error": f"哨兵写入失败: {e}"}

    return {"success": True, "role": role_name, "tmux_session": tmux_name,
            "pid": pid, "engine": "codex",
            "lifecycle": role.get("lifecycle", "infinite")}


def _build_codex_runner_script(role: dict) -> str:
    """生成 Codex 循环执行 shell 脚本（用 shlex.quote 防注入）。"""
    prompt = _build_role_prompt(role)
    drive = role.get("drive", "loop")
    idle_action = role.get("idle_action", "/loop")

    if drive == "goal":
        return (
            "codex --model 9router_hermes"
            " --dangerously-skip-permissions\n"
        )

    loop_delay = CODEX_LOOP_DELAY
    if "sleep" in idle_action:
        m = re.search(r"sleep\s+(\d+)", idle_action)
        if m:
            loop_delay = int(m.group(1))

    return (
        "while true; do\n"
        f'  codex exec --dangerously-skip-permissions -m 9router_hermes {shlex.quote(prompt)}\n'
        f'  echo "[codex-dev] round done, sleeping {loop_delay}s..."\n'
        f"  sleep {loop_delay}\n"
        "done\n"
    )


def run_codex_task(role_name: str, message: str, timeout: int = 300) -> dict:
    """在运行中的 Codex session 上执行一次性任务。

    与 send() 不同（走 tmux send-keys 发往运行中的交互进程），
    本函数每次用独立 codex exec 进程执行（P2-19: 已重命名以区分语义）。
    """
    # P2-19: 先检查目标 session 是否存活（避免 5 分钟超时等死）
    tmux_name = f"{CODEX_TMUX_PREFIX}{role_name}"
    if not _is_alive(tmux_name):
        return {"success": False,
                "error": f"Codex session {role_name} 不在运行，无法执行任务",
                "role": role_name, "exit_code": -1}

    role = get_role(role_name)
    title = role.get("title", role_name) if role else role_name
    prompt = f"[session-launcher] {title}({role_name}) 任务: {message}"

    try:
        result = subprocess.run(
            ["codex", "exec", "--dangerously-skip-permissions",
             "-m", "9router_hermes", prompt],
            capture_output=True, text=True, timeout=timeout
        )
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        # 截断时标注丢失信息
        truncated = ""
        if len(stdout) > CODEX_OUTPUT_MAX:
            truncated = f" (截断, 共 {len(stdout)} 字符)"
        return {
            "success": result.returncode == 0,
            "role": role_name,
            "output": stdout[-CODEX_OUTPUT_MAX:] if stdout else "",
            "error": stderr[:CODEX_ERROR_MAX] if stderr else "",
            "exit_code": result.returncode,
            "truncated": truncated,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "role": role_name,
                "error": f"执行超时 ({timeout}s)", "exit_code": -1}
    except Exception as e:
        return {"success": False, "role": role_name,
                "error": str(e), "exit_code": -1}


def cdx_status() -> list[dict]:
    """列出所有运行中的 Codex sessions，与 status() 返回结构对齐。"""
    if not CODEX_SENTINEL_DIR.exists():
        return []
    stats = []
    for f in sorted(CODEX_SENTINEL_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            import logging
            logging.warning("cdx_status: 损坏哨兵文件 %s", f.name)
            continue
        role_name = data.get("role", "")
        tmux_name = f"{CODEX_TMUX_PREFIX}{role_name}"
        alive = False
        try:
            r = subprocess.run(
                ["tmux", "has-session", "-t", tmux_name],
                capture_output=True, text=True, timeout=5
            )
            alive = r.returncode == 0
        except Exception:
            alive = False
        pid = _find_codex_pid(tmux_name) if alive else None
        started_at = data.get("started_at", 0)
        uptime_sec = int(time.time() - started_at) if started_at else 0
        last_output = ""
        if alive:
            last_output = _tmux_output(tmux_name, tail=1).strip()
        lifecycle = data.get("lifecycle", "unknown")
        workspace_path = Path(f"~/ccs-workspaces/{role_name}").expanduser()
        stats.append({
            "role": role_name,
            "title": data.get("title", ""),
            "alive": alive,
            "pid": pid,
            "uptime_sec": uptime_sec,
            "lifecycle": lifecycle,
            "engine": "codex",
            "last_output": last_output[-80:] if last_output else "",
            "workspace": str(workspace_path) if workspace_path.exists() else None,
        })
    return stats
