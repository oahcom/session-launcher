# Session Launcher（CCS 生命周期管理器）

## 定位

Session 生态的**执行层**——创建 CCS（Claude Code Session）进程、管理生命周期、提供跨 session 协作基础设施。

**核心职责：让每个专业角色独占一个独立进程、一个独立上下文、一颗独立循环，互不污染。**

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
| prompt_refs 指向的 md 缺失或为空 | 角色拿不到实际提示词 | 检查 personas 的 prompt_refs 文件存在且非空 |
| prompt 中包含协作逻辑 | AI 会自由解释协作规则 | 扫描 prompt 中 key words 是否含协作相关 |

---

## 项目结构

```
src/                                   （ls src 实测）
  # ── CCS 生命周期 ──
  ├── ccs.py                CLI 主入口（19 个顶层子命令）
  ├── core.py               生命周期编排（start/stop/status/send/health/dashboard）
  ├── tmux_ops.py           tmux 底层（_is_alive/_tmux_send/_role_is_busy）
  ├── ccs_socket.py         CCSClient：Unix Socket 直连
  ├── codex_ops.py          Codex session 管理（start/exec/status）
  │
  # ── 基础设施 ──
  ├── ops/sentinel.py       哨兵文件（~/.hermes/run/ccs-sentinels/ + ccs-health/）
  ├── ops/watchdog.py       伙伴存活守护线程（自动重启）
  ├── ops/tracker.py        轮次追踪 + 死锁检测
  ├── ops/runner.py         仪表板（dashboard）
  ├── ops/ccs_config.py     全局配置中心（ccs_config.json）
  ├── ops/validators.py     参数校验
  ├── ops/workspace.py      工作空间管理
  │
  # ── 路由与门禁 ──
  ├── routing/roles.py      角色加载 + workspace 注入 + 禁区映射
  ├── routing/gatekeeper.py 三源验证 + 敏感操作门禁 + 工作群组
  ├── routing/partner.py    伙伴客户端
  │
  # ── 工作流 ──
  ├── workflow/client.py    WorkflowClient（wf.py 的实现在此）
  ├── workflow/db.py        DB 连接
  ├── workflow/schema.py    Schema
  ├── workflow/state.py      chain_state（client.py:369 引用）
  ├── wf.py                 wf CLI（check/create/complete/confirm/…）
  ├── workflow_client.py    薄包装：re-export + check()
  │
  # ── 其他 ──
  ├── paths.py              路径常量（BUS_CLIENT/SISTER_BUS_*_SOCK/CCS_CLI）
  ├── ecosystem_health.py   三项目健康检查（--json/--ci）
  ├── gateway_guard.py      红线 1：拦截 gateway 重启指令
  ├── user_intent_reminder.py + ccls_daemon_intent_classifier.py  意图提醒与守护
  └── file_size_audit.py    文件大小审计
```

---

## 核心功能

### 1. CCS 生命周期管理

```bash
# 创建并启动 CCS
python3 src/ccs.py start <role> [title] [--no-attach] [--prompt "msg"]
python3 src/ccs.py start <role> --partner <p> --auto-restart --bus-track <cat>
python3 src/ccs.py start <role> --drive cron|ondemand|goal|infinite --feed-cat <category>

# 控制
python3 src/ccs.py stop <role>
python3 src/ccs.py send <role> "message"
python3 src/ccs.py output <role> [--tail N]

# 状态
python3 src/ccs.py status
python3 src/ccs.py health [role]          # 健康检查
python3 src/ccs.py register <role> <tmux> # 注册手动 tmux 为 CCS
```

### 2. 守护进程 (systemd 托管)

| 单元 | 职责 | 状态检查 |
|------|------|---------|
| `ccs-reload-knowledge.timer` | 每 10 分钟重注入角色 KNOWLEDGE 块（不带 `--restart-idle`，不重启进程） | `systemctl --user is-active ccs-reload-knowledge.timer` |
| `ccs-intent-guardian.service` | 用户意图守护：对 ccs 的疑问/任务类输入注入两条自检提醒（至多 3 次；角色回过一轮即停，R123）（"你真解决了用户问题吗？"+"你真严格按用户提示完成了吗？"），防止答非所问 | `systemctl --user is-active ccs-intent-guardian.service` |

