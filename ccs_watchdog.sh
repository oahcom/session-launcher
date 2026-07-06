#!/usr/bin/env bash
# Claude Code Watchdog — 零 LLM 调用，纯 bash 监控所有 claude 进程
# 检测卡住 → 发 Enter 唤醒（tmux session 内的 claude）
# 用法: bash ccs_watchdog.sh                # 前台运行
#       bash ccs_watchdog.sh --daemon       # 后台 daemon
#       bash ccs_watchdog.sh --stop         # 停止 daemon
#       bash ccs_watchdog.sh --status       # 查看状态

set -euo pipefail

MONITOR_DIR=/tmp/ccs-monitor
PIDFILE=/tmp/ccs_watchdog.pid
CHECK_INTERVAL=30
STUCK_THRESHOLD=120        # 120 秒无变化 → 发 Enter
BUS_THRESHOLD=300          # 300 秒无变化 → 写 bus 通知
BUS_CLIENT=~/.hermes/scripts/bus_client.py

mkdir -p "$MONITOR_DIR"

# ── 工具函数 ──

status_daemon() {
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        pid=$(cat "$PIDFILE")
        echo "watchdog 运行中 pid=$pid"
        # 显示监控到的 claude sessions
        echo ""
        echo "tmux 内的 claude session:"
        tmux list-sessions 2>/dev/null | while IFS=: read -r name _rest; do
            pane_output=$(tmux capture-pane -p -t "${name}:0.0" -S-3 2>/dev/null | tail -3)
            if echo "$pane_output" | grep -q "❯"; then
                echo "  [$name] 等待输入"
            fi
        done || true
        echo ""
        log_lines=$(wc -l < /tmp/ccs_watchdog.log 2>/dev/null || echo 0)
        echo "日志行数: $log_lines"
    else
        echo "watchdog 未运行"
    fi
}

stop_daemon() {
    if [ -f "$PIDFILE" ]; then
        pid=$(cat "$PIDFILE")
        kill "$pid" 2>/dev/null || true
        rm -f "$PIDFILE"
        echo "✅ watchdog 已停止 (pid $pid)"
    else
        echo "watchdog 未运行"
    fi
    exit 0
}

# ── 参数处理 ──

case "${1:-}" in
    --stop)   stop_daemon ;;
    --status) status_daemon; exit 0 ;;
    --daemon)
        if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
            echo "watchdog 已在运行 pid=$(cat "$PIDFILE")"
            exit 0
        fi
        nohup bash "$0" > /tmp/ccs_watchdog.log 2>&1 &
        pid=$!
        echo "$pid" > "$PIDFILE"
        echo "✅ watchdog 已启动 pid=$pid"
        echo "   检查间隔: ${CHECK_INTERVAL}s | 卡住阈值: ${STUCK_THRESHOLD}s | bus阈值: ${BUS_THRESHOLD}s"
        echo "   日志: tail -f /tmp/ccs_watchdog.log"
        exit 0
        ;;
esac

# ── 主循环 ──

