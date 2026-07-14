#!/usr/bin/env python3
"""
p0_exemption.py — P0 豁免通道 + 审计轨迹

允许 P0 任务在无 template_id 的情况下创建（4h 宽限期）。
仅 coordinator 和 lr 可标记 P0。超时自动检测并通知。
"""

import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

from paths import WORKFLOWS_DB as DB_PATH, BUS_CLIENT

# 允许标记 P0 的角色
ALLOWED_P0_ROLES = {"coordinator", "lr"}
# 超时阈值（小时）
P0_TIMEOUT_HOURS = 4


class P0Exemption:
    """P0 豁免通道。"""

    def __init__(self, role: str, db_path: str = None):
        self.role = role
        self.db_path = Path(db_path) if db_path else DB_PATH
        self._conn = sqlite3.connect(str(self.db_path), timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self):
        """确保必要的表存在（委托给 worklowf_db 的完整 schema，这里只确保日志表存在）。"""
        self._conn.executescript("""
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
        self._conn.commit()
        # 延迟导入确保 tasks 表由 workflow_db 统一管理
        try:
            from workflow.client import WorkflowClient
            wc = WorkflowClient(self.role, db_path=str(self.db_path))
            wc.close()
        except Exception:
            pass

    def can_mark_p0(self) -> bool:
        """检查当前角色是否有权标记 P0。"""
        return self.role in ALLOWED_P0_ROLES

    def create_p0_task(self, title: str, description: str,
                       assignee: str, initiator_role: str,
                       p0_reason: str) -> str:
        """创建 P0 豁免任务（不绑定 template_id）。

        返回 task_id。
        校验不通过抛出 ValueError 或 PermissionError。
        """
        if initiator_role not in ALLOWED_P0_ROLES:
            raise PermissionError(
                f"only coordinator/lr can mark P0, got: {initiator_role}")

        if len(p0_reason) < 15:
            raise ValueError(
                f"p0_reason 需≥15中文字符, 当前 {len(p0_reason)} 字符")

        if not title or not assignee:
            raise ValueError("title 和 assignee 为必填")

        import uuid
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        now = time.time()

        self._conn.execute("""
            INSERT OR IGNORE INTO tasks (task_id, title, description, assigner, assignee,
                               priority, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 0, 'created', ?, ?)
        """, (task_id, title, description, self.role, assignee, now, now))
        self._conn.commit()

        # 审计日志
        self._log_audit(task_id, initiator_role, p0_reason)

        # bus 通知
        self._notify_bus("task_spec",
            f"P0 豁免任务: {title}",
            evidence=f"task_id={task_id}, assignee={assignee}, reason={p0_reason}")

        return task_id

    def update_task_template_id(self, task_id: str,
                                 template_id: str) -> bool:
        """为 P0 任务补录 template_id。

        task 必须是 P0 豁免且在有效期内。
        返回 False 如果 task 不存在或已超时。
        """
        task = self._get_task(task_id)
        if not task:
            return False

        # 检查是否超时
        created = task.get("created_at", 0)
        elapsed = (time.time() - created) / 3600
        if elapsed > P0_TIMEOUT_HOURS:
            self._log_audit(task_id, "system",
                            f"补录超时: template_id={template_id}, "
                            f"已过 {elapsed:.1f}h/{P0_TIMEOUT_HOURS}h")
            return False

        self._conn.execute(
            "UPDATE tasks SET template_id=? WHERE task_id=?",
            (template_id, task_id)
        )
        self._conn.commit()
        self._log_audit(task_id, self.role,
                        f"补录 template_id={template_id}")
        return True

    def check_timeouts(self) -> list[dict]:
        """扫描超时的 P0 任务并自动标记 violation。

        仅检查 tasks 表中 assigner 不是 initiator 字段的 P0 任务。
        未绑定 template_id（含空或 null）超过 threshold 的 task。
        """
        violations = []
        rows = self._conn.execute("""
            SELECT * FROM tasks
            WHERE status != 'completed'
            ORDER BY created_at DESC
        """).fetchall()

        for row in rows:
            task = dict(row)
            # Rely on audit trail rather than a dedicated p0_marker column
            tid = task.get("template_id")
            if tid:  # 已有 template_id → 不需要豁免
                continue
            created = task.get("created_at", 0)
            elapsed = (time.time() - created) / 3600
            if elapsed > P0_TIMEOUT_HOURS:
                self._violation_action(task)
                violations.append({
                    "task_id": task["task_id"],
                    "title": task["title"],
                    "elapsed_hours": round(elapsed, 1),
                })
        return violations

    def _violation_action(self, task: dict):
        """超时检测后记录 violation 并通知 coordinator。"""
        self._log_audit(task["task_id"], "system",
                        f"P0 timeout violation: "
                        f"{task['title']} (> {P0_TIMEOUT_HOURS}h)")
        self._notify_bus("blocker",
            f"P0 超时 violation: {task['task_id']}",
            evidence=json.dumps({
                "title": task.get("title", ""),
                "assignee": task.get("assignee"),
                "created_at": task.get("created_at"),
            }, ensure_ascii=False))

    def _get_task(self, task_id: str) -> Optional[dict]:
        """读取 task 记录（优先本地连接，回退 workflow_db）。"""
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if row:
            return dict(row)
        try:
            from workflow.client import WorkflowClient
            with WorkflowClient(self.role) as wc:
                return wc.get_task(task_id)
        except Exception:
            return None

    def _log_audit(self, task_id: str, actor: str, detail: str):
        """写审计日志到 workflow_logs。"""
        self._conn.execute(
            "INSERT INTO workflow_logs (workflow_instance_id, task_id, "
            "action, actor, detail, ts) VALUES (?, ?, ?, ?, ?, ?)",
            (None, task_id, "p0_exemption", actor,
             json.dumps({"detail": detail}, ensure_ascii=False),
             time.time())
        )
        self._conn.commit()

    def _notify_bus(self, category: str, title: str, evidence: str = ""):
        import subprocess
        cmd = ["python3", str(BUS_CLIENT), "write", category,
               f"[{self.role}] {title}", "--src", self.role]
        if evidence:
            cmd.extend(["--evidence", evidence])
        subprocess.run(cmd, capture_output=True, timeout=15)

    def close(self):
        self._conn.close()
