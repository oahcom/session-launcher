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
    '_write_instance_claude_md',
    '_register_instance_in_workspace',
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

from ops.validators import validate_role

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
# ponytail: 从全局 settings.json 和所有插件 .mcp.json 加载
_MCP_SERVER_REGISTRY: dict[str, dict] = {}


def _load_mcp_registry() -> dict[str, dict]:
    """扫描全局 settings.json + 所有插件 .mcp.json，构建 MCP server 注册表。"""
    registry: dict[str, dict] = {}
    # 1. 全局 settings.json 的 mcpServers
    gs = Path.home() / ".claude" / "settings.json"
    if gs.exists():
        try:
            import json as _json
            gcfg = _json.loads(gs.read_text(encoding="utf-8"))
            registry.update(gcfg.get("mcpServers", {}))
        except Exception as e:
            _log.warning("全局 settings.json MCP 加载失败: %s", e)
    # 2. 扫描所有插件目录下的 .mcp.json（marketplace 注册 + 缓存）
    for base in [Path.home() / ".claude/plugins/marketplaces",
                 Path.home() / ".claude/plugins/cache"]:
        if not base.is_dir():
            continue
        for fpath in sorted(base.rglob(".mcp.json")):
            try:
                data = json.loads(fpath.read_text(encoding="utf-8"))
                # 两种格式：{"mcpServers": {...}} 或直接 {"server_name": {...}}
                entries = data.get("mcpServers", data)
                if not isinstance(entries, dict):
                    continue
                for name, cfg in entries.items():
                    if not isinstance(cfg, dict):
                        continue
                    s = json.dumps(cfg)
                    if "${CLAUDE_PLUGIN_ROOT}" in s:
                        s = s.replace("${CLAUDE_PLUGIN_ROOT}",
                                      str(fpath.parent.resolve()))
                        cfg = json.loads(s)
                    if name not in registry:
                        registry[name] = cfg
            except Exception:
                continue
    return registry


_MCP_SERVER_REGISTRY = _load_mcp_registry()

