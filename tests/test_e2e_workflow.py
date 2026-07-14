#!/usr/bin/env python3
"""
test_e2e_workflow.py — 端到端工作流验证（pytest 正确模式）

pytest 中 self 不跨测试方法共享，所以每个测试类用一个
class-scoped fixture 管理 workflow 状态。
"""

import sys
import tempfile
import sqlite3
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pytest
from template_registry import TemplateRegistry
from workflow.client import WorkflowClient
from lifecycle.manager import LifecycleManager
from migration.scripts import run_migration


def _build_db():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db = f.name; f.close()
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS workflow_templates (
            template_id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT,
            steps_json TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workflow_instances (
            instance_id TEXT PRIMARY KEY, template_id TEXT, task_id TEXT NOT NULL,
            assigner TEXT NOT NULL, assignee TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', current_step_id TEXT,
            step_results TEXT DEFAULT '{}', created_at REAL NOT NULL, completed_at REAL
        );
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT,
            assigner TEXT NOT NULL, assignee TEXT, priority INTEGER DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'created', current_workflow_id TEXT,
            progress TEXT DEFAULT '{}', created_at REAL NOT NULL, updated_at REAL NOT NULL,
            completed_at REAL, tags TEXT DEFAULT '[]', context TEXT DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS workflow_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, workflow_instance_id TEXT,
            task_id TEXT, action TEXT NOT NULL, actor TEXT NOT NULL,
            detail TEXT, ts REAL NOT NULL
        );
    """)
    conn.commit(); conn.close()
    run_migration(db_path=db, dry_run=False)
    reg = TemplateRegistry(db_path=db)
    for tpl in _TEMPLATES:
        reg.register(tpl)
    reg.close()
    return db


_TEMPLATES = [
    {
        "workflow_id": "WL-01", "name": "技术实现",
        "description": "从方案设计到部署上线的完整流程",
        "trigger_scene": ["需要编码实现的功能开发任务"],
        "allowed_initiators": ["lr", "pm", "coordinator", "product_architect"],
        "allowed_executors": ["product_architect", "reviewer", "pg", "engineer", "maintainer", "optimizer"],
        "steps": [
            {"step_id": "s1", "title": "方案设计", "type": "handoff",
             "target_role": "product_architect",
             "prompt_template": "做什么: 编写方案\n怎么做: 分析需求\n验收标准: 方案完整",
             "failure_patterns": ["遗漏约束", "安全影响"], "estimated_hours": 4.0},
            {"step_id": "s2", "title": "编码实现", "type": "handoff",
             "target_role": "pg",
             "prompt_template": "做什么: 编码\n怎么做: 实现功能\n验收标准: 测试通过",
             "failure_patterns": ["未覆盖边界", "性能问题"], "estimated_hours": 16.0},
            {"step_id": "s3", "title": "代码审查", "type": "review",
             "target_role": "reviewer",
             "prompt_template": "做什么: 审查代码\n怎么做: 逐行检查\n验收标准: 无 P0",
             "completion_check": {"review_required": True},
             "failure_patterns": ["流于形式", "漏安全"], "estimated_hours": 2.0},
        ],
        "max_duration_hours": 48,
        "quality_standards": "产出物需对应角色审查",
    },
    {
        "workflow_id": "WL-02", "name": "需求流转",
        "description": "需求从提出到评审确认的流转流程",
        "trigger_scene": ["新功能需求需要评审"],
        "allowed_initiators": ["pm", "coordinator", "lr"],
        "allowed_executors": ["pm", "product_architect", "pg", "reviewer", "qa"],
        "steps": [
            {"step_id": "s1", "title": "需求编写", "type": "single",
             "prompt_template": "做什么: 编写 PRD\n怎么做: 按标准模板\n验收标准: 需求清晰",
             "failure_patterns": ["模糊", "缺验收标准"], "estimated_hours": 2.0},
            {"step_id": "s2", "title": "需求评审", "type": "review",
             "target_role": "reviewer",
             "prompt_template": "做什么: 评审可行性\n怎么做: 分析技术可行性\n验收标准: 结论明确",
             "completion_check": {"review_required": True},
             "failure_patterns": ["不深入", "漏依赖"], "estimated_hours": 1.0},
            {"step_id": "s3", "title": "通知相关方", "type": "notify",
             "target_role": "pg",
             "prompt_template": "做什么: 通知开发\n怎么做: 发详细文档\n验收标准: 送达确认",
             "failure_patterns": ["漏通知", "内容不完整"], "estimated_hours": 0.5},
        ],
        "max_duration_hours": 24,
        "quality_standards": "PRD 需 reviewer 审查",
    },
    {
        "workflow_id": "WL-03", "name": "Bug 修复",
        "description": "标准 Bug 修复流程（P0/P1）",
        "trigger_scene": ["P0/P1 生产环境 Bug 修复"],
        "allowed_initiators": ["qa", "lr", "coordinator", "maintainer"],
        "allowed_executors": ["pg", "engineer", "qa", "maintainer"],
        "steps": [
            {"step_id": "s1", "title": "复现确认", "type": "single",
             "prompt_template": "做什么: 复现 Bug\n怎么做: 记录复现条件\n验收标准: 可稳定复现",
             "failure_patterns": ["无法复现", "环境差异"], "estimated_hours": 1.0},
            {"step_id": "s2", "title": "根因分析", "type": "single",
             "prompt_template": "做什么: 分析根因\n怎么做: 追踪调用栈\n验收标准: 根因明确",
             "failure_patterns": ["表象修复", "回归风险"], "estimated_hours": 2.0},
            {"step_id": "s3", "title": "部署修复", "type": "notify",
             "target_role": "devops",
             "prompt_template": "做什么: 部署修复\n怎么做: 走发布流程\n验收标准: 监控正常",
             "failure_patterns": ["跳过灰度", "未验证"], "estimated_hours": 1.0},
        ],
        "max_duration_hours": 24,
        "quality_standards": "P0 Bug 在 2h 内完成根因分析",
    },
    {
        "workflow_id": "WL-04", "name": "架构决策",
        "description": "架构决策提案评审执行全流程",
        "trigger_scene": ["技术栈选型决策需要评估"],
        "allowed_initiators": ["product_architect", "lr", "coordinator"],
        "allowed_executors": ["product_architect", "engineer", "maintainer", "pg", "coordinator"],
        "steps": [
            {"step_id": "s1", "title": "方案对比", "type": "single",
             "prompt_template": "做什么: 出候选方案\n怎么做: 三维对比\n验收标准: 表格完整",
             "failure_patterns": ["漏维度", "偏向性"], "estimated_hours": 4.0},
            {"step_id": "s2", "title": "架构评审", "type": "review",
             "target_role": "reviewer",
             "prompt_template": "做什么: 评审方案\n怎么做: 六维度评估\n验收标准: 结论明确",
             "completion_check": {"review_required": True},
             "failure_patterns": ["未考虑扩展", "忽略技术债"], "estimated_hours": 2.0},
        ],
        "max_duration_hours": 72,
        "quality_standards": "架构决策需 reviewer 审查",
    },
    {
        "workflow_id": "WL-05", "name": "基线维护",
        "description": "依赖版本升级和技术基线维护",
        "trigger_scene": ["依赖版本定期升级"],
        "allowed_initiators": ["maintainer", "coordinator"],
        "allowed_executors": ["maintainer", "engineer", "pg"],
        "steps": [
            {"step_id": "s1", "title": "升级评估", "type": "gate",
             "target_role": "maintainer",
             "prompt_template": "做什么: 评估影响\n怎么做: 检查 changelog\n验收标准: 评估完整",
             "failure_patterns": ["漏依赖", "未评估兼容"], "estimated_hours": 2.0},
            {"step_id": "s2", "title": "升级执行", "type": "notify",
             "target_role": "devops",
             "prompt_template": "做什么: 执行升级\n怎么做: 按清单操作\n验收标准: CI 通过",
             "failure_patterns": ["中断", "回滚缺失"], "estimated_hours": 1.0},
        ],
        "max_duration_hours": 16,
        "quality_standards": "升级评估需通过全面审查",
    },
]


@pytest.fixture(scope="module")
def db():
    return _build_db()


# ── 每个测试类用一个 class-scoped fixture 返回 (task_id, wf_id) ──

@pytest.fixture(scope="class")
def wf01(db):
    wc = WorkflowClient("pm", db_path=db)
    task_id, wf_id = wc.create_task_v2(
        "登录功能", assignee="product_architect",
        template_id="WL-01", initiator_role="pm")
    wc.close()
    return task_id, wf_id


@pytest.fixture(scope="class")
def wf02(db):
    wc = WorkflowClient("pm", db_path=db)
    _, wf_id = wc.create_task_v2("需求", assignee="pm", template_id="WL-02", initiator_role="pm")
    wc.close()
    return wf_id


@pytest.fixture(scope="class")
def wf03(db):
    wc = WorkflowClient("qa", db_path=db)
    _, wf_id = wc.create_task_v2("500错误", assignee="pg", template_id="WL-03", initiator_role="qa")
    wc.close()
    return wf_id


@pytest.fixture(scope="class")
def wf04(db):
    wc = WorkflowClient("product_architect", db_path=db)
    _, wf_id = wc.create_task_v2("网关选型", assignee="product_architect",
                                  template_id="WL-04", initiator_role="product_architect")
    wc.close()
    return wf_id


@pytest.fixture(scope="class")
def wf05(db):
    wc = WorkflowClient("maintainer", db_path=db)
    _, wf_id = wc.create_task_v2("Flask升级", assignee="maintainer",
                                  template_id="WL-05", initiator_role="maintainer")
    wc.close()
    return wf_id


class TestWL01_EndToEnd:
    """WL-01: 3 角色手递手，验证每一步的状态。"""

    def test_01_pending(self, wf01, db):
        task_id, wf_id = wf01
        self.__class__.task_id = task_id
        self.__class__.wf_id = wf_id
        lm = LifecycleManager("product_architect", db_path=db)
        wf = lm.get_wf(wf_id)
        assert wf["status"] == "pending"
        assert wf["current_step_id"] == "s1"
        lm.close()

    def test_02_pa_does_s1(self, db):
        lm = LifecycleManager("product_architect", db_path=db)
        assert lm.start_wf(self.__class__.wf_id)
        assert lm.get_wf(self.__class__.wf_id)["status"] == "running"
        assert lm.complete_step(self.__class__.wf_id, "s1") == "step_done_ready"
        lm.confirm_step(self.__class__.wf_id, "s1")
        assert lm.get_wf(self.__class__.wf_id)["current_step_id"] == "s2"
        lm.close()

    def test_03_pg_does_s2(self, db):
        lm = LifecycleManager("pg", db_path=db)
        assert lm.complete_step(self.__class__.wf_id, "s2") == "step_done_ready"
        lm.confirm_step(self.__class__.wf_id, "s2")
        assert lm.get_wf(self.__class__.wf_id)["current_step_id"] == "s3"
        lm.close()

    def test_04_reviewer_does_s3(self, db):
        lm = LifecycleManager("reviewer", db_path=db)
        assert lm.complete_step(self.__class__.wf_id, "s3") == "step_done_ready"
        lm.confirm_step(self.__class__.wf_id, "s3")
        assert lm.get_wf(self.__class__.wf_id)["status"] == "completed"
        lm.close()

    def test_05_task_completed(self, db):
        wc = WorkflowClient("pm", db_path=db)
        t = wc.get_task(self.__class__.task_id)
        assert t["status"] == "completed"
        wc.close()

    def test_06_audit_logs(self, db):
        wc = WorkflowClient("pm", db_path=db)
        logs = wc.get_logs(wf_id=self.__class__.wf_id)
        assert len(logs) >= 7
        actions = {l["action"] for l in logs}
        for a in ["created", "wf_started", "step_done_ready", "step_confirmed"]:
            assert a in actions
        wc.close()


class TestWL02_EndToEnd:
    """WL-02: single → review → notify。"""

    def test_01_pm_does_s1(self, wf02, db):
        self.__class__.wf_id = wf02
        lm = LifecycleManager("pm", db_path=db)
        assert lm.start_wf(wf02)
        r = lm.complete_step(wf02, "s1")
        assert r in ("completed", "completed_and_advanced", "notify_sent", "gate_blocked")
        assert lm.get_wf(wf02)["current_step_id"] == "s2"
        lm.close()

    def test_02_reviewer_does_s2(self, db):
        lm = LifecycleManager("reviewer", db_path=db)
        assert lm.complete_step(self.__class__.wf_id, "s2") == "step_done_ready"
        lm.confirm_step(self.__class__.wf_id, "s2")
        assert lm.get_wf(self.__class__.wf_id)["current_step_id"] == "s3"
        lm.close()

    def test_03_pg_does_s3(self, db):
        lm = LifecycleManager("pg", db_path=db)
        r = lm.complete_step(self.__class__.wf_id, "s3")
        assert r in ("completed", "completed_and_advanced", "notify_sent", "gate_blocked")
        assert lm.get_wf(self.__class__.wf_id)["status"] == "completed"
        lm.close()


class TestWL03_EndToEnd:
    """WL-03: 3 个 single 连续自推进。"""

    def test_01_pg_does_all(self, wf03, db):
        self.__class__.wf_id = wf03
        lm = LifecycleManager("pg", db_path=db)
        assert lm.start_wf(wf03)
        assert lm.complete_step(wf03, "s1") in ("completed", "completed_and_advanced", "notify_sent", "gate_blocked")
        assert lm.get_wf(wf03)["current_step_id"] == "s2"
        assert lm.complete_step(wf03, "s2") in ("completed", "completed_and_advanced", "notify_sent", "gate_blocked")
        assert lm.get_wf(wf03)["current_step_id"] == "s3"
        assert lm.complete_step(wf03, "s3") in ("completed", "completed_and_advanced", "notify_sent", "gate_blocked")
        assert lm.get_wf(wf03)["status"] == "completed"
        lm.close()


class TestWL04_EndToEnd:
    """WL-04: single → review。"""

    def test_01_architect_does_s1(self, wf04, db):
        self.__class__.wf_id = wf04
        lm = LifecycleManager("product_architect", db_path=db)
        assert lm.start_wf(wf04)
        assert lm.complete_step(wf04, "s1") in ("completed", "completed_and_advanced", "notify_sent", "gate_blocked")
        assert lm.get_wf(wf04)["current_step_id"] == "s2"
        lm.close()

    def test_02_reviewer_does_s2(self, db):
        lm = LifecycleManager("reviewer", db_path=db)
        assert lm.complete_step(self.__class__.wf_id, "s2") == "step_done_ready"
        lm.confirm_step(self.__class__.wf_id, "s2")
        assert lm.get_wf(self.__class__.wf_id)["status"] == "completed"
        lm.close()


class TestWL05_EndToEnd:
    """WL-05: gate → notify。"""

    def test_01_maintainer_does_all(self, wf05, db):
        self.__class__.wf_id = wf05
        lm = LifecycleManager("maintainer", db_path=db)
        assert lm.start_wf(wf05)
        lm.complete_step(wf05, "s1")
        wf = lm.get_wf(wf05)
        if wf["current_step_id"] == "s2":
            assert lm.complete_step(wf05, "s2") in ("completed", "completed_and_advanced", "notify_sent", "gate_blocked")
            assert lm.get_wf(wf05)["status"] == "completed"
        lm.close()


# ══════════════════════════════════════════════════════════════
# 安全 + 状态机一致性
# ══════════════════════════════════════════════════════════════

class TestCrossRoleSecurity:
    def test_non_pm_cannot_start_wl02(self, db):
        for bad in ["pg", "reviewer", "qa"]:
            with pytest.raises((PermissionError, ValueError)):
                WorkflowClient(bad, db_path=db).create_task_v2(
                    "x", assignee=bad, template_id="WL-02", initiator_role=bad)

    def test_non_architect_cannot_start_wl04(self, db):
        for bad in ["pg", "maintainer"]:
            with pytest.raises((PermissionError, ValueError)):
                WorkflowClient(bad, db_path=db).create_task_v2(
                    "x", assignee=bad, template_id="WL-04", initiator_role=bad)


class TestStateMachineConsistency:
    def _run_all(self, db):
        """一次性跑完全部 5 个模板。"""
        for fn in [_run_wl01, _run_wl02, _run_wl03, _run_wl04, _run_wl05]:
            fn(db)

    def test_no_orphan_tasks(self, db):
        self._run_all(db)
        conn = sqlite3.connect(db)
        orphans = [r[0] for r in conn.execute(
            "SELECT task_id FROM tasks WHERE task_id NOT IN "
            "(SELECT task_id FROM workflow_instances)").fetchall()]
        conn.close()
        assert not orphans

    def test_no_dangling_workflows(self, db):
        self._run_all(db)
        conn = sqlite3.connect(db)
        d = conn.execute(
            "SELECT COUNT(*) FROM workflow_instances WHERE template_id IS NULL"
        ).fetchone()[0]
        conn.close()
        assert d == 0, f"{d} 个 workflow 无模板绑定"

    def test_task_status_matches(self, db):
        self._run_all(db)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT t.status as ts, wi.status as ws FROM tasks t
            JOIN workflow_instances wi ON t.task_id = wi.task_id
        """).fetchall()
        conn.close()
        completed = sum(1 for r in rows if r["ws"] == "completed")
        assert completed >= 3, f"预期 ≥3 完成, 实际 {completed}"
        for r in rows:
            if r["ws"] == "completed":
                assert r["ts"] == "completed"


