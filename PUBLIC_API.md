# PUBLIC_API — session-launcher

## Public API

> **位置**: `~/session-launcher/src/`

## Direction

session-pipeline → **session-launcher** → hermes-session-roles (单向)

任何反向依赖（launcher→pipeline, roles→launcher, roles→pipeline）标记为 debt。

## Dependencies

## Subpackage Structure (US-01)

```
src/
├── workflow/          # 工作流子系统
│   ├── client.py      # WorkflowClient — CCS 角色使用的工作流客户端
│   ├── db.py          # DB 操作
│   ├── gateway.py     # Gate — 模板门禁
│   └── utils.py       # 任务工具
├── events/            # 信号/通知子系统
│   ├── signals.py     # 信号常量
│   ├── parser.py      # 信号解析器
│   └── notify.py      # NotificationEngine
├── routing/           # 跨角色路由
│   ├── router.py      # CrossRoleRouter
│   ├── roles.py       # RoleManager
│   └── partner.py     # PartnerClient
├── ops/               # 基础设施
│   ├── sentinel.py    # 哨兵管理
│   ├── tracker.py     # 轮次追踪
│   └── watchdog.py    # 伙伴存活守护
├── migration/         # 迁移脚本
│   └── scripts.py
├── paths.py           # 路径常量
├── template_registry.py # 模板注册中心
├── template_validator.py # 模板验证
├── workflow_gate.py   # 模板门禁
└── cross_role_router.py # 跨角色路由
```

## WorkflowClient (`workflow_client.py`)

被 pipeline/composite_runner 及所有 CCS agent 消费。

| 方法 | 参数 | 返回 | 说明 |
|------|------|------|------|
| `check_task()` | — | `Optional[dict]` | 查当前角色待办任务 |
| `create_task_v2(title, assignee, template_id, initiator_role)` | 4 str | `(task_id, wf_id)` | 创建任务+工作流 |
| `complete(wf_id, summary, artifacts)` | str, str, list[str] | `dict` | 标记完成 |
| `fail(wf_id, reason)` | str, str | `dict` | 标记失败 |
| `get_logs(role, wf_id=None)` | str, str? | `list` | 查日志 |
| `cancel(wf_id, reason)` | str, str | `dict` | 取消 |
| `notify(category, title, evidence)` | str, str, str | `dict` | 发 bus 通知 |

## LifecycleManager (`lifecycle_manager.py`)

| 方法 | 参数 | 返回 |
|------|------|------|
| `complete_step(wf_id, step_id)` | str, str | `str` |
| `fail_step(wf_id, step_id, reason)` | str, str, str | `str` |
| `confirm_step(wf_id, step_id)` | str, str | `str` |

## CrossRoleRouter (`cross_role_router.py`)

| 方法 | 参数 | 返回 |
|------|------|------|
| `route(source_role, target_role, message)` | str, str, dict | `dict` |
| `get_routes(source_role)` | str | `list[dict]` |

## Sentinel (`sentinel.py`)

| 函数 | 参数 | 返回 |
|------|------|------|
| `get_sentinels()` | — | `dict[str, dict]` |
| `write_sentinel(role, **kwargs)` | str | `None` |
| `remove_sentinel(role)` | str | `None` |
| `get_sentinel(role)` | str | `Optional[dict]` |

## Task Utilities (`task_utils.py`)

| 函数 | 参数 | 返回 |
|------|------|------|
| `check(role)` | str | `str` ("无待完成任务" 或任务 JSON) |

## StepEngine (`step_engine.py`)

| 方法 | 参数 | 返回 |
|------|------|------|
| `complete_step(wf_id, step_id)` | str, str | `dict` |
| `fail_step(wf_id, step_id, reason)` | str, str, str | `dict` |

## Signals (`signals.py`)

常量模块。对外暴露 `SIGNAL_*` 系列信号定义。

---

## Dependencies

- `paths.py` — 集中管理路径常量
- `workflow_gate.py` — 模板门禁
- `template_registry.py` — 模板注册中心
- `cross_role_router.py` — 跨角色路由

---

## 违反单向依赖的 import (debt)

| 文件 | 引用 | 说明 |
|------|------|------|
| `ecosystem_cli.py:13` | `_PIPELINE_SRC = path / "session-pipeline"` | 路径引用 |
| `ecosystem_health.py:36` | `PIPELINE = HOME / "session-pipeline"` | 路径引用 |
| `paths.py:30` | `SESSION_PIPELINE_SRC` | 路径常量 |
| `p0_exemption.py:173` | 注释提及 `workflow_db` | 仅注释 |

