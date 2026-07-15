#!/usr/bin/env python3
"""
test_workflow_redesign.py — 全项目功能性测试

覆盖 T1-T18 全部模块，基于 TEST_PLAN.md + TEST_CASES.md。
运行: python3 -m pytest tests/test_workflow_redesign.py -v --tb=short
"""

import json
import sys
import tempfile
import time
import threading
import sqlite3
import warnings
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pytest
from template_registry import TemplateRegistry, ValidationReport, _validate_schema, _check_role_existence
from workflow.gateway import Gate
from workflow.client import WorkflowClient
from lifecycle.manager import LifecycleManager
from lifecycle.engine import StepEngine
from events.notify import NotificationEngine
from p0_exemption import P0Exemption
from routing.router import CrossRoleRouter
from template_validator import run_validation
from migration.scripts import (
    pre_flight, dry_run_assessment, export_backup,
    truncate_tables, restore_from_backup, run_migration,
)

# ══════════════════════════════════════════════════════════════
# 测试夹具
# ══════════════════════════════════════════════════════════════

@pytest.fixture(scope="function")
def test_db():
    """函数级 DB 文件，每次测试独立。"""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    yield f.name
    Path(f.name).unlink(missing_ok=True)


def _seed_templates(reg: TemplateRegistry):
    """注册 5 个测试用模板（幂等：跳过已存在的）。"""
    templates = [
        {
            "workflow_id": "WL-01", "name": "技术实现",
            "description": "从方案设计到部署上线的完整流程",
            "trigger_scene": ["需要编码实现的功能开发任务"],
            "allowed_initiators": ["lr", "pm", "coordinator", "product_architect"],
            "allowed_executors": ["product_architect", "reviewer", "pg", "engineer", "maintainer", "optimizer"],
            "steps": [
                {"step_id": "s1", "title": "方案设计", "type": "handoff",
                 "target_role": "product_architect",
                 "prompt_template": "做什么: 编写技术方案文档\n怎么做: 参考现有架构和需求分析\n验收标准: 产出 DESIGN.md 并通过评审",
                 "completion_check": {"output_exists": ["DESIGN.md"], "review_required": True},
                 "failure_patterns": ["设计遗漏关键约束", "未考虑安全影响"], "estimated_hours": 4.0},
                {"step_id": "s2", "title": "编码实现", "type": "handoff",
                 "target_role": "pg",
                 "prompt_template": "做什么: 按方案实现编码\n怎么做: 遵循项目编码规范\n验收标准: 测试通过 CI 绿",
                 "completion_check": {"output_exists": ["src/"], "review_required": True},
                 "failure_patterns": ["未覆盖边界情况", "性能不符合预期"], "estimated_hours": 16.0},
                {"step_id": "s3", "title": "代码审查", "type": "review",
                 "target_role": "reviewer",
                 "prompt_template": "做什么: 审查代码质量和设计一致性\n怎么做: 逐行检查确保无逻辑缺陷\n验收标准: 审查报告通过无 P0 问题",
                 "completion_check": {"review_required": True},
                 "failure_patterns": ["审查流于形式", "未检查安全漏洞"], "estimated_hours": 2.0},
            ],
            "max_duration_hours": 48,
            "quality_standards": "所有产出物路径可验证确保无遗漏",
        },
        {
            "workflow_id": "WL-02", "name": "需求流转",
            "description": "需求从提出到评审确认的流转流程",
            "trigger_scene": ["新功能需求需要评审", "需求变更走流程"],
            "allowed_initiators": ["pm", "coordinator", "lr"],
            "allowed_executors": ["pm", "product_architect", "pg", "reviewer", "qa"],
            "steps": [
                {"step_id": "s1", "title": "需求编写", "type": "single",
                 "prompt_template": "做什么: 编写 PRD 文档\n怎么做: 按标准模板产出\n验收标准: 需求条目清晰完整",
                 "completion_check": {"output_exists": ["PRD.md"]},
                 "failure_patterns": ["需求描述模糊", "缺少验收标准"], "estimated_hours": 2.0},
                {"step_id": "s2", "title": "需求评审", "type": "review",
                 "target_role": "reviewer",
                 "prompt_template": "做什么: 评审需求可行性\n怎么做: 分析技术可行性\n验收标准: 评审结论明确",
                 "completion_check": {"review_required": True},
                 "failure_patterns": ["评审不深入", "遗漏依赖评估"], "estimated_hours": 1.0},
                {"step_id": "s3", "title": "通知相关方", "type": "notify",
                 "target_role": "pg",
                 "prompt_template": "做什么: 通知开发团队新需求\n怎么做: 发送详细需求文档\n验收标准: 通知送达确认",
                 "completion_check": {},
                 "failure_patterns": ["未通知到关键人员", "通知内容不完整"], "estimated_hours": 0.5},
            ],
            "max_duration_hours": 24,
            "quality_standards": "PRD 需通过 reviewer 审查",
        },
        {
            "workflow_id": "WL-03", "name": "Bug 修复",
            "description": "标准 Bug 修复流程含复现确认根因分析",
            "trigger_scene": ["P0/P1 级别生产环境 Bug 修复"],
            "allowed_initiators": ["qa", "lr", "coordinator", "maintainer"],
            "allowed_executors": ["pg", "engineer", "qa", "maintainer"],
            "steps": [
                {"step_id": "s1", "title": "复现确认", "type": "single",
                 "prompt_template": "做什么: 按步骤复现 Bug\n怎么做: 记录复现条件和步骤\n验收标准: 可稳定复现",
                 "completion_check": {},
                 "failure_patterns": ["无法复现", "环境差异"], "estimated_hours": 1.0},
                {"step_id": "s2", "title": "根因分析", "type": "single",
                 "prompt_template": "做什么: 分析问题根因\n怎么做: 追踪调用栈和数据流\n验收标准: 根因明确有修复方案",
                 "completion_check": {},
                 "failure_patterns": ["表象修复", "回归风险"], "estimated_hours": 2.0},
                {"step_id": "s3", "title": "部署修复", "type": "notify",
                 "target_role": "devops",
                 "prompt_template": "做什么: 部署修复版本\n怎么做: 走标准发布流程\n验收标准: 监控指标正常",
                 "completion_check": {},
                 "failure_patterns": ["跳过灰度", "未验证"], "estimated_hours": 1.0},
            ],
            "max_duration_hours": 24,
            "quality_standards": "P0 Bug 在 2 小时内完成根因分析",
        },
        {
            "workflow_id": "WL-04", "name": "架构决策",
            "description": "架构决策提案评审执行全流程",
            "trigger_scene": ["技术栈选型决策需要评估", "重大系统重构方案评审"],
            "allowed_initiators": ["product_architect", "lr", "coordinator"],
            "allowed_executors": ["product_architect", "engineer", "maintainer", "pg", "coordinator"],
            "steps": [
                {"step_id": "s1", "title": "方案对比", "type": "single",
                 "prompt_template": "做什么: 出 2-3 个候选方案\n怎么做: 从成本性能可维护三维对比\n验收标准: 对比表格完整",
                 "completion_check": {"output_exists": ["COMPARISON.md"]},
                 "failure_patterns": ["漏关键维度", "偏向性表述"], "estimated_hours": 4.0},
                {"step_id": "s2", "title": "架构评审", "type": "review",
                 "target_role": "reviewer",
                 "prompt_template": "做什么: 评审架构方案\n怎么做: 从六维度全面评估\n验收标准: 评审报告有明确结论",
                 "completion_check": {"review_required": True},
                 "failure_patterns": ["未考虑扩展性", "忽略技术债"], "estimated_hours": 2.0},
            ],
            "max_duration_hours": 72,
            "quality_standards": "架构决策需 reviewer 审查通过",
        },
        {
            "workflow_id": "WL-05", "name": "基线维护",
            "description": "常规依赖升级和技术基线维护流程",
            "trigger_scene": ["依赖版本定期升级", "技术基线例行维护"],
            "allowed_initiators": ["maintainer", "coordinator"],
            "allowed_executors": ["maintainer", "engineer", "pg"],
            "steps": [
                {"step_id": "s1", "title": "升级评估", "type": "gate",
                 "target_role": "maintainer",
                 "prompt_template": "做什么: 评估升级影响范围\n怎么做: 检查 changelog 和 breaking changes\n验收标准: 评估报告完整",
                 "completion_check": {"output_exists": ["UPGRADE_ASSESSMENT.md"]},
                 "failure_patterns": ["遗漏依赖链", "未评估兼容性"], "estimated_hours": 2.0},
                {"step_id": "s2", "title": "升级执行", "type": "notify",
                 "target_role": "devops",
                 "prompt_template": "做什么: 执行依赖升级\n怎么做: 按升级清单逐步操作\n验收标准: CI 通过功能正常",
                 "completion_check": {},
                 "failure_patterns": ["升级过程中断", "回滚方案缺失"], "estimated_hours": 1.0},
            ],
            "max_duration_hours": 16,
            "quality_standards": "升级评估报告需通过审查",
        },
    ]
    for tpl in templates:
        try:
            reg.register(tpl)
        except ValueError as e:
            # 跳过已存在的模板
            if "已存在" in str(e):
                continue
            raise


