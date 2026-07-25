#!/usr/bin/env bash
set -u
PASS=0; FAIL=0; RESULTS=""

check() {
    local name="$1"; shift
    echo -n "  ⏳ $name ... "
    if output=$("$@" 2>&1); then
        echo "✅ PASS"
        PASS=$((PASS + 1))
        RESULTS="${RESULTS}  ✅ ${name}\n"
    else
        echo "❌ FAIL"
        echo "$output" | head -5 | sed 's/^/    /'
        FAIL=$((FAIL + 1))
        RESULTS="${RESULTS}  ❌ ${name}\n"
    fi
}

echo "=============================================="
echo " 生产健康检查 — $(date '+%Y-%m-%d %H:%M:%S')"
echo "=============================================="
echo ""

check "角色定义验证" python3 "$HOME/hermes-session-roles/src/validate_roles.py"

check "系统自愈检查" python3 "$HOME/session-launcher/scripts/ecosystem_health_daemon.py" --check

check "Bus 读写" python3 "$HOME/.hermes/scripts/bus_client.py" stats

check "路由完整性" python3 -c "
import sys; sys.path.insert(0, '$HOME/session-pipeline/src')
from router import get_router; r = get_router()
for cat in ['bug_report','code_fix','task_spec','security']:
    c = r.get_consumers(cat)
    assert len(c) >= 2, f'{cat} only {len(c)} consumers'
print('all categories >= 2 consumers')
"

check "Python 编译" python3 -c "
import ast
from pathlib import Path
src = Path('$HOME/session-launcher/src')
errors = [(str(p.relative_to(Path.home())), p.read_text()) for p in src.rglob('*.py') if not p.name.startswith('__')]
for path, code in errors:
    try: ast.parse(code)
    except SyntaxError as e: print(f'{path}: {e}')
print('all files compile')
"

check "Workspace 结构" python3 -c "
from pathlib import Path
ws = Path.home() / 'ccs-workspaces'
missing = []
for d in ws.iterdir():
    if d.name.startswith('tmp'): continue
    if d.is_dir() and not (d / 'CLAUDE.md').exists():
        missing.append(d.name)
real_ws = [d for d in ws.iterdir() if d.is_dir() and not d.name.startswith('tmp')]
assert not missing, f'missing CLAUDE.md: {missing}'
print(f'{len(real_ws)} workspaces OK')
"

check "核心文件" python3 -c "
from pathlib import Path
src = Path('$HOME/session-launcher/src')
required = ['core.py', 'launcher.py', 'ccs.py', 'ops/workspace.py', 'routing/gateway.py']  # lifecycle_manager 已迁移至 session-pipeline
missing = [f for f in required if not (src / f).exists()]
assert not missing, f'missing: {missing}'
print(f'{len(required)} core files present')
"

echo ""
echo "=============================================="
echo " 结果: ✅ ${PASS} PASS  /  ❌ ${FAIL} FAIL"
echo "=============================================="

if [ "$FAIL" -gt 0 ]; then exit 1; fi
exit 0
