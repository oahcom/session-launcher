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
    'resolve_start_instance_id',
    'StartOptions',
    'ENGINE_DOMAIN',
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
    '_engine_with_source',
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
        # 唯一 tmp 名（同 ccls_daemon 的同源修复）：send() 由 pipeline 经 subprocess
        # 并发调用，固定 `.tmp` 路径会让两个进程交错写同一文件 → 落盘可能是
        # 撕裂的 JSON → _load_send_fail_streak 静默返回 {} → 三连败计数丢失，
        # 红线 3 的 3 连败门槛失效（消息被静默丢弃而无告警）。
        fd, tmp_name = tempfile.mkstemp(
            dir=str(_SEND_FAIL_STREAK_FILE.parent),
            prefix=_SEND_FAIL_STREAK_FILE.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(streak, ensure_ascii=False))
            os.replace(tmp_name, _SEND_FAIL_STREAK_FILE)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
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
import shlex
import shutil
import tempfile
import subprocess
import sys
import time
import http.client
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass

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
    workspace: str = ""
    no_auto_send: bool = False
    instance_id: int = 0
    dry_run: bool = False
    # T1A 会话承载引擎（契约 4.2 三层声明的选择层）：
    # None=未覆盖 → 解析 persona.engine（角色默认），再缺省 "opencode"。
    # 字段默认必须是 None 而不是 "ccs"——否则「显式覆盖为 ccs」与「未覆盖」
    # 不可区分，persona.engine 层会被永久遮蔽（P2 persona 默认生效即失效）。
    engine: str | None = None

from ops.sentinel import CcsSentinel, write_sentinel, read_sentinel, delete_sentinel, list_sentinels, SENTINEL_DIR

from ops.watchdog import start_watchdog
from ops.tracker import start_tracker

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
# 系统组件代发消息时的来源标识（wake_ccs 无 by_role 时使用）。
# 不能用空串：空串既不触发 user_intent_reminder 的人类判据，也不进
# gatekeeper 的 _AUTOMATED_SOURCES 白名单，语义落在两者之间的灰区。
_AUTOMATION_SOURCE = "pipeline"

# ── 已知 MCP server 注册表（name → {command, args, env}）──
# ponytail: 从全局 settings.json 和所有插件 .mcp.json 加载
_MCP_SERVER_REGISTRY: dict[str, dict] = {}


def _load_mcp_registry() -> dict[str, dict]:
    """扫描 ~/.claude.json + settings.json + 所有插件 .mcp.json，构建 MCP server 注册表。

    WHY 两个文件都要读：`claude mcp add --scope user` 写 ~/.claude.json，
    而 settings.json 是另一份配置。只读后者会让"已注册"的 server 在此处
    查不到 → 被 _write_mcp_settings 静默过滤成空，角色拿不到工具
    （实测 31/31 角色声明 mcp_servers 却全部拿到空表）。
    settings.json 后读，保持其既有优先语义。
    """
    registry: dict[str, dict] = {}
    # json 在模块顶部已导入（行 129）；此处不再重复导入别名
    # 1. ~/.claude.json（claude mcp add 的落点）
    cj = Path.home() / ".claude.json"
    if cj.exists():
        try:
            ccfg = json.loads(cj.read_text(encoding="utf-8"))
            # `or {}`：key 存在但值为 JSON null 时，.get(k, {}) 返回 None
            # → registry.update(None) 抛 TypeError，整个注册表加载失败。
            registry.update(ccfg.get("mcpServers") or {})
        except Exception as e:
            _log.warning("~/.claude.json MCP 加载失败: %s", e)
    # 2. 全局 settings.json 的 mcpServers（覆盖同名条目）
    gs = Path.home() / ".claude" / "settings.json"
    if gs.exists():
        try:
            gcfg = json.loads(gs.read_text(encoding="utf-8"))
            registry.update(gcfg.get("mcpServers") or {})
        except Exception as e:
            _log.warning("全局 settings.json MCP 加载失败: %s", e)
    # 3. 扫描所有插件目录下的 .mcp.json（marketplace 注册 + 缓存）
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
    # `or []`：persona 写 "mcp_servers": null 时 .get(k, []) 返回 None
    # → 下方推导式迭代 None 抛 TypeError，角色启动失败。
    mcp_names = (role_def or {}).get("mcp_servers") or []
    # 继承全局 mcpServers（如 hex-line 等基础设施），再按角色增补
    base_mcp = cfg.get("mcpServers") or {}
    role_mcp = {n: _MCP_SERVER_REGISTRY[n] for n in mcp_names if n in _MCP_SERVER_REGISTRY}
    base_mcp.update(role_mcp)
    # 声明了却不在注册表 → 静默过滤会让角色拿到空工具集而无从察觉。
    # 实测 31/31 角色声明 hex-line，因注册表为空全部落空（R78 根因 A）。
    _missing = [n for n in mcp_names if n not in _MCP_SERVER_REGISTRY]
    if _missing:
        _log.warning("角色 %s 声明的 MCP server 未注册，将不可用: %s",
                     (role_def or {}).get("name", "?"), _missing)
    cfg["mcpServers"] = base_mcp
    # 权限：角色级 allowlist 替换全局 bypass
    # ponytail: mcp_tools 中的工具级限制尚未映射到 permissions.allow，
    # 因为 Claude Code 无"per-server MCP tool"粒度的权限条目。
    # 需要时添加到对应的 MCP server 层（server 自身的 auth）或 hook 层。
    role_perms = (role_def or {}).get("permissions", {})
    if role_perms:
        cfg["permissions"] = role_perms
        # persona 的 allowlist 只约束「自动放行什么」，不约束「默认模式」。
        # 缺 defaultMode 时 Claude 落回 manual mode → 白名单未覆盖的命令
        # （如 maintainer 调 wf/ccs，其 17 条白名单里都没有）会弹交互确认框，
        # 而 pane 无人点击 → 角色进程活着、pane 内容冻结、零推进。实测
        # 2026-10-19 ccs-maintainer 卡 20 分钟（persona 唯一声明 permissions 的角色，
        # 其余 30 个走下面的缺省分支，均 Bash(*)+bypass 不受影响）。
        # 提示词与本处不在一处维护，无法保证同步 —— 只能保证不再无声卡死。
        if not cfg["permissions"].get("defaultMode"):
            cfg["permissions"]["defaultMode"] = "bypassPermissions"
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


# ── opencode 侧 MCP 落盘 ──
# opencode 不读 .claude/settings.json，只读项目根 opencode.json 的 `mcp` 键
# （实测 v1.19.3：opencode mcp list 对 .claude/settings.json 完全无感）。
# 形态差异：type "local"（非 "stdio"）、command 是数组（非 command+args 两段）。
_OPENCODE_JSON_NAME = "opencode.json"
_OPENCODE_SCHEMA = "https://opencode.ai/config.json"
# opencode 进程的 PATH 只有 /mnt/c/nvm4w/nvm/nodejs，裸名会
# `Executable not found in $PATH` 静默失败；候选表首个命中即用。
_OPENCODE_BIN_DIRS = (
    "/home/administrator/.local/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
)


def _resolve_executable(name: str) -> str | None:
    """命令名 → 绝对路径；解析不到返回 None（调用方必须跳过，不能写裸名）。"""
    if not name or not isinstance(name, str):
        return None
    if "/" in name:
        return name if os.path.isfile(name) and os.access(name, os.X_OK) else None
    for d in _OPENCODE_BIN_DIRS:
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    # 兜底：本进程 PATH（可能含 nvm 等，非 login shell 时不保证）
    return shutil.which(name) or None


def _mcp_cfg_to_opencode(cfg: dict) -> dict | None:
    """claude 形态 MCP 配置 → opencode 形态；不可安全转换返回 None。"""
    if not isinstance(cfg, dict):
        return None
    cmd = cfg.get("command")
    if not cmd or not isinstance(cmd, str):
        return None
    args = cfg.get("args") or []
    if not isinstance(args, list):
        return None
    exe = _resolve_executable(cmd)
    if not exe:
        return None
    out: dict = {
        "type": "local",
        "command": [exe] + [str(a) for a in args],
        "enabled": True,
    }
    env = cfg.get("env") or {}
    if isinstance(env, dict) and env:
        out["environment"] = {str(k): str(v) for k, v in env.items()}
    return out


