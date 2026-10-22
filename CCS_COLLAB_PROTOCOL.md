# CCS 协作协议 (CCS Collaboration Protocol)

> 规范 CCS 之间的跨 session 协作模式，避免死锁、无人推进、存活失联。

---

## 1. 问题复现

### 辩论死锁（2026-07-06）

```
verifier → 创建 rebutter → 双方各写 1 轮 → 同时进入被动监听 → 死锁
```

| 角色 | 状态 |
|------|------|
| verifier | `watch_read` 等反方 |
| rebutter | `Monitor` 正方消息 |
| **结果** | bus 无新消息 → 双方无限等待 |

**根因时间线：**
1. verifier 写正方第3轮 → `sleep` → 读 bus → 没有新消息 → 再次 `sleep` → 循环
2. rebutter 写反方第3轮 → `sleep` → 读 bus → 没有新消息 → 改用了 `Monitor` 监听
3. 双方都在等对方 → bus 冻结

### 第二次死锁（2026-07-07）

```
rebutter 进程异常退出 → verifier 未察觉 → rebutter 重启后进入监听模式 → 再次死锁
```

| 问题 | 说明 |
|------|------|
| 创建方不检查被创建方存活 | verifier 从不执行 `tmux has-session -t ccs-rebutter` |
| 无超时自愈 | 超过30分钟无进展，无人打破僵局 |
| 重启后不回原位 | rebutter 重启后载入的是默认 prompt，不是辩论上下文 |

---

## 2. 根因清单

| # | 根因 | 说明 |
|---|------|------|
| 1 | **双方都在等对方先动** | 无"轮到谁了"的显式标记 |
| 2 | **创建方不检查被创建方存活** | `tmux has-session` 即可检测，但从未执行 |
| 3 | **无超时保护** | 超过 N 分钟无进展时无人主动打破僵局 |
| 4 | **Prompt 中"监听"和"主动"优先级模糊** | 把 `read bus` 当作唯一入口，忽略"我是辩论方，该我写了" |
| 5 | **无仲裁者** | 没有第三方监控对话是否在正常推进 |
| 6 | **重启后上下文丢失** | CCS 重启不恢复之前的协作状态（轮次、对方角色名） |

---

## 3. 协作模式

### 3.1 主从模式 (Primary-Replica)

用于"创建者 vs 被创建者"关系，如 verifier → rebutter。

```
Primary: 创建 Replica → 发第一轮 → wait → 读回复 → 检查存活 → 发下一轮
Replica: 读 bus → 回复 → sleep → 读 bus → 回复 → ...
```

**Primary 职责：**
- 每轮检查 Replica 存活：`tmux has-session -t ccs-<replica>`
- Replica 失活 → 自动重启
- 写超时检查：对方 N 分钟无回复 → 发 bus 提醒 + 检查存活

**Replica 职责：**
- 只回复不发起
- 每轮用 `bus_client.py read --cat <cat> --limit 5 --json` 读最新消息
- 回复标题含轮次号

### 3.2 对等模式 (Peer-to-Peer)

双方对等，用轮次号协调谁该写。

```
双方互信，各自独立写下一轮。
```

**协议：**
- 每条消息标题必须含 `第N轮`
- 写完后 sleep + 读 bus 看对方是否也写了
- 超时保护：N 分钟无新轮次 → 检查对方存活 + 发提醒

### 3.3 仲裁模式 (Arbitrator)

第三方 CCS（如 supervisor/ccs-monitor）负责监控。

```
Arbitrator: 扫描 bus → 检查双方存活 → 检测死锁 → 唤醒 → 或重启
```

**职责：**
- 每轮检查辩论进展：最新消息时间戳 < 5 分钟
- 超过 5 分钟 → 检查双方存活
- 双方都活 → 发 bus 提醒 `@正方法 @反方法 请继续`
- 一方死了 → 重启并重发上下文
- 跨会议死锁 → 写 bus 升级给人

---

## 4. 轮次协议

### 4.1 消息格式

```
标题: <角色>第<N>轮: <论点摘要>
来源: --src <角色名>
分类: --cat debate
证据: --evidence "上轮引用: #<对方消息ID>"
```

### 4.2 轮次追踪

```python
# 从 bus 读取最新轮次号
import json, subprocess
result = subprocess.run(
    ["python3", "~/.hermes/scripts/bus_client.py", "read", "--cat", "debate", "--limit", "1", "--json"],
    capture_output=True, text=True, timeout=10
)
facts = json.loads(result.stdout).get("facts", [])
if facts:
    import re
    match = re.search(r"第(\d+)轮", facts[0].get("title", ""))
    next_round = int(match.group(1)) + 1 if match else 1
else:
    next_round = 1
```

### 4.3 心跳检查

```bash
# 检查对方 CCS 存活
tmux has-session -t ccs-<对方角色> 2>/dev/null && echo "alive" || echo "dead"

# 检查哨兵记录的 PID 是否存活
cat ~/.hermes/run/ccs-sentinels/<对方角色>.json | python3 -c "import sys,json,os; d=json.load(sys.stdin); pid=d.get('pid'); os.kill(pid, 0) if pid else None; print('alive')" 2>/dev/null || echo "dead"
```

### 4.4 超时自愈

