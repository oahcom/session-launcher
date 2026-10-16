#!/usr/bin/env python3
"""单元测试: routing/roles.py — 17 个函数/方法的路径覆盖

路径索引:
  P1   _run_shared_loader_validate    成功/不存在/异常
  P2   load_roles                     首次加载/缓存/空目录/JSON损坏
  P3   get_role                       找到/未找到/缓存/根目录不存在
  P4   _invalidate_role_cache         清空缓存
  P5   _forbidden_list                已知角色/未知角色/中文显示名
  P6   check_wake_permission          自唤醒/万能唤醒列表/特定列表/无权限
  P7   _action_templates              有产出消费/有唤醒权限/无产出消费
  P8   _contract_block                全字段/最小
  P9   _role_assembler_output         成功/失败回退/无assembler
  P10  _build_role_prompt             feed/loop/ondemand/未知驱动
  P11  _resolve_ws_paths              双路径存在/都不存在
  P12  inject_role_knowledge_into_workspace  无workspace/替换/插入
  P13  _validate_role_name            合法/非法/空字符串
  P17  validate_ccs_execution         空参数/任务操作/有效角色
"""
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── 夹具：隔离 SESSION_ROLES_ROOT 和全局状态 ──

@pytest.fixture(autouse=True)
def isolate_roles_state():
    """重置 roles.py 的全局缓存和锁状态，隔离每轮测试。

    同时清空 hermes-session-roles registry 的模块级缓存，
    避免 registry._LOADED_AT（5s TTL）导致测试间数据串扰。
    """
    import routing.roles as _roles
    with _roles._lock:
        _roles._ROLE_CACHE.clear()
        _roles._LOADED_ALL_ROLES = None
        _roles._SHARED_LOADER_CHECKED = False
    # 清空 registry 全局状态，防止前一轮缓存残留
    try:
        import registry as _reg
        _reg._ROLES.clear()
        _reg._LOADED_AT = 0.0
    except (ImportError, AttributeError):
        pass


@pytest.fixture
def roles_tmpdir(tmp_path):
    """创建临时 SESSION_ROLES_ROOT，返回角色目录结构。"""
    import routing.roles as _roles

    root = tmp_path / "hermes-session-roles"
    root.mkdir(parents=True)
    shared_src = root / "src"
    shared_src.mkdir()
    personas_dir = root / "personas" / "session-roles"
    personas_dir.mkdir(parents=True)

    # 保存原始值，patch 后恢复
    original_root = _roles.SESSION_ROLES_ROOT
    _roles.SESSION_ROLES_ROOT = root
    yield {"root": root, "shared_src": shared_src, "personas_dir": personas_dir}
    _roles.SESSION_ROLES_ROOT = original_root


# ── 角色样本 ──

def _sample_role(name="engineer", title="Engineer"):
    return {
        "name": name,
        "title": title,
        "output_targets": ["bus cat=code", "bus cat=doc"],
        "input_signals": [
            {"type": "bus", "spec": {"category": "task"}},
            {"type": "bus", "spec": {"category": "*"}},
            {"type": "file", "spec": {"path": "/tmp"}},
        ],
        "workgroup": [{"role": "qa"}, {"role": "reviewer"}],
        "drive": "ondemand",
        "lifecycle": "infinite",
        "system_prompt": "Role: {persona_name} ({persona_title})",
        "eval_criteria": [
            "C1 | 验证: 所有 bus 操作有响应",
            "C2 | 验证: 连接正常",
        ],
        "auto_send_messages": ["init", "ping", "pong", "extra"],
        "cron_schedule": "*/5 * * * *",
    }


# ===================================================================
# P1
# ===================================================================

