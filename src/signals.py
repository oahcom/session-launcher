#!/usr/bin/env python3
"""
Signal Checkers — 所有 input_signals 的检查逻辑。
"""
import subprocess
import shlex
import os
from pathlib import Path
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
    """检查 systemd 服务是否有异常。

    增强版：同时检查：
    1. is-active 状态（必须全部 active）
    2. journalctl 最近 30 分钟是否有 ERROR/exception/Traceback/CRITICAL
    """
    try:
        # 1. 检查服务状态
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "sister-agent-dkk.service",
             "sister-agent-ssk.service", "cron-worker.service"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.strip().split("\n"):
            if line.strip() != "active":
                return True  # 有服务异常

        # 2. 检查日志错误（仅最近 30 分钟，避免历史噪音）
        # 3 次连续失败才标 down 的红线已在维护者 prompt 里，这里做单次扫描
        log_result = subprocess.run(
            ["journalctl", "--user", "-u", "sister-agent-dkk",
             "-u", "sister-agent-ssk", "-u", "cron-worker",
             "--since", "30 minutes ago", "--no-pager"],
            capture_output=True, text=True, timeout=10
        )
        error_patterns = ["error", "exception", "traceback", "critical", "failed", "fatal"]
        for pattern in error_patterns:
            if pattern in log_result.stdout.lower():
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
             "--since", "1 hour ago", "--no-pager"],
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
    """检查 session 文件是否过大（单个文件 > 50MB）。"""
    try:
        projects_dir = Path.home() / ".claude" / "projects"
        max_bytes = 50 * 1024 * 1024
        for f in projects_dir.glob("*/*.jsonl"):
            if f.is_file() and f.stat().st_size > max_bytes:
                return True
        return False
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


def check_mem_disk(_filter: str = "") -> bool:
    """检查内存和磁盘使用情况。

    检查 /proc/meminfo 可用内存 < 500MB 或磁盘 / 使用率 > 90%。
    检测到异常返回 True（有工作需要处理）。
    """
    # 检查内存
    try:
        meminfo = Path("/proc/meminfo").read_text()
        for line in meminfo.split("\n"):
            if line.startswith("MemAvailable:"):
                # 格式: "MemAvailable:    1234567 kB"
                parts = line.split()
                if len(parts) >= 2:
                    mem_available_kb = int(parts[1])
                    mem_available_mb = mem_available_kb / 1024
                    if mem_available_mb < 500:
                        return True
                break
    except:
        pass

    # 检查磁盘使用率
    try:
        result = subprocess.run(
            ["df", "/", "--output=pcent"],
            capture_output=True, text=True, timeout=5
        )
        # 输出格式:
        # Use%
        #  90%
        lines = result.stdout.strip().split("\n")
        if len(lines) >= 2:
            usage_str = lines[1].strip().rstrip("%")
            if usage_str.isdigit():
                usage = int(usage_str)
                if usage > 90:
                    return True
    except:
        pass

    return False


SIGNAL_CHECKERS = {
    "bus_unread": check_bus_unread,
    "systemctl_active": check_systemctl_active,
    "http_health": check_http_health,
    "journalctl_errors": check_journalctl_errors,
    "git_staged": check_git_staged,
    "session_size": check_session_size,
    "running_sessions": check_running_sessions,
    "mem_disk": check_mem_disk,
}


def check_signal_by_name(name: str, filter_str: str = "") -> bool:
    """按名称检查信号。"""
    checker = SIGNAL_CHECKERS.get(name)
    if checker:
        return checker(filter_str)
    return False