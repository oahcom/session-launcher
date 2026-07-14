#!/usr/bin/env python3
"""
workflow_client.py — CCS 角色使用的工作流客户端。

三层架构：
  Workflow Template（可复用模板）→ Workflow Instance（具体执行）→ Task（目标）

所有操作一步可达。
"""

import json
import sqlite3
import time
import warnings
from pathlib import Path
from typing import Optional

from paths import WORKFLOWS_DB as DB_PATH
from paths import BUS_CLIENT
CCS_CLI = Path(__file__).resolve().parent / "ccs.py"


class WorkflowClient:
    """CCS 角色使用的工作流客户端。"""

    def __init__(self, role: str, db_path: str = None):
        self.role = role
        self.db_path = Path(db_path) if db_path else DB_PATH
        self._conn = sqlite3.connect(str(self.db_path), timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def _init_schema(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS workflow_templates (
                template_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                steps_json TEXT NOT NULL,
                steps_mermaid TEXT,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workflow_instances (
                instance_id TEXT PRIMARY KEY,
                template_id TEXT,
                task_id TEXT NOT NULL,
                assigner TEXT NOT NULL,
                assignee TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                current_step_id TEXT,
                step_results TEXT DEFAULT '{}',
                created_at REAL NOT NULL,
                completed_at REAL,
                FOREIGN KEY (task_id) REFERENCES tasks(task_id),
                FOREIGN KEY (template_id) REFERENCES workflow_templates(template_id)
            );
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                assigner TEXT NOT NULL,
                assignee TEXT,
                priority INTEGER DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'created',
                current_workflow_id TEXT,
                progress TEXT DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                completed_at REAL,
                tags TEXT DEFAULT '[]',
                context TEXT DEFAULT '{}'
            );
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

    def _log(self, wf_id: str = None, task_id: str = None,
             action: str = "", detail: str = ""):
        self._conn.execute(
            "INSERT INTO workflow_logs (workflow_instance_id, task_id, action, actor, detail, ts) "
            "VALUES (?, ?, ?, ?, ?, ?)", (wf_id, task_id, action, self.role, detail, time.time())
        )
        self._conn.commit()

    # ── Template ──────────────────────────────────────────────

    def list_templates(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM workflow_templates ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def find_template(self, name: str) -> Optional[dict]:
        row = self._conn.execute("SELECT * FROM workflow_templates WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    # ── Task CRUD ──────────────────────────────────────────────

    def create_task(self, title: str, description: str = "",
                    assignee: str = None, priority: int = 0,
                    template_id: str = None) -> str:
        """V1: 创建任务（过渡期兼容签名）。

        传 template_id=None 时发 deprecation 警告。
        内部委托给 _create_task_impl。
        """
        if template_id is None:
            warnings.warn(
                "create_task without template_id is deprecated. "
                "Use create_task_v2(title, assignee, template_id, initiator_role).",
                DeprecationWarning, stacklevel=2
            )
        return self._create_task_impl(title, description, assignee,
                                       priority, template_id)

    def create_task_v2(self, title: str, assignee: str,
                       template_id: str, initiator_role: str,
                       description: str = "") -> tuple:
        """V2: 创建任务 + 模板门禁校验。

        返回 (task_id, wf_id) 二元组。
        校验不通过时抛出 ValueError 或 PermissionError。
        """
        # 门禁校验
        from workflow_gate import Gate
        Gate(str(self.db_path)).validate_create_task(
            template_id, initiator_role, assignee)

        task_id = self._create_task_impl(title, description, assignee,
                                         0, template_id)

        # 自动创建 workflow_instance
        wf_id = self._create_workflow_instance(task_id, template_id, assignee)

        return (task_id, wf_id)

    def _create_task_impl(self, title: str, description: str,
                          assignee: str, priority: int,
                          template_id: str = None) -> str:
        """内部实现：插入 task 记录 + 通知 assignee。"""
        import uuid
        import subprocess
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        now = time.time()

        # 检查表是否存在 template_id 列
        cols = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(tasks)").fetchall()}

        if template_id and "template_id" in cols:
            self._conn.execute("""
                INSERT INTO tasks (task_id, title, description, assigner, assignee,
                                   priority, status, created_at, updated_at, template_id)
                VALUES (?, ?, ?, ?, ?, ?, 'created', ?, ?, ?)
            """, (task_id, title, description, self.role, assignee,
                  priority, now, now, template_id))
        else:
            self._conn.execute("""
                INSERT INTO tasks (task_id, title, description, assigner, assignee,
                                   priority, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'created', ?, ?)
            """, (task_id, title, description, self.role, assignee,
                  priority, now, now))
        self._conn.commit()
        self._log(task_id=task_id, action="created",
                  detail=f"title={title}, template_id={template_id}")

        # 瞬时通知 assignee（ccs send，<1ms）
        ccs_ok = True
        if assignee and assignee != self.role:
            result = subprocess.run(
                ["python3", str(CCS_CLI), "send", assignee,
                 f"[{self.role}] 你有新任务: {title} — check_task() 查看详情"],
                capture_output=True, timeout=15,
            )
            ccs_ok = result.returncode == 0

        # bus task_spec 知识存档
        evidence = f"assignee={assignee}, task_id={task_id}"
        if not ccs_ok:
            evidence += ", ccs_send_failed=true"
        self.notify("task_spec", f"创建任务: {title}", evidence=evidence)

        return task_id

    def _create_workflow_instance(self, task_id: str,
                                  template_id: str,
                                  assignee: str) -> str:
        """创建关联的 workflow_instance。"""
        wf_id = f"wf_{int(time.time()*1000) % 100000000}"
        now = time.time()
        self._conn.execute("""
            INSERT INTO workflow_instances (instance_id, template_id, task_id,
                                            assigner, assignee, status,
                                            current_step_id, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', 's1', ?)
        """, (wf_id, template_id, task_id, self.role,
              assignee, now))
        self._conn.commit()

        # 更新 Task 的 current_workflow_id
        self._conn.execute(
            "UPDATE tasks SET current_workflow_id=?, status='in_progress' "
            "WHERE task_id=?", (wf_id, task_id))
        self._conn.commit()

        self._log(wf_id=wf_id, task_id=task_id, action="created",
                  detail=f"template_id={template_id}, assignee={assignee}")
        return wf_id

    def get_task(self, task_id: str) -> Optional[dict]:
        row = self._conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            return None
        task = dict(row)
        task["progress"] = self._compute_progress(task_id)
        return task

    def _compute_progress(self, task_id: str) -> dict:
        rows = self._conn.execute(
            "SELECT * FROM workflow_instances WHERE task_id=? ORDER BY created_at",
            (task_id,)
        ).fetchall()
        if not rows:
            return {"workflow_count": 0, "completed": 0, "current": None}
        completed = sum(1 for r in rows if dict(r)["status"] == "completed")
        running = [dict(r) for r in rows if dict(r)["status"] == "running"]
        pending = [dict(r) for r in rows if dict(r)["status"] == "pending"]
        current = running[0] if running else (pending[0] if pending else None)
        total = len(rows)
        return {
            "workflow_count": total,
            "completed": completed,
            "current": {
                "instance_id": current["instance_id"] if current else None,
                "template_id": current["template_id"] if current else None,
                "current_step": current["current_step_id"] if current else None,
            } if current else None,
            "percent": int((completed / total) * 100) if total > 0 else 0,
        }

    def list_tasks(self, status: str = None, assignee: str = None) -> list[dict]:
        query = "SELECT * FROM tasks WHERE 1=1"
        params = []
        if status:
            query += " AND status=?"
            params.append(status)
        if assignee:
            query += " AND assignee=?"
            params.append(assignee)
        query += " ORDER BY priority DESC, created_at DESC"
        rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def delete_task(self, task_id: str) -> bool:
        task = self.get_task(task_id)
        if not task:
            return False
        # 铁律：下达者（assigner）不能删除任务，只有接收者或系统可以
        if task['assigner'] == self.role:
            return False
        self._log(task_id=task_id, action="deleted")
        self._conn.execute("DELETE FROM workflow_instances WHERE task_id=?", (task_id,))
        self._conn.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
        self._conn.commit()
        return True

    # ── Workflow Instance CRUD ──────────────────────────────────

    def create(self, assignee: str, task_description: str,
               workflow_json: dict = None, task_id: str = None) -> str:
        wf_id = f"wf_{int(time.time()*1000) % 100000000}"
        now = time.time()
        # 自动生成 task_id（若未提供），并创建 tasks 记录
        if not task_id:
            task_id = f"task_{int(now * 1000) % 100000000}"
            self._conn.execute("""
                INSERT OR IGNORE INTO tasks (task_id, title, description, assigner, assignee,
                                             status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """, (task_id, task_description, task_description, self.role, assignee, now, now))
        self._conn.execute("""
            INSERT INTO workflow_instances (instance_id, task_id, assigner, assignee,
                                            status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
        """, (wf_id, task_id, self.role, assignee, now))
        self._conn.commit()

        # 更新 Task 的 current_workflow_id 和状态
        if task_id:
            self._conn.execute(
                "UPDATE tasks SET current_workflow_id=?, status='in_progress' WHERE task_id=?",
                (wf_id, task_id)
            )
            self._conn.commit()

        self._log(wf_id=wf_id, task_id=task_id, action="created", detail=f"assignee={assignee}")
        return wf_id

    def get(self, wf_id: str) -> Optional[dict]:
        row = self._conn.execute("SELECT * FROM workflow_instances WHERE instance_id=?", (wf_id,)).fetchone()
        return dict(row) if row else None

    def check_task(self) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT wi.*, t.priority FROM workflow_instances wi "
            "JOIN tasks t ON wi.task_id = t.task_id "
            "WHERE wi.assignee=? AND wi.status IN ('pending', 'running') "
            "ORDER BY t.priority DESC, wi.created_at ASC LIMIT 1", (self.role,)
        ).fetchone()
        return dict(row) if row else None

    def list_my_tasks(self, status: str = None) -> list[dict]:
        query = "SELECT * FROM workflow_instances WHERE assignee=?"
        params = [self.role]
        if status:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY created_at DESC"
        rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def start(self, wf_id: str, step_id: str):
        self._conn.execute(
            "UPDATE workflow_instances SET status='running', current_step_id=? WHERE instance_id=?",
            (step_id, wf_id)
        )
        self._conn.commit()
        # 从实例获取 task_id 放入 evidence（confirm_delivery 双信号②来源）
        # notify() 会自动加 [{self.role}] 前缀，title 不再重复加
        wf = self.get(wf_id)
        task_id = wf.get("task_id", "") if wf else ""
        evidence = f"task={task_id}" if task_id else ""
        self.notify("workflow", f"已接单 {wf_id}", evidence=evidence)
        self._log(wf_id=wf_id, action="started", detail=f"step={step_id}")

    def complete(self, wf_id: str, summary: str, files: list = None):
        files_str = ", ".join(files) if files else ""
        self._conn.execute(
            "UPDATE workflow_instances SET status='completed', completed_at=?, "
            "step_results=? WHERE instance_id=?",
            (time.time(), json.dumps({"summary": summary, "files": files_str}), wf_id)
        )
        self._conn.commit()
        
        # 同步 Task 状态
        wf = self.get(wf_id)
        if wf and wf.get('task_id'):
            self._sync_task_from_workflows(wf['task_id'])
        
        self._log(wf_id=wf_id, action="completed", detail=f"summary={summary}")

    def _sync_task_from_workflows(self, task_id: str):
        """从 workflow_instances 同步 Task 状态。"""
        rows = self._conn.execute(
            "SELECT status FROM workflow_instances WHERE task_id=?", (task_id,)
        ).fetchall()
        statuses = [dict(r)["status"] for r in rows]

        if not statuses:
            return

        # 如果所有工作流都完成，Task 完成
        if all(s == "completed" for s in statuses):
            task_status = "completed"
        # 如果任何工作流失败，Task 失败
        elif any(s == "failed" for s in statuses):
            task_status = "failed"
        # 如果有 running 或 pending，Task 进行中
        elif any(s in ("running", "pending") for s in statuses):
            task_status = "in_progress"
        else:
            task_status = "completed"

        self._conn.execute("UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
                          (task_status, time.time(), task_id))
        self._conn.commit()

    def fail(self, wf_id: str, reason: str):
        self._conn.execute(
            "UPDATE workflow_instances SET status='failed', completed_at=?, "
            "step_results=? WHERE instance_id=?",
            (time.time(), json.dumps({"reason": reason}), wf_id)
        )
        self._conn.commit()
        # 同步 Task 状态
        wf = self.get(wf_id)
        if wf and wf.get('task_id'):
            self._sync_task_from_workflows(wf['task_id'])
        self._log(wf_id=wf_id, action="failed", detail=f"reason={reason}")

    def cancel(self, wf_id: str, reason: str = ""):
        self._conn.execute(
            "UPDATE workflow_instances SET status='cancelled', completed_at=?, "
            "step_results=? WHERE instance_id=?",
            (time.time(), json.dumps({"reason": reason}), wf_id)
        )
        self._conn.commit()
        # 同步 Task 状态
        wf = self.get(wf_id)
        if wf and wf.get('task_id'):
            self._sync_task_from_workflows(wf['task_id'])
        self._log(wf_id=wf_id, action="cancelled", detail=f"reason={reason}")

    def delete(self, wf_id: str) -> bool:
        wf = self.get(wf_id)
        if not wf:
            return False
        # 铁律：下达者（assigner）不能删除工作流实例
        if wf['assigner'] == self.role:
            return False
        self._log(wf_id=wf_id, action="deleted")
        self._conn.execute("DELETE FROM workflow_instances WHERE instance_id=?", (wf_id,))
        self._conn.commit()
        return True

    def archive(self, wf_id: str) -> bool:
        self._conn.execute("UPDATE workflow_instances SET status='archived' WHERE instance_id=?", (wf_id,))
        self._conn.commit()
        self._log(wf_id=wf_id, action="archived")
        return True

    def list_all(self, status: str = None, limit: int = 50) -> list[dict]:
        query = "SELECT * FROM workflow_instances WHERE 1=1"
        params = []
        if status:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # ── 日志 ──────────────────────────────────────────────────

    def get_logs(self, wf_id: str = None, task_id: str = None) -> list[dict]:
        query = "SELECT * FROM workflow_logs WHERE 1=1"
        params = []
        if wf_id:
            query += " AND workflow_instance_id=?"
            params.append(wf_id)
        if task_id:
            query += " AND task_id=?"
            params.append(task_id)
        query += " ORDER BY ts DESC LIMIT 50"
        rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # ── 通知 ──────────────────────────────────────────────────

    def notify(self, category: str, title: str, evidence: str = ""):
        import subprocess
        cmd = ["python3", str(BUS_CLIENT), "write", category,
               f"[{self.role}] {title}", "--src", self.role]
        if evidence:
            cmd.extend(["--evidence", evidence])
        subprocess.run(cmd, capture_output=True, timeout=15)

    # ── 委派方法（跨角色协作 V1.1） ──────────────────────────

    def confirm_delivery(self, task_id: str, target_role: str,
                         timeout: int = 300) -> dict:
        """委派给 PartnerClient.confirm_delivery。"""
        from partner_client import PartnerClient
        return PartnerClient(self.role).confirm_delivery(task_id, target_role, timeout)

    def check_wake_permission(self, target: str) -> bool:
        """检查本角色是否有权唤醒 target。"""
        from partner_client import PartnerClient
        return PartnerClient(self.role).check_wake_permission(target)

    def resolve_partner(self, role: str) -> dict:
        """委派给 PartnerClient.resolve。"""
        from partner_client import PartnerClient
        return PartnerClient(self.role).resolve(role)

    def wake_partner(self, role: str, context: str = "",
                     force: bool = False) -> dict:
        """委派给 PartnerClient.wake。"""
        from partner_client import PartnerClient
        return PartnerClient(self.role).wake(role, context, force)

    def close(self):
        self._conn.close()


# ── 步骤类型常量（V1.1 新增） ─────────────────────────────────────


# ── 独立操作函数（从 task_utils 导入）──


    def kanban_board(self) -> list[dict]:
        """Kanban 看板：按状态分组显示所有 workflow 实例。

        灵感来自 eyalzh/kanban-mcp (40⭐)。
        返回每个状态的 workflow 列表。
        """
        lanes = {"backlog": [], "in_progress": [], "blocked": [], "completed": [], "failed": []}
        rows = self._conn.execute(
            "SELECT instance_id, template_id, status, current_step_id, assignee, created_at, created_at as updated_at "
            "FROM workflow_instances ORDER BY updated_at DESC LIMIT 50"
        ).fetchall()

        for row in rows:
            wf = dict(row)
            status = wf["status"]
            if status in ("pending", "created"):
                lanes["backlog"].append(wf)
            elif status in ("running", "step_done_ready"):
                lanes["in_progress"].append(wf)
            elif status == "completed":
                lanes["completed"].append(wf)
            elif status == "failed":
                lanes["failed"].append(wf)
            else:
                lanes["blocked"].append(wf)

        return [{"lane": k, "count": len(v), "items": v} for k, v in lanes.items()]

    def workflow_stats(self) -> dict:
        """工作流统计摘要。"""
        stats = {}
        for lane in ("pending", "running", "completed", "failed", "cancelled"):
            row = self._conn.execute(
                "SELECT COUNT(*) as c FROM workflow_instances WHERE status=?", (lane,)
            ).fetchone()
            stats[lane] = row["c"] if row else 0
        stats["total"] = sum(stats.values())
        stats["completion_rate"] = round(stats["completed"] / max(stats["total"], 1) * 100, 1)
        return stats

from task_utils import check, complete_task, fail_task, logs