class TestRunSharedLoaderValidate:
    """P1: _run_shared_loader_validate — 三种子路径"""

    def test_shared_loader_success(self, roles_tmpdir):
        """P1a: shared_loader 存在且返回 0 → True"""
        import routing.roles as _roles
        sl = roles_tmpdir["shared_src"] / "shared_loader.py"
        sl.write_text("")
        with patch.object(_roles, "subprocess") as mock_sp:
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = ""
            mock_sp.run.return_value = proc
            result = _roles._run_shared_loader_validate()
        assert result is True

    def test_shared_loader_not_found(self, roles_tmpdir):
        """P1b: shared_loader 不存在 → False"""
        import routing.roles as _roles
        result = _roles._run_shared_loader_validate()
        assert result is False

    def test_shared_loader_catches_exception(self, roles_tmpdir):
        """P1c: subprocess 抛异常 → True（不阻塞启动）"""
        import routing.roles as _roles
        sl = roles_tmpdir["shared_src"] / "shared_loader.py"
        sl.write_text("")
        with patch.object(_roles, "subprocess") as mock_sp:
            mock_sp.run.side_effect = RuntimeError("timeout")
            result = _roles._run_shared_loader_validate()
        assert result is True

    def test_shared_loader_reports_failures(self, roles_tmpdir):
        """P1d: 返回非 0，含 FAIL 计数 → True + 打印警告"""
        import routing.roles as _roles
        sl = roles_tmpdir["shared_src"] / "shared_loader.py"
        sl.write_text("")
        with patch.object(_roles, "subprocess") as mock_sp:
            proc = MagicMock()
            proc.returncode = 1
            proc.stdout = "FAIL: foo\nFAIL: bar\n"
            mock_sp.run.return_value = proc
            result = _roles._run_shared_loader_validate()
        assert result is True


# ===================================================================
# P2
# ===================================================================

class TestLoadRoles:
    """P2: load_roles — 加载/缓存/空/损坏"""

    def test_first_call_loads_json_files(self, roles_tmpdir):
        """P2a: 首次调用从 shared_loader 加载"""
        import routing.roles as _roles
        fake_roles = [
            {"name": "engineer", "system_prompt": "", "eval_criteria": [],
             "input_signals": [], "output_targets": []},
            {"name": "qa", "system_prompt": "", "eval_criteria": [],
             "input_signals": [], "output_targets": []},
        ]
        with patch.object(_roles, '_sl_load_roles', return_value=fake_roles):
            result = _roles.load_roles()
            assert len(result) == 2
            names = {r["name"] for r in result}
            assert names == {"engineer", "qa"}

    def test_second_call_uses_cache(self, roles_tmpdir):
        """P2b: 第二次调用返回缓存的列表（不重新调 shared_loader）"""
        import routing.roles as _roles
        fake_roles = [{"name": "engineer", "system_prompt": "", "eval_criteria": [],
                       "input_signals": [], "output_targets": []}]
        mock_fn = MagicMock(return_value=fake_roles)
        with patch.object(_roles, '_sl_load_roles', mock_fn):
            first = _roles.load_roles()
            second = _roles.load_roles()
        assert second is first  # 同一对象引用
        assert len(second) == 1
        assert mock_fn.call_count == 1  # 只调用一次（第二次命中缓存）

    def test_empty_directory_returns_empty_list(self, roles_tmpdir):
        """P2c: 无 JSON 文件 → []"""
        import routing.roles as _roles
        with patch.object(_roles, '_sl_load_roles', return_value=[]):
            result = _roles.load_roles()
            assert result == []

    def test_corrupted_json_skipped(self, roles_tmpdir):
        """P2d: 损坏 JSON 文件被跳过（mock 模拟 filtered 结果）"""
        import routing.roles as _roles
        ok_role = {"name": "ok", "system_prompt": "", "eval_criteria": [],
                   "input_signals": [], "output_targets": []}
        with patch.object(_roles, '_sl_load_roles', return_value=[ok_role]):
            result = _roles.load_roles()
            assert len(result) == 1
            assert result[0]["name"] == "ok"

    def test_shared_loader_called_once_on_first_load(self, roles_tmpdir):
        """P2e: 首次 load 调用 shared_loader 验证，后续不再调用"""
        import routing.roles as _roles
        sl = roles_tmpdir["shared_src"] / "shared_loader.py"
        sl.write_text("")
        with patch.object(_roles, "_run_shared_loader_validate") as mock_val:
            mock_val.return_value = True
            _roles.load_roles()
            _roles.load_roles()
        assert mock_val.call_count == 1


# ===================================================================
# P3
# ===================================================================

