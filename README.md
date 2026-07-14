# Session Launcher（CCS 生命周期管理器）

## 定位

Session 生态的**执行层**——创建 CCS（Claude Code Session）进程、管理生命周期、提供跨 session 协作基础设施。

**核心职责：让每个专业角色独占一个独立进程、一个独立上下文、一颗独立 /loop，互不污染。**

```
┌─────────────────────────────────────────────────────────────────────────┐
│                      Hermes Session Ecosystem                          │
│                                                                         │
│  ┌───────────────────────┐                                             │
│  │  hermes-session-roles │  ← 角色定义层：JSON 模板 + prompt 蒸馏      │
│  └───────────┬───────────┘                                             │
│              │ 读取角色定义                                              │
│              ▼                                                          │
│  ┌──────────────────────────────────────────────────────────────┐      │
│  │  session-launcher  ← 本项目（执行层）                         │      │
│  │  - CCS 生命周期（tmux + Claude Code 进程）                    │      │
│  │  - 哨兵文件管理 (/tmp/ccs-sentinels/)                        │      │
│  │  - 伙伴存活守护 (watchdog)                                   │      │
│  │  - 轮次追踪 + 死锁检测 (tracker)                             │      │
│  │  - 直连通信 (<1ms Unix Socket)                               │      │
│  │  - Bus Push 实时推送                                         │      │
│  │  - 工作流模板注册与门禁                                       │      │
│  │  - P0 豁免通道                                               │      │
│  └───────────────────────┬──────────────────────────────────────┘      │
│                          │                                              │
│              ┌───────────┴───────────┐                                  │
│              ▼                       ▼                                  │
│  ┌──────────────────┐    ┌─────────────────────────────┐                │
│  │ Sister Bus       │    │ session-pipeline            │                │
│  │ SQLite + Socket  │    │ 路由层：消息分发/优先级      │                │
│  └──────────────────┘    └─────────────────────────────┘                │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 上下文隔离（本项目的核心职责）

角色定义层定义了"理想中每个角色该有多专业"，路由层定义了"消息该去哪"——但如果 PG 和 Cron 共享同一个上下文，所有专业蒸馏都被稀释为零。

**session-launcher 存在的根本原因：让每个角色独占一个独立 CLAUDE.md——互不污染、互不稀释。**

```
PG 的 CLAUDE.md:
  ├── L0 通用准则（决策阶梯、代码质量）
  ├── L1 项目红线（禁止重启 gateway）
  ├── L2 角色专业（项目架构、实现模式）
  └── L3 经验教训（踩坑记录）

Cron 的 CLAUDE.md:
  ├── L0 通用准则（YAGNI、最小改动）
  ├── L1 项目红线（死锁超时升级给人）
  ├── L2 角色专业（轮询策略、信号检测）
  └── L3 经验教训（已知空轮问题）
