#!/usr/bin/env python3
"""
Session Launcher — 真正的 CCS 启动器、消息收发器、生命周期管理器。

核心能力：
  1. start_ccs(role_name) — 创建 FIFO + tmux，启动 CCS（claude 进程）
  2. send_to_ccs(role_name, message) — 向 FIFO 写消息，实时传递
  3. stop_ccs(role_name) — 终止 CCS 并清理
  4. ccs_status() — 列出所有运行的 CCS 状态
  5. inject_prompt_into_claudemd(role) — 注入 prompt 到 CLAUDE.md

依赖：hermes-session-roles（项目 A 的角色 JSON + 语义检索）
"""
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# 确保本项目模块可以从任意 CWD 导入
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

SESSION_ROLES_ROOT = Path("/home/administrator/hermes-session-roles")
BUS_CLIENT = Path("~/.hermes/scripts/bus_client.py").expanduser()
CLAUDE_MD = Path(os.environ.get("CLAUDE_MD_PATH",
                  "~/.claude/projects/-home-administrator/CLAUDE.md")).expanduser()
SESSION_MARKER_START = "<!-- SESSION_ROLE:START -->"
SESSION_MARKER_END = "<!-- SESSION_ROLE:END -->"

# ── CCS 管理常量 ──
CCS_TMUX_PREFIX = "ccs-"                        # tmux session 名前缀
CCS_SENTINEL_DIR = Path("/tmp/ccs-sentinels")    # 哨兵文件目录


def load_roles() -> list[dict]:
    """读取项目 A 的角色 JSON 文件。"""
    roles = []
    for f in sorted(SESSION_ROLES_ROOT.glob("personas/session-roles/persona_*.json")):
        with open(f) as fp:
            roles.append(json.load(fp))
    return roles


_ROLE_CACHE: dict[str, Optional[dict]] = {}

def load_roles() -> list[dict]:
    """读取项目 A 的角色 JSON 文件。"""
    roles = []
    for f in sorted(SESSION_ROLES_ROOT.glob("personas/session-roles/persona_*.json")):
        with open(f) as fp:
            roles.append(json.load(fp))
    return roles


def get_role(role_name: str) -> Optional[dict]:
    """按名称获取角色定义（带缓存，避免重复 I/O）。"""
    if role_name in _ROLE_CACHE:
        return _ROLE_CACHE[role_name]
    for r in load_roles():
        if r.get("name") == role_name:
            _ROLE_CACHE[role_name] = r
            return r
    _ROLE_CACHE[role_name] = None
    return None


def _invalidate_role_cache() -> None:
    """清空角色缓存（用于角色文件修改后）。"""
    _ROLE_CACHE.clear()


def check_signal(signal: dict) -> bool:
    """执行 input_signals 判断是否有任务。

    用后缀匹配替代子串 in 匹配，避免误匹配（如 curl 误匹配 _curl_health.py）。
    """
    from signals import check_signal_by_name
    source = signal.get("source", "")
    filter_str = signal.get("filter", "")

    # 按 source 开头的关键词匹配（避免子串误触发）
    if "bus_client.py" in source:
        return check_signal_by_name("bus_unread", filter_str)
    if source.startswith("systemctl") and "is-active" in source:
        return check_signal_by_name("systemctl_active", filter_str)
    if source.startswith("curl "):
        return check_signal_by_name("http_health", filter_str)
    if source.startswith("journalctl"):
        return check_signal_by_name("journalctl_errors", filter_str)
    if source.startswith("git diff"):
        return check_signal_by_name("git_staged", filter_str)
    if "meminfo" in source or source.startswith("df "):
        return check_signal_by_name("mem_disk", filter_str)
    if source.startswith("ls"):
        return check_signal_by_name("session_size", filter_str)
    if source.startswith("ps aux"):
        return check_signal_by_name("running_sessions", filter_str)
    return False


