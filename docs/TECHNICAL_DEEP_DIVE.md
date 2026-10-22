# Session Launcher — Technical Deep Dive

> 2026-10-22 按代码复核（分支 `r77-routing-daemon-whitelist`）。旧版本列的 `event_log.py`、`signal_parser.py`、
> `worker_pool.py`、`pool_cli.py`、`lesson_injector.py`、`events/`、`lifecycle/`、`migration/`、`routing/router.py`、
> `routing/gateway.py`、`workflow/gateway.py`、`workflow/utils.py`、`ecosystem_cli.py` 均已不存在，
> 本次按 `ls src/` 重写。

## 1. 定位

Session 生态的执行层：起 CCS（Claude Code Session）进程、管生命周期、提供跨 session 协作基础设施。
每个角色独占一个进程、一个上下文、一颗独立循环。

## 2. 实际模块

```
src/
├── ccs.py                    # CLI（子命令见 --help）
├── core.py                   # 生命周期编排（行数现算：wc -l src/core.py）
├── tmux_ops.py               # tmux 底层 + 内存闸门 + 忙判据
├── ccs_socket.py             # CCSClient（Unix Socket 直连）
├── codex_ops.py              # Codex session
├── paths.py                  # 路径常量
├── ecosystem_health.py       # 三项目健康检查（--json / --ci）
├── file_size_audit.py        # 文件大小审计
├── gateway_guard.py          # 红线 1 拦截
├── user_intent_reminder.py   # 意图提醒（classify_intent/inject_reminder）
├── ccls_daemon_intent_classifier.py  # 意图守护 daemon
├── wf.py / workflow_client.py        # wf CLI / 薄包装
├── ops/  sentinel watchdog tracker runner validators workspace ccs_config
├── routing/  roles gatekeeper partner
└── workflow/ client db schema
```

## 3. 核心模块

### 3.1 ccs.py — CLI

顶层子命令（现取：`python3 src/ccs.py --help`）：
start, config, auto-send, roles, stop, status, send, output, dashboard, health, register,
p0draft, workspace, send-direct, wake, status-role, send-safe, codex, reload-knowledge。

### 3.2 core.py — 生命周期

**start()**：校验角色 → 查重（已在跑则不重复创建）→ `tmux_ops._memory_shortage` 闸门 →
建 `~/ccs-workspaces/<role>` → 读 persona → 注入 KNOWLEDGE 块 → 起 tmux + claude →
等提示符 → 发初始 prompt → 写哨兵 → 起 watchdog/tracker 线程与 feed_listener 子进程。

**stop()**：回收 watchdog/tracker 线程、按 pidfile 杀 feed_listener、kill tmux session、删哨兵；
`--dry-run` 只返回计划（`core.py` 的 `if dry_run:` 预检块，行号随改动漂，按 grep 定位）。

**send()**：跨角色路由门禁 → 红线 1 拦截 gateway 重启 → 忙判据 → `_tmux_send`；
tmux 探测失败按 3 连败（键含 instance_id）才写 `[ccs_send_fallback]`。

### 3.3 内存闸门（tmux_ops.py 的 `_memory_shortage`）

只有宿主机判据：`MemAvailable < 1000MB`，或 `swap_free < 128MB 且 avail < 1024MB`。连续启动间隔 8s。

> cgroup 闸门（`memory.current/memory.high ≥ 95%`）已于 2026-10-22 **删除**：它读的
> `system.slice/session-pipeline.service` 存在但 disabled/inactive，其 cgroup 路径从不出现（引擎在 `hermes.slice/workflow-engine.service`），
> OSError 被静默吞掉，从未生效；而把路径修对后 hermes.slice 长期 ≥95%（实测 6.13/6.44GB），
> 会拒掉每一次启动。`Dl+` 卡死的判据是 pane/进程状态（liveness + 对话监控），不是水位百分比。

### 3.4 ops/sentinel.py — 哨兵

