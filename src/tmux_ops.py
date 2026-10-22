__all__ = [
    '_check_memory_before_launch',
    '_find_claude_pid',
    '_is_alive',
    '_tmux_send',
    '_tmux_output',
    '_role_is_busy',
    '_restart_if_stale',
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
    'CODEX_READY_INTERVAL',
    '_MEM_FREE_MIN_MB',
    '_CCS_LAUNCH_INTERVAL',
]

#!/usr/bin/env python3
"""Auto-generated: tmux_ops.py — extracted from core.py"""

import logging
import os
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
        return f"{TMUX_PREFIX}{role}-{instance_id}"
    return f"{TMUX_PREFIX}{role}"


def parse_tmux_name(tmux_name: str) -> tuple[str, int]:
    """反向解析 tmux session 名为 (role, instance_id)。

    支持格式: ccs-{role} → (role, 0)
              ccs-{role}-{id} → (role, id)
              cdx-{role} → (role, 0)   (Codex)
              cdx-{role}-{id} → (role, id)
    非 ccs-/cdx- 前缀返回 ("", 0)。
    """
    rest = ""
    for prefix in (TMUX_PREFIX, CODEX_TMUX_PREFIX):
        if tmux_name.startswith(prefix):
            rest = tmux_name[len(prefix):]
            break
    if not rest:
        return ("", 0)
    m = re.search(r'^(.+)-(\d+)$', rest)
    if m:
        return (m.group(1), int(m.group(2)))
    return (rest, 0)

CODEX_LOOP_DELAY = 60

CODEX_OUTPUT_MAX = 2000

CODEX_ERROR_MAX = 500

CODEX_SESSION_MAX = 5

CODEX_READY_INTERVAL = 0.5

_MEM_FREE_MIN_MB = 1000  # 启动新 CCS 前可用内存至少需要 1000MB
# 曾有一道「pipeline cgroup 达 memory.high 95% 就拒启」的闸门，2026-10-22 删除：
# ① 它读的 system.slice/session-pipeline.service 从来不存在（引擎在
#    user.slice/…/hermes.slice/workflow-engine.service），OSError 被 except 吞掉
#    → 这道闸门从上线起没生效过一次，只是给了「已有防护」的错觉；
# ② 把路径修对也不成立：同一个 hermes.slice，2026-10-22 一天内 sampling 到
#    84%（10:05 三次）、93~94%（09:40 十次）、95.2%（08:5x），负载一动就跨过 95% ——
#    阈值命中取决于采样时刻，不取决于有没有在抖，等于随机拒启。
# 2026-10-16「4 个 claude 卡在 Dl+」没有水位判据能抓（宿主机还有 6.1GB 可用），
# 能抓它的是 pane 空白 + 无对话增量（conversation_monitor / _looks_stuck），
# 不是 liveness（`_is_alive` 只验 tmux session 存在，超时还返回 True）。
# 下面只留宿主机 swap/avail 兜底。
_SWAP_FREE_MIN_MB = 128
_SWAP_LOW_AVAIL_MB = 1024  # 兜底判据：swap 见底且宿主机可用内存也紧才拦

_CCS_LAUNCH_INTERVAL = 8  # 连续启动 CCS 之间至少等待秒数

_last_ccs_launch_ts: float = 0.0


def _memory_shortage(role: str) -> str | None:
    """宿主机内存/swap 不足时返回拒启原因，否则 None。"""
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
        # 兜底：swap 见底只在宿主机内存也紧时才是风险（实测 2026-10-21：
        # swap_free=0/avail=2291MB 时新 claude 正常起来；当时单看 swap 的旧判据
        # 把 archivist 挡在门外、角色停摆）。
        if swap_total_mb > 0 and swap_free_mb < _SWAP_FREE_MIN_MB \
                and avail_mb < _SWAP_LOW_AVAIL_MB:
            return (f"swap 仅剩 {swap_free_mb}MB 且可用内存仅 {avail_mb}MB"
                    f"（< {_SWAP_LOW_AVAIL_MB}MB），跳过 {role} 启动：新进程会卡在内核内存回收")
        if avail_mb < _MEM_FREE_MIN_MB:
            return (f"可用内存仅 {avail_mb}MB（< {_MEM_FREE_MIN_MB}MB），"
                    f"跳过 {role} 启动")
    except Exception:
        logging.getLogger("tmux_ops").warning("内存检查失败，跳过可用内存校验")
    return None


