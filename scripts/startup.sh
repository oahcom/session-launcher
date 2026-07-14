#!/usr/bin/env bash
# start_daemons.sh — 重启核心守护进程
# systemd 不可用（WSL2）时用 nohup 启动
set -euo pipefail

echo "[$(date)] Starting CCS ecosystem daemons..."

# Kill existing
pkill -f "auto_route.py --daemon" 2>/dev/null || true
pkill -f "workflow_engine.py daemon" 2>/dev/null || true
sleep 1

# Start pipeline daemon
nohup python3 ~/session-pipeline/src/auto_route.py --daemon \
  > /tmp/pipeline-daemon.log 2>&1 &
echo "  pipeline-daemon PID=$!"

# Start workflow engine
nohup python3 ~/session-pipeline/src/workflow_engine.py daemon --interval 10 \
  > /tmp/workflow-engine-daemon.log 2>&1 &
echo "  workflow-engine PID=$!"

echo "Done. Daemons running in background."
