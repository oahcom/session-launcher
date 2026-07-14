# CCS 三项目 Session 交付物（2026-07-14/15）

## 下个 Session 快速启动

```bash
# 1. 启动守护进程（WSL2 中 systemd 不可用时）
bash ~/session-launcher/scripts/start_daemons.sh

# 2. 启动核心 CCS 角色
cd ~/session-launcher
python3 src/ccs.py start pg --no-attach
python3 src/ccs.py start engineer --no-attach
python3 src/ccs.py start maintainer --no-attach

# 3. 检查系统状态
python3 src/ccs.py status
python3 ~/session-pipeline/src/auto_route.py --status
python3 -c "import sys; sys.path.insert(0,'src'); from workflow.client import WorkflowClient; w=WorkflowClient('pg'); t=w.check_task(); print('PG:', t['status'] if t else 'idle'); w.close()"
```

## 系统架构要点

| 项目 | 路径 | 核心文件 |
|------|------|---------|
| 角色定义层 | `~/hermes-session-roles/` | `personas/session-roles/persona_*.json` (24个) |
| 执行层 | `~/session-launcher/` | `src/ccs.py` / `src/core.py` / `src/workflow_client.py` |
| 路由层 | `~/session-pipeline/` | `src/auto_route.py` / `src/workflow_engine.py` / `src/router.py` |

## 已验证的全链路

```
任务创建 (workflow_client) 
  → PG 自动拾取 (check_task)
  → PG 执行 (理解/编码/自测)
  → 产出 code_fix (含文件列表)
  → 验证基础设施 (pipeline daemon 自动路由)
```

## 已修复的 Bug 清单

P0 (3): is_ccs_running 导出 / {{_PERM_FLAGS}} 花括号 / --allow-dangerously-skip-permissions
P1 (2): list_sentinels 路径 / 路径 DRY
P2 (5): _ROLE_CACHE / partner 字段 / 死代码 / persona_evolution 文件名 / 命名检测
