"""test_partner_exception_logging.py — 验证 partner.py 异常日志覆盖。"""
import logging
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))
sys.path.insert(1, str(Path(__file__).resolve().parent.parent.parent / "src" / "ops"))

import pytest
from routing.partner import PartnerClient


class TestPartnerExceptionLogging:
    """验证 _get_task 和 _check_bus_notification 的 except 不再静默。"""

    def test_get_task_sqlite_failure_logs_warning(self, caplog):
        pc = PartnerClient("test_role")
        caplog.set_level(logging.WARNING)
        with patch("sqlite3.connect", side_effect=RuntimeError("db gone")):
            result = pc._get_task("no-such-task")
        assert result is None
        assert "db gone" in caplog.text
        assert "_get_task(no-such-task)" in caplog.text

    def test_get_task_empty_task_id_returns_none(self):
        pc = PartnerClient("test_role")
        result = pc._get_task("")
        assert result is None

    def test_check_bus_notification_subprocess_failure_logs_warning(self, caplog):
        pc = PartnerClient("test_role")
        caplog.set_level(logging.WARNING)
        with patch("subprocess.run", side_effect=RuntimeError("bus timeout")):
            result = pc._check_bus_notification("target_role", "task-001")
        assert result is False
        assert "bus timeout" in caplog.text
        assert "_check_bus_notification" in caplog.text