def _check_memory_before_launch(role: str) -> str | None:
    """检查是否有足够内存启动新 CCS。内存不足返回错误描述，否则返回 None。"""
    global _last_ccs_launch_ts
    err = _memory_shortage(role)
    if err:
        return err

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
    found = _pane_children(tmux_name, process_name)
    if found:
        return int(found[0])
    return _pane_pid(tmux_name)


def _pane_pid(tmux_name: str) -> Optional[int]:
    """tmux pane 的 shell PID（无子进程时的兜底）。探测失败返回 None。"""
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", f"{tmux_name}:0.0", "#{pane_pid}"],
            capture_output=True, text=True, timeout=5
        )
        pid = r.stdout.strip()
        return int(pid) if pid.isdigit() else None
    except (OSError, subprocess.SubprocessError, ValueError):
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
    except subprocess.TimeoutExpired:
        # 超时 ≠ 死：探测失败时判死的代价是"重复拉起 + 在飞任务丢"，判活的代价只是
        # 本轮不动作。内存压力下 tmux server 常进 D 状态，5s 探测必然超时——实测
        # ccs status 报 pm ✅ 运行中，ccs health 却报 alive:false，引擎据此反复拉起
        # 并拦下投递（2026-10-22）。与 pipeflow/liveness.py「探测失败按不可用不判死」同口径。
        return True
    except Exception:
        return False


# 生成中的判据：状态栏的 "esc to interrupt"。它仅生成中显示，空闲时整段消失。
# 实测 2026-10-17：scout 生成中状态栏为 `... · esc to interrupt · ← 11 agents`，
# 同一时刻 6 个空闲会话该文案 0 匹配——该判据有效，不是恒定失效。
# 不用 spinner 行（✻ Shimmying… (40s · ↓ 1.4k tokens) / ✻ Churned for 2m）：
# 至少两种格式（"for X" 与 "(Xs · ↓ tokens)"）、持续变化，且空闲 pane 会残留。
# 429 重试期同样吞按键，而状态栏此时只有 "Retrying attempt"（无 esc 提示），
# 两个都要查，详见 README「提示词生效方式」。
_BUSY_SCAN_TAIL = 8  # 状态栏在最底部


def _role_is_busy(tmux_name: str, pane: str | None = None) -> bool:
    """检测 pane 是否处于生成中（Claude CLI 状态栏 esc-to-interrupt 提示）。

    ponytail: 天花板——依赖状态栏文案，Claude Code 改版换措辞即失配（降级为
    视为空闲 → 照发 C-u。C-u 不中断生成，故失配无害，仅是失去清行能力）。
    升级路径：claude CLI 若暴露状态接口则改用协议查询。
    """
    pane = _tmux_output(tmux_name, tail=_BUSY_SCAN_TAIL) if pane is None else pane
    # 截取移进函数：调用方可能传整屏（_deliver_once 要 60 行判 TUI 就绪），
    # 整屏里只要有角色自己打印过的 "esc to interrupt" 就会被永久判忙。
    bar = "\n".join(pane.splitlines()[-_BUSY_SCAN_TAIL:])
    return "esc to interrupt" in bar or "Retrying attempt" in bar


def _proc_start_time(pid: int) -> float:
    """进程启动时刻（epoch 秒）。/proc/<pid>/stat 第 22 字段是开机后的 tick 数。"""
    ticks = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    btime = next(int(l.split()[1]) for l in Path("/proc/stat").read_text().splitlines()
                 if l.startswith("btime"))
    return btime + ticks / (os.sysconf("SC_CLK_TCK") or 100)


_STUCK_MIN_SECS = 120  # 两次采样间隔下限
_PROC_CPU_HZ = os.sysconf("SC_CLK_TCK") or 100
_stuck_cache: dict[str, tuple[float, str, float]] = {}


def _proc_cpu_seconds(pid: int) -> float:
    """进程累计 CPU 秒数（utime+stime）。"""
    f = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(f[11]) + int(f[12])) / _PROC_CPU_HZ


