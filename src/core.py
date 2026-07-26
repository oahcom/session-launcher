#!/usr/bin/env python3
"""
core.py — 生命周期编排（从 tmux_ops/role_manager/codex_ops 导入）
"""

_start_feed_subprocess = None  # defined at module end

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
    # ponytail: inject_prompt_into_claudemd/clear_injected_prompt 已废弃，若需恢复从 roles.py 导入
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
    'list_roles',
    'get_config_value',
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
            _log.info("[hooks:%s] %s error: %s", event, fn.__name__, e)

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

from routing.roles import load_roles, get_role, _invalidate_role_cache, _forbidden_list, check_wake_permission, _action_templates, _build_role_prompt, _resolve_ws_paths, inject_role_knowledge_into_workspace, _validate_role_name, _ensure_bus_aliases_in_bashrc, _ROLE_NAME_RE, SESSION_ROLES_ROOT, _WS_MARKER_START, _WS_MARKER_END, SESSION_MARKER_START, SESSION_MARKER_END, _FORBIDDEN_MAP, _FORBIDDEN_DISPLAY, _WAKE_PERMISSION_MAP, _CLAUDE_MD

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

# ── 统一日志初始化（任何模块首次导入 core 时生效）──
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
_log = logging.getLogger("core")

from ops.sentinel import CcsSentinel, CcsHealth, write_sentinel, read_sentinel, delete_sentinel, list_sentinels, SENTINEL_DIR

from ops.watchdog import start_watchdog
from ops.tracker import start_tracker
# check_signal available via events.signals if needed

from paths import BUS_CLIENT
FEED_LISTENER = Path(__file__).resolve().parent.parent / "feed_listener.py"
from paths import LIFECYCLE_SENTINEL_DIR as _LIFECYCLE_SENTINEL_DIR

from ops.ccs_config import (
    get_auto_send_messages, is_auto_send_enabled, get_interval_sec,
    get_value as _cfg_get_value,
)

# ── 命名常量 ──
_CCS_READY_RETRIES = 15   # 启动 CCS 后轮询 tmux 就绪的次数
_BUS_MSG_TRUNCATE = 200   # bus 通知消息内容截断长度

# ── 已知 MCP server 注册表（name → {command, args, env}）──
# ponytail: 仅包含基础工具，persona 需要时以此扩展
_MCP_SERVER_REGISTRY: dict[str, dict] = {}

def _write_mcp_settings(ws_path: Path, role_def: dict | None) -> None:
    """写 workspace 专属 .claude/settings.json，MCP 按角色隔离。

    从全局 settings.json 继承 hooks/permissions/model 等配置，
    只覆写 mcpServers：persona 声明了哪些 server 就开哪些，
    未声明时 mcpServers 为空（仅有内置工具 + hooks）。
    """
    claude_dir = ws_path / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path = claude_dir / "settings.json"
    gs = Path.home() / ".claude" / "settings.json"
    cfg = json.loads(gs.read_text(encoding="utf-8")) if gs.exists() else {}
    mcp_names = (role_def or {}).get("mcp_servers", [])
    # 继承全局 mcpServers（如 hex-line 等基础设施），再按角色增补
    base_mcp = cfg.get("mcpServers", {})
    role_mcp = {n: _MCP_SERVER_REGISTRY[n] for n in mcp_names if n in _MCP_SERVER_REGISTRY}
    base_mcp.update(role_mcp)
    cfg["mcpServers"] = base_mcp
    settings_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    _log.info("写入 %s mcp_servers=%s", settings_path, list(cfg["mcpServers"].keys()))

