#!/usr/bin/env python3
"""
roles.py — 角色加载、workspace 知识注入、禁区映射、动作模板
"""

__all__ = [
    'load_roles',
    'get_role',
    '_invalidate_role_cache',
    '_forbidden_list',
    'check_wake_permission',
    '_action_templates',
    '_build_role_prompt',
    '_resolve_ws_paths',
    'inject_role_knowledge_into_workspace',
    '_validate_role_name',
    '_ensure_bus_aliases_in_bashrc',
    # ponytail: inject_prompt_into_claudemd/clear_injected_prompt 已废弃，保留定义兼容旧 cron 模式
    'inject_prompt_into_claudemd',
    'clear_injected_prompt',
    '_ROLE_NAME_RE',
    'SESSION_ROLES_ROOT',
    '_WS_MARKER_START',
    '_WS_MARKER_END',
    'SESSION_MARKER_START',
    'SESSION_MARKER_END',
    '_FORBIDDEN_MAP',
    '_FORBIDDEN_DISPLAY',
    '_WAKE_PERMISSION_MAP',
    '_CLAUDE_MD',
]

import json
import threading
import os
import re
import subprocess
import sys
import time

from pathlib import Path
from typing import Optional

from paths import BUS_CLIENT, ensure_paths as _ensure_paths
_ensure_paths()

_FORBIDDEN_MAP: dict[str, list[str]] = {
    "pg": ["run_tests", "edit_config", "deploy", "start_ccs",
           "edit_persona_json", "write_other_workspace"],
    "qa": ["write_code", "edit_config", "deploy", "edit_persona_json"],
    "coordinator": ["write_code", "run_tests", "deploy"],
    "product_architect": ["write_code", "run_tests", "deploy"],
}


_FORBIDDEN_DISPLAY = {
    "run_tests": "跑测试", "edit_config": "改配置", "deploy": "部署",
    "start_ccs": "启动 CCS", "edit_persona_json": "改 persona JSON",
    "write_code": "写代码", "write_other_workspace": "写其他角色 workspace",
}

_WAKE_PERMISSION_MAP: dict[str, list[str]] = {
    "*": ["coordinator", "lr"],
    "pg": ["qa", "pm", "reviewer", "product_architect"],
    "qa": ["pm", "reviewer"],
}

_ROLE_NAME_RE = re.compile(r'^[a-zA-Z0-9_-]+\Z')
# ponytail: \Z blocks trailing \n that $ would accept

SESSION_ROLES_ROOT = Path(os.environ.get(
    "SESSION_ROLES_ROOT",
    str(Path.home() / "hermes-session-roles")
))

_WS_MARKER_START = "<!-- WORKSPACE_SYS:START -->"
_WS_MARKER_END = "<!-- WORKSPACE_SYS:END -->"

SESSION_MARKER_START = "<!-- SESSION_ROLE:START -->"
SESSION_MARKER_END = "<!-- SESSION_ROLE:END -->"

_CLAUDE_MD = Path(os.environ.get("CLAUDE_MD_PATH",
                  "~/.claude/projects/-home-administrator/CLAUDE.md")).expanduser()

_lock = threading.Lock()