while true; do
    now=$(date +%s)

    # ── 1. 扫描所有包含 claude 进程的 tmux session ──
    # 找所有 tmux session，检查 pane 里是否跑着 claude
    while IFS= read -r tmux_name; do
        [ -z "$tmux_name" ] && continue

        # 检查这个 pane 的 shell 子进程里是否有 claude
        pane_pid=$(tmux display-message -p -t "${tmux_name}:0.0" "#{pane_pid}" 2>/dev/null || true)
        if [ -z "$pane_pid" ] || ! [ -d "/proc/$pane_pid" ]; then
            continue
        fi

        # 递归找 claude 进程
        claude_pid=$(pgrep -P "$pane_pid" -f "claude" 2>/dev/null || true)
        if [ -z "$claude_pid" ]; then
            continue
        fi

        # 截取最后输出
        output=$(tmux capture-pane -p -t "${tmux_name}:0.0" -S-5 2>/dev/null | tail -5)
        output_hash=$(echo "$output" | md5sum | cut -c1-16)

        # 用 session 名作为 key
        key=$(echo "$tmux_name" | tr '/' '_')
        snap_file="$MONITOR_DIR/${key}.snap"
        last_hash=""
        last_ts=0
        if [ -f "$snap_file" ]; then
            last_hash=$(head -1 "$snap_file")
            last_ts=$(tail -1 "$snap_file")
        fi

        # 首次记录
        if [ -z "$last_hash" ]; then
            echo "$output_hash" > "$snap_file"
            echo "$now" >> "$snap_file"
            echo "[$(date +%H:%M:%S)] 发现 claude session: $tmux_name (pid=$claude_pid)"
            continue
        fi

        # 比较
        if [ "$output_hash" = "$last_hash" ]; then
            stuck_time=$(( now - last_ts ))
            if [ "$stuck_time" -gt "$STUCK_THRESHOLD" ]; then
                tmux send-keys -t "${tmux_name}:0.0" Enter
                echo "[$(date +%H:%M:%S)] [$tmux_name] 卡住 ${stuck_time}s → Enter 唤醒"

                if [ "$stuck_time" -gt "$BUS_THRESHOLD" ]; then
                    python3 "$BUS_CLIENT" write notice \
                        "[watchdog] $tmux_name 卡住 ${stuck_time}s, 已发送 Enter" \
                        --evidence "stuck_time=${stuck_time}" \
                        --src watchdog 2>/dev/null || true
                fi
                # 更新时间戳，避免反复发 Enter
                echo "$output_hash" > "$snap_file"
                echo "$now" >> "$snap_file"
            fi
        else
            # 输出变了 → 更新快照
            echo "$output_hash" > "$snap_file"
            echo "$now" >> "$snap_file"
        fi

    done < <(tmux list-sessions -F "#{session_name}" 2>/dev/null || true)

    # ── 2. 扫描无 tmux 的 claude 进程（终端直连）──
    # 检测不在 tmux 中的 claude 进程：只能看 /proc，无法自动唤醒（需要用户手动操作）
    # 这里只记录日志，不干预
    for claude_pid in $(pgrep -f "claude --model" 2>/dev/null || true); do
        # 检查父进程是否在 tmux 中
        ppid=$(ps -o ppid= -p "$claude_pid" 2>/dev/null | tr -d ' ')
        in_tmux=false
        if [ -n "$ppid" ]; then
            # 向上追溯 3 层看是否在 tmux
            gpid=$(ps -o ppid= -p "$ppid" 2>/dev/null | tr -d ' ')
            ggpid=$(ps -o ppid= -p "$gpid" 2>/dev/null | tr -d ' ')
            for check_pid in "$ppid" "$gpid" "$ggpid"; do
                if [ -n "$check_pid" ] && [ "$check_pid" != "1" ]; then
                    cmd=$(ps -o cmd= -p "$check_pid" 2>/dev/null || true)
                    if echo "$cmd" | grep -q tmux; then
                        in_tmux=true
                        break
                    fi
                fi
            done
        fi
        # 非 tmux 的 claude 只记录一次
        if [ "$in_tmux" = false ]; then
            snap_file="$MONITOR_DIR/nontmux_${claude_pid}.snap"
            if [ ! -f "$snap_file" ]; then
                echo "$now" > "$snap_file"
                echo "[$(date +%H:%M:%S)] 发现非 tmux claude 进程 pid=$claude_pid (不自动唤醒)"
            fi
        fi
    done

    # 清理 1 小时前的非 tmux 记录
    for stale in "$MONITOR_DIR"/nontmux_*.snap; do
        [ -f "$stale" ] || continue
        snap_ts=$(tail -1 "$stale" 2>/dev/null || echo 0)
        age=$(( now - snap_ts ))
        if [ "$age" -gt 3600 ]; then
            rm -f "$stale"
        fi
    done

    sleep "$CHECK_INTERVAL"
done
