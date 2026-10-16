#!/usr/bin/env python3
"""
core.py — 生命周期编排（从 tmux_ops/role_manager/codex_ops 导入）
"""

# feed 子进程注册表：tmux_name -> Popen。stop() 时 kill+wait 防进程泄漏。
# 注解用字符串形式：模块顶部 subprocess 尚未导入。
_FEED_PROCS: dict[str, "subprocess.Popen"] = {}

from pathlib import Path

__all__ = [
    'start',
    'StartOptions',
    '_start_compat',
    'register_hook',
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

# ── 跨轮次 3 连败门槛（红线 3 对齐）持久化存储 ──
# 用于 send() fallback 关口的跨进程 3 连败门槛，解决 subprocess 隔离导致状态丢失
_SEND_FAIL_STREAK_FILE = Path.home() / ".hermes" / "run" / "send_fail_streak.json"
_MAX_SEND_FAIL_STREAK = 3

def _load_send_fail_streak() -> dict[str, int]:
    """读取持久化的 send 失败连续计数。"""
    try:
        if _SEND_FAIL_STREAK_FILE.exists():
            with open(_SEND_FAIL_STREAK_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _save_send_fail_streak(streak: dict[str, int]) -> None:
    """原子写入持久化的 send 失败连续计数。"""
    try:
        _SEND_FAIL_STREAK_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _SEND_FAIL_STREAK_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(streak, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_SEND_FAIL_STREAK_FILE)
    except Exception:
        pass

def _inc_send_fail_streak(role: str) -> int:
    """增加 role 的 send 失败连续计数，返回当前计数。"""
    streak = _load_send_fail_streak()
    n = streak.get(role, 0) + 1
    streak[role] = n
    _save_send_fail_streak(streak)
    return n

def _reset_send_fail_streak(role: str) -> None:
    """重置 role 的 send 失败连续计数（成功送达时调用，或手动清理）。"""
    streak = _load_send_fail_streak()
    if role in streak:
        del streak[role]
        _save_send_fail_streak(streak)

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

from tmux_ops import (_check_memory_before_launch, _find_claude_pid,
    _is_alive, _tmux_send, _tmux_output, _tmux_kill, _find_codex_pid,
    TMUX_PREFIX)

from routing.roles import load_roles, get_role, _invalidate_role_cache, _forbidden_list, check_wake_permission, _action_templates, _build_role_prompt, _resolve_ws_paths, inject_role_knowledge_into_workspace, _validate_role_name, _ROLE_NAME_RE, SESSION_ROLES_ROOT, _WS_MARKER_START, _WS_MARKER_END, _FORBIDDEN_MAP, _WAKE_PERMISSION_MAP, _CLAUDE_MD

from ops.validators import validate_role

from codex_ops import start_codex_session, run_codex_task, cdx_status

import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dataclasses import dataclass, field

# ── 统一日志初始化（任何模块首次导入 core 时生效）──
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
_log = logging.getLogger("core")

@dataclass
class StartOptions:
    """啟動 CCS 的選項封裝，取代 14 個位置參數。"""
    role: str
    title: str = ""
    detach: bool = False
    init_prompt: str = ""
    partners: list[str] | None = None
    auto_restart: bool = False
    bus_track: str = ""
    bus_timeout: int = 300
    drive: str = ""
    feed_cat: str = ""
    workspace: str = ""
    no_auto_send: bool = False
    instance_id: int = 0
    dry_run: bool = False

def _start_compat(**kwargs) -> dict:
    """向后兼容包装器：將 kwargs 轉為 StartOptions 委派給 start()。"""
    return start(StartOptions(**kwargs))

from ops.sentinel import CcsSentinel, write_sentinel, read_sentinel, delete_sentinel, list_sentinels

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
        # 继承全局权限模板（Bash + bypassPermissions），而非空 allow:[]。
        # ponytail: 空 allow:[] 会让 Write 被禁用 → 角色只能用 shell echo 伪产出、不 commit，
        # 直接破坏"产出真实对接生产"。工程 P0 修复：从全局 settings.json 继承 permissions，
        # 缺省回退到 Bash(*) + bypassPermissions（与全局一致），保证角色能真实写文件+提交。
        gs_perms = cfg.get("permissions", {})
        if gs_perms:
            cfg["permissions"] = gs_perms
        else:
            cfg["permissions"] = {"allow": ["Bash(*)"], "defaultMode": "bypassPermissions"}
    # 继承全局 env（ANTHROPIC_MODEL 等），并注入项目代码路径，
    # 让需要 import pipeflow/session-pipeline 的角色（如 engineer）不再因 ModuleNotFoundError 卡死。
    # ponytail: PYTHONPATH 缺失是 test_exit_schema/s1 等任务 ImportError→死循环的 root cause;
    # 之前手动改 workspace settings 会被本函数重写丢弃，故在此统一注入。
    base_env = cfg.get("env", {})
    base_env.setdefault("PYTHONPATH", str(Path.home() / "session-pipeline" / "src"))
    cfg["env"] = base_env
    settings_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    _log.info("写入 %s mcp_servers=%s perms_rules=%d",
              settings_path, list(cfg["mcpServers"].keys()),
              len(cfg["permissions"].get("allow", [])))

@validate_role
def start(role: StartOptions | str, title: str = "", detach: bool = False,
          init_prompt: str = "", partners: list[str] = None,
          auto_restart: bool = False, bus_track: str = "",
          bus_timeout: int = 300,
          drive: str = "", feed_cat: str = "",
          workspace: str = "",
          no_auto_send: bool = False,
          instance_id: int = 0,
          dry_run: bool = False) -> dict:
    """创建一个 CCS 并写入哨兵。

    自動從 hermes-session-roles 加载角色定义（如存在），
    構建 system prompt 並注入专业知识到 workspace CLAUDE.md。

    参数:
      role: 角色名字符串，或 StartOptions 對象（推薦新代碼使用）。
      instance_id: 实例编号。
        0=單实例/主实例（兼容舊行為：ccs-{role} + ~/ccs-workspaces/{role}/），
        >0=擴展实例（ccs-{role}-{id} + ~/ccs-workspaces/{role}/instances/{id}/）。

    向后兼容: 接受 role:str 或 StartOptions。
    新代码應調用 start(StartOptions(...))，ccs.py/watchdog.py 仍可使用 kwargs。
    """
    from tmux_ops import make_tmux_name
    # Backward compat: accept either StartOptions or positional role string
    if isinstance(role, StartOptions):
        o = role
    else:
        o = StartOptions(
            role=role, title=title, detach=detach, init_prompt=init_prompt,
            partners=partners, auto_restart=auto_restart, bus_track=bus_track,
            bus_timeout=bus_timeout, drive=drive, feed_cat=feed_cat,
            workspace=workspace, no_auto_send=no_auto_send,
            instance_id=instance_id, dry_run=dry_run,
        )
    # P1 Fix 2: workspace 参数无源头校验 — 防止路径遍历和无效 workspace 名
    if o.workspace and not _validate_role_name(o.workspace):
        return {"success": False, "error": f"非法 workspace 名: {o.workspace}"}
    # P1-1 fix: 同步所有本地變數到 o.*，避免 direct StartOptions 調用時本地默認值(0/""/None) 覆蓋實際值
    _locals_from_opts = o  # noqa: F841 — alias for clarity, all reads below use o.*
    tmux_name = make_tmux_name(o.role, o.instance_id)
    o.partners = o.partners or []
    ws_name = o.workspace or o.role

    # 实例感知工作空间路径 (P1-1 fix: 使用 o.* 而非舊的本地變數)
    if o.instance_id and not o.workspace:
        ws_path = Path(f"~/ccs-workspaces/{o.role}/instances/{o.instance_id}").expanduser()
    else:
        ws_path = Path(f"~/ccs-workspaces/{ws_name}").expanduser()

    # 0. ondemand 模式：只写 workspace + 哨兵，不启动 tmux
    if o.drive == "ondemand":
        # instance mode：创建实例级 workspace
        if o.instance_id:
            ws_path.mkdir(parents=True, exist_ok=True)
            _write_instance_claude_md(ws_path, o.role, o.instance_id)
        else:
            workspace_create(o.role)
        role_def = get_role(o.role)
        if role_def:
            inject_role_knowledge_into_workspace(role_def)
        s = CcsSentinel(
            role=o.role, title=o.title or o.role, tmux_session="",
            pid=0, started_at=time.time(), lifecycle="ondemand",
            instance_id=o.instance_id,
        )
        write_sentinel(s)
        print(f"📋 {o.role}[{o.instance_id}] (ondemand) — 已写 workspace + 哨兵")
        return {"success": True, "role": o.role, "instance_id": o.instance_id,
                "mode": "ondemand", "workspace": str(ws_path)}

    # 0.5 dry-run：只打印将执行的动作，不实际执行
    if o.dry_run:
        role_def_dr = get_role(o.role)
        _role_prompt_len = len(_build_role_prompt(role_def_dr)) if role_def_dr else 0
        actions = [
            f"tmux new-session -d -s {tmux_name} -c {ws_path}",
            "注入角色 prompt" + (f" ({_role_prompt_len} 字符)" if _role_prompt_len else ""),
            f"写哨兵 {o.role}[{o.instance_id}]",
        ]
        if o.partners:
            actions.append(f"启动 watchdog 监控 {o.partners}")
        if o.bus_track:
            actions.append(f"启动 tracker 监控 {o.bus_track}")
        print(f"📋 [dry-run] {o.role}[{o.instance_id}] 将执行:")
        for a in actions:
            print(f"  - {a}")
        return {"success": True, "dry_run": True, "role": o.role,
                "instance_id": o.instance_id, "planned_actions": actions}

    # 1. 先检查是否已运行（不消耗内存守卫额度）
    if _is_alive(tmux_name):
        if not o.detach:
            os.execvp("tmux", ["tmux", "attach", "-t", tmux_name])
        print(f"📋 {tmux_name} 已在运行")
        return {"success": True, "role": o.role, "tmux_session": tmux_name,
                "instance_id": o.instance_id}

    # 2. 内存守卫：确认有足够资源启动新 CCS 进程
    err = _check_memory_before_launch(o.role)
    if err:
        return {"success": False, "error": err}

    # 3-4.5 工作空间准备：创建 + 实例注册 + 角色注入 + drive/bus_track 推导 + MCP settings
    role_def, drive, init_prompt, bus_track = _prepare_workspace(
        o.role, ws_path, ws_name, o.instance_id, o.workspace,
        o.drive, o.init_prompt, o.bus_track)

    # 5-6 启动 tmux + claude 并等待就绪
    launch_err = _launch_tmux(tmux_name, ws_path)
    if launch_err:
        return {"success": False, "error": launch_err}

    # 6.5 技能自动发现：skills 已由 deploy_role_skills.sh 部署到
    # ~/.claude/skills/ 目录，Claude Code 启动时自动扫描。无需 /skills load。
    # 见 ~/session-launcher/scripts/deploy_role_skills.sh

    # 7-7.5 注入 prompt + auto_send 批量发送
    _inject_prompt_and_autosend(tmux_name, o.role, role_def, init_prompt, o.no_auto_send)

    # 8. 写哨兵
    pid = _find_claude_pid(tmux_name)
    print(f"📋 pid: {pid}")
    lifecycle = role_def.get("lifecycle", "infinite") if role_def else "infinite"
    s = CcsSentinel(
        role=o.role,
        title=o.title or o.role,
        instance_id=o.instance_id,
        tmux_session=tmux_name,
        pid=pid,
        started_at=time.time(),
        lifecycle=lifecycle,
        partners=o.partners,
        bus_track=bus_track,
        bus_timeout=o.bus_timeout,
        session_id="",
    )
    write_sentinel(s)

    # 9-10 守护线程编排：watchdog/tracker/feed listener/feed 子进程
    _start_monitoring_threads(o.role, tmux_name, o.partners, o.instance_id,
                              o.detach, o.auto_restart, o.feed_cat, drive,
                              bus_track, o.bus_timeout)

    result = {
        "success": True, "role": o.role, "instance_id": o.instance_id,
        "tmux_session": tmux_name,
        "pid": pid, "partners": o.partners, "bus_track": bus_track or None,
    }

    if not o.detach:
        if sys.stdin and sys.stdin.isatty():
            print(f"🎯 进入 {tmux_name} (Ctrl+B D 退出)")
            os.execvp("tmux", ["tmux", "attach", "-t", tmux_name])
        else:
            print(f"🎯 非交互环境，后台运行 (tmux attach -t {tmux_name} 进入)")
    else:
        print(f"🎯 后台运行 (tmux attach -t {tmux_name} 进入)")

    return result


# ── start() 拆分出的阶段函数（保持 print/返回值与原先内联逻辑一致）──

def _prepare_workspace(role: str, ws_path: Path, ws_name: str, instance_id: int,
                       workspace: str, drive: str, init_prompt: str,
                       bus_track: str) -> tuple[dict | None, str, str, str]:
    """步骤 3-4.5：workspace 创建 + 实例注册 + 角色注入 + drive/bus_track 推导 + MCP settings。

    返回更新后的 (role_def, drive, init_prompt, bus_track)。
    """
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
        # bus_track 推导：调用方未显式指定时，取 role_def 第一个 type=bus 且
        # spec.category 非空的 category 作为默认死锁检测分类。
        # 否则 systemd ccs@.service 路径启动的 CCS 死锁检测永不启动 (bus_age=-1)。
        if not bus_track:
            for sig in role_def.get("input_signals", []) or []:
                if sig.get("type") == "bus" and sig.get("spec", {}).get("category"):
                    bus_track = sig["spec"]["category"]
                    print(f"📋 已推导 bus_track={bus_track}（来自 role_def.input_signals）")
                    break
            if not bus_track:
                print(f"⚠ 未能推导 bus_track：{role} 无 type=bus 的 input_signals，死锁检测不启动")
    elif not init_prompt:
        print(f"⚠ 未找到 {role} 角色定义（{SESSION_ROLES_ROOT}），使用空 prompt 启动")

    if not drive:
        drive = "ondemand"

    # 4.5 写入角色专属 settings.json（MCP 隔离）
    _write_mcp_settings(ws_path, role_def)
    return role_def, drive, init_prompt, bus_track


# 9Router 路由 env 单一事实源（被 _router_env 读取；可 patch 用于测试兜底）
_ROUTER_ENV_FILE = Path.home() / ".claude" / "9router-env.sh"


# 匹配 `export ANTHROPIC_BASE_URL="http://..."` 或 `export ANTHROPIC_API_KEY=xxx`
_ROUTER_ENV_RE = re.compile(r'^export\s+(ANTHROPIC_(?:BASE_URL|API_KEY))=("?)(.*?)\2\s*$')


def _router_env() -> dict[str, str]:
    """从 _ROUTER_ENV_FILE 单一事实源读取 9Router 路由 env。

    tmux non-login shell 不执行 .bashrc，故不能靠 source 自动导出；
    systemd ccs@.service 里的 Environment= 亦会被 tmux server 的
    update-environment (默认不含 ANTHROPIC_*) 丢弃。
    因此在 new-session -e 里显式传递，覆盖两条启动路径。

    D3: 纯 Python 解析 export 行，不 `source` 整文件 —— 避免执行文件内任意命令。

    红线: 本函数不可删除——删则所有 headless/systemd 角色 claude
    缺失 ANTHROPIC_* 而卡在 /login（e5d5ccf 回归）。
    """
    env: dict[str, str] = {}
    try:
        for line in _ROUTER_ENV_FILE.read_text(encoding="utf-8").splitlines():
            m = _ROUTER_ENV_RE.match(line.strip())
            if m:
                env[m.group(1)] = m.group(3)
    except Exception as e:
        _log.debug("读取 9Router env 失败: %s", e)
    # 空/纯空白值视为缺失（文件被截断或损坏）→ 回落默认值，
    # 否则 claude 会带空 key 启动、静默卡 /login（原 bug 回归）。
    env = {k: v for k, v in env.items() if v and v.strip()}
    env.setdefault("ANTHROPIC_BASE_URL", "http://localhost:20128/v1")
    env.setdefault("ANTHROPIC_API_KEY", "9router-local")
    return env


def _launch_tmux(tmux_name: str, ws_path: Path) -> str | None:
    """步骤 5-6：启动 tmux + claude 并轮询就绪。失败返回错误描述，成功返回 None。"""
    # 权限由 workspace settings.json permissions.allow 控制
    _PERM_FLAGS = os.environ.get("CLAUDECODE_PERM_FLAGS", "")
    # ponytail: wrapper 让 claude 完成任务后不退出 session，而是循环等待下一个任务。
    # 这解决了"任务完成即退出 session"导致角色无法持续存活 24h 的根因。
    cmd = (
        "while true; do "
        "claude --bare --model 9router_hermes"
        f" {_PERM_FLAGS}"
        " --effort max; "
        "echo '[CCS] Task completed, waiting for next task...' >&2; "
        "sleep 3; "
        "done"
    )
    r_env = _router_env()
    # 确保 PATH 包含 claude 的安装路径 (/home/administrator/.local/bin)：
    # launcher 若从精简 PATH 环境启动（systemd Environment=PATH= / cron），
    # 原样透传会让 pane 内找不到 claude，角色起不来。
    _claude_dir = str(Path.home() / ".local" / "bin")
    _parts = os.environ.get("PATH", "").split(os.pathsep)
    if _claude_dir not in _parts:
        _parts.append(_claude_dir)
    _claude_path = os.pathsep.join(_parts)
    try:
        r = subprocess.run([
            "tmux", "new-session", "-d", "-s", tmux_name,
            "-c", str(ws_path),
            "-e", "FORCE_PERSONA=0",
            "-e", f"ANTHROPIC_BASE_URL={r_env['ANTHROPIC_BASE_URL']}",
            "-e", f"ANTHROPIC_API_KEY={r_env['ANTHROPIC_API_KEY']}",
            "-e", f"PATH={_claude_path}",
            "bash", "-c", f"tmux set -g bracketed-paste off; {cmd}"
        ], capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return "tmux 未安装或不在 PATH 中"
    except subprocess.TimeoutExpired:
        return "tmux 启动超时 (10s)"
    except Exception as e:
        return f"tmux 启动异常: {e}"
    if r.returncode != 0:
        return f"tmux 启动失败: {r.stderr.strip()}"

    # 6. 等 claude 就绪
    for _ in range(_CCS_READY_RETRIES):
        time.sleep(1)
        try:
            out = subprocess.run(
                ["tmux", "capture-pane", "-p", "-t", f"{tmux_name}:0.0", "-S-3"],
                capture_output=True, text=True, timeout=3
            )
            if "❯" in out.stdout or ("claude" in out.stdout.lower() and "ready" in out.stdout.lower()):
                break
        except Exception as e:
            _log.debug("tmux capture-pane 重试 %s: %s", tmux_name, e)
    time.sleep(1)

    # 7. 大声自检：claude 进程必须带路由 env，否则它会静默卡在 /login 而非报错。
    #    e5d5ccf 曾删掉 env 注入 → 所有 headless 角色静默失效，无日志可见。
    _assert_router_env_in_pane(tmux_name)
    return None


def _assert_router_env_in_pane(tmux_name: str) -> None:
    """启动后校验 claude 进程已继承 ANTHROPIC_*；缺失则 loud error（不静默）。

    --bare 模式下 tmux pane_pid 有时就是 claude 主进程本身（bash wrapper 已退出），
    故需同时检查 pane_pid 自身与其直接子进程。
    """
    try:
        pane_pid = subprocess.run(
            ["tmux", "display-message", "-p", "-t", f"{tmux_name}:0.0", "#{pane_pid}"],
            capture_output=True, text=True, timeout=3
        ).stdout.strip()
        if not pane_pid.isdigit():
            return  # pane 尚未就绪，交给上层 ready 轮询，不在此报错
        # pane 自身 + 直接子进程，任一带齐路由 env 即 OK
        candidates = [pane_pid] + subprocess.run(
            ["pgrep", "-P", pane_pid], capture_output=True, text=True, timeout=3
        ).stdout.split()
        for cpid in candidates:
            if not cpid.isdigit():
                continue
            env = subprocess.run(
                ["bash", "-c", f"tr '\\0' '\\n' < /proc/{cpid}/environ"],
                capture_output=True, text=True, timeout=3
            ).stdout
            if "ANTHROPIC_BASE_URL=" in env and "ANTHROPIC_API_KEY=" in env:
                return  # 至少一个进程带齐路由 env，OK
        _log.error(
            "⚠ %s 启动后 claude 进程未继承 ANTHROPIC_BASE_URL/ANTHROPIC_API_KEY "
            "→ 将卡在 /login。检查 _router_env 与 new-session -e 注入。",
            tmux_name
        )
    except Exception as e:
        _log.debug("路由 env 自检跳过 %s: %s", tmux_name, e)


def _auto_send_msgs_from_signals(role_def: dict | None) -> list[str]:
    """从 persona input_signals 生成精确的 auto_send 消息。

    每个 type=bus 的 input_signal 生成一条"检查待处理 {category}"消息，
    比 ccs_config.json 中千篇一律的"加载待处理 task_spec"更贴合角色职责。
    """
    msgs = []
    for sig in (role_def or {}).get("input_signals", []) or []:
        if sig.get("type") != "bus":
            continue
        cat = sig.get("spec", {}).get("category", "")
        if cat:
            msgs.append(f"检查待处理 {cat} 信号")
    return msgs

def _inject_prompt_and_autosend(tmux_name: str, role: str, role_def: dict | None,
                                init_prompt: str, no_auto_send: bool) -> None:
    """步骤 7-7.5：注入 prompt + auto_send 批量发送。"""
    # 7. 注入 prompt（自动构建的 role prompt 或用户提供的 init_prompt）
    if init_prompt:
        _tmux_send(tmux_name, init_prompt)
        time.sleep(2)

    # 7.5 自动发送消息（auto_send）
    if not no_auto_send and is_auto_send_enabled():
        # ondemand 角色：完全跳过 auto_send。被动接收 ccs send / bus 信号唤醒，
        # 启动时不主动轰 scanner 消息（ponytail: 避免无信号时硬塞噪音，也防重复投喂）。
        lifecycle = (role_def or {}).get("lifecycle", "infinite")
        if lifecycle == "ondemand":
            print(f"📋 auto_send: 跳过 ondemand 角色 (被动唤醒，不主动 scanner)")
            return
        # 优先级: 信号生成 > persona auto_send_messages > 配置兜底
        signal_msgs = _auto_send_msgs_from_signals(role_def)
        role_auto_msgs = (role_def or {}).get("auto_send_messages", [])
        config_auto_msgs = get_auto_send_messages(role)
        # 合并且去重（保序）: 信号消息在前, persona/配置消息补充
        seen = set()
        auto_msgs = []
        for m in signal_msgs + role_auto_msgs + config_auto_msgs:
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


def _start_monitoring_threads(role: str, tmux_name: str, partners: list[str],
                              instance_id: int, detach: bool, auto_restart: bool,
                              feed_cat: str, drive: str, bus_track: str,
                              bus_timeout: int) -> None:
    """步骤 9-10：watchdog/tracker/feed listener/feed 子进程编排。"""
    # 9. 启动守护线程 + feed listener（仅 detach 模式）
    if detach:
        for p in partners:
            start_watchdog(role, p, auto_restart=auto_restart, interval=30,
                          instance_id=instance_id)
            print(f"✅ 守护线程: 监控 {p} 存活")

        if feed_cat:
            _start_feed_listener(role, feed_cat)
            print(f"✅ feed listener: 实时监控 {feed_cat} 分类")

    # 9.1 bus 死锁追踪：detach 模式或已指定/推导 bus_track 时启动。
    # systemd 路径 (ccs_monitor.py 长驻监控进程, detach=False) 也生效 → 修复 bus_age=-1。
    # 无伙伴时启动无意义：tracker 仅对协作关系报死锁，solo 角色的 bus 超时
    # 由引擎 _ensure_role_alive 处理（不启动 tracker 可避免热循环空转 + 假死锁噪音）
    if bus_track and partners:
        start_tracker(role, bus_track, timeout_sec=bus_timeout,
                      interval=10, partners=partners,
                      instance_id=instance_id)
        print(f"✅ 轮次追踪: 监控 {bus_track} 死锁 (超时 {bus_timeout}s)")

    # 10. 启动 feed_listener 子进程（将 bus 实时消息注入 tmux）
    if drive not in ("ondemand",):
        # feed 子进程：任何有 tmux 会话的模式都启动（不限于 detach）。
        # 将 bus 实时消息注入到 tmux session 供 Claude 消费。
        _start_feed_subprocess(tmux_name)
        print(f"✅ feed 子进程: 实时消息注入 {tmux_name}")


@validate_role
def stop(role: str, instance_id: int = 0,
         dry_run: bool = False) -> dict:
    """终止 CCS 并清理哨兵。同时回收 watchdog/tracker 守护线程防累积。"""
    from tmux_ops import make_tmux_name
    tmux_name = make_tmux_name(role, instance_id)
    was_alive = _is_alive(tmux_name)
    # 回收守护线程：set_event + join(timeout=5)，幂等
    try:
        from ops.watchdog import stop_watchdog
        from ops.tracker import stop_tracker
        stop_watchdog(role, instance_id)
        stop_tracker(role, instance_id)
    except Exception as e:
        _log.warning("stop 回收守护线程异常: %s", e)
    # 回收 feed_listener 子进程：kill + wait，防每次 stop 泄漏一个 Python 进程
    proc = _FEED_PROCS.pop(tmux_name, None)
    if proc and proc.poll() is None:
        try:
            proc.kill()
            proc.wait(timeout=5)
            _log.info("feed 子进程已回收: %s", tmux_name)
        except Exception as e:
            _log.warning("feed 子进程回收异常 %s: %s", tmux_name, e)
    # _FEED_PROCS 是进程内字典，而 start/stop 是两次独立 CLI 调用 → 上面的 pop
    # 恒为 None。补一条 pid 文件回收路径（feed_listener.py:111 已写该文件），
    # 否则每次 stop 遗留一个 PPID=1 的孤儿（实测 9 listener / 8 会话）。
    _kill_feed_listener_by_pidfile(tmux_name)
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
        # 跨轮次 3 连败门槛（红线 3 对齐）：
        # 单次 tmux has-session 失败可能是 socket 抖动，不立即写 fallback；
        # 连续 3 次失败才写 [ccs_send_fallback] 并升级。
        # 计数键用 tmux_name（含 instance_id）：用裸 role 会让实例 1 的失败
        # 消耗实例 0 的额度，主实例首次抖动即被判"3 连败确认"（实测 2026-10-16）。
        streak = _inc_send_fail_streak(tmux_name)
        if streak < _MAX_SEND_FAIL_STREAK:
            return {"success": False,
                    "error": f"CCS {role}[{instance_id}] 探测失败 {streak}/{_MAX_SEND_FAIL_STREAK}（暂缓 fallback，防抖动误报）"}
        _reset_send_fail_streak(tmux_name)
        _write_bus_notice(role, message, source, "CCS未运行(3连败确认)")
        return {"success": False, "error": f"CCS {role}[{instance_id}] 未运行"}

    # CCS-RULE: send 前校验
    try:
        from routing.roles import validate_ccs_execution
        validate_ccs_execution(role, "send")
    except (subprocess.TimeoutExpired, OSError) as e:
        _log.warning("validate_ccs_execution %s send 降级: %s", role, e)

    # 红线1 代码化：send 前拦截 gateway 重启指令（防 SIGKILL 中断长连接）
    try:
        from gateway_guard import guard_command
        _allowed, _reason = guard_command(message)
        if not _allowed:
            _log.error("红线1 拦截: %s", _reason)
            try:
                subprocess.run(
                    [str(BUS_CLIENT), "write", "security",
                     f"[gateway_guard] 拦截 gateway 重启指令 → {role}",
                     "--evidence", f"{_reason} | source={source}",
                     "--src", "core.send"],
                    capture_output=True, timeout=5)
            except Exception as _be:
                # 拦截 gateway 重启指令是安全事件，bus 写失败也必须留痕
                _log.error("红线1 拦截事件写 bus 失败（拦截本身已生效）: %s", _be)
            return {"success": False, "blocked_by": "redline1_gateway",
                    "reason": _reason}
    except Exception as _ge:  # 校验异常绝不可阻断正常投递
        _log.debug("gateway_guard 校验异常（不阻塞）: %s", _ge)

    # 用户意图提醒：仅人类来源(user/cli)注入，pipeline/daemon 自动化排除防递归
    # 包裹 try/except：提醒注入失败绝不可吞掉原始消息（P1a 回归防护）
    send_message = message
    injected = False
    if source in ("user", "cli"):
        try:
            # record_intent 曾在此 import 但全库无定义（连 WIP 也没有），且
            # user_intent_reminder 自身已声明「意图状态持久化 intent_state.json
            # 已废弃」（见其 read_intent docstring）。同行 import 一个不存在的
            # 名字会让**整行**失败 → inject_reminder/classify_intent 也拿不到 →
            # 整个意图注入路径静默降级（异常被下方 except 吞掉，消息仍投递）。
            from user_intent_reminder import inject_reminder
            send_message = inject_reminder(message, source)
            injected = send_message != message
        except Exception as _e:  # noqa: BLE001 - 注入失败绝不可丢失用户原始消息
            send_message = message  # 降级：原样投递
            _log.warning("user_intent_reminder 注入失败，降级原样投递: %s", _e)
        # 可观测痕迹：每次注入写一条 code_fix bus 证据（REQUIREMENT #6）。
        # 独立 guard：bus I/O 故障不得影响消息投递或被注入失败混淆（resilience P1）
        if injected:
            try:
                subprocess.run(
                    [str(BUS_CLIENT), "write", "code_fix",
                     f"[user_intent_reminder] 已注入意图提醒 → {role}",
                     "--evidence", f"source={source} role={role}",
                     "--src", "core.send"],
                    capture_output=True, timeout=5)
            except Exception as _be:  # noqa: BLE001
                _log.warning("user_intent_reminder bus 证据写失败（不影响投递）: %s", _be)
    delivered = _tmux_send(tmux_name, send_message)
    # ponytail: 自动触发 after_send 钩子（生命周期 hooks 预留）
    _trigger_hooks("after_send", role=role, message=send_message, source=source)
    if not delivered:
        # 投递失败不得报 success：否则调用方（引擎/partner）以为送达，
        # 3 连败 fallback 永不触发，消息静默丢弃（实测 2026-10-16）。
        streak = _inc_send_fail_streak(tmux_name)
        if streak >= _MAX_SEND_FAIL_STREAK:
            _reset_send_fail_streak(tmux_name)
            _write_bus_notice(role, message, source, f"tmux 投递失败({streak}连败确认)")
        return {"success": False,
                "error": f"tmux 投递失败 {role}[{instance_id}] ({streak}/{_MAX_SEND_FAIL_STREAK})"}
    _reset_send_fail_streak(tmux_name)  # 成功送达清零连败计数
    return {"success": True, "sent_chars": len(send_message)}

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
    """启动 feed_listener.py 子进程，实时接收 bus 消息并注入到 tmux 会话。

    记录 Popen 句柄进 _FEED_PROCS，stop() 时按 tmux 名 kill+wait 回收。
    """
    try:
        proc = subprocess.Popen(
            [sys.executable, str(FEED_LISTENER), "--tmux-target", tmux_name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _FEED_PROCS[tmux_name] = proc
        _log.info("feed 子进程已启动: %s -> %s", FEED_LISTENER, tmux_name)
    except (OSError, json.JSONDecodeError) as e:
        _log.warning("feed 子进程启动失败: %s", e)


def _kill_feed_listener_by_pidfile(tmux_name: str) -> None:
    """按 pid 文件回收 feed_listener（跨进程，stop 时调用）。

    feed_listener.py 启动时会写 ~/.hermes/run/feed_listeners/<tmux>.pid 作为
    自身的单实例锁；stop 读同一文件即可回收，即使监听器由另一个进程启动。
    僵尸 pid（进程已死）只清文件，不报错。
    """
    import os as _os
    pid_file = Path.home() / ".hermes" / "run" / "feed_listeners" / f"{tmux_name}.pid"
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return
    try:
        _os.kill(pid, 15)  # SIGTERM：feed_listener 注册了 handler 会自清 pid 文件
        _log.info("feed 监听器已回收: %s (pid=%d)", tmux_name, pid)
    except ProcessLookupError:
        pass  # 进程已死，仅需清残留文件
    except OSError as e:
        _log.warning("feed 监听器回收失败 %s (pid=%d): %s", tmux_name, pid, e)
        return
    pid_file.unlink(missing_ok=True)


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
