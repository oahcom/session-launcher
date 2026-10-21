#!/usr/bin/env python3
"""
cross_role_router.py — 跨角色路由层（三源验证）

在 ccs send 时拦截跨角色消息，验证消息来源真实性。
三源验证逻辑（ccs-send-source-verification memory）：
  1. bus --src 查询
  2. DB assigner 查询
  3. sentinel 角色状态
  至少 2 个源一致才放行。
"""

import json
import logging
import re
import sqlite3
import subprocess
import time
from pathlib import Path

from paths import (WORKFLOWS_DB as DB_PATH, BUS_CLIENT, SESSION_ROLES_PERSONAS,
                   HERMES_WORKFLOWS)

logger = logging.getLogger(__name__)

# 落库前必须跑一遍：workflow_logs 不是凭证存储。实测 107 行 detail 里是明文审批
# 密钥（curator #429005），任何能读 workflows.db 的角色都能在 TTL 内拿它 wf confirm。
# 正则放在本模块而非从 core 导入：core.py:949 才延迟 import gatekeeper，
# 反向依赖会成环。core 侧只用于 bus 降级通知，落库通道以本函数为准。
# 不盲抹「不跟标签的 40+ 长串」：message_preview 里还有 wf_id/路径/sha，
# 抹了排障记录就废了（#427435 已有一批不可读的抹除行）。
_TOKEN_LABEL_RE = re.compile(
    r"((?:审批密钥|approval_token|token|密钥)\s*[:：=]\s*)([A-Za-z0-9_-]{20,})",
    re.IGNORECASE)


def _redact_secrets(text: str) -> str:
    """抹掉消息体里的凭证，保留标签与上下文（bus #429005）。"""
    return _TOKEN_LABEL_RE.sub(lambda m: m.group(1) + "***", text or "")

# ── 敏感操作分类 ─────────────────────────────────
# 按影响范围分级：info / operation / admin
SENSITIVE_CMDS: dict[str, list[str]] = {
    "admin": ["stop", "kill", "force_start_ccs", "register_hook"],
    "operation": ["start", "wake_ccs", "send-safe", "send-direct", "codex start"],
    "info": ["send", "output", "status", "health", "workspace"],
}
# 允许执行 admin/operation 级别的角色
ADMIN_ROLES: set = {"coordinator", "maintainer", "closer"}
OPERATION_ROLES: set = {"coordinator", "maintainer", "engineer", "pg", "devops", "closer"}
# 类别对应的允许执行者
SENSITIVE_PERMISSIONS: dict[str, set] = {
    "admin": ADMIN_ROLES,
    "operation": OPERATION_ROLES,
}

def classify_ccs_command(message: str) -> str:
    """对 ccs 命令分类：admin / operation / info / unknown。

    基于首词精确匹配，避免 send/send-safe 等前缀歧义。
    ponytail: 后续可改为 intent-NLP 分类。"""
    msg = message.strip().lower()
    if not msg:
        return "unknown"
    first = msg.split()[0]
    for cat, patterns in SENSITIVE_CMDS.items():
        for p in patterns:
            if first == p.lower():
                return cat
    return "unknown"

def check_ccs_command_permission(source_role: str, message: str) -> tuple[bool, str]:
    """检查来源角色是否有权限执行某 ccs 命令。

    返回 (allowed: bool, reason: str)。
    """
    cat = classify_ccs_command(message)
    if cat == "unknown" or cat == "info":
        return True, ""
    allowed_roles = SENSITIVE_PERMISSIONS.get(cat, set())
    if source_role in allowed_roles:
        return True, ""
    return False, f"{source_role} 无 {cat} 级权限（需要 {allowed_roles}）"

# ── WL-P0-03: 消息内容敏感度分类 ──────────────────
# 🔴 禁止绕过（必须走 workflow 步骤）
SENSITIVE_KEYWORDS_RED = {"分配", "审批", "决策", "approve", "assign", "decide",
                           "确认完成", "通过审查", "定稿"}
# 🟡 允许但记录
SENSITIVE_KEYWORDS_YELLOW = {"咨询", "状态查询", "通知", "ask", "status", "notify"}

