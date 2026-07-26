#!/usr/bin/env python3
"""
watchdog.py — 伙伴存活守护线程

定期检查伙伴 CCS 的 tmux session 是否存活，
挂了则自动重启并恢复上下文（从哨兵读取伙伴信息）。
"""
__all__ = [
    'start_watchdog',
    'check_auto_continue',
    'AUTO_CONTINUE_THRESHOLD',
]

import logging
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Optional

_log = logging.getLogger("watchdog")

from ops.sentinel import (
    CcsSentinel, read_sentinel, write_sentinel, delete_sentinel,
    update_health, SENTINEL_DIR,
)
# 延迟导入，避免 watchdog → launcher 循环依赖
# 实际导入在 _restart_partner() 内部




def _is_alive(tmux_name: str) -> bool:
    try:
        r = subprocess.run(
            ["tmux", "has-session", "-t", tmux_name],
            capture_output=True, timeout=5
        )
        return r.returncode == 0
    except Exception:
        return False


def _log_info(tag: str, msg: str):
    _log.info("[%s] %s", tag, msg)


def _audit_monitor(decision: str, detail: str, src: str = ""):
    """审计日志：watchdog 决策写入 bus monitor_audit。"""
    try:
        from bus_protocol import Blackboard
        Blackboard().write("monitor_audit",
            f"决策: {decision} → {detail}", src=src or "watchdog")
    except Exception:
        pass


def _restart_partner(partner_role: str):
    """从哨兵读取伙伴上下文并重启（保留 title/partners/bus_track 等配置）。

    必须在 read_sentinel 之后才 delete_sentinel，否则上下文永远丢失。
    """
    from core import start  # 延迟导入打破循环
    old = read_sentinel(partner_role)
    # 读完旧上下文后才删除旧哨兵（防止并发重启时哨兵膨胀）
    delete_sentinel(partner_role)
    if old:
        result = start(partner_role, title=old.title,
                       partners=old.partners if old.partners else None,
                       auto_restart=True,
                       bus_track=old.bus_track,
                       bus_timeout=old.bus_timeout,
                       detach=True)
        _log_info("watchdog", f"已发起 {partner_role} 重启: {result}")
    else:
        result = start(partner_role, detach=True)
        _log_info("watchdog", f"已发起 {partner_role} 重启: {result}")


def _run(this_role: str, partner_role: str, auto_restart: bool,
         interval: int, restart_delay: int):
    """守护线程主循环。异常不退出，记录后继续。"""
    partner_tmux = f"ccs-{partner_role}"
    tag = f"watchdog:{this_role}"

    while True:
        try:
            time.sleep(interval)

            # 检查伙伴存活
            alive = _is_alive(partner_tmux)

            # 更新本方哨兵的健康状态
            update_health(this_role,
                          last_watchdog_check=time.time(),
                          watchdog_ok=alive)

            if not alive:
                _log_info(tag, f"partner {partner_role} 已死")

                if not auto_restart:
                    _audit_monitor("伙伴死亡-不重启",
                        f"{this_role} 检测到 {partner_role} 死亡，auto_restart=False 跳过")
                    _log_info(tag, "未配置 auto-restart，跳过")
                    continue

                _audit_monitor("伙伴死亡-自动重启",
                    f"{this_role} 检测到 {partner_role} 死亡，正在自动重启",
                    src=this_role)
                _log_info(tag, f"正在重启 {partner_role}...")
                time.sleep(restart_delay)
                _restart_partner(partner_role)

                # 更新本方 restart_count
                s = read_sentinel(this_role)
                if s:
                    s.health.restart_count += 1
                    write_sentinel(s)

            # 自体存活检查：本方 CCS 进程是否还在
            self_alive = _is_alive(f"ccs-{this_role}")
            if not self_alive:
                _log_info(tag, f"自身 CCS ccs-{this_role} 已死，尝试重启")
                _audit_monitor("自体死亡-自动重启",
                    f"watchdog 检测到自身 CCS ccs-{this_role} 死亡，发起重启",
                    src=this_role)
                _restart_partner(this_role)
        except Exception as e:
            _log_info(tag, f"异常: {e}，等待下一轮重试")
            time.sleep(interval)




# ── Auto-Continue 模式（omux 启发）──
# 当 CCS session 无响应超过阈值时，自动发送 continue 指令唤醒
# 而非直接重启，减少上下文丢失

AUTO_CONTINUE_THRESHOLD = 120  # 秒
_AUTO_CONTINUE_LOCK = threading.Lock()
_AUTO_CONTINUE_SENT: dict[str, float] = {}  # role -> last_sent_ts

def check_auto_continue(role: str) -> bool:
    """检查是否需要 auto-continue。返回 True 如果发送了 continue。"""
    from core import _is_alive, _tmux_send
    now = __import__("time").time()
    with _AUTO_CONTINUE_LOCK:
        last_sent = _AUTO_CONTINUE_SENT.get(role, 0)
        if now - last_sent < AUTO_CONTINUE_THRESHOLD:
            return False
    
    tmux_name = f"ccs-{role}"
    if not _is_alive(tmux_name):
        return False
    
    try:
        _tmux_send(tmux_name, "/continue")
        _AUTO_CONTINUE_SENT[role] = now
        _audit_monitor("auto-continue",
            f"{role} 无响应超过 {AUTO_CONTINUE_THRESHOLD}s，已发送 /continue",
            src=role)
        _log.info("[auto-continue:%s] 发送 /continue 唤醒", role)
        return True
    except Exception as e:
        _log.warning("[auto-continue:%s] 失败: %s", role, e)
        return False
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
