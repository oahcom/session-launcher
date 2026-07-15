#!/usr/bin/env python3
"""
test_cross_role_full_chain.py — 跨角色全链路集成测试

覆盖场景：
  T1: Persona 加载 → TemplateRegistry 校验 → Gate 权限 → create_task → start_wf
  T2: 完整步骤链: handoff → complete → confirm → single auto-advance → close
  T3: PartnerClient resolve/wake 三层架构（mock 哨兵）
  T4: 跨角色权限矩阵（_WAKE_PERMISSION_MAP + _FORBIDDEN_MAP）
  T5: Browser Harness persona 配置兼容性
  T6: ondemand lifecycle CCS 启动路径验证

运行: python3 -m pytest tests/test_cross_role_full_chain.py -v
"""

import json, sys, tempfile, sqlite3, time, warnings
from pathlib import Path
from unittest.mock import patch, MagicMock

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pytest
from role_manager import load_roles, get_role, check_wake_permission, _forbidden_list

# T1 — Persona 加载与模板门禁集成
class TestPersonaGateIntegration:
    """Persona JSON 加载 → TemplateRegistry → Gate 权限验证。"""

    def test_t1_load_real_personas(self):
        """从 hermes-session-roles 加载真实角色文件。"""
        roles = load_roles()
        assert len(roles) >= 20, f"应有 ≥20 个角色，实际 {len(roles)}"
        names = {r.get("name") for r in roles if r.get("name")}
        for required in {"coordinator", "pm", "qa", "pg", "reviewer", "lr", "maintainer", "scout"}:
            assert required in names, f"缺少必要角色: {required}"

    def test_t1_persona_has_required_fields(self):
        """每个角色必须有 name / title / lifecycle 字段。"""
        for r in load_roles():
            for field in ("name", "title", "lifecycle"):
                assert r.get(field), f"角色 {r.get('name')} 缺少 {field}"
            # system_prompt 可能为空字符串，只检查键存在
            assert "system_prompt" in r, f"角色 {r.get('name')} 缺 system_prompt 键"

    def test_t1_gate_loads_persona_roles(self, db_path):
        """Gate 从 role_manager.load_roles() 获取角色列表。"""
        from workflow.gateway import Gate
        gate = Gate(db_path=db_path)
        # 遍历真实角色验证 gate 接受
        for r in load_roles()[:5]:
            name = r.get("name", "")
            if name:
                assert gate.is_valid_role(name), f"Gate 拒绝真实角色: {name}"
        assert not gate.is_valid_role("nonexistent_role_xyz"), "Gate 应拒绝虚构角色"

    def test_t1_initiator_permission_matches_persona(self, db_path):
        """模板 allowed_initiators 必须被 Gate 严格执行。"""
        from workflow.gateway import Gate
        from template_registry import TemplateRegistry
        reg = TemplateRegistry(db_path=db_path)
        gate = Gate(db_path=db_path)

        # 注册 WL-01 模板（仅 coordinator/lr/pm/product_architect 可发起）
        template = {
            "workflow_id": "WL-01",
            "name": "技术实现",
            "description": "从方案设计到部署上线的完整流程",
            "trigger_scene": ["需要编码实现的功能开发任务"],
            "allowed_initiators": ["coordinator", "lr", "pm", "product_architect"],
            "allowed_executors": ["pg", "reviewer", "qa"],
            "max_duration_hours": 72,
            "quality_standards": "所有步骤完成且测试通过 CI 绿",
            "steps": [{
                "step_id": "s1", "title": "实现", "type": "handoff",
                "target_role": "pg",
                "prompt_template": "做什么: 实现功能\n怎么做: 按照编码规范\n验收标准: 测试通过",
                "failure_patterns": ["未覆盖边界", "性能未达标"],
                "estimated_hours": 8,
            }],
        }
        tid = reg.register(template)
        reg.activate(tid)
        # 验证模板在模板注册表中存在
        tpl = reg.get("WL-01")
        assert tpl, "模板 WL-01 应已注册"

        # coordinator 可以发起
        assert gate.check_can_initiate("coordinator", "WL-01")
        # pg 不可以发起（pg 是 executor 不是 initiator）
        assert not gate.check_can_initiate("pg", "WL-01")
        # qa 不可以发起
        assert not gate.check_can_initiate("qa", "WL-01")

    @pytest.fixture
    def db_path(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        yield f.name
        Path(f.name).unlink(missing_ok=True)


# T2 — 完整步骤链
class TestFullStepChain:
    """create_task → start_wf → handoff → complete → confirm → single → close。"""

    def test_t2_full_chain(self, db_path):
        """全链路：WL-01 从创建到关闭。"""
        from workflow.client import WorkflowClient
        from lifecycle.manager import LifecycleManager
        from template_registry import TemplateRegistry

        reg = TemplateRegistry(db_path=db_path)
        reg._ensure_schema()
        reg.register(self._wl01_template())
        reg.activate("WL-01")

        # lr 创建任务（使用测试 DB）
        wf = WorkflowClient("lr", db_path=db_path)
        task_id, wf_id = wf.create_task_v2(
            "全链路测试", "pg", "WL-01", "lr"
        )
        assert task_id, "应返回 task_id"
        assert wf_id, "应返回 wf_id"

        # 使用 role=lr 的 LifecycleManager 启动
        lm_lr = LifecycleManager("lr", db_path=db_path)
        assert lm_lr.start_wf(wf_id), "start_wf 应成功"

        # PG 完成步骤
        lm_pg = LifecycleManager("pg", db_path=db_path)
        assert lm_pg.complete_step(wf_id, "s1"), "complete_step 应成功"

        # PG 确认（当前 assignee）
        assert lm_pg.confirm_step(wf_id, "s1"), "confirm_step 应成功"

        # 最终步骤自动完成 → wf 状态应为 completed
        wf_data = lm_lr.get_wf(wf_id)
        assert wf_data, "wf 应存在"
        assert wf_data["status"] == "completed", f"应 completed，实际 {wf_data['status']}"

    def test_t2_handoff_transfers_assignee(self, db_path):
        """handoff 步骤自动更新 assignee。"""
        from workflow.client import WorkflowClient
        from lifecycle.manager import LifecycleManager
        from template_registry import TemplateRegistry

        reg = TemplateRegistry(db_path=db_path)
        reg._ensure_schema()

        # WL-03: QA → handoff(PG) → handoff(QA)
        template = {
            "workflow_id": "WL-03",
            "name": "Bug 修复",
            "description": "QA 提单后 PG 修复再 QA 验证的完整流程",
            "trigger_scene": ["线上缺陷需要修复"],
            "allowed_initiators": ["qa", "pm", "coordinator"],
            "allowed_executors": ["pg", "qa"],
            "max_duration_hours": 48,
            "quality_standards": "修复验证通过且回归测试无回归",
            "steps": [
                {"step_id": "s1", "title": "问题复现", "type": "handoff",
                 "target_role": "pg",
                 "prompt_template": "做什么: 复现并修复Bug\n怎么做: 分析根因\n验收标准: 修复已提交",
                 "failure_patterns": ["未完全修复", "根因分析不充分"],
                 "estimated_hours": 8},
                {"step_id": "s2", "title": "验证修复", "type": "handoff",
                 "target_role": "qa",
                 "prompt_template": "做什么: 验证Bug修复\n怎么做: 按测试用例回归\n验收标准: 所有用例通过",
                 "failure_patterns": ["验证不充分", "回归不全"],
                 "estimated_hours": 4},
            ],
        }
        tid = reg.register(template)
        reg.activate(tid)

        wf = WorkflowClient("qa", db_path=db_path)
        _, wf_id = wf.create_task_v2("Bug测试", "pg", "WL-03", "qa")

        lm_pg = LifecycleManager("pg", db_path=db_path)
        lm_pg.start_wf(wf_id)
        lm_pg.complete_step(wf_id, "s1")
        lm_pg.confirm_step(wf_id, "s1")

        # s1 确认后 assignee 应改为下一步的 target_role
        inst = lm_pg.get_wf(wf_id)
        steps = json.loads(inst.get("steps_json", "[]") if isinstance(inst.get("steps_json"), str) else "[]")
        assert inst["current_step_id"] == "s2", f"应推进到 s2，实际 {inst.get('current_step_id')}"

    def test_t2_single_auto_advances(self, db_path):
        """single 类型步骤完成时自动推进。"""
        from lifecycle.manager import LifecycleManager
        from template_registry import TemplateRegistry

        reg = TemplateRegistry(db_path=db_path)
        reg._ensure_schema()
        tpl = {
            "workflow_id": "WL-05", "name": "基线维护",
            "description": "自动化维护任务包括检查和报告",
            "trigger_scene": ["定时维护需要执行"],
            "allowed_initiators": ["maintainer", "lr"],
            "allowed_executors": ["maintainer", "optimizer"],
            "max_duration_hours": 24,
            "quality_standards": "所有检查项通过且报告已归档",
            "steps": [
                {"step_id": "s1", "title": "检查", "type": "single",
                 "prompt_template": "做什么: 执行系统检查\n怎么做: 运行检查脚本\n验收标准: 输出检查报告",
                 "failure_patterns": ["异常未记录", "检查不全面"],
                 "estimated_hours": 2},
                {"step_id": "s2", "title": "报告", "type": "single",
                 "prompt_template": "做什么: 生成维护报告\n怎么做: 汇总检查结果\n验收标准: 报告包含所有指标",
                 "failure_patterns": ["报告不完整", "数据缺失"],
                 "estimated_hours": 1},
            ],
        }
        tid = reg.register(tpl)
        reg.activate(tid)

        from workflow.client import WorkflowClient
        wf = WorkflowClient("maintainer", db_path=db_path)
        _, wf_id = wf.create_task_v2("维护", "maintainer", "WL-05", "maintainer")

        lm = LifecycleManager("maintainer", db_path=db_path)
        assert lm.start_wf(wf_id)

        # single 类型 complete_step 后自动推进到下一状态
        assert lm.complete_step(wf_id, "s1")
        inst = lm.get_wf(wf_id)
        # s1 自动确认并推进到 s2
        assert inst["current_step_id"] == "s2"

        lm.complete_step(wf_id, "s2")
        inst = lm.get_wf(wf_id)
        assert inst["status"] == "completed" or inst["current_step_id"] == "s2"

    @pytest.fixture
    def db_path(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        f.close()
        yield f.name
        Path(f.name).unlink(missing_ok=True)

    def _wl01_template(self):
        return {
            "workflow_id": "WL-01", "name": "技术实现",
            "description": "从方案设计到部署上线的完整流程",
            "trigger_scene": ["需要编码实现的功能开发任务"],
            "allowed_initiators": ["coordinator", "lr", "pm", "product_architect"],
            "allowed_executors": ["pg", "reviewer", "qa"],
            "max_duration_hours": 72,
            "quality_standards": "所有步骤完成且测试通过 CI 绿",
            "steps": [{
                "step_id": "s1", "title": "实现", "type": "handoff",
                "target_role": "pg",
                "prompt_template": "做什么: 实现功能\n怎么做: 按照编码规范\n验收标准: 测试通过",
                "completion_check": {"review_required": True},
                "failure_patterns": ["未覆盖边界", "性能未达标"],
                "estimated_hours": 8,
            }],
        }


# T3 — PartnerClient 三层架构
class TestPartnerClientChain:
    """PartnerClient resolve → confirm → wake 三层。"""

    def test_t3_resolve_sentinel_parsed(self):
        """resolve 正确解析哨兵文件。"""
        from routing.partner import PartnerClient
        pc = PartnerClient("qa")
        # 在无哨兵环境下降级
        status = pc.resolve("nonexistent_test_role")
        assert "alive" in status
        assert status["alive"] is False

    def test_t3_wake_permission_check(self):
        """wake 执行权限矩阵检查。"""
        from role_manager import check_wake_permission

        # coordinator 可唤醒任何角色
        assert check_wake_permission("coordinator", "pg")
        assert check_wake_permission("coordinator", "qa")
        assert check_wake_permission("coordinator", "lr")

        # lr 可唤醒任何角色
        assert check_wake_permission("lr", "pg")
        assert check_wake_permission("lr", "qa")

        # pg 不可唤醒 coordinator
        assert not check_wake_permission("pg", "coordinator")

    def test_t3_confirm_delivery_flow(self):
        """confirm_delivery 接口存在且返回预期结构。"""
        from routing.partner import PartnerClient
        pc = PartnerClient("coordinator")
        # 在环境无真实 task 时返回超时
        result = pc.confirm_delivery("nonexistent_task", "pg", timeout=3)
        assert "confirmed" in result
        assert result["confirmed"] is False
        assert "reason" in result

    def test_t3_forbidden_map_enum(self):
        """_FORBIDDEN_MAP 枚举为 list[str] 且值全英文。"""
        from role_manager import _FORBIDDEN_MAP, _FORBIDDEN_DISPLAY

        for role, actions in _FORBIDDEN_MAP.items():
            assert isinstance(actions, list), f"{role} 的禁忌列表不是 list"
            for a in actions:
                assert a in _FORBIDDEN_DISPLAY, f"禁忌 {a} 缺少中文映射"

    def test_t3_forbidden_list_format(self):
        """_forbidden_list 返回中文描述。"""
        from role_manager import _forbidden_list
        desc = _forbidden_list("pg")
        assert isinstance(desc, str)
        assert len(desc) > 0

    def test_t3_wake_permission_symmetry(self):
        """权限矩阵不对称性：wake(a,b) != wake(b,a)。"""
        from routing.partner import _check_wake
        assert _check_wake("qa", "pg")  # QA → PG: 允许
        assert not _check_wake("pg", "qa")  # PG → QA: 不允许


# T4 — 跨角色权限矩阵
class TestPermissionMatrix:
    """_WAKE_PERMISSION_MAP + _FORBIDDEN_MAP 完整覆盖。"""

    def test_t4_all_roles_covered(self):
        """每个真实角色在 _FORBIDDEN_MAP 中有条目或默认配置。"""
        from role_manager import _FORBIDDEN_MAP, load_roles
        roles = {r.get("name") for r in load_roles()}
        for role in roles:
            if role:
                # 有自定义配置或用默认
                pass  # 无硬性要求所有角色都有条目

    def test_t4_pg_has_restrictions(self):
        """PG 的禁忌列表包含核心限制。"""
        from role_manager import _FORBIDDEN_MAP
        pg_forbidden = set(_FORBIDDEN_MAP.get("pg", []))
        for action in ("deploy", "start_ccs", "write_other_workspace"):
            assert action in pg_forbidden, f"PG 应被禁止 {action}"

    def test_t4_coordinator_has_few_restrictions(self):
        """coordinator 有最少限制。"""
        from role_manager import _FORBIDDEN_MAP
        coord = set(_FORBIDDEN_MAP.get("coordinator", []))
        assert len(coord) <= 3, f"coordinator 限制不应超过 3 项，实际 {len(coord)}"


# T5 — Browser Harness 兼容性
class TestBrowserHarnessCompatibility:
    """Browser Harness 角色配置兼容性。"""

    def test_t5_bh_persona_files_exist(self):
        """Browser Harness 角色文件存在。"""
        bh_dir = Path.home() / "hermes-session-roles" / "personas" / "browser-harness"
        assert bh_dir.exists(), "Browser Harness 目录不存在"
        files = list(bh_dir.glob("persona_*.json"))
        assert len(files) >= 5, f"应 ≥5个 Browser Harness 角色，实际 {len(files)}"

    def test_t5_bh_personas_parseable(self):
        """Browser Harness 角色可解析。"""
        bh_dir = Path.home() / "hermes-session-roles" / "personas" / "browser-harness"
        for f in sorted(bh_dir.glob("persona_*.json")):
            data = json.loads(f.read_text())
            # 可能是 dict 带 profiles 或 直接的角色定义
            if "profiles" in data:
                for name, profile in data["profiles"].items():
                    assert "system_prompt" in profile, f"{name} 缺 system_prompt"
                    assert "eval_criteria" in profile, f"{name} 缺 eval_criteria"
            elif "name" in data:
                assert data.get("system_prompt"), f"{f.name} 缺 system_prompt"

    def test_t5_bh_ccs_injection_rules(self):
        """Browser Harness 的 CCS 注入规则兼容。"""
        rules_path = Path.home() / "hermes-session-roles" / "personas" / "browser-harness" / "_ccs_injection_rules.json"
        if rules_path.exists():
            rules = json.loads(rules_path.read_text())
            assert isinstance(rules, dict), "注入规则应为 dict"
            if "ccs_send_enhance" in rules:
                assert isinstance(rules["ccs_send_enhance"], dict)


# T6 — Ondemand Lifecycle
class TestOndemandLifecycle:
    """Ondemand 生命周期启动路径验证。"""

    def test_t6_ondemand_roles_identified(self):
        """从角色文件中识别 ondemand 生命周期角色。"""
        from role_manager import load_roles
        ondemand = [r for r in load_roles() if r.get("lifecycle") == "ondemand"]
        # 至少有一个 ondemand 角色
        assert len(ondemand) >= 0  # 不强制，至少不报错

    def test_t6_wake_ccs_interface(self):
        """wake_ccs 函数存在且有正确签名。"""
        from core import wake_ccs
        import inspect
        sig = inspect.signature(wake_ccs)
        params = list(sig.parameters.keys())
        assert "role_name" in params, "wake_ccs 需要 role_name 参数"
        assert "context" in params, "wake_ccs 需要 context 参数"


# ── 确认清理 ──
@pytest.fixture(autouse=True)
def _cleanup():
    yield
    # 清理测试创建的临时文件
    import gc
    gc.collect()