```

### 严格意义上注入时禁止的事项

| 禁止行为 | 后果 | 检测方法 |
|---------|------|---------|
| PG 的 CLAUDE.md 出现 Cron 指令 | PG 上下文被稀释 | 抓指定角色的 CLAUDE.md 检查内容纯度 |
| 一个角色的 system_prompt 小于 100 行 | 提示词密度不够 | 检查各角色 prompt 行数 |
| prompt 中包含协作逻辑 | AI 会自由解释协作规则 | 扫描 prompt 中 key words 是否含协作相关 |

---

## 项目结构

```
src/
  # ── CCS 生命周期 ──
  ├── ccs.py                CLI 主入口（argparse 分发全部子命令）
  ├── core.py               CCS 生命周期编排（start/stop/status/send/health）
  ├── tmux_ops.py           tmux 底层操作（PID 查找、pane 捕获、进程管理）
  ├── role_manager.py       角色定义加载、禁区映射、workspace CLAUDE.md 注入
  ├── codex_ops.py          Codex session 管理（start/exec/status）
  ├── ccs_socket.py         CCS 直接通信客户端（<1ms Unix Socket）
  │
  # ── 基础设施 ──
  ├── sentinel.py           哨兵文件读写（/tmp/ccs-sentinels/）
  ├── watchdog.py           伙伴存活守护线程（自动重启）
  ├── tracker.py            轮次追踪 + 死锁检测
  ├── lesson_injector.py    经验教训注入（reflexion_lesson → CLAUDE.md）
  │
  # ── 信号与执行 ──
  ├── signals.py            8 种信号检查器（旧接口兼容）
  ├── signal_parser.py      标准化信号解析器（新/旧格式统一入口）
  ├── worker_pool.py        并行 HTTP 调用 9Router（stdlib-only）
  ├── pool_cli.py           worker_pool CLI 入口
  ├── task_utils.py         任务操作函数（check/complete/fail/logs）
  │
  # ── 工作流系统 ──
  ├── lifecycle_manager.py  工作流状态机
  ├── step_engine.py        5 种步骤类型引擎
  ├── notification_engine.py 步骤完成通知引擎
  ├── workflow_client.py    CCS 角色使用的工作流客户端
  ├── workflow_gate.py      模板门禁系统
  ├── template_registry.py  模板注册中心（10 字段 JSON Schema）
  ├── template_validator.py 模板 5 步验证流程
  ├── migrate_v11_collab.py V1.1 跨角色协作数据库迁移
  │
  # ── 跨角色协作 ──
  ├── partner_client.py     跨角色协作核心（Layer 1-3）
  ├── p0_exemption.py       P0 豁免通道 + 审计轨迹
  ├── cross_role_router.py  跨角色路由拦截器（存根）
  ├── migration_scripts.py  存量数据迁移 + 回滚
  │
  # ── 生态集成 ──
  ├── ecosystem_health.py   三项目统一健康检查 API（可编程 + CLI）
  ├── ecosystem_cli.py      生态 CLI（status/relations/board）
  │
  # ── 兼容 ──
  ├── launcher.py           兼容层（旧接口导出）
  └── paths.py              统一路径管理（集中 Path.home() 调用）
```

---

## 核心功能```

---

## 核心功能

### 1. CCS 生命周期管理

```bash
# 创建并启动 CCS
python3 src/ccs.py start <role> [title] [--no-attach] [--prompt "msg"]
python3 src/ccs.py start <role> --partner <p> --auto-restart --bus-track <cat>
python3 src/ccs.py start <role> --drive loop|feed|both --feed-cat <category>

# 控制
python3 src/ccs.py stop <role>
python3 src/ccs.py send <role> "message"
python3 src/ccs.py output <role> [--tail N]
python3 src/ccs.py stream <role>          # 流式输出（0.5s 增量轮询）

# 状态
python3 src/ccs.py status
python3 src/ccs.py health [role]          # 健康检查
python3 src/ccs.py register <role> <tmux> # 注册手动 tmux 为 CCS
```

### 2. 哨兵文件

每个 CCS 在 `/tmp/ccs-sentinels/<role>.json` 写入运行状态：

```json
{
  "role": "maintainer",
  "tmux_session": "ccs-maintainer",
  "pid": 12345,
  "started_at": 1700000000.0,
  "lifecycle": "infinite",
  "partner": "scout",
  "bus_track": "architecture",
  "health": { "watchdog_ok": true, "last_turn_check": 1700000100.0 }
}
```

### 3. 协作模式

| 模式 | 命令 | 说明 |
|------|------|------|
| 主从 | `--partner <role> --auto-restart` | 守护伙伴存活，挂了自动重启 |
| 对等 | `--bus-track <category>` | 追踪 bus 轮次，超时发死锁提醒 |
| 仲裁 | `--partner A --partner B --bus-track X` | 同时守护 + 追踪 |
| 直连 | `send-direct <from> <to> <msg>` | 不走 bus，<1ms Unix Socket |

### 4. 直接通信 Socket

```
ccs.py send-direct alice bob "hi"
  └─ CCSClient("alice").connect()
       └─ SUBSCRIBE → /tmp/sister_bus_ccs.sock
            └─ PUBLISH → bob (<1ms)
```

**协议**：JSON-over-newline，支持 SUBSCRIBE/PUBLISH/PING/STATS