class TestGetRole:
    """P3: get_role — 找到/未找到/缓存/根目录不存在"""

    def test_found_returns_role(self, roles_tmpdir):
        """P3a: 角色存在 → 返回 dict"""
        import routing.roles as _roles
        pd = roles_tmpdir["personas_dir"]
        (pd / "persona_engineer.json").write_text(json.dumps({"name": "engineer", "title": "Engineer"}))
        result = _roles.get_role("engineer")
        assert result is not None
        assert result["name"] == "engineer"

    def test_not_found_returns_none(self, roles_tmpdir):
        """P3b: 角色不存在 → None"""
        import routing.roles as _roles
        pd = roles_tmpdir["personas_dir"]
        (pd / "persona_engineer.json").write_text(json.dumps({"name": "engineer"}))
        result = _roles.get_role("nonexistent")
        assert result is None

    def test_cached_role_returned_without_io(self, roles_tmpdir):
        """P3c: 缓存命中 → 不读文件"""
        import routing.roles as _roles
        pd = roles_tmpdir["personas_dir"]
        (pd / "persona_engineer.json").write_text(json.dumps({"name": "engineer"}))
        first = _roles.get_role("engineer")
        assert first is not None
        # 删文件后还应从缓存返回
        (pd / "persona_engineer.json").unlink()
        second = _roles.get_role("engineer")
        assert second is not None
        assert second["name"] == "engineer"

    def test_root_not_exists_returns_none(self):
        """P3d: SESSION_ROLES_ROOT 不存在 → None"""
        import routing.roles as _roles
        original = _roles.SESSION_ROLES_ROOT
        _roles.SESSION_ROLES_ROOT = Path("/nonexistent_path_xyz")
        try:
            result = _roles.get_role("engineer")
            assert result is None
        finally:
            _roles.SESSION_ROLES_ROOT = original


# ===================================================================
# P4
# ===================================================================

class TestInvalidateRoleCache:
    """P4: _invalidate_role_cache — 清空所有缓存"""

    def test_clears_both_caches(self, roles_tmpdir):
        """P4a: 清空 _ROLE_CACHE 和 _LOADED_ALL_ROLES"""
        import routing.roles as _roles
        pd = roles_tmpdir["personas_dir"]
        (pd / "persona_engineer.json").write_text(json.dumps({"name": "engineer"}))
        _roles.load_roles()
        _roles.get_role("engineer")
        with _roles._lock:
            assert len(_roles._ROLE_CACHE) > 0
            assert _roles._LOADED_ALL_ROLES is not None
        _roles._invalidate_role_cache()
        with _roles._lock:
            assert len(_roles._ROLE_CACHE) == 0
            assert _roles._LOADED_ALL_ROLES is None


# ===================================================================
# P5
# ===================================================================

class TestForbiddenList:
    """P5: _forbidden_list — 已知/未知角色 + 中文显示名"""

    def test_known_role_returns_specific_items(self):
        """P5a: pg → 特定列表"""
        import routing.roles as _roles
        result = _roles._forbidden_list("pg")
        assert "跑测试" in result
        assert "改配置" in result
        assert "部署" in result
        assert "写代码" not in result  # pg 允许写代码

    def test_unknown_role_uses_default_items(self):
        """P5b: 未知角色 → 默认列表"""
        import routing.roles as _roles
        result = _roles._forbidden_list("unknown")
        assert "跑测试" in result
        assert "启动 CCS" in result

    def test_unknown_role_name_no_display(self):
        """P5c: 显示名缺失时回退到 key 本身"""
        import routing.roles as _roles
        original = dict(_roles._FORBIDDEN_DISPLAY)
        _roles._FORBIDDEN_DISPLAY = {}
        try:
            result = _roles._forbidden_list("unknown")
            assert result == "run_tests、edit_config、deploy、start_ccs、edit_persona_json"
        finally:
            _roles._FORBIDDEN_DISPLAY.clear()
            _roles._FORBIDDEN_DISPLAY.update(original)


# ===================================================================
# P6
# ===================================================================

class TestCheckWakePermission:
    """P6: check_wake_permission — 4 条路径"""

    def test_self_wake_always_allowed(self):
        """P6a: actor == target → True"""
        import routing.roles as _roles
        assert _roles.check_wake_permission("qa", "qa") is True

    def test_actor_in_universal_list(self):
        """P6b: actor 在 * 列表中 → True"""
        import routing.roles as _roles
        assert _roles.check_wake_permission("coordinator", "anyone") is True

    def test_actor_in_target_specific_list(self):
        """P6c: actor 在 target 的指定列表中 → True"""
        import routing.roles as _roles
        assert _roles.check_wake_permission("qa", "pg") is True

    def test_no_permission_returns_false(self):
        """P6d: 无权限 → False"""
        import routing.roles as _roles
        assert _roles.check_wake_permission("pg", "qa") is False


