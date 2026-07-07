#!/usr/bin/env python3
"""
core.py — CCS 核心操作

start / stop / status / send / output / health
所有操作通过哨兵文件协调，不直接操作 tmux。
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from sentinel import (
    CcsSentinel, CcsHealth, write_sentinel, read_sentinel,
    delete_sentinel, list_sentinels, SENTINEL_DIR,
)
from watchdog import start_watchdog
from tracker import start_tracker

TMUX_PREFIX = "ccs-"
BUS_CLIENT = Path("~/.hermes/scripts/bus_client.py").expanduser()


# ── tmux 底层操作 ──────────────────────────────────────────

def _find_claude_pid(tmux_name: str) -> Optional[int]:
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", f"{tmux_name}:0.0", "#{pane_pid}"],
            capture_output=True, text=True, timeout=5
        )
        pane_pid = r.stdout.strip()
        if pane_pid.isdigit():
            r2 = subprocess.run(
                ["pgrep", "-P", pane_pid, "-f", "claude"],
                capture_output=True, text=True, timeout=3
            )
            if r2.stdout.strip():
                return int(r2.stdout.strip().split("\n")[0])
            return int(pane_pid)
    except Exception:
        pass
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
        chunk = message[i:i+500]
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


# ── 核心操作 ────────────────────────────────────────────────

def start(role: str, title: str = "", detach: bool = False,
          init_prompt: str = "", partners: list[str] = None,
          auto_restart: bool = False, bus_track: str = "",
          bus_timeout: int = 300) -> dict:
    """创建一个 CCS 并写入哨兵。"""
    tmux_name = f"{TMUX_PREFIX}{role}"
    partners = partners or []

    if _is_alive(tmux_name):
        return {"success": False, "error": "已存在", "tmux_session": tmux_name}

    # 1. 启动 tmux + claude
    cmd = (
        "claude --model 9router_hermes"
        " --dangerously-skip-permissions"
        " --effort max"
        " --permission-mode bypassPermissions"
    )
    r = subprocess.run([
        "tmux", "new-session", "-d", "-s", tmux_name,
        "-e", "FORCE_PERSONA=0",
        "bash", "-c", f"tmux set -g bracketed-paste off; {cmd}"
    ], capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return {"success": False, "error": f"tmux 启动失败: {r.stderr.strip()}"}

    # 2. 等 claude 就绪
    for i in range(15):
        time.sleep(1)
        try:
            out = subprocess.run(
                ["tmux", "capture-pane", "-p", "-t", f"{tmux_name}:0.0", "-S-3"],
                capture_output=True, text=True, timeout=3
            )
            if "❯" in out.stdout:
                break
        except Exception:
            pass
    time.sleep(1)

    # 3. 注入初始 prompt
    if init_prompt:
        _tmux_send(tmux_name, init_prompt)
        time.sleep(2)

    # 4. 写哨兵
    pid = _find_claude_pid(tmux_name)
    s = CcsSentinel(
        role=role,
        title=title or role,
        tmux_session=tmux_name,
        pid=pid,
        started_at=time.time(),
        lifecycle="infinite",
        partner=partners[0] if partners else "",
        bus_track=bus_track,
        bus_timeout=bus_timeout,
    )
    write_sentinel(s)

    # 5. 启动守护线程（仅 detach 模式，non-detach 会被 os.execvp 杀死）
    if detach:
        for p in partners:
            start_watchdog(role, p, auto_restart=auto_restart, interval=30)
            print(f"✅ 守护线程: 监控 {p} 存活")

        # 6. 启动轮次追踪（如有 bus_track）
        if bus_track:
            start_tracker(role, bus_track, timeout_sec=bus_timeout,
                          interval=10, partners=partners)
            print(f"✅ 轮次追踪: 监控 {bus_track} 死锁 (超时 {bus_timeout}s)")
    elif partners or bus_track:
        print(f"⚠ 非 detach 模式，监控线程不会启动（需要 --no-attach）")

    result = {
        "success": True,
        "role": role,
        "tmux_session": tmux_name,
        "pid": pid,
        "partners": partners or [],
        "bus_track": bus_track or None,
    }

    if not detach:
        print(f"🎯 进入 {tmux_name} (Ctrl+B D 退出)")
        os.execvp("tmux", ["tmux", "attach", "-t", tmux_name])
    else:
        print(f"🎯 后台运行 (tmux attach -t {tmux_name} 进入)")

    return result


def stop(role: str) -> dict:
    """终止 CCS 并清理哨兵。"""
    tmux_name = f"{TMUX_PREFIX}{role}"
    was_alive = _is_alive(tmux_name)
    _tmux_kill(tmux_name)
    delete_sentinel(role)
    if not was_alive:
        return {"success": False, "error": "CCS 不存在", "role": role}
    return {"success": True, "role": role}


def status() -> list[dict]:
    """列出所有 CCS 的运行状态（含死亡的）。"""
    sentinels = list_sentinels()
    statuses = []
    for s in sentinels:
        alive = _is_alive(s.tmux_session)
        statuses.append({
            "role": s.role,
            "title": s.title,
            "alive": alive,
            "pid": s.pid,
            "uptime_sec": int(time.time() - s.started_at),
            "lifecycle": s.lifecycle,
            "partner": s.partner or None,
            "bus_track": s.bus_track or None,
            "health": {
                "watchdog_ok": s.health.watchdog_ok,
                "bus_msg_age": round(s.health.last_bus_msg_age, 0),
                "restart_count": s.health.restart_count,
            },
        })
    return statuses


def send(role: str, message: str) -> dict:
    """向 CCS 发送消息。"""
    tmux_name = f"{TMUX_PREFIX}{role}"
    if not _is_alive(tmux_name):
        return {"success": False, "error": f"CCS {role} 未运行"}
    _tmux_send(tmux_name, message)
    return {"success": True, "sent_chars": len(message)}


def output(role: str, tail: int = 20) -> str:
    """截取 CCS tmux pane 的最新输出。"""
    return _tmux_output(f"{TMUX_PREFIX}{role}", tail=tail)


def health_check(role: str = "") -> dict:
    """健康检查：返回所有（或指定）CCS 的存活状态。"""
    sentinels = list_sentinels() if not role else (
        [s] if (s := read_sentinel(role)) else []
    )
    result = {}
    for s in sentinels:
        alive = _is_alive(s.tmux_session)
        result[s.role] = {
            "alive": alive,
            "partner": s.partner or None,
            "bus_track": s.bus_track or None,
            "last_bus_msg_age": round(s.health.last_bus_msg_age, 0),
            "watchdog_ok": s.health.watchdog_ok,
            "restart_count": s.health.restart_count,
            "uptime_sec": int(time.time() - s.started_at) if alive else 0,
        }
    return result


def register(role: str, tmux_name: str, title: str = "") -> dict:
    """将手动创建的 tmux session 注册为 CCS。"""
    if not _is_alive(tmux_name):
        return {"success": False, "error": f"tmux session '{tmux_name}' 不存在"}
    pid = _find_claude_pid(tmux_name)
    s = CcsSentinel(
        role=role,
        title=title or role,
        tmux_session=tmux_name,
        pid=pid,
        started_at=time.time(),
    )
    write_sentinel(s)
    return {"success": True, "role": role, "tmux_session": tmux_name, "pid": pid}
