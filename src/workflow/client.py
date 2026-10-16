"""workflow/client.py — CCS 角色使用的工作流客户端。"""

import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

from paths import BUS_CLIENT
from workflow.db import create_connection

CCS_CLI = Path(__file__).resolve().parent.parent / "ccs.py"


class WorkflowClient:
    """CCS 角色使用的工作流客户端。"""

    def __init__(self, role: str, db_path: str = None):
        self.role = role
        self.db_path = db_path
        self._conn = create_connection(db_path)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def _log(self, wf_id: str = None, task_id: str = None,
             action: str = "", detail: str = ""):
        self._conn.execute(
            "INSERT OR IGNORE INTO workflow_logs (workflow_instance_id, task_id, action, actor, detail, ts) "
            "VALUES (?, ?, ?, ?, ?, ?)", (wf_id, task_id, action, self.role, detail, time.time())
        )
        self._conn.commit()

    # ── Task CRUD ──

    def _validate_create_task(self, template_id: str,
                                initiator_role: str,
                                assignee: str) -> None:
        """Gate.validate_create_task inlined: template existence + role validation."""
        if not template_id:
            raise ValueError("template_id is required")
        row = self._conn.execute(
            "SELECT 1 FROM workflow_templates WHERE template_id=?", (template_id,)
        ).fetchone()
        if not row:
            raise ValueError(f"template_id not found: {template_id}")
        # check is_active column if it exists (SQLite schema may vary)
        try:
            active = self._conn.execute(
                "SELECT is_active FROM workflow_templates WHERE template_id=?", (template_id,)
            ).fetchone()
            if active and active["is_active"] not in (1, "1", True):
                raise ValueError(f"template is inactive: {template_id}")
        except (sqlite3.OperationalError, KeyError):
            pass  # no is_active column in schema
        if not initiator_role:
            raise ValueError("initiator_role is required")
        if not assignee:
            raise ValueError("assignee is required")


    def create_task(self, title: str, description: str = "",
                    assignee: str = None, priority: int = 0,
                    template_id: str = None) -> str:
        if template_id is None:
            raise ValueError(
                "template_id is required. Use create_task_v2(title, assignee, template_id, initiator_role)."
            )
        return self._create_task_impl(title, description, assignee, priority, template_id)


    @classmethod
    def _title_is_placeholder(cls, title: str) -> bool:
        """检查标题是否为占位符（纯编号/角色+编号），拒绝创建空转任务。"""
        t = title.strip()
        if not t:
            return True
        import re
        if re.match(r'^(reviewer|qa|engineer|writer|maintainer|scout)\s*task\s*#?\d*$', t.lower()):
            return True
        if re.match(r'^task\s*#?\d*$', t.lower()):
            return True
        return False

    def create_task_v2(self, title: str, assignee: str,
                       template_id: str, initiator_role: str,
                       description: str = "") -> tuple:
        # 标题质量门禁：拒绝占位符标题
        if self._title_is_placeholder(title):
            raise ValueError(
                f"标题 '{title[:40]}' 是占位符，不会产生实际收益。"
                f"请提供有描述性的任务标题（≥8字符，描述具体做什么）"
            )
        self._validate_create_task(template_id, initiator_role, assignee)
        task_id = self._create_task_impl(title, description, assignee, 0, template_id)
        wf_id = self._create_workflow_instance(task_id, template_id, assignee, initiator_role)
        self._log(action="routed", detail=f"{initiator_role}→{assignee}")
        return (task_id, wf_id)

    def _create_task_impl(self, title: str, description: str,
                          assignee: str, priority: int,
                          template_id: str = None) -> str:
        import uuid
        import subprocess
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        now = time.time()
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if template_id and "template_id" in cols:
            self._conn.execute("""
                INSERT OR IGNORE INTO tasks (task_id, title, description, assigner, assignee,
                                   priority, status, created_at, updated_at, template_id)
                VALUES (?, ?, ?, ?, ?, ?, 'created', ?, ?, ?)
            """, (task_id, title, description, self.role, assignee,
                  priority, now, now, template_id))
        else:
            self._conn.execute("""
                INSERT OR IGNORE INTO tasks (task_id, title, description, assigner, assignee,
                                   priority, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'created', ?, ?)
            """, (task_id, title, description, self.role, assignee,
                  priority, now, now))
        self._conn.commit()
        self._log(task_id=task_id, action="created", detail=f"title={title}, template_id={template_id}")
        # 测试模式检测: 使用临时 DB 时跳过 ccs send 和 bus notify
        _prod_db = str(Path.home() / ".hermes" / "state" / "workflows.db")
        _is_test = self.db_path is not None and str(self.db_path) != _prod_db
        ccs_ok = True
        if not _is_test and assignee and assignee != self.role:
            result = subprocess.run(
                ["python3", str(CCS_CLI), "send", assignee,
                 f"[{self.role}] 你有新任务: {title} — check_task() 查看详情",
                 "--from", self.role],
                capture_output=True, timeout=15,
            )
            ccs_ok = result.returncode == 0
        evidence = f"assignee={assignee}, task_id={task_id}"
        if not ccs_ok:
            evidence += ", ccs_send_failed=true"
        if not _is_test:
            self.notify("task_spec", f"创建任务: {title}", evidence=evidence)
        return task_id

    def _create_workflow_instance(self, task_id: str, template_id: str, assignee: str,
                                   initiator_role: str = None) -> str:
        # ponytail: Gate was YAGNI'd. Gate.validate_create_task inlined as _validate_create_task.
        self._validate_create_task(template_id, initiator_role or self.role, assignee)

        import uuid
        wf_id = f"wf_{uuid.uuid4().hex[:12]}"
        now = time.time()
        self._conn.execute("""
            INSERT OR IGNORE INTO workflow_instances (instance_id, template_id, task_id,
                                            assigner, assignee, status,
                                            current_step_id, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', 's1', ?)
        """, (wf_id, template_id, task_id, self.role, assignee, now))
        self._conn.commit()
        self._conn.execute(
            "UPDATE tasks SET current_workflow_id=?, status='in_progress' WHERE task_id=?",
            (wf_id, task_id))
        self._conn.commit()
        self._log(wf_id=wf_id, task_id=task_id, action="created", detail=f"template_id={template_id}")
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
            "SELECT * FROM workflow_instances WHERE task_id=? ORDER BY created_at", (task_id,)
        ).fetchall()
        if not rows:
            return {"workflow_count": 0, "completed": 0, "current": None}
        completed = sum(1 for r in rows if dict(r)["status"] == "completed")
        running = [dict(r) for r in rows if dict(r)["status"] == "running"]
        pending = [dict(r) for r in rows if dict(r)["status"] == "pending"]
        current = running[0] if running else (pending[0] if pending else None)
        total = len(rows)
        return {
            "workflow_count": total, "completed": completed,
            "current": {"instance_id": current["instance_id"] if current else None,
                        "template_id": current["template_id"] if current else None,
                        "current_step": current["current_step_id"] if current else None,
                        } if current else None,
            "percent": int((completed / total) * 100) if total > 0 else 0,
        }

    def list_tasks(self, status: str = None, assignee: str = None) -> list[dict]:
        query = "SELECT * FROM tasks WHERE 1=1"
        params = []
        if status:
            query += " AND status=?"; params.append(status)
        if assignee:
            query += " AND assignee=?"; params.append(assignee)
        query += " ORDER BY priority DESC, created_at DESC"
        rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # ── Workflow Instance CRUD ──

    def create(self, assignee: str, task_description: str,
               workflow_json: dict = None, task_id: str = None) -> str:
        import uuid
        wf_id = f"wf_{uuid.uuid4().hex[:12]}"
        now = time.time()
        if not task_id:
            task_id = f"task_{int(now * 1000) % 100000000}"
            self._conn.execute("""
                INSERT OR IGNORE INTO tasks (task_id, title, description, assigner, assignee,
                                             status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """, (task_id, task_description, task_description, self.role, assignee, now, now))
        self._conn.execute("""
            INSERT OR IGNORE INTO workflow_instances (instance_id, task_id, assigner, assignee,
                                            status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
        """, (wf_id, task_id, self.role, assignee, now))
        self._conn.commit()
        if task_id:
            self._conn.execute(
                "UPDATE tasks SET current_workflow_id=?, status='in_progress' WHERE task_id=?",
                (wf_id, task_id))
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
            query += " AND status=?"; params.append(status)
        query += " ORDER BY created_at DESC"
        rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def start(self, wf_id: str, step_id: str):
        self._conn.execute(
            "UPDATE workflow_instances SET status='running', current_step_id=? WHERE instance_id=?",
            (step_id, wf_id)
        )
        self._conn.commit()
        wf = self.get(wf_id)
        task_id = wf.get("task_id", "") if wf else ""
        evidence = f"task={task_id}" if task_id else ""
        self.notify("workflow", f"已接单 {wf_id}", evidence=evidence)
        self._log(wf_id=wf_id, action="started", detail=f"step={step_id}")

    def complete(self, wf_id: str, summary: str, files: list = None):
        # 防重闭合守卫：已 completed 的工作流不允许被 wf complete 打回 step_done_ready
        cur = self._conn.execute(
            "SELECT status FROM workflow_instances WHERE instance_id=?", (wf_id,)
        ).fetchone()
        if cur and cur["status"] == "completed":
            return None  # 已完成，不覆写
        import uuid
        files_str = ", ".join(files) if files else ""
        approval_token = uuid.uuid4().hex[:16]
        # 合并到既有 step_results（保留 approval_token_ttl 等生命周期字段），
        # 统一字段名 approval_token 与 lifecycle.manager 保持一致
        import json as _json
        _cur = self._conn.execute(
            "SELECT step_results FROM workflow_instances WHERE instance_id=?", (wf_id,)
        ).fetchone()
        _step_results = {}
        if _cur and _cur["step_results"]:
            _step_results = _json.loads(_cur["step_results"])
        _step_results.update({
            "summary": summary,
            "files": files_str,
            "approval_token": approval_token,
            "approval_token_ttl": time.time() + 3600,
            "approval_consumed_at": None,
        })
        self._conn.execute(
            "UPDATE workflow_instances SET status='step_done_ready', completed_at=?, "
            "step_results=? WHERE instance_id=?",
            (time.time(), _json.dumps(_step_results, ensure_ascii=False), wf_id)
        )
        self._conn.commit()
        wf = self.get(wf_id)
        # 通知审批者：把 token 发给下一步审批人
        if wf:
            task_id = wf.get("task_id", "")
            reviewer = "reviewer"
            if wf.get("template_id") == "writer_document":
                reviewer = "lr"
            self.notify("workflow", f"审批请求 {wf_id}",
                evidence=f"task={task_id}, 审批者={reviewer}, token_available=true")
            self._log(wf_id=wf_id, action="step_done_ready",
                      detail=f"审批者={reviewer}, token_written=true")
        if wf and wf.get('task_id'):
            self._sync_task_from_workflows(wf['task_id'])
        return approval_token

    def _sync_task_from_workflows(self, task_id: str):
        rows = self._conn.execute(
            "SELECT status FROM workflow_instances WHERE task_id=?", (task_id,)
        ).fetchall()
        statuses = [dict(r)["status"] for r in rows]
        if not statuses:
            return
        if all(s == "completed" for s in statuses):
            task_status = "completed"
        elif any(s == "failed" for s in statuses):
            task_status = "failed"
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
        wf = self.get(wf_id)
        if wf and wf.get('task_id'):
            self._sync_task_from_workflows(wf['task_id'])
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

    def find_zombies(self, minutes: int = 120, limit: int = 50) -> list[dict]:
        """僵尸检测: running > minutes 分钟的工作流。

        两种情况算僵尸：
        1. 无 timeout_count 追踪（完全无人管理）
        2. timeout_count=0（追踪启动但没有实际回收动作）
        ponytail: 纯 SQL LIKE 匹配 timeout_count 值；若 step_results JSON 结构
        变更（如 timeout_count 不再是 int），需要改为 json_extract。
        """
        now = time.time()
        rows = self._conn.execute(
            "SELECT * FROM workflow_instances WHERE status='running' "
            "AND ? - created_at > ? "
            "AND (step_results NOT LIKE '%timeout_count%' "
            "OR step_results LIKE '%\"timeout_count\": 0%') "
            "ORDER BY created_at LIMIT ?",
            (now, minutes * 60, limit),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["running_minutes"] = round((now - d["created_at"]) / 60, 1)
            out.append(d)
        return out

    # ── 日志 ──

    def get_logs(self, wf_id: str = None, task_id: str = None) -> list[dict]:
        query = "SELECT * FROM workflow_logs WHERE 1=1"
        params = []
        if wf_id:
            query += " AND workflow_instance_id=?"; params.append(wf_id)
        if task_id:
            query += " AND task_id=?"; params.append(task_id)
        query += " ORDER BY ts DESC LIMIT 50"
        rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # ── 通知 ──

    def notify(self, category: str, title: str, evidence: str = ""):
        import subprocess
        cmd = ["python3", str(BUS_CLIENT), "write", category,
               f"[{self.role}] {title}", "--src", self.role]
        if evidence:
            cmd.extend(["--evidence", evidence])
        subprocess.run(cmd, capture_output=True, timeout=15)

    # ── 委派方法 ──

    def confirm_delivery(self, task_id: str, target_role: str, timeout: int = 300) -> dict:
        from routing.partner import PartnerClient
        return PartnerClient(self.role).confirm_delivery(task_id, target_role, timeout)

    def check_wake_permission(self, target: str) -> bool:
        from routing.partner import PartnerClient
        return PartnerClient(self.role).check_wake_permission(target)

    # ── Confirm 密钥验证 ──

        # ponytail: thin wrapper over lifecycle manager; full TTL/consumed-once semantics live there
    def confirm_step(self, wf_id: str, token: str, reason: str = "assigner approval") -> dict:
        """审批步骤：按 approval_token 反查 step_id，委托 lifecycle.manager 状态流转。
        不在此处自行 UPDATE（旧实现字段名/状态值错乱，绕过审批链路）。"""
        import json
        inst = self.get(wf_id)
        if not inst:
            return {"confirmed": False, "reason": f"workflow {wf_id} 不存在"}
        step_results = json.loads(inst.get("step_results", "{}"))
        hit_step_id = None
        for step_id, res in step_results.items():
            if res.get("approval_token") == token:
                hit_step_id = step_id
                break
        if not hit_step_id:
            return {"confirmed": False, "reason": "approval_token 不匹配，审批拒绝"}
        # 委托 lifecycle.manager：密钥验证 + _advance_unsafe 推进
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "session-pipeline" / "src"))
        from lifecycle.manager import LifecycleManager
        lm = LifecycleManager(role=self.role)
        ok = lm.confirm_step(wf_id, step_id=hit_step_id, token=token,
                             approved=True, reason=reason)
        if ok:
            self._log(wf_id=wf_id, action="step_confirmed",
                      detail=f"token={token[:8]}... step={hit_step_id} 审批通过")
        return {"confirmed": ok}

    # ── 子工作流 ──

    # ── Kanban ──

    def kanban_board(self) -> list[dict]:
        lanes = {"backlog": [], "in_progress": [], "blocked": [], "completed": [], "failed": [], "cancelled": []}
        rows = self._conn.execute(
            "SELECT instance_id, template_id, status, current_step_id, assignee, created_at, created_at as updated_at "
            "FROM workflow_instances ORDER BY updated_at DESC LIMIT 50"
        ).fetchall()
        for row in rows:
            wf = dict(row); status = wf["status"]
            if status in ("pending", "created"):
                lanes["backlog"].append(wf)
            elif status in ("running", "step_done_ready"):
                lanes["in_progress"].append(wf)
            elif status == "completed":
                lanes["completed"].append(wf)
            elif status == "failed":
                lanes["failed"].append(wf)
            elif status == "cancelled":
                lanes["cancelled"].append(wf)
            elif status in ("blocked", "archived"):
                lanes["blocked"].append(wf)  # archived → blocked is still active concern
            else:
                lanes["blocked"].append(wf)
        return [{"lane": k, "count": len(v), "items": v} for k, v in lanes.items()]

    def workflow_stats(self) -> dict:
        stats = {}
        for lane in ("pending", "running", "completed", "failed", "cancelled"):
            row = self._conn.execute("SELECT COUNT(*) as c FROM workflow_instances WHERE status=?", (lane,)).fetchone()
            stats[lane] = row["c"] if row else 0
        stats["total"] = sum(stats.values())
        stats["completion_rate"] = round(stats["completed"] / max(stats["total"], 1) * 100, 1)
        return stats

    def close(self):
        self._conn.close()
