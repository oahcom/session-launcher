#!/usr/bin/env python3
"""Test signals.py — all checker functions with mocked external dependencies.

Run: cd ~/session-launcher && python3 -m pytest tests/test_signals.py -v -x
"""

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Module loading: set up `events` package, parser, then signals ──

_launcher_src = Path(__file__).resolve().parent.parent / "src"
if str(_launcher_src) not in sys.path:
    sys.path.insert(0, str(_launcher_src))

# Ensure `events` package exists in sys.modules for dotted imports
if "events" not in sys.modules:
    _events_pkg = types.ModuleType("events")
    _events_pkg.__path__ = [str(_launcher_src / "events")]
    _events_pkg.__file__ = str(_launcher_src / "events" / "__init__.py")
    sys.modules["events"] = _events_pkg

# Load events.parser (needed for check_signal's deferred import)
if "events.parser" not in sys.modules:
    _parser_spec = importlib.util.spec_from_file_location(
        "events.parser",
        _launcher_src / "events" / "parser.py",
    )
    _parser_mod = importlib.util.module_from_spec(_parser_spec)
    sys.modules["events.parser"] = _parser_mod
    _parser_spec.loader.exec_module(_parser_mod)

# Load events.signals
_spec = importlib.util.spec_from_file_location(
    "events.signals",
    _launcher_src / "events" / "signals.py",
)
_signals = importlib.util.module_from_spec(_spec)
sys.modules["events.signals"] = _signals
_spec.loader.exec_module(_signals)

# ── shorthand references ──

check_bus_unread = _signals.check_bus_unread
check_systemctl_active = _signals.check_systemctl_active
check_http_health = _signals.check_http_health
check_journalctl_errors = _signals.check_journalctl_errors
check_git_staged = _signals.check_git_staged
check_session_size = _signals.check_session_size
check_running_sessions = _signals.check_running_sessions
check_mem_disk = _signals.check_mem_disk
check_signal = _signals.check_signal
check_signal_by_name = _signals.check_signal_by_name
SIGNAL_CHECKERS = _signals.SIGNAL_CHECKERS


# ═══════════════════════════════════════════════════════════════
# SIGNAL_CHECKERS dict
# ═══════════════════════════════════════════════════════════════

class TestSignalCheckersDict:
    def test_contains_all_expected_keys(self):
        assert set(SIGNAL_CHECKERS.keys()) == {
            "bus_unread", "systemctl_active", "http_health",
            "journalctl_errors", "git_staged", "session_size",
            "running_sessions", "mem_disk",
        }

    def test_each_value_is_callable(self):
        for name, fn in SIGNAL_CHECKERS.items():
            assert callable(fn), f"{name} is not callable"


# ═══════════════════════════════════════════════════════════════
# check_bus_unread
# ═══════════════════════════════════════════════════════════════

class TestCheckBusUnread:
    @patch.object(_signals.subprocess, "run")
    def test_no_unread(self, mock_run):
        mock_run.return_value = MagicMock(stdout="0 unread", returncode=0)
        assert not check_bus_unread()

    @patch.object(_signals.subprocess, "run")
    def test_has_unread(self, mock_run):
        mock_run.return_value = MagicMock(stdout="3 unread", returncode=0)
        assert check_bus_unread()

    @patch.object(_signals.subprocess, "run")
    def test_filter_matches(self, mock_run):
        mock_run.return_value = MagicMock(stdout="3 unread in security", returncode=0)
        assert check_bus_unread("security")

    @patch.object(_signals.subprocess, "run")
    def test_filter_does_not_match(self, mock_run):
        mock_run.return_value = MagicMock(stdout="3 unread in security", returncode=0)
        assert not check_bus_unread("performance")

    @patch.object(_signals.subprocess, "run")
    def test_exception_returns_false(self, mock_run):
        mock_run.side_effect = Exception("boom")
        assert not check_bus_unread()


# ═══════════════════════════════════════════════════════════════
# check_systemctl_active
# ═══════════════════════════════════════════════════════════════

