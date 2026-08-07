#!/usr/bin/env python3
"""
watchdog.py — 伙伴存活守护线程

定期检查伙伴 CCS 的 tmux session 是否存活，
挂了则自动重启并恢复上下文（从哨兵读取伙伴信息）。
"""
__all__ = [
    'start_watchdog',
    'stop_watchdog',
    'check_auto_continue',
    'AUTO_CONTINUE_THRESHOLD',
]

import logging
import os
import subprocess
import threading
import time

_log = logging.getLogger("watchdog")

from ops.sentinel import (
    read_sentinel, write_sentinel, delete_sentinel,
    update_health,
)
# 延迟导入，避免 watchdog → launcher 循环依赖
# 实际导入在 _restart_partner() 内部




def _is_alive(tmux_name: str) -> bool:
    """双检：tmux session 存在 + pane 内 claude 进程存活。

    与 tmux_ops._is_alive（单检）不同：watchdog 需判断 claude 进程是否真实存活，
    而非仅 tmux session 存在（tmux session 可在 claude 退出后仍留）。"""
    try:
        r = subprocess.run(
            ["tmux", "has-session", "-t", tmux_name],
            capture_output=True, timeout=5
        )
        if r.returncode != 0:
            return False
        # 再查 pane PID 对应的 claude 进程是否存活
        r2 = subprocess.run(
            ["tmux", "display-message", "-p", "-t", f"{tmux_name}:0.0", "#{pane_pid}"],
            capture_output=True, text=True, timeout=3
        )
        if r2.stdout.strip().isdigit():
            pane_pid = r2.stdout.strip()
            r3 = subprocess.run(
                ["pgrep", "-P", pane_pid, "-f", "claude"],
                capture_output=True, text=True, timeout=3,
            )
            if r3.stdout.strip():
                return True
            # 如果 pane_pid 本身还活着（claude 就是直接子进程）
            try:
                os.kill(int(pane_pid), 0)
                return True
            except OSError:
                return False
        return True  # 无 PID 信息时信任 tmux
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
    except Exception as e:
        _log.debug("audit_monitor bus write failed: %s", e)


def _restart_partner(partner_role: str, instance_id: int = 0):
    """从哨兵读取伙伴上下文并重启（保留 title/partners/bus_track 等配置）。"""
    from core import start  # 延迟导入打破循环
    old = read_sentinel(partner_role, instance_id)
    delete_sentinel(partner_role, instance_id)
    if old:
        result = start(partner_role, title=old.title,
                       partners=old.partners if old.partners else None,
                       auto_restart=True,
                       bus_track=old.bus_track,
                       bus_timeout=old.bus_timeout,
                       instance_id=instance_id,
                       detach=True)
        _log_info("watchdog", f"已发起 {partner_role}[{instance_id}] 重启: {result}")
    else:
        result = start(partner_role, instance_id=instance_id, detach=True)
        _log_info("watchdog", f"已发起 {partner_role}[{instance_id}] 重启: {result}")