def has_work(roles: list[dict]) -> Optional[dict]:
    """检查是否有角色有工作。返回有工作且优先级最高的角色。

    集成 session-pipeline 路由：bus 有积压时按消息优先级排序角色。
    """
    try:
        _PIPELINE_SRC = Path("/home/administrator/session-pipeline/src")
        if str(_PIPELINE_SRC) not in sys.path:
            sys.path.insert(0, str(_PIPELINE_SRC))
        from router import get_router, priority
        from bus_protocol import Blackboard
        router = get_router()
        bb = Blackboard()
        facts = bb.unconsumed()
        if facts:
            urgency: dict[str, float] = {}
            for r in roles:
                name = r.get("name", "")
                try:
                    consume_cats = router.role_consume_categories(name)
                except Exception:
                    continue
                if "*" in consume_cats:
                    urgency[name] = urgency.get(name, 0) + 100
                else:
                    for cat in consume_cats:
                        p = priority(cat)
                        urgency[name] = urgency.get(name, 0) + (10 - min(p, 9))
            roles.sort(key=lambda r: -urgency.get(r.get("name", ""), 0))
    except (ImportError, Exception):
        # pipeline 不可用或 bus 连接失败 → fall through 到基本信号检查
        pass
    # ── 基本信号检查（无论 pipeline 是否可用都执行）──
    for role in roles:
        signals = role.get("input_signals", [])
        if not signals:
            continue
        for signal in signals:
            if check_signal(signal):
                return role
    return None


# ── Prompt 注入 ─────────────────────────────────────────

def inject_prompt_into_claudemd(role: dict) -> str:
    """将角色的 system_prompt 通过 marker 注入到 CLAUDE.md。"""
    prompt = role.get("system_prompt", "").format(
        persona_name=role["name"],
        persona_title=role["title"]
    )
    lifecycle = role.get("lifecycle", "infinite")
    drive = role.get("drive", "cron")
    inject_block = (
        f"{SESSION_MARKER_START}\n"
        f"# Session Role: {role['name']} ({role['title']})\n"
        f"# Lifecycle: {lifecycle} | Drive: {drive}\n"
        f"# 自动注入 — 由 session-launcher 管理\n\n"
        f"{prompt}\n\n"
        f"{SESSION_MARKER_END}\n"
    )
    if not CLAUDE_MD.exists():
        CLAUDE_MD.write_text(inject_block)
        return "created"
    content = CLAUDE_MD.read_text(encoding="utf-8")
    if SESSION_MARKER_START in content and SESSION_MARKER_END in content:
        # 用 rindex 定位最后一组标记对（避免手动添加多个 START 导致错误替换）
        start = content.rindex(SESSION_MARKER_START)
        end = content.rindex(SESSION_MARKER_END) + len(SESSION_MARKER_END)
        new_content = content[:start] + inject_block + content[end:]
    elif SESSION_MARKER_START in content:
        start = content.rindex(SESSION_MARKER_START)
        new_content = content[:start] + inject_block
    else:
        new_content = content.rstrip() + "\n\n" + inject_block
    CLAUDE_MD.write_text(new_content, encoding="utf-8")
    prompt_file = Path("/tmp/session_role_prompt.txt")
    prompt_file.write_text(prompt)
    return "injected"


def clear_injected_prompt() -> None:
    """清除 CLAUDE.md 中的 session role 注入块。"""
    if not CLAUDE_MD.exists():
        return
    content = CLAUDE_MD.read_text(encoding="utf-8")
    if SESSION_MARKER_START not in content:
        return
    start = content.index(SESSION_MARKER_START)
    if SESSION_MARKER_END in content:
        end = content.index(SESSION_MARKER_END) + len(SESSION_MARKER_END)
    else:
        end = len(content)
    new_content = content[:start] + content[end:]
    CLAUDE_MD.write_text(new_content, encoding="utf-8")


# ── 生命周期哨兵 ────────────────────────────────────────