def classify_message_content(text: str) -> str:
    """对 bus 消息内容做敏感度分类: 🔴/🟡/🟢

    ponytail: 基于关键词匹配, 后续可改为 intent 分类。"""
    for kw in SENSITIVE_KEYWORDS_RED:
        if kw in text:
            return "red"
    for kw in SENSITIVE_KEYWORDS_YELLOW:
        if kw in text:
            return "yellow"
    return "green"

# ── WL-P0-03: 工作群组矩阵 ──────────────────────
# 决定哪些角色之间可以进行敏感通信
# 动态从 persona JSON 加载 workgroup 字段
# 兼容旧行为: 无 workgroup 字段时回退为硬编码矩阵
def _load_workgroup_from_personas() -> dict[str, set[str]] | None:
    """从 persona JSON 动态加载工作群组矩阵。

    返回: {role: set(allowed_target_roles)}
    workgroup 字段支持两种格式：
      - 字符串列表: ["coordinator", "lr"]
      - 字典列表: [{"role": "coordinator", "mode": "peer-to-peer"}]
    coordinator 始终有 * 权限。
    若动态加载无法构建有效矩阵，返回 None 让调用方使用硬编码回退。
    """
    matrix = {"coordinator": {"*"}}  # coordinator 兜底可联系所有人
    loaded_any = False

    try:
        for f in SESSION_ROLES_PERSONAS.glob("*.json"):
            data = json.loads(f.read_text())
            name = data.get("name")
            if not name:
                continue
            workgroup = data.get("workgroup", [])
            if not workgroup:
                continue
            # 将 workgroup 条目统一提取为 role 字符串
            roles = set()
            for w in workgroup:
                if isinstance(w, str):
                    roles.add(w)
                elif isinstance(w, dict) and "role" in w:
                    roles.add(w["role"])
            if roles:
                # setdefault 而非赋值：preset 的兜底权限（如 coordinator 的 {"*"}）必须
                # 保留。persona 的 workgroup 是"角色声明的协作面"，不是"权限上限"——
                # 用赋值会让 coordinator 只剩 4 个目标，发不出红消息（实测 2026-10-16）。
                matrix.setdefault(name, set()).update(roles)
                loaded_any = True
    except Exception as e:
        logger.warning("_load_workgroup_from_personas: %s", e)
        return None  # 异常 → 使用硬编码回退

    return matrix if loaded_any else None


def _load_template_edges() -> dict[str, set[str]]:
    """从模板声明的步骤衔接读取协作边（发送方 → 下一步 target_role）。

    WHY 必须并入：persona 的 workgroup 是角色**自述**的协作面，模板边是系统
    实际执行面。主链 21 条边里有 8 条（engineer->qa、pm->engineer、
    product_architect->engineer 等）不在发送方自述里，门禁于是拦住引擎自己
    派发的步骤交接 —— 24h 内 30 条拒绝全部来自这一层。

    只读文件模板：它们由引擎加载执行，角色无法编辑；DB 模板同理由引擎写入。
    读失败即跳过，不影响其余边。
    """
    edges: dict[str, set[str]] = {}
    for f in HERMES_WORKFLOWS.glob("*.json"):
        try:
            steps = (json.loads(Path(f).read_text()) or {}).get("steps") or []
        except (OSError, ValueError) as e:
            logger.warning("_load_template_edges: %s: %s", f, e)
            continue
        for a, b in zip(steps, steps[1:]):
            src, dst = a.get("target_role"), b.get("target_role")
            if src and dst and src != dst:
                edges.setdefault(src, set()).add(dst)
    return edges


# 兼容旧代码: 如果动态加载结果为空，使用硬编码矩阵
_workgroup_dynamic = _load_workgroup_from_personas()
# 动态矩阵中不存在的角色（如无 persona JSON 的系统角色）补充权限
_SUPPLEMENT_MATRIX: dict[str, set[str]] = {
    "workflow_engine": {"coordinator", "lr", "product_architect", "engineer", "pg",
                         "reviewer", "qa", "scout", "devops", "maintainer"},
}
if _workgroup_dynamic:
    _workgroup_dynamic.update(_SUPPLEMENT_MATRIX)
    # 模板边是执行面，与自述取并集：任一来源允许即放行。
    for _src, _dsts in _load_template_edges().items():
        _workgroup_dynamic.setdefault(_src, set()).update(_dsts)
