# CCS 协作系统实施任务书

> 基于 HERMES_SESSION_ARCHITECTURE.md V2，逐阶段实施。
> 每个任务有明确的输入/输出/验收标准，可直接指派给 CCS 执行。

---

## Phase 2：CLAUDE.md 独立化（系统级 CCS 工作空间）

### Task 2.1：创建 ccs_workspaces 目录结构

**目标**：为系统级 CCS 创建独立工作空间。

**输入**：无
**输出**：`~/ccs-workspaces/` 目录结构

```bash
mkdir -p ~/ccs-workspaces/ccs-monitor
mkdir -p ~/ccs-workspaces/ccs-coordinator
```

**验收标准**：
- 目录存在
- 每个子目录对应一个系统级 CCS 角色

---

### Task 2.2：编写 ccs-monitor CLAUDE.md

**目标**：为 ccs-monitor 创建独立工作空间的 CLAUDE.md。

**输入**：`HERMES_SESSION_ARCHITECTURE.md` 第九节
**输出**：`~/ccs-workspaces/ccs-monitor/CLAUDE.md`

**内容模板**：
```markdown
# ccs-monitor

## 身份
你是 ccs-monitor，CCS 监控者。
你通过三种驱动方式接收指令：/loop 自循环、ccs-send 外驱、feed push 实时。

## 职责
- 检查所有 CCS tmux session 是否存活
- 检测 bus 死锁/终局/异常
- 发写通知/升级给人

## 禁令
- 不调 9Router / 不执行业务逻辑
- 不退出 / 不休眠超过 60s
- 不直接操作 tmux（通过 bus action 指令）

## 驱动方式
① /loop 每 30s 巡检（读 sentinel + bus）
② ccs-send 接收其他 CCS 发来的指令
③ feed push 实时触发（bus 新消息唤醒）

## 工作空间
~/ccs-workspaces/ccs-monitor/
```

**验收标准**：
- CLAUDE.md 存在且内容完整
- 包含三种驱动方式说明
- 包含禁令列表
- 不包含全局 CLAUDE.md 的无关职责

---

### Task 2.3：更新 ccs_start.py 支持 --workspace 参数

**目标**：让 ccs_start.py 支持启动系统级 CCS 时注入独立 CLAUDE.md。

**输入**：`src/ccs_start.py` 当前代码
**输出**：`src/ccs_start.py` 新增 `--workspace` 参数

**修改逻辑**：
```python
# ccs_start.py start() 函数
def start(role, workspace=None, ...):
    if workspace:
        # 系统级 CCS：读取独立 CLAUDE.md
        claude_md = Path(f"~/ccs-workspaces/{workspace}/CLAUDE.md").expanduser()
        # 注入到启动命令
        cmd = f"claude --cd={claude_md.parent} ..."
    else:
        # 普通 CCS：使用全局 CLAUDE.md
        pass
```

**验收标准**：
- `ccs start ccs-monitor` 读取 `~/ccs-workspaces/ccs-monitor/CLAUDE.md`
- `ccs start verifier` 使用全局 CLAUDE.md
- 参数可选，不影响现有行为

---

### Task 2.4：创建 ccs-workspaces 脚手架命令

**目标**：提供命令行工具创建新的 CCS 工作空间。

**输入**：`src/ccs.py` 当前代码
**输出**：`ccs.py` 新增 `workspace` 子命令

```bash
# 创建新工作空间
ccs workspace create ccs-coordinator

# 列出所有工作空间
ccs workspace list
```

**验收标准**：
- `ccs workspace create <role>` 自动创建目录 + 生成默认 CLAUDE.md
- `ccs workspace list` 列出所有 `~/ccs-workspaces/*/CLAUDE.md`
- 新生成的 CLAUDE.md 包含三驱动框架

---

## Phase 3：三驱动统一封装

### Task 3.1：统一 API 设计

**目标**：提供统一的 CCS 启动命令，支持三种驱动方式。