def write_lifecycle_sentinel(role: dict) -> None:
    """为 ondemand 角色写生命周期哨兵文件。"""
    lifecycle = role.get("lifecycle", "infinite")
    max_minutes = role.get("max_minutes", 30)
    sentinel_dir = Path("/tmp/session-launcher")
    sentinel_dir.mkdir(parents=True, exist_ok=True)
    sentinel = sentinel_dir / f"{role['name']}.active"
    # 统一用 time.time()（Unix float），与 start_ccs 哨兵格式一致
    sentinel.write_text(json.dumps({
        "role": role["name"],
        "title": role["title"],
        "lifecycle": lifecycle,
        "pid": os.getpid(),
        "max_minutes": max_minutes if lifecycle == "ondemand" else None,
        "started_at": time.time(),
    }))


def check_ondemand_timeout(max_minutes: int = 30) -> list[str]:
    """检查 ondemand 角色是否超时，超时返回角色名列表。"""
    sentinel_dir = Path("/tmp/session-launcher")
    if not sentinel_dir.exists():
        return []
    from datetime import datetime, timezone, timedelta
    timed_out = []
    for f in sentinel_dir.glob("*.active"):
        try:
            data = json.loads(f.read_text())
            if data.get("lifecycle") != "ondemand":
                continue
            started_at = data.get("started_at")
            if not started_at:
                continue
            # 兼容两种格式：ISO-8601 string 或 Unix float
            if isinstance(started_at, (int, float)):
                started = datetime.fromtimestamp(started_at, tz=timezone.utc)
            else:
                started = datetime.fromisoformat(started_at)
            elapsed = datetime.now(timezone.utc) - started
            max_m = data.get("max_minutes", max_minutes)
            if elapsed > timedelta(minutes=max_m):
                timed_out.append(data["role"])
                f.unlink()
        except (json.JSONDecodeError, OSError, ValueError):
            f.unlink()
    return timed_out


def cleanup_stale_sentinels() -> list[str]:
    """清理孤儿哨兵文件。"""
    sentinel_dir = Path("/tmp/session-launcher")
    if not sentinel_dir.exists():
        return []
    from datetime import datetime, timezone, timedelta
    cleaned = []
    for f in sentinel_dir.glob("*.active"):
        try:
            data = json.loads(f.read_text())
            pid = data.get("pid")
            if pid and not Path(f"/proc/{pid}").exists():
                f.unlink()
                cleaned.append(data["role"])
                continue
            lifecycle = data.get("lifecycle")
            if lifecycle == "ondemand":
                started_at = data.get("started_at")
                if started_at:
                    if isinstance(started_at, (int, float)):
                        started = datetime.fromtimestamp(started_at, tz=timezone.utc)
                    else:
                        started = datetime.fromisoformat(started_at)
                    elapsed = datetime.now(timezone.utc) - started
                    max_m = data.get("max_minutes", 30)
                    if elapsed > timedelta(minutes=max_m):
                        f.unlink()
                        cleaned.append(f"{data['role']}(timeout)")
        except (json.JSONDecodeError, OSError, ValueError):
            f.unlink()
    return cleaned


# ═══════════════════════════════════════════════════════════
#  核心新增：CCS 生命周期管理
# ═══════════════════════════════════════════════════════════

def _build_role_prompt(role: dict) -> str:
    """构建角色 system_prompt，追加 Bus 轮询循环指令。

    不用 str.format()，因为 prompt 里含 bash 代码（{0}, $5 等），
    用简单替换避免花括号冲突。
    """
    base = (role.get("system_prompt", "")
            .replace("{persona_name}", role["name"])
            .replace("{persona_title}", role["title"]))
    # 追加 Bus 轮询循环（让 CCS 不退出，持续读 bus 消息）
    BUS_LOOP_SUFFIX = """
## 工作循环（自动执行，不要退出）

你是持久运行的 CCS（Claude Code Session），不要退出。
执行完本职工作后，进入循环等待模式：

### 每轮循环
1. python3 ~/.hermes/scripts/bus_client.py read --cat task --limit 5 --json 2>/dev/null
   → 检查是否有分配给本角色的新任务
2. python3 ~/.hermes/scripts/bus_client.py search "interjection:{name}" --limit 3 2>/dev/null
   → 检查是否有外部插入的指令（由 coordinator 或其他角色写入）
3. 如果有新指令或任务 → 先执行
4. 如果没有任何事做 → sleep 30 → 回到第 1 步

/loop
"""
    return base + BUS_LOOP_SUFFIX.format(name=role["name"])


