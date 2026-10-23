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

模块表以 `CLAUDE.md` 的「目录结构」为准（2026-10-22 复核：原树 17 个具名文件里 14 个已不存在，整段删除）。

### 3.1 ccs.py — CLI

顶层子命令（现取：`python3 src/ccs.py --help`）：
start, config, auto-send, roles, stop, status, send, output, dashboard, health, register,
p0draft, workspace, send-direct, wake, status-role, send-safe, codex, reload-knowledge。

### 3.2 core.py — 生命周期

**start()**：校验角色 → 查重（已在跑则不重复创建）→ `tmux_ops._memory_shortage` 闸门 →
建 `~/ccs-workspaces/<role>` → 读 persona → 注入 KNOWLEDGE 块 → 起 tmux + claude →
等提示符 → 发初始 prompt → 写哨兵 → 起 watchdog/tracker 线程与 feed_listener 子进程。

**stop()**：回收 watchdog/tracker 线程、按 pidfile 杀 feed_listener、kill tmux session、删哨兵；
`--dry-run` 只返回计划（`core.py` 的 `if dry_run:` 预检块，行号随改动漂，按 grep 定位）。`output`/`health` **没有** `--dry-run`：那两个参数
2026-10-22 已删——旧实现收下参数照常真执行，只是把输出换成 JSON，传它现在直接 argparse 报错。

**验证标准注入**（`routing/roles.py`）：persona 的 `eval_criteria` 只取**前 3 条**写进 workspace
CLAUDE.md 的「### 验证标准」（`[:3]`，与 pipeline 运行期 `CRITERIA_WINDOW=3` 同口径）。写到第 4 条
不会出现在角色上下文里，也不会被 eval_checker 执行——判据要按重要性排前面。

**send()**：跨角色路由门禁 → 红线 1 拦截 gateway 重启 → 忙判据 → `_tmux_send`；
tmux 探测失败按 3 连败（键含 instance_id）才写 `[ccs_send_fallback]`。

### 3.3 内存闸门（tmux_ops.py 的 `_memory_shortage`）

只有宿主机判据：`MemAvailable < 1000MB`，或 `swap_free < 128MB 且 avail < 1024MB`。连续启动间隔 8s。

> cgroup 闸门（`memory.current/memory.high ≥ 95%`）已于 2026-10-22 **删除**：它读的
> `system.slice/session-pipeline.service` 存在但 disabled/inactive，其 cgroup 路径从不出现（引擎在 `hermes.slice/workflow-engine.service`），
> OSError 被静默吞掉，从未生效；把路径修对也不成立 —— 同一个 slice 一天内采到
> 84% / 93~94% / 95.2%，负载一动就跨过阈值，命中与否取决于采样时刻。`Dl+` 卡死没有水位判据能抓，
> 抓它的是 pane 空白 + 无对话增量（`conversation_monitor` / `_looks_stuck`）。

### 3.4 ops/sentinel.py — 哨兵

**会落盘**（旧文档说不落盘是错的）：`~/.hermes/run/ccs-sentinels/<role>.json`（文件数 = 当时运行的角色数，现算 `ls ~/.hermes/run/ccs-sentinels | wc -l`），
健康字段**两处都写**：哨兵文件的 `health` 对象（`ops/sentinel.py:97`），以及
`~/.hermes/run/ccs-health/<role>.json`（`update_health` 写、`watchdog.py:128` 与 `tracker.py:86` 调，见 `ops/sentinel.py:380`；
pipeline `survival_monitor` 也往同一目录写 `survival_*`）。只看哨兵里的 `health` 会把没更新的默认值当真实健康状态。结构见 `CcsSentinel`（`:64`）与 `CcsHealth`（`:55`），
键包含 role / instance_id / tmux_session / pid / started_at / lifecycle / partners / bus_track / drive /
bus_timeout / session_id / engine / health。`ccs status` **读哨兵目录**（`core.py:868` 的 `list_sentinels()`），alive 才按 tmux 现查：输出条数 = 目录里的
哨兵文件数（含已死 session），不是存活数（实测同一时刻两者都是 11）。

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

### 3.10 探测成本与 tmux 客户端风暴（2026-10-22 实测）