**意图判定逻辑**：`src/user_intent_reminder.py` 的 `classify_intent` 将输入分为 `QUESTION`/`TASK`/`OTHER`；仅疑问/任务类在 `core.send` 路径经 `inject_reminder` 注入提醒（`pipeline`/`daemon` 自动化来源排除，防递归自提醒）。守护类 `IntentReminderDaemon`（`src/ccls_daemon_intent_classifier.py`）从 systemd 自启动，验证：`python3 src/ccls_daemon_intent_classifier.py --selfcheck`（11/11 PASS）。

### 3. 哨兵文件

每个 CCS 在 `~/.hermes/run/ccs-sentinels/<role>.json` 写入运行状态（健康字段另落 `~/.hermes/run/ccs-health/<role>.json`）：

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

### 4. 协作模式

| 模式 | 命令 | 说明 |
|------|------|------|
| 主从 | `--partner <role> --auto-restart` | 守护伙伴存活，挂了自动重启 |
| 对等 | `--bus-track <category>` | 追踪 bus 轮次，超时发死锁提醒 |
| 仲裁 | `--partner A --partner B --bus-track X` | 同时守护 + 追踪 |

### 4. Bus Push 实时推送

```
bus_client.py write debate "消息"
  └─ INSERT INTO facts (SQLite)        ← 唯一路径

显式 PUBLISH 才推 socket（hermes_bus/socket_server.py:144-152）
  └─ /tmp/sister_bus_feed.sock → 订阅者（feed_listener.py）
```

**实测**：写入路径没有推送调用——`bus_client.py` 里无 `PUBLISH`，`hermes_bus` 里无 `_notify_feed`
（`grep -rn '_notify_feed' ~/.hermes/scripts` 无命中）。所以「写完自动推送」这条链路**未实现**，
feed_listener 只有在有人显式 `publish('feed', …)` 时才收到消息；当前无调用方（未验证实际到达率）。
SQLite 写入与 socket 推送彼此独立：push 不可用不影响写入。

**写入报 `database is locked`（读取正常）**：不是并发高，是某个进程持有未提交的写事务把 WAL 写锁占死。
定位：`ino=$(stat -c %i ~/.hermes/sister_bus/blackboard.db-shm); grep ":$ino" /proc/locks | grep WRITE` → 取 pid → `ps -o args= -p <pid>`。
处置：`systemctl --user restart session-pipeline-daemon.service`（实测锁立即释放，写入 0.07s）。

---

## 跨项目接口

session-launcher 被 session-pipeline 通过 subprocess 调用以向 CCS 发消息：

```
session-pipeline/src/routing/routes.py → subprocess run([sys.executable, CCS_CLI, "send", role, message])
```

**注意**：这是 pipeline→launcher 的唯一调用路径。launcher 不反向调用 pipeline。

`CCS_CLI` 定义在 `src/paths.py:46`（`SESSION_LAUNCHER_SRC / "ccs.py"`，本地定义，非从 `hermes_bus.config` 导入）；pipeline 侧另有自己的 `CCS_CLI`。

**`wf complete <wf_id> --summary` 是角色与引擎的唯一一次性交接**：实测三天 497 个 completed 步骤里 32% 连 `exit_messages` 都没有，`--summary` 默认 `done` 会被引擎当成该步的全部产出。摘要里带证据（命令 + 输出摘要 / 路径 / exit code），否则下游只能拍脑袋判「做完了还是建成即盖戳」。

### MCP 隔离

launcher 在启动 CCS 时为每个角色写独立的 `.claude/settings.json`，实现 MCP 按角色隔离：

```python
# core.py: _write_mcp_settings()
# 从 persona JSON 读取 mcp_servers 字段
# 全局 mcpServers 全量继承，再按 persona 的 mcp_servers 增补（不是只开声明的）
```

---

## 验证

```bash
python3 -m pytest tests/ -q          # 全 passed 即基线（数量不写死：并发改动会增减）
python3 src/ecosystem_health.py --json   # 全 true 即健康；兄弟仓改代码期间失败项集中在 pipeline 侧 import
python3 src/ccs.py health
```