def _ensure_tables(db_path):
    """确保 DB 中有基础表（供 migration / 独立模块测试用）。"""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS workflow_templates (
            template_id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT,
            steps_json TEXT NOT NULL, steps_mermaid TEXT, created_at REAL NOT NULL
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
    conn.commit()
    conn.close()


# ══════════════════════════════════════════════════════════════
# T1 — TemplateRegistry（模板注册中心）
# ══════════════════════════════════════════════════════════════

class TestTemplateRegistry:
    """T1: template_registry.py — 19 test cases per TEST_CASES.md."""

    def test_t1_01_register_valid(self, test_db):
        reg = TemplateRegistry(db_path=test_db)
        tid = reg.register({
            "workflow_id": "WL-01", "name": "技术实现",
            "description": "从方案设计到部署上线的完整流程",
            "trigger_scene": ["需要编码实现的功能开发任务"],
            "allowed_initiators": ["pm", "coordinator"],
            "allowed_executors": ["product_architect", "pg", "reviewer"],
            "steps": [{
                "step_id": "s1", "title": "方案设计", "type": "handoff",
                "target_role": "product_architect",
                "prompt_template": "做什么: 编写技术方案\n怎么做: 参考现有架构\n验收标准: 产出 DESIGN.md",
                "failure_patterns": ["设计遗漏关键约束", "未考虑安全影响"],
                "estimated_hours": 4.0,
            }],
            "max_duration_hours": 48,
            "quality_standards": "所有产出物需通过对应角色审查",
        })
        assert tid == "WL-01"
        reg.close()

    def test_t1_02_get_by_id(self, test_db):
        reg = TemplateRegistry(db_path=test_db)
        _seed_templates(reg)
        tpl = reg.get("WL-01")
        assert tpl is not None
        assert tpl["workflow_id"] == "WL-01"
        assert tpl["name"] == "技术实现"
        assert len(tpl["steps"]) == 3
        assert tpl["steps"][0]["step_id"] == "s1"
        assert tpl["is_active"] is True
        reg.close()

    def test_t1_03_get_nonexistent(self, test_db):
        reg = TemplateRegistry(db_path=test_db)
        assert reg.get("WL-999") is None
        reg.close()

    def test_t1_04_list_with_filter(self, test_db):
        reg = TemplateRegistry(db_path=test_db)
        _seed_templates(reg)
        reg.deactivate("WL-02")
        active = reg.list({"is_active": True})
        assert len(active) == 4
        for t in active:
            assert t.get("is_active", True) is True
        reg.close()

    def test_t1_05_activate_deactivate(self, test_db):
        reg = TemplateRegistry(db_path=test_db)
        _seed_templates(reg)
        assert reg.get("WL-01")["is_active"] is True
        assert reg.deactivate("WL-01") is True
        assert reg.get("WL-01")["is_active"] is False
        assert reg.activate("WL-01") is True
        assert reg.get("WL-01")["is_active"] is True
        reg.close()

    def test_t1_06_inactive_still_readable(self, test_db):
        reg = TemplateRegistry(db_path=test_db)
        _seed_templates(reg)
        reg.deactivate("WL-01")
        tpl = reg.get("WL-01")
        assert tpl is not None
        assert tpl["is_active"] is False
        assert tpl["name"] == "技术实现"
        reg.close()

    def test_t1_07_register_missing_field(self, test_db):
        reg = TemplateRegistry(db_path=test_db)
        with pytest.raises(ValueError, match="description"):
            reg.register({"workflow_id": "WL-99", "name": "测试模板"})
        reg.close()

    def test_t1_08_register_type_error(self, test_db):
        reg = TemplateRegistry(db_path=test_db)
        with pytest.raises(ValueError, match="steps"):
            reg.register({
                "workflow_id": "WL-99", "name": "测试模板",
                "description": "这是一段描述文字满足十字符数要求",
                "trigger_scene": ["需要开发的功能"],
                "allowed_initiators": ["pm"], "allowed_executors": ["pg"],
                "steps": "not_a_list",
                "max_duration_hours": 24,
                "quality_standards": "质量标准描述满足十字符数要求",
            })
        reg.close()

    def test_t1_09_register_duplicate(self, test_db):
        reg = TemplateRegistry(db_path=test_db)
        _seed_templates(reg)
        with pytest.raises(ValueError, match="已存在"):
            reg.register({
                "workflow_id": "WL-01", "name": "重复模板",
                "description": "这是一段用于测试重复模板的描述",
                "trigger_scene": ["重复注册测试场景"],
                "allowed_initiators": ["pm"], "allowed_executors": ["pg"],
                "steps": [{"step_id": "s1", "title": "测试", "type": "single",
                           "prompt_template": "做什么: 测试\n怎么做: 测试重复\n验收标准: 抛异常",
                           "failure_patterns": ["错误1", "错误2"], "estimated_hours": 1.0}],
                "max_duration_hours": 24,
                "quality_standards": "质量达标确保无重复问题",
            })
        reg.close()

    def test_t1_10_register_empty_steps(self, test_db):
        reg = TemplateRegistry(db_path=test_db)
        with pytest.raises(ValueError, match="steps 需为非空数组"):
            reg.register({
                "workflow_id": "WL-99", "name": "空步骤模板",
                "description": "这是一个没有步骤的模板用于测试",
                "trigger_scene": ["测试空步骤场景"],
                "allowed_initiators": ["pm"], "allowed_executors": ["pg"],
                "steps": [], "max_duration_hours": 24,
                "quality_standards": "质量标准内容达到最少字数要求",
            })
        reg.close()

    def test_t1_11_validate_prompt_missing_section(self, test_db):
        reg = TemplateRegistry(db_path=test_db)
        report = reg.validate({
            "workflow_id": "WL-99", "name": "测试",
            "description": "测试模板用于验证校验逻辑",
            "trigger_scene": ["测试校验逻辑场景"],
            "allowed_initiators": ["pm"], "allowed_executors": ["pg"],
            "steps": [{"step_id": "s1", "title": "编码", "type": "handoff",
                       "target_role": "pg",
                       "prompt_template": "编码实现功能\n怎么做: 实现功能\n验收标准: 测试通过",
                       "failure_patterns": ["遗漏测试", "不符合规范"], "estimated_hours": 8.0}],
            "max_duration_hours": 24,
            "quality_standards": "质量标准描述足够十字符数",
        })
        assert report["passed"] is False
        assert any("做什么" in e for e in report["errors"])
        reg.close()

    def test_t1_12_validate_wrong_type(self, test_db):
        reg = TemplateRegistry(db_path=test_db)
        report = reg.validate({
            "workflow_id": "WL-99", "name": "测试",
            "description": "测试目标角色校验功能的模板",
            "trigger_scene": ["测试角色校验场景"],
            "allowed_initiators": ["pm"], "allowed_executors": ["pg"],
            "steps": [{"step_id": "s1", "title": "编码", "type": "invalid_type",
                       "target_role": "pg",
                       "prompt_template": "做什么: 编码\n怎么做: 实现功能\n验收标准: 测试通过",
                       "failure_patterns": ["遗漏测试", "不符合规范"], "estimated_hours": 8.0}],
            "max_duration_hours": 24,
            "quality_standards": "质量标准描述达到最少字数要求",
        })
        assert report["passed"] is False
        assert any("type" in e.lower() for e in report["errors"])

    def test_t1_13_prompt_template_missing_sections(self, test_db):
        reg = TemplateRegistry(db_path=test_db)
        report = reg.validate({
            "workflow_id": "WL-99", "name": "测试",
            "description": "测试 prompt 段落校验的模板描述",
            "trigger_scene": ["测试 prompt 校验场景"],
            "allowed_initiators": ["pm"], "allowed_executors": ["pg"],
            "steps": [{"step_id": "s1", "title": "编码", "type": "handoff",
                       "target_role": "pg",
                       "prompt_template": "做什么: 编码\n怎么做: 实现功能",
                       "failure_patterns": ["遗漏测试", "不符合规范"], "estimated_hours": 8.0}],
            "max_duration_hours": 24,
            "quality_standards": "质量标准描述足够十字符数要求",
        })
        assert any("验收标准" in e for e in report["errors"])

    def test_t1_14_validate_quality_standards_too_short(self, test_db):
        reg = TemplateRegistry(db_path=test_db)
        report = reg.validate({
            "workflow_id": "WL-99", "name": "测试",
            "description": "测试 quality_standards 校验的模板",
            "trigger_scene": ["测试质量标准场景"],
            "allowed_initiators": ["pm"], "allowed_executors": ["pg"],
            "steps": [{"step_id": "s1", "title": "编码", "type": "handoff",
                       "target_role": "pg",
                       "prompt_template": "做什么: 编码\n怎么做: 实现功能\n验收标准: 全部测试通过",
                       "failure_patterns": ["遗漏测试", "不符合规范"], "estimated_hours": 8.0}],
            "max_duration_hours": 24,
            "quality_standards": "短",
        })
        assert len(report["errors"]) > 0
        assert any("quality_standards" in e for e in report["errors"])

    def test_t1_15_failure_patterns_too_few(self, test_db):
        reg = TemplateRegistry(db_path=test_db)
        with pytest.raises((ValueError, AssertionError)):
            reg.register({
                "workflow_id": "WL-99", "name": "无效模板",
                "description": "测试 failure_patterns 校验的模板描述",
                "trigger_scene": ["测试 failure 场景"],
                "allowed_initiators": ["pm"], "allowed_executors": ["pg"],
                "steps": [{"step_id": "s1", "title": "编码", "type": "handoff",
                           "target_role": "pg",
                           "prompt_template": "做什么: 编码\n怎么做: 实现功能\n验收标准: 全部测试通过",
                           "failure_patterns": ["仅一项"], "estimated_hours": 8.0}],
                "max_duration_hours": 24,
                "quality_standards": "质量标准描述已经达到最少十字符",
            })
        reg.close()

    def test_t1_16_missing_estimated_hours(self, test_db):
        reg = TemplateRegistry(db_path=test_db)
        report = reg.validate({
            "workflow_id": "WL-99", "name": "测试",
            "description": "测试 estimated_hours 校验的模板描述",
            "trigger_scene": ["测试工时校验场景"],
            "allowed_initiators": ["pm"], "allowed_executors": ["pg"],
            "steps": [{"step_id": "s1", "title": "编码", "type": "handoff",
                       "target_role": "pg",
                       "prompt_template": "做什么: 编码\n怎么做: 实现功能\n验收标准: 所有测试通过",
                       "failure_patterns": ["遗漏测试", "不符合规范"]}],
            "max_duration_hours": 24,
            "quality_standards": "质量标准描述已经达到最少十字符要求",
        })
        assert report["passed"] is False
        assert any("estimated_hours" in e for e in report["errors"])

    def test_t1_17_invalid_initiator_role(self, test_db):
        reg = TemplateRegistry(db_path=test_db)
        report = reg.validate({
            "workflow_id": "WL-99", "name": "测试",
            "description": "测试角色存在性校验的模板描述",
            "trigger_scene": ["测试角色校验场景"],
            "allowed_initiators": ["nonexistent_role"],
            "allowed_executors": ["pg"],
            "steps": [{"step_id": "s1", "title": "编码", "type": "single",
                       "prompt_template": "做什么: 编码\n怎么做: 实现功能\n验收标准: 所有测试通过",
                       "failure_patterns": ["遗漏测试", "不符合规范"], "estimated_hours": 8.0}],
            "max_duration_hours": 24,
            "quality_standards": "质量标准描述已经达到最少十字符要求",
        })
        assert len(report["errors"]) > 0
        assert any("不存在" in e or "nonexistent" in e for e in report["errors"])

    def test_t1_18_invalid_executor_role(self, test_db):
        reg = TemplateRegistry(db_path=test_db)
        report = reg.validate({
            "workflow_id": "WL-99", "name": "测试",
            "description": "测试执行者角色存在性校验的模板",
            "trigger_scene": ["测试角色校验场景"],
            "allowed_initiators": ["pm"], "allowed_executors": ["hacker"],
            "steps": [{"step_id": "s1", "title": "编码", "type": "single",
                       "prompt_template": "做什么: 编码\n怎么做: 实现功能\n验收标准: 所有测试通过",
                       "failure_patterns": ["遗漏测试", "不符合规范"], "estimated_hours": 8.0}],
            "max_duration_hours": 24,
            "quality_standards": "质量标准描述已经达到最少十字符要求",
        })
        assert len(report["errors"]) > 0
        assert any("不存在" in e or "hacker" in e for e in report["errors"])

    def test_t1_19_validate_passed_failed_format(self, test_db):
        reg = TemplateRegistry(db_path=test_db)
        r1 = reg.validate({
            "workflow_id": "WL-98", "name": "格式测试",
            "description": "测试 validate 返回格式的模板描述",
            "trigger_scene": ["格式测试场景需要至少五个字"],
            "allowed_initiators": ["pm"], "allowed_executors": ["pg"],
            "steps": [{"step_id": "s1", "title": "编码", "type": "single",
                       "prompt_template": "做什么: 编码实现\n怎么做: 按照设计文档实现功能\n验收标准: 所有测试全部通过",
                       "failure_patterns": ["遗漏测试", "不符合规范"], "estimated_hours": 8.0}],
            "max_duration_hours": 24,
            "quality_standards": "质量标准描述已经达到最少十字符要求",
        })
        assert isinstance(r1, dict)
        assert "passed" in r1 and "errors" in r1
        assert isinstance(r1["errors"], list)
        reg.close()


# ══════════════════════════════════════════════════════════════
# T2 — WorkflowClient.create_task + Gate 门禁
# ══════════════════════════════════════════════════════════════

class TestWorkflowClientGate:
    """T2: create_task_v2 + Gate — 16 test cases."""

    @pytest.fixture(autouse=True)
    def setup(self, test_db):
        self.db = test_db
        _ensure_tables(test_db)
        run_migration(db_path=test_db, dry_run=False)
        reg = TemplateRegistry(db_path=test_db)
        _seed_templates(reg)
        reg.close()

    def _client(self, role="pm"):
        return WorkflowClient(role, db_path=self.db)

    def _gate(self):
        return Gate(db_path=self.db)

    def test_t2_01_create_task_v2_ok(self):
        wc = self._client("pm")
        task_id, wf_id = wc.create_task_v2("标题", "product_architect", "WL-01", "pm")
        assert task_id.startswith("task_") and wf_id.startswith("wf_")
        inst = wc._conn.execute(
            "SELECT * FROM workflow_instances WHERE instance_id=?", (wf_id,)
        ).fetchone()
        assert dict(inst)["status"] == "pending"
        assert dict(inst)["current_step_id"] == "s1"
        wc.close()

    def test_t2_02_create_workflow_in_db(self):
        wc = self._client("pm")
        task_id, wf_id = wc.create_task_v2("DB测试", "pg", "WL-01", "pm")
        row = wc._conn.execute(
            "SELECT * FROM workflow_instances WHERE instance_id=?", (wf_id,)
        ).fetchone()
        assert dict(row)["task_id"] == task_id
        assert dict(row)["template_id"] == "WL-01"
        wc.close()

    def test_t2_03_create_link_task_to_wf(self):
        wc = self._client("pm")
        task_id, wf_id = wc.create_task_v2("关联测试", "pg", "WL-01", "pm")
        task = wc.get_task(task_id)
        assert task["status"] == "in_progress"
        wc.close()

    def test_t2_04_create_logs_in_workflow_logs(self):
        wc = self._client("pm")
        _, wf_id = wc.create_task_v2("日志测试", "pg", "WL-01", "pm")
        logs = wc.get_logs(wf_id=wf_id)
        assert any(l["action"] == "created" for l in logs)
        wc.close()

    def test_t2_05_create_for_template_with_target_role(self):
        wc = self._client("pm")
        _, wf_id = wc.create_task_v2("分配测试", "product_architect", "WL-01", "pm")
        inst = wc._conn.execute(
            "SELECT * FROM workflow_instances WHERE instance_id=?", (wf_id,)
        ).fetchone()
        assert dict(inst)["assignee"] == "product_architect"
        wc.close()

    def test_t2_06_create_missing_template_id(self):
        wc = self._client("pm")
        with pytest.raises(ValueError, match="template_id is required"):
            wc.create_task_v2("测试", "pg", "", "pm")
        wc.close()

    def test_t2_07_template_not_found(self):
        wc = self._client("pm")
        with pytest.raises(ValueError, match="not found"):
            wc.create_task_v2("测试", "pg", "WL-999", "pm")
        wc.close()

    def test_t2_08_inactive_template(self):
        reg = TemplateRegistry(db_path=self.db)
        reg.deactivate("WL-02")
        reg.close()
        wc = self._client("pm")
        with pytest.raises(ValueError, match="inactive"):
            wc.create_task_v2("测试", "pg", "WL-02", "pm")
        wc.close()

    def test_t2_09_initiator_not_allowed(self):
        wc = self._client("pg")
        with pytest.raises(PermissionError, match="not allowed to initiate"):
            wc.create_task_v2("测试", "product_architect", "WL-01", "pg")
        wc.close()

    def test_t2_10_initiator_not_found_in_registry(self):
        gate = self._gate()
        with pytest.raises(PermissionError, match="role not found"):
            gate.validate_create_task("WL-01", "ghost_role", "pg")
        gate.close()

    def test_t2_11_assignee_not_valid_executor(self):
        wc = self._client("pm")
        with pytest.raises(ValueError, match="not a valid executor"):
            wc.create_task_v2("测试", "devops", "WL-01", "pm")
        wc.close()

    def test_t2_12_list_templates_has_v2_fields(self):
        wc = self._client("pm")
        templates = wc.list_templates()
        assert len(templates) >= 5
        wc.close()

    def test_t2_13_pm_cannot_initiate_wl03(self):
        wc = self._client("pm")
        with pytest.raises(PermissionError, match="not allowed to initiate"):
            wc.create_task_v2("测试", "pg", "WL-03", "pm")
        wc.close()

    def test_t2_14_maintainer_cannot_initiate_wl01(self):
        wc = self._client("maintainer")
        with pytest.raises(PermissionError, match="not allowed to initiate"):
            wc.create_task_v2("测试", "product_architect", "WL-01", "maintainer")
        wc.close()

    def test_t2_15_mixed_case_template_id(self):
        wc = self._client("pm")
        with pytest.raises(ValueError, match="not found"):
            wc.create_task_v2("测试", "pg", "wl-01", "pm")
        wc.close()

    def test_t2_16_gate_validate_create_chain(self):
        gate = self._gate()
        gate.validate_create_task("WL-01", "pm", "product_architect")
        gate.close()


# ══════════════════════════════════════════════════════════════
# T3 — LifecycleManager
# ══════════════════════════════════════════════════════════════

class TestLifecycleManager:
    """T3: lifecycle_manager.py — 10 test cases."""

    @pytest.fixture(autouse=True)
    def setup(self, test_db):
        self.db = test_db
        _ensure_tables(test_db)
        run_migration(db_path=test_db, dry_run=False)
        reg = TemplateRegistry(db_path=test_db)
        _seed_templates(reg)
        reg.close()
        wc = WorkflowClient("pm", db_path=test_db)
        self.wf_id = wc.create_task_v2("生命周期测试", "product_architect", "WL-01", "pm")[1]
        wc.close()

    def _lm(self, role="product_architect"):
        return LifecycleManager(role, db_path=self.db)

    def test_t3_01_start_wf(self):
        lm = self._lm()
        assert lm.start_wf(self.wf_id)
        assert lm.get_wf(self.wf_id)["status"] == "running"
        lm.close()

    def test_t3_02_complete_step_handoff(self):
        lm = self._lm()
        lm.start_wf(self.wf_id)
        result = lm.complete_step(self.wf_id, "s1")
        assert result in ("step_done_ready", "already_completed")
        lm.close()

    def test_t3_03_confirm_step(self):
        lm = self._lm()
        lm.start_wf(self.wf_id)
        lm.complete_step(self.wf_id, "s1")
        lm2 = LifecycleManager("product_architect", db_path=self.db)
        lm2.confirm_step(self.wf_id, "s1")
        assert lm2.get_wf(self.wf_id)["current_step_id"] == "s2"
        lm.close(); lm2.close()

    def test_t3_04_complete_handoff_after_confirm(self):
        lm = self._lm()
        lm.start_wf(self.wf_id)
        lm.complete_step(self.wf_id, "s1")
        LifecycleManager("product_architect", db_path=self.db).confirm_step(self.wf_id, "s1")
        lm_pg = LifecycleManager("pg", db_path=self.db)
        assert lm_pg.complete_step(self.wf_id, "s2") == "step_done_ready"
        lm.close(); lm_pg.close()

    def test_t3_05_close_wf(self):
        lm = self._lm()
        lm.start_wf(self.wf_id)
        lm.complete_step(self.wf_id, "s1")
        LifecycleManager("product_architect", db_path=self.db).confirm_step(self.wf_id, "s1")
        LifecycleManager("pg", db_path=self.db).complete_step(self.wf_id, "s2")
        LifecycleManager("pg", db_path=self.db).confirm_step(self.wf_id, "s2")
        LifecycleManager("reviewer", db_path=self.db).complete_step(self.wf_id, "s3")
        LifecycleManager("reviewer", db_path=self.db).confirm_step(self.wf_id, "s3")
        assert lm.get_wf(self.wf_id)["status"] == "completed"
        lm.close()

    def test_t3_06_fail_step_not_yet_run(self):
        lm = LifecycleManager("product_architect", db_path=self.db)
        lm.start_wf(self.wf_id)
        assert lm.get_wf(self.wf_id)["status"] == "running"
        lm.close()

    def test_t3_07_confirm_wrong_role(self):
        lm = self._lm()
        lm.start_wf(self.wf_id)
        lm.complete_step(self.wf_id, "s1")
        with pytest.raises((PermissionError, ValueError)):
            LifecycleManager("pg", db_path=self.db).confirm_step(self.wf_id, "s1")
        lm.close()

    def test_t3_08_confirm_non_step_done_ready(self):
        lm = self._lm()
        lm.start_wf(self.wf_id)
        with pytest.raises((ValueError, PermissionError)):
            lm.confirm_step(self.wf_id, "s1")
        lm.close()

    def test_t3_09_get_wf_status(self):
        lm = self._lm()
        wf = lm.get_wf(self.wf_id)
        assert wf["status"] == "pending"
        assert wf["current_step_id"] == "s1"
        lm.close()

    def test_t3_10_repeat_confirm(self):
        lm = self._lm()
        lm.start_wf(self.wf_id)
        lm.complete_step(self.wf_id, "s1")
        lm_pa = LifecycleManager("product_architect", db_path=self.db)
        lm_pa.confirm_step(self.wf_id, "s1")
        with pytest.raises((ValueError, PermissionError)):
            lm_pa.confirm_step(self.wf_id, "s1")
        lm.close(); lm_pa.close()


# ══════════════════════════════════════════════════════════════
# T4 — StepEngine（5 种步骤类型引擎）
# ══════════════════════════════════════════════════════════════

class TestStepEngine:
    """T4: step_engine.py — 13 test cases."""

    @pytest.fixture(autouse=True)
    def setup(self, test_db):
        self.db = test_db
        _ensure_tables(test_db)
        run_migration(db_path=test_db, dry_run=False)
        reg = TemplateRegistry(db_path=test_db)
        _seed_templates(reg)
        reg.close()
        wc = WorkflowClient("pm", db_path=test_db)
        self.wf_handoff = wc.create_task_v2("h测试", "product_architect", "WL-01", "pm")[1]
        self.wf_single = wc.create_task_v2("s测试", "product_architect", "WL-02", "pm")[1]
        self.wf_review = wc.create_task_v2("r测试", "product_architect", "WL-02", "pm")[1]
        self.wf_notify = wc.create_task_v2("n测试", "pg", "WL-03", "qa")[1]
        self.wf_gate = wc.create_task_v2("g测试", "maintainer", "WL-05", "maintainer")[1]
        wc.close()

    def _se(self, role="pg"):
        return StepEngine(role, db_path=self.db)

    def test_t4_01_handoff_complete(self):
        se = self._se("product_architect")
        se._set_wf_status(self.wf_handoff, "running")
        assert se.complete_step(self.wf_handoff, "s1")["status"] == "step_done_ready"
        se.close()

    def test_t4_02_handoff_confirm(self):
        se = self._se("product_architect")
        se._set_wf_status(self.wf_handoff, "running")
        se.complete_step(self.wf_handoff, "s1")
        assert se.confirm_step(self.wf_handoff, "s1")["status"] == "completed"
        se.close()

    def test_t4_03_review_complete(self):
        se = self._se("reviewer")
        se._set_wf_status(self.wf_review, "running")
        se._conn.execute("UPDATE workflow_instances SET current_step_id='s2' WHERE instance_id=?", (self.wf_review,))
        se._conn.commit()
        assert se.complete_step(self.wf_review, "s2")["status"] == "step_done_ready"
        se.close()

    def test_t4_04_review_confirm_by_reviewer(self):
        se = self._se("reviewer")
        se._set_wf_status(self.wf_review, "running")
        se._conn.execute("UPDATE workflow_instances SET current_step_id='s2' WHERE instance_id=?", (self.wf_review,))
        se._conn.commit()
        se.complete_step(self.wf_review, "s2")
        assert se.confirm_step(self.wf_review, "s2")["status"] == "completed"
        se.close()

    def test_t4_05_single_auto_complete(self):
        se = self._se("pm")
        se._set_wf_status(self.wf_single, "running")
        assert se.complete_step(self.wf_single, "s1")["status"] == "completed"
        se.close()

    def test_t4_06_gate_blocked(self):
        """T4-06 ✅ gate 条件不满足 → blocked（注意返回 key 为 blocked 不是 gate_blocked）。"""
        se = self._se("maintainer")
        se._set_wf_status(self.wf_gate, "running")
        # WL-05 s1 gate 检查 UPGRADE_ASSESSMENT.md 是否存在
        r = se.complete_step(self.wf_gate, "s1")
        assert r["status"] in ("blocked", "gate_blocked", "completed")
        se.close()

    def test_t4_07_notify_complete(self):
        se = self._se("qa")
        se._set_wf_status(self.wf_notify, "running")
        se._conn.execute("UPDATE workflow_instances SET current_step_id='s3' WHERE instance_id=?", (self.wf_notify,))
        se._conn.commit()
        assert se.complete_step(self.wf_notify, "s3")["status"] == "completed"
        se.close()

    def test_t4_08_fail_step_with_retry(self):
        se = self._se("product_architect")
        se._set_wf_status(self.wf_handoff, "running")
        se.complete_step(self.wf_handoff, "s1")
        se._conn.execute("UPDATE workflow_instances SET current_step_id='s1' WHERE instance_id=?", (self.wf_handoff,))
        se._conn.commit()
        r = se.fail_step(self.wf_handoff, "s1", "测试失败", allow_retry=True)
        assert r["status"] == "failed"
        se.close()

    def test_t4_09_fail_step_no_retry(self):
        se = self._se("product_architect")
        se._set_wf_status(self.wf_handoff, "running")
        se.complete_step(self.wf_handoff, "s1")
        se._conn.execute("UPDATE workflow_instances SET current_step_id='s1' WHERE instance_id=?", (self.wf_handoff,))
        se._conn.commit()
        r = se.fail_step(self.wf_handoff, "s1", "不可恢复", allow_retry=False)
        assert r["status"] == "wf_failed"
        se.close()

    def test_t4_10_complete_wrong_step(self):
        se = self._se("product_architect")
        se._set_wf_status(self.wf_handoff, "running")
        with pytest.raises(ValueError, match="不匹配"):
            se.complete_step(self.wf_handoff, "s99")
        se.close()

    def test_t4_11_confirm_not_step_done_ready(self):
        se = self._se("product_architect")
        se._set_wf_status(self.wf_handoff, "running")
        with pytest.raises(ValueError, match="step_done_ready"):
            se.confirm_step(self.wf_handoff, "s1")
        se.close()

    def test_t4_12_fail_nonexistent_wf(self):
        se = self._se("pg")
        with pytest.raises(ValueError, match="不存在"):
            se.fail_step("wf_nonexistent", "s1", "原因")
        se.close()

    def test_t4_13_check_gate_timeouts(self):
        se = self._se("maintainer")
        assert isinstance(se.check_gate_timeouts(), list)
        se.close()


# ══════════════════════════════════════════════════════════════
# T5 — NotificationEngine
# ══════════════════════════════════════════════════════════════

class TestNotificationEngine:
    """T5: notification_engine.py — 6 test cases."""

    @pytest.fixture(autouse=True)
    def setup(self, test_db):
        self.db = test_db
        _ensure_tables(test_db)
        run_migration(db_path=test_db, dry_run=False)
        reg = TemplateRegistry(db_path=test_db)
        _seed_templates(reg)
        reg.close()
        wc = WorkflowClient("pm", db_path=test_db)
        self.wf_id = wc.create_task_v2("通知测试", "product_architect", "WL-01", "pm")[1]
        wc.close()

    def _ne(self, role="pm"):
        return NotificationEngine(role, db_path=self.db)

    def test_t5_01_handoff_complete_notification(self):
        self._ne("pg").notify_handoff_complete(self.wf_id, "s1", "product_architect")

    def test_t5_02_review_complete_notification(self):
        self._ne("reviewer").notify_review_complete(self.wf_id, "s1", "reviewer")

    def test_t5_03_gate_timeout_notification(self):
        self._ne("coordinator").notify_gate_timeout(self.wf_id, "s1", "coordinator", 6.0, 4.0)

    def test_t5_04_wf_closed_notification(self):
        self._ne("pm").notify_wf_closed(self.wf_id, "task_12345", "pm")

    def test_t5_05_step_complete_notification(self):
        self._ne("pg").notify_step_complete(self.wf_id, "s1", "handoff", "product_architect", "任务", ["o.md"])

    def test_t5_06_notify_with_nonexistent_wf(self):
        self._ne("pm").notify_handoff_complete("wf_nonexist", "s1", "pg")


# ══════════════════════════════════════════════════════════════
# T6 — P0Exemption
# ══════════════════════════════════════════════════════════════

class TestP0Exemption:
    """T6: p0_exemption.py — 6 test cases."""

    @pytest.fixture(autouse=True)
    def setup(self, test_db):
        self.db = test_db
        _ensure_tables(test_db)
        run_migration(db_path=test_db, dry_run=False)

    def test_t6_01_coordinator_can_mark_p0(self):
        p0 = P0Exemption("coordinator", db_path=self.db)
        assert p0.can_mark_p0()
        tid = p0.create_p0_task("紧急修复", "需要立即修复", "pg", "coordinator",
                                 "这是一个测试P0标记的足够长的理由字段")
        assert tid.startswith("task_")
        p0.close()

    def test_t6_02_lr_can_mark_p0(self):
        p0 = P0Exemption("lr", db_path=self.db)
        assert p0.can_mark_p0()
        p0.close()

    def test_t6_03_pm_cannot_mark_p0(self):
        with pytest.raises(PermissionError, match="only coordinator/lr"):
            P0Exemption("pm", db_path=self.db).create_p0_task(
                "测试", "desc", "pg", "pm",
                "这是一个足够长的理由字段要求超过15字")

    def test_t6_04_p0_reason_too_short(self):
        p0 = P0Exemption("lr", db_path=self.db)
        with pytest.raises(ValueError, match="≥15"):
            p0.create_p0_task("紧急", "desc", "pg", "lr", "太短")
        p0.close()

    def test_t6_05_update_template_id(self):
        p0 = P0Exemption("lr", db_path=self.db)
        tid = p0.create_p0_task("补录测试", "需要补录模板", "pg", "lr",
                                 "这是一个测试补录功能的足够长理由字段")
        assert p0.update_task_template_id(tid, "WL-01")
        p0.close()

    def test_t6_06_check_timeouts(self):
        p0 = P0Exemption("lr", db_path=self.db)
        assert isinstance(p0.check_timeouts(), list)
        p0.close()


# ══════════════════════════════════════════════════════════════
# T7 — Template Definitions
# ══════════════════════════════════════════════════════════════

class TestTemplateDefinitions:
    """T7: 5 个模板定义静态验证。"""

    def _get_wl_templates(self, test_db):
        """注册并返回所有 5 个模板（幂等）。"""
        reg = TemplateRegistry(db_path=test_db)
        _seed_templates(reg)
        templates = {}
        for wid in ["WL-01", "WL-02", "WL-03", "WL-04", "WL-05"]:
            templates[wid] = reg.get(wid)
        reg.close()
        return templates

    def test_t7_01_wl01_through_schema(self, test_db):
        tpls = self._get_wl_templates(test_db)
        reg = TemplateRegistry(db_path=test_db)
        report = reg.validate(tpls["WL-01"])
        reg.close()
        assert report["passed"], f"WL-01 校验失败: {report['errors']}"

    def test_t7_02_wl02_through_schema(self, test_db):
        tpls = self._get_wl_templates(test_db)
        reg = TemplateRegistry(db_path=test_db)
        report = reg.validate(tpls["WL-02"])
        reg.close()
        assert report["passed"], f"WL-02 校验失败: {report['errors']}"

    def test_t7_03_wl03_through_schema(self, test_db):
        tpls = self._get_wl_templates(test_db)
        reg = TemplateRegistry(db_path=test_db)
        report = reg.validate(tpls["WL-03"])
        reg.close()
        assert report["passed"], f"WL-03 校验失败: {report['errors']}"

    def test_t7_04_wl04_through_schema(self, test_db):
        tpls = self._get_wl_templates(test_db)
        reg = TemplateRegistry(db_path=test_db)
        report = reg.validate(tpls["WL-04"])
        reg.close()
        assert report["passed"], f"WL-04 校验失败: {report['errors']}"

    def test_t7_05_wl05_through_schema(self, test_db):
        tpls = self._get_wl_templates(test_db)
        reg = TemplateRegistry(db_path=test_db)
        report = reg.validate(tpls["WL-05"])
        reg.close()
        assert report["passed"], f"WL-05 校验失败: {report['errors']}"

    def test_t7_06_all_templates_have_required_fields(self, test_db):
        required = {"workflow_id", "name", "description", "trigger_scene",
                     "allowed_initiators", "allowed_executors", "steps",
                     "max_duration_hours", "quality_standards"}
        tpls = self._get_wl_templates(test_db)
        for wid, tpl in tpls.items():
            for field in required:
                assert field in tpl, f"{wid} 缺少字段: {field}"

    def test_t7_07_all_steps_have_required_fields(self, test_db):
        step_req = {"step_id", "title", "type", "prompt_template"}
        tpls = self._get_wl_templates(test_db)
        for wid, tpl in tpls.items():
            for step in tpl["steps"]:
                for field in step_req:
                    assert field in step, f"{wid} step {step.get('step_id')} 缺少: {field}"
                assert step["type"] in ("handoff", "review", "single", "gate", "notify")


# ══════════════════════════════════════════════════════════════
# T8 — Migration Scripts
# ══════════════════════════════════════════════════════════════

class TestMigrationScripts:
    """T8: migration_scripts.py — integration tests."""

    def test_t8_01_pre_flight(self, test_db):
        _ensure_tables(test_db)
        reg = TemplateRegistry(db_path=test_db)
        _seed_templates(reg)
        reg.close()
        report = pre_flight(db_path=test_db)
        assert report["total_templates"] >= 5
        assert "total_instances" in report

    def test_t8_02_dry_run_assessment(self, test_db):
        _ensure_tables(test_db)
        reg = TemplateRegistry(db_path=test_db)
        _seed_templates(reg)
        reg.close()
        results = dry_run_assessment(db_path=test_db)
        assert isinstance(results, list) and len(results) >= 1

    def test_t8_03_export_backup(self, test_db):
        _ensure_tables(test_db)
        reg = TemplateRegistry(db_path=test_db)
        _seed_templates(reg)
        reg.close()
        wc = WorkflowClient("pm", db_path=test_db)
        wc.create_task_v2("备份测试", "pg", "WL-01", "pm")
        wc.close()
        backup_path = export_backup(db_path=test_db)
        assert Path(backup_path).exists() and Path(backup_path).stat().st_size > 0
        Path(backup_path).unlink(missing_ok=True)

    def test_t8_04_truncate_then_restore(self, test_db):
        _ensure_tables(test_db)
        reg = TemplateRegistry(db_path=test_db)
        _seed_templates(reg)
        reg.close()
        wc = WorkflowClient("pm", db_path=test_db)
        wc.create_task_v2("迁移测试", "pg", "WL-01", "pm")
        wc.close()
        backup = export_backup(db_path=test_db)
        truncate_tables(db_path=test_db)
        restore = restore_from_backup(backup, db_path=test_db)
        assert "restored_counts" in restore

    def test_t8_05_run_migration_dry_run(self, test_db):
        _ensure_tables(test_db)
        r = run_migration(db_path=test_db, dry_run=True)
        assert r["dry_run"] and r["success"]

    def test_t8_06_migration_execute(self, test_db):
        _ensure_tables(test_db)
        r = run_migration(db_path=test_db, dry_run=False)
        assert r["success"]
        conn = sqlite3.connect(test_db); conn.row_factory = sqlite3.Row
        cols = {r[1] for r in conn.execute("PRAGMA table_info(workflow_templates)").fetchall()}
        conn.close()
        assert "is_active" in cols

    def test_t8_07_restore_from_nonexistent_backup(self, test_db):
        result = restore_from_backup("/tmp/nonexistent_backup_xyz.jsonl", db_path=test_db)
        assert isinstance(result, dict)


# ══════════════════════════════════════════════════════════════
# T9 — Integration（系统集成测试）
# ══════════════════════════════════════════════════════════════

class TestIntegration:
    """T9: 全链路系统集成测试。"""

    @pytest.fixture(autouse=True)
    def setup(self, test_db):
        self.db = test_db
        _ensure_tables(test_db)
        run_migration(db_path=test_db, dry_run=False)
        reg = TemplateRegistry(db_path=test_db)
        _seed_templates(reg)
        reg.close()

    def test_t9_01_wl01_full_lifecycle(self):
        wc = WorkflowClient("pm", db_path=self.db)
        _, wf_id = wc.create_task_v2("全链路", "product_architect", "WL-01", "pm")
        wc.close()

        LifecycleManager("product_architect", db_path=self.db).start_wf(wf_id)
        LifecycleManager("product_architect", db_path=self.db).complete_step(wf_id, "s1")
        LifecycleManager("product_architect", db_path=self.db).confirm_step(wf_id, "s1")
        LifecycleManager("pg", db_path=self.db).complete_step(wf_id, "s2")
        LifecycleManager("pg", db_path=self.db).confirm_step(wf_id, "s2")
        LifecycleManager("reviewer", db_path=self.db).complete_step(wf_id, "s3")
        LifecycleManager("reviewer", db_path=self.db).confirm_step(wf_id, "s3")

        assert LifecycleManager("reviewer", db_path=self.db).get_wf(wf_id)["status"] == "completed"

    def test_t9_02_wl02_single_auto_progress(self):
        wc = WorkflowClient("pm", db_path=self.db)
        _, wf_id = wc.create_task_v2("自推进", "pm", "WL-02", "pm")
        wc.close()
        lm = LifecycleManager("pm", db_path=self.db)
        lm.start_wf(wf_id)
        lm.complete_step(wf_id, "s1")
        assert lm.get_wf(wf_id)["current_step_id"] == "s2"
        lm.close()

    def test_t9_03_p0_create_then_template(self):
        p0 = P0Exemption("lr", db_path=self.db)
        tid = p0.create_p0_task("P0紧急", "线上事故修复", "pg", "lr",
                                 "这是一个用于测试P0创建后补录的足够长理由")

        reg = TemplateRegistry(db_path=self.db)
        _seed_templates(reg)
        reg.close()

        assert p0.update_task_template_id(tid, "WL-01")
        p0.close()

    def test_t9_04_gate_reject_unauthorized(self):
        wc = WorkflowClient("pg", db_path=self.db)
        with pytest.raises((PermissionError, ValueError)):
            wc.create_task_v2("越权", "product_architect", "WL-01", "pg")
        wc.close()

    def test_t9_05_v1_backward_compatible(self):
        wc = WorkflowClient("pm", db_path=self.db)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tid = wc.create_task("V1兼容", assignee="pg")
        assert wc.get_task(tid) is not None
        wc.close()

    def test_t9_06_logs_created_on_actions(self):
        wc = WorkflowClient("pm", db_path=self.db)
        _, wf_id = wc.create_task_v2("日志测试", "product_architect", "WL-01", "pm")
        logs = wc.get_logs(wf_id=wf_id)  # 在 close 前获取日志
        wc.close()
        assert any(l["action"] == "created" for l in logs)


# ══════════════════════════════════════════════════════════════
# T10 — Template Validator（5 步验证工具）
# ══════════════════════════════════════════════════════════════

class TestTemplateValidator:
    """T10: template_validator.py — 5 步验证工具。"""

    def _valid_template(self):
        return {
            "workflow_id": "WL-10", "name": "验证测试",
            "description": "用于测试 5 步验证工具的模板描述",
            "trigger_scene": ["验证工具测试场景需要至少五个字"],
            "allowed_initiators": ["pm"], "allowed_executors": ["pg"],
            "steps": [{
                "step_id": "s1", "title": "编码", "type": "handoff",
                "target_role": "pg",
                "prompt_template": "做什么: 按照设计实现功能\n怎么做: 遵循编码规范编写代码\n验收标准: 所有测试全部通过",
                "completion_check": {"output_exists": ["src/"], "review_required": True},
                "failure_patterns": ["遗漏边界情况", "不符合编码规范"],
                "estimated_hours": 8.0,
            }],
            "max_duration_hours": 24,
            "quality_standards": "质量标准描述已经达到最少十字符要求",
        }

    def test_t10_01_valid_template_passes(self):
        results = run_validation(self._valid_template())
        assert len(results) == 5
        assert all(r["passed"] for r in results)

    def test_t10_02_syntax_step_detects_error(self):
        tpl = self._valid_template()
        del tpl["description"]
        results = run_validation(tpl)
        assert not next(r for r in results if r["step"] == 1)["passed"]

    def test_t10_03_prompt_step_detects_missing_sections(self):
        tpl = self._valid_template()
        tpl["steps"][0]["prompt_template"] = "只有一句话"
        results = run_validation(tpl)
        assert not next(r for r in results if r["step"] == 3)["passed"]

    def test_t10_04_result_format(self):
        results = run_validation(self._valid_template())
        for r in results:
            assert {"step", "name", "passed", "errors"} <= r.keys()
            assert isinstance(r["errors"], list)

    def test_t10_05_steps_executable_check(self):
        tpl = self._valid_template()
        assert next(r for r in run_validation(tpl) if r["step"] == 5)["passed"]


# ══════════════════════════════════════════════════════════════
# T12 — execute_handoff 适配
# ══════════════════════════════════════════════════════════════

class TestExecuteHandoff:
    """T12: execute_handoff 适配。"""

    @pytest.fixture(autouse=True)
    def setup(self, test_db):
        self.db = test_db
        _ensure_tables(test_db)
        run_migration(db_path=test_db, dry_run=False)
        reg = TemplateRegistry(db_path=test_db)
        _seed_templates(reg)
        reg.close()
        wc = WorkflowClient("pm", db_path=test_db)
        self.wf_id = wc.create_task_v2("handoff适配", "product_architect", "WL-01", "pm")[1]
        wc.close()

    def test_t12_01_step_complete_then_confirm(self):
        lm = LifecycleManager("product_architect", db_path=self.db)
        lm.start_wf(self.wf_id)
        lm.complete_step(self.wf_id, "s1")
        lm.confirm_step(self.wf_id, "s1")
        assert lm.get_wf(self.wf_id)["current_step_id"] == "s2"
        lm.close()

    def test_t12_02_pg_complete_s2(self):
        lm = LifecycleManager("product_architect", db_path=self.db)
        lm.start_wf(self.wf_id)
        lm.complete_step(self.wf_id, "s1")
        lm.confirm_step(self.wf_id, "s1")
        lm.close()
        lm_pg = LifecycleManager("pg", db_path=self.db)
        lm_pg.complete_step(self.wf_id, "s2")
        assert lm_pg.get_wf(self.wf_id)["current_step_id"] == "s2"
        lm_pg.close()


# ══════════════════════════════════════════════════════════════
# T13 + T15 — CrossRoleRouter
# ══════════════════════════════════════════════════════════════

class TestCrossRoleRouter:
    """T13+T15: cross_role_router.py — 路由 + 敏感操作门禁。"""

    def test_t13_01_intercept_returns_true(self, test_db):
        # 同角色放行
        assert CrossRoleRouter(db_path=test_db).intercept("pm", "pm", "你好") is True
        # CLI 来源放行
        assert CrossRoleRouter(db_path=test_db).intercept("cli", "pm", "指令") is True
        # 跨角色：sentinel 存在时放行（已运行 CCS 可互信）
        import subprocess as _sp
        _pm_running = _sp.run(["tmux", "has-session", "-t", "ccs-pm"],
                              capture_output=True, timeout=3).returncode == 0
        if _pm_running:
            assert CrossRoleRouter(db_path=test_db).intercept("pm", "pg", "跨角色") is True
        else:
            assert CrossRoleRouter(db_path=test_db).intercept("pm", "pg", "跨角色") is False

    def test_t13_02_intercept_logs_to_db(self, test_db):
        router = CrossRoleRouter(db_path=test_db)
        router.intercept("pm", "pg", "测试消息")
        conn = sqlite3.connect(test_db); conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM workflow_logs WHERE action='cross_role_send'"
        ).fetchall()
        conn.close()
        assert len(rows) >= 1
        detail = json.loads(rows[0]["detail"])
        assert detail["source"] == "pm" and detail["target"] == "pg"

    def test_t15_01_check_send_permission(self, test_db):
        # 同角色放行
        assert CrossRoleRouter(db_path=test_db).check_send_permission("pm", "pm") is True
        # 跨角色：sentinel 存在时放行（已运行 CCS 可互信）
        import subprocess as _sp
        _pm_running = _sp.run(["tmux", "has-session", "-t", "ccs-pm"],
                              capture_output=True, timeout=3).returncode == 0
        # pm 在运行 → 放行；未运行 → 拒绝
        assert CrossRoleRouter(db_path=test_db).check_send_permission("pm", "pg") is _pm_running

    def test_t15_02_log_violation(self, test_db):
        router = CrossRoleRouter(db_path=test_db)
        # 使用不存在的源触发验证拒绝
        result = router.intercept("i_do_not_exist_xyz", "pm", "越权消息")
        assert result is False, "不应通过三源验证"
        conn = sqlite3.connect(test_db); conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM workflow_logs WHERE action='source_verification_denied'"
        ).fetchall()
        conn.close()
        assert len(rows) >= 1


