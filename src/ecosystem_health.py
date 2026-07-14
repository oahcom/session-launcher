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
    "signals": "events.signals",
    "signal_parser": "signal_parser",
    "template_registry": "template_registry",
    "workflow_client": "workflow.client",
    "partner_client": "routing.partner",
}

def _check_module_import(project_path: Path, module: str) -> CheckItem:
    start = time.time()
    python_path = f"{project_path}/src"
    actual = _IMPORT_MAP.get(module, module)
    result = subprocess.run(
        [sys.executable, "-c", f"import {actual}"],
        env={**os.environ, "PYTHONPATH": python_path},
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
         "from router import get_router; r = get_router(); print(len(r._routing))"],
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
        for pyfile in src.rglob("*.py"):
            for i, line in enumerate(pyfile.read_text().splitlines(), 1):
                if "/home/administrator" in line and "os.environ" not in line \
                   and "Path.home()" not in line and "# fallback" not in line:
                    found.append(f"{pyfile.name}:{i}")
    ok = len(found) == 0
    detail = "; ".join(found[:5]) if found else "无硬编码路径"
    return CheckItem(name="paths:no_hardcode", passed=ok,
                     detail=detail,
                     duration_ms=(time.time() - start) * 1000)


def _check_sentinels() -> CheckItem:
    """检查 CCS 会话（tmux 实时派生）。"""
    from ops.sentinel import list_sentinels
    start = time.time()
    sentinels = list_sentinels()
    return CheckItem(name="sentinels:active", passed=True,
                     detail=f"{len(sentinels)} 活跃会话",
                     duration_ms=(time.time() - start) * 1000)


def _check_workspaces() -> CheckItem:
    """检查工作空间。"""
    start = time.time()
    ws_dir = HOME / "ccs-workspaces"
    count = len([d for d in ws_dir.iterdir() if d.is_dir()]) if ws_dir.is_dir() else 0
    return CheckItem(name="workspaces:count", passed=True,
                     detail=f"{count} 工作空间",
                     duration_ms=(time.time() - start) * 1000)


# ── 主检查流程 ──────────────────────────────────────────────

def check_all() -> HealthReport:
    """执行全部健康检查，返回报告。"""
    report = HealthReport(timestamp=time.time())

    # 1. 目录存在性
    for name, path in ALL_PROJECTS.items():
        report.checks.append(_check_dir_exists(path, f"dir:{name}"))

    # 2. 模块导入（核心模块）
    core_modules = {
        "launcher": ["core", "tmux_ops", "role_manager", "codex_ops",
                     "sentinel", "signals", "signal_parser",
                     "template_registry", "workflow_client", "partner_client"],
        "roles": ["registry", "search", "models", "validate_roles"],
        "pipeline": ["router", "reliability", "config_loader",
                     "workflow_engine", "composite_runner"],
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
