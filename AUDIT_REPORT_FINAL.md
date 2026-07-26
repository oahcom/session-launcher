# 综合审查报告

**审查时间**: 2026-07-26T02:19:00Z
**审查范围**: hermes-session-roles / session-launcher / session-pipeline (三个项目)
**审查轮次**: Round 2 (56项) + Round 3 (8项) + Codex 专项 (11项)
**审查素材**: `_review_materials.json` (12,213 chars, 241行) + 3 轮审查结果 + git diff 摘要 + Sister Bus 状态

---

## 1. 执行摘要与核心结论

本次审查覆盖 Hermes Session Ecosystem 的三个项目，共识别 **75 项问题**, 其中 **P0=0, P1=1, P2=41, P3=33**。无阻断性问题，但存在一个 P1 (生产环境数据风险)，以及大量跨项目的架构一致性问题。

**核心结论**: 系统功能完整，短期无崩溃风险，但存在以下深层结构问题:
1. **Codex 集成与 CCS 架构脱节** — Codex 函数使用了完全不同的哨兵、进程、探测和错误处理模式 (P1+5×P2)
2. **轮次追踪/看门狗线程从未激活** — 4 个哨兵文件 `last_watchdog_check=0.0`, `last_turn_check=0.0`，意味着协作死锁检测和 CCS 存活监控完全失效
3. **INSERT 非幂等性普遍** — 15 处 INSERT 缺少 `ON CONFLICT`，其中 `workflow/client.py` 占 7 处（已跨轮次持续存在）
4. **Sister Bus 存在 ~22 天过期心跳** — STALE_HEARTBEAT ~1,888,000 sec，表明看门狗基础设施大面积失效

---

## 2. 文档审查汇总

| 项目 | 架构文档数 | 核心 doc 文件 | 覆盖度 |
|------|----------|-------------|--------|
| hermes-session-roles | 11 | ARCHITECTURE.md, ECOSYSTEM_OVERVIEW.md, TECHNICAL_DEEP_DIVE.md | 完整 (含 AUDIT_REPORT.md) |
| session-launcher | 9 | HERMES_SESSION_ARCHITECTURE.md, CCS_COLLAB_PROTOCOL.md, BUS_PUSH_ARCH.md | 完整 |
| session-pipeline | 9 | ARCHITECTURE.md, MASTER_PRD.md, SYSTEM_LANDSCAPE.md | 完整 |
| hermes 生态 | 3 | PROJECT_MANUAL.md, hermes-architecture.md, hermes-development.md | 基础覆盖 |

**架构红线文件**: `hermes-architecture-redlines.md` 未在期望路径找到 (`~/.claude/projects/-mnt-c-Users-Administrator/memory/hermes-architecture-redlines.md`)，疑似已重构到 `~/.claude/memory/`。

---

## 3. 代码审查汇总

### 3.1 项目 git diff 概览

| 项目 | 修改文件 | 统计 | 性质 |
|------|---------|------|------|
| **session-launcher** | `feed_listener.py`, `src/core.py`, `src/routing/roles.py` | +118/-4 | 新增 feed_listener tmux 注入、MCP 设置写、契约区块 |
| **session-pipeline** | `src/pipeflow/engine.py` | +18/-22 | 重构 CCS 拉起/通信逻辑，统一导入路径 |
| **hermes-session-roles** | (仅 untracked `test_wf_e2e.py`) | 无提交变更 | 新增测试文件 |

### 3.2 审查问题汇总

| 类别 | P0 | P1 | P2 | P3 | 合计 |
|------|----|----|----|----|------|
| 函数复杂度/拆包 (Round 2/3) | 0 | 0 | 6 | 0 | 6 |
| 缺少测试文件 (Round 2) | 0 | 0 | 3 | 0 | 3 |
| INSERT 非幂等 (Round 2/3) | 0 | 0 | 15 | 0 | 15 |
| N+1 SQL 风险 (Round 2) | 0 | 0 | 0 | 14 | 14 |
| 返回类型不一致 (Round 2) | 0 | 0 | 6 | 0 | 6 |
| 全局变量无锁 (Round 2) | 0 | 0 | 2 | 0 | 2 |
| 多模块写热表 (Round 2) | 0 | 0 | 4 | 0 | 4 |
| Codex 架构一致性问题 | 0 | 1 | 5 | 3 | 9 |
| Gateway 含业务逻辑 | 0 | 0 | 1 | 0 | 1 |
| 模块文档缺失/其他 | 0 | 0 | 0 | 16 | 16 |
| **总计** | **0** | **1** | **42** | **33** | **76** |