def start_ccs(role_name: str) -> dict:
    """启动一个持久 CCS（交互式 tmux session），返回状态信息。

    claude 在交互模式下保持 stdin 打开，用 tmux send-keys 往里塞消息。
    不需要 FIFO 管道（因为 claude < fifo 读到 EOF 就退出）。

    流程：
    1. 读角色 JSON → 构建 system_prompt
    2. 启动 tmux session 运行 claude（交互模式）
    3. 通过 send-keys 注入初始 prompt
    4. 写哨兵文件
    """
    role = get_role(role_name)
    if not role:
        return {"success": False, "error": f"角色 {role_name} 不存在"}
    if is_ccs_running(role_name):
        return {"success": False, "error": f"CCS {role_name} 已在运行"}

    # 1. 构建 prompt
    prompt = _build_role_prompt(role)
    tmux_name = f"{CCS_TMUX_PREFIX}{role_name}"

    # 2. 创建哨兵目录
    CCS_SENTINEL_DIR.mkdir(parents=True, exist_ok=True)

    # 3. 启动交互式 claude（不设 -p，自然进入交互模式）
    cmd = (
        f'claude --model 9router_hermes'
        f' --dangerously-skip-permissions'
        f' --effort max'
        f' --permission-mode bypassPermissions'
    )
    tmux_cmd = [
        "tmux", "new-session", "-d", "-s", tmux_name,
        "bash", "-c", cmd
    ]
    result = subprocess.run(tmux_cmd, capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        return {"success": False, "error": f"tmux 启动失败: {result.stderr.strip()}"}

    # 4. 轮询等待 claude 就绪（最长 10 秒，有交互输出则提前返回）
    for _ in range(10):
        time.sleep(1)
        output = ccs_capture_output(role_name, tail=3)
        if "❯" in output or "bypass" in output or "max" in output:
            break

    # 5. 注入初始 prompt
    send_to_ccs(role_name, prompt)

    # 6. 写哨兵（记录 pid）
    sentinel = CCS_SENTINEL_DIR / f"{role_name}.json"
    sentinel.write_text(json.dumps({
        "role": role_name,
        "title": role.get("title", ""),
        "tmux_session": tmux_name,
        "pid": _find_claude_pid(tmux_name),
        "started_at": time.time(),
        "lifecycle": role.get("lifecycle", "infinite"),
    }))

    return {
        "success": True,
        "role": role_name,
        "tmux_session": tmux_name,
        "pid": _find_claude_pid(tmux_name),
    }


def _find_claude_pid(tmux_name: str) -> Optional[int]:
    """从 tmux pane 中找到 claude 进程 PID。"""
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", f"{tmux_name}:0.0", "#{pane_pid}"],
            capture_output=True, text=True, timeout=5
        )
        pane_pid = result.stdout.strip()
        if pane_pid.isdigit():
            return int(pane_pid)
    except Exception:
        pass
    return None


def send_to_ccs(role_name: str, message: str) -> dict:
    """向 CCS 发送消息（通过 tmux send-keys）。

    claude 在交互模式下不接受管道 stdin 连续写入（读到 EOF 就退出），
    因此使用 tmux send-keys 模拟键盘输入，claude 会一直保持在交互模式。
    """
    tmux_name = f"{CCS_TMUX_PREFIX}{role_name}"
    try:
        # 验证 tmux session 存在
        result = subprocess.run(
            ["tmux", "has-session", "-t", tmux_name],
            capture_output=True, timeout=5
        )
        if result.returncode != 0:
            return {"success": False, "error": f"CCS {role_name} tmux session 不存在"}

        # 用 -l (literal) 确保消息原样输入，不被 tmux 解释为保留键名（Enter/Space/C-c）
        send_cmd = ["tmux", "send-keys", "-l", "-t", f"{tmux_name}:0.0", message]
        subprocess.run(send_cmd, capture_output=True, timeout=5)
        return {"success": True, "sent_chars": len(message)}
    except Exception as e:
        return {"success": False, "error": f"tmux send-keys 失败: {e}"}


