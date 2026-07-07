# Hermes Session 协作架构设计 V2

> 版本: 2.0 | 日期: 2026-07-07 | 状态: 实施中

---

## 一、设计原则

**核心理念：** 将协作逻辑从 prompt 文本下沉到基础设施层，LLM 只负责语义决策，代码负责执行保障。

| # | 原则 | 说明 |
|---|------|------|
| 1 | **三层分离** | 角色定义 / 工作空间 / 驱动机制 各自独立 |
| 2 | **双层监控** | 代码层（机械可靠）+ LLM 层（语义智能） |
| 3 | **零轮询** | 所有实时通知走 Unix Socket push，非 poll |
| 4 | **静默降级** | 任何组件故障不影响其他组件，降级到最简单模式 |
| 5 | **审计可追溯** | 所有决策写入 bus，可事后复盘 |

---

## 二、系统架构

```
┌─────────────────────────────────────────────────────────────────────────┐
│                     Hermes Session Ecosystem                            │
│                                                                         │
│  ┌────────────────────┐    ┌──────────────────────┐   ┌──────────────┐  │
│  │ hermes-session-    │    │ session-launcher     │   │ session-     │  │
│  │ roles (定义层)     │    │ (执行层)              │   │ pipeline     │  │
│  │                    │    │                       │   │ (路由层)     │  │
│  │ persona_*.json     │───→│ ccs.py               │   │ router.py    │  │
│  │ → name/title       │    │ → start/stop/status  │───│ auto_route.py│  │
│  │ → system_prompt    │    │ → watchdog (守护)    │   │ reliability  │  │
│  │ → input_signals    │    │ → tracker (轮次)     │   └──────────────┘  │
│  │ → output_targets   │    │ → sentinel (状态)    │                     │
│  │ → drive_type       │    │ → feed_listener      │                     │
│  │ → watch_cats       │    │ → workspaces/        │                     │
│  └────────────────────┘    └──────────────────────┘   └──────────────┘  │
│                                      │                                  │
│                          ┌───────────┴───────────┐                     │
│                          ▼                       ▼                     │
│               ┌─────────────────┐   ┌──────────────────────┐          │
│               │ Sister Bus      │   │ Sister Bus Push      │          │
│               │ (SQLite FTS5)   │   │ (Unix Socket)        │          │
│               │ 持久化主路径    │   │ 实时辅路径           │          │
│               └─────────────────┘   └──────────────────────┘          │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 三、三层指令体系

### 层次结构

```
┌──────────────────────────────────────────────────────────────┐
│  第一层：角色定义（hermes-session-roles）                     │
│  身份 + 专业领域 + 输入/输出定义                              │
│  存储：persona_*.json → CLAUDE.md                            │
├──────────────────────────────────────────────────────────────┤
│  第二层：工作空间（session-launcher/ccs_workspaces/）         │
│  禁令 + 权限 + 工作方式 + 上下文                              │
│  系统级 CCS：独立 CLAUDE.md                                   │
│  普通级 CCS：全局 ~/.claude/CLAUDE.md                        │
├──────────────────────────────────────────────────────────────┤
│  第三层：驱动机制（session-launcher + session-pipeline）       │
│  /loop + ccs-send + feed push                                 │
│  所有 CCS 统一，不区分系统级和普通级                           │
└──────────────────────────────────────────────────────────────┘
```

### 各层职责

| 层次 | 负责 | 改动频率 | 存储位置 |
|------|------|---------|---------|
| 角色定义 | "我是谁" | 角色增删 | `hermes-session-roles/persona_*.json` |
| 工作空间 | "我能做什么、禁止做什么" | 架构变更 | `ccs_workspaces/<role>/CLAUDE.md` |
| 驱动机制 | "怎么执行、何时触发" | 系统优化 | session-launcher + session-pipeline |

### CLAUDE.md 内容对比

| 内容 | 系统级 CLAUDE.md | 普通级 CLAUDE.md |
|------|-----------------|-----------------|
| 位置 | `~/ccs-workspaces/<role>/CLAUDE.md` | `~/.claude/CLAUDE.md` |
| 角色定义 | ✅ 独立 | ✅ 共享 |
| 权限/禁令 | ✅ 独立（如"不调 LLM"） | ✅ 共享（全局） |
| 工作方式 | ✅ 独立（如"每 30s 巡检"） | ✅ 共享（bus 协议） |
| 上下文 | ✅ 独立（监控历史） | ✅ 共享 |

---

## 四、三种驱动机制（所有 CCS 通用）

### 4.1 /loop 自循环

```
CLAUDE.md 指令 → claude /loop → 每 30s 自动执行 → 写审计
```

| 层次 | 实现 |
|------|------|
| 角色定义 | `persona_*.json: drive: "loop"` |
| 启动注入 | `session-launcher/src/ccs.py start` 时发送 `/loop` 到 tmux |
| 执行 | claude 内部 `/loop` 循环 |

### 4.2 ccs-send 外驱

```
其他 CCS → bus_client.py write → session-pipeline 路由 → ccs-send → tmux send-keys
```

| 层次 | 实现 |
|------|------|
| 角色定义 | `persona_*.json: input_signals: ["bus cat=task @ccs-role"]` |
| 消息路由 | `session-pipeline/src/router.py get_consumers()` → 决定谁该收 |
| 消息推送 | `session-launcher/ccs_send <role> "消息"` → tmux send-keys |
| 执行 | claude 收到消息 → 处理 → 回复 |

### 4.3 feed push 实时

```
bus_client.py write → bus_protocol.py Blackboard.write()
  → INSERT (SQLite) + _notify_feed() → socket push
  → socket_server.py broadcast → claude 收到
