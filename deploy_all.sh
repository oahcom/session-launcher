#!/usr/bin/env bash
# deploy_all.sh — 全角色一键部署 (CCS + Codex)
# 用法: bash deploy_all.sh [--force-restart]

set -euo pipefail

LAUNCHER_DIR="$(cd "$(dirname "$0")" && pwd)"
LAUNCHER="python3 src/launcher.py"

cd "$LAUNCHER_DIR"

# CCS roles (claude engine)
CCS_ROLES=(maintainer scout consumer curator developer closer archivist optimizer coordinator supervisor)

# Codex roles (codex engine)
CODEX_ROLES=(codex-dev)

echo "=== Session Launcher - 全角色部署 ==="
echo "时间: $(date)"
echo ""

echo "--- CCS Sessions (claude) ---"
for role in "${CCS_ROLES[@]}"; do
    STATUS=$($LAUNCHER status 2>/dev/null | grep "\[$role" || true)
    if [ -n "$STATUS" ]; then
        if [ "${1:-}" = "--force-restart" ]; then
            echo "⚠ $role restarting..."
            $LAUNCHER stop "$role" 2>/dev/null
            sleep 1
        else
            echo "✓ $role running"
            continue
        fi
    fi
    echo "▶ $role..."
    RESULT=$($LAUNCHER start "$role" 2>&1 || true)
    if echo "$RESULT" | grep -q "success"; then
        echo "✅ $role"
    else
        echo "○ $role no task"
    fi
done

echo ""
echo "--- Codex Sessions (codex) ---"
for role in "${CODEX_ROLES[@]}"; do
    STATUS=$($LAUNCHER cdx-status 2>/dev/null | grep "\[$role" || true)
    if [ -n "$STATUS" ]; then
        if [ "${1:-}" = "--force-restart" ]; then
            echo "⚠ $role restarting..."
            $LAUNCHER stop "$role" 2>/dev/null
            sleep 1
        else
            echo "✓ $role running"
            continue
        fi
    fi
    echo "▶ $role..."
    RESULT=$($LAUNCHER start "$role" --drive goal --no-attach 2>&1 || true)
    if echo "$RESULT" | grep -q "success"; then
        echo "✅ $role"
    else
        echo "○ $role no task"
    fi
done

echo ""
echo "=== 运行状态 ==="
echo "CCS:"
$LAUNCHER status 2>/dev/null || echo "  none"
echo "Codex:"
$LAUNCHER status 2>/dev/null | grep -i "codex\|CodeX" || echo "  none"