def start(role: str, title: str = "", detach: bool = False,
          init_prompt: str = "", partners: list[str] = None,
          auto_restart: bool = False, bus_track: str = "",
          bus_timeout: int = 300,
          drive: str = "ondemand", feed_cat: str = "",
          workspace: str = "",
          no_auto_send: bool = False) -> dict:
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

    # 4.5 写入角色专属 settings.json（MCP 隔离）
    _write_mcp_settings(ws_path, role_def)

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
    for _ in range(_CCS_READY_RETRIES):
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

    # 6.5 自动加载角色技能（skills→/skill load）
    skill_names = (role_def or {}).get("skills", [])
    if skill_names:
        for sk in skill_names:
            _tmux_send(tmux_name, f"/skill {sk}")
            time.sleep(0.5)
        _log.info("[skills] auto-loaded %d skills for %s", len(skill_names), role)

    # 7. 注入 prompt（自动构建的 role prompt 或用户提供的 init_prompt）
    if init_prompt:
        _tmux_send(tmux_name, init_prompt)
        time.sleep(2)

    # 7.5 自动发送消息（auto_send）
    if not no_auto_send and is_auto_send_enabled():
        # 从 persona JSON 读取角色级 auto_send_messages
        role_auto_msgs = (role_def or {}).get("auto_send_messages", [])
        # 从 ccs_config.json 读取
        config_auto_msgs = get_auto_send_messages(role)
        # 合并且去重: persona 优先, 配置补充
        seen = set()
        auto_msgs = []
        for m in role_auto_msgs + config_auto_msgs:
            if m not in seen:
                seen.add(m)
                auto_msgs.append(m)
        if auto_msgs:
            interval = get_interval_sec()
            print(f"📋 auto_send: {len(auto_msgs)} 条消息 (间隔 {interval}s)")
            for i, msg in enumerate(auto_msgs, 1):
                print(f"  [{i}/{len(auto_msgs)}] {msg}")
                _tmux_send(tmux_name, msg)
                if i < len(auto_msgs):
                    time.sleep(interval)

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

    # 9. 启动守护线程 + feed listener（仅 detach 模式）
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

    # 10. 启动 feed_listener 子进程（将 bus 实时消息注入 tmux）
    # 任何有 tmux 会话的模式都启动，不限于 detach
    if drive not in ("ondemand",):
        _start_feed_subprocess(tmux_name)
        print(f"✅ feed 子进程: 实时消息注入 {tmux_name}")
    elif partners or bus_track:
        print(f"⚠ 非 detach 模式，监控线程不会启动（需要 --no-attach）")

    result = {
        "success": True, "role": role, "tmux_session": tmux_name,
        "pid": pid, "partners": partners or [], "bus_track": bus_track or None,
    }

    if not detach:
        if sys.stdin and sys.stdin.isatty():
            print(f"🎯 进入 {tmux_name} (Ctrl+B D 退出)")
            os.execvp("tmux", ["tmux", "attach", "-t", tmux_name])
        else:
            print(f"🎯 非交互环境，后台运行 (tmux attach -t {tmux_name} 进入)")
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
    """向 CCS 发送消息。失败通知写入 bus [ccs_send_fallback]。"""
    if not _validate_role_name(role):
        _write_bus_notice(role, message, source, "非法角色名")
        return {"success": False, "error": f"非法角色名: {role}"}
    # 跨角色路由拦截（三源验证 + 敏感命令门禁）
    if role != "self" and source != "cli":
        try:
            from routing.gatekeeper import CrossRoleRouter, check_ccs_command_permission
            # 三源验证
            allowed = CrossRoleRouter().intercept(source or "unknown", role, message)
            if not allowed:
                _write_bus_notice(role, message, source, "三源验证拒绝")
                return {"success": False,
                        "error": f"三源验证拒绝: {source}→{role}，消息前缀非可靠来源"}
            # 敏感命令门禁
            cmd_ok, reason = check_ccs_command_permission(source or "unknown", message)
            if not cmd_ok:
                _write_bus_notice(role, message, source, f"敏感操作门禁拒绝: {reason}")
                return {"success": False, "error": f"敏感操作门禁拒绝: {reason}"}
        except Exception as e:
            _write_bus_notice(role, message, source, f"门禁降级: {e}")

    tmux_name = f"{TMUX_PREFIX}{role}"
    if not _is_alive(tmux_name):
        _write_bus_notice(role, message, source, "CCS未运行")
        return {"success": False, "error": f"CCS {role} 未运行"}

    # CCS-RULE: send 前校验
    try:
        from routing.roles import validate_ccs_execution
        validate_ccs_execution(role, "send")
    except Exception:
        pass  # 降级：校验不可用时不阻塞

    _tmux_send(tmux_name, message)
    # ponytail: 自动触发 after_send 钩子（生命周期 hooks 预留）
    _trigger_hooks("after_send", role=role, message=message, source=source)
    return {"success": True, "sent_chars": len(message)}

def _write_bus_notice(role: str, message: str, source: str, reason: str) -> None:
    """降级时写一条 [ccs_send_fallback] 通知到 bus。

    使用 notice 分类而非 architecture，防止级联风暴：
    architecture 被 10 个角色 consume → 反复触发路由 + 创建 workflow → blocker 洪水。
    notice 仅 maintainer/writer/public 3 角色消费，不会放大级联。
    """
    try:
        subprocess.run(
            [str(BUS_CLIENT), "write", "notice",
             f"[ccs_send_fallback] {source}→{role} 失败: {reason}",
             "--evidence", f"message='{message[:_BUS_MSG_TRUNCATE]}' reason={reason}",
             "--src", "core.send"],
            capture_output=True, timeout=5)
    except Exception:
        pass  # 写 bus 失败不阻塞

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


def list_roles() -> list[dict]:
    """列出所有可启动角色, 附带运行状态。"""
    roles = load_roles()
    result = []
    for r in roles:
        name = r.get("name", "?")
        tmux_name = f"{TMUX_PREFIX}{name}"
        alive = _is_alive(tmux_name)
        result.append({
            "name": name,
            "title": r.get("title", ""),
            "category": r.get("category", ""),
            "description": r.get("description", ""),
            "lifecycle": r.get("lifecycle", "infinite"),
            "alive": alive,
            "drive": r.get("drive", ""),
        })
    return result


def get_config_value(key_path: str):
    """查询 ccs_config.json 值（通过点号路径）。"""
    return _cfg_get_value(key_path)


from ops.runner import dashboard, _start_feed_listener

from ops.workspace import register, workspace_create, workspace_list


def _start_feed_subprocess(tmux_name: str) -> None:
    """启动 feed_listener.py 子进程，实时接收 bus 消息并注入到 tmux 会话。"""
    try:
        subprocess.Popen(
            [sys.executable, str(FEED_LISTENER), "--tmux-target", tmux_name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _log.info("feed 子进程已启动: %s -> %s", FEED_LISTENER, tmux_name)
    except Exception as e:
        _log.warning("feed 子进程启动失败: %s", e)

