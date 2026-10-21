# session-launcher — CCS 生命周期执行层

> 是本项目的操作手册，不是你运行时的身份。运行时身份由角色 workspace CLAUDE.md 定义。

## 项目职责

CCS 创建/停止/检测/注入，worktree 管理，哨兵系统，跨角色协作基础设施。

## 核心架构

```
ccs.py start <role>  → 读取 persona JSON + prompt 模板
                     → 创建 tmux session（ccs-<role>）
                     → 创建 ccs-workspaces/<role>/ 目录
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
python3 src/ccs.py start <role> --no-attach   # 后台（无 --detach 参数）
python3 src/ccs.py start <role> --partner x   # 带伙伴
```

### 服务检查
```bash
python3 src/ccs.py status          # 不带参数；单角色用 status-role <role>
python3 src/ecosystem_health.py --json                # 实测 115/115 全 pass、exit=0（2026-10-22）
```
