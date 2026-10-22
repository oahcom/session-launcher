# session-launcher — CCS 生命周期执行层

> 是本项目的操作手册，不是你运行时的身份。运行时身份由角色 workspace CLAUDE.md 定义。

## 项目职责

CCS 创建/停止/检测/注入，哨兵系统，跨角色协作基础设施。（worktree 管理不在本仓：全仓只有 `ecosystem_health.py` 一行排除注释）

## 核心架构

```
ccs.py start <role>  → 读取 persona JSON + prompt 模板
                     → 创建 tmux session（ccs-<role>；instance_id>0 时 ccs-<role>-<id>）
                     → 创建 ccs-workspaces/<role>/ 目录
                     → inject_role_knowledge_into_workspace() 写入角色知识
                     → tmux send 发送初始任务指令
                     → 写入哨兵
                     → 启动 watchdog/turn_tracker（CLI 进程内 daemon 线程：start 返回即消失）
```

## 关键路径

```
~/.claude/settings.json（全局 MCP）
         ↓ 继承
~/.claude/CLAUDE.md（全球身份）
         ↓ 叠加 WORKSPACE_SYS marker
ccs-workspaces/<role>/CLAUDE.md（角色身份：<!-- KNOWLEDGE:START --> 块 = 最终身份层）
```

SESSION_ROLE marker 与 ~/CLAUDE.md 运行时注入已退场：`routing/roles.py:113-117` 只剩
常量定义，全仓无写入调用。

## 关键规则

### 启动参数
```bash
python3 src/ccs.py start <role>               # 交互式
python3 src/ccs.py start <role> --no-attach   # 后台（无 --detach 参数）
# --partner / --bus-track / --auto-restart 已退场：参数仍在 argparse，守护线程随 CLI 进程退出
```

### 服务检查
```bash
python3 src/ccs.py status          # 不带参数；单角色用 status-role <role>
python3 src/ecosystem_health.py --json                # 只打 summary（当前 114/115，wf:engine 随 pipeline 侧 import 抖动）
python3 src/ecosystem_health.py --ci                  # 要门禁 exit code 用这个
```