# 自动化来源：受信系统组件（非人类、非角色）。这些来源的消息由其自身触发逻辑
# 决定去向，不走三源验证的 workgroup 检查——否则 daemon 发的消息会被静默丢弃。
# routing_daemon 曾缺失此项：cron_schedule 与 ccs_send_fallback 全部被拒
# （实测 #374753 "→engineer 失败: 三源验证拒绝"），角色因此收不到任何定时任务。
_AUTOMATED_SOURCES = frozenset({
    "cli", "loop", "pipeline", "cron-worker", "workflow_engine", "routing_daemon",
})

WORKGROUP_MATRIX: dict[str, set[str]] = _workgroup_dynamic if _workgroup_dynamic else {
    "coordinator": {"*"},
    "lr": {"*"},
    "pm": {"coordinator", "lr", "product_architect", "pg", "qa", "writer"},
    "product_architect": {"coordinator", "lr", "pm", "pg", "reviewer", "qa"},
    "pg": {"coordinator", "lr", "pm", "product_architect", "reviewer", "qa", "devops"},
    "engineer": {"coordinator", "lr", "pm", "pg", "reviewer"},
    "reviewer": {"coordinator", "lr", "pm", "product_architect", "pg", "qa"},
    "qa": {"coordinator", "lr", "pm", "pg", "reviewer", "devops"},
    "devops": {"coordinator", "lr", "pg", "qa"},
    "writer": {"coordinator", "pm", "lr"},
    "maintainer": {"coordinator", "lr", "pm", "pg", "devops"},
    "scout": {"coordinator", "lr", "pm"},
    "closer": {"coordinator", "lr"},
    "workflow_engine": {"coordinator", "lr", "product_architect", "engineer", "pg",
                         "reviewer", "qa", "scout", "devops", "maintainer"},
}

# 审计上限: 每角色每小时可发送的敏感操作次数
SENSITIVE_RATE_LIMIT = 5  # 次/小时