def _looks_stuck(tmux_name: str, pid: int) -> bool:
    """状态栏说忙，但 pane 一字未动且 CPU 也没走 → 判定卡死。

    只看状态栏的忙判据会把卡死的 CLI 永久当成「在生成」：2026-10-21 实测
    consumer / product_architect / maintainer 挂着 esc-to-interrupt 90+ 分钟，
    45 秒只烧 1–2 秒 CPU、pane 内容完全不变，因此一直不重启、一直拿不到新 prompt。
    要求两次采样间隔 ≥ _STUCK_MIN_SECS，所以单次调用不会判卡死（首次调用只记样本）。
    """
    now = time.monotonic()
    pane = _tmux_output(tmux_name, tail=40)
    try:
        cpu = _proc_cpu_seconds(pid)
    except (OSError, ValueError, IndexError):
        return False
    prev = _stuck_cache.get(tmux_name)
    _stuck_cache[tmux_name] = (now, pane, cpu)
    if not prev:
        return False
    prev_ts, prev_pane, prev_cpu = prev
    if now - prev_ts < _STUCK_MIN_SECS or pane != prev_pane:
        return False
    return cpu - prev_cpu < 5.0


def _kill(pid: int, sig: int = 9) -> None:
    """杀进程的唯一出口。

    单独一层是为了可测：直接 os.kill 时测试只能 patch 全局 os.kill，而
    subprocess 超时清理（Popen.kill）也走它——实测三个用例因此恒录到无关
    kill（pid 每次不同），在负载高的机器上 100% 红（2026-10-22）。
    """
    os.kill(pid, sig)


def _restart_if_stale(tmux_name: str, prompt_file: Path) -> bool:
    """进程比 prompt 文件旧且角色空闲时重启它——claude 只在启动时读该文件，/clear 不重读。

    只杀 claude 不碰 wrapper：杀 wrapper 会连 tmux session 一起没。
    """
    if not _is_alive(tmux_name):
        return False
    # 采集失败按忙处理：宁可漏一次重启，也不能把生成中的进程杀到一半
    if not _tmux_output(tmux_name, tail=_BUSY_SCAN_TAIL):
        return False
    pid = _find_claude_pid(tmux_name)
    try:
        ok = bool(pid) and Path(f"/proc/{pid}/comm").read_text().strip() == "claude"
    except OSError:
        ok = False
    if not ok:
        return False
    if _role_is_busy(tmux_name) and not _looks_stuck(tmux_name, pid):
        return False
    if _proc_start_time(pid) >= prompt_file.stat().st_mtime:
        return False
    _kill(pid)
    print(f"    {tmux_name}: 已重启 pid={pid}（wrapper 3s 内重拉）")
    return True


def _composer_text(pane: str) -> str:
    """取 Claude CLI 输入框内的文本（最后两条横线之间）。

    已提交的消息会滚到横线之上、同样带 `❯` 前缀，只看最后一条 `❯` 行
    会把滞留误判为已提交——横线边界是唯一可靠的判据。看不到边界时返回
    None（与"输入框为空"的空串区分：前者是 TUI 未就绪，须继续补 Enter）。
    """
    lines = pane.splitlines()
    rules = [i for i, ln in enumerate(lines)
             if ln.startswith("─") and len(ln) > 20]
    if len(rules) < 2:
        return None
    return "\n".join(lines[rules[-2] + 1:rules[-1]])


def _submitted(tmux_name: str, message: str) -> bool:
    """输入框里是否仍显示本条消息（True=已提交，False=滞留或输入框未就绪）。

    Claude CLI 启动阶段会吞掉紧随文本之后的 Enter：实测新建 session 后 2s
    发送，文本停在输入框永不提交，角色零响应，而 send-keys 返回码为 0
    （假成功）。
    """
    raw = _tmux_output(tmux_name, tail=60)
    if raw.startswith("[错误]") or not raw.strip():
        return True  # 采集失败（含 session 不存在）：无从判断，按已提交处理，不空等
    probe = message.splitlines()[0][:24] if message.splitlines() else ""
    if not probe:
        return True
    composer = _composer_text(raw)
    if composer is None:
        return False  # TUI 尚未画出输入框 → 按未提交处理，继续补 Enter
    # 多行消息在输入框里只显示**尾部**（实测 archivist 启动消息：首行已滚出
    # 视口，首行探测恒不命中 → 滞留被当成已提交，消息躺 25 分钟未发）。
    # 首行与末行各取一个探针，任一仍在输入框内即判滞留。
    probe_tail = message.splitlines()[-1][:24]
    return probe not in composer and (not probe_tail or probe_tail not in composer)


