# 审批密钥：实测（ADR，2026-10-22 重写）

原版 145 行写的是「计划中的机制」：`secrets.token_urlsafe(32)`、密钥存 `step_results[step_id]`、
`ccs send` 把密钥推给 assigner、`confirm_step` 校验。2026-10-22 拿生产库把每一步量了一遍，
两处与文档相反，于是按实测重写。

## 实测（`~/.hermes/state/workflows.db` 只读）

| 签发路径 | 落库位置 | 签发 | 已消费 | 结论 |
|---|---|---|---|---|
| `lifecycle.manager._issue_approval_token` | `step_results[step_id]` | 470 | 41 | 活的：41 次凭钥通过 |
| `WorkflowClient.complete_step`（launcher） | `step_results` **顶层** | 117 | 0 | 死的：引擎只读 `[step_id]`，顶层键没人读 |

`workflow_logs` 的 `step_confirmed` 67 条，最近 24h 19 条；角色发起 44 条
（pm 10 / engineer 10 / closer 6 / reviewer 4 / maintainer 4 / security_auditor 2 / qa 2），
`workflow_engine` 23 条。

## 口径陷阱（原文档和后续统计都栽在这里）

消费成功 = 同一事务里 `approval_token = None` + `approval_consumed_at = now()`。
于是「token 仍在」的集合按定义就是「未消费」的集合，用它算消费率永远得 0
（「全库 429 张 approval_consumed_at 全为 null」就是这么来的）。
统计凭证消费率只能用 `approval_consumed_at`，不能用「凭证是否还在」。

41 次消费里有 27 次发生在最近 48h（最新 10-22 00:35），且 **27 次是
`confirmed_by=workflow_engine`** —— 即 TTL 60min + grace 30min 到点后的超时兜底放行，
其余由 pm / engineer / pg 等角色主动 confirm。这条兜底就是「待审步等待中位数 90 分钟」的来源：
密钥通道不只是给角色的便利入口，它同时是**系统把卡住的评审步放行**的通道。

## 仍然成立的设计

- 每步一张一次性密钥，TTL 3600s；无审批人（系统发起 / 自审 / 首步）不签发。
- `confirm_step` 校验顺序：状态必须 `step_done_ready` → 已消费拒 → 过期拒
  （`allow_expired` 只给引擎/系统）→ 比对 → 二次身份校验（只有该步 assigner 能确认）→
  置 `approval_consumed_at` + `approval_token=None`。
- 推送不可靠：bus 会把密钥脱敏成 `***`，所以审批人必须能自己拉：
  `wf approval <wf_id> <step_id>` → `get_approval_token`（fail-closed：非 assigner 直接 `PermissionError`）。
- 引擎不读密钥：`engine.py` 里只有一个 dataclass 字段 `approval_prompt`，
  `step_done_ready` 直接进推进分支（engine.py:1203）。密钥只约束「角色主动 confirm」这一个入口，
  不是推进闸门。

## 已废弃

launcher `WorkflowClient.complete_step` 的顶层签发 + 向 reviewer 广播「审批请求」：
117 张、0 消费、引擎看不见，只会制造无法完成的 confirm 往返。终态守卫仍在 `_update_if_mutable`。

## 复盘

设计文档描述的是「打算怎么做」而不是「现在是什么」，而本文件此前 **0 处引用**——
没人读的文档不会被实现校正，只会持续误导下一个读它的人。
