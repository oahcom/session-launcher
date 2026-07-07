# Session Launcher 项目

## 项目概述
Session 生态的**执行层**——CCS 创建、生命周期管理、跨 session 协作基础设施。
依赖 hermes-session-roles（角色定义）和 session-pipeline（消息路由）。

## 整体架构
```
hermes-session-roles (定义层) → 读取角色 JSON
        │
        ▼
  session-launcher (执行层) ← 本项目
  - ccs.py: 创建/停止/状态/消息
  - watchdog: 伙伴存活守护
  - turn_tracker: 轮次追踪 + 死锁检测
  - sentinel: /tmp/ccs-sentinels/ 哨兵
        │
        ├──→ Sister Bus (SQLite) + Feed Push (Unix Socket)
        └──→ session-pipeline (路由层)
```

**铁律：修改本项目时必须同时考虑上下游影响。**

## 项目结构
```
src/
  ccs.py               → CCS 核心（start/stop/status/send）
  ccs_start.py         → 旧版独立启动器（逐步迁入 ccs.py）
  launcher.py          → 旧版启动器（保留向后兼容）
  signals.py           → 8 种信号检查器
  worker_pool.py       → 并行 HTTP 调用 9Router
  work_generator.py    → LLM 任务模板生成
  orchestrator.py      → 全自动 CCS 协调器
tests/
  test_e2e_mock.py     → 19 个 mock 测试
CCS_COLLAB_PROTOCOL.md → 协作协议文档（根因分析 + 规范）
deploy_all.sh          → 全角色一键部署
ccs_watchdog.sh        → 纯 bash 监控（零 LLM）
BUS_PUSH_ARCH.md       → Bus Push 架构设计文档
feed_listener.py       → 实时监听 bus 新消息（零轮询）
```

## Git 工作流
1. 禁止切换分支，始终在 main 分支工作
2. 小步提交，每完成一个逻辑单元立即 commit
3. 出错用新提交修复，不要 revert
4. 本地即生产环境，切换分支会影响运行中的服务

## 协作红线
1. 协作逻辑在代码层，不在 prompt 文本中
2. 创建方必须守护被创建方
3. 轮次追踪必须内置
4. 死锁超时 > 15 分钟 → 写入 bus 升级给人
5. **实时推送必须可用**：bus write() 末尾调用 _notify_feed()，feed socket 不可用时静默降级
