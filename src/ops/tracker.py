#!/usr/bin/env python3
"""
tracker.py — 轮次追踪 + 死锁检测

定期 poll bus 某分类的最新时间戳，超时则发提醒。
与 watchdog 交叉验证：如果伙伴也活但 bus 仍停，强制触发。

从 subprocess bus_client 迁移到 Blackboard 直接 API（P2 修复）。
"""
__all__ = [
    'start_tracker',
]

import logging
import threading
import time
from datetime import datetime
from typing import Optional

from ops.sentinel import update_health

_log = logging.getLogger("tracker")
# 当独立导入时（未走 core.py），确保日志能被看到
if not _log.handlers and not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def _log_info(tag: str, msg: str):
    _log.info("[%s] %s", tag, msg)


def _bus_read_latest(cat: str) -> Optional[dict]:
    """读取 bus 某分类最新一条消息，直接使用 Blackboard API。"""
    try:
        from bus_protocol import Blackboard
        bb = Blackboard()
        facts = bb.read(cat=cat, limit=1)
        return facts[0] if facts else None
    except Exception as e:
        _log.warning("_bus_read_latest: 读取 %s 失败: %s", cat, e)
        return None


def _bus_write(cat: str, text: str, src: str = ""):
    try:
        from bus_protocol import Blackboard
        bb = Blackboard()
        bb.write(cat, text, src=src)
    except Exception:
        _log.warning("_bus_write: 写入失败 cat=%s src=%s", cat, src or 'none')


def _audit_monitor(decision: str, detail: str, src: str = ""):
    """审计日志：所有 CCS 决策写入 bus monitor_audit。"""
    _bus_write("monitor_audit", f"决策: {decision} → {detail}", src=src or "tracker")


def _run(this_role: str, bus_cat: str, timeout_sec: int,
         interval: int, partners: list[str], instance_id: int = 0):
    """轮次追踪主循环。异常不退出，记录后继续。"""
    tag = f"tracker:{this_role}[{instance_id}]"
    last_reminder = 0.0

    while True:
        try:
            time.sleep(interval)

            latest = _bus_read_latest(bus_cat)
            if not latest:
                continue

            ts = latest.ts
            age = time.time() - ts
            src = latest.src

            # 更新本方哨兵的 bus 消息年龄
            update_health(this_role, instance_id=instance_id,
                          last_bus_msg_age=age, last_turn_check=time.time())

            # 未超时 → 跳过
            if age <= timeout_sec:
                continue

            # 已超时且自己不是上一轮作者 → 可能死锁
            if src == this_role:
                continue

            now = time.time()
            if now - last_reminder < 1800:
                continue

            # 交叉检查伙伴存活（所有 instance）
            partner_alive = False
            for p in partners:
                p_alive = False
                try:
                    import subprocess
                    from tmux_ops import make_tmux_name
                    tmux_name = make_tmux_name(p, instance_id)
                    r = subprocess.run(
                        ["tmux", "has-session", "-t", tmux_name],
                        capture_output=True, timeout=5
                    )
                    p_alive = r.returncode == 0
                except Exception as e:
                    _log.warning("tmux_check: 检查伙伴 %s 失败: %s", p, e)
                if p_alive:
                    partner_alive = True

            if partner_alive:
                # 伙伴活但 bus 停 → 直接发提醒给伙伴
                _bus_write(bus_cat,
                           f"[{this_role}] 死锁检测: {bus_cat} 最后消息 {int(age)}s 前 (by {src})，请继续",
                           src=this_role)
                _audit_monitor("死锁检测",
                    f"{this_role} 检测到 {bus_cat} 超时 {int(age)}s，伙伴 {partners} 存活，已发提醒",
                    src=this_role)
                _log_info(tag, f"死锁提醒: {bus_cat} 超时 {int(age)}s，已通知 {src}")
            else:
                _audit_monitor("死锁-伙伴死亡",
                    f"{this_role} 检测到 {bus_cat} 超时 {int(age)}s，伙伴 {partners} 已死",
                    src=this_role)
                _log_info(tag, f"死锁: {bus_cat} 超时 {int(age)}s，但伙伴已死（watchdog 负责重启）")

            last_reminder = now

        except Exception as e:
            _log.warning("%s: 异常 %s，等待下一轮重试", tag, e)
            time.sleep(interval)


def start_tracker(this_role: str, bus_cat: str,
                  timeout_sec: int = 300,
                  interval: int = 10,
                  partners: Optional[list[str]] = None,
                  instance_id: int = 0) -> threading.Thread:
    """启动轮次追踪线程。daemon=False 保持存活。"""
    t = threading.Thread(
        target=_run,
        args=(this_role, bus_cat, timeout_sec, interval, partners or [], instance_id),
        daemon=False,
        name=f"tracker:{this_role}:{bus_cat}:{instance_id}",
    )
    t.start()
    return t
