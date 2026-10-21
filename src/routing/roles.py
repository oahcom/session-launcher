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

from pathlib import Path
from typing import Optional

from paths import BUS_CLIENT, ensure_paths as _ensure_paths, CCS_WORKSPACES as WORKSPACES
_ensure_paths()

# ── shared_loader 导入：统一角色数据入口 ──
# hermes-session-roles/src/shared_loader.py 是角色加载和 produce/consume
# 解析的单一权威来源。直接 import 复用 registry.load_all 的加载管线
# （含 prompt_refs 渲染、旧格式兼容），避免两套解析逻辑不同步。
_shared_src = str(Path.home() / "hermes-session-roles" / "src")
if _shared_src not in sys.path:
    sys.path.insert(0, _shared_src)
from shared_loader import load_roles as _sl_load_roles  # noqa: E402
from shared_loader import parse_produce_categories as _sl_produce  # noqa: E402
from shared_loader import parse_consume_categories as _sl_consume  # noqa: E402

_FORBIDDEN_MAP: dict[str, list[str]] = {
    "pg": ["run_tests", "edit_config", "deploy", "start_ccs",
           "edit_persona_json", "write_other_workspace"],
    "qa": ["write_code", "edit_config", "deploy", "edit_persona_json"],
    "coordinator": ["write_code", "run_tests", "deploy"],
    "product_architect": ["write_code", "run_tests", "deploy"],
    # 以下 9 个角色此前无条目 → 落到 conversation_monitor._DEFAULT_FORBIDDEN，
    # 而该默认表含 run_tests。实测后果（2026-10-20）：
    #  - engineer 本职是「编码实现 + 自测」，跑 pytest 被判越界 → 引擎反复注入
    #    「请立即停止」并把该提醒混进任务 prompt（bus #368015），打断正在工作的角色
    #  - reviewer 职责是验证他人代码，跑测试是核心手段，同样被拦
    #  - maintainer（#416574）、knowledge_curator（#417927 独立复验）同型误判
    #  - 生产 DB 42 个步骤有 violation_count，其中 6 个 ≥3 次会触发 cancel_step
    # 修法：显式声明每个角色真正该禁的，只有 coordinator/architect 这类决策角色
    # 才禁 run_tests（它们不该自己跑测试，该派给 qa）。
    "engineer": ["edit_config", "deploy", "start_ccs", "edit_persona_json"],
    "reviewer": ["edit_config", "deploy", "start_ccs", "edit_persona_json"],
    "maintainer": ["edit_config", "deploy", "edit_persona_json"],
    "optimizer": ["edit_config", "deploy", "start_ccs", "edit_persona_json"],
    "scout": ["edit_config", "deploy", "start_ccs", "edit_persona_json", "write_code"],
    "writer": ["edit_config", "deploy", "start_ccs", "edit_persona_json", "write_code"],
    # closer 实测有 3 个 .py 产出（收尾脚本），不禁 write_code
    "closer": ["edit_config", "deploy", "start_ccs", "edit_persona_json"],
    "curator": ["edit_config", "deploy", "start_ccs", "edit_persona_json"],
    "pm": ["edit_config", "deploy", "start_ccs", "edit_persona_json", "write_code"],
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

    数据源：优先调用 shared_loader.load_roles()（含 prompt_refs 渲染、
    旧格式兼容），回退到直接 glob。两条路径共享 registry 的加载管线，
    确保 name/category/output_targets/input_signals 等字段语义一致。
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
    try:
        roles = _sl_load_roles()
    except Exception:
        # fallback: 直接 glob 读 JSON（shared_loader 不可用时的降级路径）
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
    """该角色的追加禁区；未定义则返回空串。

    WHY 无定义不再套用通用默认值：_FORBIDDEN_MAP 只覆盖 4 个角色，其余落到旧默认
    [run_tests, deploy, ...]。这些角色本就有各自职责——engineer（开发工程师）需要
    跑测试、devops（运维工程师）的职责就是部署——通用默认值与 persona 的
    constraints（「遵循 X 角色红线」）直接冲突，实测已把「禁区：跑测试、部署」写进
    engineer/devops 的 workspace CLAUDE.md。无定义即不追加声明，角色红线由 base.md
    与 persona constraints 承担，那才是权威源。
    """
    items = _FORBIDDEN_MAP.get(role_name)
    if not items:
        return ""
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
    produce = _sl_produce(role.get("output_targets", []))
    # 消费分类与 shared_loader 一致（排除 "*" + 去重）
    consume = [c for c in _sl_consume(role.get("input_signals", [])) if c != "*"]

    lines = ["## 动作模板（填空即执行）"]
    lines.append("# bus_write/bus_read 是 system alias（定义在 .bashrc）")
    # 参数形式对齐 alias 实际签名：bus_write 经 alias 只多传 --src，
    # `write` 子命令只接 2 个位置参数（cat, text）——旧模板写 3 个
    # （cat + 标题 + 内容），实测报 `unrecognized arguments: <内容>`。
    # bus_read 的 alias 是 `read --cat`，限额用 --limit 而非位置参数。
    for cat in produce:
        lines.append(f"\n写 {cat} → 输出完成")
        lines.append(f'  bus_write {cat} "{cat.upper()}: 【标题】—— 【内容摘要/路径】"')
    consume_targets = consume[:3]
    if consume_targets:
        lines.append("\n读消息")
        for cat in consume_targets:
            lines.append(f"  bus_read {cat} --limit 5")

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

    # 未定义则该行整体不输出：空标签「禁区：」既无信息，又会被角色读成
    # 「没有禁区」的反向暗示。
    _forbidden = _forbidden_list(name)
    if _forbidden:
        lines.append(f"\n禁区：{_forbidden}")
    return "\n".join(lines)

def _contract_block(role: dict) -> str:
    """从角色定义构建 ## 契约 区块（产出/消费分类、协作组、验证标准、驱动方式）。"""
    produce = _sl_produce(role.get("output_targets", []))
    consume = [c for c in _sl_consume(role.get("input_signals", [])) if c != "*"]

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
        lines.append("- 定时调度: 无 (事件驱动)")
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

    if drive == "cron":
        msgs.append(f"驱动模式: cron（{cron_schedule}）")
        msgs.append("等待 cron-worker 唤醒。")
    elif drive == "goal":
        msgs.append("驱动模式: goal（主动执行至目标达成）")
    elif drive == "infinite":
        # 引擎按需拉起时常驻；无 drive=="ondemand" 的启动闸门拦截
        msgs.append("驱动模式: infinite（常驻，持续等待信号）")
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
        # ccs-<name> 可能是**另一个角色/工作区自己的** workspace（persona `ccs-coordinator`、
        # 目录 `ccs-product_architect` 与 `coordinator`/`product_architect` 并存）。实测
        # ccs-coordinator 的 KNOWLEDGE 块曾被写成 coordinator 的 33KB 契约，两个角色抢一份身份。
        # 归属只在两处结构化位置取证，不做全文子串匹配（"不属于 ccs-X" 也会命中）：
        #   1. 文件头的 `<identity>你是 <name>，…`
        #   2. `<!-- WORKSPACE_SYS:START -->` 块里的 `本 workspace 属于 <name>，…`
        #      —— 该块可能被追加在文件末尾（ops/workspace.py），所以按块找、不按偏移读。
        # 不依赖角色表：角色表加载失败时判据仍成立，本模块也不在运行时依赖另一个仓库。
        # 取不到证据时按旧行为注入（fail-open）：少注入一个目录是静默退化，
        # 抛异常会让 ccs start 直接失败。
        try:
            _txt = (p2 / "CLAUDE.md").read_text(encoding="utf-8", errors="ignore")
        except OSError:
            _txt = ""
        _sys = re.search(r"<!-- WORKSPACE_SYS:START -->(.*?)<!-- WORKSPACE_SYS:END -->", _txt, re.S)
        # 全文搜归属声明，不设偏移窗口：窗口 400 时实测 22 个文件命中位置在 6395-7940 → 死判据。
        _owned = f"你是 {p2.name}，" in _txt or bool(
            _sys and f"属于 {p2.name}，" in _sys.group(1))
        if not _owned:
            paths.append(p2 / "CLAUDE.md")
    return paths

_PROTECTED_BLOCK_RE = re.compile(
    # 与下方 _bl 去重里 re.split 的 pattern 保持一致：这两类块是用户/运维的地盘，一律保留原件。
    r"<!-- (?:WORKSPACE_SYS|BH_KNOWLEDGE)[^>]*-->.*?<!-- /?(?:WORKSPACE_SYS|BH_KNOWLEDGE)[^>]*-->",
    re.S,
)


def _strip_stale_prompt_prefix(prefix: str) -> str:
    """删掉 KNOWLEDGE 块之前的旧版手写 prompt 残留，受保护标记块保留原样。

    WHY: 注入只替换 KNOWLEDGE 块之间的内容，块外的旧 prompt 永远留着，并和新契约
    一起进 system prompt（实测 22/37 个 workspace 残留 :20128、幽灵角色 architect、
    无口径判据）。判据取旧 prompt 独有的两个标记同时出现，缺一不动文件：
    只有 <agent_loop> 或只有 </knowledge_reference> 都可能是角色自己写的正文。
    幂等：删完前缀不再含 <agent_loop>；若残留保护块自身含该标记，第二次也只会
    重复抽出同样的块，结果字节不变。
    """
    if "<agent_loop>" not in prefix or "</knowledge_reference>" not in prefix:
        return prefix
    kept = [m.group(0) for m in _PROTECTED_BLOCK_RE.finditer(prefix)]
    # 没有受保护块时整段不动：曾返回空串，等于把 2.3KB 手写前缀（identity/红线表）删光，
    # 且由 10 分钟定时器自动执行、无备份，workspace 不在 git 里无法回滚。
    return ("\n\n".join(kept) + "\n\n") if kept else prefix


def inject_role_knowledge_into_workspace(role: dict) -> str:
    """将角色契约 + base.md 通用红线写入 workspace 级 CLAUDE.md（KNOWLEDGE 块）。

    base.md 内容通过 role_assembler 编译注入（含角色职责红线、Git 规范、自审查指令等）。
    原有硬编码的 sec_redlines/git_rules/review_rules 已由 base.md 覆盖，移除冗余。
    """
    # 生产 prompt 只认主仓：worktree 里的 base.md 会整块覆盖 CLAUDE.md（实测 35611B 脏版本），
    # 角色 prompt 被静默换成过期内容且无从察觉，所以非主仓一律拒绝，除非显式豁免。
    _canonical = (Path.home() / "hermes-session-roles").resolve()
    _root = SESSION_ROLES_ROOT.expanduser().resolve()
    # 只认显式真值：ALLOW=0/false/no 都是"关"，曾用 os.environ.get() 判真 → 关等于开。
    if _root != _canonical and os.environ.get("ALLOW_ROLES_WORKTREE_INJECT", "").strip().lower() not in ("1", "true", "yes"):
        return f"refused (SESSION_ROLES_ROOT={_root} != {_canonical}; 设 ALLOW_ROLES_WORKTREE_INJECT=1 才放行)"
    name = role.get("name", "")

    # 读取 role TASKS.md 的 negative 教训，注入 KNOWLEDGE 块（评分信号回写闭环补全）
    # WHY: task_evidence 负向评分回写 TASKS.md，此处把教训注入 KNOWLEDGE 块随 role prompt 下发，
    # 让 role 从历史模板任务教训中学习（评分信号闭环闭合）。
    _tasks_lessons = ""
    _MAX_LESSONS = 5
    try:
        # 防路径遍历：name 归一化并约束在 WORKSPACES 内
        _safe_name = name
        try:
            _tasks_path = (WORKSPACES / _safe_name / "TASKS.md").resolve()
            if not _tasks_path.is_relative_to(WORKSPACES.resolve()):
                raise ValueError("path traversal")
        except Exception:
            _tasks_path = None  # 非法名 → 跳过读取

        if _tasks_path and _tasks_path.exists():
            _txt = _tasks_path.read_text(encoding="utf-8", errors="ignore")
            # 仅提取 `- [negative]` 行 + `## 历史教训` 标题
            _lessons_raw = []
            for _line in _txt.splitlines():
                _s = _line.strip()
                if _s.startswith("- [negative]") or _s == "## 历史教训":
                    # 防 prompt injection：只保留前 120 字，去除指令式措辞
                    _clean = _s[:120].replace("请", "").replace("必须", "").replace("务必", "")
                    _lessons_raw.append(f"- [lesson] {_clean}")
                    if len(_lessons_raw) >= _MAX_LESSONS:
                        break
            if _lessons_raw:
                _tasks_lessons = "\n".join(_lessons_raw) + "\n"
    except Exception:
        pass  # 读取失败不影响主流程

    ws_paths = _resolve_ws_paths(name)
    if not ws_paths:
        return "skipped (no workspace)"

    # 优先使用 role_assembler 输出（含 base.md + 角色 prompt + driver mixin）
    assembled = _role_assembler_output(name, role)
    if assembled:
        # 动作模板只有 4 个角色手写在 prompts/roles/*.md 里，其余角色的
        # workspace 缺这块（33/37 实测无「禁区」）。_action_templates 是同内容的
        # 动态版，却从未被调用——这里接上。已有手写版的角色输出里已含该块，
        # 跳过以免同一段在 KNOWLEDGE 块内重复两次。
        if "动作模板" not in assembled:
            assembled += "\n\n" + _action_templates(role)
        knowledge_block = (
            f"\n\n<!-- KNOWLEDGE:START -->\n"
            f"{assembled}\n"
            + (_tasks_lessons and "\n## 历史教训（评分回写，自动学习）\n" + _tasks_lessons or "")
            + "\n<!-- KNOWLEDGE:END -->\n"
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
            + (_tasks_lessons and "\n## 历史教训（评分回写，自动学习）\n" + _tasks_lessons or "")
            + "\n<!-- KNOWLEDGE:END -->\n"
        )

    injected = 0
    for claude_md in ws_paths:
        content = claude_md.read_text(encoding="utf-8")

        # 移除旧 KNOWLEDGE 块（如有），总是注入最新的
        start_marker = "<!-- KNOWLEDGE:START -->"
        end_marker = "<!-- KNOWLEDGE:END -->"
        start_idx = content.find(start_marker)
        # 块外的旧版 prompt 残留先清掉（含 WORKSPACE_SYS 在内的标记块原样保留），
        # 再走下面的「旧块替换」逻辑，_bl 去重那套不动。
        if start_idx > 0:
            _head = _strip_stale_prompt_prefix(content[:start_idx])
            if _head != content[:start_idx]:
                content, start_idx = _head + content[start_idx:], len(_head)
        if start_idx >= 0:
            end_idx = content.find(end_marker, start_idx)
            if end_idx >= 0:
                end_idx += len(end_marker)
                # 前导会随每次注入累积上一版副本（只替换块、不清理块外）。
                # 整段行都在新块内的 ## 段即历史残留，删；标记块先抽出保护。
                _bl = {ln.strip() for ln in knowledge_block.splitlines() if ln.strip()}
                _parts = re.split(r"(<!-- (?:WORKSPACE_SYS|BH_KNOWLEDGE)[^>]*-->.*?<!-- /?(?:WORKSPACE_SYS|BH_KNOWLEDGE)[^>]*-->)",
                                  content[:start_idx], flags=re.S)
                before = "".join(
                    part if i % 2 else "".join(
                        s for s in re.split(r"(?m)^(?=## )", part)
                        if not all(ln.strip() in _bl for ln in s.splitlines() if ln.strip()))
                    for i, part in enumerate(_parts)).rstrip()



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

        # 只在内容真的变了才写：定时重注入（ccs-reload-knowledge.timer）每 10 分钟
        # 跑一次，无条件 write_text 会把所有 workspace 的 mtime 刷成"刚刚"，
        # 让 code-checksum-* 与新鲜度判据全部失去意义。
        if new_content != content:
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
    # 审查块已删：曾硬编码 `cd /home/administrator/session-launcher`，正是 base.md 明令
    # 禁止的「在 A 仓库改代码却审 B 仓库」（实测致 reviewer 整轮空转）。回退路径只留
    # 与仓库无关的安全线；审查流程由 base.md 给，此处不再复制一份会漂移的副本。
    return sec + git


def _validate_role_name(name: str) -> bool:
    """角色名仅允许字母、数字、下划线、连字符。"""
    return bool(_ROLE_NAME_RE.match(name))

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
