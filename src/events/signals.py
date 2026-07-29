#!/usr/bin/env python3
"""
Signal Checkers — 所有 input_signals 的检查逻辑。

支持新旧两种格式：
  新格式: {"type": "bus|shell|http|journalctl|custom", "spec": {...}, "filter": "...", "schedule": "...", "timeout_sec": 10}
  旧格式: {"source": "bus cat=security"} — 自动转换 + warning
"""
import logging
import subprocess
import shlex
import os
import warnings
import re
import urllib.request
from pathlib import Path

LOG = logging.getLogger("signals")

from paths import BUS_CLIENT as _BUS_CLIENT_PATH

BUS_CLIENT = str(_BUS_CLIENT_PATH)


# ── 新格式统一入口 ──

def check_signal(signal_def: dict) -> bool:
    """统一入口：按 type 分发检查信号。

    新格式字段：
      - type: "bus" | "shell" | "http" | "journalctl" | "custom"
      - spec: 对应类型的参数（dict）
        bus: {"category": "security"}
        shell: {"command": "systemctl is-active ..."}
        http: {"url": "http://localhost:8890"}
        journalctl: {"unit": "sister-agent-dkk", "command": "journalctl ..."}
        custom: {"description": "..."}
      - filter: 过滤关键词（可选，用 | 分隔）
      - schedule: cron 表达式（可选，不参与即时检查）
      - timeout_sec: 超时秒数（可选，默认 10）

    旧格式（source 字段）自动转换并打印 warning。
    """
    # 委托给 signal_parser 统一解析器
    from events.parser import parse_signal
    return parse_signal(signal_def)


# ── 旧格式兼容层（保留供 check_signal_by_name 使用） ──

def check_bus_unread(filter_str: str = "") -> bool:
    """检查 bus 是否有未读消息。"""
    try:
        result = subprocess.run(
            ["python3", BUS_CLIENT, "unread", "--all"],
            capture_output=True, text=True, timeout=10
        )
        output = result.stdout
        if "0 unread" in output.lower() or "✅ no unread" in output.lower():
            return False
        if filter_str and filter_str.lower() not in output.lower():
            return False
        return True
    except Exception:
        return False


def check_systemctl_active(filter_str: str = "") -> bool:
    """检查 systemd 服务是否有异常。"""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "sister-agent-dkk.service",
             "sister-agent-ssk.service", "cron-worker.service"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.strip().split("\n"):
            if line.strip() != "active":
                return True

        log_result = subprocess.run(
            ["journalctl", "--user", "-u", "sister-agent-dkk",
             "-u", "sister-agent-ssk", "-u", "cron-worker",
             "--since", "30 minutes ago", "--no-pager"],
            capture_output=True, text=True, timeout=10
        )
        app_error_pattern = re.compile(
            r'(?:Traceback|exception|critical|fatal'
            r'|ERROR|error\b(?!.*with result))',
            re.IGNORECASE
        )
        for line in log_result.stdout.splitlines():
            if "failed with result" in line.lower():
                continue
            if app_error_pattern.search(line):
                return True

        return False
    except Exception:
        return False


def check_http_health(filter_str: str = "") -> bool:
    """检查 HTTP 端点是否健康。"""
    from paths import HEALTH_CHECK_ENDPOINTS
    endpoints = HEALTH_CHECK_ENDPOINTS
    for ep in endpoints:
        try:
            result = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--connect-timeout", "3", ep],
                capture_output=True, text=True, timeout=3
            )
            if result.stdout.strip() != "200":
                return True
        except Exception:
            return True
    return False


def check_journalctl_errors(filter_str: str = "") -> bool:
    """检查 journalctl 是否有错误。"""
    try:
        result = subprocess.run(
            ["journalctl", "--user", "-u", "sister-agent-dkk", "-u", "sister-agent-ssk",
             "--since", "1 hour ago", "--no-pager"],
            capture_output=True, text=True, timeout=10
        )
        for f in filter_str.split("|"):
            if f.lower() in result.stdout.lower():
                return True
        return False
    except Exception:
        return False


def check_git_staged(filter_str: str = "") -> bool:
    """检查是否有 staged 代码。"""
    repos = [
        str(Path.home() / ".hermes"),
        str(Path.home() / "hermes-session-roles"),
        str(Path.home() / "dkk-projects" / "auto-switch-ip"),
    ]
    for repo in repos:
        try:
            result = subprocess.run(
                ["git", "-C", repo, "diff", "--cached", "--name-only"],
                capture_output=True, text=True, timeout=5
            )
            if result.stdout.strip():
                return True
        except Exception as _e:
            LOG.warning("git_staged check failed for %s: %s", repo, _e)
    return False


def check_session_size(filter_str: str = "") -> bool:
    """检查 session 文件是否过大（单个文件 > 50MB）。"""
    try:
        projects_dir = Path.home() / ".claude" / "projects"
        max_bytes = 50 * 1024 * 1024
        for f in projects_dir.glob("*/*.jsonl"):
            if f.is_file() and f.stat().st_size > max_bytes:
                return True
        return False
    except Exception:
        return False


def check_running_sessions(filter_str: str = "") -> bool:
    """检查活跃的 session。"""
    try:
        result = subprocess.run(
            ["ps", "aux"],
            capture_output=True, text=True, timeout=5
        )
        claude_sessions = [l for l in result.stdout.split("\n") if "claude" in l.lower()]
        return len(claude_sessions) > 0
    except Exception:
        return False


def check_mem_disk(filter_str: str = "") -> bool:
    """检查内存和磁盘使用情况。"""
    try:
        meminfo = Path("/proc/meminfo").read_text()
        for line in meminfo.split("\n"):
            if line.startswith("MemAvailable:"):
                parts = line.split()
                if len(parts) >= 2:
                    mem_available_kb = int(parts[1])
                    mem_available_mb = mem_available_kb / 1024
                    if mem_available_mb < 500:
                        return True
                break
    except Exception as _e:
        LOG.warning("mem_disk meminfo check failed: %s", _e)
        mem_available_mb = -1  # safe fallback — treat as unknown

    try:
        result = subprocess.run(
            ["df", "/", "--output=pcent"],
            capture_output=True, text=True, timeout=5
        )
        lines = result.stdout.strip().split("\n")
        if len(lines) >= 2:
            usage_str = lines[1].strip().rstrip("%")
            if usage_str.isdigit():
                usage = int(usage_str)
                if usage > 90:
                    return True
    except Exception as _e:
        LOG.warning("mem_disk df check failed: %s", _e)

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
    """按名称检查信号（旧接口，保留兼容）。"""
    checker = SIGNAL_CHECKERS.get(name)
    if checker:
        return checker(filter_str)
    return False