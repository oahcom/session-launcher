#!/usr/bin/env python3
"""
watchdog.py — 伙伴存活守护线程

定期检查伙伴 CCS 的 tmux session 是否存活，
挂了则自动重启并恢复上下文（从哨兵读取伙伴信息）。
"""
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Optional

from sentinel import (
    CcsSentinel, read_sentinel, write_sentinel, delete_sentinel,
    update_health, SENTINEL_DIR,
)

TMUX_PREFIX = "ccs-"


def _is_alive(tmux_name: str) -> bool:
    try:
        r = subprocess.run(
            ["tmux", "has-session", "-t", tmux_name],
            capture_output=True, timeout=5
        )
        return r.returncode == 0
    except Exception:
        return False


def _log(tag: str, msg: str):
    print(f"[{tag}] {datetime.now():%H:%M:%S} {msg}", flush=True)


def _restart_partner(partner_role: str):
    """从哨兵读取伙伴上下文并重启。"""
    old = read_sentinel(partner_role)
    cmd = [sys.executable, sys.argv[0], "start", partner_role]
    if old:
        cmd.append(old.title or partner_role)
        if old.partner:
            cmd += ["--partner", old.partner]
        if old.bus_track:
            cmd += ["--bus-track", old.bus_track, "--bus-timeout", str(old.bus_timeout)]
    else:
        cmd.append(partner_role)
    cmd += ["--no-attach"]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _log("watchdog", f"已发起 {partner_role} 重启")


def _run(this_role: str, partner_role: str, auto_restart: bool,
         interval: int, restart_delay: int):
    """守护线程主循环。异常不退出，记录后继续。"""
    partner_tmux = f"{TMUX_PREFIX}{partner_role}"
    tag = f"watchdog:{this_role}"

    while True:
        try:
            time.sleep(interval)

            alive = _is_alive(partner_tmux)

            # 更新本方哨兵的健康状态
            update_health(this_role,
                          last_watchdog_check=time.time(),
                          watchdog_ok=alive)

            if alive:
                continue

            _log(tag, f"partner {partner_role} 已死")

            if not auto_restart:
                _log(tag, "未配置 auto-restart，跳过")
                continue

            _log(tag, f"正在重启 {partner_role}...")
            delete_sentinel(partner_role)
            time.sleep(restart_delay)
            _restart_partner(partner_role)

            # 更新本方 restart_count
            s = read_sentinel(this_role)
            if s:
                s.health.restart_count += 1
                write_sentinel(s)
        except Exception as e:
            _log(tag, f"异常: {e}，等待下一轮重试")
            time.sleep(interval)


def start_watchdog(this_role: str, partner_role: str,
                   auto_restart: bool = False,
                   interval: int = 30,
                   restart_delay: int = 5) -> threading.Thread:
    """启动守护线程。daemon=True 但需主线程保持存活（detach 模式由 caller 保证）。"""
    t = threading.Thread(
        target=_run,
        args=(this_role, partner_role, auto_restart, interval, restart_delay),
        daemon=False,
        name=f"watchdog:{this_role}:{partner_role}",
    )
    t.start()
    return t
