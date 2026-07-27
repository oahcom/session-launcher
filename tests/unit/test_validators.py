"""test_validators.py — validate_role 注册表一致性检查测试。"""
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

    """无效角色名应拒绝。"""

    def test_unknown_role_rejected(self):
        result = _dummy("nonexistent_role_xyz")
        assert result["success"] is False
        assert "注册表" in result.get("error", "")

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