class TestCheckSystemctlActive:
    @patch.object(_signals.subprocess, "run")
    def test_all_active_no_journal_errors(self, mock_run):
        """全部服务 active + journal 无错误 → False（无问题）"""
        mock_run.side_effect = [
            MagicMock(stdout="active\nactive\nactive", returncode=0),   # systemctl
            MagicMock(stdout="", returncode=0),                         # journalctl
        ]
        assert not check_systemctl_active()

    @patch.object(_signals.subprocess, "run")
    def test_inactive_service_triggers(self, mock_run):
        """任一服务 inactive → True"""
        mock_run.return_value = MagicMock(stdout="inactive\nactive\nactive", returncode=0)
        assert check_systemctl_active()

    @patch.object(_signals.subprocess, "run")
    def test_journal_error_triggers(self, mock_run):
        """journalctl 含 ERROR → True"""
        mock_run.side_effect = [
            MagicMock(stdout="active\nactive\nactive", returncode=0),
            MagicMock(stdout="ERROR: something broke", returncode=0),
        ]
        assert check_systemctl_active()

    @patch.object(_signals.subprocess, "run")
    def test_journal_error_with_result_is_ignored(self, mock_run):
        """"failed with result" 行不触发报警"""
        mock_run.side_effect = [
            MagicMock(stdout="active\nactive\nactive", returncode=0),
            MagicMock(stdout="unit failed with result exit-code\n", returncode=0),
        ]
        assert not check_systemctl_active()

    @patch.object(_signals.subprocess, "run")
    def test_journal_traceback_triggers(self, mock_run):
        """journalctl 含 Traceback → True"""
        mock_run.side_effect = [
            MagicMock(stdout="active\nactive\nactive", returncode=0),
            MagicMock(stdout="Traceback (most recent call last):\n  File X", returncode=0),
        ]
        assert check_systemctl_active()

    @patch.object(_signals.subprocess, "run")
    def test_exception_returns_false(self, mock_run):
        mock_run.side_effect = Exception("boom")
        assert not check_systemctl_active()


# ═══════════════════════════════════════════════════════════════
# check_http_health
# ═══════════════════════════════════════════════════════════════

class TestCheckHttpHealth:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        import paths as _paths_mod
        # raising=False: the loaded paths module may be from session-pipeline (no HEALTH_CHECK_ENDPOINTS),
        # or from session-launcher (has it). Either way, set the test value.
        # Then check_http_health's inner `from paths import HEALTH_CHECK_ENDPOINTS`
        # will get sys.modules['paths'] which now has the attribute.
        monkeypatch.setattr(
            _paths_mod, "HEALTH_CHECK_ENDPOINTS",
            ["http://test.local"], raising=False,
        )

    @patch.object(_signals.subprocess, "run")
    def test_all_200(self, mock_run):
        mock_run.return_value = MagicMock(stdout="200", returncode=0)
        assert not check_http_health()

    @patch.object(_signals.subprocess, "run")
    def test_non_200_triggers(self, mock_run):
        mock_run.return_value = MagicMock(stdout="503", returncode=0)
        assert check_http_health()

    @patch.object(_signals.subprocess, "run")
    def test_exception_triggers(self, mock_run):
        mock_run.side_effect = Exception("timeout")
        assert check_http_health()


# ═══════════════════════════════════════════════════════════════
# check_journalctl_errors
# ═══════════════════════════════════════════════════════════════