**输入**：`src/ccs.py` 当前代码
**输出**：`src/ccs.py` 统一 CLI 接口

```bash
# 主从模式 + 实时推送
ccs start verifier "辩论正方" \
  --partner rebutter \
  --auto-restart \
  --bus-track debate \
  --drive loop \
  --feed-cat debate \
  --no-attach

# 对等模式
ccs start pro --bus-track debate --drive loop --no-attach
```

**验收标准**：
- 所有参数在一条命令中完成
- `--drive loop` / `--bus-track` / `--partner` 互不冲突
- 系统级 CCS 使用 `--workspace` 自动注入独立 CLAUDE.md
- 普通 CCS 不受影响

---

### Task 3.2：session-pipeline 自动路由 ccs-send

**目标**：让 session-pipeline 自动路由 ccs-send 消息到正确的 CCS。

**输入**：`session-pipeline/src/router.py` 当前代码
**输出**：`session-pipeline/src/router.py` 更新

```python
# router.py 路由逻辑
def get_consumers(category):
    """返回该分类的所有消费者 CCS。"""
    # 读取 hermes-session-roles 的 persona_*.json
    # 找 output_targets 含 cat=<category> 的角色
    return [ccs for ccs in all_roles if category in ccs.output_targets]
```

**验收标准**：
- `get_consumers("debate")` 返回所有 `output_targets` 含 `cat=debate` 的 CCS
- `get_consumers("notice")` 返回 ccs-monitor
- 路由逻辑从角色 JSON 自动生成，不硬编码

---

### Task 3.3：feed listener 自动启动

**目标**：启动 CCS 时自动启动对应的 feed listener。

**输入**：`session-launcher/src/ccs.py` 当前代码
**输出**：`session-launcher/src/ccs.py` 更新

```python
# ccs.py start() 中
if watch_cats:
    # 自动启动 feed listener 监听这些分类
    start_feed_listener(role, watch_cats)
```

**验收标准**：
- CCS 启动后自动订阅 feed socket
- 收到消息后唤醒 claude
- CCS 停止后 feed listener 自动清理

---

## Phase 4：LLM 驾驭工程

### Task 4.1：ccs-monitor 决策树 prompt

**目标**：为 ccs-monitor 设计明确的决策树，限制 LLM 的决策范围。

**输入**：`HERMES_SESSION_ARCHITECTURE.md` 第五节
**输出**：`~/ccs-workspaces/ccs-monitor/CLAUDE.md` 更新

**决策树模板**：
```
## 决策树

收到 feed push 或 ccs-send 消息时，按以下顺序判断：

1. 新消息 → 读消息内容
2. 是否终局关键词？→ 是：写 bus notice 结论汇总 → 结束
3. 是否错误/异常？→ 是：分析根因 → 写 bus action 指令 → 结束
4. 是否死锁？→ 是：检查双方存活 → 判断原因 → 写 action 指令
5. 以上都不是 → 跳过

禁止事项：
- 不要自由发挥写消息
- 不要直接操作 tmux
- 每轮最多思考 30 秒 → 超时强制结束
```

**验收标准**：
- 决策树包含明确的判断路径
- 每个分支有明确的输出动作
- 包含超时保护逻辑

---

### Task 4.2：审计 bus cat=monitor_audit

**目标**：为所有 LLM 决策提供审计追踪。

**输入**：`HERMES_SESSION_ARCHITECTURE.md` 第五节
**输出**：`~/.hermes/scripts/bus_client.py` 更新

```python
# 在所有 LLM 决策点写入审计
_bus_write("monitor_audit", f"决策: {decision} → {action}", src="ccs-monitor")
```

**验收标准**：
- 每次 ccs-monitor 做出决策，都写入 `bus cat=monitor_audit`
- 包含决策内容、时间戳、CCS 状态
- 可事后查询复盘

---

### Task 4.3：LLM 降级策略

**目标**：LLM 不可用时，代码层继续工作。

