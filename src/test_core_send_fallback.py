"""WL-P2-03: 通知降级 — ccs send 失败→写 bus [ccs_send_fallback] 标记测试。"""

import os, sys, json, time, subprocess, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from paths import BUS_CLIENT


class TestSendFallback(unittest.TestCase):

    def test_write_bus_notice_exists(self):
        """_write_bus_notice 是核心降级函数，确认签名正确。"""
        from core import _write_bus_notice
        self.assertTrue(callable(_write_bus_notice))

    def test_send_returns_error_on_invalid_role(self):
        """非法角色名 → send 返回错误 + _write_bus_notice 被调用。"""
        from core import send
        r = send("__nonexistent__", "test", source="cli")
        self.assertFalse(r.get("success"), f"expected failure, got {r}")
        # _validate_role_name 对非法角色名返回 False, send 报"未运行"
        self.assertIn("未运行", r.get("error", ""))

    def test_send_returns_error_on_ccs_not_running(self):
        """CCS 未运行 → send 返回错误 + 写 bus。"""
        from core import send
        r = send("scout", "test message", source="coordinator")
        self.assertFalse(r.get("success"), f"expected failure, got {r}")
        # 确认返回信息
        self.assertIn("未运行", r.get("error", ""))

    def test_bus_client_write_usable(self):
        """bus_client write 命令可用（走降级路径的工具）。"""
        r = subprocess.run(
            ["python3", str(BUS_CLIENT), "write", "architecture",
             "[ccs_send_fallback] test fallback message",
             "--evidence", "reason=test",
             "--src", "core.send"],
            capture_output=True, timeout=10
        )
        self.assertEqual(r.returncode, 0,
                         msg=f"bus_client failed: {r.stderr.decode()}")


if __name__ == "__main__":
    unittest.main()