- `status-role` 走 `routing/partner.py:resolve()`：`is_ccs_running`（`tmux has-session`）+ `_find_pid` 两次 tmux 往返（删 `_output` 之前是三次）。tmux 正常时 <0.1s；服务器卡死时每次顶到 5s，`status-role` 在卡死窗口实测 20~25s。
- pipeline `route_all_to_ccs` 对每个角色起一个 `ccs.py status-role` 子进程（10s 超时）→ 19 个角色 ≈ 190s/轮，journal 里成批 `status-role ... timed out after 10 seconds`。
- 12:45 起 `[ccls_daemon] tmux ls` 连续 30s 超时；12:46 起 `workflow.engine` 每角色 `has-session` 5s 超时；12:52 单次 `has-session` 实测 **101.8s**，随后服务器重启、`tmux ls` 归零，19 个 pane 全消失。
- 自愈：12:53:47–12:54:26 按需重建 11 个会话并刷新哨兵，中断约 1.5 分钟。`systemctl --user is-active` 全程无感 —— 只查 systemd 抓不到这类事故。
- 巡检一行：`time tmux list-sessions >/dev/null; tmux ls | wc -l`（>1s 或会话数骤降 = 风暴/重启）。
- 结论：`status-role` 超时是 tmux 卡死的症状，不是 launcher 的 bug；调大探测超时只会拉长停滞。要减的是「每角色一次 tmux 往返」的调用量（批量化/缓存），不是超时值。
- pipeline 侧按同一结论做了减法：`_check_and_start_role` / `_wait_for_role_ready` 里并存的 `tmux has-session` 全部删除，判据统一为 `liveness.tmux_snapshot()` 的 pane 快照（一处 `list-panes -a` 服务全部角色），两条调用点各少一次往返。见 `~/session-pipeline/docs/TECHNICAL_DEEP_DIVE.md` §8。
- 已删：`resolve()` 的 `last_active_sec` —— 它恒为 `0.0`/`-1.0`、全仓无消费者，却每次探测多花一次 `capture-pane`（tmux 卡死时就是 5s）。字段与 `_output` 包装一并删除，`status-role` 少一次 tmux 往返。

### 3.11 对话质量口径（`scripts/dialogue_quality.py`）

- 唯一真相是**按回合**统计：一条 user 字符串消息 → 下一条 user 字符串之前；`tool_result` 是 list，不算开局。块级统计已被证伪：2026-10-21 把 1,900 条「只带文本」的 assistant 块当低质量，复测 81.5% 是同回合紧接着调工具的过渡句，53% 的回合末块其实是网关错误。
- 跑：`python3 scripts/dialogue_quality.py --hours 6`（默认 6h/24h 两档）；`--self-check` 只验口径不读转录；`--cut <ISO>` 指定起点；`--bus` 额外写一条 `performance` fact。
- 自触发：systemd user `dialogue-quality.timer` 每天 09:10 跑 24h 档，产物落 `~/.hermes/reports/dialogue_quality/<日期>.txt`（journal 里也有）。只有 print 的报告没人看，等于量了没用。
- 三个分母分开看：零工具回合率（该回合没有任何 `tool_use`）、回合末文本的「无证据」数、网关错误按端点分布。混算会把网关问题记成角色问题 —— 这是上一版判据犯的错。
- 另一种口径（不是分母）是**产物行**：`~/ccs-workspaces/<role>` 里**窗口内被改过的文件的全文行数**（按内容 md5 去重，按 `\n` 计数所以无尾换行的文件少 1 行；跳过 `CLAUDE.md`/`TASKS.md`、`tmp*`/`.tmp`/`.archive` 草稿、`.bak` 备份、日志/缓存后缀，含 NUL 的二进制整文件不计）。回合率只回答「说了什么」，回答不了「留下什么」。
- 产物行对**行动型角色**会误判：2026-10-22 实测 24h `devops` 0 行产物、却写了 12 条 bus —— 它的产出是重启/路由/裁决这类动作。所以每角色行同时给 `bus=` 计数。读法：0 行 + 有 bus = 产出在 bus 没有文件；**0 行 + 0 bus 只说明没有可核对的落盘产出** —— 活可能干在代码仓里（`codex-dev` 24h 就是 0 行 0 bus）。
- 已知偏差（写在代码注释里）：窗口按文件 mtime 切，`cp -p`/解包保留旧 mtime 会漏计（实测 writer 少 46%），老文件在窗口内改一次则整篇计入（`qa/test_cases.md` 只因一次写入就整篇入账）；巡检型角色爱把报告写成 `.out`（`maintainer` 24h 有 47 个、3,627 行），其中 3,604 行在 `tmp/` 下 —— 挡住它们的是 `tmp*` 规则，不是 `.out` 规则。
- 读法边界（2026-10-22 实测）：同 cut 40 分钟内容量摆动 ±7%，所以**只比同一角色、同长度窗口、幅度 >15% 的差异**；不同角色之间不可横比（跳过清单对不同角色影响差 70%）。表只列零工具回合最多的 8 个角色，未列角色约占产物行 43%，别把「没出现在表里」读成没产出。
- 数字随运行态变化，引用必须带 `--hours`/`--cut`，不要抄本节或别处的具体值。

### 3.12 死角色按定向派单唤醒（`ccs reap` / `src/role_reaper.py`）