def stop_ccs(role_name: str) -> dict:
    """终止 CCS：kill tmux session + 清理哨兵。"""
    tmux_name = f"{CCS_TMUX_PREFIX}{role_name}"
    sentinel = CCS_SENTINEL_DIR / f"{role_name}.json"

    # 1. kill tmux session
    try:
        subprocess.run(
            ["tmux", "kill-session", "-t", tmux_name],
            capture_output=True, text=True, timeout=5
        )
    except Exception:
        pass

    # 2. 清理哨兵
    sentinel.unlink(missing_ok=True)

    return {"success": True, "role": role_name}


def is_ccs_running(role_name: str) -> bool:
    """检查 CCS 是否在运行（tmux session 存在）。"""
    tmux_name = f"{CCS_TMUX_PREFIX}{role_name}"
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", tmux_name],
            capture_output=True, timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


def ccs_status() -> list[dict]:
    """列出所有正在运行的 CCS。

    返回每个 CCS 的状态信息：
      - role: 角色名
      - title: 角色标题
      - alive: 是否存活
      - pid: claude 进程 PID
      - uptime: 运行时长（秒）
      - lifecycle: 生命周期类型
      - fifo: FIFO 路径
      - idle: 是否空闲（tmux 最后活动时间）
    """
    sentinel_dir = CCS_SENTINEL_DIR
    if not sentinel_dir.exists():
        return []

    statuses = []
    for sentinel in sorted(sentinel_dir.glob("*.json")):
        try:
            data = json.loads(sentinel.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        role = data.get("role", "?")
        alive = is_ccs_running(role)
        pid = data.get("pid")
        started_at = data.get("started_at", 0)
        # 兼容两种格式：Unix float 或 ISO-8601 string
        if isinstance(started_at, (int, float)):
            started = started_at
        elif isinstance(started_at, str):
            try:
                started = datetime.fromisoformat(started_at).timestamp()
            except (ValueError, TypeError):
                started = time.time()
        else:
            started = time.time()
        uptime = int(time.time() - started) if started else 0

        # 如果 tmux 挂了但哨兵还在 → 孤儿清理
        if not alive:
            sentinel.unlink(missing_ok=True)
            continue

        statuses.append({
            "role": role,
            "title": data.get("title", ""),
            "alive": alive,
            "pid": pid,
            "uptime_sec": uptime,
            "lifecycle": data.get("lifecycle", "infinite"),
        })
    return statuses


def ccs_capture_output(role_name: str, tail: int = 10) -> str:
    """截取 CCS tmux pane 的当前输出。"""
    tmux_name = f"{CCS_TMUX_PREFIX}{role_name}"
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", f"{tmux_name}:0.0"],
            capture_output=True, text=True, timeout=5
        )
        lines = result.stdout.strip().split("\n")
        return "\n".join(lines[-tail:])
    except Exception as e:
        return f"[错误] 无法读取: {e}"


# ═══════════════════════════════════════════════════════════
#  CLI 入口
# ═══════════════════════════════════════════════════════════

