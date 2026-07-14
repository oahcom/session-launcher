#!/bin/bash
# density-check.sh — 密度基线测量脚本
# 用法: bash density-check.sh <role>
# 输出: JSON { role, l1, l3, total, grade }
# 说明: 测量单个角色的 L1（项目红线）和 L3（经验教训）密度
# 验收标准: L1+L3 >= 5 良好, >= 3 及格, < 3 不合格

ROLE=$1
if [ -z "$ROLE" ]; then
    echo "{\"error\":\"用法: density-check.sh <role>\"}"
    exit 1
fi

# 角色名到工作空间目录的映射
_ROLE_WS_DIR() {
    case "$1" in
        coordinator) echo "$HOME/ccs-workspaces/ccs-coordinator";;
        *)           echo "$HOME/ccs-workspaces/$1";;
    esac
}
WS="$(_ROLE_WS_DIR "$ROLE")/CLAUDE.md"

if [ ! -f "$WS" ]; then
    echo "{\"role\":\"$ROLE\",\"l1\":0,\"l3\":0,\"total\":0,\"grade\":\"无文件\",\"error\":\"$WS 不存在\"}"
    exit 0
fi

L1=$(grep -cE '红线|禁止|绝对' "$WS" 2>/dev/null)
L3=$(grep -c 'Source:.*bus #\|参考来源：.*#reflexion' "$WS" 2>/dev/null)
TOTAL=$((L1 + L3))

if [ "$TOTAL" -ge 5 ]; then GRADE="良好"
elif [ "$TOTAL" -ge 3 ]; then GRADE="及格"
else GRADE="不合格"; fi

echo "{\"role\":\"$ROLE\",\"l1\":$L1,\"l3\":$L3,\"total\":$TOTAL,\"grade\":\"$GRADE\"}"
