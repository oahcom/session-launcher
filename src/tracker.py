#!/usr/bin/env python3
"""
tracker.py — 轮次追踪 + 死锁检测

定期 poll bus 某分类的最新时间戳，超时则发提醒。
与 watchdog 交叉验证：如果伙伴也活但 bus 仍停，强制触发。
"""
import json
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from sentinel import update_health

TMUX_PREFIX = "ccs-"
BUS_CLIENT = Path("~/.hermes/scripts/bus_client.py").expanduser()


def _log(tag: str, msg: str):
    print(f"[{tag}] {datetime.now():%H:%M:%S} {msg}", flush=True)


def _bus_read_latest(cat: str) -> Optional[dict]:
    """读取 bus 某分类最新一条消息。"""
    try:
        r = subprocess.run(
            ["python3", str(BUS_CLIENT), "read", "--cat", cat, "--limit", "1", "--json"],
            capture_output=True, text=True, timeout=15
        )
        data = __import__("json").loads(r.stdout)
        facts = data.get("facts", data) if isinstance(data, dict) else data
        return facts[0] if facts else None
    except Exception:
        return None


def _bus_write(cat: str, text: str, src: str = ""):
    cmd = ["python3", str(BUS_CLIENT), "write", cat, text]
    if src:
        cmd += ["--src", src]
    subprocess.run(cmd, capture_output=True, timeout=15)


def _run(this_role: str, bus_cat: str, timeout_sec: int,
         interval: int, partners: list[str]):
    """轮次追踪主循环。异常不退出，记录后继续。"""
    tag = f"tracker:{this_role}"
    last_reminder = 0.0

    while True:
        try:
            time.sleep(interval)

            latest = _bus_read_latest(bus_cat)
            if not latest:
                continue

            ts = latest.get("timestamp", 0)
            age = time.time() - ts
            src = latest.get("src", "")

            # 更新本方哨兵的 bus 消息年龄
            update_health(this_role, last_bus_msg_age=age, last_turn_check=time.time())

            # 未超时 → 跳过
            if age <= timeout_sec:
                continue

            # 已超时且自己不是上一轮作者 → 发提醒
            if src == this_role:
                continue

            now = time.time()
            if now - last_reminder < 1800:
                continue

            # 交叉检查伙伴存活
            partner_alive = False
            for p in partners:
                p_alive = False
                try:
                    r = subprocess.run(
                        ["tmux", "has-session", "-t", f"{TMUX_PREFIX}{p}"],
                        capture_output=True, timeout=5
                    )
                    p_alive = r.returncode == 0
                except Exception:
                    pass
                if p_alive:
                    partner_alive = True

            if partner_alive:
                # 伙伴活但 bus 停 → 直接发提醒给伙伴
                _bus_write(bus_cat,
                           f"[{this_role}] 死锁检测: {bus_cat} 最后消息 {int(age)}s 前 (by {src})，请继续",
                           src=this_role)
                _log(tag, f"死锁提醒: {bus_cat} 超时 {int(age)}s，已通知 {src}")
            else:
                _log(tag, f"死锁: {bus_cat} 超时 {int(age)}s，但伙伴已死（watchdog 负责重启）")

            last_reminder = now

        except Exception as e:
            _log(tag, f"异常: {e}，等待下一轮重试")
            time.sleep(interval)


def start_tracker(this_role: str, bus_cat: str,
                  timeout_sec: int = 300,
                  interval: int = 10,
                  partners: list[str] = None) -> threading.Thread:
    """启动轮次追踪线程。daemon=False 保持存活。"""
    t = threading.Thread(
        target=_run,
        args=(this_role, bus_cat, timeout_sec, interval, partners or []),
        daemon=False,
        name=f"tracker:{this_role}:{bus_cat}",
    )
    t.start()
    return t
