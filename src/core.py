#!/usr/bin/env python3
"""
core.py — 生命周期编排（从 tmux_ops/role_manager/codex_ops 导入）
"""

__all__ = [
    'start',
    'register_hook',
    'set_routing_policy',
    'get_routing_policy',
    'route_target',
    'stop',
    'status',
    'send',
    'output',
    'health_check',
    'register',
    'workspace_create',
    'workspace_list',
    'force_start_ccs',
    'wake_ccs',
    'start_codex_session',
    'run_codex_task',
    'cdx_status',
    'get_role',
    'inject_role_knowledge_into_workspace',
    '_invalidate_role_cache',
    'load_roles',
    'dashboard',
    '_forbidden_list',
    'check_wake_permission',
    '_build_role_prompt',
    'inject_prompt_into_claudemd',
    'clear_injected_prompt',
    'write_lifecycle_sentinel',
    'check_ondemand_timeout',
    'cleanup_stale_sentinels',
    '_is_alive',
    '_find_claude_pid',
    '_tmux_send',
    '_find_codex_pid',
    'TMUX_PREFIX',
    'BUS_CLIENT',
    'FEED_LISTENER',
    '_FORBIDDEN_MAP',
    '_WAKE_PERMISSION_MAP',
    'SESSION_ROLES_ROOT',
    '_CLAUDE_MD',
    '_LIFECYCLE_SENTINEL_DIR',
    '_check_memory_before_launch',
    '_validate_role_name',
    '_resolve_ws_paths',
    '_action_templates',
]





# ── 生命周期钩子（NeMo-Relay 模式）──
_LIFECYCLE_HOOKS: dict[str, list] = {
    "before_start": [], "after_start": [],
    "before_stop": [], "after_stop": [],
    "before_send": [], "after_send": [],
    "on_health_check": [],
}

def register_hook(event: str, fn) -> None:
    if event in _LIFECYCLE_HOOKS:
        _LIFECYCLE_HOOKS[event].append(fn)

def _trigger_hooks(event: str, **kwargs) -> None:
    for fn in _LIFECYCLE_HOOKS.get(event, []):
        try:
            fn(**kwargs)
        except Exception as e:
            print(f"[hooks:{event}] {fn.__name__} error: {e}", flush=True)

# ── 路由策略状态（MCP Gateway 模式）──
# sticky: 同角色消息路由到同一 CCS session
# round-robin: 轮询分发
# priority: 按消息优先级路由
_ROUTING_POLICIES: dict[str, str] = {}  # role -> policy

def set_routing_policy(role: str, policy: str = "sticky") -> None:
    if policy not in ("sticky", "round-robin", "priority"):
        policy = "sticky"
    _ROUTING_POLICIES[role] = policy

def get_routing_policy(role: str) -> str:
    return _ROUTING_POLICIES.get(role, "sticky")

def route_target(role: str, candidates: list[str]) -> str:
    policy = get_routing_policy(role)
    if policy == "sticky":
        return candidates[0] if candidates else role
    elif policy == "round-robin":
        idx = hash(role + str(int(__import__("time").time() / 60))) % max(len(candidates), 1)
        return candidates[idx] if candidates else role
    return candidates[0] if candidates else role
from tmux_ops import (_check_memory_before_launch, _find_claude_pid, _find_claude_session_id,
    _is_alive, _tmux_send, _tmux_output, _tmux_kill, _find_codex_pid,
    _active_codex_session_count, _wait_codex_ready,
    TMUX_PREFIX, CODEX_TMUX_PREFIX,
    CODEX_LOOP_DELAY, CODEX_OUTPUT_MAX, CODEX_ERROR_MAX, CODEX_SESSION_MAX,
    CODEX_READY_RETRIES, CODEX_READY_INTERVAL, _MEM_FREE_MIN_MB, _CCS_LAUNCH_INTERVAL)

from role_manager import load_roles, get_role, _invalidate_role_cache, _forbidden_list, check_wake_permission, _action_templates, _build_role_prompt, _resolve_ws_paths, inject_role_knowledge_into_workspace, _validate_role_name, _ensure_bus_aliases_in_bashrc, inject_prompt_into_claudemd, clear_injected_prompt, _ROLE_NAME_RE, SESSION_ROLES_ROOT, _WS_MARKER_START, _WS_MARKER_END, SESSION_MARKER_START, SESSION_MARKER_END, _FORBIDDEN_MAP, _FORBIDDEN_DISPLAY, _WAKE_PERMISSION_MAP, _CLAUDE_MD

from codex_ops import start_codex_session, _build_codex_runner_script, run_codex_task, cdx_status, _active_codex_session_count, _wait_codex_ready, CODEX_SESSION_MAX, CODEX_TMUX_PREFIX, CODEX_LOOP_DELAY