def _write_opencode_mcp(ws_path: Path, role_def: dict | None) -> None:
    """写 workspace 根 opencode.json 的 `mcp` 键（opencode 侧角色 MCP 隔离）。

    与 _write_mcp_settings 同一份角色声明，但落盘位置/形态不同。
    只覆盖 `mcp` 键，保留文件里其它键（可能是用户手写配置）。
    """
    mcp_names = (role_def or {}).get("mcp_servers") or []
    conv: dict[str, dict] = {}
    for n in mcp_names:
        cfg = _MCP_SERVER_REGISTRY.get(n)
        if cfg is None:
            continue
        oc = _mcp_cfg_to_opencode(cfg)
        if oc is None:
            _log.warning("MCP server %s 无法转换为 opencode 形态（命令不可解析），已跳过", n)
            continue
        conv[n] = oc
    if (role_def or {}).get("permissions"):
        # opencode 的 permission 是 allow/ask/分级对象，与 claude 的
        # permissions.allow 不同构，猜着映射会静默放宽或收紧权限。
        _log.info("opencode 权限模型与 claude 不同构，一期不映射")
    path = ws_path / _OPENCODE_JSON_NAME
    if not conv:
        # 无可转换项：既不改也不建，避免把用户手写的 mcp 覆盖成空表。
        return
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            _log.warning("解析 %s 失败，保留原文件不覆盖: %s", path, e)
            return
        if not isinstance(data, dict):
            _log.warning("%s 顶层不是对象，保留原文件不覆盖", path)
            return
    data.setdefault("$schema", _OPENCODE_SCHEMA)
    data["mcp"] = conv
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    _log.info("写入 %s mcp_servers=%s", path, list(conv.keys()))


# ── opencode 全局 provider 落盘（契约 C4：oc-native 原生协议，经 L7 拦截）──
# 目标是全局 ~/.config/opencode/opencode.jsonc（不是 workspace 的 opencode.json）。
# C4 的 baseURL 值指向第八闸口 L7 拦截层（http://127.0.0.1:7899/v1，透明反代
# api.opencode.ai）：429/transport 判出即换 ok 渠道透明重试，仍属 oc-native
# 原生协议，不经 9router。
# 只合并 `provider.oc-native` 一个键：provider.9router 段与用户手写键原样保留
# （用户裁决 D2：9router 段不许改写、不许删）；JSON 坏了不覆盖只告警。
_OPENCODE_GLOBAL_JSON = Path("~/.config/opencode/opencode.jsonc").expanduser()


def _oc_native_provider() -> dict:
    """契约 C4 的 provider.oc-native 段（字段名/值一字不差）。

    baseURL 值经第八闸口 L7 拦截层（127.0.0.1:7899/v1，透明反代
    api.opencode.ai），429/transport 判出即换 ok 渠道透明重试；仍属
    oc-native 原生协议，不经 9router。
    """
    return {
        "npm": "@ai-sdk/openai-compatible",
        "name": "OpenCode Native",
        "options": {"baseURL": "http://127.0.0.1:7899/v1"},
        "models": {
            "deepseek-v4.1-flash-free": {
                "name": "DSv4.1 Flash Free",
                "limit": {"context": 200000, "output": 32000},
            }
        },
    }


# 钉住的默认模型：原生免费模型（opencode/deepseek-v4.1-flash-free）。
# 流量经配置里内置 provider 的 baseURL 覆盖（→127.0.0.1:7899/inference/v1）
# 进第八闸口 L7；原生 provider 自动带鉴权/协议，无需自建 provider 段
# （2026-11-15 实证；oc-native 曲线方案已退役，值不再被模型选择引用）。
_NATIVE_MODEL = "opencode/deepseek-v4.1-flash-free"
_NATIVE_GATE_BASEURL = "http://127.0.0.1:7899/inference/v1"
_LEGACY_MODEL = "oc-native/deepseek-v4.1-flash-free"   # 旧钉值，写入时迁移


def _write_opencode_provider(path: Path | None = None) -> bool:
    """写全局 opencode.jsonc 的 `provider.oc-native` 键（契约 C4）。

    与 _write_opencode_mcp 同一合并策略：只覆盖 provider.oc-native 一个键，
    保留文件里其它键（provider.9router 段与用户手写配置）。
    JSON 解析失败不覆盖只告警。返回 True=已写入，False=未动文件。
    """
    p = Path(path) if path is not None else _OPENCODE_GLOBAL_JSON
    data: dict = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            _log.warning("解析 %s 失败，保留原文件不覆盖: %s", p, e)
            return False
        if not isinstance(data, dict):
            _log.warning("%s 顶层不是对象，保留原文件不覆盖", p)
            return False
    provider = data.get("provider")
    if provider is None:
        provider = {}
    elif not isinstance(provider, dict):
        _log.warning("%s provider 不是对象，保留原文件不覆盖", p)
        return False
    provider["oc-native"] = _oc_native_provider()
    data["provider"] = provider
    # 内置 provider 覆盖：原生模型流量改道 L7（原生鉴权/协议保持，2026-11-15
    # 实证此为正解）。嵌套 setdefault=用户自己的覆盖不被改写。
    oc_builtin = provider.setdefault("opencode", {})
    if isinstance(oc_builtin, dict):
        opts = oc_builtin.setdefault("options", {})
        if isinstance(opts, dict):
            opts.setdefault("baseURL", _NATIVE_GATE_BASEURL)
    data.setdefault("$schema", _OPENCODE_SCHEMA)
    # 顶层 model：钉原生免费模型（setdefault=用户手写不覆盖）。
    # 旧钉值（oc-native）是本工具自己写入的遗留，迁移为原生 id。
    if data.get("model") == _LEGACY_MODEL:
        data["model"] = _NATIVE_MODEL
    data.setdefault("model", _NATIVE_MODEL)
    # 原子写：并发起多个 opencode 会话时不撕裂文件
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2, ensure_ascii=False))
        os.replace(tmp_name, p)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    _log.info("写入 %s provider.oc-native（9router 与其它键保留）", p)
    return True

def resolve_start_instance_id(role: str, explicit: int | None) -> tuple[int | None, str | None]:
    """解析 start 的 instance_id（不记名角色每次 start 开新实例，活实例永不复用）。

    优先级：
    1. explicit 非 None（--instance-id 显式指定，含 0）→ 原样使用，永不自动分配；
    2. 注册角色（get_role 有 persona）→ 0（主实例语义：已运行则 attach/复用）；
    3. 不记名角色 → next_free_instance 自动分配最小空闲实例号（活实例不合并/
       不复用，死实例编号可回收）；17 槽全满 → (None, 错误文案)。
    返回 (instance_id, error)：error 非 None 时 instance_id 为 None。
    """
    if explicit is not None:
        return explicit, None
    if get_role(role) is not None:
        return 0, None
    # 惰性导入防循环（tmux_ops ↔ sentinel 双向惰性导入）
    from tmux_ops import next_free_instance
    nid = next_free_instance(role)
    if nid is None:
        return None, (f"不记名实例已满（17 个并发：ccs-{role} / ccs-{role}-1..16），"
                      f"请先 ccs stop --instance-id N 释放")
    return nid, None