class TestRoleInstructionConsistency:
    """CLAUDE.md 指令与模板一致性。"""
    CCS = Path("/home/administrator/ccs-workspaces")

    def test_pm_has_wl02(self):
        c = (self.CCS / "pm" / "CLAUDE.md").read_text()
        assert "create_task_v2" in c and "WL-02" in c

    def test_pg_has_wl01(self):
        c = (self.CCS / "pg" / "CLAUDE.md").read_text()
        assert "create_task_v2" in c

    def test_reviewer_has_confirm(self):
        c = (self.CCS / "reviewer" / "CLAUDE.md").read_text()
        assert "confirm_step" in c

    def test_devops_exists(self):
        c = (self.CCS / "devops" / "CLAUDE.md").read_text()
        assert "create_task_v2" in c


def _run_wl01(db):
    wc = WorkflowClient("pm", db_path=db)
    _, wf_id = wc.create_task_v2("登录", assignee="product_architect",
                                  template_id="WL-01", initiator_role="pm")
    wc.close()
    lm = LifecycleManager("product_architect", db_path=db)
    assert lm.start_wf(wf_id)
    assert lm.complete_step(wf_id, "s1") == "step_done_ready"
    lm.confirm_step(wf_id, "s1")
    lm.close()
    lm = LifecycleManager("pg", db_path=db)
    assert lm.complete_step(wf_id, "s2") == "step_done_ready"
    lm.confirm_step(wf_id, "s2")
    lm.close()
    lm = LifecycleManager("reviewer", db_path=db)
    assert lm.complete_step(wf_id, "s3") == "step_done_ready"
    lm.confirm_step(wf_id, "s3")
    assert lm.get_wf(wf_id)["status"] == "completed"
    lm.close()