`tests/test_wl_selfcheck.py` 断言的是本仓 `src/workflow/client.py` 里的 `kanban_board` /
`_validate_create_task`（2026-10-22 修：原断言去已不存在的 `pipeline_workflow/client.py` 找，
4 条全假失败；Gate 已内联为 `_validate_create_task`）。

## 提示词生效方式

`core.py:_launch_tmux` 起的是
`claude --model 9router_hermes --tools Bash,Edit,Read,Write,Agent,WebSearch,WebFetch,Skill --effort high`，
workspace 的 `CLAUDE.md` 靠 claude 的 auto-discovery 加载（cwd 就是 `~/ccs-workspaces/<role>`）。
不加 `--bare`：它只暴露 Bash/Edit/Read，Task/Agent、WebSearch、Write、Skill 全部消失。
也不加 `--append-system-prompt-file`：与 auto-discovery 同时给会把同一份 prompt（实测 15–40KB/角色）注入两次，
而严格探针（`--tools ""` 禁工具、排除模型自读文件）下发现式加载能逐字复述文件中段小节，两者等价。
实测一次：`/clear` 之后角色仍背出 9 小时前的旧文案，重启进程后才答出新文案（n=1，未验证
claude 内部是否缓存该文件——不要当成通用结论，要重测再改写本节）。

**两件不同的事，别混**：

| 变更 | 生效方式 |
|---|---|
| prompt 内容（`CLAUDE.md` 被重写） | 杀 claude，wrapper 3s 内重拉即重读。`ccs reload-knowledge --restart-idle` 就是这条：进程启动早于 CLAUDE.md mtime 且 pane 空闲的角色直接重启，忙的跳过。**别挂 timer**——prompt 一改所有空闲角色都该重启一次，10 分钟定时等于定期清空上下文 |
| 启动参数（flags） | 杀 claude **没用**：argv 烤死在 pane 的 `bash -c` 字符串里，重拉还是旧参数（实测 reviewer：重启后新 pid 的 cmdline 仍带 `--bare`）。必须重建 session：`kill` claude → `tmux kill-session` → `ccs start <role> --no-attach` |

切换启动参数的完整序列（子代理实测可复制；第 4 步是提交点）：

```bash
cd ~/session-launcher
# 1 复核空闲：必须 0 命中
tmux capture-pane -p -J -t ccs-<R> | tail -8 | grep -c "esc to interrupt\|Retrying attempt"
# 2 内存闸门：MemAvailable > 1229MB
free -m
# 3 找该角色的 claude pid（role = cwd basename）
for p in $(pgrep -x claude); do [ "$(basename "$(readlink /proc/$p/cwd)")" = "<R>" ] && pid=$p; done
# 4 先杀 claude、立刻杀 session（包装器 sleep 3 是唯一窗口，别超过）
kill -9 "$pid"; tmux kill-session -t ccs-<R>
# 5 重建：注册表内角色
PYTHONPATH=src timeout 200 python3 -m ccs start <R> --no-attach
#    注册表外角色（archivist/curator/codex-dev 等，ccs start 报「角色不在注册表中」）
PYTHONPATH=src python3 -c "from core import _launch_tmux; from pathlib import Path; print(_launch_tmux('ccs-<R>', Path.home()/'ccs-workspaces'/'<R>', role='<R>'))"
PYTHONPATH=src python3 -m ccs register <R> ccs-<R>
# 6 验证：cmdline 含 --tools + session 存活 + free -m
```

不要用 `ccs stop`：它的 `tmux kill-session` 有 5s 超时，超时后会留下孤儿 claude。
判断空闲要看 pane 同时没有 `esc to interrupt` **和** `Retrying attempt`——只查前者会在 429 重试时误发按键。

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| SESSION_ROLES_ROOT | ~/hermes-session-roles | 角色定义目录（非此路径时注入默认拒绝） |
| ALLOW_ROLES_WORKTREE_INJECT | (未设置) | 设为 1/true/yes 才允许从 worktree 注入生产 prompt |
| SESSION_PIPELINE_SRC | ~/session-pipeline/src | Pipeline 源码目录 |