**认证**：`export CCS_SOCKET_TOKEN=my_secret_key`

### 5. Bus Push 实时推送

```
bus_client.py write debate "消息"
  └─ Blackboard.write()
       ├─ INSERT INTO facts (SQLite)   ← 主路径
       └─ _notify_feed()              ← 辅路径
            └─ /tmp/sister_bus_feed.sock → broadcast
```

依赖服务：`socket_server.py`（asyncio Unix Socket）、`feed_listener.py`（实时监听）

**降级保障**：feed socket 不可用时静默降级，不丢 SQLite 写入。

### 6. 工作空间管理

```bash
python3 src/ccs.py workspace create <name>   # 创建/更新 workspace CLAUDE.md
python3 src/ccs.py workspace list            # 列出所有 workspace
```

workspace CLAUDE.md 使用 `<!-- WORKSPACE_SYS:START/END -->` 标记系统区域，用户的额外内容保留在外部。

### 7. 工作流模板

```bash
python3 src/template_registry.py list                # 列出模板
python3 src/template_registry.py register <file>     # 注册模板（10 字段验证）
python3 src/template_registry.py validate <file>     # 校验不入库
python3 src/template_validator.py <file>              # 5 步验证
```

10 字段模板 = workflow_id + name + description + trigger_scene + allowed_initiators + allowed_executors + steps + max_duration_hours + quality_standards + notify_template

### 8. 跨角色协作

```bash
python3 src/partner_client.py resolve <role>           # 查询角色状态
python3 src/partner_client.py wake <role> --as <actor> # 唤醒角色
python3 src/partner_client.py confirm <task_id> <role> # 双信号等待确认
python3 src/partner_client.py send-safe <role> <msg>   # 安全发送（自动唤醒）
```

### 9. Worker Pool

并行 HTTP 调用 9Router（stdlib-only，无 `requests` 依赖）：

```bash
python3 src/worker_pool.py <role> '[{"id":"t1","prompt":"..."}]'
```

### 10. P0 豁免

仅 coordinator 和 lr 可创建 P0 豁免任务，4 小时内必须补录 template_id，超时自动检测并通知。

---

## 红线

1. **协作逻辑在代码层，不在 prompt 文本中** — 创建/守护/重启/死锁检测全部代码内置
2. **创建方必须守护被创建方** — `--partner` 自动启动 watchdog
3. **轮次追踪必须内置** — `--bus-track` 自动启动 turn_tracker
4. **死锁超时 > 15 分钟** → 写入 bus 升级给人
5. **stdlib only** — 禁止新增第三方 Python 依赖
6. **不使用 eval()** — 改用安全的 regex 模式匹配

---

## 依赖项目

| 项目 | 关系 | 说明 |
|------|------|------|
| hermes-session-roles | 上游定义层 | 读取角色 JSON 定义（25 角色 + 57 Browser Harness 人格） |
| session-pipeline | 下游路由层 | 消息优先级分发与消费者调度 |
| Sister Bus | 基础设施 | SQLite blackboard + Unix Socket feed |
| 9Router | 推理引擎 | HTTP API (localhost:20128) |

---

## 测试

```bash
# 6 维度深度 QA（推荐）
python3 -m pytest tests/test_6dimension_deep_qa.py -v

# 端到端 mock
python3 -m pytest tests/test_e2e_mock.py -v

# 系统健康检查
python3 -m pytest tests/test_system_health.py -v

# 验证全部模块导入
PYTHONPATH=src python3 -c "import core, tmux_ops, role_manager, codex_ops, signals, sentinel, watchdog, tracker, launcher; print('All imports OK')"
```

---

## 系统别名（注册到 ~/.bash_aliases）

```bash
ccs <role> [title]          # 启动 CCS 并 attach
ccs-status                  # 列出所有 CCS 状态
ccs-ls                      # tmux 列表（只看 ccs-）
ccs-stop <role>             # 停止 CCS
ccs-send <role> "消息"       # 向 CCS 发消息
ccs-out <role>              # 查看 CCS 输出
ccs-stream <role>           # 流式输出（实时跟踪）
```
