# Session Launcher — Technical Deep Dive

## 1. 项目定位

Session 生态的**执行层**。创建 CCS（Claude Code Session）进程、管理生命周期、跨 session 协作基础设施。核心职责：让每个专业角色独占一个独立进程、一个独立上下文、一颗独立 /loop，互不污染。

```
roles (定义层 JSON) → launcher (执行层) → pipeline (路由层)
                        ↕
                   Sister Bus (通信总线)
```

## 2. 架构概览

```
session-launcher/
├── ccs.py                 # CLI 主入口（argparse 分发全部子命令）
├── core.py                # 生命周期编排（start/stop/status/send/health）
├── tmux_ops.py            # tmux 底层操作
├── codex_ops.py           # Codex session 管理
├── ccs_socket.py          # CCS 直接通信客户端（<1ms Unix Socket）
├── role_manager.py        # → routing.roles（角色定义加载与知识注入）
├── launcher.py            # 兼容层
├── event_log.py           # 事件日志
├── paths.py               # 统一路径管理
├── signal_parser.py       # 信号解析器
├── worker_pool.py         # 并行 HTTP 调用 9Router（stdlib-only）
├── pool_cli.py            # worker_pool CLI
├── lesson_injector.py     # 经验教训注入
│
├── events/                # 信号系统
│   ├── signals.py         # 8 种信号检查器
│   ├── parser.py          # 信号解析
│   └── notify.py          # 通知管理
│
├── lifecycle/             # 工作流状态机
│   └── engine.py          # 生命周期引擎
│
├── migration/             # 数据迁移
│   └── scripts.py         # V1.1 协作迁移
│
├── ops/                   # 运行时基础设施
│   ├── sentinel.py        # 哨兵系统（tmux 实时派生）
│   ├── watchdog.py        # 伙伴存活守护线程
│   ├── tracker.py         # 轮次追踪 + 死锁检测
│   ├── runner.py          # 仪表板 + feed listener
│   └── workspace.py       # 工作空间管理
│
├── routing/              # 路由与协作
│   ├── partner.py         # 跨角色协作（Layer 1-3）
│   ├── roles.py           # 角色加载/注入/禁区
│   ├── router.py          # 跨角色路由拦截器
│   └── gateway.py         # 路由网关
│
├── workflow/              # 工作流系统
│   ├── client.py          # CCS 角色使用的工作流客户端
│   ├── db.py              # 工作流数据库
│   ├── gateway.py         # 模板门禁系统
│   └── utils.py           # 工具函数
│
├── ecosystem_health.py    # 三项目统一健康检查
└── ecosystem_cli.py       # 生态 CLI
```

## 3. 核心模块详解

### 3.1 ccs.py — CLI 主入口

argparse 分发 12 个子命令：

| 命令 | 功能 | 示例 |
|------|------|------|
| `start` | 创建并启动 CCS | `ccs.py start verifier --partner rebutter` |
| `stop` | 终止 CCS | `ccs.py stop verifier` |
| `status` | 列出所有 CCS 状态 | `ccs.py status` |
| `send` | 发送消息 | `ccs.py send verifier "你好"` |
| `output` | 查看输出 | `ccs.py output verifier --tail 20` |
| `stream` | 流式输出 | `ccs.py stream verifier` |
| `health` | 健康检查 | `ccs.py health` |
| `dashboard` | 聚合健康仪表板 | `ccs.py dashboard` |
| `register` | 注册手动 tmux 为 CCS | `ccs.py register role tmux_name` |
| `workspace` | 管理工作空间 | `ccs.py workspace create <name>` |
| `send-direct` | 直连通信（<1ms） | `ccs.py send-direct alice bob hi` |
| `send-safe` | 自动唤醒后发送 | `ccs.py send-safe role msg --by-role src` |
| `wake` | 唤醒 CCS | `ccs.py wake role --by-role src --context ctx` |
| `reload-knowledge` | 刷新角色 KNOWLEDGE 块 | `ccs.py reload-knowledge maintainer` |
| `codex` | Codex session 管理 | `ccs.py codex start role` |

### 3.2 core.py — 生命周期编排

**start() 流程（11 步）:**
1. 校验角色名合法性
2. 检查是否已运行（重复启动不创建）
3. 内存守卫（确认可用内存 ≥ 1000MB）
4. 创建工作空间 `~/ccs-workspaces/<name>`
5. 从 hermes-session-roles 加载角色定义
6. 注入角色知识到 workspace CLAUDE.md
7. 构建初始 role prompt
8. 启动 tmux session + claude 进程
9. 等待 claude 就绪（轮询 `❯` 提示符）
10. 注入 prompt
11. 写哨兵 + 启动守护线程

**stop():** 终止 tmux session + 清理哨兵
**send():** 发送消息前经过三源验证 + 敏感命令门禁
**health_check():** 返回所有 CCS 的存活状态

### 3.3 sentinel.py — 哨兵系统（创新点）

数据全部从 tmux sessions + 角色 JSON **实时派生**，无持久化 JSON 文件（健康状态写 `/tmp/ccs-health/{role}.json`）。

**数据结构:**

```python
@dataclass
class CcsSentinel:
    role: str
    title: str
    tmux_session: str
    pid: int | None
    started_at: float
    lifecycle: str
    partners: list[str]
    bus_track: str
    bus_timeout: int
    session_id: str
    engine: str           # "ccs" | "codex"
    health: CcsHealth

@dataclass
class CcsHealth:
    last_watchdog_check: float
    watchdog_ok: bool
    last_turn_check: float
    last_bus_msg_age: float
    restart_count: int
```