# ── 验证网关：加载角色前通过 shared_loader 验证 ──
# hermes-session-roles 的 shared_loader 是 produce/consume 解析的单一权威来源。
# 这里仍直接读取 JSON（需要 raw dict 注入 workspace），
# 但启动时调用 shared_loader --validate 做完整性门禁。
def _run_shared_loader_validate() -> bool:
    """调用 shared_loader --validate 检查角色定义完整性。

    验证失败打印警告（不阻塞启动，由 validate_roles.py 的预提交挂钩强制执行）。
    """
    sl_path = SESSION_ROLES_ROOT / "src" / "shared_loader.py"
    if not sl_path.exists():
        return False
    try:
        r = subprocess.run(
            [sys.executable, str(sl_path), "--validate"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            print(f"  [roles] ⚠ shared_loader 验证发现 {r.stdout.count('FAIL')} 个问题",
                  file=sys.stderr)
            return True
    except Exception as e:
        print(f"  [roles] ⚠ shared_loader 调用失败: {e}", file=sys.stderr)
    return True

_SHARED_LOADER_CHECKED = False

# 角色级缓存：每次从文件读取后缓存，_invalidate_role_cache() 手动刷新
_ROLE_CACHE: dict[str, Optional[dict]] = {}

_LOADED_ALL_ROLES: list[dict] | None = None


def load_roles() -> list[dict]:
    """读取角色 JSON 文件（验证网关 + 缓存）。

    验证：首次加载时通过 shared_loader --validate 确认角色定义完整性。
    数据源：仍直接读 JSON（需要 raw dict 注入 workspace），
    但 produce/consume 解析已统一至 shared_loader 的 roles_export.json。
    """
    global _LOADED_ALL_ROLES, _SHARED_LOADER_CHECKED
    _need_validate = False
    with _lock:
        if _LOADED_ALL_ROLES is not None:
            return _LOADED_ALL_ROLES
        if not _SHARED_LOADER_CHECKED:
            _need_validate = True
    if _need_validate:
        _run_shared_loader_validate()
        _SHARED_LOADER_CHECKED = True
    roles = []
    for f in sorted(SESSION_ROLES_ROOT.glob("personas/session-roles/persona_*.json")):
        try:
            with open(f) as fp:
                roles.append(json.load(fp))
        except (json.JSONDecodeError, OSError):
            continue
    with _lock:
        _LOADED_ALL_ROLES = roles
    return roles

def get_role(role_name: str) -> Optional[dict]:
    """按名称获取角色定义（带缓存，避免重复 I/O）。"""
    with _lock:
        if role_name in _ROLE_CACHE:
            return _ROLE_CACHE[role_name]
    if not SESSION_ROLES_ROOT.exists():
        with _lock:
            _ROLE_CACHE[role_name] = None
        return None
    for r in load_roles():
        if r.get("name") == role_name:
            with _lock:
                _ROLE_CACHE[role_name] = r
            return r
    with _lock:
        _ROLE_CACHE[role_name] = None
    return None

def _invalidate_role_cache() -> None:
    """清空角色缓存（用于角色文件修改后）。"""
    global _LOADED_ALL_ROLES
    with _lock:
        _ROLE_CACHE.clear()
        _LOADED_ALL_ROLES = None

def _forbidden_list(role_name: str) -> str:
    items = _FORBIDDEN_MAP.get(
        role_name,
        ["run_tests", "edit_config", "deploy", "start_ccs", "edit_persona_json"]
    )
    return "、".join(_FORBIDDEN_DISPLAY.get(i, i) for i in items)

def check_wake_permission(actor: str, target: str) -> bool:
    """检查 actor 是否有权唤醒 target。"""
    if actor == target:
        return True  # 自唤醒
    if actor in _WAKE_PERMISSION_MAP.get("*", []):
        return True
    return actor in _WAKE_PERMISSION_MAP.get(target, [])

def _action_templates(role: dict) -> str:
    """生成角色对应的动作填空模板，含跨角色协作示例。"""
    name = role.get("name", "")
    output_targets = role.get("output_targets", [])
    input_signals = role.get("input_signals", [])

    produce = []
    for t in output_targets:
        m = re.search(r"bus cat=(\w+)", t)
        if m:
            produce.append(m.group(1))

    consume = []
    for s in input_signals:
        if s.get("type") == "bus":
            cat = s.get("spec", {}).get("category", "")
            if cat and cat != "*":
                consume.append(cat)

    lines = ["## 动作模板（填空即执行）"]
    lines.append("# bus_write/bus_read/bus_unread/bus_search 是 system alias（定义在 .bashrc）")
    for cat in produce:
        lines.append(f"\n写 {cat} → 输出完成")
        lines.append(f'  bus_write {cat} "{cat.upper()}: 【标题】" "【内容/路径】"')
    consume_targets = consume[:3]
    if consume_targets:
        lines.append(f"\n读消息")
        for cat in consume_targets:
            lines.append(f"  bus_read {cat} 5")

    # ── 跨角色协作示例（仅拥有唤醒权限的角色） ──
    # 构建拥有唤醒权限的角色列表
    _wake_roles = set()
    for _target, _actors in _WAKE_PERMISSION_MAP.items():
        if _target == "*":
            _wake_roles.update(_actors)
        else:
            for _a in _actors:
                _wake_roles.add(_a)
    if name in _wake_roles:
        lines.append("""
## 跨角色协作（PartnerClient）
# 检查其他角色是否存活
  python3 ~/session-launcher/src/routing/partner.py resolve <role>
# 等待对方确认接单（超时自动唤醒）
  python3 ~/session-launcher/src/routing/partner.py confirm <task_id> <role> --as <my_role>
# 唤醒离线角色
  python3 ~/session-launcher/src/routing/partner.py wake <role> --as <my_role> --context "任务描述"
# 安全发送消息（自动唤醒离线接收方）
  python3 ~/session-launcher/src/routing/partner.py send-safe <role> <消息> --as <my_role>""")

    lines.append(f"\n禁区：{_forbidden_list(name)}")
    return "\n".join(lines)

def _contract_block(role: dict) -> str:
    """从角色定义构建 ## 契约 区块（产出/消费分类、协作组、验证标准、驱动方式）。"""
    produce = []
    for t in role.get("output_targets", []):
        m = re.search(r"bus cat=(\w+)", t)
        if m:
            produce.append(m.group(1))

    consume = []
    for s in role.get("input_signals", []):
        if s.get("type") == "bus":
            cat = s.get("spec", {}).get("category", "")
            if cat and cat != "*":
                consume.append(cat)

    workgroup = role.get("workgroup", [])
    drive = role.get("drive", "")
    cron_schedule = role.get("cron_schedule", "")
    auto_msgs = role.get("auto_send_messages", [])

    lines = ["\n## 契约"]
    lines.append(f"- 产出分类: {', '.join(produce) if produce else '无'}")
    lines.append(f"- 消费分类: {', '.join(consume) if consume else '无'}")
    if workgroup:
        wg_names = [w["role"] if isinstance(w, dict) else str(w) for w in workgroup]
        lines.append(f"- 协作组: {', '.join(wg_names)}")
    if drive:
        lines.append(f"- 驱动方式: {drive}")
    if cron_schedule:
        lines.append(f"- 定时调度: {cron_schedule} (由 cron-worker 触发)")
    else:
        lines.append(f"- 定时调度: 无 (事件驱动)")
    if auto_msgs:
        lines.append(f"- 自动发送: {', '.join(auto_msgs[:3])}{'...' if len(auto_msgs)>3 else ''}")

    eval_criteria = role.get("eval_criteria", [])[:3]
    if eval_criteria:
        lines.append("")
        lines.append("### 验证标准")
        for i, c in enumerate(eval_criteria, 1):
            short = c.split("| 验证:")[0].strip()
            lines.append(f"{i}. {short}")

    return "\n".join(lines)

def _role_assembler_output(name: str, role: dict | None = None) -> str:
    """调用 role_assembler.py 获取角色定义文本（不含 BUS_LOOP_SUFFIX）。
    失败时回退到 role.system_prompt。"""
    assembler = Path(os.environ.get('SESSION_ROLES_ROOT', str(Path.home() / 'hermes-session-roles'))) / 'src' / 'role_assembler.py'
    if assembler.exists():
        try:
            r = subprocess.run(
                [sys.executable, str(assembler), name],
                capture_output=True, text=True, timeout=10
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except Exception as _e:
            import logging
            logging.getLogger("roles").warning(
                "role_assembler failed for %s (falling back to system_prompt): %s", name, _e
            )
    if role:
        return (role.get("system_prompt", "")
                .replace("{persona_name}", role["name"])
                .replace("{persona_title}", role["title"]))
    return ""

def _build_role_prompt(role: dict) -> str:
    """构建会话启动 prompt。

    角色身份/契约/红线由 workspace CLAUDE.md KNOWLEDGE 块提供，
    这里仅做启动时的驱动模式提示和初始指令。
    """
    name = role.get("name", "")
    title = role.get("title", "")
    drive = role.get("drive", "ondemand")
    cron_schedule = role.get("cron_schedule", "")
    msgs = [f"## {title} ({name}) — 已就绪"]

    if drive == "loop":
        # loop 已淘汰，等效 ondemand
        msgs.append("驱动模式: ondemand（手动触发）")
        msgs.append("无待处理任务时进入空闲等待。")
    elif drive == "cron":
        msgs.append(f"驱动模式: cron（{cron_schedule}）")
        msgs.append("等待 cron-worker 唤醒。")
    elif drive == "goal":
        msgs.append("驱动模式: goal（主动执行至目标达成）")
    else:
        msgs.append("驱动模式: ondemand（按需启动）")
        msgs.append("等待上游角色或 cron-worker 通过 `ccs send` 发送任务。")

    msgs.append("")
    msgs.append("启动自检:")
    msgs.append("1. 确认 CLAUDE.md KNOWLEDGE 区块已加载身份与契约")
    msgs.append("2. 确认 MCP 工具列表中有所需工具")
    msgs.append("3. 执行当前角色的第一条 eval_criteria")
    return "\n".join(msgs)

def _resolve_ws_paths(name: str) -> list[Path]:
    """解析角色的 workspace CLAUDE.md 路径列表（可能多个）。"""
    paths = []
    p1 = Path(f"~/ccs-workspaces/{name}").expanduser()
    p2 = Path(f"~/ccs-workspaces/ccs-{name}").expanduser()
    if p1.exists() and (p1 / "CLAUDE.md").exists():
        paths.append(p1 / "CLAUDE.md")
    if p2.exists() and (p2 / "CLAUDE.md").exists() and p2 / "CLAUDE.md" not in paths:
        paths.append(p2 / "CLAUDE.md")
    return paths

def inject_role_knowledge_into_workspace(role: dict) -> str:
    """将角色契约 + base.md 通用红线写入 workspace 级 CLAUDE.md（KNOWLEDGE 块）。

    base.md 内容通过 role_assembler 编译注入（含角色职责红线、Git 规范、自审查指令等）。
    原有硬编码的 sec_redlines/git_rules/review_rules 已由 base.md 覆盖，移除冗余。
    """
    name = role.get("name", "")

    ws_paths = _resolve_ws_paths(name)
    if not ws_paths:
        return "skipped (no workspace)"

    # 优先使用 role_assembler 输出（含 base.md + 角色 prompt + driver mixin）
    assembled = _role_assembler_output(name, role)
    if assembled:
        knowledge_block = (
            f"\n\n<!-- KNOWLEDGE:START -->\n"
            f"{assembled}\n"
            f"<!-- KNOWLEDGE:END -->\n"
        )
    else:
        # 回退: 仅 contract + base.md（旧路径）
        contract = _contract_block(role)
        base_path = Path(os.environ.get('SESSION_ROLES_ROOT',
                          str(Path.home() / 'hermes-session-roles'))) / 'prompts' / 'base.md'
        base_content = base_path.read_text(encoding='utf-8') if base_path.exists() else _fallback_base_content()
        knowledge_block = (
            f"\n\n<!-- KNOWLEDGE:START -->\n"
            f"# 契约 — {role.get('title', name)}\n\n"
            f"{contract}\n"
            f"{base_content}\n"
            f"<!-- KNOWLEDGE:END -->\n"
        )

    injected = 0
    for claude_md in ws_paths:
        content = claude_md.read_text(encoding="utf-8")

        # 移除旧 KNOWLEDGE 块（如有），总是注入最新的
        start_marker = "<!-- KNOWLEDGE:START -->"
        end_marker = "<!-- KNOWLEDGE:END -->"
        start_idx = content.find(start_marker)
        if start_idx >= 0:
            end_idx = content.find(end_marker, start_idx)
            if end_idx >= 0:
                end_idx += len(end_marker)
                before = content[:start_idx].rstrip()
                after = content[end_idx:].lstrip()
                new_content = before + knowledge_block + after
            else:
                new_content = content[:start_idx].rstrip() + knowledge_block
        else:
            if "<!-- WORKSPACE_SYS:END -->" in content:
                insert_at = content.rindex("<!-- WORKSPACE_SYS:END -->") + len("<!-- WORKSPACE_SYS:END -->")
                new_content = content[:insert_at] + knowledge_block + content[insert_at:]
            else:
                new_content = content + knowledge_block

        claude_md.write_text(new_content, encoding="utf-8")
        injected += 1

    return f"injected ({injected} workspace(s))"

def _fallback_base_content() -> str:
    """base.md 不可用时的回退（旧硬编码通用规则）。"""
    sec = (
        "\n### 安全红线（全员通用）\n"
        "- ❌ 禁止硬编码凭据、密钥、令牌——使用环境变量或密钥管理注入\n"
        "- ❌ 禁止 `shell=True` + 字符串拼接（用 `subprocess.run([...])` 代替）\n"
        "- ❌ 禁止 SQL 字符串拼接（用参数化查询或 ORM）\n"
        "- ✅ 所有外部输入必须验证类型、范围、格式\n"
        "- ✅ 文件路径使用 `os.path.realpath()` 规范化防路径遍历\n"
    )
    git = (
        "\n### Git 操作规范（本地即生产）\n"
        "- ❌ 禁止切分支（`git switch`、`git checkout <branch>`、`git checkout -b`）——本地是生产环境\n"
        "- ❌ 禁止 git checkout <文件> 或 git stash——会破坏其他 session 的未提交更改\n"
        "- ❌ 禁止 `git commit --no-verify` 跳过 hooks——代码质量最后一道防线\n"
        "- ✅ 每次变更后必须 `git add → git commit → git push`，不 push = 变更丢失\n"
        "- ✅ commit message 格式: `feat/fix/refactor: 中文描述`\n"
    )
    review = (
        "\n### 提交前自审查\n"
        "- ✅ 执行 `cd /home/administrator/session-launcher && codex review --uncommitted -c model=\"9router_hermes\"`\n"
        "- ✅ 逐问题修复 → 重新运行 → 连续两轮零问题才可提交\n"
        "- ✅ 审查结论以 `# Review: <结论>` 写入 commit message\n"
    )
    return sec + git + review


def _validate_role_name(name: str) -> bool:
    """角色名仅允许字母、数字、下划线、连字符。"""
    return bool(_ROLE_NAME_RE.match(name))

def _ensure_bus_aliases_in_bashrc() -> None:
    """在 ~/.bashrc 写入 bus 别名（仅追加一次）。"""
    bashrc = Path.home() / ".bashrc"
    if not bashrc.exists():
        return
    content = bashrc.read_text()
    marker = "# ── CCS Bus aliases ──"
    if marker in content:
        return
    alias_block = f"""
{marker}
alias bus_write='python3 {BUS_CLIENT} write'
alias bus_read='python3 {BUS_CLIENT} read --cat'
alias bus_unread='python3 {BUS_CLIENT} unread'
alias bus_search='python3 {BUS_CLIENT} search'
"""
    bashrc.write_text(content.rstrip() + "\n" + alias_block)

def inject_prompt_into_claudemd(role: dict) -> str:
    """DEPRECATED — 旧版 launcher cron 模式使用。CCS 模式应使用 inject_role_knowledge_into_workspace()。"""
    _ensure_bus_aliases_in_bashrc()
    prompt = _build_role_prompt(role)
    ctx = _role_assembler_output(role["name"], role)
    if ctx:
        prompt += "\n" + ctx
    lifecycle = role.get("lifecycle", "infinite")
    drive = role.get("drive", "cron")
    templates = _action_templates(role)
    inject_block = (
        f"{SESSION_MARKER_START}\n"
        f"# Session Role: {role['name']} ({role['title']})\n"
        f"# Lifecycle: {lifecycle} | Drive: {drive}\n"
        f"# 自动注入 — 由 session-launcher 管理\n\n"
        f"{prompt}\n\n"
        f"{templates}\n\n"
        f"{SESSION_MARKER_END}\n"
    )
    if not _CLAUDE_MD.exists():
        _CLAUDE_MD.write_text(inject_block)
        return "created"
    content = _CLAUDE_MD.read_text(encoding="utf-8")
    if SESSION_MARKER_START in content and SESSION_MARKER_END in content:
        start_idx = content.rindex(SESSION_MARKER_START)
        end_idx = content.rindex(SESSION_MARKER_END) + len(SESSION_MARKER_END)
        new_content = content[:start_idx] + inject_block + content[end_idx:]
    elif SESSION_MARKER_START in content:
        start_idx = content.rindex(SESSION_MARKER_START)
        new_content = content[:start_idx] + inject_block
    else:
        new_content = content.rstrip() + "\n\n" + inject_block
    _CLAUDE_MD.write_text(new_content, encoding="utf-8")
    prompt_file = Path("/tmp/session_role_prompt.txt")
    prompt_file.write_text(prompt)
    return "injected"

def clear_injected_prompt() -> None:
    """清除主项目 CLAUDE.md 中的 session role 注入块（legacy）。"""
    if not _CLAUDE_MD.exists():
        return
    content = _CLAUDE_MD.read_text(encoding="utf-8")
    if SESSION_MARKER_START not in content:
        return
    start_idx = content.index(SESSION_MARKER_START)
    if SESSION_MARKER_END in content:
        end_idx = content.index(SESSION_MARKER_END) + len(SESSION_MARKER_END)
    else:
        end_idx = len(content)
    new_content = content[:start_idx] + content[end_idx:]
    _CLAUDE_MD.write_text(new_content, encoding="utf-8")

def validate_ccs_execution(role: str, action: str) -> None:
    """CCS-RULE 代码层门禁：注入规则执行前校验。

    配合 _ccs_injection_rules.json 的 prompt_layer 约束，
    此函数在代码侧强制执行关键规则：
    - CCS-RULE-001: ccs send 接收门禁
    - CCS-RULE-002: 任务执行前验证

    在 start/send 前调用，验证角色/动作合法性。

    ponytail: rules v1.1 prompt_layer only, 方案B增量.
    add when: 需要 socket listener 动态门禁时扩展此函数。
    """
    if not role or not action:
        raise ValueError("role and action required")
    # CCS-RULE-002: 含 task 操作时需要 task 状态合法
    if "task" in action.lower() or "workflow" in action.lower():
        # ponytail: Gate was YAGNI'd (deleted). Validation now relies on persona JSON existence.
        if not SESSION_ROLES_ROOT.joinpath("personas/session-roles", f"persona_{role}.json").exists():
            # fallback: check loaded role cache
            if get_role(role) is None:
                print(f"  [roles] ⚠ CCS-RULE: role '{role}' not found in persona JSON", file=sys.stderr)
    # CCS-RULE-001: 非标准格式的 ccs send 拒绝（由调用方提供格式校验）