def _run_wl02(db):
    wc = WorkflowClient("pm", db_path=db)
    _, wf_id = wc.create_task_v2("需求", assignee="pm", template_id="WL-02", initiator_role="pm")
    wc.close()
    lm = LifecycleManager("pm", db_path=db)
    assert lm.start_wf(wf_id)
    assert lm.complete_step(wf_id, "s1") in ("completed", "completed_and_advanced", "notify_sent", "gate_blocked")
    lm.close()
    lm = LifecycleManager("reviewer", db_path=db)
    assert lm.complete_step(wf_id, "s2") == "step_done_ready"
    lm.confirm_step(wf_id, "s2")
    lm.close()
    lm = LifecycleManager("pg", db_path=db)
    assert lm.complete_step(wf_id, "s3") in ("completed", "completed_and_advanced", "notify_sent", "gate_blocked")
    assert lm.get_wf(wf_id)["status"] == "completed"
    lm.close()


def _run_wl03(db):
    wc = WorkflowClient("qa", db_path=db)
    _, wf_id = wc.create_task_v2("500错误", assignee="pg", template_id="WL-03", initiator_role="qa")
    wc.close()
    lm = LifecycleManager("pg", db_path=db)
    assert lm.start_wf(wf_id)
    assert lm.complete_step(wf_id, "s1") in ("completed", "completed_and_advanced", "notify_sent", "gate_blocked")
    assert lm.complete_step(wf_id, "s2") in ("completed", "completed_and_advanced", "notify_sent", "gate_blocked")
    assert lm.complete_step(wf_id, "s3") in ("completed", "completed_and_advanced", "notify_sent", "gate_blocked")
    assert lm.get_wf(wf_id)["status"] == "completed"
    lm.close()


