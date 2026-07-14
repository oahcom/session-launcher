# 全角色基线测量报告

**生成时间:** 2026-07-14
**执行角色:** pg

## 1. 总览

| 指标 | 数值 |
|------|------|
| 总角色数 | 25 |
| 有 CLAUDE.md | 12 (48%) |
| 无 CLAUDE.md | 13 (52%) |

## 2. Density (红线密度)

| 等级 | 角色数 | 角色 |
|------|--------|------|
| 良好 (≥5) | 9 | pg(11), lr(8), maintainer(5), curator(5), coordinator(5), optimizer(5), product_architect(5), pm(5), reviewer(5), qa(5), test-pg(11) |
| 不合格 (<3) | 2 | ccs-monitor(1) |
| 无文件 | 13 | scout, engineer, closer, codex-dev, debate_verifier, knowledge_curator, security_auditor, devops, writer, investigator * 4 |

**发现:** L3(经验教训引用) 全部为 0，说明没有角色 CLAUDE.md 中引用了 bus reflexion 来源。

## 3. Purity (上下文纯度)

| 等级 | 角色数 | 角色 |
|------|--------|------|
| 良好 | 1 | ccs-monitor |
| 及格 | 9 | maintainer, curator, coordinator, optimizer, product_architect, pm, reviewer, qa, lr |
| 不合格 | 2 | pg, test-pg |
| 无文件 | 13 | — |

**发现:**
- ccs-monitor 唯一 signal 完美匹配，其他角色 signal 均有偏差
- pg 和 test-pg 存在禁区指令污染 (forbidden=True)
- 信号引用匹配问题普遍存在

## 4. 建议

1. **补齐 13 个缺失的 CLAUDE.md** — 尤其是日常使用的 engineer/developer
2. **修复 signal 引用** — 9 个 "及格" 角色 signal 未匹配 persona JSON
3. **净化 pg/test-pg 禁区指令**
4. **建立 L3 机制** — 使 CLAUDE.md 能引用 bus reflexion 来源
