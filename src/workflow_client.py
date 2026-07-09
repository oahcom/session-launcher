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
from pathlib import Path
from typing import Optional

DB_PATH = Path.home() / ".hermes" / "state" / "workflows.db"
BUS_CLIENT = Path.home() / ".hermes" / "scripts" / "bus_client.py"


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
                    assignee: str = None, priority: int = 0) -> str:
        import uuid
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        now = time.time()
        self._conn.execute("""
            INSERT INTO tasks (task_id, title, description, assigner, assignee,
                               priority, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'created', ?, ?)
        """, (task_id, title, description, self.role, assignee, priority, now, now))
        self._conn.commit()
        self._log(task_id=task_id, action="created", detail=f"title={title}")
        return task_id

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
            "SELECT * FROM workflow_instances WHERE assignee=? AND status IN ('pending', 'running') "
            "ORDER BY created_at ASC LIMIT 1", (self.role,)
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
        self._log(wf_id=wf_id, action="failed", detail=f"reason={reason}")

    def cancel(self, wf_id: str, reason: str = ""):
        self._conn.execute(
            "UPDATE workflow_instances SET status='cancelled', completed_at=?, "
            "step_results=? WHERE instance_id=?",
            (time.time(), json.dumps({"reason": reason}), wf_id)
        )
        self._conn.commit()
        self._log(wf_id=wf_id, action="cancelled", detail=f"reason={reason}")

    def delete(self, wf_id: str) -> bool:
        wf = self.get(wf_id)
        if not wf:
            return False
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

    def close(self):
        self._conn.close()


# ── CLI 便捷函数 ──────────────────────────────────────────────────

def check(role: str) -> str:
    client = WorkflowClient(role)
    task = client.check_task()
    client.close()
    if not task:
        return "无待完成任务"
    return (f"任务: {task['task_id']}\n"
            f"workflow_id: {task['instance_id']}\n"
            f"创建时间: {time.strftime('%Y-%m-%d %H:%M', time.localtime(task['created_at']))}")


def complete_task(role: str, wf_id: str, summary: str, files: list = None) -> str:
    client = WorkflowClient(role)
    client.complete(wf_id, summary, files)
    client.notify("workflow", f"{role} 完成任务: {summary}",
                  evidence=f"文件: {', '.join(files) if files else '无'}")
    client.close()
    return "任务已标记完成"


def fail_task(role: str, wf_id: str, reason: str) -> str:
    client = WorkflowClient(role)
    client.fail(wf_id, reason)
    client.notify("blocker", f"{role} 任务失败: {reason}", evidence=reason)
    client.close()
    return "任务已标记失败"


def logs(role: str, wf_id: str = None, task_id: str = None) -> str:
    client = WorkflowClient(role)
    entries = client.get_logs(wf_id, task_id)
    client.close()
    if not entries:
        return "无日志"
    lines = []
    for e in entries:
        ts = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(e['ts']))
        lines.append(f"[{ts}] {e['action']} by {e['actor']}: {e['detail']}")
    return "\n".join(lines)
