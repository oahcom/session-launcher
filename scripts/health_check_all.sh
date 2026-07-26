#!/usr/bin/env bash
# =============================================================================
# Hermes Session Ecosystem — 三项目统一健康检查
# 验证 session-launcher / hermes-session-roles / session-pipeline 的完整性
# 用法: bash scripts/health_check_all.sh [--json]
# =============================================================================

set -e
REPORT=""
HAS_ERROR=0

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}✅${NC} $1"; }
fail() { echo -e "  ${RED}❌${NC} $1"; HAS_ERROR=1; }
warn() { echo -e "  ${YELLOW}⚠️${NC} $1"; }

JSON_MODE=false
if [[ "$1" == "--json" ]]; then JSON_MODE=true; fi

if $JSON_MODE; then
    ok()   { REPORT+="\"$1\": \"ok\",\n"; }
    fail() { REPORT+="\"$1\": \"fail\",\n"; HAS_ERROR=1; }
    warn() { REPORT+="\"$1\": \"warn\",\n"; }
fi

echo "=========================================="
echo " Hermes Session Ecosystem — 健康检查"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="
echo ""

# ── 1. 项目目录存在性 ──────────────────────────────────
echo "--- 1. 项目目录 ---"

LAUNCHER="/home/administrator/session-launcher"
ROLES="/home/administrator/hermes-session-roles"
PIPELINE="/home/administrator/session-pipeline"

for dir in "$LAUNCHER" "$ROLES" "$PIPELINE"; do
    if [[ -d "$dir" ]]; then
        ok "$(basename $dir) 存在"
    else
        fail "$(basename $dir) 缺失"
    fi
done

# ── 2. 核心模块可导入 ──────────────────────────────────
echo ""
echo "--- 2. Python 模块导入 ---"

check_import() {
    local project=$1 module=$2 label=$3
    if PYTHONPATH="$project/src" python3 -c "import $module" 2>/dev/null; then
        ok "$label ($module)"
    else
        fail "$label ($module)"
    fi
}

check_import "$LAUNCHER" "core" "session-launcher core"
check_import "$LAUNCHER" "tmux_ops" "session-launcher tmux_ops"
check_import "$LAUNCHER" "role_manager" "session-launcher role_manager"
check_import "$LAUNCHER" "codex_ops" "session-launcher codex_ops"
check_import "$LAUNCHER" "sentinel" "session-launcher sentinel"
check_import "$LAUNCHER" "signals" "session-launcher signals"
check_import "$LAUNCHER" "events.parser" "session-launcher signal_parser"
check_import "$LAUNCHER" "template_registry" "session-launcher template_registry"
check_import "$LAUNCHER" "workflow_client" "session-launcher workflow_client"
check_import "$LAUNCHER" "partner_client" "session-launcher partner_client"

check_import "$ROLES" "registry" "session-roles registry"
check_import "$ROLES" "search" "session-roles search"
check_import "$ROLES" "models" "session-roles models"

check_import "$PIPELINE" "router" "session-pipeline router"
check_import "$PIPELINE" "reliability" "session-pipeline reliability"
check_import "$PIPELINE" "config_loader" "session-pipeline config_loader"
check_import "$PIPELINE" "workflow_engine" "session-pipeline workflow_engine"
check_import "$PIPELINE" "composite_runner" "session-pipeline composite_runner"

# ── 3. 角色定义完整性 ──────────────────────────────────
echo ""
echo "--- 3. 角色定义 ---"

if cd "$ROLES" 2>/dev/null; then
    ROLE_OUTPUT=$(python3 src/validate_roles.py 2>&1)
    ROLE_COUNT=$(echo "$ROLE_OUTPUT" | grep "角色数:" | grep -oP '\d+')
    ERROR_COUNT=$(echo "$ROLE_OUTPUT" | grep "错误数:" | grep -oP '\d+')
    if [[ "$ROLE_COUNT" -ge 25 && "$ERROR_COUNT" == "0" ]]; then
        ok "角色验证: $ROLE_COUNT 角色, 0 错误"
    else
        fail "角色验证: $ROLE_COUNT 角色, $ERROR_COUNT 错误"
    fi
fi

# ── 4. 搜索测试 ─────────────────────────────────────────
echo ""
echo "--- 4. 语义搜索测试 ---"

if cd "$ROLES" 2>/dev/null; then
    SEARCH_OUTPUT=$(python3 tests/test_search.py 2>&1)
    if echo "$SEARCH_OUTPUT" | grep -q "15/15 通过"; then
        ok "搜索测试: 15/15 通过"
    else
        fail "搜索测试未全部通过"
    fi
fi

# ── 5. 路由表一致性 ────────────────────────────────────
echo ""
echo "--- 5. 路由表 ---"

if PIPELINE_OK=$(cd "$PIPELINE" && PYTHONPATH=src python3 -c "
from router import get_router
r = get_router()
print(f'{len(r._routing)}')
" 2>/dev/null); then
    ok "路由表: $PIPELINE_OK 角色"
else
    fail "路由表加载失败"
fi

# ── 6. 哨兵 / 工作空间 ──────────────────────────────────
echo ""
echo "--- 6. 运行状态 ---"

SENTINEL_COUNT=$(ls /tmp/ccs-sentinels/*.json 2>/dev/null | wc -l)
if [[ "$SENTINEL_COUNT" -gt 0 ]]; then
    ok "CCS 哨兵: $SENTINEL_COUNT 活跃"
else
    warn "无活跃 CCS 哨兵（正常，未启动任何 CCS）"
fi

WS_COUNT=$(ls /home/administrator/ccs-workspaces/ 2>/dev/null | wc -l)
if [[ "$WS_COUNT" -gt 0 ]]; then
    ok "工作空间: $WS_COUNT 个"
else
    warn "无工作空间"
fi

# ── 7. 语法检查 ─────────────────────────────────────────
echo ""
echo "--- 7. 全部 Python 语法 ---"

SYNTAX_OK=0
SYNTAX_FAIL=0
for f in $(find "$LAUNCHER/src" "$ROLES/src" "$PIPELINE/src" -name "*.py" -not -path "*/__pycache__/*" 2>/dev/null); do
    if python3 -c "import ast; ast.parse(open('$f').read())" 2>/dev/null; then
        SYNTAX_OK=$((SYNTAX_OK + 1))
    else
        SYNTAX_FAIL=$((SYNTAX_FAIL + 1))
        fail "语法错误: $f"
    fi
done
ok "语法检查: $SYNTAX_OK 文件正确"
if [[ "$SYNTAX_FAIL" -gt 0 ]]; then
    fail "$SYNTAX_FAIL 文件语法错误"
fi

# ── 汇总 ────────────────────────────────────────────
echo ""
echo "=========================================="
if [[ "$HAS_ERROR" -eq 0 ]]; then
    echo -e " 状态: ${GREEN}健康 ✅${NC}"
    echo " 所有检查通过"
else
    echo -e " 状态: ${RED}异常 ❌${NC}"
    echo " 存在 $HAS_ERROR 个问题"
fi
echo "=========================================="

if $JSON_MODE; then
    REPORT="${REPORT%,}"
    echo -e "{\n  \"timestamp\": \"$(date -Iseconds)\",\n  \"status\": \"$([ $HAS_ERROR -eq 0 ] && echo 'healthy' || echo 'unhealthy')\",\n  \"checks\": {\n${REPORT}\n  }\n}"
fi