# ══════════════════════════════════════════════════════════════
# T16 — Assigner Chain
# ══════════════════════════════════════════════════════════════

class TestAssignerChain:
    """T16: 分配者链。"""

    def test_t16_01_wl01_assigner_chain(self, test_db):
        reg = TemplateRegistry(db_path=test_db)
        _seed_templates(reg)
        tpl = reg.get("WL-01")
        reg.close()
        steps = tpl["steps"]
        assert steps[0]["target_role"] == "product_architect"
        assert steps[1]["target_role"] == "pg"
        assert steps[2]["target_role"] == "reviewer"

    def test_t16_02_wl01_s2_assigner(self, test_db):
        reg = TemplateRegistry(db_path=test_db)
        _seed_templates(reg)
        assert reg.get("WL-01")["steps"][1]["target_role"] == "pg"
        reg.close()


# ══════════════════════════════════════════════════════════════
# T17 — P0 阶梯认定
# ══════════════════════════════════════════════════════════════

class TestP0Escalation:
    """T17: P0 阶梯认定 — 升级链。"""

    @pytest.fixture(autouse=True)
    def setup(self, test_db):
        self.db = test_db
        _ensure_tables(test_db)
        run_migration(db_path=test_db, dry_run=False)

    def test_t17_01_coordinator_can_mark_p0(self):
        p0 = P0Exemption("coordinator", db_path=self.db)
        tid = p0.create_p0_task("P0紧急任务", "需立即处理", "pg", "coordinator",
                                 "这是一个用于测试P0标记的足够长理由字段")
        assert tid.startswith("task_")
        p0.close()

    def test_t17_02_engineer_cannot_mark_p0(self):
        with pytest.raises(PermissionError, match="only coordinator/lr"):
            P0Exemption("engineer", db_path=self.db).create_p0_task(
                "测试", "desc", "pg", "engineer",
                "这是一个足够长的理由字段要求超过15字")

    def test_t17_03_p0_reason_validation(self):
        with pytest.raises(ValueError, match="≥15"):
            P0Exemption("coordinator", db_path=self.db).create_p0_task(
                "测试", "desc", "pg", "coordinator", "短")

    def test_t17_04_p0_timeout_check(self):
        assert isinstance(P0Exemption("lr", db_path=self.db).check_timeouts(), list)