def _ensure_submitted(tmux_name: str, message: str, window: int = 12) -> bool:
    """补 Enter 直到输入框清空。Enter 无损，滞留才是丢消息。

    窗口 12×0.6s 覆盖 CLI 启动（实测 TUI 就绪约 2s，早于此的 Enter 会被丢弃）。
    始终返回 True：此处已发出文本，返回 False 会让调用方重发、把同一内容
    再叠一遍；滞留只报错留痕。
    """
    for _ in range(window):
        # 先要求 TUI 已画出输入框边界：启动 1s 时 pane 还是空白，此时"没看到
        # 文本"等同于"没看到任何东西"，据此放行正是滞留漏判的那条路径。
        # 采集不到 pane（mock/无 session）不在此列——`_submitted` 已按已提交
        # 处理，不能让它白等满窗口。
        pane = _tmux_output(tmux_name, tail=60)
        # 空 pane **不能**当作已提交：启动 1s 时 pane 就是全空的，正是滞留
        # 发生的那种状态。只有采集本身失败（session 已不存在）才不必空等。
        if pane.startswith("[错误]"):
            return True
        if _composer_text(pane) is not None and _submitted(tmux_name, message):
            return True
        time.sleep(0.6)
        subprocess.run(
            ["tmux", "send-keys", "-t", f"{tmux_name}:0.0", "Enter"],
            capture_output=True, timeout=_send_timeout(),
        )
    logging.getLogger("tmux_ops").error("tmux 消息停留在输入框未提交: %s", tmux_name)
    return True


def _paste(tmux_name: str, message: str) -> None:
    """把整条消息作为字面文本写入输入框（每 500 字符一块）。

    -l: literal mode，防止 prompt 中出现 q/C-c/Space 等 tmux 特殊键名时
    被解释为按键指令（bus #141691, D3 P0 审查检出）。
    `--` 必须加：chunk 以 `-` 开头时（分块恰好落在 "- P0: ..." 列表行前）
    tmux 把它当 flag → rc=1 "invalid flag -" → 整个分块投递失败。
    实测 reviewer 模板行首全是 `- P0/P1`，每轮 3 连败。
    返回码非 0 抛异常，由 `_tmux_send` 的重试循环接住。
    """
    for i in range(0, len(message), 500):
        r = subprocess.run(
            ["tmux", "send-keys", "-l", "-t", f"{tmux_name}:0.0",
             "--", message[i:i + 500]],
            capture_output=True, timeout=_send_timeout(),
        )
        if r.returncode != 0:
            raise RuntimeError(f"tmux send-keys failed (rc={r.returncode})")
        time.sleep(0.05)


def _tmux_send(tmux_name: str, message: str, retries: int = 3):
    """Send message to tmux session with retry on failure.

    ponytail: 重试3次、间隔1s，覆盖 tmux session 暂时不可用的场景。

    返回 True=已投递，False=重试耗尽仍失败。**调用方必须检查返回值**：
    旧版超限后静默返回 None，core.send 照报 success=True，消息实际丢弃且
    3 连败 fallback 永不触发（实测 2026-10-16，send-keys 恒 rc=1 时仍
    返回 {'success': True, 'sent_chars': 11}）。

    忙时**直接拒投**：生成中的角色收到新消息会把它当成当前回合的输入，
    前面的推理上下文被下一任务覆盖（实测 2026-10-20 closer：9m58s 推理中途
    收到 skill_audit，立即转去执行，原审计结论丢失）。旧实现忙时只跳过 C-u
    继续 paste+Enter，误以为「消息进从队列等回合结束再读」，实际 Claude CLI
    下一轮就取走执行。拒投后由调用方把消息留在 bus，下一轮重推。
    """
    for attempt in range(retries):
        try:  # noqa: PERF203 — 逐次重试，异常即下次尝试
            outcome = _deliver_once(tmux_name, message)
            if outcome == "busy":
                # 忙不作故障：不重试（1s 内不会变闲）、不上报，由调用方下轮重推
                return "busy"
            if outcome:
                return _ensure_submitted(tmux_name, message)
        except Exception as e:
            logging.getLogger("tmux_ops").warning(
                "tmux send 失败 (attempt %d/%d) %s: %s", attempt + 1, retries, tmux_name, e)
            if attempt < retries - 1:
                time.sleep(1.0)
    logging.getLogger("tmux_ops").error("tmux send 重试耗尽，消息未投递: %s", tmux_name)
    return False


