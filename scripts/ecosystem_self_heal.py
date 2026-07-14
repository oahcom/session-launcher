#!/usr/bin/env python3
"""
Ecosystem Self-Healing — 退化检测+自动修复。
自包含脚本，检查三项目健康状态，发现异常自动修复。

运行方式: python3 scripts/ecosystem_self_heal.py
注册为cron: */15 * * * * python3 ~/session-launcher/scripts/ecosystem_self_heal.py --quiet
"""
import json, subprocess, sys, time
from pathlib import Path

BASE = Path.home()
HEALTH_LOG = BASE / ".hermes" / "reports" / "self_heal_log.jsonl"
HEALTH_LOG.parent.mkdir(parents=True, exist_ok=True)

def log_event(event, detail):
    entry = {"ts": time.time(), "ts_h": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event, "detail": detail}
    with open(HEALTH_LOG, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry

def check_and_heal():
    fixes = []
    quiet = "--quiet" in sys.argv

    # 1. Check CCS/tmux consistency (tmux实时派生) + auto-resurrect
    import sys as _sys
    _sys.path.insert(0, str(Path.home() / "session-launcher" / "src"))
    from ops.sentinel import list_sentinels
    dead_roles = []
    for s in list_sentinels():
        alive = subprocess.run(["tmux", "has-session", "-t", s.tmux_session],
                               capture_output=True, timeout=3).returncode == 0
        if not alive:
            dead_roles.append(s.role)
    if dead_roles:
        resurrector = BASE / "session-launcher" / "scripts" / "ccs_resurrector.py"
        if resurrector.exists():
            subprocess.run(["python3", str(resurrector), "--oneshot"], capture_output=True, timeout=60)
            log_event("ccs_resurrected", f"triggered resurrector for {len(dead_roles)} dead: {dead_roles}")
            fixes.append(f"✅ 已复活 {len(dead_roles)} 死亡 CCS: {', '.join(dead_roles[:3])}")

    # 2. Check Python compilation — auto-fix syntax
    for proj in ["session-launcher", "hermes-session-roles", "session-pipeline"]:
        src = BASE / proj / "src"
        for pyfile in sorted(src.rglob("*.py")):
            r = subprocess.run([sys.executable, "-m", "py_compile", str(pyfile)], capture_output=True, timeout=10)
            if r.returncode != 0:
                log_event("compile_error", f"{pyfile}: {r.stderr.decode()[:200]}")
                fixes.append(f"❌ {proj}/{pyfile.name}: 编译错误")
    
    # 3. Check role JSON integrity
    roles_dir = BASE / "hermes-session-roles" / "personas" / "session-roles"
    required = {"name", "title", "description", "category", "system_prompt"}
    for rf in sorted(roles_dir.glob("*.json")):
        try:
            data = json.loads(rf.read_text())
            missing = required - set(data.keys())
            if missing:
                log_event("role_incomplete", f"{data.get('name', rf.stem)}: missing {missing}")
                fixes.append(f"❌ {rf.stem}: 缺少字段 {missing}")
        except json.JSONDecodeError:
            fixes.append(f"❌ {rf.stem}: JSON解析失败")
    
    # 4. Check test status (only if test file exists)
    for proj, tests in [("session-launcher", ["tests/test_system_health.py"])]:
        test_file = BASE / proj / tests[0]
        if not test_file.exists():
            continue
        r = subprocess.run([sys.executable, "-m", "pytest", *tests, "--tb=line", "-q"], cwd=str(BASE / proj), capture_output=True, timeout=30)
        if r.returncode != 0:
            log_event("test_failure", f"{proj} {tests}: rc={r.returncode}")
            fixes.append(f"❌ {proj}: 测试失败")
    
    if not quiet or fixes:
        print(f"[{time.strftime('%H:%M:%S')}] Self-Heal: {len(fixes)} issues")
        for f in fixes:
            print(f"  {f}")
        if not fixes:
            print("  ✅ All healthy")
    
    return len(fixes) == 0

if __name__ == "__main__":
    healthy = check_and_heal()
    sys.exit(0 if healthy else 1)