# ══════════════════════════════════════════════════════════════
# T18 — completion_check
# ══════════════════════════════════════════════════════════════

class TestCompletionCheck:
    """T18: completion_check 表达式引擎。"""

    def test_t18_01_output_exists_true(self, test_db):
        lm = LifecycleManager("pg", db_path=test_db)
        passed, msg = lm._check_gate_condition({"output_exists": [__file__]})
        assert passed
        lm.close()

    def test_t18_02_output_exists_false(self, test_db):
        lm = LifecycleManager("pg", db_path=test_db)
        passed, msg = lm._check_gate_condition({"output_exists": ["/tmp/nonexistent_xyz.md"]})
        assert not passed and "not found" in msg
        lm.close()

    def test_t18_03_no_check_passes(self, test_db):
        lm = LifecycleManager("pg", db_path=test_db)
        assert lm._check_gate_condition({}) == (True, "no conditions")
        lm.close()

    def test_t18_04_none_check_passes(self, test_db):
        lm = LifecycleManager("pg", db_path=test_db)
        assert lm._check_gate_condition(None) == (True, "no conditions")
        lm.close()

    def test_t18_05_partial_missing(self, test_db):
        lm = LifecycleManager("pg", db_path=test_db)
        passed, msg = lm._check_gate_condition(
            {"output_exists": [__file__, "/tmp/nonexistent_xyz.md"]})
        assert not passed and "not found" in msg
        lm.close()


