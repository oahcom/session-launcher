#!/usr/bin/env python3
"""
Hermes Session Ecosystem — 统一健康检查 API

提供三项目（session-launcher / hermes-session-roles / session-pipeline）
的可编程健康检查接口，替代手动逐项目检查。

用法:
    from ecosystem_health import check_all, HealthReport
    
    report = check_all()
    print(report.summary())
    print(report.json())

CLI:
    python3 src/ecosystem_health.py          # 文本输出
    python3 src/ecosystem_health.py --json   # JSON 输出
    python3 src/ecosystem_health.py --ci     # CI 模式（exit code=0/1）
"""

import ast
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


# ── 项目根路径 ──────────────────────────────────────────────
HOME = Path.home()
LAUNCHER = HOME / "session-launcher"
ROLES = HOME / "hermes-session-roles"
PIPELINE = HOME / "session-pipeline"

ALL_PROJECTS = {"launcher": LAUNCHER, "roles": ROLES, "pipeline": PIPELINE}

# 例外目录：hermes-session-roles/src/paths.py 使用 Path.home() 正确，但文档字符串中含示例路径
# 以及 .claude/worktrees/ 为 git worktrees（历史代码副本），非当前生产代码
_HARDCODE_EXCLUDE_DIRS = {"hermes-session-roles/src", ".claude/worktrees"}


# ── 检查结果模型 ─────────────────────────────────────────────

@dataclass
class CheckItem:
    """单项检查结果。"""
    name: str
    passed: bool
    detail: str = ""
    duration_ms: float = 0.0

    def to_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed,
                "detail": self.detail, "duration_ms": round(self.duration_ms, 1)}