@validate_role
def start(role: StartOptions | str, title: str = "", detach: bool = False,
          init_prompt: str = "", partners: list[str] = None,
          auto_restart: bool = False, bus_track: str = "",
          bus_timeout: int = 300,
          drive: str = "",
          workspace: str = "",
          no_auto_send: bool = False,
          instance_id: int = 0,
          dry_run: bool = False,
          engine: str | None = None) -> dict:
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
            bus_timeout=bus_timeout, drive=drive,
            workspace=workspace, no_auto_send=no_auto_send,
            instance_id=instance_id, dry_run=dry_run,
            engine=engine,
        )
    # P1 Fix 2: workspace 参数无源头校验 — 防止路径遍历和无效 workspace 名
    if o.workspace and not _validate_role_name(o.workspace):
        return {"success": False, "error": f"非法 workspace 名: {o.workspace}"}

    # ── T1A 引擎解析（契约 4.2 三层声明）──
    # 优先级：StartOptions.engine/--engine（按次覆盖，最高）→ persona.engine（角色默认）→ 缺省 "opencode"。
    # 未知取值显式报错（禁止静默回落缺省）；取值域 = _ENGINE_REGISTRY 表键（契约 4.1）。
    if o.engine and o.engine not in _ENGINE_REGISTRY:
        return {"success": False,
                "error": f"engine 取值非法: {o.engine!r}"
                         f"（取值域 {sorted(_ENGINE_REGISTRY)}）"}
    engine = o.engine or "opencode"
    _role_def_e = get_role(o.role)
    if _role_def_e is None:
        # typo 防呆（替代已移除的注册表白名单拒绝）：未注册名显式可见，
        # 不静默起出会话——`ccs start engneer` 这类 typo 下一行就暴露。
        # dry-run/ondemand/真实启动共用此路径，输出口径一致。
        print(f"不记名角色（未注册，无 persona）：{o.role}")
    if _role_def_e and "engine" in _role_def_e:
        _pe = _role_def_e.get("engine")
        if _pe not in _ENGINE_REGISTRY:
            return {"success": False,
                    "error": f"persona {o.role} 的 engine 取值非法: {_pe!r}"
                             f"（取值域 {sorted(_ENGINE_REGISTRY)}，未知值不得静默回落缺省）"}
        if not o.engine:
            engine = _pe
    # codex 三入口统一口径（正常/dry-run/ondemand）：本期未接入主启动面，一律显式
    # 拒绝并指路孤岛入口。此前只有真正 _launch_tmux 时拒绝，dry-run 返回
    # success:true、ondemand 还会先建 workspace 写哨兵——同一取值三种结论，
    # 调用方（预检脚本/评估面）会据 dry-run 的 success 误判"可以起"。
    if engine == "codex":
        return {"success": False,
                "error": ("engine='codex' 本期未接入主启动面（孤岛入口: ccs.py codex start）；"
                          "注册表行位已保留，接线时只补 inner_builder，不动主流程")}
    # P1-1 fix: 同步所有本地變數到 o.*，避免 direct StartOptions 調用時本地默認值(0/""/None) 覆蓋實際值
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
            # engine 必须透传：persona 声明 ccs + CLI 覆盖 opencode 时，首启哨兵尚未
            # 写入，注入侧读不到事实源会回落 ccs → 写 CLAUDE.md，而 opencode 只认
            # AGENTS.md → 空脑运行（P1 上下文链）。值来自上方已解析的 engine。
            inject_role_knowledge_into_workspace(role_def, engine=engine)
        s = CcsSentinel(
            role=o.role, title=o.title or o.role, tmux_session="",
            pid=0, started_at=time.time(), lifecycle="ondemand",
            instance_id=o.instance_id,
            engine=engine,
        )
        try:
            write_sentinel(s)
        except Exception as e:  # noqa: BLE001 — 受控失败，不得抛 traceback 也不得报成功
            # 哨兵是会话事实载体：写不进去 = 该会话无身份/无判活依据。ondemand 无
            # tmux 可回收，清掉可能残留的半截文件后返回结构化失败（CLI rc≠0）。
            _cleanup_sentinel_best_effort(o.role, o.instance_id)
            _log.error("哨兵写入失败（ondemand）%s[%s]: %s",
                       o.role, o.instance_id, e)
            return {"success": False, "mode": "ondemand", "engine": engine,
                    "error": f"哨兵写入失败（ondemand）{o.role}[{o.instance_id}]: {e}"}
        print(f"📋 {o.role}[{o.instance_id}] (ondemand) — 已写 workspace + 哨兵")
        return {"success": True, "role": o.role, "instance_id": o.instance_id,
                "mode": "ondemand", "workspace": str(ws_path)}

    # 0.5 dry-run：只打印将执行的动作，不实际执行
    if o.dry_run:
        role_def_dr = get_role(o.role)
        _role_prompt_len = len(_build_role_prompt(role_def_dr)) if role_def_dr else 0
        # 引擎配方行：让预检输出能区分 opencode/ccs 承载（验证裸 start=opencode、
        # persona 角色=persona 引擎）。配方串只拼不执行；缺 inner_builder（codex）
        # 显式标 <未接入>，不静默。
        _row_dr = _ENGINE_REGISTRY.get(engine) or {}
        _builder_dr = _row_dr.get("inner_builder")
        actions = [
            f"tmux new-session -d -s {tmux_name} -c {ws_path}",
            f"引擎 {engine} 启动配方: {_builder_dr() if _builder_dr else '<未接入>'}",
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
                "instance_id": o.instance_id, "engine": engine,
                "planned_actions": actions}

    # 1. 先检查是否已运行（不消耗内存守卫额度）
    if _is_alive(tmux_name):
        # 交互式调用必须 attach，否则用户看到的是"命令跑完了但界面没出现"。
        # 判据用 isatty：systemd 环境无 TTY，execvp 会 "open terminal failed"
        # 并以 1 退出（实测 ccs@maintainer 熔断 6 次）。降级路径要出声——
        # 静默跳过 attach 正是"没有进入 claude 界面"这一类投诉的成因。
        if not o.detach:
            if sys.stdin and sys.stdin.isatty():
                os.execvp("tmux", ["tmux", "attach", "-t", tmux_name])
            else:
                # stderr：stdout 是 pipe 时块缓冲，这两行要到进程退出才 flush，
                # journalctl 里会排到「已退出」之后 —— 实测骗过一次读日志的人，
                # 把 12 分钟前的"已在运行"排到崩溃瞬间。
                print(f"🎯 {tmux_name} 已在运行；非交互环境未 attach"
                      f"（进入请执行 tmux attach -t {tmux_name}）",
                      file=sys.stderr)
        print(f"📋 {tmux_name} 已在运行", file=sys.stderr)
        return {"success": True, "role": o.role, "tmux_session": tmux_name,
                "instance_id": o.instance_id}

    # 2. 内存守卫：确认有足够资源启动新 CCS 进程
    err = _check_memory_before_launch(o.role)
    if err:
        return {"success": False, "error": err}

    # 3-4.5 工作空间准备：创建 + 实例注册 + 角色注入 + drive/bus_track 推导 + MCP settings
    role_def, drive, init_prompt, bus_track = _prepare_workspace(
        o.role, ws_path, ws_name, o.instance_id, o.workspace,
        o.drive, o.init_prompt, o.bus_track, engine=engine)

    # 5-6 启动 tmux + 引擎（按注册表分派）并等待就绪
    launch_err = _launch_tmux(tmux_name, ws_path, role=o.role, engine=engine)
    if launch_err:
        return {"success": False, "error": launch_err}

    # 6.5 技能自动发现：skills 已由 deploy_role_skills.sh 部署到
    # ~/.claude/skills/ 目录，Claude Code 启动时自动扫描。无需 /skills load。
    # 见 ~/session-launcher/scripts/deploy_role_skills.sh

    # 7-7.5 注入 prompt + auto_send 批量发送
    _inject_prompt_and_autosend(tmux_name, o.role, role_def, init_prompt,
                                o.no_auto_send, engine=engine)

    # 8. 写哨兵（engine=启动编排收尾落盘的身份事实源，契约 4.2）
    _pid_finder = _ENGINE_REGISTRY[engine].get("pid_finder")
    pid = _pid_finder(tmux_name) if _pid_finder else None
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
        engine=engine,
    )
    try:
        write_sentinel(s)
    except Exception as e:  # noqa: BLE001 — 哨兵写失败必须受控，不得留下无身份残活会话
        # 因果：tmux 已在 _launch_tmux 拉起、引擎在跑，但哨兵没落盘 → 监控线程
        # 尚未启动（第 9 步在其后）、feed/watchdog 无从索引；更糟的是
        # _engine_with_source 的前缀兜底会把 ccs- 前缀的 opencode 会话标成 ccs
        # （tmux-prefix-fallback），评估面据此判成 claude 转录缺失 → 假阴性。
        # 故按受控失败处理：best-effort 回收刚拉起的会话（不留残活），清半截
        # 哨兵文件，返回结构化失败让 CLI rc≠0。
        _cleanup_sentinel_best_effort(o.role, o.instance_id)
        _log.error("哨兵写入失败 %s[%s]（tmux=%s）→ 回收会话防残活: %s",
                   o.role, o.instance_id, tmux_name, e)
        _kill_tmux_best_effort(tmux_name)
        return {"success": False, "role": o.role, "instance_id": o.instance_id,
                "tmux_session": tmux_name, "engine": engine,
                "error": (f"哨兵写入失败 {o.role}[{o.instance_id}]: {e}"
                          f"（已回收 tmux {tmux_name}，不留无哨兵残活会话）")}

    # 9-10 守护线程编排：watchdog/tracker/feed listener/feed 子进程
    _start_monitoring_threads(o.role, tmux_name, o.partners, o.instance_id,
                              o.detach, o.auto_restart, drive,
                              bus_track, o.bus_timeout)

    result = {
        "success": True, "role": o.role, "instance_id": o.instance_id,
        "tmux_session": tmux_name,
        "pid": pid, "partners": o.partners, "bus_track": bus_track or None,
        "engine": engine,
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

def _cleanup_sentinel_best_effort(role: str, instance_id: int = 0) -> None:
    """哨兵写入失败后的 best-effort 清理：删半截 .json 与 _atomic_write 的 .tmp。

    清理失败只 warning，不掩盖原始写入错误（否则调用方拿到的 error 文案会变成
    "清理失败"，哨兵写失败这一根因被替换掉）。
    """
    key = f"{role}-{instance_id}" if instance_id else role
    try:
        delete_sentinel(role, instance_id)
    except Exception as e:  # noqa: BLE001
        _log.warning("清理哨兵文件失败 %s: %s", key, e)
    try:
        # 延迟取 SENTINEL_DIR：与 write_sentinel 读同一模块全局（含测试/注入时的
        # 目录替换），避免 core 模块级导入的旧值指向另一个目录。
        from ops.sentinel import SENTINEL_DIR as _SD
        (_SD / f"{key}.tmp").unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001
        _log.warning("清理哨兵临时文件失败 %s: %s", key, e)


def _kill_tmux_best_effort(tmux_name: str) -> None:
    """受控失败时回收刚拉起的 tmux 会话（best-effort）。

    为什么必须杀：引擎已在跑但会话没有哨兵/监控，是最坏的残活形态——占用资源、
    挡住后续 start（_is_alive 判"已在运行"直接返回 success），且前缀兜底会把
    它误标成 ccs。杀失败只 warning：原始失败原因必须保留在 error 文案里。
    """
    if not tmux_name:
        return
    try:
        _tmux_kill(tmux_name)
    except Exception as e:  # noqa: BLE001
        _log.warning("回收 tmux 会话失败 %s: %s", tmux_name, e)


def _prepare_workspace(role: str, ws_path: Path, ws_name: str, instance_id: int,
                       workspace: str, drive: str, init_prompt: str,
                       bus_track: str, engine: str | None = None) -> tuple[dict | None, str, str, str]:
    """步骤 3-4.5：workspace 创建 + 实例注册 + 角色注入 + drive/bus_track 推导 + MCP settings。

    engine=本次已解析出的会话承载引擎（契约 4.2 三层声明结果），必须透传给注入：
    首启时哨兵还没落盘，注入侧无法从事实源取 engine，只能回落 persona/ccs ——
    persona=ccs + --engine opencode 的组合会在首启写成 CLAUDE.md（opencode 不读）。
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
        inject_role_knowledge_into_workspace(role_def, engine=engine)
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
    # opencode 不读 .claude/settings.json，需另写项目根 opencode.json。
    if engine == "opencode":
        _write_opencode_mcp(ws_path, role_def)
        # 全局 provider 段（契约 C4）：oc-native 原生直连；9router 段原样保留（D2）。
        _write_opencode_provider()
    return role_def, drive, init_prompt, bus_track


# 9Router 路由 env 单一事实源（被 _router_env 读取；可 patch 用于测试兜底）
_ROUTER_ENV_FILE = Path.home() / ".claude" / "9router-env.sh"


# 匹配 `export ANTHROPIC_BASE_URL="http://..."` 或 `export ANTHROPIC_API_KEY=xxx`
_ROUTER_ENV_RE = re.compile(r'^export\s+(ANTHROPIC_(?:BASE_URL|API_KEY))=("?)(.*?)\2\s*$')


# 9Router 实际监听端口：20127 为当前主实例（优先），20128 为托盘/旧实例（兜底）。
_ROUTER_PORTS = ("20127", "20128")


def _router_alive(base_url: str) -> bool:
    """探测 9Router 是否在跑（HTTP 层，探源站根）。

    不探 `/v1/models`：实测 10.0s / 4.2s（要加载模型列表），1s 超时下恒 False
    （2026-10-26 三次实测 10.03s / 4.16s / 0.0006s）→ `_live_router_base_url`
    每次都走"全部不可达"兜底，主端口一挂就把角色指向死端口（claude 卡 /login）。
    根路径 1.4ms（跟随 307 到 /login 也只要 9ms），任何 HTTP 响应码都算"有人在说
    HTTP"——纯 TCP connect 会把任意监听者当成 9Router。

    必须吞 `http.client.HTTPException`：端口被非 HTTP 服务占用时（实测 20197 跑
    SSH banner），`h.getresponse()` 直接抛 BadStatusLine，它不继承 OSError，
    漏掉会一路穿透到 launch()（_router_env 不在 try 内），本该 False 兜底却崩启动。
    """
    try:
        _p = urlsplit(base_url)
        urllib.request.urlopen(f"{_p.scheme}://{_p.netloc}/", timeout=1.5).close()
        return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        return False
    except Exception as e:  # 探针契约：永不抛。白名单外的类型（MemoryError 在 swap 吃紧时现实可触发）
        # 会一路穿透 _live_router_base_url → _router_env → start()（三者都无 try），
        # 把"启动失败"变成 traceback。仍要 warning+exc_info，别把代码 bug 静默成"路由器挂了"。
        _log.warning("9Router 探测异常 %s: %s", base_url, e, exc_info=True)
        return False


def _live_router_base_url() -> str:
    """按 20127 → 20128 优先级探测存活的 9Router 并返回其 Base URL。

    两个端口可能同时在跑（托盘实例 + 主实例），也可能只剩一个。
    必须避免把角色指向已死端口 —— 那会让 claude 静默卡在 /login。
    全部不可达时回落首选端口，让 claude 用明确报错暴露问题。
    """
    for port in _ROUTER_PORTS:
        url = f"http://localhost:{port}/v1"
        if _router_alive(url):
            return url
        _log.debug("9Router %s 不可达，尝试下一优先级", url)
    return f"http://localhost:{_ROUTER_PORTS[0]}/v1"


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
    # BASE_URL 由实时探测决定 —— 不采信 env 文件里的值。
    # 文件可能残留已死端口，指向它会让角色启动后静默卡 /login。
    env["ANTHROPIC_BASE_URL"] = _live_router_base_url()
    env.setdefault("ANTHROPIC_API_KEY", "9router-local")
    return env

# ── T1A 会话承载引擎注册表（契约 4.1/4.3）──
# 取值域 = 表键 {"ccs","opencode","codex"}（契约 4.1；ccs=claude 承载引擎历史名）。
# 启动分派=查表，不是分叉树：每引擎一行注册数据（启动配方/进程名/就绪判据/env
# 策略/哨兵 pid 来源），新增引擎=加一行，不再挖孤岛。opencode 与 claude 的启动
# 不对称（9router env 注入与否）是表数据差异而非逻辑分叉——主流程只有一条。
# codex 本期不接（P6 孤岛不动）：行位保留、inner_builder=None，启动面显式拒绝。


def _ccs_inner_cmd() -> str:
    """ccs（claude）行启动配方 —— 现行 wrapper 的 inner 原样搬入（P4 零回归主体）。

    权限：workspace settings.json 的 permissions.defaultMode=bypassPermissions 在
    目录未受信/设置未被读取时不生效（实测 2026-10-26 02:51 那批 pane：settings.json
    里写着 bypassPermissions，pane 却卡在 "Do you want to create pm_contract_recheck.py?"
    上等人工按 1；同期 24h 真实角色产出只有 7 条）。argv 里显式给同义 flag，
    不依赖 settings 解析路径。CLAUDECODE_PERM_FLAGS 仍可覆盖。
    空串要当作"未设置"：tests/test_mcp_settings.py 里 setdefault(...,"") 会把它设成空，
    直接 .get(k, default) 会拿到 "" 从而静默丢掉 flag（审查实测：全量 pytest 同进程内
    新 flag 根本不存在，而单跑该文件却"通过"）。
    不带 --bare：bare 模式只暴露 Bash/Edit/Read 三个工具（实测 init 事件
    "tools":["Bash","Edit","Read"]），Task/Agent、WebSearch、Write、Skill 全部没有
    ——而 prompt 一直在要求角色联网检索、写产出、按任务开子代理，这些在 bare 下
    是物理不可能，角色只能退化成 bash heredoc + curl。
    CLAUDE.md 走 auto-discovery（cwd 就是 ws_path）：严格探针实测（--tools "" 禁工具，
    排除模型自读文件）能逐字复述文件中段小节，与 --append-system-prompt-file 等价，
    所以两个参数都不给——同时给会把同一份 prompt（实测 15–40KB/角色，另加
    ~/.claude/CLAUDE.md 18KB）注入两次。
    MCP 与 hooks 不受这里影响：--tools 只裁「内置工具集」（help 原话），
    用户级 ~/.claude.json 里的 hex-line/hex-graph 照样随每个 pane 起 2 个 server
    （实测 15 个新 pane 带 30 个 MCP 子进程；带 --bare 的旧 pane 同样有）——
    角色大量在用 mcp__hex-line__read_file（近 6h 1712 次），所以不能加
    --strict-mcp-config，那条会让角色的文件读写习惯全线失效。
    hooks 同理：21 个 workspace 的 settings.json 都继承 hex-line 的
    SessionStart/PreToolUse/PostToolUse（node hook.mjs，5s 超时）。
    注意：这串 argv 在 tmux session 创建时就烤进 pane 的 bash -c 里，改了参数只有重建
    session（ccs start）才生效——只杀 claude 会让 wrapper 用旧 argv 重拉（实测 reviewer）。
    --effort high 而非 max：9router 回退链含 oc/big-pickle 节点，该节点
    只接受 none/low/medium/high，收到 max 直接返回 400 Param Incorrect。
    实测（2026-10-16）同一参数下 big-pickle+max→400，+high→200。
    回退落点与角色无关，故在源头统一降档，而非按角色配置。
    """
    _PERM_FLAGS = (os.environ.get("CLAUDECODE_PERM_FLAGS")
                   or "--permission-mode bypassPermissions")
    _TOOLSET = "Bash,Edit,Read,Write,Agent,WebSearch,WebFetch,Skill"
    return ("claude --model 9router_hermes"
            f" --tools {shlex.quote(_TOOLSET)}"
            f" {_PERM_FLAGS}"
            " --effort high")


def _opencode_gate_port() -> int | None:
    """opencode 闸口 HTTP 代理端口：env OPENCODE_GATE_PORT → config opencode_gate.proxy_port
    → GET /slots 动态发现（kind=="opencode" 的 port；端口随 slot 编号顺延，不写死）。

    都拿不到/非法返回 None —— 调用方必须显式降级为不注入并告警，
    不许静默指向不存在的端口。
    """
    raw = os.environ.get("OPENCODE_GATE_PORT") or _cfg_get_value("opencode_gate.proxy_port")
    if raw is not None and str(raw).strip():
        try:
            port = int(raw)
            if 1 <= port <= 65535:
                return port
            _log.warning("opencode 闸口端口越界: %r", raw)
        except (TypeError, ValueError):
            _log.warning("opencode 闸口端口非法: %r", raw)
    try:
        from ops.gate_port import discover_gate_port
        port = discover_gate_port()
        if port:
            return port
    except Exception as e:  # noqa: BLE001 — 发现失败走显式降级
        _log.debug("opencode 闸口端口动态发现失败: %s", e)
    return None


def _opencode_inner_cmd() -> str:
    """opencode 行启动配方（契约 4.3/D3）：工作区 cwd 直接拉 TUI-in-tmux。

    不注入 ANTHROPIC_*：opencode.jsonc 已自配 provider。钉
    --model opencode/deepseek-v4.1-flash-free（原生免费模型）——流量经
    配置里内置 provider 的 baseURL 覆盖（http://127.0.0.1:7899/inference/v1）
    进第八闸口 L7 拦截层，429 判出换 ok 透明重试（2026-11-15 实证：原生
    provider/模型直用，oc-native 曲线方案已退役，原生自动带鉴权）。
    二进制不在 PATH，用绝对路径（~/.opencode/bin/opencode）。
    专属闸口出口：HTTPS_PROXY/HTTP_PROXY 指向 opencode 闸口端口、NO_PROXY
    放行本地回环（9router 在 127.0.0.1，不得被闸口代理劫持）。端口可配置
    （env OPENCODE_GATE_PORT → config opencode_gate.proxy_port）；读不到时
    显式降级为不注入并告警（不许静默指向不存在的端口）——模型钉住不受
    降级影响（L7 是本地回环，无需代理 env）。
    """
    exe = str(Path.home() / ".opencode" / "bin" / "opencode")
    base = f"{exe} --model {shlex.quote(_NATIVE_MODEL)}"
    port = _opencode_gate_port()
    if not port:
        _log.warning("opencode 闸口端口未配置（env OPENCODE_GATE_PORT / "
                     "config opencode_gate.proxy_port 均无），显式降级：不注入 "
                     "HTTPS_PROXY/HTTP_PROXY")
        return base
    proxy = f"http://127.0.0.1:{port}"
    return (f"HTTPS_PROXY={shlex.quote(proxy)} HTTP_PROXY={shlex.quote(proxy)} "
            f"NO_PROXY=127.0.0.1,localhost {base}")


_ENGINE_REGISTRY: dict[str, dict] = {
    "ccs": {
        "process_name": "claude",
        "inner_builder": _ccs_inner_cmd,
        "inject_router_env": True,
        "assert_router_env": True,
        "pid_finder": _find_claude_pid,
        "ready_tail": 3,
        "ready_any": ("❯",),
        "ready_all": (("claude", "ready"),),
        "fail_re": r"unknown (option|argument)|Usage:\s+claude|error:",
    },
    "opencode": {
        "process_name": "opencode",
        "inner_builder": _opencode_inner_cmd,
        "inject_router_env": False,
        "assert_router_env": False,
        "pid_finder": lambda t: _find_engine_pid_by_comm(t, "opencode"),
        "ready_tail": 20,
        # 就绪判据（真机标定 2026-11-13，opencode v1.19.3）：输入占位提示
        # 「Ask anything」出现 = TUI 就绪可投递（标定帧存 evidence/t1a-core/）。
        "ready_any": ("Ask anything",),
        "ready_all": (),
        "fail_re": r"unknown (option|argument)|Usage:\s+opencode|error:",
    },
    "codex": {
        # 本期不接（孤岛不动，P6）：主启动面显式拒绝，走 ccs.py codex start 孤岛入口。
        # 行位保留=注册表结构容纳第三引擎；接线时只补 inner_builder，不动主流程。
        "process_name": "codex",
        "inner_builder": None,
        "inject_router_env": False,
        "assert_router_env": False,
        "pid_finder": None,
        "ready_tail": 3,
        "ready_any": (),
        "ready_all": (),
        "fail_re": r"unknown (option|argument)|error:",
    },
}
ENGINE_DOMAIN = tuple(_ENGINE_REGISTRY)  # 契约 4.1 取值域（=表键；不把数量写死成两个）


def _wrap_relaunch(inner_cmd: str) -> str:
    """共享 while-true 重拉骨架（契约 4.3：两分支共用，配方差异全在 inner 数据）。

    退出即重拉要有退避：免费车道耗尽/参数错误时引擎会 rc≠0 立刻退出，
    固定 sleep 3 等于每 3s 打一次上游（实测 10-30 18:23 起 6 个角色反复空转）。
    存活 <60s 视为快速失败，连续次数 ×10s，封顶 120s；长跑后清零。
    $SECONDS 是 shell 启动至今，不是本次引擎的存活时长 —— 必须减 t0。
    只对「失败且快速退出」退避：成功退出（rc=0）不惩罚正常任务。
    """
    return (
        "n=0; while true; do t0=$SECONDS; "
        f"{inner_cmd}; rc=$?; "
        "if [ $((SECONDS-t0)) -lt 60 ] && [ $rc -ne 0 ]; then n=$((n+1)); else n=0; fi; "
        "s=$((n*10)); [ $s -gt 120 ] && s=120; [ $s -lt 3 ] && s=3; "
        "echo \"[CCS] Task completed, waiting ${s}s...\" >&2; "
        "sleep $s; "
        "done"
    )


def _find_engine_pid_by_comm(tmux_name: str, comm: str) -> int | None:
    """按 comm 在 pane 进程树中找引擎主进程 pid（非 claude 引擎的哨兵 pid 来源）。

    claude 路径仍走 tmux_ops._find_claude_pid（零改动）；本函数只服务注册表
    其他引擎行。pane_pid 是 wrapper bash（while-true 常驻），不能当引擎 pid 用
    ——判活双检第一检要求真实引擎进程（「pane 活 ≠ agent 活」）。
    """
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", f"{tmux_name}:0.0", "#{pane_pid}"],
            capture_output=True, text=True, timeout=3)
        pane_pid = (r.stdout or "").strip()
        if not pane_pid.isdigit():
            return None
        out = subprocess.run(["pgrep", "-P", pane_pid, "-x", comm],
                             capture_output=True, text=True, timeout=3).stdout.split()
        if out and out[0].isdigit():
            return int(out[0])
    except Exception as e:
        _log.debug("_find_engine_pid_by_comm %s %s: %s", tmux_name, comm, e)
    return None


def _engine_pid_and_source(tmux_name: str, engine: str) -> tuple[int | None, str]:
    """返回 (引擎进程 pid, pid_source)；engine=真实引擎进程，pane-fallback=兜底。

    status/status-role 的 pid 必须指向承载引擎的进程：pane_pid 是 wrapper bash
    （while-true 常驻），把 wrapper 当引擎 pid 有两个后果——① 消费者 kill 该
    pid 只会杀掉 wrapper，数秒后被重拉（"杀不死"）；② pm smoke 断言 pid 进程名
    时会拿到 bash 而非 opencode/claude，无法证明会话真由该引擎承载。

    注册表 pid_finder 是唯一取数入口（查表不写字面量）；ccs 行保留既有
    _find_claude_pid 兜底（命中 claude 才叫 engine，pane pid 兜底标 pane-fallback）。
    """
    if not tmux_name:
        return None, "sentinel"
    row = _ENGINE_REGISTRY.get(engine) or {}
    comm = row.get("process_name")
    if comm:
        pid = _find_engine_pid_by_comm(tmux_name, comm)
        if pid:
            return pid, "engine"
    finder = row.get("pid_finder")
    if finder:
        try:
            pid = finder(tmux_name)
        except Exception as e:  # noqa: BLE001 — 取 pid 失败不得打断 status
            _log.debug("pid_finder %s %s 异常: %s", tmux_name, engine, e)
            pid = None
        if pid:
            return pid, "pane-fallback"
    return None, "pane-fallback"


def _ready_hit(last: str, row: dict) -> bool:
    """就绪判据命中判定（判据集中注册表参数化，防散点 if 漂移）。

    ready_any: 任一子串命中即就绪；ready_all: 每组全含才就绪（legacy claude 第二支）。
    """
    if any(x in last for x in row["ready_any"]):
        return True
    if row["ready_all"] and all(all(w in last.lower() for w in grp)
                                for grp in row["ready_all"]):
        return True
    return False


def _launch_tmux(tmux_name: str, ws_path: Path, role: str = "",
                 engine: str = "ccs") -> str | None:
    """步骤 5-6：按引擎注册表分派启动配方，拉起 tmux + 引擎并轮询就绪。

    失败返回错误描述，成功返回 None。claude 路径与改造前逐字等价（P4 零回归）。
    """
    row = _ENGINE_REGISTRY.get(engine)
    if row is None:
        return f"未知 engine: {engine!r}（取值域 {sorted(_ENGINE_REGISTRY)}）"
    if row.get("inner_builder") is None:
        return (f"engine={engine!r} 本期未接入主启动面（孤岛入口: ccs.py codex start）；"
                f"注册表行位已保留，接线时只补 inner_builder，不动主流程")
    cmd = _wrap_relaunch(row["inner_builder"]())
    # 确保 PATH 包含 claude 的安装路径 (/home/administrator/.local/bin)：
    # launcher 若从精简 PATH 环境启动（systemd Environment=PATH= / cron），
    # 原样透传会让 pane 内找不到 claude，角色起不来。
    #
    # 实测（R41，2026-10-19）：`tmux new-session -e PATH=` 对 pane 进程**无效**，
    # pane 环境继承自 tmux **client 进程**。workflow-engine.service 无
    # Environment=PATH，经它拉起的 closer/writer 因此每 3s 报
    # `claude: command not found` 却因 pane_pid（wrapper）存活而被判活。
    # 故必须把修好的 PATH 传给 client 自身，而非只传给 -e。
    # 两处都必须补：
    #   1. ~/.local/bin — claude 可执行文件所在（R41）。
    #   2. 解释器自身目录 — hermes_bus 靠 miniforge3 的 `__editable__.hermes_core.pth`
    #      才能 import；pane 的 PATH 落到 /usr/bin/python3(3.12) 时该 .pth 不存在，
    #      `wf confirm` 委托 pipeline 审批会在 `from hermes_bus.config import ...`
    #      处 ModuleNotFoundError（实测 security_auditor #414311：单元含审批 token
    #      却提交不回去，步骤卡在 step_done_ready 靠 TTL 兜底自动放行）。
    _claude_dir = str(Path.home() / ".local" / "bin")
    _py_dir = str(Path(sys.executable).resolve().parent)
    _parts = os.environ.get("PATH", "").split(os.pathsep)
    for _d in (_py_dir, _claude_dir):
        if _d not in _parts:
            _parts.insert(0, _d)
    _claude_path = os.pathsep.join(_parts)
    _env = {**os.environ, "PATH": _claude_path}
    # env 注入按行取表（表数据差异，不是逻辑分叉）：claude 行注 9router ANTHROPIC_*
    # （缺了静默卡 /login，e5d5ccf 回归红线）；opencode 行不注（opencode.jsonc 自配）。
    _env_args = ["-e", "FORCE_PERSONA=0",
                 "-e", f"CCS_ROLE={role}"]
    if row["inject_router_env"]:
        r_env = _router_env()
        _env_args += ["-e", f"ANTHROPIC_BASE_URL={r_env['ANTHROPIC_BASE_URL']}",
                      "-e", f"ANTHROPIC_API_KEY={r_env['ANTHROPIC_API_KEY']}"]
    _env_args += ["-e", f"PATH={_claude_path}"]
    try:
        r = subprocess.run([
            "tmux", "new-session", "-d", "-s", tmux_name,
            "-c", str(ws_path),
            *_env_args,
            "bash", "-c", cmd
        ], capture_output=True, text=True, timeout=10, env=_env)
    except FileNotFoundError:
        return "tmux 未安装或不在 PATH 中"
    except subprocess.TimeoutExpired:
        return "tmux 启动超时 (10s)"
    except Exception as e:
        return f"tmux 启动异常: {e}"
    if r.returncode != 0:
        return f"tmux 启动失败: {r.stderr.strip()}"

    # 6. 等引擎就绪。**未命中要报错**：CLI 不认识某个 argv（例如降级后没有
    # --permission-mode）时会 rc=1 立即退出，wrapper 每 3s 重拉，pane 永远没有提示符；
    # 旧写法照样 return None（报成功），后续 prompt 注进空 pane，故障只在角色"不动"时暴露。
    _ready, _last = False, ""
    for _ in range(_CCS_READY_RETRIES):
        time.sleep(1)
        try:
            out = subprocess.run(
                ["tmux", "capture-pane", "-p", "-t", f"{tmux_name}:0.0",
                 f"-S-{row['ready_tail']}"],
                capture_output=True, text=True, timeout=3
            )
            _last = (out.stdout or "").strip()
            if _ready_hit(_last, row):
                _ready = True
                break
        except Exception as e:
            _log.debug("tmux capture-pane 重试 %s: %s", tmux_name, e)
    if not _ready:
        # 只把"有硬失败证据"当失败：CLI 不认 argv 会打印 unknown option / Usage / error。
        # 没有这类文本 = 慢启动（重负载下引擎首屏可能 >15s），放行并留 warning——
        # 否则慢启动角色会被判失败，比原来的静默容忍更糟。
        if re.search(row["fail_re"], _last, re.I):
            return (f"{tmux_name} 启动后 {_CCS_READY_RETRIES}s 内没有出现提示符，"
                    f"且 pane 报参数/启动错误；pane 最近输出: {_last[-200:]!r}")
        if engine != "ccs":
            # 非 ccs 引擎：判据超时 = 就绪未知，显式失败，禁止静默放行（计划书第 2 层）。
            # 放行的实际后果已实测：opencode 文案漂移（v1.19.3 → 1.19.5 提示）会让
            # "Ask anything" 判据失效，而 prompt/auto_send 仍会打进未就绪的 TUI，
            # 并留下一个无就绪证据的会话。与哨兵失败同口径：回收会话不留残活。
            _kill_tmux_best_effort(tmux_name)
            return (f"{tmux_name} 启动后 {_CCS_READY_RETRIES}s 内未命中 {engine} 就绪判据 "
                    f"{row['ready_any']!r}，就绪状态未知；已回收会话，禁止静默放行"
                    f"（不注入 prompt、不跑 auto_send）；pane 最近输出: {_last[-200:]!r}")
        _log.warning("%s %ss 内未见提示符，但无参数/启动错误文本，按慢启动放行",
                     tmux_name, _CCS_READY_RETRIES)
    time.sleep(1)

    # 7. 大声自检（claude 行）：claude 进程必须带路由 env，否则它会静默卡在
    #    /login 而非报错。e5d5ccf 曾删掉 env 注入 → 所有 headless 角色静默失效。
    if row["assert_router_env"]:
        _assert_router_env_in_pane(tmux_name)
    return None


def _assert_router_env_in_pane(tmux_name: str) -> None:
    """启动后校验 claude 进程已继承 ANTHROPIC_*；缺失则 loud error（不静默）。

    tmux pane_pid 有时就是 claude 主进程本身（bash wrapper 已退出），
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
                                init_prompt: str, no_auto_send: bool,
                                engine: str | None = None) -> None:
    """步骤 7-7.5：注入 prompt + auto_send 批量发送。

    engine 必须由调用方给出：此刻哨兵尚未落盘（第 8 步才写），投递链若反查
    哨兵会回退成 ccs → 拿错输入框几何 → opencode 首条 prompt 判滞留空转。
    """
    # 7. 注入 prompt（自动构建的 role prompt 或用户提供的 init_prompt）
    if init_prompt:
        _tmux_send(tmux_name, init_prompt, engine=engine)
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
                _tmux_send(tmux_name, msg, engine=engine)
                if i < len(auto_msgs):
                    time.sleep(interval)


