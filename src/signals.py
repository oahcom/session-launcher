#!/usr/bin/env python3
"""
Signal Checkers — 所有 input_signals 的检查逻辑。
"""
import subprocess
from typing import Any


BUS_CLIENT = "/home/administrator/.hermes/scripts/bus_client.py"


def check_bus_unread(_filter: str = "") -> bool:
    """检查 bus 是否有未读消息。"""
    try:
        result = subprocess.run(
            ["python3", BUS_CLIENT, "unread", "--all"],
            capture_output=True, text=True, timeout=10
        )
        output = result.stdout
        if "0 unread" in output.lower() or "✅ no unread" in output.lower():
            return False
        if _filter and _filter.lower() not in output.lower():
            return False
        return True
    except:
        return False


def check_systemctl_active(_filter: str = "") -> bool:
    """检查 systemd 服务是否有异常。"""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "sister-agent-dkk.service",
             "sister-agent-ssk.service", "cron-worker.service"],
            capture_output=True, text=True, timeout=5
        )
        # 如果有服务不是 active
        for line in result.stdout.strip().split("\n"):
            if line.strip() != "active":
                return True
        return False
    except:
        return False


def check_http_health(_filter: str = "") -> bool:
    """检查 HTTP 端点是否健康。"""
    endpoints = ["http://localhost:8890", "http://localhost:20128"]
    for ep in endpoints:
        try:
            result = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--connect-timeout", "3", ep],
                capture_output=True, text=True, timeout=3
            )
            if result.stdout.strip() != "200":
                return True
        except:
            return True
    return False


def check_journalctl_errors(_filter: str = "") -> bool:
    """检查 journalctl 是否有错误。"""
    try:
        result = subprocess.run(
            ["journalctl", "--user", "-u", "sister-agent-dkk", "-u", "sister-agent-ssk",
             "--since", "1h", "--no-pager"],
            capture_output=True, text=True, timeout=10
        )
        for f in _filter.split("|"):
            if f.lower() in result.stdout.lower():
                return True
        return False
    except:
        return False


def check_git_staged(_filter: str = "") -> bool:
    """检查是否有 staged 代码。"""
    repos = [
        "/home/administrator/.hermes",
        "/home/administrator/hermes-session-roles",
        "/home/administrator/dkk-projects/auto-switch-ip",
    ]
    for repo in repos:
        try:
            result = subprocess.run(
                ["git", "-C", repo, "diff", "--cached", "--name-only"],
                capture_output=True, text=True, timeout=5
            )
            if result.stdout.strip():
                return True
        except:
            pass
    return False


def check_session_size(_filter: str = "") -> bool:
    """检查 session 文件是否过大。"""
    try:
        result = subprocess.run(
            ["ls", "-lt", "~/.claude/projects/*/*.jsonl"],
            shell=True, capture_output=True, text=True, timeout=5
        )
        lines = result.stdout.strip().split("\n")
        # 检查前几个文件的大小
        return len(lines) > 5
    except:
        return False


def check_running_sessions(_filter: str = "") -> bool:
    """检查活跃的 session。"""
    try:
        result = subprocess.run(
            ["ps", "aux"],
            capture_output=True, text=True, timeout=5
        )
        claude_sessions = [l for l in result.stdout.split("\n") if "claude" in l.lower()]
        return len(claude_sessions) > 0
    except:
        return False


SIGNAL_CHECKERS = {
    "bus_unread": check_bus_unread,
    "systemctl_active": check_systemctl_active,
    "http_health": check_http_health,
    "journalctl_errors": check_journalctl_errors,
    "git_staged": check_git_staged,
    "session_size": check_session_size,
    "running_sessions": check_running_sessions,
}


def check_signal_by_name(name: str, filter_str: str = "") -> bool:
    """按名称检查信号。"""
    checker = SIGNAL_CHECKERS.get(name)
    if checker:
        return checker(filter_str)
    return False