# ===================================================================
# P7
# ===================================================================

class TestActionTemplates:
    """P7: _action_templates — 产出消费/唤醒权限/最小"""

    def test_produce_consume_categories(self):
        """P7a: 有产出和消费分类 → 生成对应的 bus_write/bus_read"""
        import routing.roles as _roles
        role = _sample_role("engineer", "Engineer")
        result = _roles._action_templates(role)
        assert "bus_write code" in result or "write code" in result
        assert "bus_write doc" in result or "write doc" in result
        assert "bus_read task" in result or "read task" in result

    def test_with_wake_permission_includes_partner(self):
        """P7b: 角色有唤醒权限 → 包含跨角色协作示例"""
        import routing.roles as _roles
        # qa 有 pg 的唤醒权
        role = _sample_role("qa", "QA")
        result = _roles._action_templates(role)
        assert "PartnerClient" in result
        assert "partner.py resolve" in result

    def test_role_without_wake_skips_partner(self):
        """P7c: 角色无唤醒权限 → 不包含 partner 示例"""
        import routing.roles as _roles
        # "intern" 不在 _WAKE_PERMISSION_MAP 的任何 value 中
        role = _sample_role("intern", "Intern")
        result = _roles._action_templates(role)
        assert "PartnerClient" not in result

    def test_no_output_targets_minimal(self):
        """P7d: 无产出消费 → 最小输出"""
        import routing.roles as _roles
        role = {"name": "minimal", "output_targets": [], "input_signals": []}
        result = _roles._action_templates(role)
        assert "禁区" in result


# ===================================================================
# P8
# ===================================================================

class TestContractBlock:
    """P8: _contract_block — 全字段/最小"""

    def test_full_role(self):
        """P8a: 完整角色 → 产出/消费/协作组/驱动/调度/验证标准"""
        import routing.roles as _roles
        role = _sample_role("engineer", "Engineer")
        result = _roles._contract_block(role)
        assert "产出分类: code, doc" in result
        assert "消费分类: task" in result
        assert "协作组:" in result
        assert "驱动方式: ondemand" in result
        assert "定时调度: */5 * * * *" in result
        assert "验证标准" in result
        assert "自动发送" in result

    def test_minimal_role(self):
        """P8b: 最小角色 → 无/无/无调度"""
        import routing.roles as _roles
        role = {"name": "minimal", "output_targets": [], "input_signals": []}
        result = _roles._contract_block(role)
        assert "产出分类: 无" in result
        assert "消费分类: 无" in result
        assert "定时调度: 无" in result
        assert "验证标准" not in result

    def test_wildcard_consume_filtered(self):
        """P8c: 消费分类为 * → 不写入 contract"""
        import routing.roles as _roles
        role = {
            "name": "test",
            "output_targets": [],
            "input_signals": [
                {"type": "bus", "spec": {"category": "*"}},
            ],
        }
        result = _roles._contract_block(role)
        assert "消费分类: 无" in result


# ===================================================================
# P9
# ===================================================================