**兼容性:** `list_sentinels()` 自动发现所有 `ccs-*` 和 `cdx-*` 前缀的 tmux session。
**跨 session 内存:** 进程内的 `_CROSS_SESSION_MEMORY` dict，记录角色最近的跨 session 动作。

### 3.4 watchdog.py — 伙伴存活守护

**设计:** 每个 CCS 可以为多个伙伴启动守护线程。检测到伙伴 tmux session 死亡后，从哨兵读取上下文自动重启。

```python
start_watchdog(this_role, partner_role, auto_restart=False, interval=30)
```

**Auto-Continue 模式:** 当 CCS 无响应超过 120s 时发送 `/continue` 指令唤醒，而非直接重启，减少上下文丢失。

**重要逻辑:** `read_sentinel → delete_sentinel → start` 顺序关键：先读旧上下文再删旧哨兵，防止并发重启时哨兵膨胀。

### 3.5 tracker.py — 轮次追踪 + 死锁检测

**设计:** 定期 poll bus 某分类的最新时间戳，超时则发提醒。与 watchdog 交叉验证。

```python
start_tracker(this_role, bus_cat, timeout_sec=300, interval=10, partners=None)
```

**降级阶梯:**
1. 超时 → 检查自己是否是上一轮作者（是则跳过）
2. 超时 + 伙伴存活 → 发 bus notice 提醒伙伴
3. 超时 + 伙伴死亡 → 写审计日志（watchdog 负责重启）

### 3.6 ccs_socket.py — 直接通信

通过 `sister_bus_ccs.sock` 实现 CC-SC 间的直连通信（<1ms）。

```
SUBSCRIBE → 注册为 "ccs-{role}"
PUBLISH  → 按 JSON-over-newline 路由给目标
```

### 3.7 routing/roles.py — 角色禁区与权限

**角色禁区（Forbidden Zone）:**

| 角色 | 禁止操作 |
|------|---------|
| pg | run_tests, edit_config, deploy, start_ccs, edit_persona_json, write_other_workspace |
| qa | write_code, edit_config, deploy, edit_persona_json |
| coordinator | write_code, run_tests, deploy |
| product_architect | write_code, run_tests, deploy |

**唤醒权限:**

| 角色 | 可唤醒 |
|------|--------|
| coordinator, lr | 所有角色（`*`） |
| qa, pm, reviewer, product_architect | pg |
| pm, reviewer | qa |

### 3.8 routing/partner.py — 跨角色协作

三层架构（V1.1 COLLAB_ENHANCEMENT）:

- **Layer 1 — Confirm:** 双信号确认交付（task.status 变更 + bus 通知），超时 300s，含 5 级降级阶梯
- **Layer 2 — Status:** 完整的伙伴状态解析（哨兵 + tmux + workflow DB）
- **Layer 3 — Wake:** 强制发送消息，必要时自动唤醒

### 3.9 ecosystem_health.py — 统一健康检查

三项目统一健康检查 API，覆盖:

1. 目录存在性（三个项目根）
2. 模块导入（核心模块可加载）
3. Python 语法检查（AST 解析所有 .py 文件）
4. 角色验证（调用 validate_roles.py）
5. 路由表加载
6. 硬编码路径检测（`/home/administrator` 路径残留）
7. CCS 活跃会话数
8. 工作空间数量

## 4. 协作模式

| 模式 | CLI 参数 | 说明 |
|------|----------|------|
| 主从 | `--partner role --auto-restart` | 守护伙伴存活，挂了自动重启 |
| 对等 | `--bus-track category` | 追踪 bus 轮次，超时发死锁提醒 |
| 仲裁 | `--partner A --partner B --bus-track X` | 同时守护 + 追踪 |
| 直连 | `send-direct from to msg` | 不走 bus，<1ms Unix Socket |

## 5. Watcher 线程

CCS 启动后（detach 模式）自动启动的守护线程:

| 线程 | 来源 | 功能 |
|------|------|------|
| watchdog | `ops/watchdog.py` | 伙伴存活守护，interval=30s |
| tracker | `ops/tracker.py` | 轮次死锁检测，interval=10s |
| feed listener | `ops/runner.py` | 实时监听 bus 分类推送 |

## 6. 工作流系统

session-launcher 包含一套完整的工作流管理系统（主要由 routing/partner.py + workflow/ 实现）：

- **三层架构:** Template（可复用）→ Instance（执行）→ Task（目标）
- **状态机:** created → pending → running → completed / failed / cancelled
- **5 种步骤类型:** 由 step_engine.py 管理
- **模板验证门禁:** 10 字段 JSON Schema，5 步验证流程

## 7. 重要设计原则

1. **上下文隔离:** 每个角色独占独立 CLAUDE.md，分 L0-L3 四层（通用准则→红线→专业→经验）
2. **协作逻辑在代码层:** 不在 prompt 中写协作规则，由 ccs.py + partner.py 代码实现
3. **死锁超时 300s:** 超过 5 分钟 bus 无消息 → 发提醒；超过 15 分钟 → 升级给人
4. **连续启动间隔:** 至少 8s 间隔防资源竞争
5. **内存守卫:** 可用内存 < 1000MB 时拒绝启动新 CCS