def _start_monitoring_threads(role: str, tmux_name: str, partners: list[str],
                              instance_id: int, detach: bool, auto_restart: bool,
                              drive: str, bus_track: str,
                              bus_timeout: int) -> None:
    """步骤 9-10：watchdog/tracker/feed 子进程编排。"""
    # 9. 启动守护线程（仅 detach 模式）
    if detach:
        for p in partners:
            start_watchdog(role, p, auto_restart=auto_restart, interval=30,
                          instance_id=instance_id)
            print(f"✅ 守护线程: 监控 {p} 存活")

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
    # 预检：dry_run 只报计划，不做任何副作用。此前该参数被接收但从未读取，
    # `ccs stop X --dry-run` 会真的 kill CCS 并删哨兵（含在跑工作流的角色）。
    if dry_run:
        return {"success": True, "dry_run": True, "role": role,
                "instance_id": instance_id, "was_alive": was_alive,
                "planned": ["stop_watchdog", "stop_tracker", "kill_feed_listener",
                            f"tmux kill-session -t {tmux_name}", "delete_sentinel"]}
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

def _engine_with_source(key: str, tmux_name: str = "") -> tuple[str, str]:
    """按事实源优先返回 (engine, engine_source)。

    哨兵文件为身份事实源：文件里有显式 engine 键才算事实（source="sentinel"）。
    文件缺失/损坏/缺 engine 键时按 cdx- 前缀反推兜底（source="tmux-prefix-fallback"）。
    来源标注必填：推断值不得冒充事实（契约 4.2 + T1A 裁决 A「不许静默编造」）。
    """
    try:
        path = SENTINEL_DIR / f"{key}.json"
        if path.exists():
            data = json.loads(path.read_text())
            if isinstance(data, dict) and "engine" in data:
                return data["engine"], "sentinel"
    except (json.JSONDecodeError, OSError) as e:
        _log.debug("_engine_with_source 读哨兵失败 %s: %s", key, e)
    inferred = ("codex" if (str(tmux_name).startswith("cdx-")
                            or str(key).startswith("cdx-")) else "ccs")
    return inferred, "tmux-prefix-fallback"


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
        _eng, _eng_src = _engine_with_source(s.sentinel_key, s.tmux_session)
        # pid 取真实引擎进程（不是 wrapper/pane pid）；取不到时保留哨兵值并标注
        # pid_source，不静默冒充"这就是引擎 pid"（契约 4.2 防编造同口径）。
        if alive:
            _pid, _pid_src = _engine_pid_and_source(s.tmux_session, _eng)
            if _pid is None:
                _pid, _pid_src = s.pid, "pane-fallback"
        else:
            _pid, _pid_src = s.pid, "sentinel"
        statuses.append({
            "role": s.role,
            "instance_id": s.instance_id,
            "title": s.title,
            "alive": alive,
            "pid": _pid,
            "pid_source": _pid_src,
            "uptime_sec": int(time.time() - s.started_at),
            "lifecycle": s.lifecycle,
            "partner": s.partners[0] if s.partners else None,
            "bus_track": s.bus_track or None,
            "workspace": ws,
            "engine": _eng,
            "engine_source": _eng_src,
            "health": {
                "watchdog_ok": s.health.watchdog_ok,
                # last_watchdog_check=0 表示 watchdog 线程从未跑过（该角色未配 partners，
                # 见 _start_monitoring_threads）。此时 watchdog_ok 只是 dataclass 默认值，
                # 不能当作"检查过且正常"展示。实测 2026-10-16：8/8 角色全是 0.0 却显示 ✅。
                "watchdog_measured": s.health.last_watchdog_check > 0,
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

    # 预检：dry_run 只报计划，不做任何副作用（含写 bus 与投递）。此前该参数
    # 被接收但从未读取，`ccs send X msg --dry-run` 会真的把文本写进 pane，
    # 并可能触发角色执行——预检变成真实投递。
    if dry_run:
        return {"success": True, "dry_run": True, "role": role,
                "instance_id": instance_id, "source": source,
                "planned": [f"tmux send-keys -t {make_tmux_name(role, instance_id)}"],
                "message_preview": message[:200]}

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
        except Exception as e:  # noqa: BLE001
            # 门禁自身故障绝不可杀掉整个 ccs send：曾经只捕获
            # (subprocess.TimeoutExpired, OSError)，而 gatekeeper 写库遇锁抛的是
            # sqlite3.OperationalError（DatabaseError → Error，**不是** OSError 子类）
            # → 异常穿透 main() → ccs.py 以 rc=1 崩溃 → 调用方（工作流引擎）只看到
            # 截断的 traceback，真正的 "database is locked" 被埋掉。
            # 降级语义与原先一致：写 bus 通知 + 继续投递。
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
    _intent = None  # 注入成功后由 inject_reminder 的函数属性带出，缺失时降级
    if source in ("user", "cli"):
        try:
            # record_intent 曾在此 import 但全库无定义（连 WIP 也没有），且
            # user_intent_reminder 自身已声明「意图状态持久化 intent_state.json
            # 已废弃」（见其 read_intent docstring）。同行 import 一个不存在的
            # 名字会让**整行**失败 → inject_reminder/classify_intent 也拿不到 →
            # 整个意图注入路径静默降级（异常被下方 except 吞掉，消息仍投递）。
            from user_intent_reminder import inject_reminder
            send_message = inject_reminder(message, source)
            # 复用 inject_reminder 内部的同一份判定（函数属性），不重算：
            # 重算会拿已被提醒头包裹的 send_message，尾部不再是原始问句。
            _intent = getattr(inject_reminder, "intent", None)
        except Exception as _e:  # noqa: BLE001 - 注入失败绝不可丢失用户原始消息
            send_message = message  # 降级：原样投递
            _log.warning("user_intent_reminder 注入失败，降级原样投递: %s", _e)
        # 可观测痕迹：**所有人类来源都写**，不再只在注入成功时写（审查 P1#5）。
        # 只写注入过的 → "好的/收到" 这类 OTHER 输入不刷新痕迹，上一条已完成的请求
        # 会在 TTL 内被反复贴回去，角色可能重做。
        # 但摘要为空（纯空白/控制字符）时不写：那会把该角色最新痕迹变成无正文，
        # 反而掐掉在飞的提醒（审查 P2-6）。
        _digest = _req_digest(message) if message else ""
        if _digest:
            try:
                subprocess.run(
                    [str(BUS_CLIENT), "write", "code_fix",
                     # 标题只陈述事实：本条是"用户请求痕迹"，是否注入提醒由 daemon 决定。
                     # 旧标题「已注入意图提醒」在只写注入时成立，现在会虚增计数（审查 P2-5）。
                     f"[user_intent_reminder] 用户请求痕迹 → {role}",
                     "--evidence", _trace_evidence(source, role, _intent, _digest),
                     "--src", "core.send"],
                    capture_output=True, timeout=5)
            except Exception as _be:  # noqa: BLE001
                _log.warning("user_intent_reminder bus 证据写失败（不影响投递）: %s", _be)
    delivered = _tmux_send(tmux_name, send_message)
    if delivered == "busy":
        # 忙 ≠ 故障：角色正在推理，消息未发送也未丢弃。不累计连败、不写
        # fallback 通知（那是真丢消息的信号），只告诉调用方稍后重试。
        # bus 侧本就未标记消费，路由/引擎下轮自然重推同一 id，不会重复投递。
        return {"success": False, "reason": "busy",
                "error": f"CCS {role}[{instance_id}] 思考中，未投递"}
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


_APPROVAL_TOKEN_RE = re.compile(
    r"(?i)((?:\*{0,2}`?)?(?:审批密钥|密钥|approval[_ \-]?token|token)[`'\"]{0,2}\*{0,2}\s*"
    r"(?:[:：=]|(?<=\btoken) )\s*[\"'`*]{0,2})"
    r"([A-Za-z0-9_-]{20,})")


def _redact_token(m: re.Match) -> str:
    return m.group(1) + "***"


def _redact_secrets(message: str) -> str:
    """抹掉消息体里的审批凭证（bus #427435/R03）。

    approval_token = secrets.token_urlsafe(32)，43 字符 [A-Za-z0-9_-]。
    抹「标签 + 分隔符 + 20 位以上值」，标签含 密钥/审批密钥/approval_token/token
    （大小写不敏感），分隔符容忍 : ：= 与 `**`/引号/反引号包裹及任意空白 —— 实测
    审批请求有 密钥: / approval_token: / {"approval_token": " 等多种拼接（#427316
    /#427364 两种明文体都漏过）。不按长度盲抹：盲抹会把 wf_id、路径、sha 一起
    干掉，排障记录就废了（#427435）。
    """
    return _APPROVAL_TOKEN_RE.sub(_redact_token, message or "")


# 形状脱敏：_redact_secrets 只认"标签 + 20 位以上值"，漏掉裸 sk-/AKIA 与短口令
# （审证实测 "export OPENAI_API_KEY=sk-proj-…" 原样进 bus、所有角色可读）。
_SECRET_SHAPES = (
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "***"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "***"),
    # 英文标签必须带显式分隔符：否则 "password is required" 会被抹成 "*** required"，
    # 把提示语义一起毁掉（审查 P3-2 实测）。
    # api key 允许空格写法："api key: <43 位>" 实测原样进 bus（审查 important）。
    (re.compile(r"(?i)\b(?:password|passwd|secret|api[ _-]?key|token)\s*[:=：]\s*\S+"), "***"),
    # 中文口令容忍单空格：'密码 hunter2' 不带冒号也很常见（但 '密码是什么' 无分隔符，不命中）。
    # 值必须是 ASCII token：否则 '密码 忘了怎么办' / '密码：过期了怎么办' 整句被吞成 ***，
    # 请求正文丢光（第二轮审查实测 5 例中 1 例全丢、2 例过抹）。
    (re.compile(r"(?:密码|口令)(?:\s*[:=：]|\s)\s*[A-Za-z0-9!@#$%^&*_+=\-]{4,}"), "***"),
)


def _trace_evidence(source: str, role: str, intent, req: str) -> str:
    """用户请求痕迹 evidence 的唯一构造点（产出侧曾零覆盖，审查 P1#6）。

    req_ts 取毫秒：daemon 按 `req != st.request_ts` 判"新请求"，整秒精度会让同秒
    第二条请求继承已耗尽的预算（审查 P2#11）。

    sig 覆盖**整条正文**（不只是 role|req_ts|req）：否则截获一条合法痕迹后单独
    改写 intent= / source= 仍能通过验签 —— 把 task 翻成 other 就能让该角色彻底
    静默，且无需密钥（审查 P2-2）。天花板：同 uid 的 agent 能读到密钥，MAC 是
    "防误写/防跨用户/防手滑"，不是同 uid 内部认证（P1-1，已写进 verify_trace）。
    """
    req_ts = f"{time.time():.3f}"
    body = (f"source={source} role={role} req_ts={req_ts}"
            + (f" intent={intent.value}" if intent else "")
            + (f" req={req}" if req else ""))
    from user_intent_reminder import sign_trace
    return f"{body} sig={sign_trace(body)}"


def _req_digest(message: str) -> str:
    """请求正文压成单行摘要（140 字符），供 daemon 重复提醒携带。

    换行必须折叠：bus evidence 是单行字段，且提醒里多行正文会被 Claude CLI
    当成多条消息逐行提交（实测 coordinator 收到 22 字符裸首行）。脱敏沿用
    _redact_secrets —— 痕迹落到共享 bus，凭证不得随摘要外泄。
    控制字符必须剥掉：^C/^U/ESC 会经 _tmux_send 原样写进角色 pty（清行、清屏、
    中断），且同一条正文会被重复提醒重发至多 3 次。末尾空白先剥再截，否则
    140 字符截断可能停在空格上，下游 strip 后只剩 139（独立审查 D9）。
    """
    text = _redact_secrets(message or "")
    for _pat, _rep in _SECRET_SHAPES:  # 未带标签的凭证形状也要抹（P1#2）
        text = _pat.sub(_rep, text)
    # 结构清洗（控制字符/孤立代理项/折叠/截断显形）复用提醒模块的同一实现：
    # daemon 边界也要做同样的洗，两处逻辑必须一致，否则洗过的正文验签对不上。
    from user_intent_reminder import sanitize_request
    return sanitize_request(text)


def _write_bus_notice(role: str, message: str, source: str, reason: str) -> None:
    """降级时写一条 [ccs_send_fallback] 通知到 bus。

    使用 notice 分类而非 architecture，防止级联风暴：
    architecture 被 10 个角色 consume → 反复触发路由 + 创建 workflow → blocker 洪水。
    notice 仅 maintainer/writer/public 3 角色消费，不会放大级联。

    消息体必须脱敏：本通知是「投递失败」的排障记录，不是凭证通道。实测
    #427364/#427316 把审批 token 明文写进了共享 bus（bus #427435/R03），
    任何一个角色都能拿它伪造 confirm。故与 token 同形的串一律抹掉。
    """
    try:
        subprocess.run(
            [str(BUS_CLIENT), "write", "notice",
             f"[ccs_send_fallback] {source}→{role} 失败: {reason}",
             "--evidence", f"message='{_redact_secrets(message)[:_BUS_MSG_TRUNCATE]}' reason={reason}",
             "--src", "core.send"],
            capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass  # 写 bus 失败不阻塞

def output(role: str, tail: int = 20, instance_id: int = 0) -> str:
    """截取 CCS tmux pane 的最新输出。"""
    from tmux_ops import make_tmux_name
    return _tmux_output(make_tmux_name(role, instance_id), tail=tail)

def health_check(role: str = "", instance_id: int = 0) -> dict:
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
            # 见 status()：False 表示比"不健康"更弱的状态——从未测量过。
            "watchdog_measured": s.health.last_watchdog_check > 0,
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
    # 契约 4.2：自动重启只读哨兵，永不回读 persona。哨兵在时把出生引擎透传给
    # start（否则 start 会重新按 persona 解析，CLI 覆盖成 opencode 的会话会翻回
    # ccs —— 引擎翻转最隐蔽的入口就是这里：ccs wake 先判死后 force_start）。
    _sent = read_sentinel(role_name)
    _born_engine = getattr(_sent, "engine", None) if _sent else None
    if _born_engine in _ENGINE_REGISTRY:
        return start(role_name, init_prompt=init_prompt, engine=_born_engine)
    # 哨兵缺失（无历史会话）→ 保持原行为：不臆造引擎，走声明层/默认解析。
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
            # 必须透传 by_role 作 source：丢空会导致 source="" → 既不走三源验证门禁
            # （send() 里 `role != "self" and source != "cli"` 之外的路径），又让角色间唤醒无法归因。
            # 回归实证（R88）：`ccs send` 的 CLI 默认 source="cli" 被当作人类来源，
            # 每次唤醒写入一条 `已注入意图提醒` 痕迹；bus 对同内容 UPSERT 刷新 ts →
            # 痕迹永不过期（_REQUEST_TTL=1800 形同虚设）→ qa 被反复唤醒空转，
            # 单条 fact 实测被重写 264 次。保留 "" 会让该判据无法区分人机。
            return send(role_name, context, source=by_role or _AUTOMATION_SOURCE)
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


from ops.runner import dashboard

from ops.workspace import register, workspace_create, workspace_list


def _start_feed_subprocess(tmux_name: str) -> None:
    """启动 feed_listener.py 子进程，实时接收 bus 消息并注入到 tmux 会话。

    记录 Popen 句柄进 _FEED_PROCS，stop() 时按 tmux 名 kill+wait 回收。
    """
    # 先回收同 target 的旧 listener：pidfile 单实例锁只防"同时存活"，防不住
    # "旧进程 PPID=1 悬挂 + pidfile 被清"的泄漏路径。实测 ccs-qa 累积 6 个
    # listener、ccs-scout 3 个（2026-10-16 23:58），重复投递同一 tmux。
    _kill_feed_listener_by_pidfile(tmux_name)
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
        # 同 ops/workspace.py：身份文件不是日志容器。
        "- 本文件是身份定义，禁止追加运行日志/巡检记录\n"
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
