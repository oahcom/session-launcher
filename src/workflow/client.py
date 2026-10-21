"""workflow/client.py — CCS 角色使用的工作流客户端。"""

import json
import sys
import time
from pathlib import Path
from typing import Optional

from paths import BUS_CLIENT
from workflow.db import create_connection

CCS_CLI = Path(__file__).resolve().parent.parent / "ccs.py"

# 终态：任何写入型操作（complete/fail/cancel）都不应改写这些状态。
# 与 pipeline 侧 lifecycle.manager 的状态机对齐，避免两套语义分裂。
# archived 是 pipeline workflow/client.py:483 写的归档态；漏掉它会让归档实例被 wf complete 复活。
# delivery_blocked **不是**终态：pipeflow/engine.py:_recover_delivery_blocked 会把它还原成
# running 重试，纳入守卫会切断该恢复路径。
_TERMINAL_STATUSES = frozenset({"completed", "cancelled", "failed", "archived"})
_TERMINAL_PLACEHOLDERS = ",".join("?" * len(_TERMINAL_STATUSES))


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

    def _update_if_mutable(self, wf_id: str, set_sql: str, params: tuple) -> bool:
        """原子地把实例改到新状态。终态→False（不覆写），不存在→KeyError。

        WHY 条件 UPDATE 而非先 SELECT 再 UPDATE：两条语句之间另一写入者可关闭
        实例，UPDATE 照样覆盖——实测可把已 cancelled 的链复活。把终态判定下推
        到 WHERE，由 SQLite 写锁保证判定与写入原子；正常路径也因此少一次 SELECT。
        complete/fail/cancel 三个写入路径共用，漏掉一个就少一条链的守卫。
        """
        cur = self._conn.execute(
            f"UPDATE workflow_instances SET {set_sql} "
            f"WHERE instance_id=? AND status NOT IN ({_TERMINAL_PLACEHOLDERS})",
            (*params, wf_id, *_TERMINAL_STATUSES),
        )
        if cur.rowcount:
            self._conn.commit()
            return True
        self._conn.rollback()
        # 0 行有两种含义：终态（幂等空操作）还是 id 不存在（调用方拼错）。
        row = self._conn.execute(
            "SELECT 1 FROM workflow_instances WHERE instance_id=?", (wf_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"工作流不存在: {wf_id}")
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
            "SELECT is_active FROM workflow_templates WHERE template_id=?", (template_id,)
        ).fetchone()
        if not row:
            raise ValueError(f"template_id not found: {template_id}")
        if row["is_active"] not in (1, "1", True):
            raise ValueError(f"template is inactive: {template_id}")
        if not initiator_role:
            raise ValueError("initiator_role is required")
        if not assignee:
            raise ValueError("assignee is required")


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
        """建任务 + 工作流实例。

        template_id 为空时委托 session-pipeline 的同名实现——后者含完整的
        模板自动推荐、assignee 对齐首步、5 分钟去重与跨角色 fan-out 防御。
        WHY 委托而非复制：本文件与 pipeline 版是**分叉**的两份（本份独有
        find_zombies / workflow_stats 供 `wf cleanup` 用，pipeline 版独有
        推荐与对齐）。复制会让两者再次漂移——实测 2026-10-17：pipeline 侧
        修好自动推荐后，角色走的仍是本份的 `template_id is required` 硬闸门，
        建流入口依然不可用（role prompt pm.md:33 教的正是不带 -t 的形式）。
        显式传 template_id 时保持原路径不动，避免改变既有行为。
        """
        if not template_id:
            return self._delegate_create_task_v2(
                title, assignee, initiator_role, description)
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

    def _delegate_create_task_v2(self, title: str, assignee: str,
                                 initiator_role: str, description: str) -> tuple:
        """委托 pipeline 版 create_task_v2（template_id 缺省路径）。

        用子进程而非 in-process import：两个项目都有 `workflow` 包，
        in-process 会被已加载的 launcher 版遮蔽（实测 ModuleNotFoundError:
        workflow.gateway —— pipeline 的 gateway.py 不在 launcher 包里）。
        子进程给 pipeline/src 干净的 sys.path，成本是 ~0.3s 进程启动。
        """
        import subprocess
        from paths import SESSION_PIPELINE_SRC as _psrc
        _code = (
            "import sys, json\n"
            f"sys.path.insert(0, {str(_psrc)!r})\n"
            "from workflow.client import WorkflowClient\n"
            f"wc = WorkflowClient({self.role!r}, db_path={self.db_path!r})\n"
            "try:\n"
            f"    tid, wid = wc.create_task_v2({title!r}, {assignee!r}, '', "
            f"{initiator_role!r}, description={description!r})\n"
            "    print(json.dumps({'ok': True, 'task_id': tid, 'wf_id': wid}))\n"
            "except Exception as e:\n"
            "    print(json.dumps({'ok': False, 'error': str(e)}))\n"
            "finally:\n"
            "    wc.close()\n"
        )
        _r = subprocess.run([sys.executable, "-c", _code],
                            capture_output=True, text=True, timeout=60)
        _out = (_r.stdout or "").strip().splitlines()
        _last = _out[-1] if _out else ""
        if not _last.startswith("{"):
            raise ValueError(
                f"委托 pipeline 建流失败: {(_r.stderr or _r.stdout or 'no output')[-400:]}")
        _res = json.loads(_last)
        if not _res.get("ok"):
            raise ValueError(_res.get("error", "unknown"))
        return (_res["task_id"], _res["wf_id"])

    def _create_task_impl(self, title: str, description: str,
                          assignee: str, priority: int,
                          template_id: str = None) -> str:
        import uuid
        import subprocess
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        now = time.time()
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if template_id and "template_id" in cols:
            cur = self._conn.execute("""
                INSERT OR IGNORE INTO tasks (task_id, title, description, assigner, assignee,
                                   priority, status, created_at, updated_at, template_id)
                VALUES (?, ?, ?, ?, ?, ?, 'created', ?, ?, ?)
            """, (task_id, title, description, self.role, assignee,
                  priority, now, now, template_id))
        else:
            cur = self._conn.execute("""
                INSERT OR IGNORE INTO tasks (task_id, title, description, assigner, assignee,
                                   priority, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'created', ?, ?)
            """, (task_id, title, description, self.role, assignee,
                  priority, now, now))
        self._conn.commit()
        # OR IGNORE 会把约束冲突吞成 0 行；不校验就 notify 会向总线广播一个
        # 查不到的 task_id，调用方按成功继续建链，需求就此静默丢失。
        if not cur.rowcount:
            raise RuntimeError(f"任务未写入（task_id={task_id} 已存在或约束冲突）")
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
        if not row and task_id.startswith("wf_"):
            # 角色手里只有 [实例] wf_id（base.md:76），本方法只查 tasks →
            # 「未找到」→ progress.current.contract 整块消失，角色只能猜验收标准。
            # 同一张库里 workflow_instances.task_id 指向 tasks.task_id，直接回查。
            row = self._conn.execute(
                "SELECT t.* FROM tasks t JOIN workflow_instances w ON w.task_id=t.task_id "
                "WHERE w.instance_id=?", (task_id,)).fetchone()
        if not row:
            return None
        task = dict(row)
        # 按 wf_id 问就只答那一条实例：一个 task 下可能挂多条链（子链/fan-out），
        # 按 task 聚合会把「你要问的那步」换成另一条链的当前步 —— 角色照错契约交付。
        only = task_id if task_id.startswith("wf_") else ""
        task["progress"] = self._compute_progress(task["task_id"], only)
        return task

    def _compute_progress(self, task_id: str, only_instance: str = "") -> dict:
        rows = self._conn.execute(
            "SELECT * FROM workflow_instances WHERE task_id=? ORDER BY created_at", (task_id,)
        ).fetchall()
        if not rows:
            return {"workflow_count": 0, "completed": 0, "current": None}
        completed = sum(1 for r in rows if dict(r)["status"] == "completed")
        running = [dict(r) for r in rows if dict(r)["status"] == "running"]
        pending = [dict(r) for r in rows if dict(r)["status"] == "pending"]
        if only_instance:
            pinned = [dict(r) for r in rows if dict(r)["instance_id"] == only_instance]
            current = pinned[0] if pinned else None
        else:
            current = running[0] if running else (pending[0] if pending else None)
        total = len(rows)
        return {
            "workflow_count": total, "completed": completed,
            "current": {"instance_id": current["instance_id"] if current else None,
                        "template_id": current["template_id"] if current else None,
                        "current_step": current["current_step_id"] if current else None,
                        "contract": self._step_contract(current) if current else None,
                        } if current else None,
            "percent": int((completed / total) * 100) if total > 0 else 0,
        }

    def _step_contract(self, inst: dict) -> Optional[dict]:
        """当前步骤的机器验收契约（exit_condition + exit_schema）。

        角色看不到它就得猜：实测 qa 的 bug_report.md 正文缺 exit_schema.mustContain
        里的词，完成消息标题合规却不被引擎认，同一份产出被重投 4 次、白等约 30 分钟。
        模板不存在/JSON 坏/步骤找不到一律返回 None（契约是旁路信息，不能让它挡路）。
        """
        try:
            row = self._conn.execute(
                "SELECT steps_json FROM workflow_templates WHERE template_id=?",
                (inst.get("template_id"),)).fetchone()
            steps = json.loads((row["steps_json"] if row else "") or "[]")
            sid = inst.get("current_step_id")
            for s in steps:
                if (s.get("step_id") or s.get("id")) == sid:
                    return {"step": sid,
                            "exit_condition": s.get("exit_condition") or {},
                            "exit_schema": s.get("exit_schema") or {}}
        except (json.JSONDecodeError, TypeError, KeyError, IndexError):
            return None
        return None

    def state(self, wf_id: str) -> Optional[dict]:
        """一条链的当前状态：实例 + 每步的状态/被拒次数/完成者。

        为什么需要这个浅接口：角色 48h 内手写了 14,909 次 sqlite3 查询（占全部
        Bash 调用 32%），其中 1,207 次只为了读 workflow_instances.step_results
        这一列。实现放在 workflow/state.py（client 已过 600 行红线）。
        """
        from workflow.state import chain_state
        return chain_state(self._conn, wf_id)

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

    def complete(self, wf_id: str, summary: str, files: list = None):
        # 终态守卫见 _update_if_mutable：守卫放在 UPDATE 上，守卫与写入不可分离
        # （原实现只挡 completed，实测把 cancelled 实例复活成 step_done_ready）。
        #
        # 不再自铸 approval_token（2026-10-22）：执行者在提交完成时自己生成密钥、
        # 自己写 ttl、硬编码审批人 reviewer，而写入位置是 step_results **顶层**，
        # 引擎只读 step_results[step_id] 的 token（engine.py 审批门控段）——引擎
        # 永远看不见它，于是审批被静默跳过，同时把顶层污染成非 dict 键（实测 413
        # 个）。顶层这一路实测 117 签发 / 0 消费（bus #438436 复核：step 级另有
        # 429 签发 / 41 消费，最近一次 10-22 00:35，那一路仍有效，未删）。
        # 删的只是引擎永远读不到的顶层通道。
        files_str = ", ".join(files) if files else ""
        import json as _json
        _cur = self._conn.execute(
            "SELECT step_results FROM workflow_instances WHERE instance_id=?", (wf_id,)
        ).fetchone()
        _step_results = {}
        if _cur and _cur["step_results"]:
            _step_results = _json.loads(_cur["step_results"])
        _step_results.update({"summary": summary, "files": files_str})
        if not self._update_if_mutable(
                wf_id,
                "status='step_done_ready', completed_at=?, step_results=?",
                (time.time(), _json.dumps(_step_results, ensure_ascii=False))):
            return None  # 终态：不写库，也不发审批通知
        wf = self.get(wf_id)
        # 不再向 reviewer 广播「审批请求」：没有密钥可批，这条通知只会制造
        # 无法完成的 confirm 往返（实测角色侧 172 次 engineer confirm 全失败）。
        if wf:
            self._log(wf_id=wf_id, action="step_done_ready", detail="completed_by_role")
        if wf and wf.get('task_id'):
            self._sync_task_from_workflows(wf['task_id'])
        return True

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
        if not self._update_if_mutable(
                wf_id,
                "status='failed', completed_at=?, step_results=?",
                (time.time(), json.dumps({"reason": reason}))):
            return None  # 终态，不覆写
        wf = self.get(wf_id)
        if wf and wf.get('task_id'):
            self._sync_task_from_workflows(wf['task_id'])
        self._log(wf_id=wf_id, action="failed", detail=f"reason={reason}")
        return True

    def cancel(self, wf_id: str, reason: str = ""):
        if not self._update_if_mutable(
                wf_id,
                "status='cancelled', completed_at=?, step_results=?",
                (time.time(), json.dumps({"reason": reason}))):
            return None  # 终态，不覆写（重复 cancel 幂等无动作）
        wf = self.get(wf_id)
        if wf and wf.get('task_id'):
            self._sync_task_from_workflows(wf['task_id'])
        self._log(wf_id=wf_id, action="cancelled", detail=f"reason={reason}")
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

    def confirm(self, wf_id: str, step_id: str, token: str = "",
                approved: bool = True, reason: str = "") -> dict:
        """审批步骤（wf confirm CLI 入口）。委托 pipeline 版实现。

        WHY 子进程而非 in-process import：两个项目都有 `workflow` 包，先加载
        本 launcher 版后，pipeline 的 `workflow.client` 会被已加载模块遮蔽
        （与 create_task_v2 同一坑，见 _delegate_create_task_v2 docstring）。

        WHY 不在此处实现：审批涉及 token 校验 / consumed-once / only-assigner
        三重语义，全部在 pipeline 的 lifecycle.manager 里；在此复刻一份必然
        随对方演进而漂移（R94 #629e5df 正是只在 wf.py 加了子命令、未同步本
        类，导致 `wf confirm` 100% AttributeError —— 实测 49 次调用 55 处报错）。
        """
        import subprocess
        from paths import SESSION_PIPELINE_SRC as _psrc
        _code = (
            "import sys, json\n"
            f"sys.path.insert(0, {str(_psrc)!r})\n"
            "from workflow.client import WorkflowClient\n"
            f"wc = WorkflowClient({self.role!r}, db_path={self.db_path!r})\n"
            "try:\n"
            f"    r = wc.confirm({wf_id!r}, {step_id!r}, token={token!r}, "
            f"approved={approved!r}, reason={reason!r})\n"
            "    print(json.dumps({'ok': True, 'result': r}, default=str))\n"
            "except Exception as e:\n"
            "    print(json.dumps({'ok': False, 'error': str(e)}))\n"
            "finally:\n"
            "    wc.close()\n"
        )
        _r = subprocess.run([sys.executable, "-c", _code],
                            capture_output=True, text=True, timeout=60)
        _out = (_r.stdout or "").strip().splitlines()
        _last = _out[-1] if _out else ""
        if not _last.startswith("{"):
            raise ValueError(
                f"委托 pipeline 审批失败: {(_r.stderr or _r.stdout or 'no output')[-400:]}")
        _res = json.loads(_last)
        if not _res.get("ok"):
            raise ValueError(_res.get("error", "unknown"))
        return _res.get("result") or {}

    def pull_approval(self, wf_id: str, step_id: str) -> dict:
        """拉取自己是审批人的步骤密钥。委托 pipeline 版实现（同 confirm）。

        WHY 子进程：两个项目都有 `workflow` 包，先加载本 launcher 版后，pipeline
        的 `workflow.client` 会被已加载模块遮蔽。
        """
        import subprocess
        from paths import SESSION_PIPELINE_SRC as _psrc
        _code = (
            "import sys, json\n"
            f"sys.path.insert(0, {str(_psrc)!r})\n"
            "from workflow.client import WorkflowClient\n"
            f"wc = WorkflowClient({self.role!r}, db_path={self.db_path!r})\n"
            "try:\n"
            f"    r = wc.pull_approval({wf_id!r}, {step_id!r})\n"
            "    print(json.dumps(r, default=str))\n"
            "except Exception as e:\n"
            "    print(json.dumps({'ok': False, 'error': str(e)}))\n"
            "finally:\n"
            "    wc.close()\n"
        )
        _r = subprocess.run([sys.executable, "-c", _code],
                            capture_output=True, text=True, timeout=60)
        _out = (_r.stdout or "").strip().splitlines()
        _last = _out[-1] if _out else ""
        if not _last.startswith("{"):
            raise ValueError(
                f"委托 pipeline 拉取失败: {(_r.stderr or _r.stdout or 'no output')[-400:]}")
        return json.loads(_last)

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
