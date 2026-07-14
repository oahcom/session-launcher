__all__ = [
    '_check_memory_before_launch',
    '_find_claude_pid',
    '_find_claude_session_id',
    '_is_alive',
    '_tmux_send',
    '_tmux_output',
    '_tmux_kill',
    '_find_codex_pid',
    '_active_codex_session_count',
    '_write_codex_sentinel',
    '_wait_codex_ready',
    'TMUX_PREFIX',
    'CODEX_TMUX_PREFIX',
    'CODEX_SENTINEL_DIR',
    'CODEX_LOOP_DELAY',
    'CODEX_OUTPUT_MAX',
    'CODEX_ERROR_MAX',
    'CODEX_SESSION_MAX',
    'CODEX_READY_RETRIES',
    'CODEX_READY_INTERVAL',
    '_MEM_FREE_MIN_MB',
    '_CCS_LAUNCH_INTERVAL',
]

#!/usr/bin/env python3
"""Auto-generated: tmux_ops.py — extracted from core.py"""

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

TMUX_PREFIX = "ccs-"

CODEX_TMUX_PREFIX = "cdx-"

CODEX_SENTINEL_DIR = Path("/tmp/cdx-sentinels")

CODEX_LOOP_DELAY = 60

CODEX_OUTPUT_MAX = 2000

CODEX_ERROR_MAX = 500

CODEX_SESSION_MAX = 5

CODEX_READY_RETRIES = 15

CODEX_READY_INTERVAL = 0.5

_MEM_FREE_MIN_MB = 1000  # 启动新 CCS 前可用内存至少需要 1000MB

_CCS_LAUNCH_INTERVAL = 8  # 连续启动 CCS 之间至少等待秒数

_last_ccs_launch_ts: float = 0.0


def _check_memory_before_launch(role: str) -> str | None:
    """检查是否有足够内存启动新 CCS。内存不足返回错误描述，否则返回 None。"""
    global _last_ccs_launch_ts
    try:
        meminfo = Path("/proc/meminfo").read_text()
        for line in meminfo.split("\n"):
            if line.startswith("MemAvailable:"):
                parts = line.split()
                if len(parts) >= 2:
                    avail_mb = int(parts[1]) // 1024
                    if avail_mb < _MEM_FREE_MIN_MB:
                        return (f"可用内存仅 {avail_mb}MB（< {_MEM_FREE_MIN_MB}MB），"
                                f"跳过 {role} 启动")
                break
    except Exception:
        pass

    # 连续启动间隔
    now = time.time()
    elapsed = now - _last_ccs_launch_ts
    if _last_ccs_launch_ts > 0 and elapsed < _CCS_LAUNCH_INTERVAL:
        wait = _CCS_LAUNCH_INTERVAL - elapsed
        print(f"⏳ 等待 {wait:.0f}s 避免并发启动 OOM...", flush=True)
        time.sleep(wait)

    _last_ccs_launch_ts = time.time()
    return None

def _find_claude_pid(tmux_name: str, process_name: str = "claude") -> Optional[int]:
    """从 tmux pane 中找到子进程 PID。

    Args:
        tmux_name: tmux session name
        process_name: 要查找的进程名（默认 'claude'，codex 场景传 'codex'）
    """
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", f"{tmux_name}:0.0", "#{pane_pid}"],
            capture_output=True, text=True, timeout=5
        )
        pane_pid = r.stdout.strip()
        if pane_pid.isdigit():
            # 先精确匹配指定进程名
            r2 = subprocess.run(
                ["pgrep", "-P", pane_pid, "-x", process_name],
                capture_output=True, text=True, timeout=3
            )
            if r2.stdout.strip():
                return int(r2.stdout.strip().split("\n")[0])
            # 回退到模糊匹配（兼容旧版 claude 在不同环境的进程名差异）
            r3 = subprocess.run(
                ["pgrep", "-P", pane_pid, "-f", process_name],
                capture_output=True, text=True, timeout=3
            )
            if r3.stdout.strip():
                return int(r3.stdout.strip().split("\n")[0])
            return int(pane_pid)
    except Exception:
        pass
    return None