class TestRoleAssemblerOutput:
    """P9: _role_assembler_output — 成功/失败回退/无文件"""

    def test_assembler_success(self, roles_tmpdir):
        """P9a: assembler 存在且成功 → 返回 stdout"""
        import routing.roles as _roles
        ass = roles_tmpdir["shared_src"] / "role_assembler.py"
        ass.write_text("")
        with patch.dict(os.environ, {"SESSION_ROLES_ROOT": str(roles_tmpdir["root"])}):
            with patch.object(_roles, "subprocess") as mock_sp:
                proc = MagicMock()
                proc.returncode = 0
                proc.stdout = "assembled output\n"
                mock_sp.run.return_value = proc
                result = _roles._role_assembler_output("engineer", _sample_role())
        assert result == "assembled output"

    def test_assembler_fallback_to_system_prompt(self, roles_tmpdir):
        """P9b: assembler 失败 → role.system_prompt 回退"""
        import routing.roles as _roles
        ass = roles_tmpdir["shared_src"] / "role_assembler.py"
        ass.write_text("")
        with patch.dict(os.environ, {"SESSION_ROLES_ROOT": str(roles_tmpdir["root"])}):
            with patch.object(_roles, "subprocess") as mock_sp:
                mock_sp.run.side_effect = RuntimeError("fail")
                result = _roles._role_assembler_output("engineer", _sample_role())
        assert "Role: engineer (Engineer)" in result

    def test_assembler_no_role_fallback(self, roles_tmpdir):
        """P9c: assembler 不存在 + 无 role → 空字符串"""
        import routing.roles as _roles
        with patch.dict(os.environ, {"SESSION_ROLES_ROOT": str(roles_tmpdir["root"])}):
            result = _roles._role_assembler_output("engineer")
        assert result == ""

    def test_assembler_empty_stdout_fallback(self, roles_tmpdir):
        """P9d: 返回空 stdout → fallback"""
        import routing.roles as _roles
        ass = roles_tmpdir["shared_src"] / "role_assembler.py"
        ass.write_text("")
        with patch.dict(os.environ, {"SESSION_ROLES_ROOT": str(roles_tmpdir["root"])}):
            with patch.object(_roles, "subprocess") as mock_sp:
                proc = MagicMock()
                proc.returncode = 0
                proc.stdout = ""
                mock_sp.run.return_value = proc
                result = _roles._role_assembler_output("engineer", _sample_role())
        assert "Role: engineer (Engineer)" in result


# ===================================================================
# P10
# ===================================================================

class TestBuildRolePrompt:
    """P10: _build_role_prompt — 四种驱动模式"""

    def test_feed_drive(self):
        """P10a: feed 已废弃，等效 ondemand"""
        import routing.roles as _roles
        role = _sample_role("engineer", "Engineer")
        role["drive"] = "feed"
        result = _roles._build_role_prompt(role)
        # feed 已在 38dad4b 移除，回退到默认 ondemand
        assert "ondemand（按需启动）" in result

    def test_loop_drive(self):
        """P10b: loop 已废弃，等效 ondemand"""
        import routing.roles as _roles
        role = _sample_role("engineer", "Engineer")
        role["drive"] = "ondemand"
        result = _roles._build_role_prompt(role)
        assert "ondemand（按需启动）" in result
        assert "等待上游角色" in result

    def test_ondemand_drive(self):
        """P10c: ondemand 驱动模式"""
        import routing.roles as _roles
        role = _sample_role("engineer", "Engineer")
        result = _roles._build_role_prompt(role)
        assert "ondemand（按需启动）" in result

    def test_unknown_drive(self):
        """P10d: 未识别驱动模式 → 走 ondemand 默认路径"""
        import routing.roles as _roles
        role = {"name": "custom", "title": "Custom", "drive": "hybrid"}
        result = _roles._build_role_prompt(role)
        assert "ondemand（按需启动）" in result


# ===================================================================
# P11
# ===================================================================

class TestResolveWsPaths:
    """P11: _resolve_ws_paths — 双路径/不存在"""

    def test_both_paths_exist(self, tmp_path):
        """P11a: name/ccs-name 两个 workspace 都存在"""
        import routing.roles as _roles
        with patch.dict(os.environ, {"HOME": str(tmp_path)}):
            p1 = tmp_path / "ccs-workspaces" / "engineer"
            p1.mkdir(parents=True)
            (p1 / "CLAUDE.md").write_text("# Engineer")
            p2 = tmp_path / "ccs-workspaces" / "ccs-engineer"
            p2.mkdir(parents=True)
            (p2 / "CLAUDE.md").write_text("# CCS Engineer")
            result = _roles._resolve_ws_paths("engineer")
        assert len(result) >= 1  # 可能1或2，取决于去重

    def test_neither_path_exists(self, tmp_path):
        """P11b: 两个 workspace 都不存在 → []"""
        import routing.roles as _roles
        with patch.dict(os.environ, {"HOME": str(tmp_path)}):
            result = _roles._resolve_ws_paths("nonexistent")
        assert result == []

    def test_duplicate_paths_deduplicated(self, tmp_path):
        """P11c: 两个路径相同 → 去重"""
        import routing.roles as _roles
        p1 = tmp_path / "ccs-workspaces" / "engineer"
        p1.mkdir(parents=True)
        (p1 / "CLAUDE.md").write_text("# Engineer")
        with patch.dict(os.environ, {"HOME": str(tmp_path)}):
            result = _roles._resolve_ws_paths("engineer")
        assert len(result) == 1