- 缺口：派单只投**已运行**的 pane（`route_to_ccs`），系统类派单不 mint workflow，而 `ccs start` 死 pane 的唯一调用者是工作流引擎 → `ondemand` 角色 pane 一死就再无派单可投。2026-10-22 实测 `devops` 死 10.5h，43 条 `@devops` 未消费，其中 17 条是引擎写的 task_spec 派单。
- 判据：`lifecycle == ondemand` + pane 不在（`alive` 假**且** `instances` 空，否则 `ccs-<role>-1` 在跑时会再起一个同名 consumer）+ 有 `@role` 事实年龄在 **[10min, 6h]** + 该事实不是 `notice`、不是角色自写（`src == role`）。infinite 角色被停是运维事件，顺手拉起会掩盖它。
- **收件人 = 首行出现 @role，位置不限**。这是实测唯一可行的口径：机器派单 `needs_implementation @pg 工作流任务: wf_x`、协调者派单 `[coordinator] 派单 @engineer: …`、链式派单 `[@a→@b] …` 三处收件人位置各不相同，而 bus 的 `tags` 列 24h 内 751/765 为空 —— 没有结构化收件人可读。代价是提及也会唤醒；过唤醒由窗口+冷却+cap 三道兜底，漏唤醒没有兜底。口径与 pipeline `_is_for_me` 一致（全文 @），不另立第三套「定向」定义。
- `notice` 与自写必须排掉：24h 定向积压 40 条里 26 条是角色自己的 `[context_warn]`/`[eval_checker]` 告警，启动 pane 去读自己的告警纯属空转。
- 读未消费必须 `bb.unconsumed(consumer=role, limit=0)`：该查询按 `ts DESC` 建序，加 `LIMIT` 会让近期广播（实测 >100 条）把真正等待唤醒的旧派单挤出窗口。回归测试用「120 条新广播 + 1 条 5h 派单」钉住。
- 预算：单轮最多启动 2 个、按积压条数降序（cap 只该挡「凑巧靠前」的，不该挡最堵的）、同角色 30min 冷却（`~/.hermes/run/wake_cooldown.json`，tmp+`os.replace` 原子写）。**失败也记冷却并写 `monitor_audit`**：否则起不来的角色每 15 分钟重试、bus 零痕迹；`ccs reap` 遇 `start_failed` 退出码 1，让 systemd 记成 failed 而不是每 15 分钟绿一次。
- `started` ≠ 角色可用：`wake_ccs` 的 success 只代表 tmux session 建成，`core.start` 就绪轮询超时同样返回成功、claude 卡登录时 pane 也 alive。审计因此写「已启动 pane」而不是「已唤起角色」。
- 权限：`_WAKE_PERMISSION_MAP["*"]` 加 `reaper`。没有这一位时真实首跑返回 `权限不足: reaper 无权启动 ccs-monitor` —— 唤醒门禁对**所有**非 coordinator/lr 主体关闭，包括 pipeline 自己。
- 实测产出（2026-10-23 12:54 只读测量，30 天窗口）：死角色 7 个、定向未消费 40 条（ccs-monitor 26 / devops 14），**其中落在 [10min,6h] 的 0 条** —— 阈值在当前数据上不咬人，它是安全网不是清道夫；40/40 都已被别的 consumer 标记 consume（广播式 claim ≠ 送达），老账最老 6 天。放宽到 24h 只多 1 个角色，收益近零。
- 实战 2026-10-22 22:30：`ccs reap` 真跑 → 启动 `ccs-monitor`（1 条 220min 未消费派单，该派单已被 pipeline claim 未投递）→ pane 建立 + cooldown 落盘 + bus `#443579`。该 pane 在 02:20–05:19 的批量重启中被换掉（现网 12 个 pane 全部创建于该时段），不是唤醒链路的问题。
- 自触发与回滚：`ccs-reap.timer`（15 分钟，`OnBootSec=5min` + `OnUnitActiveSec=15min`）跑 `ccs-reap.service`（`ExecStart=%h/session-launcher/src/ccs.py reap`，即 `ccs` 包装脚本指向的那个生产 checkout）。回滚 = `systemctl --user disable --now ccs-reap.timer` + 删 `~/.hermes/run/wake_cooldown.json`；只 revert 代码停不掉唤醒，因为触发在仓库外的 user unit 里。

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
3. 轮次死锁判据 300s（`tracker.py` 的 `timeout_sec`，另有 1800s 节流）：伙伴还活着就向它的 bus cat 写「请继续」，伙伴已死只写 `monitor_audit`；**面向人**的升级在 `partner.py`（300s 存活但不接单 → 「需要人工介入」）。没有 15 分钟档。
   前提：tracker 只在 `--partner` 下启动（`core.py` 的 `if bus_track and partners:`），该参数已退场 → 现网这条链不跑。
4. 连续启动间隔 8s；启动前只有宿主机内存判据，见 §3.3。
