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

check "系统自愈检查" python3 "$HOME/session-launcher/scripts/ecosystem_health_daemon.py" --quiet

check "Bus 读写" python3 "$HOME/.hermes/scripts/bus_client.py" stats

check "路由完整性" python3 -c "
import sys; sys.path.insert(0, '$HOME/session-pipeline/src')
from routing.router import get_router; r = get_router()
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
import json, subprocess
from pathlib import Path
# 以 persona 为准反向校验（不用「扫描目录+过滤名单」：名单是事后补的，
# 且无 persona 的废弃目录会被误报、tmp* 命名的真角色会被漏报）。
# 未激活的角色（有 persona、无 workspace）不算故障：workspace 由启动时的
# workspace_create() 自动建。但它们一旦被路由选中就会被拉起，故列为 WARN。
roles_root = Path.home() / 'hermes-session-roles' / 'personas' / 'session-roles'
ws = Path.home() / 'ccs-workspaces'
names = set()
for f in roles_root.glob('persona_*.json'):
    try:
        d = json.loads(f.read_text())
    except Exception:
        continue
    n = d.get('name')
    if n:
        names.add(n)
active = sorted(n for n in names
                if (ws / n / 'CLAUDE.md').exists() or (ws / f'ccs-{n}' / 'CLAUDE.md').exists())
idle = sorted(names - set(active))
print(f'{len(active)} 个角色 workspace OK')
if idle:
    print(f'  WARN {len(idle)} 个角色有 persona 但未激活（启动时自动建）: {idle}')
"

# 测试套件：完整输出落日志（tail -3 会把断言与堆栈截干净，排障必须重跑）；
# 退出码交给 check()；日志路径在失败时打印。
check "测试套件（launcher）" bash -c 'set -o pipefail; cd "$HOME/session-launcher" && python3 -m pytest tests/ -q --no-header -p no:cacheprovider > /tmp/health_launcher_tests.log 2>&1; _rc=$?; tail -3 /tmp/health_launcher_tests.log; [ $_rc -ne 0 ] && echo "完整日志: /tmp/health_launcher_tests.log"; exit $_rc'

check "测试套件（pipeline）" bash -c 'set -o pipefail; cd "$HOME/session-pipeline" && python3 -m pytest tests/ -q --no-header -p no:cacheprovider > /tmp/health_pipeline_tests.log 2>&1; _rc=$?; tail -3 /tmp/health_pipeline_tests.log; [ $_rc -ne 0 ] && echo "完整日志: /tmp/health_pipeline_tests.log"; exit $_rc'

check "测试套件（roles）" bash -c 'set -o pipefail; cd "$HOME/hermes-session-roles" && python3 -m pytest tests/ -q --no-header -p no:cacheprovider > /tmp/health_roles_tests.log 2>&1; _rc=$?; tail -3 /tmp/health_roles_tests.log; [ $_rc -ne 0 ] && echo "完整日志: /tmp/health_roles_tests.log"; exit $_rc'

echo ""
echo "=============================================="
echo " 结果: ✅ ${PASS} PASS  /  ❌ ${FAIL} FAIL"
echo "=============================================="

if [ "$FAIL" -gt 0 ]; then exit 1; fi
exit 0
