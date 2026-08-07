# PUBLIC_API — session-launcher

## Public API

> **位置**: `~/session-launcher/src/`

## Direction

session-pipeline → **session-launcher** → hermes-session-roles (单向)

任何反向依赖（launcher→pipeline, roles→launcher, roles→pipeline）标记为 debt。

## Dependencies

## Subpackage Structure

```
src/
├── ccs.py              # CLI 入口（start/stop/send/status/output）
├── ccs_socket.py       # CCS 直接通信（Unix Socket: CCSClient / CCSStreamer）
├── core.py             # 生命周期编排（start/send/stop/health）
├── launcher.py         # Launcher 编排入口
├── lifecycle_manager.py# CCS 生命周期钩子（complete_step/fail_step/confirm_step）
├── paths.py            # 路径常量
├── role_manager.py     # 角色管理
├── sentinel.py         # 根目录哨兵（跨会话动作记录）
├── tmux_ops.py         # tmux 操作封装
├── codex_ops.py        # Codex CLI 集成
├── ecosystem_health.py # 三项目健康检查
├── migration_scripts.py# 迁移脚本
├── wf.py               # CLI 快捷入口
├── workflow_client.py  # WorkflowClient — CCS 角色工作流客户端
├── events/             # 信号/解析子系统
│   ├── signals.py      # 信号常量 + 所有 input_signals 检查逻辑
│   └── parser.py       # 信号解析器（signal_parser）
├── ops/                # 基础设施
│   ├── sentinel.py     # CcsSentinel 哨兵文件读写
│   ├── ccs_config.py   # 配置中心
│   ├── mcp_settings.py # MCP 设置
│   ├── runner.py       # Runner 执行器
│   ├── tracker.py      # 轮次追踪
│   ├── validators.py   # 参数校验
│   ├── watchdog.py     # 伙伴存活守护
│   └── workspace.py    # Workspace 管理
├── routing/            # 跨角色路由
│   ├── gatekeeper.py   # 门禁系统 + CrossRoleRouter
│   ├── partner.py      # PartnerClient
│   └── roles.py        # RoleManager（角色加载 + workspace 注入 + 权限）
├── workflow/           # 工作流客户端封装
│   ├── client.py       # WorkflowClient 类
│   ├── db.py           # DB 连接
│   └── schema.py       # Schema 定义
└── migration/          # 迁移脚本
    └── scripts.py
```

## WorkflowClient (`workflow_client.py`)

被 pipeline/composite_runner 及所有 CCS agent 消费。

| 方法 | 参数 | 返回 | 说明 |
|------|------|------|------|
| `check_task()` | — | `Optional[dict]` | 查当前角色待办任务 |
| `create_task_v2(title, assignee, template_id, initiator_role, description="")` | str×4+str? | `(task_id, wf_id)` | 创建任务+工作流（标题质量门禁） |
| `complete(wf_id, summary, files=None)` | str, str, list? | `None` | 标记完成（防重闭合守卫） |
| `fail(wf_id, reason)` | str, str | `None` | 标记失败 |
| `get_logs(wf_id=None, task_id=None)` | str?, str? | `list` | 查日志 |
| `cancel(wf_id, reason="")` | str, str? | `None` | 取消 |
| `notify(category, title, evidence="")` | str, str, str? | `None` | 发 bus 通知（subprocess 调 bus_client） |

## LifecycleManager (`lifecycle_manager.py`)

backward-compat 重导出，实际实现位于 `session-pipeline/src/lifecycle/manager.py`。

| 方法 | 参数 | 返回 |
|------|------|------|
| `complete_step(wf_id, step_id)` | str, str | `str` |
| `fail_step(wf_id, step_id, reason)` | str, str, str | `str` |
| `confirm_step(wf_id, step_id)` | str, str | `str` |

## CrossRoleRouter (`routing/gatekeeper.py`)

| 方法 | 参数 | 返回 |
|------|------|------|
| `intercept(source, target, message)` | str, str, str | `bool` |

## Sentinel (`ops/sentinel.py`)

| 函数 | 参数 | 返回 |
|------|------|------|
| `write_sentinel(s)` | `CcsSentinel` | `Path` |
| `read_sentinel(key_or_role, instance_id=0)` | str, int? | `Optional[CcsSentinel]` |
| `list_sentinels()` | — | `list[CcsSentinel]` |
| `delete_sentinel(key_or_role, instance_id=0)` | str, int? | `bool` |
| `update_health(role, instance_id=0, **kwargs)` | str, int? | `bool` |

## LifecycleSentinel (`core.py`)

生命周期哨兵写入/超时检查/清理（内联在 core.py，无独立模块）。

| 函数 | 参数 | 返回 |
|------|------|------|
| `write_lifecycle_sentinel(role)` | `dict` | `None` |
| `check_ondemand_timeout(max_minutes=30)` | int? | `list[str]` |
| `cleanup_stale_sentinels()` | — | `list[str]` |

## CCSClient / CCSStreamer (`ccs_socket.py`)

Unix Socket 通信客户端（`Sister Bus` 之外的直接 CCS 通信通道）。

| 类 | 说明 |
|------|------|
| `CCSClient` | 向 CCS 发送消息的客户端 |
| `CCSStreamer` | 流式读取 CCS 输出的客户端 |

## Signals (`signals.py`)

常量 + 检查器模块。对外暴露 `SIGNAL_*` 系列信号定义，并承载所有 `input_signals` 的检查逻辑。

---

## Dependencies

- `paths.py` — 集中管理路径常量
- `routing/gatekeeper.py` — 门禁系统 + CrossRoleRouter
- `routing/roles.py` — 角色加载与 workspace 注入
- `ops/ccs_config.py` — 配置中心

---

## 违反单向依赖的 import (debt)

| 文件 | 引用 | 说明 |
|------|------|------|
| `ecosystem_health.py:36` | `PIPELINE = HOME / "session-pipeline"` | 路径引用 |
| `paths.py:30` | `SESSION_PIPELINE_SRC` | 路径常量（常量） |
| `lifecycle_manager.py` | `from lifecycle.manager import *` | 跨项目重导出 |