class CrossRoleRouter:
    """跨角色路由拦截器 — 三源消息溯源。"""

    def __init__(self, db_path: str = None):
        self.db_path = Path(db_path) if db_path else DB_PATH

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workflow_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_instance_id TEXT,
                task_id TEXT,
                action TEXT NOT NULL,
                actor TEXT NOT NULL,
                detail TEXT,
                ts REAL NOT NULL
            );
        """)
        # 去重查询按 ts 窗口取候选（300s ≈ 250 行），无索引时会全表扫描
        # （实测 42k 行 6.29ms/次，且每次 ccs send 都跑）。索引仅 8B/行。
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wf_logs_ts ON workflow_logs(ts)")
        return conn

    def intercept(self, source: str, target: str, message: str) -> bool:
        """拦截跨角色消息，三源验证后决定放行/拒绝。

        返回 True 放行，False 拒绝。
        """
        # 同角色消息 & 系统角色直接放行（放行前先分类并落库，供速率统计按敏感度过滤）
        if source == target or source in _AUTOMATED_SOURCES:
            self._log_cross_role_send(source, target, message)
            return True

        # WL-P0-01: 消息内容敏感度分类
        msg_sensitivity = classify_message_content(message)
        self._log_cross_role_send(source, target, message, msg_sensitivity)
        allowed_targets = WORKGROUP_MATRIX.get(source, set())

        # 全类型消息统一校验: 未知来源且不在 workgroup → 拒绝
        if not allowed_targets and source not in _AUTOMATED_SOURCES:
            self._log_violation(source, target, message,
                                f"unknown source {source}, not in workgroup matrix")
            return False

        # 🟢 绿消息（自由）→ 放行
        if msg_sensitivity == "green":
            return True

        # 🟡 黄消息（允许但记录）→ 已由 _log_cross_role_send 记录, 放行
        if msg_sensitivity == "yellow":
            return True

        # 🔴 红消息（决策/分配/审批）→ 全套门禁校验
        # 1. 工作群组校验: 非矩阵角色发 🔴 消息被拦截
        if "*" not in allowed_targets and target not in allowed_targets:
            self._log_violation(source, target, message,
                                "not in workgroup matrix, red message blocked")
            return False

        # 2. 审计上限: 每小时超过5条 🔴 消息 → 强制拦截, 提示创建task
        # 只统计红消息：绿/黄不计入，否则正常协作量就能耗尽敏感额度。
        # 历史行无 sensitivity 字段（修复前写入）→ 用 json_extract 判 NULL，
        # 它们在 detail 里没有该键，等价于不计入（保守放行，不追溯拦住旧流量）。
        conn = self._get_conn()
        try:
            hour_ago = time.time() - 3600
            count = conn.execute(
                "SELECT COUNT(*) as c FROM workflow_logs "
                "WHERE actor=? AND action='cross_role_send' AND ts>? "
                "AND json_extract(detail, '$.sensitivity')='red'",
                (source, hour_ago)
            ).fetchone()["c"]
            if count > SENSITIVE_RATE_LIMIT:
                msg = (f"rate limit: {count} red msgs in last hour — "
                       f"请使用 create_task() 创建任务后发送 🔴 消息")
                self._log_violation(source, target, message, msg)
                return False
        finally:
            conn.close()

        # 3. 三源验证: 验证消息来源真实性
        evidence = self._source_triple_check(source, message)
        if not evidence["sources_ok"]:
            self._log_violation(source, target, message, evidence["detail"])
            return False

        return True

    def _source_triple_check(self, claimed_source: str, message: str) -> dict:
        """三源验证：bus --src / DB assigner / sentinel。

        返回 dict: {sources_ok: bool, detail: str, match_count: int, total_checked: int}
        """
        matches = 0
        total = 0
        details = []

        # 源1: bus --src 查询。只有 "matched" 才算一源——搜到结果但不含
        # claimed_source 若也计分，任何 bus 命中 + 同名会话即凑满 2 源，
        # 三源验证形同虚设（实测 2026-10-16）。
        total += 1
        bus_src = self._check_bus_source(claimed_source, message)
        if bus_src == "matched":
            matches += 1
            details.append("bus_src=matched")
        else:
            details.append(f"bus_src={bus_src or 'not_found'}")

        # 源2: DB assigner 查询
        total += 1
        db_assigner = self._check_db_assigner(claimed_source)
        if db_assigner == "matched":
            matches += 1
            details.append(f"db_assigner={db_assigner}")
        else:
            details.append("db_assigner=not_found")

        # 源3: sentinel 角色状态
        total += 1
        sentinel_ok = self._check_sentinel(claimed_source)
        if sentinel_ok:
            matches += 1
            details.append("sentinel=exists")
        else:
            details.append("sentinel=not_found")

        sources_ok = matches >= 2
        return {
            "sources_ok": sources_ok,
            "match_count": matches,
            "total_checked": total,
            "detail": "; ".join(details),
        }

    def _check_bus_source(self, claimed_source: str, message: str) -> str:
        """源1: 在 bus 中检索同一消息，看是否有 src==claimed_source 的事实。

        三层旧错误叠加，使本源统计上恒为空（R110 需求文档）：
        1. 消息以 `[workflow]` 开头，方括号不在 blackboard 的清洗表里 → FTS5 语法错。
        2. 旧实现解析 `--src`/`src=` 文本，而 search 输出是 `[id] (cat) title`。
        3. blackboard.search 把多词查询用 `_` 拼成**单个 token**，多词必 0 命中
           （实测：4 词组合 0 条，`wf_sub_xxx` 单 token 20 条）。
        故只认 wf_/task_ 这类高熵 ID。普通词兜底会假阳性：`hello there` 里的
        "there" 在 bus 里必然命中大量无关事实，凑成「有源」——本源自查不得为
        通过而通过，认不出就返回空，交给其余两源定夺。
        """
        token = next((t for t in re.split(r"[^\w一-鿿]+", message or "")
                      if t.startswith(("wf_", "task_")) and len(t) >= 6), "")
        if not token:
            return ""
        try:
            r = subprocess.run(
                ["python3", str(BUS_CLIENT), "search", token,
                 "--limit", "50", "--json"],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode != 0:
                return ""
            facts = (json.loads(r.stdout or "{}") or {}).get("facts") or []
            return ("matched" if any(f.get("src") == claimed_source for f in facts)
                    else "")
        except Exception as e:
            # timeout / JSONDecodeError / OSError 一律 fail-closed
            logger.warning("_check_bus_source: %s", e)
            return ""

    def _check_db_assigner(self, claimed_source: str) -> str:
        """源2: 该角色是否持有未终结的工作流实例。

        旧实现取 `ORDER BY created_at DESC LIMIT 10` 的 assigner 集合，与系统规模
        脱钩：实测 24h 内 9 个角色当过 assigner，10 行窗口只覆盖 4 个，ccs-monitor
        等角色即使有活跃工作流也永远匹配不上（假阴性 → 真正当班的消息被拒）。
        改为定向查询，窗口大小不再参与判定。

        未终结 = running | delivery_blocked（后者只是投递达上限暂停，实例仍活）。
        枚举值以生产库实测为准（无 pending/step_done_ready）。
        """
        try:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT 1 FROM workflow_instances WHERE assigner=? "
                    "AND status IN ('running','delivery_blocked') LIMIT 1",
                    (claimed_source,),
                ).fetchone()
                return "matched" if row else ""
            finally:
                conn.close()
        except Exception as e:
            logger.warning("_check_db_assigner: %s", e)
            return ""

    def _check_sentinel(self, claimed_source: str) -> bool:
        """源3: 检查 tmux session 是否存在（含多 instance，单次 tmux list-sessions）。"""
        from tmux_ops import make_tmux_name, MAX_INSTANCES
        try:
            r = subprocess.run(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                capture_output=True, text=True, timeout=5)
            if r.returncode != 0:
                return False
            sessions = set(r.stdout.strip().split("\n"))
        except Exception as e:
            logger.warning("_check_sentinel: %s", e)
            return False
        # 检查主实例
        if make_tmux_name(claimed_source, 0) in sessions:
            return True
        # cdx- 主实例
        if f"cdx-{claimed_source}" in sessions:
            return True
        # 检查扩展实例 (1-16)
        for i in range(1, MAX_INSTANCES + 1):
            if make_tmux_name(claimed_source, i) in sessions:
                return True
            if f"cdx-{claimed_source}-{i}" in sessions:
                return True
        return False

    def check_send_permission(self, source: str, target: str,
                               category: str = "") -> bool:
        """检查发送权限（兼容旧接口）。"""
        return self.intercept(source, target, category)

    def _log_cross_role_send(self, source: str, target: str, message: str,
                             sensitivity: str = ""):
        """记录跨角色消息到 workflow_logs。

        sensitivity 供速率限制按红消息过滤——不记录的话统计会把绿/黄消息
        一并计入，正常协作量即可耗尽 🔴 额度（实测 2026-10-16）。

        非红消息 300s 窗口内相同内容去重（#432065）：workflow_engine 重投
        同一 /goal 曾单条落库 3494 行、全表 86% 为此类，无上限膨胀。红消息
        必须逐条落库——速率限制 COUNT 依赖精确行数，去重会削弱门禁。
        """
        detail = json.dumps({
            "source": source, "target": target,
            "message_preview": _redact_secrets(message)[:200],
            "sensitivity": sensitivity,
        }, ensure_ascii=False)
        conn = self._get_conn()
        try:
            if sensitivity != "red":
                dup = conn.execute(
                    "SELECT 1 FROM workflow_logs WHERE action='cross_role_send' "
                    "AND actor=? AND detail=? AND ts>? LIMIT 1",
                    (source, detail, time.time() - 300)
                ).fetchone()
                if dup:
                    return
            conn.execute(
                "INSERT INTO workflow_logs (workflow_instance_id, task_id, "
                "action, actor, detail, ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (None, None, "cross_role_send", source, detail, time.time())
            )
            conn.commit()
        finally:
            conn.close()

    def _log_violation(self, source: str, target: str, message: str, detail: str):
        """记录验证失败违规日志。"""
        conn = self._get_conn()
        try:
            conn.execute(
                "INSERT INTO workflow_logs (workflow_instance_id, task_id, "
                "action, actor, detail, ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (None, None, "source_verification_denied", source,
                 json.dumps({
                     "source": source, "target": target,
                     "message_preview": _redact_secrets(message)[:200],
                     "detail": _redact_secrets(detail),
                     "violation": True,
                 }, ensure_ascii=False),
                 time.time())
            )
            conn.commit()
        finally:
            conn.close()