def main():
    """launcher 主入口 — 可按角色启动 CCS 或作为配置生成器。

    用法：
      python3 src/launcher.py                     # 配置生成模式（旧行为）
      python3 src/launcher.py start <role>        # 启动 CCS
      python3 src/launcher.py send <role> <消息>    # 发消息
      python3 src/launcher.py stop <role>          # 停止
      python3 src/launcher.py status               # 看所有 CCS
      python3 src/launcher.py output <role>        # 看输出
    """
    import argparse

    parser = argparse.ArgumentParser(description="Session Launcher — CCS 管理器")
    sub = parser.add_subparsers(dest="command")

    # start <role>
    p_start = sub.add_parser("start", help="启动一个持久 CCS")
    p_start.add_argument("role", help="角色名（maintainer / scout / consumer ...）")

    # send <role> <message>
    p_send = sub.add_parser("send", help="向 CCS 发送消息")
    p_send.add_argument("role", help="角色名")
    p_send.add_argument("message", help="消息内容")

    # stop <role>
    p_stop = sub.add_parser("stop", help="停止 CCS")
    p_stop.add_argument("role", help="角色名")

    # status
    sub.add_parser("status", help="列出所有运行中的 CCS")

    # output <role>
    p_out = sub.add_parser("output", help="查看 CCS 输出")
    p_out.add_argument("role", help="角色名")
    p_out.add_argument("--tail", type=int, default=10)

    # 旧模式（无参数）
    args = parser.parse_args()

    if args.command == "start":
        result = start_ccs(args.role)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("success"):
            # 也注入 CLAUDE.md
            role = get_role(args.role)
            if role:
                inject_prompt_into_claudemd(role)
                print(f"✅ Prompt 已注入 CLAUDE.md")

    elif args.command == "send":
        result = send_to_ccs(args.role, args.message)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.command == "stop":
        result = stop_ccs(args.role)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.command == "status":
        stats = ccs_status()
        if not stats:
            print("没有运行中的 CCS")
        else:
            print(f"运行中的 CCS: {len(stats)}\n")
            for s in stats:
                uptime_m = int(s["uptime_sec"] / 60)
                print(f"  [{s['role']:12}] {s['title']}  "
                      f"{'✅' if s['alive'] else '❌'}  "
                      f"运行 {uptime_m}分  pid={s['pid']}  {s['lifecycle']}")

    elif args.command == "output":
        print(ccs_capture_output(args.role, tail=args.tail))

    else:
        # 无命令 → 旧模式（配置生成，不启动）
        cleanup_stale_sentinels()
        roles = load_roles()
        if not roles:
            print("No roles loaded", file=sys.stderr)
            sys.exit(1)
        active_role = has_work(roles)
        if active_role:
            lifecycle = active_role.get("lifecycle", "infinite")
            drive = active_role.get("drive", "cron")
            cron_schedule = active_role.get("cron_schedule", "")
            inject_prompt_into_claudemd(active_role)
            print(f"# Active role: {active_role['name']}")
            prompt = (active_role.get("system_prompt", "")
                  .replace("{persona_name}", active_role["name"])
                  .replace("{persona_title}", active_role["title"]))
            if drive == "cron" and cron_schedule:
                # prompt 可能含 """，用 repr 方式避免三引号打断语法
                prompt_safe = prompt.replace('"', '\\"')
                startup_cmd = f"CronCreate(cron=\"{cron_schedule}\", prompt=\"{prompt_safe[:500]}\", recurring=true)"
            elif drive == "loop":
                startup_cmd = f"/loop \"{prompt[:200]}...\""
            else:
                startup_cmd = f"# {prompt[:200]}..."
            print(f"# Startup command: {startup_cmd}")
            print(f"export SESSION_ROLE={shlex.quote(active_role['name'])}")
            print(f"export SESSION_STARTUP={shlex.quote(startup_cmd)}")
            print(f"export SESSION_LIFECYCLE={shlex.quote(lifecycle)}")
            print(f"export SESSION_DRIVE={shlex.quote(drive)}")
            # shell 转义：用 shlex.quote 防止单引号/空格/特殊字符断裂
            env_file = Path("/tmp/session_role_env.sh")
            env_file.write_text(
                f"export SESSION_ROLE={shlex.quote(active_role['name'])}\n"
                f"export SESSION_STARTUP={shlex.quote(startup_cmd)}\n"
                f"export SESSION_LIFECYCLE={shlex.quote(lifecycle)}\n"
                f"export SESSION_DRIVE={shlex.quote(drive)}\n"
            )
        else:
            clear_injected_prompt()
            print("# No work for any session role. Exiting.")
            sys.exit(0)


if __name__ == "__main__":
    main()
