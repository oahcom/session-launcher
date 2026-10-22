# session-launcher 问题单

> 2026-10-16 建单，2026-10-22 在分支 `r77-routing-daemon-whitelist` 逐条对照代码复核。
> 结论：L1–L9 全部已在代码中修复，本文件只留索引，不再当作待办。

| 条目 | 原缺陷 | 现状与证据 |
|------|--------|-----------|
| L1 | feed_listener 把 `Enter` 当字面文本 | 已修：拆成两次 send-keys，`feed_listener.py:120-127` |
| L2 | persona workgroup 覆盖内置 `"*"` 通配 | 已修：`matrix.setdefault(name, set()).update(roles)`，`src/routing/gatekeeper.py:148` |
| L3 | `_tmux_send` 吞异常、调用方仍报成功 | 已修：失败返回 False + `error("tmux send 重试耗尽…")`，`src/tmux_ops.py:421-451` |
| L4 | ondemand 哨兵 `tmux_session=""` 判为存活 | 已修：空串直接 False，`src/tmux_ops.py:215-218` |
| L5 | feed_listener 跨进程不回收、孤儿常驻 | 已修：pidfile `~/.hermes/run/feed_listeners/<tmux>.pid`，`core.py:1282`、`feed_listener.py:144-169` |
| L6 | 限流把绿/黄消息一并计入 | 已修：SQL 只数 `sensitivity='red'`，`gatekeeper.py:296` |
| L7 | `_check_bus_source` 的 `"partial"` 当通过 | 已修：`partial` 分支已删（`grep partial src/routing/gatekeeper.py` 无命中） |
| L8 | 3 连败计数键用 role，忽略 instance_id | 已修：键用含实例的 `tmux_name`，`core.py:951-957` |
| L9 | 红线 1 拦截通知写 bus 失败被静默吞 | 已修：`_log.error` 留痕，`core.py:983` |
| L10 | 文档指向已删模块/不存在参数 | 本轮修：PUBLIC_API / TECHNICAL_DEEP_DIVE / README 重写 |

跨项目两条归属仍在对方仓：L2 的 persona workgroup（hermes-session-roles）、L5 的单元 `KillMode`（session-pipeline）。本仓无法验证其当前部署值。