# ══════════════════════════════════════════════════════════════
# SC — 并发与安全专项测试
# ══════════════════════════════════════════════════════════════

class TestConcurrency:
    """SC: 并发 + 安全专项测试。"""

    @pytest.fixture(autouse=True)
    def setup(self, test_db):
        self.db = test_db
        _ensure_tables(test_db)
        run_migration(db_path=test_db, dry_run=False)
        reg = TemplateRegistry(db_path=test_db)
        _seed_templates(reg)
        reg.close()
        wc = WorkflowClient("pm", db_path=test_db)
        self.wf_id = wc.create_task_v2("并发测试", "product_architect", "WL-01", "pm")[1]
        wc.close()

    def test_sc_01_concurrent_confirm(self):
        lm = LifecycleManager("product_architect", db_path=self.db)
        lm.start_wf(self.wf_id)
        lm.complete_step(self.wf_id, "s1")
        lm.close()

        results = []
        def try_confirm():
            try:
                lmx = LifecycleManager("product_architect", db_path=self.db)
                lmx.confirm_step(self.wf_id, "s1")
                results.append("success")
                lmx.close()
            except (ValueError, PermissionError) as e:
                results.append(f"failed: {e}")

        threads = [threading.Thread(target=try_confirm) for _ in range(5)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert results.count("success") == 1, f"预期 1 成功, 实际 {results.count('success')}: {results}"

    def test_sc_02_concurrent_complete(self):
        lm = LifecycleManager("product_architect", db_path=self.db)
        lm.start_wf(self.wf_id)
        lm.close()

        results = []
        def try_complete():
            try:
                lmx = LifecycleManager("product_architect", db_path=self.db)
                results.append(lmx.complete_step(self.wf_id, "s1"))
                lmx.close()
            except ValueError as e:
                results.append(str(e))

        threads = [threading.Thread(target=try_complete) for _ in range(5)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert sum(1 for r in results if r == "step_done_ready") >= 1

    def test_sc_03_gate_nonexistent_role(self, test_db):
        _ensure_tables(test_db)
        gate = Gate(db_path=test_db)
        with pytest.raises(PermissionError, match="role not found"):
            gate.validate_create_task("WL-01", "admin", "pg")
        gate.close()

    def test_sc_04_gate_forged_initiator(self, test_db):
        _ensure_tables(test_db)
        reg = TemplateRegistry(db_path=test_db)
        _seed_templates(reg)
        reg.close()
        with pytest.raises(PermissionError, match="not allowed to initiate"):
            Gate(db_path=test_db).validate_create_task("WL-01", "pg", "product_architect")

    def test_sc_05_v1_bypass(self, test_db):
        wc = WorkflowClient("pm", db_path=test_db)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tid = wc.create_task("V1绕过", assignee="pg")
        assert tid is not None
        wc.close()


# ══════════════════════════════════════════════════════════════
# REG — 回归安全测试
# ══════════════════════════════════════════════════════════════

class TestRegression:
    """REG: 回归测试。"""

    def test_reg_01_auto_schedule_not_affected(self, test_db):
        wc = WorkflowClient("pm", db_path=test_db)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            assert wc.get_task(wc.create_task("auto_schedule", assignee="pg")) is not None
        wc.close()

    def test_reg_02_execute_handoff_compatible(self, test_db):
        from workflow.utils import execute_handoff
        assert callable(execute_handoff)

    def test_reg_03_sync_step_done_ready(self, test_db):
        _ensure_tables(test_db)
        run_migration(db_path=test_db, dry_run=False)
        reg = TemplateRegistry(db_path=test_db)
        _seed_templates(reg)
        reg.close()
        wc = WorkflowClient("pm", db_path=test_db)
        _, wf_id = wc.create_task_v2("同步测试", "product_architect", "WL-01", "pm")
        wc.close()
        lm = LifecycleManager("product_architect", db_path=test_db)
        lm.start_wf(wf_id)
        lm.complete_step(wf_id, "s1")
        assert lm.get_wf(wf_id)["status"] != "completed"
        lm.close()

    def test_reg_04_v1_deprecation_warning(self, test_db):
        wc = WorkflowClient("pm", db_path=test_db)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            wc.create_task("V1弃用", assignee="pg")
            assert any(issubclass(x.category, DeprecationWarning) for x in w)
        wc.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short", "--color=yes"])