| 阶段 | 动作 | 时序 |
|------|------|------|
| 1 | 读 bus 最新消息 | 每轮执行 |
| 2 | 计算距离上轮时间差 | sleep 后执行 |
| 3 | 未超时 → 继续下一轮 | - |
| 4 | 超时 5 分钟 → 检查对方存活 | `tmux has-session -t ccs-<role>` |
| 5 | 对方死 → 重启 | `python3 src/ccs.py start <role> --no-attach --prompt "..."` |
| 6 | 对方活 → 发 bus 提醒 | `bus_client.py write notice "@<role> 请回复 #<上轮ID>" --src <本方> |

---

## 5. Prompt 模板规范

### 5.1 协作 CCS 的 system_prompt 必须包含

```
## 协作规范
1. 每次写新消息后 10 秒，读 bus 查对方回复
2. 每 3 轮必须检查一次对方存活:
   tmux has-session -t ccs-<对方角色>
3. 对方挂了立刻重启:
   cd ~/session-launcher && python3 src/ccs.py start <角色> --no-attach
4. 对方 > 5 分钟无回复:
   - 检查存活
   - 发 bus 提醒: bus_client.py write notice "@<角色> 请继续 #<上轮ID>" --src <本方>
5. 永远不要用 Monitor/事件监听器 替代主动循环
```

### 5.2 辩论模式专用 prompt

```
## 辩论模式
- 角色: <正方/反方>
- 对方角色: <对方角色名>
- 轮次: 从 bus 上轮消息提取轮次号，+1 即为本轮

### 轮次循环
1. 读上轮: bus_client.py read --cat debate --limit 1 --json
2. 提取对方论点和轮次号
3. 写本轮回复: bus_client.py write debate "<角色>第<N>轮: <论点>" --src <角色>
4. sleep 30
5. 跳回 1

### 死锁保护
- 写后 5 分钟 bus 无任何新消息 → 进入检查流程
- 不允许进入纯监听模式（不写 Monitor 事件监听器）
```

---

## 6. 实时推送机制

### 6.0 Bus Push 架构

数据流与证据见 `BUS_PUSH_ARCH.md`（唯一出处）。此处只留一条现状：写入路径无推送调用，
feed socket 仅在显式 `PUBLISH` 时广播；`feed_listener` 断线 5s 自动重连（`feed_listener.py:171-198`），
socket 不可用不影响 SQLite 写入。

**feed_listener.py 用法：**
```bash
python3 feed_listener.py                     # 实时监听
python3 feed_listener.py --on-debate-end     # 检测辩论结束
```

---

## 7. 监控告警

### 7.1 死锁检测 (bash)

```bash
#!/usr/bin/env bash
# 检查辩论是否停滞（5 分钟内无新消息）
LAST=$(python3 ~/.hermes/scripts/bus_client.py read --cat debate --limit 1 --json 2>/dev/null \
  | python3 -c "import sys,json,time; d=json.load(sys.stdin); f=d.get('facts',[{}])[0]; print(time.time()-f.get('timestamp',0))" 2>/dev/null || echo "999")
if [ "${LAST%.*}" -gt 300 ]; then
  echo "DEADLOCK: 辩论已停滞 ${LAST%.*}s"
else
  echo "OK: last msg ${LAST%.*}s ago"
fi
```

### 7.2 存活检查 (bash)

```bash
for role in verifier rebutter; do
  if tmux has-session -t "ccs-$role" 2>/dev/null; then
    echo "  $role: alive"
  else
    echo "  $role: DEAD"
  fi
done
```

---

## 8. 红线

1. **绝不两个 CCS 同时处于被动监听** — 至少一方承担主动推进职责
2. **创建方必须守护被创建方的生命周期** — verifier 创建 rebutter → verifier 负责检查 rebutter 存活
3. **永远设置超时保护** — 任何 `sleep` 或 `watch_read` 之前，设一个 max wait
4. **轮次号是唯一有序标识** — 不允许无轮次号的辩论消息
5. **CCS 重启后必须重建上下文** — 不能假设对方会重发历史消息
6. **禁止在协作 CCS 中使用 Monitor/事件监听器** — 它们导致"看起来在工作，实际在空转"
7. **CRITICAL 死锁超时 > 15 分钟 → 写入 bus architecture 升级给人**

---

## 9. 验证方法

| 场景 | 验证方法 | 预期 |
|------|----------|------|
| 正常辩论 | 双方每轮交替写 bus，轮次递增 | 轮次差 <= 1 |
| 一方死锁 | bus 最新消息 < 5 分钟 | 超时触发检查 |
| 一方挂掉 | `tmux has-session` 返回非 0 | 存活检查触发重启 |
| 仲裁介入 | 仲裁扫描 bus + 双方存活 | 死锁检测 + 唤醒指令 |
---

## 10. 已删除的旧入口（只留结论）

`ccs_start.py`（`2e2af8b` 删）、`launcher.py`（`93a7940` 删）、`deploy_all.sh` 均已不存在，`src/events/`、
`signals.py`、`worker_pool.py` 同样不在 `ls src/` 里；唯一入口是 `ccs.py`。历史上「旧 API 保留、双轨运行」
那一套迁移策略随旧文件一起失效，不要再按它设计兼容层。
