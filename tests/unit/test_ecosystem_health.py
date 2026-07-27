"""test_ecosystem_health.py — 健康检查模型 + 检查函数测试覆盖。"""
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ecosystem_health import (
    CheckItem, HealthReport, check_all,
    _check_dir_exists, _check_syntax, _check_hardcoded_paths,
)


class TestCheckItem:
    def test_defaults(self):
        item = CheckItem(name="test", passed=True)
        assert item.name == "test"
        assert item.passed is True
        assert item.detail == ""

    def test_to_dict(self):
        item = CheckItem(name="x", passed=False, detail="err", duration_ms=12.3)
        d = item.to_dict()
        assert d["name"] == "x"
        assert d["passed"] is False
        assert d["detail"] == "err"
        assert d["duration_ms"] == 12.3


class TestHealthReport:
    def test_empty_report(self):
        r = HealthReport(timestamp=100.0)
        assert r.passed_count == 0
        assert r.total_count == 0
        assert r.all_passed is True

    def test_add_items(self):
        r = HealthReport()
        r.add("a", True)
        r.add("b", False, "failed")
        assert r.passed_count == 1
        assert r.total_count == 2
        assert r.all_passed is False

    def test_summary_healthy(self):
        r = HealthReport()
        r.add("ok", True)
        s = r.summary()
        assert "健康" in s
        assert "1/1" in s

    def test_summary_unhealthy(self):
        r = HealthReport()
        r.add("ok", True)
        r.add("bad", False)
        s = r.summary()
        assert "异常" in s
        assert "1/2" in s

    def test_json(self):
        r = HealthReport(timestamp=1000.0)
        r.add("ok", True)
        j = json.loads(r.json())
        assert j["all_passed"] is True
        assert j["summary"] == "1/1"
        assert j["timestamp"] == 1000.0

    def test_text_report(self):
        r = HealthReport(timestamp=1000.0)
        r.add("健康检查", True)
        t = r.text_report()
        assert "健康检查" in t
        assert "健康" in t


class TestCheckDirExists:
    def test_dir_exists(self, tmp_path):
        item = _check_dir_exists(tmp_path, "test_dir")
        assert item.passed is True

    def test_dir_not_exists(self):
        item = _check_dir_exists(Path("/nonexistent_path_xyz_abc"), "missing")
        assert item.passed is False


class TestCheckSyntax:
    def test_valid_syntax(self, tmp_path):
        f = tmp_path / "good.py"
        f.write_text("x = 1\n")
        item = _check_syntax(f)
        assert item.passed is True

    def test_invalid_syntax(self, tmp_path):
        f = tmp_path / "bad.py"
        f.write_text("x = \n")
        item = _check_syntax(f)
        assert item.passed is False


class TestCheckHardcodedPaths:
    def test_no_hardcoded(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "clean.py").write_text("x = Path.home() / 'ok'\n")
        item = _check_hardcoded_paths()
        # Uses actual project paths — just verify it doesn't crash and returns CheckItem
        assert isinstance(item, CheckItem)
        assert item.name == "paths:no_hardcode"

    def test_hardcoded_detected(self, tmp_path):
        with patch("ecosystem_health.ALL_PROJECTS", {
            "test": tmp_path
        }):
            src = tmp_path / "src"
            src.mkdir()
            (src / "hard.py").write_text("/home/administrator/secret\n")
            item = _check_hardcoded_paths()
            assert item.passed is False
            assert "hard.py" in item.detail


class TestCheckAll:
    @patch("ecosystem_health.subprocess.run")
    def test_check_all_returns_report(self, mock_run):
        # Mock all subprocess calls to succeed
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "4 roles\n"
        mock_run.return_value.stderr = ""

        with patch("ecosystem_health._check_sentinels") as mock_sentinels:
            mock_sentinels.return_value = CheckItem("sentinels", True, "2 sessions", 1.0)
            with patch("ecosystem_health._check_workspaces") as mock_ws:
                mock_ws.return_value = CheckItem("workspaces", True, "5 dirs", 1.0)
                report = check_all()
                assert isinstance(report, HealthReport)
                assert report.total_count > 0


if __name__ == "__main__":
    import pytest as _p
    import sys as _s
    _s.exit(_p.main([__file__, "-v"]))
