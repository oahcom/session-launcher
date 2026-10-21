# 审批密钥：实测（ADR，2026-10-22 重写）

原版 145 行写的是「计划中的机制」：`secrets.token_urlsafe(32)`、密钥存 `step_results[step_id]`、
`ccs send` 把密钥推给 assigner、`confirm_step` 校验。2026-10-22 拿生产库把每一步量了一遍，
两处与文档相反，于是按实测重写。

## 实测（`~/.hermes/state/workflows.db` 只读）

| 签发路径 | 落库位置 | 签发 | 已消费 | 结论 |
|---|---|---|---|---|
| `lifecycle.manager._issue_approval_token` | `step_results[step_id]` | 470 | 41 | 活的：41 次凭钥通过 |
| `WorkflowClient.complete_step`（launcher） | `step_results` **顶层** | 117 | 0 | 死的：引擎只读 `[step_id]`，顶层键没人读 |

`workflow_logs` 的 `step_confirmed`（日志口径，含无钥的 assigner 校验确认）67 条，
最近 24h 19 条；角色发起 44 条（pm 10 / engineer 10 / closer 6 / reviewer 4 / maintainer 4 /
security_auditor 2 / qa 2），`workflow_engine` 23 条。下面的 actor 分布是**密钥消费口径**，两个数不同源。

## 口径陷阱（原文档和后续统计都栽在这里）

消费成功 = 同一事务里 `approval_token = None` + `approval_consumed_at = now()`。
于是「token 仍在」的集合按定义就是「未消费」的集合，用它算消费率永远得 0
（「全库 429 张 approval_consumed_at 全为 null」就是这么来的）。
统计凭证消费率只能用 `approval_consumed_at`，不能用「凭证是否还在」。

41 次的 actor 分布：`workflow_engine` 21，角色 20（pm 5 / engineer 5 / closer 3 /
maintainer 2 / product_architect 1 / security_auditor 1 / qa 1 / coordinator 1 / pg 1）。
最近 48h 有 27 次（最新 10-22 00:35），其中 engine 16、角色 11。
engine 那 21 次是 TTL 60min + grace 30min 到点后的超时兜底放行——「待审步等待中位数
90 分钟」就是它。所以密钥通道不只是给角色的便利入口，它同时是**系统把卡住的评审步放行**的通道。

## 已全部退场（2026-10-22）

上表是通道活着时的样子。同一天两次提交把整条链接连根拔掉，本文件此后只作证据留存：

| 环节 | 删除于 | 现机制 |
|---|---|---|
| 签发（引擎 `_issue_approval_token`、TTL、审批门控） | pipeline `841e545` / `e2521ce` | `step_done_ready` 直接推进 |
| 拉取通道（`wf approval`、`pull_approval`、三个读取器） | launcher `32568d7`、pipeline `2a8a016` | 无密钥可拉 |
| 确认校验（密钥分支、`allow_expired`） | pipeline `2a8a016` | 只凭「该步派单方」身份 |

驳回语义改由评审文本承担（`CHANGES_REQUESTED` / `rejected` 关键词，由
`wf_lifecycle` 的 `review_gate` 消费）。那 21 次引擎超时兜底放行没有替身，但评审人什么都不写
不再等于通过：`review_gate` 判 `unknown` 且 fail-closed 不推进（pipeline `3613a2e` 引擎 tick、
`8218c1b` 角色 `wf complete` 两条路径都装了）。

## 已废弃

launcher `WorkflowClient.complete_step` 的顶层签发 + 向 reviewer 广播「审批请求」：
117 张、0 消费、引擎看不见，只会制造无法完成的 confirm 往返。终态守卫仍在 `_update_if_mutable`。

## 复盘

设计文档描述的是「打算怎么做」而不是「现在是什么」，而本文件此前 **0 处引用**——
没人读的文档不会被实现校正，只会持续误导下一个读它的人。
