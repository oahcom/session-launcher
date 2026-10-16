# session-launcher — CCS 生命周期执行层

> 是本项目的操作手册，不是你运行时的身份。运行时身份由角色 workspace CLAUDE.md 定义。

## 项目职责

CCS 创建/停止/检测/注入，worktree 管理，哨兵系统，跨角色协作基础设施。

## 核心架构

```
ccs.py start <role>  → 读取 persona JSON + prompt 模板
                     → 创建 tmux session（ccs-<role>）
                     → 创建 cc-workspaces/<role>/ worktree
                     → inject_role_knowledge_into_workspace() 写入角色知识
                     → tmux send 发送初始任务指令
                     → 写入哨兵
                     → 启动 watchdog/turn_tracker
```

## 关键路径

```
~/.claude/settings.json（全局 MCP）
         ↓ 继承
~/.claude/CLAUDE.md（全球身份）
         ↓ 叠加 WORKSPACE_SYS marker
ccs-workspaces/<role>/CLAUDE.md（角色身份：<!-- KNOWLEDGE:START --> 块）
         ↓ 叠加 SESSION_ROLE marker
主项目 ~/CLAUDE.md（运行时注入，legacy）
```

## 关键规则

### 启动参数
```bash
python3 src/ccs.py start <role>               # 交互式
python3 src/ccs.py start <role> --detach      # 后台
python3 src/ccs.py start <role> --partner x   # 带伙伴
```

### 服务检查
```bash
python3 src/ccs.py status <role>
python3 src/ecosystem_health.py --json   # 或 --ci（异常时 exit 1）
```

### 意图守护 daemon（ccls_daemon_intent_classifier.py）
`ccs-intent-guardian.service` 托管。触发源 = 该角色存在**未对齐的用户请求**
（读 bus 中 `core.send` 写的 `[user_intent_reminder] 已注入意图提醒 → <role>` 痕迹，其
`ts` 即最近一次用户请求）。有请求才提醒，预算 `_MAX_REMINDERS=3`、TTL `_REQUEST_TTL=1800s`；
**无请求即零注入**。

曾用「pane 快照无变化」判停滞 —— 对 infinite 常驻角色该判据等价于「角色空闲」，
而空闲是其正常态，于是形成自激循环：注入 → 角色回"是/是/待机" → pane 变化 →
预算重置 → 再注入。实测 coordinator 767 条发言里 64% 是此类空转、pm 42%、engineer 56%
（有真实任务的 scout/qa/lr ≈ 0%）；每次注入烧掉一个完整 LLM 回合、产出为零。
改用请求驱动后实测 110min 零空转（2026-10-16）。

- 源码更新后 daemon 主动退出，交 systemd `Restart=always` 以新代码拉起 → 重启计数增长属**设计**，非崩溃循环
- 改触发器/预算后必须重启该 service，否则跑的是旧代码

### 路由策略
- sticky（默认）：同角色消息路由到同一 CCS
- round-robin：轮询分发
- priority：按优先级路由

### 禁止操作
- ❌ 直接修改 ccs-workspaces/<role>/ 下的 CLAUDE.md（由 inject 管理）
- ❌ 重启 hermes-gateway（SIGKILL 中断长连接）
- ❌ 跨 worktree 修改文件

## 代码布局（2026-08-23 实测）
```
src/
  core.py              ── 生命周期编排
  ccs.py               ── CLI 入口
  launcher.py          ── CCS 启动编排（start/detach/partner 实际入口）
  ccs_socket.py        ── CCS 直接通信（Unix Socket）
  ecosystem_health.py  ── 三项目健康检查
  paths.py             ── 路径管理
  codex_ops.py         ── Codex 集成
  tmux_ops.py          ── tmux 操作封装
  workflow_client.py   ── CCS 工作流客户端
  wf.py                ── CLI 快捷入口
  user_intent_reminder.py ── 用户输入意图提醒（deamon 重复提醒 ccs 是否真解决用户问题）
  events/
    signals.py         ── 信号常量 + input_signals 检查逻辑
    parser.py          ── 信号解析器
  ops/
    sentinel.py        ── 哨兵文件读写（CcsSentinel）
    ccs_config.py      ── 配置中心
    tracker.py         ── 轮次追踪
    watchdog.py        ── 伙伴存活守护
    runner.py          ── 执行器
    validators.py      ── 参数校验
    workspace.py       ── Workspace 管理
  routing/
    roles.py           ── 角色加载 + workspace 注入 + 权限
    gatekeeper.py      ── 门禁系统 + CrossRoleRouter
    partner.py         ── 伙伴客户端
  workflow/
    client.py          ── WorkflowClient
    db.py              ── DB 连接
    schema.py          ── Schema 定义
  migration/
    scripts.py         ── 迁移脚本
```
注：`role_manager.py` / `lifecycle_manager.py` 已于 git f6c19ae 删除（YAGNI 收敛），
其职责并入 core.py / pipeline lifecycle.manager，此处不再列出。

## 测试命令
```bash
cd ~/session-launcher && python3 -m pytest tests/ -q
python3 -m py_compile src/*.py src/**/*.py
python3 src/ecosystem_health.py --json
```
