"""workflow/client.py — CCS 角色使用的工作流客户端。"""

import json
import os
import sqlite3
import time
import warnings
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

    # ── Template ──

    def list_templates(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM workflow_templates ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def find_template(self, name: str) -> Optional[dict]:
        row = self._conn.execute("SELECT * FROM workflow_templates WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

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

    def delete_task(self, task_id: str) -> bool:
        task = self.get_task(task_id)
        if not task:
            return False
        if task['assigner'] == self.role and self.role != 'system':
            return False
        self._log(task_id=task_id, action="deleted")
        self._conn.execute("DELETE FROM workflow_instances WHERE task_id=?", (task_id,))
        self._conn.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
        self._conn.commit()
        return True

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
        confirm_token = uuid.uuid4().hex[:16]
        self._conn.execute(
            "UPDATE workflow_instances SET status='step_done_ready', completed_at=?, "
            "step_results=? WHERE instance_id=?",
            (time.time(), json.dumps({"summary": summary, "files": files_str, "confirm_token": confirm_token}), wf_id)
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
        return confirm_token

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

    def archive(self, wf_id: str) -> bool:
        self._conn.execute("UPDATE workflow_instances SET status='archived' WHERE instance_id=?", (wf_id,))
        self._conn.commit()
        self._log(wf_id=wf_id, action="archived")
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

    def list_all(self, status: str = None, limit: int = 50) -> list[dict]:
        query = "SELECT * FROM workflow_instances WHERE 1=1"
        params = []
        if status:
            query += " AND status=?"; params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

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

    def resolve_partner(self, role: str) -> dict:
        from routing.partner import PartnerClient
        return PartnerClient(self.role).resolve(role)

    def wake_partner(self, role: str, context: str = "", force: bool = False) -> dict:
        from routing.partner import PartnerClient
        return PartnerClient(self.role).wake(role, context, force)

    # ── Confirm 密钥验证 ──

        # ponytail: delegates to lifecycle manager; upgrade to full impl with TTL
    def confirm_step(self, wf_id: str, token: str) -> dict:
        """用 confirm_token 密钥确认审批。无正确密钥拒绝通过。"""
        import json
        inst = self.get(wf_id)
        if not inst:
            return {"confirmed": False, "reason": f"workflow {wf_id} 不存在"}
        step_results = json.loads(inst.get("step_results", "{}"))
        stored_token = step_results.get("confirm_token", "")
        if not stored_token:
            return {"confirmed": False, "reason": "该步骤未生成 confirm_token，无法审批"}
        if token != stored_token:
            return {"confirmed": False, "reason": "confirm_token 不匹配，审批拒绝"}
        self._conn.execute(
            "UPDATE workflow_instances SET status='completed', current_step_id=? "
            "WHERE instance_id=?", ("approved", wf_id)
        )
        self._conn.commit()
        self._log(wf_id=wf_id, action="step_confirmed", detail=f"token={token[:8]}... 审批通过")
        return {"confirmed": True}

    # ── 子工作流 ──

    # 角色→默认子工作流模板映射
    _ROLE_TEMPLATE_MAP = {
        "engineer": "dev_implement",
        "pg": "pg_implement",
        "qa": "qa_test_execute",
        "reviewer": "reviewer_pr_review",
        "writer": "writer_document",
        "pm": "pm_requirements",
        "product_architect": "architect_full_design",
        "scout": "scout_research_cycle",
        "coordinator": "coordinator_dispatch",
        "devops": "devops_deploy_execute",
        "security_auditor": "security_audit_scan",
        "knowledge_curator": "knowledge_sync",
        "lr": "lr_tech_decision",
        "maintainer": "maintainer_health_monitor",
        "closer": "closer_close_loop",
        "optimizer": "developer_implementation",
    }

    def spawn_child(self, parent_wf_id: str, child_assignee: str,
                    child_template: str = "", title: str = "",
                    context: dict = None) -> str:
        """从父工作流创建子工作流（handoff 场景）。
        父步骤完成后自动把结果传给下一个角色执行。
        若 child_template 未指定，按角色自动选默认模板。"""
        parent = self.get(parent_wf_id)
        if not parent:
            raise ValueError(f"parent {parent_wf_id} 不存在")
        parent_task = self.get_task(parent["task_id"]) if parent.get("task_id") else {}
        title = title or f"子任务: {parent_task.get('title', parent_wf_id)}"
        if not child_template:
            child_template = self._ROLE_TEMPLATE_MAP.get(child_assignee, "dev_implement")
        child_task_id, child_wf_id = self.create_task_v2(
            title=title, assignee=child_assignee,
            template_id=child_template, initiator_role=self.role,
            description=f"parent={parent_wf_id} summary={parent.get('step_results','')[:200]}"
        )
        self._conn.execute(
            "UPDATE workflow_instances SET parent_wf_id=?, subflow_source_step_id=? WHERE instance_id=?",
            (parent_wf_id, parent.get("current_step_id", ""), child_wf_id)
        )
        self._conn.commit()
        self._log(wf_id=child_wf_id, action="spawned",
                  detail=f"parent={parent_wf_id}, child={child_wf_id}, target={child_assignee}")
        return child_wf_id

    def get_children(self, parent_wf_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM workflow_instances WHERE parent_wf_id=? ORDER BY created_at",
            (parent_wf_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def advance_pipeline(self, wf_id: str, summary: str) -> dict:
        """推进多步骤流水线：当前步骤完成后自动派发下一个 handoff 角色的子工作流。
        返回下一个步骤信息或 None（流程结束）。"""
        import json, uuid
        inst = self.get(wf_id)
        if not inst:
            return {"advanced": False, "reason": "实例不存在"}
        template_id = inst.get("template_id", "")
        row = self._conn.execute(
            "SELECT steps_json FROM workflow_templates WHERE template_id=?", (template_id,)
        ).fetchone()
        if not row:
            return {"advanced": False, "reason": f"模板 {template_id} 不存在"}
        steps = json.loads(dict(row)["steps_json"])
        current_step = inst.get("current_step_id", "")
        # 找当前步骤的下一步
        found = False
        for s in steps:
            if found and s.get("type") == "handoff":
                # 更新父工作流 current_step
                child_id = self.spawn_child(wf_id, s["target_role"],
                    title=f"{s.get('title', template_id)}", context={"summary": summary})
                self._conn.execute(
                    "UPDATE workflow_instances SET current_step_id=? WHERE instance_id=?",
                    (s["step_id"], wf_id)
                )
                self._conn.commit()
                self._log(wf_id=wf_id, action="handoff",
                          detail=f"→ {child_id} ({s['target_role']})")
                return {"advanced": True, "next_step": s["step_id"],
                        "child_wf_id": child_id, "target_role": s["target_role"]}
            if s["step_id"] == current_step:
                found = True
        return {"advanced": False, "reason": "已到流水线末尾"}

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