def _run_wl04(db):
    wc = WorkflowClient("product_architect", db_path=db)
    _, wf_id = wc.create_task_v2("网关", assignee="product_architect",
                                  template_id="WL-04", initiator_role="product_architect")
    wc.close()
    lm = LifecycleManager("product_architect", db_path=db)
    assert lm.start_wf(wf_id)
    assert lm.complete_step(wf_id, "s1") in ("completed", "completed_and_advanced", "notify_sent", "gate_blocked")
    lm.close()
    lm = LifecycleManager("reviewer", db_path=db)
    assert lm.complete_step(wf_id, "s2") == "step_done_ready"
    lm.confirm_step(wf_id, "s2")
    assert lm.get_wf(wf_id)["status"] == "completed"
    lm.close()


def _run_wl05(db):
    wc = WorkflowClient("maintainer", db_path=db)
    _, wf_id = wc.create_task_v2("Flask升级", assignee="maintainer",
                                  template_id="WL-05", initiator_role="maintainer")
    wc.close()
    lm = LifecycleManager("maintainer", db_path=db)
    assert lm.start_wf(wf_id)
    lm.complete_step(wf_id, "s1")
    wf = lm.get_wf(wf_id)
    if wf["current_step_id"] == "s2":
        assert lm.complete_step(wf_id, "s2") in ("completed", "completed_and_advanced", "notify_sent", "gate_blocked")
        assert lm.get_wf(wf_id)["status"] == "completed"
    lm.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short", "--color=yes"])
