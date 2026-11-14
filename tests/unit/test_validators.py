"""test_validators.py — validate_role 格式校验测试（不记名放行语义）。

行为变更记录（2026-11-14，按用户「不记名角色…不给予其他限制」需求）：
persona 注册表白名单拒绝已移除（validate_role 只留格式安全线），因此——
- 原 test_unknown_role_rejected（钉"未注册名必须拒绝"）改写为
  test_unknown_role_allowed_as_anonymous（钉"未注册名=不记名角色，放行"）；
- 原 test_known_roles_refresh_after_persona_appears（钉 _KNOWN_ROLES 快照重扫）
  随 _KNOWN_ROLES 机制一并删除——该快照只服务白名单校验，全仓无其它消费方
  （ccs roles 走 routing.roles.load_roles，不读 _KNOWN_ROLES）。
格式安全线（空名/路径遍历/shell 注入）断言原样保留。
"""
from ops.validators import validate_role

@validate_role
def _dummy(role, *args, **kwargs):
    return {"success": True, "role": role}

class TestValidateRole:
    """有效角色名应通过校验。"""

    def test_known_role_pm(self):
        assert _dummy("pm")["success"] is True

    def test_known_role_engineer(self):
        assert _dummy("engineer")["success"] is True

    def test_known_role_qa(self):
        assert _dummy("qa")["success"] is True

    def test_known_role_devops(self):
        assert _dummy("devops")["success"] is True

    def test_known_role_security_auditor(self):
        assert _dummy("security_auditor")["success"] is True

    def test_known_role_knowledge_curator(self):
        assert _dummy("knowledge_curator")["success"] is True

    """未注册名 = 不记名角色，放行（白名单拒绝已按需求移除）。"""

    def test_unknown_role_allowed_as_anonymous(self):
        result = _dummy("nonexistent_role_xyz")
        assert result["success"] is True

    """格式安全线仍然拒绝：空名 / 路径遍历 / shell 注入。"""

    def test_empty_role_rejected(self):
        result = _dummy("")
        assert result["success"] is False

    def test_path_traversal_rejected(self):
        result = _dummy("../etc/passwd")
        assert result["success"] is False

    def test_special_chars_rejected(self):
        result = _dummy("role; rm -rf /")
        assert result["success"] is False

if __name__ == "__main__":
    import pytest, sys
    sys.exit(pytest.main([__file__, "-v"]))
