# PUBLIC_API — session-launcher

> 实测基准：分支 `r77-routing-daemon-whitelist`（2026-10-22）。可执行验证：
> `PYTHONPATH=src python3 -c "import core, tmux_ops, codex_ops, routing.roles, routing.gatekeeper, workflow.client; print('OK')"`

## Direction

session-pipeline → **session-launcher** → hermes-session-roles（定义层只读）。

launcher → pipeline 是已知的反向引用，全部是路径常量，不是业务调用：
- `paths.py` 的 `SESSION_PIPELINE_SRC = ~/session-pipeline/src`
- `ecosystem_health.py` **不用**它：自己定义 `PIPELINE = HOME / "session-pipeline"`（按名检索 `PIPELINE =`）
- `wf.py` 的 `templates` 子命令 `from template_registry import TemplateRegistry`

## Subpackage Structure

```
src/
├── ccs.py                       # CLI 入口（19 个顶层子命令，argparse）
├── core.py                      # 生命周期编排（start/stop/status/send/health/dashboard）
├── tmux_ops.py                  # tmux 底层（_is_alive / _tmux_send / _role_is_busy）
├── ccs_socket.py                # CCSClient：Unix Socket 直连
├── codex_ops.py                 # Codex session 管理
├── paths.py                     # BUS_CLIENT / SISTER_BUS_*_SOCK / CCS_CLI / SESSION_PIPELINE_SRC
├── ecosystem_health.py          # 三项目健康检查（--json / --ci）
├── file_size_audit.py           # 文件大小审计
├── gateway_guard.py             # 红线 1：拦截 gateway 重启指令
├── user_intent_reminder.py      # classify_intent / inject_reminder
├── ccls_daemon_intent_classifier.py  # 意图守护 daemon（--selfcheck）
├── workflow_client.py           # 薄包装：re-export workflow.client + check()
├── wf.py                        # wf CLI
├── ops/                         # sentinel watchdog tracker runner validators workspace ccs_config
├── routing/                     # roles gatekeeper partner
└── workflow/                    # client db schema
```

CLI 子命令（现取：`python3 src/ccs.py --help`）：
start, config, auto-send, roles, stop, status, send, output, dashboard, health,
register, p0draft, workspace, send-direct, wake, status-role, send-safe, codex, reload-knowledge。

wf 子命令（`src/wf.py`）：check, create, complete, fail, notify, task, state, logs,
cancel, confirm, templates, my, ls, kanban, cleanup, stats。

## WorkflowClient（实现于 `workflow/client.py`，`workflow_client.py` 与 `wf.py` 转导出）

公开方法：`create_task_v2` `get_task` `state` `list_tasks` `list_my_tasks` `check_task`
`complete` `fail` `cancel` `confirm` `get_logs` `find_zombies` `kanban_board`
`workflow_stats` `notify` `get` `close`（行号见 `grep -n '    def ' src/workflow/client.py`）。

- `complete(wf_id, summary)`：已终态则返回 `None`（空操作，不复活实例）；不再签发 approval_token（2026-10-22 退场）。
- `confirm(wf_id, step_id, token="", approved=True, reason="")`：审批**按该步审批人身份**鉴权（`session-pipeline/src/lifecycle/manager.py:confirm`）；`token` 只是兼容形参，pipeline 不读它（密钥 2026-10-22 退场），`wf confirm` CLI 已无 `--token`。

## Sentinel（`ops/sentinel.py`）

- `sentinel.py:CcsHealth`、`sentinel.py:CcsSentinel` 两个 dataclass（都在 `src/ops/sentinel.py`）。
- `sentinel.py:SENTINEL_DIR` 指向 `~/.hermes/run/ccs-sentinels`，健康字段另落 `~/.hermes/run/ccs-health/`。
- 函数：`write_sentinel` / `read_sentinel` / `list_sentinels` / `delete_sentinel` / `update_health`（行号会漂，现取 `grep -n '^def ' src/ops/sentinel.py`）。

## CrossRoleRouter（`routing/gatekeeper.py`）

`intercept(source, target, message) -> bool`：工作群组矩阵 → 每小时 5 条红消息限流 → 三源验证。

## CCSClient（`ccs_socket.py:34`）

SUBSCRIBE/PUBLISH/PING/STATS，JSON-over-newline。**无认证**：同机进程可任意 PUBLISH。
原 `stream` 方法已删（`be758a1`）；要 tail 用 `ccs.py output --tail`。

## 已不存在，勿引用

`src/sentinel.py`、`src/migration_scripts.py`、`src/migration/`、`src/events/`、`src/lifecycle/`、`src/ops/mcp_settings.py`
（`ls src/` 均无；`events/` 于 `1ba44dc` 删，`role_manager.py`/`lifecycle_manager.py` 于 `f6c19ae` 删）。
LifecycleManager 在 session-pipeline 的 `lifecycle/manager.py`，launcher 侧没有重导出。
