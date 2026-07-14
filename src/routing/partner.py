#!/usr/bin/env python3
"""
partner_client.py — 跨角色协作核心模块 + 自包含 CLI。

三层架构（COLLAB_ENHANCEMENT.md V1.1）：
  Layer 1: Confirm — 双信号确认交付
  Layer 2: Status — 伙伴状态解析（哨兵 + tmux + workflow DB）
  Layer 3: Wake — 唤醒 / 强制发送消息

使用方式（CLI）：
  python3 src/partner_client.py resolve <role>
  python3 src/partner_client.py wake <role> --as <actor> --context "..."
  python3 src/partner_client.py confirm <task_id> <role> --timeout 300
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

# 确保可从 session-launcher 导入模块

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_THIS_DIR)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)


from ops.sentinel import read_sentinel, SENTINEL_DIR
# core imports are lazy (inside functions) to break circular dependency



# ── lazy core import helper (break circular) ──
def _core() -> Any:
    import core as _m
    return _m

def _output(*a: Any, **kw: Any) -> None:
    return _core().output(*a, **kw)

def _check_wake(*a: Any, **kw: Any) -> Any:
    return _core().check_wake_permission(*a, **kw)

def _wake_ccs(*a: Any, **kw: Any) -> Any:
    return _core().wake_ccs(*a, **kw)

def _force_start(*a: Any, **kw: Any) -> Any:
    return _core().force_start_ccs(*a, **kw)

def _find_pid(*a: Any, **kw: Any) -> Any:
    return _core()._find_claude_pid(*a, **kw)

def _send(*a: Any, **kw: Any) -> Any:
    return _core().send(*a, **kw)

def _bus_client() -> Any:
    return _core().BUS_CLIENT

def is_ccs_running(role_name: str) -> bool:
    """检查 CCS 是否在运行（自动加 ccs- tmux 前缀）。"""
    from core import _is_alive
    return _is_alive(f"ccs-{role_name}")

from paths import WORKFLOWS_DB as WORKFLOW_DB

from paths import ensure_paths as _ensure_paths
_ensure_paths()


class PartnerClient:
    """跨角色协作核心模块。"""

    def __init__(self, role: str):
        self.role = role

    # ── Layer 1: Confirm ────────────────────────────────────

    def confirm_delivery(self, task_id: str, target_role: str,
                         timeout: int = 300) -> dict:
        """双信号等待交付确认。

        接收方接单的两种信号（任一先到即确认）：
          ① task.status != 'created'
          ② bus 通知（来自目标 role 的 start() 调用）

        超时降级阶梯：
          30s  → 首次 poll
          60s  → 检查 partner 是否 alive
          120s → 发 bus notice 提醒
          240s → dead 则自动唤醒
          300s → alive 但不接单 → 升级给人
        """
        start_ts = time.time()
        notified_at_120 = False
        woken_at_240 = False

        # 快速失败：任务不存在立即返回
        task = self._get_task(task_id)
        if task is None:
            self._write_bus("architecture",
                f"[{self.role}] 升级: task {task_id} 在 workflow DB 中不存在",
                evidence=f"confirm_delivery({task_id}, {target_role}) failed immediately")
            return {
                "confirmed": False,
                "reason": f"task {task_id} 不存在于 workflow DB",
                "task": None,
                "elapsed_sec": 0.0,
            }

        while (elapsed := time.time() - start_ts) < timeout:
            elapsed = time.time() - start_ts

            # 信号①：task.status != 'created'
            if task.get("status") != "created":
                return {
                    "confirmed": True,
                    "task": task,
                    "elapsed_sec": round(elapsed, 1),
                }

            # 信号②：bus 通知
            if self._check_bus_notification(target_role, task_id):
                return {
                    "confirmed": True,
                    "task": task,
                    "elapsed_sec": round(elapsed, 1),
                    "signal": "bus_notification",
                }

            # 降级阶梯
            if elapsed >= 240 and not woken_at_240:
                alive = is_ccs_running(target_role)
                if not alive:
                    self._write_bus("workflow",
                        f"⚠ [{self.role}] {task_id} 分配给 {target_role} "
                        f"后 {int(elapsed)}s 未接单，alive=False，已自动唤醒",
                        evidence=f"partner_status: alive=False → wake({target_role})")
                    self.wake(target_role, context=f"task {task_id} 等待处理")
                woken_at_240 = True

            elif elapsed >= 120 and not notified_at_120:
                self._write_bus("workflow",
                    f"⚠ [{self.role}] {task_id} 分配给 {target_role} 后 "
                    f"{int(elapsed)}s 未接单，alive={is_ccs_running(target_role)}",
                    evidence=f"请检查 {target_role} 状态")
                notified_at_120 = True

            elif elapsed >= 60:
                alive = is_ccs_running(target_role)
                # 不额外动作，仅在下次循环中执行降级

            time.sleep(5)

        # 超时
        task = self._get_task(task_id)
        alive = is_ccs_running(target_role)
        if alive:
            self._write_bus("architecture",
                f"[{self.role}] 升级: {target_role} 存活但不接单，"
                f"task {task_id} 超时 {timeout}s，需要人工介入")
        return {
            "confirmed": False,
            "reason": f"超时 {timeout}s: target={target_role} alive={alive}",
            "task": task,
            "elapsed_sec": round(elapsed, 1),
        }

    def _get_task(self, task_id: str) -> Optional[dict]:
        """从 workflow DB 读取 task 记录。"""
        try:
            import sqlite3
            conn = sqlite3.connect(str(WORKFLOW_DB), timeout=5)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            conn.close()
            return dict(row) if row else None
        except Exception:
            return None

    def _check_bus_notification(self, role: str, task_id: str) -> bool:
        """检查 bus 是否有来自 role 的接单通知。

        匹配 title 和 evidence 两个字段：
          title: 通知标题（可能不含 task_id，仅用于确认来源角色）
          evidence: 通知证据字段（由 start() 写入 task=task_id）
        """
        try:
            result = subprocess.run(
                ["python3", str(_bus_client()), "search",
                 f"{role} 已接单",
                 "--limit", "5", "--json"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                return False
            data = json.loads(result.stdout)
            facts = data.get("facts", data) if isinstance(data, dict) else data
            if isinstance(facts, list):
                for fact in facts:
                    # 搜索 title
                    text = fact.get("text", fact.get("content", fact.get("title", "")))
                    if task_id in text:
                        return True
                    # 搜索 evidence
                    evidence = fact.get("e", fact.get("evidence", ""))
                    if task_id in evidence:
                        return True
            return False
        except Exception:
            return False

    # ── Layer 2: Status ─────────────────────────────────────

    def resolve(self, role: str) -> dict:
        """完整的伙伴状态解析（哨兵 + tmux + workflow DB）。

        返回字段：
          role, alive, pid, lifecycle, pending_tasks,
          last_active_sec, current_task_id, bus_msg_age
        """
        sentinel = read_sentinel(role)
        alive = is_ccs_running(role)
        tmux_alive = alive

        # PID
        pid = sentinel.pid if sentinel else None
        if alive:
            tmux_name = f"ccs-{role}"
            pid = _find_pid(tmux_name) or pid

        # lifecycle
        lifecycle = sentinel.lifecycle if sentinel else "unknown"

        # pending_tasks + current_task_id
        pending_tasks = 0
        current_task_id = None
        try:
            import sqlite3
            conn = sqlite3.connect(str(WORKFLOW_DB), timeout=5)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT instance_id, task_id, status FROM workflow_instances "
                "WHERE assignee=? ORDER BY created_at DESC LIMIT 20",
                (role,)
            ).fetchall()
            conn.close()
            for r in rows:
                d = dict(r)
                if d["status"] in ("pending", "running"):
                    pending_tasks += 1
                    if current_task_id is None:
                        current_task_id = d["task_id"]
        except Exception:
            pass

        # bus_msg_age
        bus_msg_age = sentinel.health.last_bus_msg_age if sentinel and hasattr(sentinel, "health") else -1.0

        # last_active_sec: tmux last activity or uptime
        last_active_sec = -1.0
        if alive:
            try:
                output = _output(role, tail=1)
                if output:
                    last_active_sec = 0.0  # 有输出表示活跃
            except Exception:
                pass

        return {
            "role": role,
            "alive": alive,
            "pid": pid,
            "lifecycle": lifecycle,
            "pending_tasks": pending_tasks,
            "last_active_sec": last_active_sec,
            "current_task_id": current_task_id,
            "bus_msg_age": bus_msg_age,
        }

    # ── Layer 3: Wake ───────────────────────────────────────

    def check_wake_permission(self, target: str) -> bool:
        """检查本角色是否有权唤醒 target。"""
        return _check_wake(self.role, target)

    def wake(self, role: str, context: str = "",
             force: bool = False) -> dict:
        """唤醒目标角色（自动检查权限）。

        force=True 跳过权限检查（仅用于自我修复场景）。
        """
        if not force and not self.check_wake_permission(role):
            return {
                "success": False,
                "error": f"权限不足: {self.role} 无权唤醒 {role}",
            }

        return _wake_ccs(role, context=context, by_role=self.role if not force else "")

    def force_send(self, role: str, message: str,
                   auto_wake: bool = True) -> dict:
        """发送消息，必要时自动唤醒。

        auto_wake=True：如果目标角色不在线，自动唤醒后再发送。
        """
        alive = is_ccs_running(role)

        if not alive:
            if not auto_wake:
                return {"success": False, "error": f"{role} 不在线，auto_wake=False"}
            if not self.check_wake_permission(role):
                return {
                    "success": False,
                    "error": f"权限不足: {self.role} 无权唤醒 {role}",
                }
            wake_result = self.wake(role, context=message)
            if not wake_result.get("success"):
                return wake_result
            # 等待 CCS 就绪
            for _ in range(10):
                if is_ccs_running(role):
                    break
                time.sleep(1)

        return _send(role, message)

    # ── 辅助 ────────────────────────────────────────────────

    def _write_bus(self, category: str, title: str, evidence: str = ""):
        """写入 bus 消息。"""
        cmd = ["python3", str(_bus_client()), "write", category,
               title, "--src", self.role]
        if evidence:
            cmd.extend(["--evidence", evidence])
        try:
            subprocess.run(cmd, capture_output=True, timeout=15)
        except Exception:
            pass


# ════════════════════════════════════════════════════════════
# CLI 入口
# ════════════════════════════════════════════════════════════

def cli_resolve(args: Any) -> dict:
    """resolve <role> — 查询角色状态。"""
    pc = PartnerClient(args.as_role or "unknown")
    status = pc.resolve(args.role)
    print(json.dumps(status, ensure_ascii=False, indent=2))


def cli_wake(args: Any) -> dict:
    """wake <role> --as <actor> --context <...> — 唤醒角色。"""
    if not args.as_role:
        print(json.dumps({"success": False, "error": "需要 --as 参数指定调用方角色"},
                         ensure_ascii=False))
        sys.exit(1)
    pc = PartnerClient(args.as_role)
    result = pc.wake(args.role, context=args.context or "", force=args.force)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("success"):
        sys.exit(1)


def cli_confirm(args: Any) -> dict:
    """confirm <task_id> <role> [--timeout N] — 确认交付。"""
    actor = args.as_role or "unknown"
    pc = PartnerClient(actor)
    result = pc.confirm_delivery(args.task_id, args.role, timeout=args.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("confirmed"):
        sys.exit(1)


def cli_send(args: Any) -> dict:
    """send-safe <role> <message> — 安全发送消息（自动唤醒）。"""
    if not args.as_role:
        print(json.dumps({"success": False, "error": "需要 --as 参数指定调用方角色"},
                         ensure_ascii=False))
        sys.exit(1)
    pc = PartnerClient(args.as_role)
    result = pc.force_send(args.role, args.message,
                           auto_wake=not args.no_auto_wake)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("success"):
        sys.exit(1)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="PartnerClient — 跨角色协作 CLI")
    sub = parser.add_subparsers(dest="command")

    # resolve
    p_resolve = sub.add_parser("resolve", help="查询角色状态")
    p_resolve.add_argument("role", help="目标角色名")
    p_resolve.add_argument("--as", dest="as_role", default="",
                           help="调用方角色（影响权限判断）")
    p_resolve.set_defaults(func=cli_resolve)

    # wake
    p_wake = sub.add_parser("wake", help="唤醒角色")
    p_wake.add_argument("role", help="目标角色名")
    p_wake.add_argument("--as", dest="as_role", required=True,
                        help="调用方角色（必填，用于权限检查）")
    p_wake.add_argument("--context", default="", help="唤醒附带上下文")
    p_wake.add_argument("--force", action="store_true",
                        help="跳过权限检查（仅自我修复）")
    p_wake.set_defaults(func=cli_wake)

    # confirm
    p_confirm = sub.add_parser("confirm", help="确认交付（双信号等待）")
    p_confirm.add_argument("task_id", help="任务 ID")
    p_confirm.add_argument("role", help="接收方角色")
    p_confirm.add_argument("--as", dest="as_role", default="",
                           help="调用方角色")
    p_confirm.add_argument("--timeout", type=int, default=300,
                           help="超时秒数（默认 300）")
    p_confirm.set_defaults(func=cli_confirm)

    # send-safe
    p_send = sub.add_parser("send-safe", help="安全发送消息（自动唤醒）")
    p_send.add_argument("role", help="目标角色名")
    p_send.add_argument("message", help="消息内容")
    p_send.add_argument("--as", dest="as_role", required=True,
                        help="调用方角色（必填）")
    p_send.add_argument("--no-auto-wake", action="store_true",
                        help="不自动唤醒（目标离线则报错）")
    p_send.set_defaults(func=cli_send)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()


def check_send_permission(source_role: str, target_role: str) -> bool:
    """WL-P0-03: ccs send routing gate - validate inter-CCS messaging.
    Only roles with wake permission can send to each other.
    """
    from ops.sentinel import read_sentinel
    wm = {
        "*": ["coordinator", "lr"],
        "pg": ["qa", "pm", "reviewer", "product_architect"],
        "qa": ["pm", "reviewer"],
    }
    if source_role in wm:
        return target_role in wm[source_role]
    if "*" in wm:
        return source_role in wm["*"]
    return False


# Module-level aliases for test compatibility

def check_wake_permission(source_role: str, target_role: str) -> bool:
    """Module-level check: can source_role wake target_role?"""
    from role_manager import _WAKE_PERMISSION_MAP
    if source_role in _WAKE_PERMISSION_MAP:
        return target_role in _WAKE_PERMISSION_MAP[source_role]
    if "*" in _WAKE_PERMISSION_MAP:
        return source_role in _WAKE_PERMISSION_MAP["*"]
    return False

def check_send_permission(source_role: str, target_role: str) -> bool:
    """WL-P0-03: ccs send routing gate"""
    return check_wake_permission(source_role, target_role)