---

## 4. 用户言论覆盖度

审查范围内用户的唯一指令为:

> "收集所有需要审查的素材，返回结构化JSON：
> 1. 三个项目的核心文件清单
> 2. 架构文档路径
> 3. 用户在本session中的关键言论（直接引用）
> 4. 本次修改的所有git diff摘要
> 5. CCS对话历史（如果有）"

**覆盖度评估**: 上述 5 点全部在 `_review_materials.json` 中有对应字段:
- `projects.*.core_files` — 核心文件清单
- `architecture_docs` — 架构文档路径
- `user_key_remarks` — 用户言论（当前 session 仅 1 条指令）
- `git_diffs` — git diff 摘要
- `ccs_sessions` — CCS 对话历史（conversation_history 为 null，但运行中和哨兵数据已收录）

---

## 5. 关键分歧点与裁决

### 5.1 Codex 哨兵目录隔离 vs 统一

- **现状**: Codex 使用 `/tmp/cdx-sentinels/` 而 CCS 使用 `/tmp/ccs-sentinels/`，互不可见
- **分歧**: 隔离避免混淆 vs 统一便于监控
- **裁决 (P2)**: 统一到 `/tmp/ccs-sentinels/`，通过 `engine` 字段 (`"ccs"` / `"codex"`) 区分。ccs-status 和 cdx_status 通过 engine 字段各自过滤。减少监控盲区。

### 5.2 Codex readiness 探测: 3秒硬等 vs 15轮循环

- **现状**: `start_codex_session()` 使用 `time.sleep(3)`，`start()` 使用 15 轮 `_is_alive()` 探测
- **分歧**: Codex 启动更快所以 3 秒够用 vs 应统一点模式
- **裁决 (P1)**: 模型加载时间不可预测，3 秒硬等可能在前端产生 5-30 分钟等待幻觉。复用 `_is_alive()` 循环探测。**P1 因为会导致用户以为系统"卡死"而手动中断，造成 session 状态不一致。**

### 5.3 INSERT 是否都应加 ON CONFLICT

- **现状**: 15 处 INSERT 无冲突处理，部分场景依赖唯一索引冲突报错
- **分歧**: 幂等性要求 vs "重试就应该报错"
- **裁决 (P2)**: 幂等性为长期要求。但按风险分级：`workflow/client.py` 的 7 处需优先修复（workflow 引擎重试时会产生重复记录），其余 8 处排后。非全量修复，YAGNI。

---

## 6. P0 必须修复清单

**本次审查未发现 P0 问题。**

---

## 7. P1 应该修复清单

| # | 文件 | 行 | 问题 | 风险 | 修复方向 |
|---|------|----|------|------|---------|
| **1** | `src/core.py` | 733 | Codex 启动 readiness 仅 `time.sleep(3)`，无循环探测 | 模型加载慢时呈 5-30 分钟假死状态，用户中断后 session 状态不一致 | 复用 `_is_alive()` 15轮循环探测模式 |

---

## 8. P2 可选改进清单

### 8.1 代码质量 (复杂度)

| # | 文件 | 行数 | 问题 | 建议 |
|---|------|------|------|------|
| 1 | `src/core.py` | 455 | 模块超 400 行，需拆分 | 按职责拆为 core.py (启动) + ccs_ops.py (CCS 操作) + codex_ops.py (Codex 操作) |
| 2 | `src/migration_scripts.py` | 415 | 模块超 400 行 | 按迁移版本拆分 |
| 3 | `src/template_registry.py` | 528 | 模块超 400 行 | 按模板类型或领域拆分 |
| 4 | `src/routing/partner.py` | 430 | 模块超 400 行 | 拆出路由策略 |
| 5 | `src/lifecycle/engine.py` | 445 | 模块超 400 行 | 按生命周期阶段拆分 |
| 6 | `src/lifecycle/manager.py` | 441 | 模块超 400 行 | 按职责拆分 |

