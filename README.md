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
│              ├────────────────────────────────────┐                      │
│              ▼                                    ▼                      │
│  ┌──────────────────────────────┐  ┌──────────────────────────────┐    │
│  │  session-launcher  ← 本项目  │  │ session-pipeline             │    │
│  │  执行层                      │  │ 路由层 + 工作流执行           │    │
│  │  - CCS 生命周期(tmux+claude) │  │ - 路由表自动推导              │    │
│  │  - 哨兵文件管理             │  │ - 消息分发+优先级              │    │
│  │  - 伙伴存活守护(watchdog)    │  │ - 工作流引擎(pipeflow)        │    │
│  │  - 轮次追踪+死锁检测        │  │ - 生命周期状态机              │    │
│  │  - 直连通信 Unix Socket      │  │ - 可靠性(熔断/重试/TTL)       │    │
│  │  - Bus Push 实时推送         │  │ - 路由表持久化(rdb)           │    │
│  │  - P0 豁免通道              │  │                                │    │
│  └──────┬───────────────────────┘  └──────────┬───────────────────┘    │
│         │                                     │                         │
│         │  pipeline → launcher 的唯一调用：    │                         │
│         │  subprocess ccs.py send              │                         │
│         │                                     │                         │
│         ▼                                     ▼                         │
│  ┌────────────────────────────────────────────────────────┐             │
│  │                    Sister Bus                           │             │
│  │           SQLite Blackboard + Unix Socket               │             │
│  └────────────────────────────────────────────────────────┘             │
└─────────────────────────────────────────────────────────────────────────┘
```

注意：**实际关系不是三层流水线**。session-roles（定义层）同时被 launcher 和 pipeline 独立消费。pipeline 不直接调 launcher 的 API，而是通过 `subprocess` 执行 `ccs.py send`。pipeline 和 launcher 之间无直接通信。

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
  ├── ops/sentinel.py       哨兵文件读写（/tmp/ccs-sentinels/）
  ├── ops/watchdog.py       伙伴存活守护线程（自动重启）
  ├── ops/tracker.py        轮次追踪 + 死锁检测
  ├── ops/ccs_config.py     全局配置中心
  │
  # ── 路由与门禁 ──
  ├── routing/roles.py      角色加载 + workspace 注入 + 禁区映射
  ├── routing/gatekeeper.py 三源验证 + 敏感操作门禁 + 工作群组
  ├── routing/partner.py    伙伴客户端
  │
  # ── 兼容 ──
  ├── paths.py              统一路径管理（从 hermes_bus.config 导入）
  └── ...
```

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
  "partners": ["scout"],
  "bus_track": "architecture",
  "health": { "watchdog_ok": true, "last_bus_msg_age": -1, "restart_count": 0 }
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

---

## 跨项目接口

session-launcher 被 session-pipeline 通过 subprocess 调用以向 CCS 发消息：

```
pipeline/routes.py → subprocess run([sys.executable, CCS_CLI, "send", role, message])
```

**注意**：这是 pipeline→launcher 的唯一调用路径。launcher 不反向调用 pipeline。

`CCS_CLI` 定义在 `paths.py`（从 `hermes_bus.config` 导入），两个项目的 `paths.py` 指向同一路径。

### MCP 隔离

launcher 在启动 CCS 时为每个角色写独立的 `.claude/settings.json`，实现 MCP 按角色隔离：

```python
# core.py: _write_mcp_settings()
# 从 persona JSON 读取 mcp_servers 字段
# 只给每个角色开启它声明需要的 MCP server
```

---

## 验证

```bash
python3 tests/test_wl_selfcheck.py -v
python3 src/ecosystem_health.py --json
python3 src/ccs.py health
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| SESSION_ROLES_ROOT | ~/hermes-session-roles | 角色定义目录 |
| SESSION_PIPELINE_SRC | ~/session-pipeline/src | Pipeline 源码目录 |
| HERMES_SCRIPTS_DIR | ~/.hermes/scripts | Hermes 脚本目录 |
| CCS_SOCKET_TOKEN | (无) | CCS Socket 认证令牌 |