**输入**：`session-launcher/src/ccs.py` 当前代码
**输出**：`session-launcher/src/ccs.py` 更新

```python
# watchdog 线程不依赖 LLM
# 代码层机械执行：PID 检查 → 超时重启 → 轮次追踪
# LLM 层只是辅助判断
```

**验收标准**：
- LLM 不可用时，watchdog 仍能重启死掉的 CCS
- LLM 不可用时，turn_tracker 仍能检测 bus 死锁
- LLM 不可用时，feed_listener 仍能接收推送

---

## Phase 5：向后兼容

### Task 5.1：旧 CCS 透明迁移

**目标**：旧的 CCS 使用方式继续工作，不受新架构影响。

**输入**：所有旧 CCS
**输出**：无变化，旧行为继续

```bash
# 旧方式继续可用
ccs verifier "辩论正方"  # 走全局 CLAUDE.md

# 新方式启动系统级 CCS
ccs start ccs-monitor    # 走 ~/ccs-workspaces/ccs-monitor/
```

**验收标准**：
- 旧 CCS 行为完全不变
- 新系统级 CCS 使用独立工作空间
- 全局 CLAUDE.md 仍可用

---

## Phase 6：监控与可观测性

### Task 6.1：CCS 健康仪表板

**目标**：提供 CCS 运行状态的可视化。

**输入**：`session-launcher/src/ccs.py` status
**输出**：终端输出格式化

```bash
ccs status
# 输出：
# 运行中的 CCS: 4
#   [ccs-monitor ] Session 监控者  ✅  bus=debate  pid=3996529
#   [ccs-verifier] 辩论正方       ✅  bus=debate  pid=2333322
#   [ccs-rebutter] 辩论反方       ✅  bus=debate  pid=1392138
```

**验收标准**：
- 显示所有 CCS 的角色、状态、PID、bus 分区
- 显示 watchdog/turn_tracker 状态
- 显示 feed listener 状态

---

## 实施状态（2026-07-15 更新）

| 阶段 | 任务 | 依赖 | 预计时间 | 状态 |
|------|------|------|---------|------|
| Phase 2 | 2.1-2.4 CLAUDE.md 独立化 | 无 | 1-2h | ✅ 已实施 |
| Phase 3 | 3.1-3.3 三驱动统一封装 | Phase 2 | 2-3h | ✅ 评估通过（代码超前设计） |
| Phase 4 | 4.1 决策树 prompt | Phase 2 | 1h | ✅ 已在 ccs-monitor CLAUDE.md |
| Phase 4 | 4.2 monitor_audit 审计 | Phase 2 | 1h | ✅ 已实施（5 个决策点） |
| Phase 4 | 4.3 LLM 降级 | Phase 2 | 1h | ✅ 验证通过（离线运行） |
| Phase 5 | 5.1 向后兼容 | Phase 2 | 0.5h | ✅ 旧 CCS 行为不变 |
| Phase 6 | 6.1 健康仪表板 | Phase 2 | 1h | ✅ `ccs dashboard` 已实现 |

**总计完成：7/7 Phase（100%）**

## 文件清单

| 文件 | 任务 | 状态 |
|------|------|------|
| `HERMES_SESSION_ARCHITECTURE.md` | 架构设计 | ✅ 已完成 |
| `BUS_PUSH_ARCH.md` | 推送设计 | ✅ 已完成 |
| `CCS_COLLAB_PROTOCOL.md` | 协作协议 | ✅ 已完成 |
| `IMPLEMENTATION_TASKS.md` | 本文件 | ✅ 已完成 |
| `ccs_workspaces/ccs-monitor/CLAUDE.md` | Task 2.2 | ✅ 已创建 |
| `ccs_workspaces/ccs-coordinator/CLAUDE.md` | Task 2.2 | ✅ 已创建 |
| `src/ccs.py` + `core.py` | Task 2.3-3.1, 6.1 | ✅ --workspace + dashboard |