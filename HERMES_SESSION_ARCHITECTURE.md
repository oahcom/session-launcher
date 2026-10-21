# Hermes Session 协作架构设计 V2

> 版本: 2.0 | 日期: 2026-07-07 | 状态: 已实施（2026-10-22 按代码复核：Phase 2-4 已落地；数字类指标为当时实测，不可复验）

---

## 一、设计原则

**核心理念：** 将协作逻辑从 prompt 文本下沉到基础设施层，LLM 只负责语义决策，代码负责执行保障。

| # | 原则 | 说明 |
|---|------|------|
| 1 | **三层分离** | 角色定义 / 工作空间 / 驱动机制 各自独立 |
| 2 | **双层监控** | 代码层（机械可靠）+ LLM 层（语义智能） |
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
│  │ persona_*.json     │───→│ ccs.py               │   │ routing/     │  │
│  │ → name/title       │    │ → start/stop/status  │───│  router.py   │  │
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
│  第二层：工作空间（~/ccs-workspaces/<role>/）                 │
│  禁令 + 权限 + 工作方式 + 上下文                              │
│  系统级 CCS：独立 CLAUDE.md                                   │
│  普通级 CCS：全局 ~/.claude/CLAUDE.md                        │
├──────────────────────────────────────────────────────────────┤
│  第三层：驱动机制（session-launcher + session-pipeline）       │
│  包装循环 + ccs-send + feed push                             │
│  所有 CCS 统一，不区分系统级和普通级                           │
└──────────────────────────────────────────────────────────────┘
```

### 各层职责

| 层次 | 负责 | 改动频率 | 存储位置 |
|------|------|---------|---------|
| 角色定义 | "我是谁" | 角色增删 | `hermes-session-roles/persona_*.json` |
| 工作空间 | "我能做什么、禁止做什么" | 架构变更 | `~/ccs-workspaces/<role>/CLAUDE.md` |
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

### 4.1 角色自循环（while-true 包装，不是 `/loop`）

```
workspace CLAUDE.md 指令 → 包装循环起 claude → 本轮结束即重启下一轮
```

| 层次 | 实现 |
|------|------|
| 角色定义 | `persona_*.json: drive`（实测取值 `infinite`/`ondemand`/`cron`，无 `loop`） |
| 启动包装 | `ccs.py start` 起 `bash -c "while true; do claude …; done"`（core.py:627），**不发 `/loop`**（`grep -rn '/loop' src/` = 0） |
| 执行 | 每轮 claude 由 workspace `CLAUDE.md` 驱动，轮间由包装循环重启 |

### 4.2 ccs-send 外驱

```
其他 CCS → bus_client.py write → session-pipeline 路由 → ccs-send → tmux send-keys
```

| 层次 | 实现 |
|------|------|
| 角色定义 | `persona_*.json: input_signals: ["bus cat=task @ccs-role"]` |
| 消息路由 | `session-pipeline/src/routing/router.py:165 get_consumers()` → 决定谁该收 |
| 消息推送 | `session-launcher/src/ccs.py send <role> "消息"` → tmux send-keys（无 `ccs_send` 脚本） |
| 执行 | claude 收到消息 → 处理 → 回复 |

### 4.3 feed push 实时

```
bus_client.py write → bus_protocol.py Blackboard.write()
  → INSERT (SQLite) + _notify_feed() → socket push
  → socket_server.py broadcast → claude 收到
```

| 层次 | 实现 |
|------|------|
| 角色定义 | persona **无** `watch_cats` 字段（全库 grep 0 命中）；分类来自启动参数 `--feed-cat` |
| 推送源 | **不存在**：`grep -rn _notify_feed ~/.hermes/scripts` 无命中，写入只落 SQLite |
| 广播 | `hermes_bus/socket_server.py:44` 有 `feed` agent，但只在显式 `PUBLISH` 时 `_push_to`（:144-152） |
| 接收 | `session-launcher/feed_listener.py` → 检测关键词 → 写 notice |

### 三驱动统一流程

```
CCS 启动（session-launcher）
  │
  ├─ ① 包装循环 ────── bash while true 重启 claude，按 CLAUDE.md 执行
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
│  驱动: 包装循环 + ccs-send + feed push（三合一）            │
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

细节与实测见 `BUS_PUSH_ARCH.md`（唯一出处，本节不重复）。

**现行事实**：写入路径不含推送调用，`feed_listener` 只在显式 `PUBLISH` 时收到消息（见 §四 4.3 证据行）。
本文件早先记录的 20/20 推送成功率、43-51ms 延迟是 2026-07-07 的观测，当前代码下**无法复验**。

---

## 七、项目职责矩阵

| 功能 | hermes-session-roles | session-launcher | session-pipeline |
|------|---------------------|------------------|------------------|
| 角色 JSON 定义 | ✅ 负责 | 读取 | — |
| CLAUDE.md 管理 | — | ✅ 负责（启动时注入） | — |
| 工作空间管理 | — | ✅ 负责（系统级 CCS） | — |
| 包装循环（core.py:627） | — | ✅ 负责 | — |
| ccs-send 消息路由 | — | 发送 | ✅ 路由决策 |
| feed push 推送源 | — | 订阅 | —（只写 SQLite，无推送源） |
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
    ① 包装循环起 claude，tmux pane 每约 30s 巡检状态
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
- [x] watchdog 线程（daemon=True，watchdog.py:248）
- [x] turn_tracker 线程（daemon=True，tracker.py:167）
- [x] sentinel 健康字段
- [x] 三方三轮审查

### Phase 2：CLAUDE.md 独立化（完成）
- [x] 工作空间目录 `~/ccs-workspaces/`（`ccs workspace create/list`）
- [x] ccs-monitor CLAUDE.md 存在：`~/ccs-workspaces/ccs-monitor/CLAUDE.md`
- [x] `--workspace` 参数（`src/ccs.py:70`）；`ccs_start.py` 已删（`2e2af8b`），入口是 `ccs.py`
- [x] 启动时注入角色知识块（`routing/roles.py`）

### Phase 3：三驱动统一封装（完成）
- [x] 统一 API：`ccs.py start --drive … --feed-cat … --partner …`
- [x] pipeline 侧 `routing/router.py get_consumers()` 自动路由
- [x] feed listener 随 `start` 自动拉起（`core.py:818-823`）

### Phase 4：LLM 驾驭工程（部分）
- [x] ccs-monitor CLAUDE.md 决策树
- [ ] 审计 bus cat=monitor_audit（未验证：无独立消费者）
- [x] 降级策略：watchdog/tracker 不依赖 LLM