class TestCheckJournalctlErrors:
    @patch.object(_signals.subprocess, "run")
    def test_filter_matches(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ERROR: disk full", returncode=0)
        assert check_journalctl_errors("disk")

    @patch.object(_signals.subprocess, "run")
    def test_filter_does_not_match(self, mock_run):
        mock_run.return_value = MagicMock(stdout="everything fine", returncode=0)
        assert not check_journalctl_errors("ERROR")

    @patch.object(_signals.subprocess, "run")
    def test_multiple_filters_or_match(self, mock_run):
        """多个 filter 用 | 分隔，任一匹配即 True"""
        mock_run.return_value = MagicMock(stdout="OOM killer triggered", returncode=0)
        assert check_journalctl_errors("ERROR|OOM|CRITICAL")

    @patch.object(_signals.subprocess, "run")
    def test_multiple_filters_no_match(self, mock_run):
        mock_run.return_value = MagicMock(stdout="all good", returncode=0)
        assert not check_journalctl_errors("ERROR|OOM")

    @patch.object(_signals.subprocess, "run")
    def test_exception_returns_false(self, mock_run):
        mock_run.side_effect = Exception("boom")
        assert not check_journalctl_errors()

    @patch.object(_signals.subprocess, "run")
    def test_empty_filter_with_output(self, mock_run):
        """空 filter + 非空 stdout → True（空字符串 in 任何字符串 = True）"""
        mock_run.return_value = MagicMock(stdout="some output", returncode=0)
        assert check_journalctl_errors("") is True


# ═══════════════════════════════════════════════════════════════
# check_git_staged
# ═══════════════════════════════════════════════════════════════

class TestCheckGitStaged:
    @patch.object(_signals.subprocess, "run")
    def test_no_staged_changes(self, mock_run):
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        assert not check_git_staged()

    @patch.object(_signals.subprocess, "run")
    def test_has_staged_changes(self, mock_run):
        mock_run.return_value = MagicMock(stdout="modified.py\n", returncode=0)
        assert check_git_staged()

    @patch.object(_signals.subprocess, "run")
    def test_exception_logs_warning(self, mock_run):
        mock_run.side_effect = Exception("git not found")
        with patch.object(_signals.LOG, "warning") as mock_warn:
            assert not check_git_staged()
            # 3 repos → 3 warnings
            assert mock_warn.call_count == 3


# ═══════════════════════════════════════════════════════════════
# check_session_size
# ═══════════════════════════════════════════════════════════════

class TestCheckSessionSize:
    @patch.object(Path, "glob")
    def test_no_large_files(self, mock_glob):
        mock_glob.return_value = []
        assert not check_session_size()

    @patch.object(Path, "glob")
    def test_large_file_triggers(self, mock_glob):
        fake_file = MagicMock(spec=Path)
        fake_file.is_file.return_value = True
        fake_file.stat.return_value.st_size = 100 * 1024 * 1024  # 100MB
        mock_glob.return_value = [fake_file]
        assert check_session_size()

    @patch.object(Path, "glob")
    def test_small_file_no_trigger(self, mock_glob):
        fake_file = MagicMock(spec=Path)
        fake_file.is_file.return_value = True
        fake_file.stat.return_value.st_size = 1024  # 1KB
        mock_glob.return_value = [fake_file]
        assert not check_session_size()

    @patch.object(Path, "glob")
    def test_exception_returns_false(self, mock_glob):
        mock_glob.side_effect = Exception("permission denied")
        assert not check_session_size()


# ═══════════════════════════════════════════════════════════════
# check_running_sessions
# ═══════════════════════════════════════════════════════════════

class TestCheckRunningSessions:
    @patch.object(_signals.subprocess, "run")
    def test_has_claude_processes(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="user 12345 0.0 0.1 1234 567 ? S 10:00 0:00 python3 claude\n",
            returncode=0,
        )
        assert check_running_sessions()

    @patch.object(_signals.subprocess, "run")
    def test_no_claude_processes(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="user 12345 0.0 0.1 1234 567 ? S 10:00 0:00 python3 main.py\n",
            returncode=0,
        )
        assert not check_running_sessions()

    @patch.object(_signals.subprocess, "run")
    def test_exception_returns_false(self, mock_run):
        mock_run.side_effect = Exception("boom")
        assert not check_running_sessions()


# ═══════════════════════════════════════════════════════════════
# check_mem_disk
# ═══════════════════════════════════════════════════════════════

class TestCheckMemDisk:
    @patch.object(_signals.subprocess, "run")
    @patch.object(Path, "read_text")
    def test_normal_ok(self, mock_read, mock_run):
        """内存充足 + 磁盘不足 90% → False"""
        mock_read.return_value = "MemAvailable: 2000000 kB\n"
        mock_run.return_value = MagicMock(stdout="Use%\n 50%\n", returncode=0)
        assert not check_mem_disk()

    @patch.object(_signals.subprocess, "run")
    @patch.object(Path, "read_text")
    def test_low_memory_triggers(self, mock_read, mock_run):
        """内存 < 500MB → True"""
        mock_read.return_value = "MemAvailable: 200000 kB\n"
        mock_run.return_value = MagicMock(stdout="Use%\n 50%\n", returncode=0)
        assert check_mem_disk()

    @patch.object(_signals.subprocess, "run")
    @patch.object(Path, "read_text")
    def test_high_disk_triggers(self, mock_read, mock_run):
        """磁盘 > 90% → True"""
        mock_read.return_value = "MemAvailable: 2000000 kB\n"
        mock_run.return_value = MagicMock(stdout="Use%\n 95%\n", returncode=0)
        assert check_mem_disk()

    @patch.object(_signals.subprocess, "run")
    @patch.object(Path, "read_text")
    def test_meminfo_exception_falls_through_to_disk(self, mock_read, mock_run):
        """/proc/meminfo 读取失败 → 降级检查磁盘"""
        mock_read.side_effect = Exception("permission denied")
        mock_run.return_value = MagicMock(stdout="Use%\n 50%\n", returncode=0)
        assert not check_mem_disk()

    @patch.object(_signals.subprocess, "run")
    @patch.object(Path, "read_text")
    def test_both_exception_returns_false(self, mock_read, mock_run):
        """两个检查都异常 → False"""
        mock_read.side_effect = Exception("meminfo error")
        mock_run.side_effect = Exception("df error")
        with patch.object(_signals.LOG, "warning"):
            assert not check_mem_disk()


# ═══════════════════════════════════════════════════════════════
# check_signal — 统一入口测试
# ═══════════════════════════════════════════════════════════════

class TestCheckSignal:
    def test_custom_type_returns_true(self):
        """custom 类型始终返回 True"""
        assert check_signal({"type": "custom", "spec": {}})

    def test_unknown_type_returns_false(self):
        """未知 type 返回 False"""
        assert not check_signal({"type": "nonexistent", "spec": {}})


# ═══════════════════════════════════════════════════════════════
# check_signal_by_name — 旧接口兼容
# ═══════════════════════════════════════════════════════════════

class TestCheckSignalByName:
    def test_invalid_name_returns_false(self):
        assert not check_signal_by_name("does_not_exist")

    def test_unknown_name_returns_false(self):
        assert not check_signal_by_name("")

    @patch.object(_signals.subprocess, "run")
    def test_valid_name_delegates(self, mock_run):
        """有效的 check 名称应委托给对应 checker"""
        mock_run.return_value = MagicMock(stdout="3 unread", returncode=0)
        assert check_signal_by_name("bus_unread")


# ═══════════════════════════════════════════════════════════════
# 自检（脚本模式）
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # 列出并运行测试
    tests = [n for n in dir() if n.startswith("test_")]
    passed, failed = 0, 0
    for name in sorted(tests):
        try:
            globals()[name]()
            print(f"  PASS {name}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {name}: {e}")
            failed += 1
    # 也运行类内的测试
    for cls_name in sorted(globals()):
        cls = globals()[cls_name]
        if isinstance(cls, type) and cls_name.startswith("Test"):
            for name in sorted(dir(cls)):
                if name.startswith("test_"):
                    try:
                        getattr(cls(), name)()
                        print(f"  PASS {cls_name}.{name}")
                        passed += 1
                    except Exception as e:
                        print(f"  FAIL {cls_name}.{name}: {e}")
                        failed += 1
    print(f"\n{passed}/{passed + failed} passed")
    raise SystemExit(0 if failed == 0 else 1)
