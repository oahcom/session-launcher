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

import threading
import time
from datetime import datetime
from typing import Optional

from ops.sentinel import update_health, _get_role_json




def _log(tag: str, msg: str):
    print(f"[{tag}] {datetime.now():%H:%M:%S} {msg}", flush=True)


def _bus_read_latest(cat: str) -> Optional[dict]:
    """读取 bus 某分类最新一条消息，直接使用 Blackboard API。"""
    try:
        from bus_protocol import Blackboard
        bb = Blackboard()
        facts = bb.read(cat=cat, limit=1)
        return facts[0] if facts else None
    except Exception:
        return None


def _bus_write(cat: str, text: str, src: str = ""):
    try:
        from bus_protocol import Blackboard
        bb = Blackboard()
        bb.write(cat, text, src=src)
    except Exception:
        pass


def _audit_monitor(decision: str, detail: str, src: str = ""):
    """审计日志：所有 CCS 决策写入 bus monitor_audit。"""
    _bus_write("monitor_audit", f"决策: {decision} → {detail}", src=src or "tracker")


def _get_collab_mode(role: str, partner: str) -> str:
    """从角色 JSON workgroup 读取协作模式，默认 peer-to-peer。"""
    data = _get_role_json(role)
    if data:
        wg = data.get("workgroup", [])
        for entry in wg:
            if entry.get("role") == partner:
                return entry.get("mode", "peer-to-peer")
    return "peer-to-peer"


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

            # 已超时且自己不是上一轮作者 → 可能死锁
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
                    import subprocess
                    from core import TMUX_PREFIX  # noqa: F811
                    r = subprocess.run(
                        ["tmux", "has-session", "-t", f"{TMUX_PREFIX}{p}"],
                        capture_output=True, timeout=5
                    )
                    p_alive = r.returncode == 0
                except Exception:
                    pass
                if p_alive:
                    partner_alive = True

            # 协作模式差异化处理
            mode = _get_collab_mode(this_role, p) if partners else "peer-to-peer"
            if mode == "notify-only":
                # notify-only: 跳过死锁检测，只做送达确认
                _bus_write(bus_cat,
                           f"[{this_role}] 送达确认: {bus_cat} 最后消息 {int(age)}s 前 (by {src})",
                           src=this_role)
                _log(tag, f"notify-only 送达: {bus_cat} 超时 {int(age)}s")
            elif mode == "master-slave":
                if partner_alive:
                    # master-slave: 只检测 slave 存活，不要求双向轮次
                    _log(tag, f"master-slave: {bus_cat} 超时 {int(age)}s，slave {partners} 存活，等待 slave 响应")
                else:
                    # slave 死 → 写 bus 让 master 重启
                    _bus_write(bus_cat,
                               f"[{this_role}] 死锁检测: slave {p} 已死，请 master 重启",
                               src=this_role)
                    _audit_monitor("死锁-slave死亡",
                        f"{this_role} 检测到 slave {partners} 已死，请求 master 重启",
                        src=this_role)
                    _log(tag, f"master-slave: slave {partners} 已死，请求重启")
            else:
                # peer-to-peer: 现有死锁检测逻辑（双向轮次超时检查）
                if partner_alive:
                    _bus_write(bus_cat,
                               f"[{this_role}] 死锁检测: {bus_cat} 最后消息 {int(age)}s 前 (by {src})，请继续",
                               src=this_role)
                    _audit_monitor("死锁检测",
                        f"{this_role} 检测到 {bus_cat} 超时 {int(age)}s，伙伴 {partners} 存活，已发提醒",
                        src=this_role)
                    _log(tag, f"死锁提醒: {bus_cat} 超时 {int(age)}s，已通知 {src}")
                else:
                    _audit_monitor("死锁-伙伴死亡",
                        f"{this_role} 检测到 {bus_cat} 超时 {int(age)}s，伙伴 {partners} 已死",
                        src=this_role)
                    _log(tag, f"死锁: {bus_cat} 超时 {int(age)}s，但伙伴已死（watchdog 负责重启）")

            last_reminder = now

        except Exception as e:
            _log(tag, f"异常: {e}，等待下一轮重试")
            time.sleep(interval)


def start_tracker(this_role: str, bus_cat: str,
                  timeout_sec: int = 300,
                  interval: int = 10,
                  partners: Optional[list[str]] = None) -> threading.Thread:
    """启动轮次追踪线程。daemon=False 保持存活。"""
    t = threading.Thread(
        target=_run,
        args=(this_role, bus_cat, timeout_sec, interval, partners or []),
        daemon=False,
        name=f"tracker:{this_role}:{bus_cat}",
    )
    t.start()
    return t