```

| 层次 | 实现 |
|------|------|
| 角色定义 | `persona_*.json: watch_cats: ["debate", "code_fix"]` |
| 推送源 | `session-pipeline/bus_protocol.py write()` → `_notify_feed()` |
| 广播 | `hermes_core/sister_bus/socket_server.py` → feed agent → broadcast |
| 接收 | `session-launcher/feed_listener.py` → 检测关键词 → 写 notice |

### 三驱动统一流程

```
CCS 启动（session-launcher）
  │
  ├─ ① 注入 /loop ──── claude 自循环执行 CLAUDE.md 指令
  │
  ├─ ② 启动 feed_listener ── 订阅 feed socket，收到消息 → 唤醒 claude
  │
  └─ ③ 外部 ccs-send ── tmux send-keys 发消息 → claude 收到
```

---

## 五、双层监控架构

```
┌─────────────────────────────────────────────────────────────┐
│                   LLM 监控层 (ccs-monitor)                    │
│  理解上下文、判断根因、做语义决策、处理未知场景               │
│                                                             │
│  输入: feed push + sentinel + bus 上下文                    │
│  输出: bus action 指令（重启 / 纠正 / 升级给人）            │
│  驱动: /loop + ccs-send + feed push（三合一）               │
│  可靠: ❌ 不可控但强大 —— 需要驾驭工程                      │
└────────────────────────┬────────────────────────────────────┘
                         │  escalate on uncertainty
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   代码监控层 (watchdog)                       │
│  机械执行：PID 检查 → 超时重启 → 轮次追踪                    │
│                                                             │
│  输入: tmux has-session + sentinel                          │
│  输出: subprocess.Popen 重启 / bus 提醒                     │
│  驱动: daemon 线程每 30s                                    │
│  可靠: ✅ 快准狠 —— 但只会机械执行                           │
└─────────────────────────────────────────────────────────────┘
```

| 场景 | 代码层处理 | LLM 层处理 |
|------|-----------|-----------|
| tmux 进程死 | ✅ 立即重启 | — |
| 超时无响应 | ✅ 重启 | — |
| bus 死锁 >5min | — | ✅ 判断是思考中还是真死锁 |
| 重复同样输出 | — | ✅ 检测到循环，发新指令打破 |
| 错误/幻觉 | — | ✅ 检测到异常模式，纠正或重启 |
| 跨 CCS 死锁 | — | ✅ 双方都在等，仲裁介入 |
| 终局检测 | — | ✅ 理解辩论是否真的结束 |
| 升级给人 | — | ✅ 无法判断时写 bus 升级 |

### LLM 可靠性保障（驾驭工程）

1. **LLM 只建议，代码判断**：ccs-monitor 写 bus `action` 指令，代码层执行
2. **明确决策树**：system_prompt 写死决策路径，不允许自由发挥
3. **分段超时**：每轮最多 30s 思考 → 超时强制下行
4. **审计跟踪**：所有 LLM 决策写入 bus cat=monitor_audit
5. **静默降级**：LLM 不可用时，代码 watchdog 继续工作

---

## 六、实时推送架构（feed push）

```
bus_client.py write debate "消息"
  └─ Blackboard.write()
       ├─ ① INSERT INTO facts (SQLite)  ← 持久化
       └─ ② _notify_feed() → /tmp/sister_bus_feed.sock
            └─ ③ socket_server.py → broadcast
                 ├─ ccs-verifier → 实时收到
                 ├─ ccs-monitor  → 实时检测
                 └─ feed_listener.py → 检测终局 → write notice