import json
import logging
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from ops.sentinel import CcsSentinel, CcsHealth, write_sentinel, read_sentinel, delete_sentinel, list_sentinels, SENTINEL_DIR

from ops.watchdog import start_watchdog
from ops.tracker import start_tracker
# check_signal available via events.signals if needed

from paths import BUS_CLIENT
FEED_LISTENER = Path(__file__).resolve().parent.parent / "feed_listener.py"
from paths import LIFECYCLE_SENTINEL_DIR as _LIFECYCLE_SENTINEL_DIR

def start(role: str, title: str = "", detach: bool = False,
          init_prompt: str = "", partners: list[str] = None,
          auto_restart: bool = False, bus_track: str = "",
          bus_timeout: int = 300,
          drive: str = "loop", feed_cat: str = "",
          workspace: str = "") -> dict:
    """创建一个 CCS 并写入哨兵。

    自动从 hermes-session-roles 加载角色定义（如存在），
    构建 system prompt 并注入专业知识到 workspace CLAUDE.md。

    若 workspace 指定，tmux 在该工作空间目录启动（系统级 CCS）；
    若未指定，使用 ~/ccs-workspaces/<role>（兼容旧行为）。
    """
    if not _validate_role_name(role):
        return {"success": False, "error": f"非法角色名: {role}"}
    tmux_name = f"{TMUX_PREFIX}{role}"
    partners = partners or []
    ws_name = workspace or role  # 指定 workspace 则用自定义工作空间

    # 0. ondemand 模式：只写 workspace + 哨兵，不启动 tmux
    if drive == "ondemand":
        workspace_create(role)
        role_def = get_role(role)
        if role_def:
            inject_role_knowledge_into_workspace(role_def)
        s = CcsSentinel(
            role=role, title=title or role, tmux_session="",
            pid=0, started_at=time.time(), lifecycle="ondemand",
        )
        write_sentinel(s)
        print(f"📋 {role} (ondemand) — 已写 workspace + 哨兵")
        return {"success": True, "role": role, "mode": "ondemand",
                "workspace": str(Path(f"~/ccs-workspaces/{role}").expanduser())}

    # 1. 先检查是否已运行（不消耗内存守卫额度）
    if _is_alive(tmux_name):
        if not detach:
            os.execvp("tmux", ["tmux", "attach", "-t", tmux_name])
        print(f"📋 {tmux_name} 已在运行")
        return {"success": True, "role": role, "tmux_session": tmux_name}

    # 2. 内存守卫：确认有足够资源启动新 CCS 进程
    err = _check_memory_before_launch(role)
    if err:
        return {"success": False, "error": err}

    # 3. 确保工作空间存在并更新系统 CLAUDE.md
    ws_path = Path(f"~/ccs-workspaces/{ws_name}").expanduser()
    result = workspace_create(ws_name)
    if result.get("success"):
        action = result.get("action", "")
        print(f"📁 {'已创建' if action == 'created' else '已更新'} 工作空间: {ws_path}")

    # 4. 注入角色知识到 workspace
    role_def = get_role(role)
    if role_def:
        inject_role_knowledge_into_workspace(role_def)
        if not init_prompt:
            init_prompt = _build_role_prompt(role_def)
            print(f"📋 已构建角色 prompt ({len(init_prompt)} 字符)")
    elif not init_prompt:
        print(f"⚠ 未找到 {role} 角色定义（{SESSION_ROLES_ROOT}），使用空 prompt 启动")

    # 5. 启动 tmux + claude
    _PERM_FLAGS = os.environ.get("CLAUDECODE_PERM_FLAGS",
        " --allow-dangerously-skip-permissions --dangerously-skip-permissions --permission-mode bypassPermissions")
    cmd = (
        "claude --bare --model 9router_hermes"
        f"{_PERM_FLAGS}"
        " --effort max"
    )
    r = subprocess.run([
        "tmux", "new-session", "-d", "-s", tmux_name,
        "-c", str(ws_path),
        "-e", "FORCE_PERSONA=0",
        "bash", "-c", f"tmux set -g bracketed-paste off; {cmd}"
    ], capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return {"success": False, "error": f"tmux 启动失败: {r.stderr.strip()}"}

    # 6. 等 claude 就绪
    for _ in range(15):
        time.sleep(1)
        try:
            out = subprocess.run(
                ["tmux", "capture-pane", "-p", "-t", f"{tmux_name}:0.0", "-S-3"],
                capture_output=True, text=True, timeout=3
            )
            if "❯" in out.stdout:
                break
        except Exception:
            pass
    time.sleep(1)

    # 7. 注入 prompt（自动构建的 role prompt 或用户提供的 init_prompt）
    if init_prompt:
        _tmux_send(tmux_name, init_prompt)
        time.sleep(2)

    # 8. 写哨兵
    pid = _find_claude_pid(tmux_name)
    print(f"📋 pid: {pid}")
    s = CcsSentinel(
        role=role,
        title=title or role,
        tmux_session=tmux_name,
        pid=pid,
        started_at=time.time(),
        lifecycle="infinite",
        partners=partners,
        bus_track=bus_track,
        bus_timeout=bus_timeout,
        session_id="",
    )
    write_sentinel(s)

    # 9. 启动守护线程（仅 detach 模式）
    if detach:
        for p in partners:
            start_watchdog(role, p, auto_restart=auto_restart, interval=30)
            print(f"✅ 守护线程: 监控 {p} 存活")

        if bus_track:
            start_tracker(role, bus_track, timeout_sec=bus_timeout,
                          interval=10, partners=partners)
            print(f"✅ 轮次追踪: 监控 {bus_track} 死锁 (超时 {bus_timeout}s)")

        if feed_cat:
            _start_feed_listener(role, feed_cat)
            print(f"✅ feed listener: 实时监控 {feed_cat} 分类")
    elif partners or bus_track:
        print(f"⚠ 非 detach 模式，监控线程不会启动（需要 --no-attach）")

    result = {
        "success": True, "role": role, "tmux_session": tmux_name,
        "pid": pid, "partners": partners or [], "bus_track": bus_track or None,
    }

    if not detach:
        print(f"🎯 进入 {tmux_name} (Ctrl+B D 退出)")
        os.execvp("tmux", ["tmux", "attach", "-t", tmux_name])
    else:
        print(f"🎯 后台运行 (tmux attach -t {tmux_name} 进入)")

    return result

def stop(role: str) -> dict:
    """终止 CCS 并清理哨兵。"""
    if not _validate_role_name(role):
        return {"success": False, "error": f"非法角色名: {role}"}
    tmux_name = f"{TMUX_PREFIX}{role}"
    was_alive = _is_alive(tmux_name)
    _tmux_kill(tmux_name)
    delete_sentinel(role)
    if not was_alive:
        return {"success": False, "error": "CCS 不存在", "role": role}
    return {"success": True, "role": role}

def status() -> list[dict]:
    """列出所有 CCS 的运行状态（含死亡的）。"""
    sentinels = list_sentinels()
    statuses = []
    for s in sentinels:
        alive = _is_alive(s.tmux_session)
        statuses.append({
            "role": s.role,
            "title": s.title,
            "alive": alive,
            "pid": s.pid,
            "uptime_sec": int(time.time() - s.started_at),
            "lifecycle": s.lifecycle,
            "partner": s.partners[0] if s.partners else None,
            "bus_track": s.bus_track or None,
            "workspace": f"~/ccs-workspaces/{s.role}" if Path(
                f"~/ccs-workspaces/{s.role}").expanduser().exists() else None,
            "health": {
                "watchdog_ok": s.health.watchdog_ok,
                "bus_msg_age": round(s.health.last_bus_msg_age, 0),
                "restart_count": s.health.restart_count,
            },
        })
    return statuses

def send(role: str, message: str, source: str = "") -> dict:
    """向 CCS 发送消息。"""
    if not _validate_role_name(role):
        return {"success": False, "error": f"非法角色名: {role}"}
    # 跨角色路由拦截（三源验证：bus/src / DB assigner / sentinel）
    if role != "self" and source != "cli":
        try:
            from routing.router import CrossRoleRouter
            allowed = CrossRoleRouter().intercept(source or "unknown", role, message)
            if not allowed:
                return {"success": False,
                        "error": f"三源验证拒绝: {source}→{role}，消息前缀非可靠来源"}
        except Exception:
            pass  # 降级：DB/总线不可用时放行

    tmux_name = f"{TMUX_PREFIX}{role}"
    if not _is_alive(tmux_name):
        return {"success": False, "error": f"CCS {role} 未运行"}

    # CCS-RULE: send 前校验
    try:
        from role_manager import validate_ccs_execution
        validate_ccs_execution(role, "send")
    except Exception:
        pass  # 降级：校验不可用时不阻塞

    _tmux_send(tmux_name, message)
    # ponytail: 自动触发 after_send 钩子（生命周期 hooks 预留）
    _trigger_hooks("after_send", role=role, message=message, source=source)
    return {"success": True, "sent_chars": len(message)}

def output(role: str, tail: int = 20) -> str:
    """截取 CCS tmux pane 的最新输出。"""
    return _tmux_output(f"{TMUX_PREFIX}{role}", tail=tail)

def health_check(role: str = "") -> dict:
    """健康检查：返回所有（或指定）CCS 的存活状态。"""
    sentinels = list_sentinels() if not role else (
        [s] if (s := read_sentinel(role)) else []
    )
    result = {}
    for s in sentinels:
        alive = _is_alive(s.tmux_session)
        result[s.role] = {
            "alive": alive,
            "partner": s.partners[0] if s.partners else None,
            "bus_track": s.bus_track or None,
            "last_bus_msg_age": round(s.health.last_bus_msg_age, 0),
            "watchdog_ok": s.health.watchdog_ok,
            "restart_count": s.health.restart_count,
            "uptime_sec": int(time.time() - s.started_at) if alive else 0,
        }
    return result

def force_start_ccs(role_name: str, by_role: str = "",
                    init_prompt: str = "") -> dict:
    """强制启动 CCS（内部独立检查权限）。"""
    if not _validate_role_name(role_name):
        return {"success": False, "error": f"非法角色名: {role_name}"}
    if by_role and not check_wake_permission(by_role, role_name):
        return {"success": False,
                "error": f"权限不足: {by_role} 无权启动 {role_name}"}
    if _is_alive(f"{TMUX_PREFIX}{role_name}"):
        return {"success": False, "error": f"CCS {role_name} 已在运行"}
    return start(role_name, init_prompt=init_prompt)

def wake_ccs(role_name: str, context: str = "",
             by_role: str = "") -> dict:
    """唤醒 CCS：优先检查已有 session，否则启动新 CCS。"""
    alive = _is_alive(f"{TMUX_PREFIX}{role_name}")
    if alive:
        if context:
            return send(role_name, context)
        return {"success": True, "action": "already_running", "role": role_name}
    return force_start_ccs(role_name, by_role=by_role)

def write_lifecycle_sentinel(role: dict) -> None:
    """为 ondemand 角色写生命周期哨兵文件。"""
    _LIFECYCLE_SENTINEL_DIR.mkdir(parents=True, exist_ok=True)
    sentinel = _LIFECYCLE_SENTINEL_DIR / f"{role['name']}.active"
    sentinel.write_text(json.dumps({
        "role": role["name"],
        "title": role["title"],
        "lifecycle": role.get("lifecycle", "infinite"),
        "pid": os.getpid(),
        "max_minutes": role.get("max_minutes", 30) if role.get("lifecycle") == "ondemand" else None,
        "started_at": time.time(),
    }))

def check_ondemand_timeout(max_minutes: int = 30) -> list[str]:
    """检查 ondemand 角色是否超时。"""
    if not _LIFECYCLE_SENTINEL_DIR.exists():
        return []
    timed_out = []
    for f in sorted(_LIFECYCLE_SENTINEL_DIR.glob("*.active")):
        try:
            data = json.loads(f.read_text())
            if data.get("lifecycle") != "ondemand":
                continue
            started_at = data.get("started_at")
            if not started_at:
                continue
            if isinstance(started_at, (int, float)):
                started = datetime.fromtimestamp(started_at, tz=timezone.utc)
            else:
                started = datetime.fromisoformat(started_at)
            elapsed = datetime.now(timezone.utc) - started
            max_m = data.get("max_minutes", max_minutes)
            if elapsed > timedelta(minutes=max_m):
                timed_out.append(data["role"])
                f.unlink()
        except (json.JSONDecodeError, OSError, ValueError):
            f.unlink()
    return timed_out

def cleanup_stale_sentinels() -> list[str]:
    """清理孤儿哨兵文件。"""
    if not _LIFECYCLE_SENTINEL_DIR.exists():
        return []
    cleaned = []
    for f in sorted(_LIFECYCLE_SENTINEL_DIR.glob("*.active")):
        try:
            data = json.loads(f.read_text())
            pid = data.get("pid")
            if pid and not Path(f"/proc/{pid}").exists():
                f.unlink()
                cleaned.append(data["role"])
                continue
            lifecycle = data.get("lifecycle")
            if lifecycle == "ondemand":
                started_at = data.get("started_at")
                if started_at:
                    if isinstance(started_at, (int, float)):
                        started = datetime.fromtimestamp(started_at, tz=timezone.utc)
                    else:
                        started = datetime.fromisoformat(started_at)
                    elapsed = datetime.now(timezone.utc) - started
                    max_m = data.get("max_minutes", 30)
                    if elapsed > timedelta(minutes=max_m):
                        f.unlink()
                        cleaned.append(f"{data['role']}(timeout)")
        except (json.JSONDecodeError, OSError, ValueError):
            f.unlink()
    return cleaned


from ops.runner import dashboard, _start_feed_listener

from ops.workspace import register, workspace_create, workspace_list

