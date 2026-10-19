__all__ = [
    '_check_memory_before_launch',
    '_find_claude_pid',
    '_is_alive',
    '_tmux_send',
    '_tmux_output',
    '_role_is_busy',
    '_tmux_kill',
    '_find_codex_pid',
    '_active_codex_session_count',
    '_wait_codex_ready',
    'TMUX_PREFIX',
    'MAX_INSTANCES',
    'make_tmux_name',
    'parse_tmux_name',
    'CODEX_TMUX_PREFIX',
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

import logging
import threading
import re
import subprocess
import time
from pathlib import Path
from typing import Optional



def _send_timeout() -> float:
    """tmux 写入超时，按系统负载自适应。

    固定 3s 在高负载下被打穿：实测 load 15~17 时
    `tmux send-keys -l -t ccs-reviewer:0.0` 单次 2.3~5.0s，3 次重试全败 →
    工作流投递彻底失败（父链重投 → 排队 → auto-cancel 循环）。
    tmux 服务端在 role pane 忙着跑长工具调用时尤其慢，与消息大小无关。

    ponytail: 只读 loadavg 一阶自适应；若将来需要更精确，改用
    /proc/pressure/io 或直接测历史 P99 延迟。返回 5/10/20s。
    """
    try:
        load = float(Path("/proc/loadavg").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 5.0
    if load >= 12:
        return 20.0
    if load >= 6:
        return 10.0
    return 5.0
TMUX_PREFIX = "ccs-"

CODEX_TMUX_PREFIX = "cdx-"

# 单个角色的最大扩展实例数（用于多 instance 遍历检查）
MAX_INSTANCES = 16


def make_tmux_name(role: str, instance_id: int = 0) -> str:
    """生成 tmux session 名。

    instance_id=0 时保持旧格式 ccs-{role}（向后兼容），
    >0 时扩展为 ccs-{role}-{id} 实现角色多 session。
    """
    if instance_id:
        return f"ccs-{role}-{instance_id}"
    return f"ccs-{role}"


def parse_tmux_name(tmux_name: str) -> tuple[str, int]:
    """反向解析 tmux session 名为 (role, instance_id)。

    支持格式: ccs-{role} → (role, 0)
              ccs-{role}-{id} → (role, id)
              cdx-{role} → (role, 0)   (Codex)
              cdx-{role}-{id} → (role, id)
    非 ccs-/cdx- 前缀返回 ("", 0)。
    """
    if tmux_name.startswith("ccs-"):
        rest = tmux_name[4:]
    elif tmux_name.startswith("cdx-"):
        rest = tmux_name[4:]
    else:
        return ("", 0)
    m = re.search(r'^(.+)-(\d+)$', rest)
    if m:
        return (m.group(1), int(m.group(2)))
    return (rest, 0)

CODEX_LOOP_DELAY = 60

CODEX_OUTPUT_MAX = 2000

CODEX_ERROR_MAX = 500

CODEX_SESSION_MAX = 5

CODEX_READY_RETRIES = 15

CODEX_READY_INTERVAL = 0.5

_MEM_FREE_MIN_MB = 1000  # 启动新 CCS 前可用内存至少需要 1000MB
# swap 余量下限：MemAvailable 充足但 swap 见底时，新 claude 仍会卡在内核
# __mem_cgroup_handle_over_high（实测 2026-10-16：SwapFree=0 时 4 个 claude
# 停在 Dl+ 不可中断睡眠，pane 空白、界面不出现）。只看 MemAvailable 的
# 原判据对此完全无感——available 显示 6.1GB 却起不来角色。
_SWAP_FREE_MIN_MB = 128

_CCS_LAUNCH_INTERVAL = 8  # 连续启动 CCS 之间至少等待秒数

_launch_lock = threading.Lock()
_last_ccs_launch_ts: float = 0.0


def _check_memory_before_launch(role: str) -> str | None:
    """检查是否有足够内存启动新 CCS。内存不足返回错误描述，否则返回 None。"""
    global _last_ccs_launch_ts
    try:
        # /proc/meminfo 里 MemAvailable 排在 SwapTotal/SwapFree 之前，逐个
        # startswith 判并 break 会让 swap 读不到 → 先整体解析，再判断。
        fields = {}
        for line in Path("/proc/meminfo").read_text().split("\n"):
            parts = line.split()
            if len(parts) >= 2 and parts[0].endswith(":"):
                try:
                    fields[parts[0][:-1]] = int(parts[1])
                except ValueError:
                    continue
        avail_mb = fields.get("MemAvailable", 0) // 1024
        swap_total_mb = fields.get("SwapTotal", 0) // 1024
        swap_free_mb = fields.get("SwapFree", 0) // 1024
        # swap 见底：新进程会卡在内核 __mem_cgroup_handle_over_high（不可中断
        # 睡眠）——pane 空白、Claude 界面不出现。MemAvailable 对此无感，必须
        # 独立判。swap_total=0 表示未启用 swap，不构成拦阻理由。
        if swap_total_mb > 0 and swap_free_mb < _SWAP_FREE_MIN_MB:
            return (f"swap 仅剩 {swap_free_mb}MB（< {_SWAP_FREE_MIN_MB}MB），"
                    f"跳过 {role} 启动：新进程会卡在内核内存回收")
        if avail_mb < _MEM_FREE_MIN_MB:
            return (f"可用内存仅 {avail_mb}MB（< {_MEM_FREE_MIN_MB}MB），"
                    f"跳过 {role} 启动")
    except Exception:
        logging.getLogger("tmux_ops").warning("内存检查失败，跳过可用内存校验")

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

    使用 pgrep -x 精确匹配，避免 -f 模糊匹配可能誤匹配其他過程
    (例如 'claude' 字符串出現在命令行參數中的其他程序)。
    若精確匹配失敗，回退到 pane_pid。

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
            # 精確匹配指定进程名 (P1 fix: 移除 -f 模糊匹配回退)
            r2 = subprocess.run(
                ["pgrep", "-P", pane_pid, "-x", process_name],
                capture_output=True, text=True, timeout=3
            )
            if r2.stdout.strip():
                return int(r2.stdout.strip().split("\n")[0])
            return int(pane_pid)
    except Exception:
        pass
    return None


def _is_alive(tmux_name: str) -> bool:
    """单检：仅验证 tmux session 存在。供 core/runner/codex_ops 等快速路径使用。"""
    # 空串必须短路为 False：`tmux has-session -t ""` 在 server 存活时 rc=0，
    # 会让 ondemand 哨兵（tmux_session=""）被误报 alive（实测 2026-10-16）。
    if not tmux_name:
        return False
    try:
        r = subprocess.run(
            ["tmux", "has-session", "-t", tmux_name],
            capture_output=True, timeout=5
        )
        return r.returncode == 0
    except Exception:
        return False


# 生成中的判据：状态栏的 "esc to interrupt"。它仅生成中显示，空闲时整段消失。
# 实测 2026-10-17：scout 生成中状态栏为 `... · esc to interrupt · ← 11 agents`，
# 同一时刻 6 个空闲会话该文案 0 匹配——该判据有效，不是恒定失效。
# 不用 spinner 行（✻ Shimmying… (40s · ↓ 1.4k tokens) / ✻ Churned for 2m）：
# 至少两种格式（"for X" 与 "(Xs · ↓ tokens)"）、持续变化，且空闲 pane 会残留。
_BUSY_SCAN_TAIL = 8  # 状态栏在最底部


def _role_is_busy(tmux_name: str) -> bool:
    """检测 pane 是否处于生成中（Claude CLI 状态栏 esc-to-interrupt 提示）。

    ponytail: 天花板——依赖状态栏文案，Claude Code 改版换措辞即失配（降级为
    视为空闲 → 照发 C-u。C-u 不中断生成，故失配无害，仅是失去清行能力）。
    升级路径：claude CLI 若暴露状态接口则改用协议查询。
    """
    return "esc to interrupt" in _tmux_output(tmux_name, tail=_BUSY_SCAN_TAIL)


def _tmux_send(tmux_name: str, message: str, retries: int = 3,
               retry_delay: float = 1.0, wait_idle: int = 0):
    """Send message to tmux session with retry on failure.

    ponytail: 重试3次、间隔1s，覆盖 tmux session 暂时不可用的场景。

    返回 True=已投递，False=重试耗尽仍失败。**调用方必须检查返回值**：
    旧版超限后静默返回 None，core.send 照报 success=True，消息实际丢弃且
    3 连败 fallback 永不触发（实测 2026-10-16，send-keys 恒 rc=1 时仍
    返回 {'success': True, 'sent_chars': 11}）。

    wait_idle>0: 机制2（可选）——角色忙时最多等 wait_idle 秒，仍未闲则降级直投。
    默认 wait_idle=0: 机制1——忙时跳过 C-u 直接投递（消息进 CLI 输入队列，
    等当前回合结束被读取，不打断生成）；闲时才发 C-u 清输入行。
    """
    deadline = time.time() + max(wait_idle, 0)
    # 机制2：等到闲再投。放在 retry 循环外，避免等待消耗重试次数。
    while wait_idle and _role_is_busy(tmux_name) and time.time() < deadline:
        time.sleep(2)
    for attempt in range(retries):
        try:
            # 忙检测：角色在生成中。C-c 会中断推理（Claude Code 键位：
            # Ctrl+C=Interrupt, Ctrl+U=Delete to line start，官方 interactive-mode 文档）。
            if not _role_is_busy(tmux_name):
                # C-u 清输入行，不中断生成。不可用 C-c：它在 Claude CLI 生成中等于
                # 「中断当前推理」（实测 lr 6 分钟内被打断 7 次，transcript 留下 7 条
                # [Request interrupted by user]，且正在跑的 Bash 调用被拒）。
                # 退 pager 不是投递的职责——角色自身卡在 pager 归 watchdog 管。
                subprocess.run(
                    ["tmux", "send-keys", "-t", f"{tmux_name}:0.0", "C-u"],
                    capture_output=True, timeout=_send_timeout(),
                )
            time.sleep(0.05)
            for i in range(0, len(message), 500):
                chunk = message[i:i + 500]
                # -l: literal mode，防止 prompt 中出现 q/C-c/Space 等 tmux 特殊键名时
                # 被解释为按键指令而非文本输入（bus #141691, D3 P0 审查检出）
                # `--` 必须加：chunk 以 `-` 开头时（分块恰好落在 "- P0: ..."
                # 列表行前）tmux 把它当 flag → rc=1 "invalid flag -" →
                # 整个分块消息投递失败。实测 reviewer 模板行首全是 `- P0/P1`，
                # 每轮 3 连败，wf_22b96 的 reviewer 子链 5 个连续 failed。
                r = subprocess.run(
                    ["tmux", "send-keys", "-l", "-t", f"{tmux_name}:0.0", "--", chunk],
                    capture_output=True, timeout=_send_timeout(),
                )
                if r.returncode != 0:
                    raise RuntimeError(f"tmux send-keys failed (rc={r.returncode})")
                time.sleep(0.05)
            r = subprocess.run(
                ["tmux", "send-keys", "-t", f"{tmux_name}:0.0", "Enter"],
                capture_output=True, timeout=_send_timeout(),
            )
            if r.returncode == 0:
                return True
        except Exception as e:
            logging.getLogger("tmux_ops").warning(
                "tmux send 失败 (attempt %d/%d) %s: %s", attempt + 1, retries, tmux_name, e)
            if attempt < retries - 1:
                time.sleep(retry_delay)
    logging.getLogger("tmux_ops").error("tmux send 重试耗尽，消息未投递: %s", tmux_name)
    return False


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
    except Exception as e:
        logging.getLogger("tmux_ops").warning("_find_codex_pid(%s): %s", tmux_name, e)
    return None


def _active_codex_session_count() -> int:
    """当前运行的 Codex session 数量（tmux session 前缀 cdx- 实时统计）。"""
    try:
        r = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=5
        )
        if not r.stdout.strip():
            return 0
        return sum(1 for line in r.stdout.strip().split("\n")
                   if line.strip().startswith("cdx-"))
    except Exception:
        return 0




def _wait_codex_ready(tmux_name: str, timeout: float = 15) -> bool:
    """轮询等待 tmux session 就绪 + PID 有效，最大等待 timeout 秒。

    ponytail: 使用 deadline 而非 retry_count*interval。如需更复杂的 readiness
    (health endpoint, 日志模式匹配)，扩展 check 函数。"""
    deadline = time.time() + max(timeout, 1.0)
    while time.time() < deadline:
        if _is_alive(tmux_name):
            pid = _find_codex_pid(tmux_name)
            if pid is not None:
                return True
        time.sleep(min(CODEX_READY_INTERVAL, timeout / 3))
    return False