@dataclass
class HealthReport:
    """完整健康报告。"""
    timestamp: float = 0.0
    checks: list[CheckItem] = field(default_factory=list)

    def add(self, name: str, passed: bool, detail: str = "", duration_ms: float = 0.0):
        self.checks.append(CheckItem(name=name, passed=passed,
                                      detail=detail, duration_ms=duration_ms))

    @property
    def passed_count(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    @property
    def total_count(self) -> int:
        return len(self.checks)

    @property
    def all_passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def summary(self) -> str:
        status = "✅ 健康" if self.all_passed else "❌ 异常"
        return (f"Hermes Session Ecosystem 健康检查\n"
                f"  {status} | {self.passed_count}/{self.total_count} 通过\n"
                f"  耗时: {sum(c.duration_ms for c in self.checks):.0f}ms")

    def json(self, indent: int = 2) -> str:
        return json.dumps({
            "timestamp": self.timestamp,
            "all_passed": self.all_passed,
            "summary": f"{self.passed_count}/{self.total_count}",
            "checks": [c.to_dict() for c in self.checks],
        }, ensure_ascii=False, indent=indent)

    def text_report(self) -> str:
        lines = ["=" * 50,
                 "Hermes Session Ecosystem — 健康检查",
                 f"  {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.timestamp))}",
                 "=" * 50, ""]
        for c in self.checks:
            icon = "✅" if c.passed else "❌"
            lines.append(f"  {icon} {c.name}")
            if c.detail:
                lines.append(f"     {c.detail}")
        lines.extend(["", "=" * 50,
                      f"  状态: {'健康 ✅' if self.all_passed else '异常 ❌'}",
                      f"  {self.passed_count}/{self.total_count} 通过",
                      "=" * 50])
        return "\n".join(lines)


# ── 检查函数 ────────────────────────────────────────────────

def _check_dir_exists(path: Path, name: str) -> CheckItem:
    start = time.time()
    ok = path.is_dir()
    return CheckItem(name=name, passed=ok,
                     detail=f"{path}" if not ok else "",
                     duration_ms=(time.time() - start) * 1000)


# ── 子包映射 ──
_IMPORT_MAP = {
    "sentinel": "ops.sentinel",
    # template_registry & workflow_client: 已废弃，工作流统一至 session-pipeline
    "partner_client": "routing.partner",
    "router": "routing.router",
}

def _check_module_import(project_path: Path, module: str) -> CheckItem:
    start = time.time()
    python_path = f"{project_path}/src"
    actual = _IMPORT_MAP.get(module, module)
    result = subprocess.run(
        [sys.executable, "-c", f"import {actual}"],
        env={**os.environ, "PYTHONPATH": python_path + ":" + os.environ.get("PYTHONPATH", "")},
        capture_output=True, text=True, timeout=10
    )
    ok = result.returncode == 0
    detail = result.stderr[:200] if not ok else ""
    return CheckItem(name=f"import:{module}", passed=ok,
                     detail=detail,
                     duration_ms=(time.time() - start) * 1000)


def _check_syntax(path: Path) -> CheckItem:
    """检查单个 Python 文件语法。"""
    start = time.time()
    try:
        ast.parse(path.read_text())
        return CheckItem(name=f"syntax:{path.name}", passed=True,
                         duration_ms=(time.time() - start) * 1000)
    except SyntaxError as e:
        return CheckItem(name=f"syntax:{path.name}", passed=False,
                         detail=str(e),
                         duration_ms=(time.time() - start) * 1000)


def _check_roles() -> CheckItem:
    """验证角色定义完整性。"""
    start = time.time()
    result = subprocess.run(
        [sys.executable, "src/validate_roles.py"],
        cwd=str(ROLES), capture_output=True, text=True, timeout=15
    )
    ok = result.returncode == 0
    detail = result.stdout.strip().split("\n")[-1] if ok else result.stderr[:200]
    return CheckItem(name="roles:validation", passed=ok,
                     detail=detail,
                     duration_ms=(time.time() - start) * 1000)


def _check_router() -> CheckItem:
    """验证路由表加载。"""
    start = time.time()
    result = subprocess.run(
        [sys.executable, "-c",
         "from routing.router import get_router; r = get_router(); print(len(r.routing))"],
        cwd=str(PIPELINE),
        env={**os.environ, "PYTHONPATH": f"{PIPELINE}/src"},
        capture_output=True, text=True, timeout=10
    )
    ok = result.returncode == 0
    detail = f"{result.stdout.strip()} roles" if ok else result.stderr[:200]
    return CheckItem(name="router:loaded", passed=ok,
                     detail=detail,
                     duration_ms=(time.time() - start) * 1000)


def _check_hardcoded_paths() -> CheckItem:
    """检查是否有残留的硬编码路径。"""
    start = time.time()
    found = []
    for name, proj_path in ALL_PROJECTS.items():
        src = proj_path / "src"
        if not src.is_dir():
            continue
        # 检查该目录是否在例外列表中
        try:
            rel = src.resolve().relative_to(Path.home())
            if any(str(rel).startswith(d) for d in _HARDCODE_EXCLUDE_DIRS):
                continue
        except ValueError:
            pass
        for pyfile in src.rglob("*.py"):
            for i, line in enumerate(pyfile.read_text().splitlines(), 1):
                # 排除 docstring/comment 中的示例路径（字符串字面量/注释）
                stripped = line.lstrip()
                if stripped.startswith(("#", "\"", "'", "'''", '"""')):
                    continue
                if "/home/administrator" in line and "os.environ" not in line \
                   and "Path.home()" not in line and "# fallback" not in line:
                    found.append(f"{pyfile.name}:{i}")
    ok = len(found) == 0
    detail = "; ".join(found[:5]) if found else "无硬编码路径"
    return CheckItem(name="paths:no_hardcode", passed=ok,
                     detail=detail,
                     duration_ms=(time.time() - start) * 1000)


_TMUX_PROBE_TIMEOUT = 2.0


def _tmux_alive(timeout: float = _TMUX_PROBE_TIMEOUT) -> bool:
    """tmux server 是否应答（单次、短超时）。

    WHY 单独探一次：单会话探测失败(None)按「不计失败」处理是对的（瞬时抖动），
    但 19 个会话**全部** None 就不是抖动，是 server 不响应。2026-10-22 实测
    tmux server 自 08:29 不应答、11 个 client 挂起，而 health --json 依然全绿
    （只是每个会话各花 3s 超时，整轮 57s）。这里先探一次，不响应直接红。
    """
    try:
        return subprocess.run(["tmux", "ls"], capture_output=True,
                              timeout=timeout).returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def _pane_has_claude(tmux_name: str, engine: str | None = None) -> bool | None:
    """pane 的后代进程里是否存在承载引擎进程（直拉时含 pane_pid 自身）。
    三态：True/False/None（探测失败）。

    函数名保留历史名（测试按名打桩）；扫描目标已泛化为按引擎查进程名
    （ccs→claude，claude 行为逐字同型）。engine 缺省经哨兵解析（缺省回退
    ccs）；引擎未注册 → None（判活不可用=不可判定，不计失败）。

    WHY 必须看后代而非 pane_pid：wrapper (`while true; do claude ...; done`)
    本身常驻，pane_pid 恒活——实测 closer 因 PATH 缺 .local/bin 而每 3s 报
    `claude: command not found` 达 1.4h，所有判活环节均未察觉（R41, 2026-10-19）。
    """
    try:
        from tmux_ops import _engine_of, _engine_spec
        needle = _engine_spec(engine or _engine_of(tmux_name)).process_name
    except Exception:
        return None  # 引擎未注册/解析失败：显式降级为不可判定，禁止猜测
    try:
        out = subprocess.run(
            ["tmux", "display-message", "-p", "-t", f"{tmux_name}:0.0", "#{pane_pid}"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        if not out:
            return None
        # 直接拉起（无 wrapper）时引擎进程就是 pane_pid 本身：只按 comm 精确
        # 等值认它——wrapper（bash -c "while true; do claude…"）的 cmdline 也
        # 含引擎名，子串匹配会把「wrapper 活着但引擎没起」误判为活（R41 反面）。
        try:
            if Path(f"/proc/{out}/comm").read_text().strip() == needle:
                return True
        except OSError:
            pass
        kids = subprocess.run(["pgrep", "-P", out], capture_output=True, text=True,
                              timeout=3).stdout.split()
        for k in kids:
            try:
                if needle in Path(f"/proc/{k}/cmdline").read_bytes().decode(
                        "utf8", "replace"):
                    return True
            except OSError:
                continue
        return False
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def _check_sentinels() -> CheckItem:
    """检查 CCS 会话（tmux 实时派生）。

    旧实现 passed=True 是字面量，故无论系统实际状态如何恒为绿——僵尸角色
    （pane 活、引擎进程未启动）被完全掩盖（R41, 2026-10-19）。现改为真判活：
    任一 pane 后代无引擎进程（按哨兵 engine 查表，claude 行为同型）即 fail，
    并点名到具体 session。探测失败(None)不计失败——避免瞬时 tmux 不可达造成假告警。
    """
    from ops.sentinel import list_sentinels
    start = time.time()
    try:
        sentinels = list_sentinels()
    except Exception as _e:  # noqa: BLE001 - 健康检查自身不得抛出
        return CheckItem(name="sentinels:active", passed=False,
                         detail=f"list_sentinels 失败: {_e}",
                         duration_ms=(time.time() - start) * 1000)
    if sentinels and not _tmux_alive():
        # 不响应就别逐个探（每个 3s，19 个会话要 57s），也别报绿
        return CheckItem(name="sentinels:active", passed=False,
                         detail=f"tmux 无应答（>{_TMUX_PROBE_TIMEOUT:.0f}s）："
                                f"{len(sentinels)} 个会话状态不可判定",
                         duration_ms=(time.time() - start) * 1000)
    zombies = [s.tmux_session for s in sentinels if _pane_has_claude(s.tmux_session) is False]
    ok = not zombies
    return CheckItem(name="sentinels:active", passed=ok,
                     detail=(f"{len(sentinels)} 活跃会话" if ok else
                             f"{len(zombies)}/{len(sentinels)} 僵尸(pane活但无引擎进程): "
                             + ", ".join(zombies[:5])),
                     duration_ms=(time.time() - start) * 1000)


def _check_workflow_engine() -> CheckItem:
    """引擎活着 ≠ 链在推进（coordinator.md:6 的 #428132）。

    2026-10-22 03:1x 实测：service 显示 active，锁文件里的 PID 却是另一个
    手工起在 tmux 外的 daemon，systemd 拉起的那个一直在等锁——三套巡检都报绿，
    16 个实例停在 running 无人推进。判据分三层，任一不成立即 fail：

    1. systemd 单元 active
    2. 单元 MainPID 真的持有 ~/.hermes/run/workflow-daemon.lock
    3. 有活跃实例时，最近 30 分钟必须有 workflow_logs 落库（步骤真的在动）

    锁只读探（/proc/<pid>/fd），不抢锁：daemon 取锁是 open("w") 截断 + flock 两步，
    健康检查若在中间抢到锁，daemon 的 LOCK_NB 会失败并 exit(1)（codex 审查 P3）。
    锁文件内容只作诊断，不参与判定——它是上次成功的写入，进程死后不清。
    """
    start = time.time()
    def _fail(why: str) -> CheckItem:
        return CheckItem(name="wf:engine", passed=False, detail=why,
                         duration_ms=(time.time() - start) * 1000)
    try:
        state = subprocess.run(
            ["systemctl", "--user", "is-active", "workflow-engine"],
            capture_output=True, text=True, timeout=5).stdout.strip()
        if state != "active":
            return _fail(f"workflow-engine 状态={state or 'unknown'}，链无人推进")
        main_pid = subprocess.run(
            ["systemctl", "--user", "show", "workflow-engine", "-p", "MainPID", "--value"],
            capture_output=True, text=True, timeout=5).stdout.strip()
        lock = HOME / ".hermes" / "run" / "workflow-daemon.lock"
        lock_pid = lock.read_text().strip() if lock.exists() else ""
        # 只读探：MainPID 的 fd 表里有没有这个锁文件。不 flock（见 docstring）。
        held = False
        try:
            for fd in os.listdir(f"/proc/{main_pid}/fd"):
                try:
                    if os.readlink(f"/proc/{main_pid}/fd/{fd}").endswith("workflow-daemon.lock"):
                        held = True
                        break
                except OSError:
                    continue
        except OSError:
            pass
        if not held:
            return _fail(f"单元 MainPID={main_pid} 未持有锁（锁文件里是 pid={lock_pid or '空'}）"
                         "——手工 daemon 占着锁，或进程没进主循环；链无人推进")
        import sqlite3
        db = HOME / ".hermes" / "state" / "workflows.db"
        if not db.exists():
            return _fail(f"生产库不存在: {db}")
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        try:
            active = conn.execute(
                "SELECT COUNT(*) FROM workflow_instances "
                # delivery_blocked 不是终态（client.py:17 / gatekeeper.py:401：
                # 投递达上限暂停，实例仍活）——漏掉它，全部实例卡在该状态时
                # active=0 → 新鲜度判据被跳过 → 报绿（codex 审查 P2）。
                "WHERE status IN ('pending','running','step_done_ready','delivery_blocked')"
            ).fetchone()[0]
            last = conn.execute("SELECT MAX(ts) FROM workflow_logs").fetchone()[0] or 0
        finally:
            conn.close()
        idle_min = (time.time() - last) / 60 if last else -1
        if active and idle_min > 30:
            return _fail(f"{active} 个活跃实例，但 {idle_min:.0f} 分钟无步骤日志（链停）")
        return CheckItem(name="wf:engine", passed=True,
                         detail=f"active pid={main_pid} 活跃实例={active} 最近日志={idle_min:.0f}min前",
                         duration_ms=(time.time() - start) * 1000)
    except Exception as _e:  # noqa: BLE001 - 健康检查自身不得抛出
        return _fail(f"探测失败: {_e}")


def _check_workspaces() -> CheckItem:
    """检查工作空间。"""
    start = time.time()
    ws_dir = HOME / "ccs-workspaces"
    count = len([d for d in ws_dir.iterdir() if d.is_dir()]) if ws_dir.is_dir() else 0
    # 目录不存在才是真异常；旧实现 passed=True 是字面量（R41）
    return CheckItem(name="workspaces:count", passed=ws_dir.is_dir(),
                     detail=f"{count} 工作空间",
                     duration_ms=(time.time() - start) * 1000)


# ── 主检查流程 ──────────────────────────────────────────────

def check_all() -> HealthReport:
    """执行全部健康检查，返回报告。"""
    report = HealthReport(timestamp=time.time())

    # 1. 目录存在性
    for name, path in ALL_PROJECTS.items():
        report.checks.append(_check_dir_exists(path, f"dir:{name}"))

    # 2. 模块导入（核心模块——同步实际 src 目录）
    core_modules = {
        "launcher": ["core", "codex_ops", "ccs", "ccs_socket",
                     "paths", "tmux_ops", "gateway_guard", "wf",
                     "workflow_client", "user_intent_reminder"],
        "roles": [],  # roles 无独立模块，仅 JSON 定义
        "pipeline": ["router", "reliability", "config_loader",
                     "contract_updater", "eval_checker", "eval_consistency",
                     "liveness", "output_validator", "p0_exemption",
                     "routing_daemon", "task_evidence", "template_registry",
                     "drift_detector", "paths",
                     "cron_scheduler", "conversation_monitor"],
    }
    for proj_name, modules in core_modules.items():
        proj_path = ALL_PROJECTS[proj_name]
        for mod in modules:
            report.checks.append(_check_module_import(proj_path, mod))

    # 3. 语法检查
    for proj_path in ALL_PROJECTS.values():
        src = proj_path / "src"
        if not src.is_dir():
            continue
        for pyfile in sorted(src.rglob("*.py")):
            report.checks.append(_check_syntax(pyfile))

    # 4. 角色验证
    report.checks.append(_check_roles())

    # 5. 路由表
    report.checks.append(_check_router())

    # 6. 路径硬编码检查
    report.checks.append(_check_hardcoded_paths())

    # 7. 运行状态
    report.checks.append(_check_sentinels())
    report.checks.append(_check_workspaces())
    report.checks.append(_check_workflow_engine())

    return report


# ── CLI ─────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Ecosystem Health Check")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument("--ci", action="store_true", help="CI 模式（exit code=1 当异常）")
    args = parser.parse_args()

    report = check_all()

    if args.json:
        print(report.json())
    else:
        print(report.text_report())

    if args.ci and not report.all_passed:
        sys.exit(1)


if __name__ == "__main__":
    main()
