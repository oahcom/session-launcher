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
python3 src/ecosystem_health.py --check
```

### 路由策略
- sticky（默认）：同角色消息路由到同一 CCS
- round-robin：轮询分发
- priority：按优先级路由

### 禁止操作
- ❌ 直接修改 ccs-workspaces/<role>/ 下的 CLAUDE.md（由 inject 管理）
- ❌ 重启 hermes-gateway（SIGKILL 中断长连接）
- ❌ 跨 worktree 修改文件

## 代码布局
```
src/
  core.py              ── 生命周期编排
  ccs.py               ── CLI 入口
  ccs_socket.py        ── CCS 直接通信（Unix Socket）
  ecosystem_health.py  ── 三项目健康检查
  paths.py             ── 路径管理
  codex_ops.py         ── Codex 集成
  tmux_ops.py          ── tmux 操作封装
  role_manager.py      ── 角色管理
  workflow_client.py   ── CCS 工作流客户端
  wf.py                ── CLI 快捷入口
  lifecycle_manager.py ── backward-compat 重导出（实际在 pipeline lifecycle.manager）
  events/
    signals.py         ── 信号常量 + input_signals 检查逻辑
    parser.py          ── 信号解析器
  ops/
    sentinel.py        ── 哨兵文件读写（CcsSentinel）
    lifecycle_sentinel.py ── 生命周期哨兵写入/超时检查/清理
    ccs_config.py      ── 配置中心
    mcp_settings.py    ── MCP 设置
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

## 测试命令
```bash
cd ~/session-launcher && python3 -m pytest src/ -x -q
python3 -m py_compile src/*.py src/**/*.py
python3 src/ecosystem_health.py --check
```