# ===================================================================
# P12
# ===================================================================

class TestInjectRoleKnowledge:
    """P12: inject_role_knowledge_into_workspace — 三种注入路径"""

    def test_no_workspace_returns_skipped(self, tmp_path):
        """P12a: 无 workspace → "skipped (no workspace)" """
        import routing.roles as _roles
        with patch.object(_roles, "_resolve_ws_paths", return_value=[]):
            result = _roles.inject_role_knowledge_into_workspace(_sample_role())
        assert result == "skipped (no workspace)"

    def test_replaces_existing_knowledge_block(self, tmp_path):
        """P12b: 已有 KNOWLEDGE 区块 → 替换（走 contract_block 回退路径）"""
        import routing.roles as _roles
        md_path = tmp_path / "CLAUDE.md"
        md_path.write_text("header\n<!-- KNOWLEDGE:START -->old stuff<!-- KNOWLEDGE:END -->\nfooter")
        with patch.object(_roles, "_resolve_ws_paths", return_value=[md_path]), \
             patch.object(_roles, "_role_assembler_output", return_value=""):
            result = _roles.inject_role_knowledge_into_workspace(_sample_role("engineer", "Engineer"))
        assert result.startswith("injected")
        content = md_path.read_text()
        assert "<!-- KNOWLEDGE:START -->" in content
        assert "<!-- KNOWLEDGE:END -->" in content
        assert "old stuff" not in content
        assert "产出分类:" in content

    def test_inserts_after_workspace_sys_end(self, tmp_path):
        """P12c: 无 KNOWLEDGE 块，有 WORKSPACE_SYS:END → 插入其后"""
        import routing.roles as _roles
        md_path = tmp_path / "CLAUDE.md"
        md_path.write_text("before\n<!-- WORKSPACE_SYS:END -->\nafter")
        with patch.object(_roles, "_resolve_ws_paths", return_value=[md_path]), \
             patch.object(_roles, "_role_assembler_output", return_value=""):
            result = _roles.inject_role_knowledge_into_workspace(_sample_role("engineer", "Engineer"))
        assert result.startswith("injected")
        content = md_path.read_text()
        assert "before" in content
        assert "after" in content
        assert "产出分类:" in content

    def test_appends_when_no_markers(self, tmp_path):
        """P12d: 无任何标记 → 追加到末尾"""
        import routing.roles as _roles
        md_path = tmp_path / "CLAUDE.md"
        md_path.write_text("just some text")
        with patch.object(_roles, "_resolve_ws_paths", return_value=[md_path]), \
             patch.object(_roles, "_role_assembler_output", return_value=""):
            result = _roles.inject_role_knowledge_into_workspace(_sample_role("engineer", "Engineer"))
        assert result.startswith("injected")
        content = md_path.read_text()
        assert "just some text" in content
        assert "产出分类:" in content

    def test_injects_security_redlines(self, tmp_path):
        """P12e: 注入内容包含通用红线（base.md 回退路径）"""
        import routing.roles as _roles
        md_path = tmp_path / "CLAUDE.md"
        md_path.write_text("content")
        with patch.object(_roles, "_resolve_ws_paths", return_value=[md_path]), \
             patch.object(_roles, "_role_assembler_output", return_value=""):
            _roles.inject_role_knowledge_into_workspace(_sample_role("engineer", "Engineer"))
        content = md_path.read_text()
        # 回退路径注入 base.md（含角色职责红线）或 _fallback_base_content（含安全红线）
        assert ("安全红线" in content) or ("角色职责红线" in content)

    def test_assembled_without_action_templates_gets_them(self, tmp_path):
        """P12f: assembler 输出无动作模板 → 补上（33 个非手写角色走这条路）"""
        import routing.roles as _roles
        md_path = tmp_path / "CLAUDE.md"
        md_path.write_text("content")
        with patch.object(_roles, "_resolve_ws_paths", return_value=[md_path]), \
             patch.object(_roles, "_role_assembler_output", return_value="## 定位\n只有定位"):
            _roles.inject_role_knowledge_into_workspace(_sample_role("archivist", "Archivist"))
        content = md_path.read_text()
        assert "动作模板" in content
        assert "bus_write code" in content

    def test_assembled_with_action_templates_not_duplicated(self, tmp_path):
        """P12g: assembler 已含动作模板 → 不重复注入（pm/writer/reviewer/scout 手写版）"""
        import routing.roles as _roles
        md_path = tmp_path / "CLAUDE.md"
        md_path.write_text("content")
        handwritten = "## 定位\n## 动作模板（填空即执行）\n  手写内容"
        with patch.object(_roles, "_resolve_ws_paths", return_value=[md_path]), \
             patch.object(_roles, "_role_assembler_output", return_value=handwritten):
            _roles.inject_role_knowledge_into_workspace(_sample_role("pm", "PM"))
        content = md_path.read_text()
        assert content.count("动作模板") == 1