```

| 测试指标 | 结果 |
|---------|------|
| 推送成功率 | 100% (20/20) |
| 端到端延迟 | 43-51ms |
| 终局检测 | ✅ |
| SQLite 持久化 | ✅ 100% |
| feed 降级 | 静默，不丢数据 |

---

## 七、项目职责矩阵

| 功能 | hermes-session-roles | session-launcher | session-pipeline |
|------|---------------------|------------------|------------------|
| 角色 JSON 定义 | ✅ 负责 | 读取 | — |
| CLAUDE.md 管理 | — | ✅ 负责（启动时注入） | — |
| 工作空间管理 | — | ✅ 负责（系统级 CCS） | — |
| /loop 注入 | — | ✅ 负责 | — |
| ccs-send 消息路由 | — | 发送 | ✅ 路由决策 |
| feed push 推送源 | — | 订阅 | ✅ 写入触发 |
| watchdog 线程 | — | ✅ 负责 | — |
| turn_tracker 线程 | — | ✅ 负责 | — |
| sentinel 系统 | — | ✅ 负责 | — |
| 哨兵文件管理 | — | ✅ 负责 | 读取 |
| CCS 启动/停止 | — | ✅ 负责 | — |
| 消息优先级路由 | — | — | ✅ 负责 |
| ACK 追踪 | — | — | ✅ 负责 |

---

## 八、系统级 CCS 工作空间

### ccs-monitor 工作空间

```
~/ccs-workspaces/ccs-monitor/
  CLAUDE.md
    # ccs-monitor
    ## 身份
    你是 ccs-monitor，CCS 监控者。

    ## 职责
    - 检查所有 CCS tmux session 是否存活
    - 检测 bus 死锁/终局/异常
    - 发写通知/升级给人

    ## 禁令
    - 不调 LLM / 不执行业务逻辑
    - 不退出 / 不休眠超过 60s
    - 不直接操作 tmux（通过 bus action 指令）

    ## 工作方式（三种驱动）
    ① /loop 每 30s 巡检
    ② ccs-send 接收其他 CCS 发来的指令
    ③ feed push 实时触发

    ## 工作空间
    ~/ccs-workspaces/ccs-monitor/
```

### 普通级 CCS

```
~/.claude/CLAUDE.md  ← 全局共享
persona_*.json  → 专业领域 + prompt
```

---

## 九、实施路径

### Phase 1：架构落地（当前）
- [x] bus_protocol.py feed push
- [x] socket_server.py feed agent
- [x] feed_listener.py 实时监听
- [x] watchdog 线程（daemon=False）
- [x] turn_tracker 线程（daemon=False）
- [x] sentinel 健康字段
- [x] 三方三轮审查

### Phase 2：CLAUDE.md 独立化
- [ ] 创建 ccs_workspaces 目录结构
- [ ] 编写 ccs-monitor CLAUDE.md
- [ ] ccs_start.py 支持 `--workspace` 参数
- [ ] 启动时注入独立 CLAUDE.md（系统级）

### Phase 3：三驱动统一封装
- [ ] session-launcher 提供统一 API：
      `ccs.py start --drive loop --feed-cat debate --partner rebutter`
- [ ] session-pipeline 自动路由 ccs-send
- [ ] feed push 自动启动对应 listener

### Phase 4：LLM 驾驭工程
- [ ] ccs-monitor CLAUDE.md 加决策树
- [ ] 审计 bus cat=monitor_audit
- [ ] 降级策略实现
