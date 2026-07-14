#!/bin/bash
# purity-check.sh — 上下文纯度检测脚本
# 用法: bash purity-check.sh <role>

ROLE=$1
[ -z "$ROLE" ] && { echo '{"error":"用法: purity-check.sh <role>"}'; exit 1; }

_ROLE_WS_DIR() {
    case "$1" in
        coordinator) echo "$HOME/ccs-workspaces/ccs-coordinator";;
        *)           echo "$HOME/ccs-workspaces/$1";;
    esac
}
WS=$(_ROLE_WS_DIR "$ROLE")/CLAUDE.md
PERSONA_DIR=$HOME/hermes-session-roles/personas/session-roles
[ ! -f "$WS" ] && { echo "{\"role\":\"$ROLE\",\"grade\":\"无文件\",\"error\":\"WS not found\"}"; exit 0; }

# ── 检查 1：禁区指令（纯 Python，在枚举层做白名单）──
CHECK1=$(python3 -c "
import sys, json
sys.path.insert(0, '$HOME/session-launcher/src')
from launcher import _FORBIDDEN_MAP, _forbidden_list

role = '$ROLE'
# 本角色禁区枚举列表
my_items = set(_FORBIDDEN_MAP.get(role, []))

# 中文显示映射（_forbidden_list 输出的是中文）
DISPLAY = {
    'run_tests': '跑测试', 'edit_config': '改配置', 'deploy': '部署',
    'start_ccs': '启动 CCS', 'edit_persona_json': '改 persona JSON',
    'write_code': '写代码', 'write_other_workspace': '写其他角色 workspace',
}

# 本角色禁区中文文本
my_display = set()
my_display_text = _forbidden_list(role)
for sep in ['、', ' ', ',']:
    for item in my_display_text.split(sep):
        item = item.strip()
        if item:
            my_display.add(item.lower())

# 读取本角色 CLAUDE.md
with open('$WS') as f:
    ws_content = f.read().lower()

# 遍历其他角色禁区
found = []
for other_role, other_items in _FORBIDDEN_MAP.items():
    if other_role == role:
        continue
    for item in other_items:
        # 如果本角色也有相同的枚举项 → 共享禁区，不判污染
        if item in my_items:
            continue
        # 转中文显示后检查 CLAUDE.md
        display_text = DISPLAY.get(item, item.replace('_', ' '))
        if display_text.lower() in my_display:
            continue
        if display_text.lower() in ws_content:
            found.append(f'{other_role}:{display_text}')

if found:
    print('FAIL: ' + ', '.join(found))
else:
    print('PASS')
")

if echo "$CHECK1" | grep -q "^FAIL"; then
    FORBIDDEN_FAIL=true
else
    FORBIDDEN_FAIL=false
fi

# ── 检查 2：信号引用匹配（沿用）──
ROLE_SIGNALS=$(python3 -c "
import json, os, re
d=os.path.expanduser('$PERSONA_DIR')
for f in os.listdir(d):
    if not f.endswith('.json'): continue
    with open(os.path.join(d,f)) as fp:
        data=json.load(fp)
        if data.get('name')=='$ROLE':
            sigs=data.get('input_signals',[])
            for s in sigs:
                if s.get('type')=='bus':
                    cat=s.get('spec',{}).get('category','')
                    if cat: print(cat)
            outs=data.get('output_targets',[])
            for o in outs:
                m=re.search(r'cat=(\w+)', o)
                if m: print(m.group(1))
")
SIGNAL_FAIL=false
for cat in bug_report code_fix task_spec test_report deployment_report architecture prd system_design; do
    if grep -qi "$cat" "$WS" 2>/dev/null && ! echo "$ROLE_SIGNALS" | grep -qx "$cat"; then
        SIGNAL_FAIL=true
    fi
done

# ── 评分 ──
FAILS=0
$FORBIDDEN_FAIL && ((FAILS++))
$SIGNAL_FAIL && ((FAILS++))

[ "$FAILS" -ge 2 ] && GRADE="不合格"
[ "$FAILS" -eq 1 ] && GRADE="及格"
[ "$FAILS" -eq 0 ] && GRADE="良好"

echo "{\"role\":\"$ROLE\",\"forbidden\":$FORBIDDEN_FAIL,\"signal\":$SIGNAL_FAIL,\"fails\":$FAILS,\"grade\":\"$GRADE\"}"
