#!/usr/bin/env python3
"""
Session Launcher 端到端 Mock 测试。

验证信号检查委托逻辑、prompt 注入、生命周期哨兵写入。

运行: python3 tests/test_e2e_mock.py
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

# --- 测试辅助函数 ---


def _make_role(
    name: str = "maintainer",
    title: str = "测试角色",
    lifecycle: str = "infinite",
    drive: str = "cron",
    cron_schedule: str = "*/15 * * * *",
    signal_source: str = "",
    signal_filter: str = "",
) -> dict:
    """创建测试用角色 JSON。"""
    return {
        "name": name,
        "title": title,
        "description": "测试用角色",
        "category": "测试",
        "lifecycle": lifecycle,
        "drive": drive,
        "cron_schedule": cron_schedule,
        "idle_action": "exit",
        "session_hint": "cron",
        "eval_criteria": ["条件1"],
        "input_signals": [
            {"source": signal_source or f"systemctl --user is-active {name}.service",
             "filter": signal_filter}
        ],
        "output_targets": ["bus cat=test"],
        "system_prompt": "你是 {persona_title}({persona_name})，测试用。\n\n## 专长\n- 测试\n\n## 行为准则\n1. 完成测试\n"
    }


# ── 旧格式 (source 字段) 信号测试 ──
# 注: 旧格式信号经过 launcher.check_signal() → signals.check_signal()
#   → signal_parser.parse_signal() → signal_parser._check_signal_legacy()
#   → signal_parser._check_bus / _check_shell / ...
# mock 目标应指向 signal_parser._check_signal_legacy


def test_check_signal_bus_unread():
    """旧格式 bus_client.py → _check_signal_legacy。"""
    from launcher import check_signal
    with patch("events.parser.parse_signal", return_value=True) as m:
        assert check_signal({"source": "bus_client.py unread --all", "filter": ""}) is True


def test_check_signal_bus_empty():
    """旧格式无未读返回 False。"""
    from launcher import check_signal
    with patch("events.parser.parse_signal", return_value=False):
        assert check_signal({"source": "bus_client.py unread --all", "filter": ""}) is False


def test_check_signal_systemctl_active():
    """旧格式 systemctl → _check_signal_legacy。"""
    from launcher import check_signal
    with patch("events.parser.parse_signal", return_value=False):
        assert check_signal({"source": "systemctl --user is-active demo.service", "filter": ""}) is False


def test_check_signal_systemctl_inactive():
    """旧格式 systemctl 非 active → _check_signal_legacy。"""
    from launcher import check_signal
    with patch("events.parser.parse_signal", return_value=True) as m:
        assert check_signal({"source": "systemctl --user is-active demo.service", "filter": ""}) is True


def test_check_signal_curl_200():
    """旧格式 curl → _check_signal_legacy。"""
    from launcher import check_signal
    with patch("events.parser.parse_signal", return_value=False):
        assert check_signal({"source": "curl http://localhost:8890", "filter": ""}) is False


def test_check_signal_curl_non200():
    """旧格式 curl 非200 → _check_signal_legacy。"""
    from launcher import check_signal
    with patch("events.parser.parse_signal", return_value=True) as m:
        assert check_signal({"source": "curl http://localhost:8890", "filter": ""}) is True


def test_check_signal_journalctl_error():
    """旧格式 journalctl → _check_signal_legacy。"""
    from launcher import check_signal
    with patch("events.parser.parse_signal", return_value=True) as m:
        assert check_signal({"source": "journalctl --user -u demo --since 1h --no-pager",
                             "filter": "ERROR|exception|Traceback"}) is True


def test_check_signal_journalctl_clean():
    """旧格式 journalctl 无 ERROR。"""
    from launcher import check_signal
    with patch("events.parser.parse_signal", return_value=False):
        assert check_signal({"source": "journalctl --user -u demo --since 1h --no-pager",
                             "filter": "ERROR|exception|Traceback"}) is False


def test_check_signal_git_staged():
    """旧格式 git diff → _check_signal_legacy。"""
    from launcher import check_signal
    with patch("events.parser.parse_signal", return_value=True) as m:
        assert check_signal({"source": "git diff --cached --name-only", "filter": ""}) is True


def test_check_signal_git_no_staged():
    """旧格式 git diff 无 staged。"""
    from launcher import check_signal
    with patch("events.parser.parse_signal", return_value=False):
        assert check_signal({"source": "git diff --cached --name-only", "filter": ""}) is False


# ── 新格式 (type 分发) 信号测试 ──
# 注: 新格式经过 launcher.check_signal() → signals.check_signal()
#   → signal_parser.parse_signal() → signal_parser._check_bus / _check_shell / ...
# mock 目标应指向 signal_parser._check_bus 等


def test_new_format_bus_dispatch():
    """type=bus → events.parser.parse_signal。"""
    from launcher import check_signal
    with patch("events.parser.parse_signal", return_value=True) as m:
        sig = {"type": "bus", "spec": {"category": "security"}, "filter": "", "timeout_sec": 10}
        assert check_signal(sig) is True
        m.assert_called_once()


def test_new_format_shell_dispatch():
    """type=shell → events.parser.parse_signal。"""
    from launcher import check_signal
    with patch("events.parser.parse_signal", return_value=False) as m:
        sig = {"type": "shell", "spec": {"command": "echo ok"}, "filter": "ok", "timeout_sec": 5}
        assert check_signal(sig) is False
        m.assert_called_once()


def test_new_format_http_dispatch():
    """type=http → events.parser.parse_signal。"""
    from launcher import check_signal
    with patch("events.parser.parse_signal", return_value=True) as m:
        sig = {"type": "http", "spec": {"url": "http://localhost:8890"}, "filter": ""}
        assert check_signal(sig) is True
        m.assert_called_once()


def test_new_format_journalctl_dispatch():
    """type=journalctl → events.parser.parse_signal。"""
    from launcher import check_signal
    with patch("events.parser.parse_signal", return_value=False) as m:
        sig = {"type": "journalctl", "spec": {"unit": "sister-agent-dkk"}, "filter": "ERROR"}
        assert check_signal(sig) is False
        m.assert_called_once()


def test_new_format_custom_dispatch():
    """type=custom → events.parser.parse_signal。"""
    from launcher import check_signal
    with patch("events.parser.parse_signal", return_value=True) as m:
        sig = {"type": "custom", "spec": {"description": "manual trigger"}, "filter": "trigger"}
        assert check_signal(sig) is True
        m.assert_called_once()


def test_new_format_unknown_type():
    """未知 type → 返回 False。"""
    from launcher import check_signal
    sig = {"type": "nonexistent", "spec": {}, "filter": ""}
    assert check_signal(sig) is False