def _write_mcp_settings(ws_path: Path, role_def: dict | None) -> None:
    """写 workspace 专属 .claude/settings.json，MCP + 权限按角色隔离。

    继承全局 settings.json 的 mcpServers/hooks/model 等配置，
    覆写 mcpServers（按 persona 声明）和 permissions（按角色最小权限）。
    角色无 permissions 字段时写空 allow list → 所有操作需人工确认。
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
    # 权限：角色级 allowlist 替换全局 bypass
    # ponytail: mcp_tools 中的工具级限制尚未映射到 permissions.allow，
    # 因为 Claude Code 无"per-server MCP tool"粒度的权限条目。
    # 需要时添加到对应的 MCP server 层（server 自身的 auth）或 hook 层。
    role_perms = (role_def or {}).get("permissions", {})
    if role_perms:
        cfg["permissions"] = role_perms
    else:
        # 严格默认：无一预授权 → 所有操作弹出确认
        cfg["permissions"] = {"allow": []}
    settings_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    _log.info("写入 %s mcp_servers=%s perms_rules=%d",
              settings_path, list(cfg["mcpServers"].keys()),
              len(cfg["permissions"].get("allow", [])))

@validate_role
def start(role: str, title: str = "", detach: bool = False,
          init_prompt: str = "", partners: list[str] = None,
          auto_restart: bool = False, bus_track: str = "",
          bus_timeout: int = 300,
          drive: str = "", feed_cat: str = "",
          workspace: str = "",
          no_auto_send: bool = False,
          instance_id: int = 0,
          dry_run: bool = False) -> dict:
    """创建一个 CCS 并写入哨兵。

    自动从 hermes-session-roles 加载角色定义（如存在），
    构建 system prompt 并注入专业知识到 workspace CLAUDE.md。

    参数:
      instance_id: 实例编号。
        0=单实例/主实例（兼容旧行为：ccs-{role} + ~/ccs-workspaces/{role}/），
        >0=扩展实例（ccs-{role}-{id} + ~/ccs-workspaces/{role}/instances/{id}/）。
    """
    from tmux_ops import make_tmux_name
    tmux_name = make_tmux_name(role, instance_id)
    partners = partners or []
    ws_name = workspace or role

    # 实例感知工作空间路径
    if instance_id and not workspace:
        ws_path = Path(f"~/ccs-workspaces/{role}/instances/{instance_id}").expanduser()
    else:
        ws_path = Path(f"~/ccs-workspaces/{ws_name}").expanduser()

    # 0. ondemand 模式：只写 workspace + 哨兵，不启动 tmux
    if drive == "ondemand":
        # instance mode：创建实例级 workspace
        if instance_id:
            ws_path.mkdir(parents=True, exist_ok=True)
            _write_instance_claude_md(ws_path, role, instance_id)
        else:
            workspace_create(role)
        role_def = get_role(role)
        if role_def:
            inject_role_knowledge_into_workspace(role_def)
        s = CcsSentinel(
            role=role, title=title or role, tmux_session="",
            pid=0, started_at=time.time(), lifecycle="ondemand",
            instance_id=instance_id,
        )
        write_sentinel(s)
        print(f"📋 {role}[{instance_id}] (ondemand) — 已写 workspace + 哨兵")
        return {"success": True, "role": role, "instance_id": instance_id,
                "mode": "ondemand", "workspace": str(ws_path)}

    # 1. 先检查是否已运行（不消耗内存守卫额度）
    if _is_alive(tmux_name):
        if not detach:
            os.execvp("tmux", ["tmux", "attach", "-t", tmux_name])
        print(f"📋 {tmux_name} 已在运行")
        return {"success": True, "role": role, "tmux_session": tmux_name,
                "instance_id": instance_id}

    # 2. 内存守卫：确认有足够资源启动新 CCS 进程
    err = _check_memory_before_launch(role)
    if err:
        return {"success": False, "error": err}

    # 3. 确保工作空间存在
    if instance_id and not workspace:
        # 扩展实例：在 role workspace 下创建 instances/{id}/ 子目录
        ws_path.mkdir(parents=True, exist_ok=True)
        # 创建实例级 CLAUDE.md（包含 WORKSPACE_SYS marker）
        _write_instance_claude_md(ws_path, role, instance_id)
        # 在角色级 workspace 写入 instance 注册信息
        _register_instance_in_workspace(role, instance_id, ws_path)
        print(f"📁 已创建实例 workspace: {ws_path}")
    else:
        result = workspace_create(ws_name)
        if result.get("success"):
            action = result.get("action", "")
            print(f"📁 {'已创建' if action == 'created' else '已更新'} 工作空间: {ws_path}")

    # 4. 注入角色知识到 workspace
    role_def = get_role(role)
    if role_def:
        inject_role_knowledge_into_workspace(role_def)
        # drive 解析：CLI 未指定时从 persona JSON 读取，JSON 无值则 fallback "ondemand"
        if not drive:
            drive = (role_def.get("drive", "") or "").lower()
        if not init_prompt:
            init_prompt = _build_role_prompt(role_def)
            print(f"📋 已构建角色 prompt ({len(init_prompt)} 字符)")
    elif not init_prompt:
        print(f"⚠ 未找到 {role} 角色定义（{SESSION_ROLES_ROOT}），使用空 prompt 启动")

    if not drive:
        drive = "ondemand"

    # 4.5 写入角色专属 settings.json（MCP 隔离）
    _write_mcp_settings(ws_path, role_def)

    # 5. 启动 tmux + claude — 权限由 workspace settings.json permissions.allow 控制
    _PERM_FLAGS = os.environ.get("CLAUDECODE_PERM_FLAGS", "")
    cmd = (
        "claude --bare --model 9router_hermes"
        f"{_PERM_FLAGS}"
        " --effort max"
    )
    try:
        r = subprocess.run([
            "tmux", "new-session", "-d", "-s", tmux_name,
            "-c", str(ws_path),
            "-e", "FORCE_PERSONA=0",
            "bash", "-c", f"tmux set -g bracketed-paste off; {cmd}"
        ], capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return {"success": False, "error": "tmux 未安装或不在 PATH 中"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "tmux 启动超时 (10s)"}
    except Exception as e:
        return {"success": False, "error": f"tmux 启动异常: {e}"}
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
        except Exception as e:
            _log.debug("tmux capture-pane 重试 %s: %s", tmux_name, e)
    time.sleep(1)

    # 6.5 技能自动发现：skills 已由 deploy_role_skills.sh 部署到
    # ~/.claude/skills/ 目录，Claude Code 启动时自动扫描。无需 /skills load。
    # 见 ~/session-launcher/scripts/deploy_role_skills.sh

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
        instance_id=instance_id,
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
            start_watchdog(role, p, auto_restart=auto_restart, interval=30,
                          instance_id=instance_id)
            print(f"✅ 守护线程: 监控 {p} 存活")

        if bus_track:
            start_tracker(role, bus_track, timeout_sec=bus_timeout,
                          interval=10, partners=partners,
                          instance_id=instance_id)
            print(f"✅ 轮次追踪: 监控 {bus_track} 死锁 (超时 {bus_timeout}s)")

        if feed_cat:
            _start_feed_listener(role, feed_cat)
            print(f"✅ feed listener: 实时监控 {feed_cat} 分类")

    # 10. 启动 feed_listener 子进程（将 bus 实时消息注入 tmux）
    if drive not in ("ondemand",):
        # feed 子进程：任何有 tmux 会话的模式都启动（不限于 detach）。
        # 将 bus 实时消息注入到 tmux session 供 Claude 消费。
        _start_feed_subprocess(tmux_name)
        print(f"✅ feed 子进程: 实时消息注入 {tmux_name}")
    elif partners or bus_track:
        print(f"⚠ 非 detach 模式，监控线程不会启动（需要 --no-attach）")

    result = {
        "success": True, "role": role, "instance_id": instance_id,
        "tmux_session": tmux_name,
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

@validate_role
def stop(role: str, instance_id: int = 0,
         dry_run: bool = False) -> dict:
    """终止 CCS 并清理哨兵。"""
    from tmux_ops import make_tmux_name
    tmux_name = make_tmux_name(role, instance_id)
    was_alive = _is_alive(tmux_name)
    _tmux_kill(tmux_name)
    delete_sentinel(role, instance_id)
    if not was_alive:
        return {"success": False, "error": "CCS 不存在", "role": role}
    return {"success": True, "role": role, "instance_id": instance_id}

def status() -> list[dict]:
    """列出所有 CCS 的运行状态（含死亡的）。"""
    sentinels = list_sentinels()
    statuses = []
    for s in sentinels:
        alive = _is_alive(s.tmux_session)
        ws_path = Path(f"~/ccs-workspaces/{s.role}").expanduser()
        if s.instance_id:
            ws = str(ws_path / "instances" / str(s.instance_id))
        elif ws_path.exists():
            ws = str(ws_path)
        else:
            ws = None
        statuses.append({
            "role": s.role,
            "instance_id": s.instance_id,
            "title": s.title,
            "alive": alive,
            "pid": s.pid,
            "uptime_sec": int(time.time() - s.started_at),
            "lifecycle": s.lifecycle,
            "partner": s.partners[0] if s.partners else None,
            "bus_track": s.bus_track or None,
            "workspace": ws,
            "health": {
                "watchdog_ok": s.health.watchdog_ok,
                "bus_msg_age": round(s.health.last_bus_msg_age, 0),
                "restart_count": s.health.restart_count,
            },
        })
    return statuses


@validate_role
def send(role: str, message: str, source: str = "",
         instance_id: int = 0,
         dry_run: bool = False) -> dict:
    """向 CCS 发送消息。失败通知写入 bus [ccs_send_fallback]。
    instance_id>0 向指定实例发送，0 向主实例发送。
    """
    from tmux_ops import make_tmux_name

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
        except (subprocess.TimeoutExpired, OSError) as e:
            _write_bus_notice(role, message, source, f"门禁降级: {e}")

    tmux_name = make_tmux_name(role, instance_id)
    if not _is_alive(tmux_name):
        _write_bus_notice(role, message, source, "CCS未运行")
        return {"success": False, "error": f"CCS {role}[{instance_id}] 未运行"}

    # CCS-RULE: send 前校验
    try:
        from routing.roles import validate_ccs_execution
        validate_ccs_execution(role, "send")
    except (subprocess.TimeoutExpired, OSError) as e:
        _log.warning("validate_ccs_execution %s send 降级: %s", role, e)

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
    except (OSError, subprocess.TimeoutExpired):
        pass  # 写 bus 失败不阻塞

def output(role: str, tail: int = 20, instance_id: int = 0,
            dry_run: bool = False) -> str:
    """截取 CCS tmux pane 的最新输出。"""
    from tmux_ops import make_tmux_name
    return _tmux_output(make_tmux_name(role, instance_id), tail=tail)

def health_check(role: str = "", instance_id: int = 0,
                 dry_run: bool = False) -> dict:
    """健康检查：返回所有（或指定）CCS 的存活状态。"""
    if role and instance_id:
        sentinels = [s] if (s := read_sentinel(role, instance_id)) else []
    else:
        sentinels = list_sentinels() if not role else (
            [s] if (s := read_sentinel(role, instance_id)) else []
        )
    result = {}
    for s in sentinels:
        alive = _is_alive(s.tmux_session)
        key = f"{s.role}[{s.instance_id}]" if s.instance_id else s.role
        result[key] = {
            "role": s.role,
            "instance_id": s.instance_id,
            "alive": alive,
            "partner": s.partners[0] if s.partners else None,
            "bus_track": s.bus_track or None,
            "last_bus_msg_age": round(s.health.last_bus_msg_age, 0),
            "watchdog_ok": s.health.watchdog_ok,
            "restart_count": s.health.restart_count,
            "uptime_sec": int(time.time() - s.started_at) if alive else 0,
        }
    return result

@validate_role
def force_start_ccs(role_name: str, by_role: str = "",
                    init_prompt: str = "") -> dict:
    """强制启动 CCS（内部独立检查权限）。"""
    if by_role and not _ROLE_NAME_RE.match(by_role):
        return {"success": False,
                "error": f"非法来源角色名: {by_role}"}
    if by_role and not check_wake_permission(by_role, role_name):
        return {"success": False,
                "error": f"权限不足: {by_role} 无权启动 {role_name}"}
    if _is_alive(f"{TMUX_PREFIX}{role_name}"):
        return {"success": False, "error": f"CCS {role_name} 已在运行"}
    return start(role_name, init_prompt=init_prompt)

@validate_role
def wake_ccs(role_name: str, context: str = "",
             by_role: str = "") -> dict:
    """唤醒 CCS：优先检查已有 session，否则启动新 CCS。"""
    if by_role and not _ROLE_NAME_RE.match(by_role):
        return {"success": False,
                "error": f"非法来源角色名: {by_role}"}
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
    """列出所有可启动角色, 附带运行状态（含多 instance 信息）。"""
    roles = load_roles()
    from tmux_ops import make_tmux_name, MAX_INSTANCES
    from ops.sentinel import _list_tmux_sessions
    # 单次 tmux list-sessions 获取所有活跃 session
    active_sessions = _list_tmux_sessions()
    def _has_session(role: str, instance: int = 0) -> bool:
        t = make_tmux_name(role, instance)
        return t in active_sessions

    result = []
    for r in roles:
        name = r.get("name", "?")
        alive = _has_session(name, 0)
        instances = []
        for i in range(1, MAX_INSTANCES + 1):
            if _has_session(name, i):
                instances.append(i)
        result.append({
            "name": name,
            "title": r.get("title", ""),
            "category": r.get("category", ""),
            "description": r.get("description", ""),
            "lifecycle": r.get("lifecycle", "infinite"),
            "alive": alive,
            "instances": instances,
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
    except (OSError, json.JSONDecodeError) as e:
        _log.warning("feed 子进程启动失败: %s", e)


# ── 多 instance 辅助函数 ──

def _write_instance_claude_md(ws_path: Path, role: str, instance_id: int = 0) -> None:
    """为实例 workspace 写入 CLAUDE.md（含 WORKSPACE_SYS marker）。"""
    claude_md = ws_path / "CLAUDE.md"
    if claude_md.exists():
        return
    label = f"{role}[{instance_id}]" if instance_id else role
    content = (
        f"{_WS_MARKER_START}\n"
        f"> 本 workspace 属于 {label}，由 CCS Launcher 自动管理。\n"
        "> 实例级工作空间，与主实例隔离。\n"
        "\n"
        f"## 工作流提示\n"
        "- 每个任务完成后更新 CLAUDE.md\n"
        "- 使用 bus 跨角色通信\n"
        f"- 实例 ID: {instance_id}\n"
        f"{_WS_MARKER_END}\n"
    )
    claude_md.write_text(content, encoding="utf-8")


def _register_instance_in_workspace(role: str, instance_id: int, ws_path: Path) -> None:
    """在角色级 workspace 的 instances/REGISTRY.md 中注册实例信息。"""
    role_ws = Path(f"~/ccs-workspaces/{role}").expanduser()
    reg_path = role_ws / "instances" / "REGISTRY.md"
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    entry = f"- [{instance_id}]({instance_id}/) — started at {datetime.now(timezone.utc).isoformat()}\n"
    if reg_path.exists():
        content = reg_path.read_text()
        if f"[{instance_id}]" not in content:
            reg_path.write_text(content.rstrip() + "\n" + entry)
    else:
        reg_path.write_text(f"# Instance Registry — {role}\n\n{entry}")


# ── dev 环境隔离（DESIGN-dev-environment.md） ──
_ENV_PREFIX = {"dev": "dev:", "staging": "test:", "prod": ""}

def _get_env() -> str:
    """从 CCS_ROLE 解析环境后缀: engineer+dev → dev"""
    role = os.environ.get("CCS_ROLE", "")
    if "+" in role:
        return role.split("+", 1)[1]
    m = re.search(r'\+(dev|staging|prod)\b', role)
    return m.group(1) if m else "prod"

def _env_path(base: str) -> str:
    """环境感知路径: ~/workspace/ → ~/workspace+dev/"""
    env = _get_env()
    return f"{base}+{env}" if env != "prod" else base