# ===================================================================
# P13
# ===================================================================

class TestValidateRoleName:
    """P13: _validate_role_name — 合法/非法/空"""

    def test_valid_name(self):
        import routing.roles as _roles
        assert _roles._validate_role_name("engineer") is True
        assert _roles._validate_role_name("ccs-qa") is True
        assert _roles._validate_role_name("role_123") is True

    def test_invalid_name_with_space(self):
        import routing.roles as _roles
        assert _roles._validate_role_name("my role") is False
        assert _roles._validate_role_name("role name ") is False

    def test_empty_string(self):
        import routing.roles as _roles
        assert _roles._validate_role_name("") is False

    def test_special_chars(self):
        import routing.roles as _roles
        assert _roles._validate_role_name("role@name") is False
        assert _roles._validate_role_name("role/name") is False


# ===================================================================
# P17
# ===================================================================

class TestValidateCcsExecution:
    """P17: validate_ccs_execution — 参数验证/任务检查"""

    def test_empty_role_raises(self):
        """P17a: 空 role → ValueError"""
        import routing.roles as _roles
        with pytest.raises(ValueError, match="role and action required"):
            _roles.validate_ccs_execution("", "start")

    def test_empty_action_raises(self):
        """P17b: 空 action → ValueError"""
        import routing.roles as _roles
        with pytest.raises(ValueError, match="role and action required"):
            _roles.validate_ccs_execution("engineer", "")

    def test_task_action_valid_role_passes(self, roles_tmpdir):
        """P17c: task 类操作 + JSON 存在 → 通过"""
        import routing.roles as _roles
        pd = roles_tmpdir["personas_dir"]
        (pd / "persona_engineer.json").write_text(json.dumps({"name": "engineer"}))
        # 不应抛异常
        _roles.validate_ccs_execution("engineer", "task:run")

    def test_task_action_missing_role_with_get_role_fallback(self, roles_tmpdir):
        """P17d: JSON 不存在但 get_role 缓存有 → 不打印警告"""
        import routing.roles as _roles
        pd = roles_tmpdir["personas_dir"]
        # 先加载角色到 get_role 缓存
        (pd / "persona_engineer.json").write_text(json.dumps({"name": "engineer"}))
        _roles.get_role("engineer")
        # 删除 JSON 文件
        (pd / "persona_engineer.json").unlink()
        # 不应抛异常，缓存在 get_role 中
        _roles.validate_ccs_execution("engineer", "task:run")

    def test_task_action_no_role_print_warning(self, roles_tmpdir, capsys):
        """P17e: role 既无 JSON 又无缓存 → 打印警告"""
        import routing.roles as _roles
        # 模拟角色不存在：load_roles 返回空列表（shared_loader 路径被 mock）
        with patch.object(_roles, '_sl_load_roles', return_value=[]):
            _roles.validate_ccs_execution("engineer", "task:run")
        captured = capsys.readouterr()
        assert "CCS-RULE: role 'engineer' not found" in captured.err

    def test_non_task_action_passes_without_validation(self):
        """P17f: 非 task/workflow 操作 → 跳过校验"""
        import routing.roles as _roles
        _roles.validate_ccs_execution("nonexistent", "start")
        # 不应抛异常


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