**会落盘**（旧文档说不落盘是错的）：`~/.hermes/run/ccs-sentinels/<role>.json`（文件数 = 当时运行的角色数，现算 `ls ~/.hermes/run/ccs-sentinels | wc -l`），
健康字段另落 `~/.hermes/run/ccs-health/<role>.json`。结构见 `CcsSentinel`（`:64`）与 `CcsHealth`（`:55`），
键包含 role / instance_id / tmux_session / pid / started_at / lifecycle / partners / bus_track / drive /
bus_timeout / session_id / engine / health。

### 3.5 ops/watchdog.py — 伙伴存活守护

`start_watchdog(this_role, partner_role, auto_restart=False, interval=30)`。
伙伴 tmux 存活但 120s 无输出 → 先发 `/continue` 温和唤醒（`check_auto_continue`，自带节流），
真死才重启；顺序是 `read_sentinel → delete → start`，防并发重启把哨兵写膨胀。

### 3.6 ops/tracker.py — 轮次追踪

`start_tracker(this_role, bus_cat, timeout_sec=300, interval=10, partners=None)`：
轮询 bus 最新时间戳，超时提醒；与 watchdog 交叉验证。

### 3.7 ccs_socket.py — 直连

`CCSClient`（`:34`）：SUBSCRIBE/PUBLISH，JSON-over-newline，走 `/tmp/sister_bus_ccs.sock`（该文件当前不存在、无监听，见 §4 直连行）。
无认证——同机进程可任意 PUBLISH。

### 3.8 routing/ — 角色、门禁、伙伴

- `roles.py`：persona 加载 + workspace KNOWLEDGE 注入 + 禁区校验。
- `gatekeeper.py`：工作群组矩阵（`setdefault` 保留内置 `"*"`）→ 每小时 5 条红消息限流 → 三源验证。
- `partner.py`：伙伴客户端（confirm/status/wake 三层）。
- 禁区与唤醒权限表的唯一出处是 `routing/roles.py` 的 `_FORBIDDEN_MAP`（:59）/ `_WAKE_PERMISSION_MAP`（:96），`core.py` 只是从 roles 导入，本文件不复制。

### 3.9 ecosystem_health.py

`--json` 输出 `summary` 与逐项 `checks`（现取：`python3 src/ecosystem_health.py --json`）；`--ci` 任一检查失败即 exit 1。覆盖三项目目录、模块导入、语法、
角色校验、路由表、硬编码路径、CCS 会话数、工作空间数（条数随运行态变化，别引用具体数）。

## 4. 协作模式（参数保留，能力已退场）

| 模式 | CLI | 说明 |
|------|-----|------|
| 主从 | `--partner role --auto-restart` | 参数仍被接受，但 watchdog 是 CLI 进程内的 daemon 线程：`start()` 返回即消失 |
| 对等 | `--bus-track category` | 同上（tracker 线程），轮次追踪实际由 pipeline 模板的 exit_condition 承担 |
| 仲裁 | `--partner A --partner B --bus-track X` | 同上 |
| 直连 | `send-direct from to msg` | 需要 `/tmp/sister_bus_ccs.sock`；该文件不存在、无监听（`ls /tmp/sister_bus_*` 只有 cron/dkk/feed/ssk） |

## 5. 工作流

`workflow/client.py`（`WorkflowClient`）是唯一实现，`wf.py` 与 `workflow_client.py` 只是入口；
模板/实例/任务的定义与状态机在 session-pipeline（`template_registry.py`、`lifecycle/manager.py`、
`pipeflow/`）。本仓没有 `step_engine.py`；`confirm()` 的 `token` 只是兼容形参（pipeline `confirm_step` 不读，审批按 assigner 身份），`wf confirm` 已无 `--token`。

## 6. 设计原则

1. 上下文隔离：每角色独占独立 CLAUDE.md（L0 通用→L1 红线→L2 专业→L3 经验）。
2. 协作逻辑在代码层，不写进 prompt。
3. 死锁 300s 提醒；同一角色 30 分钟内不重复（`tracker.py:98` 的 1800s 节流）。**没有更高级的自动升级档**——跨角色升级走 pipeline 模板的超时链。
4. 连续启动间隔 8s；内存/cgroup 闸门见 §3.3。
