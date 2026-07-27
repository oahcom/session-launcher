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
import sqlite3
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

from paths import WORKFLOWS_DB as DB_PATH, BUS_CLIENT, SESSION_ROLES_PERSONAS

# ── 敏感操作分类 ─────────────────────────────────
# 按影响范围分级：info / operation / admin
SENSITIVE_CMDS: dict[str, list[str]] = {
    "admin": ["stop", "kill", "force_start_ccs", "register_hook"],
    "operation": ["start", "wake_ccs", "send-safe", "send-direct", "codex start"],
    "info": ["send", "send-safe", "output", "status", "health", "workspace"],
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

    ponytail: 基于前缀匹配，后续可改为 intent-NLP 分类。"""
    msg = message.strip().lower()
    if not msg:
        return "unknown"
    cmds = msg.split()
    for cat, patterns in SENSITIVE_CMDS.items():
        for p in patterns:
            if msg.startswith(p.lower()):
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
                matrix[name] = roles
                loaded_any = True
    except Exception as e:
        logger.warning("_load_workgroup_from_personas: %s", e, exc_info=True)
        return None  # 异常 → 使用硬编码回退

    return matrix if loaded_any else None


# 兼容旧代码: 如果动态加载结果为空，使用硬编码矩阵
_workgroup_dynamic = _load_workgroup_from_personas()
# 动态矩阵中不存在的角色（如无 persona JSON 的系统角色）补充权限
_SUPPLEMENT_MATRIX: dict[str, set[str]] = {
    "workflow_engine": {"coordinator", "lr", "product_architect", "engineer", "pg",
                         "reviewer", "qa", "scout", "devops", "maintainer"},
}
if _workgroup_dynamic:
    _workgroup_dynamic.update(_SUPPLEMENT_MATRIX)
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
        return conn

    def intercept(self, source: str, target: str, message: str) -> bool:
        """拦截跨角色消息，三源验证后决定放行/拒绝。

        返回 True 放行，False 拒绝。
        """
        self._log_cross_role_send(source, target, message)

        # 同角色消息直接放行
        if source == target or source in ("cli", "loop", "pipeline", "cron-worker"):
            return True

        # WL-P0-01: 消息内容敏感度分类
        msg_sensitivity = classify_message_content(message)
        allowed_targets = WORKGROUP_MATRIX.get(source, set())

        # 全类型消息统一校验: 未知来源且不在 workgroup → 拒绝
        if not allowed_targets and not source in ("cli", "loop", "pipeline", "cron-worker"):
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
                                f"not in workgroup matrix, red message blocked")
            return False

        # 2. 审计上限: 每小时超过5条 🔴 消息 → 强制拦截, 提示创建task
        conn = self._get_conn()
        try:
            hour_ago = time.time() - 3600
            count = conn.execute(
                "SELECT COUNT(*) as c FROM workflow_logs "
                "WHERE actor=? AND action='cross_role_send' AND ts>?",
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

        # 源1: bus --src 查询
        total += 1
        bus_src = self._check_bus_source(claimed_source, message)
        if bus_src:
            matches += 1
            details.append(f"bus_src={bus_src}")
        else:
            details.append("bus_src=not_found")

        # 源2: DB assigner 查询
        total += 1
        db_assigner = self._check_db_assigner(claimed_source)
        if db_assigner:
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
        """源1: 从 message 提取关键词搜索 bus，检查 --src 是否匹配 claimed_source。"""
        # 提取消息中的任务关键词（取前20个非空格字符）
        keywords = message.strip().split()[:3]
        if not keywords:
            return ""
        query = " ".join(keywords)

        try:
            r = subprocess.run(
                ["python3", str(BUS_CLIENT), "search", query],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode != 0:
                return ""
            # 搜索结果格式: [id] (cat) text
            for line in r.stdout.split("\n"):
                if "--src" in line or "src=" in line:
                    found_src = line.strip()
                    if claimed_source in found_src:
                        return "matched"
            # 如果搜索到任何结果，返回 "found" 但不一定匹配
            if r.stdout.strip() and not r.stdout.strip().startswith("No results"):
                return "partial"
            return ""
        except Exception as e:
            logger.warning("_check_bus_source: %s", e, exc_info=True)
            return ""

    def _check_db_assigner(self, claimed_source: str) -> str:
        """源2: 查询 workflows.db 最近 10 条任务，找 assigner 匹配。"""
        try:
            conn = self._get_conn()
            try:
                # 检查表是否存在
                tbl = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='workflow_instances'"
                ).fetchone()
                if not tbl:
                    return ""
                rows = conn.execute(
                    """SELECT wi.assigner FROM workflow_instances wi
                       ORDER BY wi.created_at DESC LIMIT 10"""
                ).fetchall()
                for r in rows:
                    if r["assigner"] == claimed_source:
                        return "matched"
                if rows:
                    return ""  # 有记录但无匹配 → 不计数
                return ""
            finally:
                conn.close()
        except Exception as e:
            logger.warning("_check_db_assigner: %s", e, exc_info=True)
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
            logger.warning("_check_sentinel: %s", e, exc_info=True)
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

    def _log_cross_role_send(self, source: str, target: str, message: str):
        """记录跨角色消息到 workflow_logs。"""
        conn = self._get_conn()
        try:
            conn.execute(
                "INSERT INTO workflow_logs (workflow_instance_id, task_id, "
                "action, actor, detail, ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (None, None, "cross_role_send", source,
                 json.dumps({
                     "source": source, "target": target,
                     "message_preview": message[:200],
                 }, ensure_ascii=False),
                 time.time())
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
                     "message_preview": message[:200],
                     "detail": detail,
                     "violation": True,
                 }, ensure_ascii=False),
                 time.time())
            )
            conn.commit()
        finally:
            conn.close()