def _run(this_role: str, partner_role: str, auto_restart: bool,
         interval: int, restart_delay: int, instance_id: int = 0,
         stop_event: threading.Event = None):
    """守护线程主循环。异常不退出，记录后继续。

    stop_event 置位时退出循环，回收线程（stop() 调用）。
    Event.wait 替代 time.sleep 保证及时响应停止请求。
    """
    tag = f"watchdog:{this_role}[{instance_id}]"

    from tmux_ops import make_tmux_name
    partner_tmux = make_tmux_name(partner_role, instance_id)
    self_tmux = make_tmux_name(this_role, instance_id)
    _restarting_self = False
    if stop_event is None:
        stop_event = threading.Event()  # 永不置位 → 保持旧行为

    while not stop_event.is_set():
        try:
            if stop_event.wait(interval):
                break

            # 检查伙伴存活
            alive = _is_alive(partner_tmux)

            # 更新本方哨兵的健康状态
            update_health(this_role, instance_id=instance_id,
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
                if stop_event.wait(restart_delay):
                    break
                _restart_partner(partner_role, instance_id=instance_id)

                # 更新本方 restart_count
                s = read_sentinel(this_role, instance_id)
                if s:
                    s.health.restart_count += 1
                    write_sentinel(s)

            # 自体存活检查：本方 CCS 进程是否还在
            if _restarting_self:
                continue  # 正在自愈中，跳过本轮
            self_alive = _is_alive(self_tmux)
            if not self_alive:
                _log_info(tag, f"自身 CCS {self_tmux} 已死，尝试重启")
                _audit_monitor("自体死亡-自动重启",
                    f"watchdog 检测到自身 CCS {self_tmux} 死亡，发起重启",
                    src=this_role)
                _restarting_self = True
                # 先移除本线程的注册表条目，防止 stop() 回收到已自愈的旧线程
                _WATCHDOG_THREADS.pop(_wd_key(this_role, instance_id), None)
                _restart_partner(this_role, instance_id=instance_id)
                _restarting_self = False
        except Exception as e:
            _log_info(tag, f"异常: {e}，等待下一轮重试")
            time.sleep(interval)




# ── Auto-Continue 模式（omux 启发）──
# 当 CCS session 无响应超过阈值时，自动发送 continue 指令唤醒
# 而非直接重启，减少上下文丢失

AUTO_CONTINUE_THRESHOLD = 120  # 秒
_AUTO_CONTINUE_LOCK = threading.Lock()
_AUTO_CONTINUE_SENT: dict[str, float] = {}  # role -> last_sent_ts

def check_auto_continue(role: str, instance_id: int = 0) -> bool:
    """检查是否需要 auto-continue。返回 True 如果发送了 continue。"""
    from core import _is_alive, _tmux_send
    from tmux_ops import make_tmux_name
    now = time.time()
    key = f"{role}[{instance_id}]"
    with _AUTO_CONTINUE_LOCK:
        last_sent = _AUTO_CONTINUE_SENT.get(key, 0)
        if now - last_sent < AUTO_CONTINUE_THRESHOLD:
            return False

    tmux_name = make_tmux_name(role, instance_id)
    if not _is_alive(tmux_name):
        return False

    try:
        _tmux_send(tmux_name, "/continue")
        _AUTO_CONTINUE_SENT[key] = now
        _audit_monitor("auto-continue",
            f"{role}[{instance_id}] 无响应超过 {AUTO_CONTINUE_THRESHOLD}s，已发送 /continue",
            src=role)
        _log.info("[auto-continue:%s] 发送 /continue 唤醒", key)
        return True
    except Exception as e:
        _log.warning("[auto-continue:%s] 失败: %s", key, e)
        return False
# 注册表：role[instance] -> [(thread, stop_event), ...]。多伙伴时同角色可有多
# 个 watchdog（每伙伴一个），stop() 时按哨兵遍历全部回收。
_WATCHDOG_THREADS: dict[str, list[tuple[threading.Thread, threading.Event]]] = {}


def _wd_key(this_role: str, instance_id: int) -> str:
    return f"{this_role}[{instance_id}]"


def start_watchdog(this_role: str, partner_role: str,
                   auto_restart: bool = False,
                   interval: int = 30,
                   restart_delay: int = 5,
                   instance_id: int = 0) -> threading.Thread:
    """启动守护线程。instance_id 决定监控哪个实例的 tmux session。"""
    stop_event = threading.Event()
    t = threading.Thread(
        target=_run,
        args=(this_role, partner_role, auto_restart, interval, restart_delay, instance_id, stop_event),
        daemon=True,
        name=f"watchdog:{this_role}:{partner_role}:{instance_id}",
    )
    t.start()
    _WATCHDOG_THREADS.setdefault(_wd_key(this_role, instance_id), []).append((t, stop_event))
    return t


def stop_watchdog(this_role: str, instance_id: int = 0) -> None:
    """停止并回收指定角色的全部 watchdog 线程（幂等）。join 限时 5s 防阻塞。"""
    entries = _WATCHDOG_THREADS.pop(_wd_key(this_role, instance_id), None)
    if not entries:
        return
    for t, stop_event in entries:
        stop_event.set()
    for t, _stop_event in entries:
        t.join(timeout=5)
