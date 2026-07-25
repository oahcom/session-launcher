#!/usr/bin/env python3
"""
ecosystem_health.py — 生态系统健康自动验证（可重复使用）

用途: 验证 session-launcher 全套件编译、导入、DB、角色文件、测试状态
运行: python3 scripts/ecosystem_health.py
输出: .ecosystem_health.json + 控制台摘要
"""

import subprocess, sys, json, time
from pathlib import Path

# Ensure src/ is on path
SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

results = {"timestamp": time.time(), "checks": [], "passed": 0, "failed": 0}

def check(name, ok, detail=""):
    results["checks"].append({"name": name, "ok": ok, "detail": detail})
    if ok: results["passed"] += 1
    else: results["failed"] += 1
    mark = "✅" if ok else "❌"
    print(f"  {mark} {name}" + (f" — {detail}" if detail else ""))

def run(cmd, timeout=30):
    try: return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(SRC.parent))
    except subprocess.TimeoutExpired: return None
    except Exception as e: return None

# 1. PyCompile all source files
print("\n## 1. PyCompile 编译检查")
errors = []
for py in sorted(SRC.rglob("*.py")):
    r = run([sys.executable, "-m", "py_compile", str(py)])
    if r and r.returncode != 0: errors.append(py.name)
check("所有 .py 文件编译通过", len(errors) == 0, f"失败: {errors}" if errors else "")

# 2. Core module imports (from project root)
print("\n## 2. 模块导入完整性")
core_modules = ["core", "ccs", "launcher",
                "events.signals", "events.parser", "events.notify",
                "ops.workspace", "ops.sentinel", "ops.tracker", "ops.watchdog",
                "routing.gateway"]
bad = []
for mod in core_modules:
    r = run([sys.executable, "-c", f"import sys; sys.path.insert(0, '{SRC}'); import {mod}"], timeout=10)
    if not r or r.returncode != 0: bad.append(mod)
check("核心模块全部可导入", len(bad) == 0, f"失败: {bad}" if bad else "")

# 4. DB schema health
print("\n## 4. 数据库架构健康")
from paths import WORKFLOWS_DB
if WORKFLOWS_DB.exists():
    import sqlite3
    conn = sqlite3.connect(str(WORKFLOWS_DB))
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    table_names = [t[0] for t in tables]
    expected = {"workflow_templates", "workflow_instances", "tasks", "workflow_logs"}
    missing = expected - set(table_names)
    check("workflows.db 表完整", len(missing) == 0, f"缺表: {missing}" if missing else "")
    conn.close()
else:
    check("workflows.db 存在", False, f"文件不存在: {WORKFLOWS_DB}")

def _valid_json(path, key):
    try: return bool(json.loads(path.read_text()).get(key))
    except: return False

# 5. Role persona files
print("\n## 5. 角色文件健康")
from paths import SESSION_ROLES_PERSONAS
if SESSION_ROLES_PERSONAS.exists():
    personas = list(SESSION_ROLES_PERSONAS.glob("persona_*.json"))
    valid = sum(1 for p in personas if _valid_json(p, "name"))
    check(f"角色文件 {len(personas)} 个", valid > 0, f"有效/总数: {valid}/{len(personas)}")
else:
    check("角色目录存在", False, str(SESSION_ROLES_PERSONAS))

# 6. Test suite status
print("\n## 6. 测试集状态")
r = run([sys.executable, "-m", "pytest", "tests/", "--tb=no", "-q", "-p", "no:timeout"], timeout=120)
if r:
    out = r.stdout.strip()
    last = out.split("\n")[-1] if out else ""
    ok = "failed" not in last and ("passed" in last or "==" in last)
    check("测试集全部通过", ok, last[:120])
    results["test_output"] = last
else:
    check("测试集可运行", False, "超时或崩溃")

# Summary
print(f"\n## 摘要")
s = "✅ 健康" if results["failed"] == 0 else f"⚠ {results['failed']} 项异常"
print(f"  通过: {results['passed']}, 失败: {results['failed']}, 状态: {s}")
results["status"] = s

REPORT = SRC.parent / ".ecosystem_health.json"
REPORT.write_text(json.dumps(results, indent=2, ensure_ascii=False))
print(f"  报告: {REPORT}")