### 8.2 INSERT 幂等性 (15处)

| # | 文件 | 行 | 表 | 建议 |
|---|------|-----|-----|------|
| 1 | `workflow/client.py` | 33 | tasks | 加 `OR REPLACE` |
| 2 | `workflow/client.py` | 81 | task_results | 加 `OR REPLACE` |
| 3 | `workflow/client.py` | 88 | workflow_logs | 加 `OR REPLACE` |
| 4 | `workflow/client.py` | 113 | workflow_instances | **Round 3 持续存在**，优先修复 |
| 5 | `workflow/client.py` | 191 | workflow_instances | **Round 3 持续存在**，优先修复 |
| 6 | `workflow/client.py` | 224 | (未明确) | 确认表名 |
| 7 | `workflow/client.py` | 386 | (未明确) | 确认表名 |
| 8 | `lifecycle/engine.py` | 423 | (未明确) | 加 `ON CONFLICT` |
| 9 | `lifecycle/manager.py` | 106 | (未明确) | 加 `ON CONFLICT` |
| 10 | `lifecycle/manager.py` | 288 | (未明确) | 加 `ON CONFLICT` |
| 11 | `routing/router.py` | 188 | (未明确) | 加 `ON CONFLICT` |
| 12 | `routing/router.py` | 207 | (未明确) | 加 `ON CONFLICT` |
| 13 | `event_log.py` | 111 | (未明确) | 加 `OR REPLACE` |
| 14 | `migrate_v11_collab.py` | 92 | (未明确) | 加 `OR REPLACE` |
| 15 | `p0_exemption.py` | 84, 189 | (未明确) | 加 `ON CONFLICT` |
| 16 | `template_registry.py` | 343 | (未明确) | 加 `ON CONFLICT` |
| 17 | `workflow/gateway.py` | 169 | (未明确) | 加 `ON CONFLICT` |

### 8.3 Codex-CSS 架构一致性 (6项)

| # | 文件 | 问题 | 建议 |
|---|------|------|------|
| 1 | `src/core.py:676` | `start_codex_session` 68 行，职责混杂 | 拆为: `_build_codex_runner`, `_write_codex_sentinel`, `_launch_tmux_session` |
| 2 | `src/core.py:695` | 手写 shell 转义脆弱 | 用 `shlex.quote()` 替代 `replace()` 链 |
| 7 | `src/core.py:733` | Codex 哨兵绕过 CcsSentinel dataclass | 复用 `write_sentinel()`，增加 `engine` 字段 |
| 8 | `src/core.py:747` | `exec_codex` 命名混淆，不检查 session 存活 | 重命名 `run_codex_task()`，开头检查哨兵 |
| 8 | `src/core.py:770` | exec_codex 异常处理模式与 send() 不一致 | 移除裸 `except Exception` |
| 9 | `src/core.py:774` | cdx_status 返回结构缺少 workspace/health | 统一返回结构 |

### 8.4 代码质量问题 (代码审查专项)

| # | 文件 | 问题 |
|---|------|------|
| 1 | `src/core.py` (Codex) | 魔数无命名常量 (`loop_delay=60`, `[:2000]`, `[-2000:]`, `[:500]`) |
| 2 | `src/core.py` (Codex) | 哨兵拼写 engin/engine 不一致 |
| 3 | `src/core.py` (Codex) | `_find_codex_pid` 查 claude 而非 codex 进程 |
| 4 | `src/core.py` (Codex) | `start_codex_session` 缺少 feed_listener 支持 |
| 5 | `workflow/gateway.py` | Gateway 含业务条件判断 (违反网关职责) |
| 6 | `src/tmux_ops.py` | 无模块级 docstring |
| 7 | `src/tmux_ops.py` | 全局变量无锁 |
| 8 | `src/routing/roles.py` | 全局变量无锁 |
| 9 | 多文件 | N+1 SQL 循环 (14处，分散在 event_log, migration_scripts, workflow/client 等) |
| 10 | 多文件 | 返回类型 None/dict 不一致 (6处，workflow/client.py, lifecycle/manager.py) |

