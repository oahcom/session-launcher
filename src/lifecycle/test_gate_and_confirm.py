"""WL-P2-01: LifecycleGate 集成测试 — step 级权限校验 + confirm_step 集成。"""

import os, sys, sqlite3, time, json, unittest, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_templates (
    template_id TEXT PRIMARY KEY,
    name TEXT, description TEXT, steps_json TEXT NOT NULL,
    allowed_initiators TEXT, allowed_executors TEXT,
    is_active INTEGER DEFAULT 1, created_at REAL
);
CREATE TABLE IF NOT EXISTS workflow_instances (
    instance_id TEXT PRIMARY KEY, template_id TEXT, task_id TEXT,
    assigner TEXT, assignee TEXT, status TEXT, current_step_id TEXT,
    step_results TEXT DEFAULT '{}', created_at REAL, completed_at REAL
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY, title TEXT, status TEXT
);
CREATE TABLE IF NOT EXISTS workflow_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_instance_id TEXT, task_id TEXT,
    action TEXT, actor TEXT, detail TEXT, ts REAL
);
"""


def _make_db():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    conn = sqlite3.connect(f.name)
    conn.executescript(DB_SCHEMA)
    # Insert dev_implement template with step role bindings
    steps = [
        {"step_id": "s1", "title": "理解需求", "target_role": "engineer"},
        {"step_id": "s2", "title": "编码", "target_role": "engineer"},
        {"step_id": "s3", "title": "自测", "target_role": "engineer"},
    ]
    conn.execute(
        "INSERT INTO workflow_templates (template_id, name, steps_json, is_active, created_at) VALUES (?, ?, ?, 1, ?)",
        ("dev_implement", "工程师实施", json.dumps(steps), time.time())
    )
    # Insert WL-01 template with handoff steps
    wl01_steps = [
        {"step_id": "s1", "title": "技术方案设计", "type": "handoff", "target_role": "product_architect"},
        {"step_id": "s2", "title": "代码开发", "type": "handoff", "target_role": "pg"},
        {"step_id": "s3", "title": "代码审查", "type": "review", "target_role": "reviewer"},
        {"step_id": "s4", "title": "测试验证", "type": "handoff", "target_role": "qa"},
    ]
    conn.execute(
        "INSERT INTO workflow_templates (template_id, name, steps_json, is_active, created_at) VALUES (?, ?, ?, 1, ?)",
        ("WL-01", "技术实现", json.dumps(wl01_steps), time.time())
    )
    conn.commit()
    conn.close()
    return f.name


class TestLifecycleGate(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.db_path = _make_db()
        # Build some workflow instances
        conn = sqlite3.connect(cls.db_path)
        conn.execute(
            "INSERT INTO tasks (task_id, title, status) VALUES ('t1', 'test', 'in_progress')"
        )
        for wid, tpl, step, assigner, assignee in [
            ("wf_eng", "dev_implement", "s1", "engineer", "engineer"),
            ("wf_wl01_s1", "WL-01", "s1", "pm", "product_architect"),
            ("wf_wl01_s3", "WL-01", "s3", "pm", "reviewer"),
        ]:
            conn.execute(
                "INSERT INTO workflow_instances (instance_id, template_id, task_id, assigner, assignee, status, current_step_id, step_results, created_at) VALUES (?, ?, 't1', ?, ?, 'running', ?, '{}', ?)",
                (wid, tpl, assigner, assignee, step, time.time())
            )
            # Mark step as step_done_ready for confirm_step tests
            sr = json.dumps({step: {"status": "step_done_ready", "completed_at": time.time(), "completed_by": assignee}})
            conn.execute("UPDATE workflow_instances SET step_results=? WHERE instance_id=?", (sr, wid))
        conn.commit()
        conn.close()

    def test_gate_engineer_can_execute_dev_implement(self):
        """engineer 可以执行 dev_implement (无 target_role 约束的模板是 s1/s2/s3 全部开放)。"""
        from lifecycle.gate import LifecycleGate
        g = LifecycleGate(self.db_path)
        # dev_implement has no target_role on its steps (plain strings), so all roles can execute
        self.assertTrue(g.check_can_execute("engineer", "dev_implement", "s1"))
        self.assertTrue(g.check_can_execute("engineer", "dev_implement", "s3"))

    def test_gate_wl01_step_roles(self):
        """WL-01 steps 有明确的 target_role 约束。"""
        from lifecycle.gate import LifecycleGate
        g = LifecycleGate(self.db_path)
        # s1 target_role=product_architect
        self.assertTrue(g.check_can_execute("product_architect", "WL-01", "s1"))
        self.assertFalse(g.check_can_execute("pm", "WL-01", "s1"))
        # s3 target_role=reviewer
        self.assertTrue(g.check_can_execute("reviewer", "WL-01", "s3"))
        self.assertFalse(g.check_can_execute("engineer", "WL-01", "s3"))

    def test_confirm_step_integration_allowed(self):
        """LifecycleGate 集成: 允许的角色可以 confirm_step。"""
        from lifecycle.manager import LifecycleManager
        lm = LifecycleManager("engineer", self.db_path)
        # engineer can confirm dev_implement s1 (no target_role constraint)
        r = lm.confirm_step("wf_eng", "s1")
        self.assertEqual(r["status"], "completed")

    def test_confirm_step_integration_denied(self):
        """LifecycleGate 集成: 无权限角色 confirm_step 抛出 PermissionError。"""
        from lifecycle.manager import LifecycleManager
        lm = LifecycleManager("pm", self.db_path)
        # pm cannot confirm WL-01 s1 (target_role=product_architect)
        with self.assertRaises(PermissionError) as ctx:
            lm.confirm_step("wf_wl01_s1", "s1")
        self.assertIn("LifecycleGate 拒绝", str(ctx.exception))

    def test_confirm_step_wl01_s3_reviewer_allowed(self):
        """reviewer 可以 confirm WL-01 s3 (review 步骤)。"""
        from lifecycle.manager import LifecycleManager
        lm = LifecycleManager("reviewer", self.db_path)
        r = lm.confirm_step("wf_wl01_s3", "s3")
        self.assertEqual(r["status"], "completed")


if __name__ == "__main__":
    unittest.main()