def _deliver_once(tmux_name: str, message: str):
    """单次投递：闲时清行 → 逐块写入 → Enter。

    返回 True=已提交 / False=写入失败 / "busy"=角色思考中、未碰 pane。

    忙时**不碰 pane**：生成中的角色会把新消息当成当前回合的输入，前面的
    推理被下一任务覆盖（实测 2026-10-20 closer：9m58s 推理中途收到
    skill_audit，立即转去执行，原审计结论丢失）。旧实现忙时只跳过 C-u 继续
    paste+Enter，误以为「消息进从队列等回合结束再读」，实际下一轮就取走。

    探测失败（None）视为空闲：宁可发出去，也不因探测失灵把所有消息饿死
    （参见 2026-10-17 假忙导致的全链停摆）。
    """
    # 一次 capture 同时判忙和判 TUI 就绪：两者都要看 pane，分两次取会多付
    # 一次 tmux 调用（实测单次 0.1~5s）。
    pane = _tmux_output(tmux_name, tail=max(_BUSY_SCAN_TAIL, 60))
    if _role_is_busy(tmux_name, pane) is True:
        logging.getLogger("tmux_ops").info(
            "tmux send 跳过（角色思考中）: %s", tmux_name)
        return "busy"
    # C-u 清输入行，不中断生成。不可用 C-c：它在 Claude CLI 生成中等于
    # 「中断当前推理」（transcript 留下 [Request interrupted by user]）。
    # 退 pager 不是投递的职责——角色卡在 pager 归 watchdog 管。
    #
    # 只在 TUI 已画出输入框时发：CLI 启动阶段（输入框还没画出来）发出去的
    # C-u 不会被行编辑器吃掉，0x15 会留在 tty 输入队列，变成首条 prompt 的
    # 第一个字节。实测 195 个会话首条提示 43 个以 \x15 开头（22%），55 个
    # 污染样本里 43 个正是会话第 1 条 prompt —— 角色读到的是「控制字符 +
    # 就绪横幅」。输入框不存在时也没有残留要清，跳过不丢功能。
    if _composer_text(pane) is not None:
        subprocess.run(
            ["tmux", "send-keys", "-t", f"{tmux_name}:0.0", "C-u"],
            capture_output=True, timeout=_send_timeout(),
        )
    _paste(tmux_name, message)
    r = subprocess.run(
        ["tmux", "send-keys", "-t", f"{tmux_name}:0.0", "Enter"],
        capture_output=True, timeout=_send_timeout(),
    )
    return r.returncode == 0


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

def _pane_children(tmux_name: str, process_name: str) -> list[str]:
    """pane PID 下精确匹配子进程（pgrep -x）。探测失败返回空表。"""
    pane_pid = _pane_pid(tmux_name)
    if pane_pid is None:
        return []
    for flag in ("-x", "-f"):
        # -x 先精确匹配；codex 是 node 脚本（进程名不是 'codex'），-x 永不命中，
        # 必须回退 -f（实测 2026-10-20：node .../bin/codex exec）。
        try:
            r = subprocess.run(
                ["pgrep", "-P", str(pane_pid), flag, process_name],
                capture_output=True, text=True, timeout=3
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if r.stdout.strip():
            return r.stdout.strip().split("\n")
    return []


def _find_codex_pid(tmux_name: str) -> Optional[int]:
    """查找 Codex 子进程 PID（不 fallback 到 pane PID）。

    不返回 pane PID 作兜底：否则 _wait_codex_ready 会把未就绪的 session
    误判为就绪。
    """
    found = _pane_children(tmux_name, "codex")
    return int(found[0]) if found else None


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