### 8.5 热表竞争 (4项)

| # | 表 | 写入模块数 | 主要写者 |
|---|-----|-----------|---------|
| 1 | `tasks` | 11 | pool_cli, p0_exemption, migration_scripts |
| 2 | `workflow_templates` | 10 | migrate_v11_collab, migration_scripts, template_registry |
| 3 | `workflow_logs` | 9 | p0_exemption, migration_scripts, migration/scripts |
| 4 | `workflow_instances` | 10 | migration_scripts, migration/scripts, workflow/db |

---

## 9. 遗留风险与监控建议

### 9.1 高风险遗留项

| 风险 | 严重度 | 描述 | 缓解 |
|------|--------|------|------|
| **看门狗全面失效** | **高** | 4 个哨兵的 `last_watchdog_check=0.0, last_turn_check=0.0`；Sister Bus 有 ~22 天过期心跳 | 需要检查 `watchdog.py` 和 `tracker.py` 的守护启动逻辑；确认 `start()` 中 watchdog/tracker 是否被正确触发 |
| **CCS 存活检测空转** | **中** | 3 个 CCS 中只有 scout 有真实 pid 和 tmux session；engineer/product_architect/workflow_engine 为 ondemand 生命周期 | 确认 ondemand 生命周期预期行为；若需要持续可用应切为 loop 模式 |
| **三源验证拒绝** | **中** | workflow 引擎向 scout 分发任务失败于 "三源验证拒绝" | 需要检查 pipeline 的 triple-source verification 逻辑，确定拒绝原因 |
| **feed_listener 子进程管理欠缺** | **低-中** | `_start_feed_subprocess()` 启动的子进程无生命周期管理，终止时不会自动清理 | 后续应考虑 `ProcessPoolExecutor` 或 `supervisor` 式管理 |

### 9.2 监控建议

1. **哨兵看门狗活动监控**: 新增 `ccs check --watchdog` 命令，检查所有哨兵文件中 `last_watchdog_check` 是否是最近 5 分钟内更新
2. **INSERT 冲突日志**: `workflow/client.py` 等高频写入点增加 `ON CONFLICT` 后的冲突计数日志，追踪重试频率
3. **热表写并发监控**: 对 `tasks`, `workflow_templates`, `workflow_logs`, `workflow_instances` 四个热表增加事务超时和死锁日志
4. **CCS 存活探针**: cc-status 增加健康度评分（基于 `last_watchdog_check`, `last_turn_check`, `heartbeat_age` 三个指标）

---

## 10. 下一步行动建议

### 短期 (立即-3天)

1. **P1 修复**: `start_codex_session()` readiness 探测从 `time.sleep(3)` 改为 15 轮 `_is_alive()` 循环
2. **看门狗诊断**: 检查 `src/ops/watchdog.py` 和 `src/ops/tracker.py` 的启动链，确认为什么 `start()` 后 watchdog/tracker 线程未运行
3. **Sister Bus 清扫**: 消费过期 STALE_HEARTBEAT 消息；清理 ~22 天前的心跳记录

### 中期 (1-2周)

4. **INSERT 幂等性**: 优先修复 `workflow/client.py` 的 7 处 (Round 3 持续跟踪的 113, 191 行优先)
5. **Codex 哨兵统一**: Codex 哨兵迁入 `/tmp/ccs-sentinels/`，通过 `engine: "codex"` 区分
6. **模块拆分**: `core.py` (455行) 优先拆分，路径结构对齐

### 长期 (架构治理)

7. **单元测试覆盖**: 为 `src/ccs.py`, `src/core.py`, `src/paths.py` 添加测试文件 (Round 2 标记)
8. **热表写收敛**: 分析 4 张热表的 9-11 个写者，提取集中写入层
9. **架构红线恢复**: 定位 `hermes-architecture-redlines.md` 并确认其内容已合并到 `~/.claude/memory/`