def _find_claude_session_id(role: str = "") -> Optional[str]:
    """从 CCS 独立工作目录中查找最新的 claude session ID。"""
    try:
        if role:
            base = Path(f"/tmp/ccs-sessions/{role}/.claude/projects")
        else:
            base = Path.home() / ".claude" / "projects"
        if not base.exists():
            return None
        latest_file = None
        latest_time = 0
        for proj_dir in base.iterdir():
            if not proj_dir.is_dir():
                continue
            for f in proj_dir.glob("*.jsonl"):
                mtime = f.stat().st_mtime
                if mtime > latest_time:
                    latest_time = mtime
                    latest_file = f
        if not latest_file:
            return None
        return latest_file.stem
    except Exception:
        return None


def _is_alive(tmux_name: str) -> bool:
    try:
        r = subprocess.run(
            ["tmux", "has-session", "-t", tmux_name],
            capture_output=True, timeout=5
        )
        return r.returncode == 0
    except Exception:
        return False


def _tmux_send(tmux_name: str, message: str):
    for i in range(0, len(message), 500):
        chunk = message[i:i + 500]
        subprocess.run(
            ["tmux", "send-keys", "-t", f"{tmux_name}:0.0", chunk],
            capture_output=True, timeout=3
        )
        time.sleep(0.05)
    subprocess.run(
        ["tmux", "send-keys", "-t", f"{tmux_name}:0.0", "Enter"],
        capture_output=True, timeout=3
    )


def _tmux_output(tmux_name: str, tail: int = 20) -> str:
    try:
        r = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", f"{tmux_name}:0.0"],
            capture_output=True, text=True, timeout=5
        )
        lines = r.stdout.strip().split("\n")
        return "\n".join(lines[-tail:])
    except Exception as e:
        return f"[错误] {e}"


def _tmux_kill(tmux_name: str):
    subprocess.run(
        ["tmux", "kill-session", "-t", tmux_name],
        capture_output=True, timeout=5
    )

def _find_codex_pid(tmux_name: str) -> Optional[int]:
    """查找 Codex 子进程 PID（仅精确匹配 codex，不 fallback 到 pane PID）。

    与 _find_claude_pid 不同：不返回 tmux pane PID 作为兜底，
    确保 _wait_codex_ready 不会误判未就绪的 session 为就绪。
    """
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", f"{tmux_name}:0.0", "#{pane_pid}"],
            capture_output=True, text=True, timeout=5
        )
        pane_pid = r.stdout.strip()
        if pane_pid.isdigit():
            for flag in ("-x", "-f"):
                r2 = subprocess.run(
                    ["pgrep", "-P", pane_pid, flag, "codex"],
                    capture_output=True, text=True, timeout=3
                )
                if r2.stdout.strip():
                    return int(r2.stdout.strip().split("\n")[0])
    except Exception:
        pass
    return None


def _active_codex_session_count() -> int:
    """当前运行的 Codex session 数量（通过哨兵 + tmux 存活确认）。"""
    count = 0
    if not CODEX_SENTINEL_DIR.exists():
        return 0
    for f in CODEX_SENTINEL_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        tmux_name = f"{CODEX_TMUX_PREFIX}{data.get('role', '')}"
        try:
            r = subprocess.run(["tmux", "has-session", "-t", tmux_name],
                               capture_output=True, timeout=5)
            if r.returncode == 0:
                count += 1
        except Exception:
            continue
    return count


def _write_codex_sentinel(role_name: str, role_title: str,
                          tmux_name: str, pid: Optional[int],
                          lifecycle: str) -> Path:
    """线程安全写入 Codex 哨兵（直接写 CODEX_SENTINEL_DIR）。"""
    CODEX_SENTINEL_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "role": role_name,
        "title": role_title,
        "tmux_session": tmux_name,
        "pid": pid,
        "started_at": time.time(),
        "lifecycle": lifecycle,
        "engine": "codex",
    }
    path = CODEX_SENTINEL_DIR / f"{role_name}.json"
    # 原子写入：先写临时文件再 rename，避免并发读写出错
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(path)
    return path


def _wait_codex_ready(tmux_name: str, timeout: float = 15) -> bool:
    """轮询等待 tmux session 就绪 + PID 有效，最大等待 timeout 秒。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_alive(tmux_name):
            pid = _find_codex_pid(tmux_name)
            if pid is not None:
                return True
        time.sleep(CODEX_READY_INTERVAL)
    return False
