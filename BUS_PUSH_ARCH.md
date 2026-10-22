# Bus Push Architecture

> 2026-07-07 设计稿，2026-10-22 按代码复核。**现状与设计不符之处已在下文标出**。

## 消息流（实测）

```
bus_client.py write debate "终局"
  └─ SQLite facts 表（唯一写入路径）

hermes_bus/client.py:43 publish(to_agent, ...)
  └─ PUBLISH → /tmp/sister_bus_feed.sock
       └─ socket_server._dispatch（hermes_bus/socket_server.py:144-152）
            └─ _push_to("feed") → 所有订阅者
                 └─ feed_listener.py → 检测终局关键词 → 写 bus notice
```

## 与设计稿的差异（这三条是重点）

| 设计稿 | 实测 |
|---|---|
| `Blackboard.write()` 末尾调 `_notify_feed()` 自动推送 | **不存在**：`grep -rn _notify_feed ~/.hermes/scripts` 无命中；`bus_client.py` 无 `PUBLISH` |
| 服务端在 `hermes_core/sister_bus/socket_server.py` | 实际监听进程是 `~/.hermes/scripts/sister_socket_server.py` → `hermes_bus` 包；`hermes_core/sister_bus/socket_server.py` 只是 5 行兼容 shim |
| `AGENT_NAMES = (dkk, ssk, cron, feed)` | 属实，见 `hermes_bus/socket_server.py:44` |

结论：**写入不会自动推送**。要推送必须有人显式 `publish('feed', …)`，当前没有调用方
（`ss -xp | grep feed` 能看到多条 ESTAB 订阅者连接，说明订阅端活着，但不证明有消息；条数随进程重启变化，别写死）。

## 组件

| 组件 | 职责 | 路径 |
|------|------|------|
| Blackboard 存储 | facts 表读写（FTS5） | `~/.hermes/sister_bus/blackboard.db` |
| socket server | asyncio，SUBSCRIBE/PUBLISH/PRESENCE | `~/.hermes/scripts/hermes_bus/socket_server.py` |
| feed socket | Unix Socket 通知通道 | `/tmp/sister_bus_feed.sock` |
| feed_listener | 订阅 + 终局检测 + 注入 tmux | `session-launcher/feed_listener.py` |

## feed_listener 行为（可验证）

- 连接失败/断线 → 5s 重试（`feed_listener.py:171-198`）。
- `--tmux-target <ccs-role>` 时按 `~/.hermes/run/feed_listeners/<tmux>.pid` 做单实例锁（`:140-174`）。
- 注入用两次 `tmux send-keys`：`-l` 发正文，再单独发 `Enter`（`:120-127`）。
- 角色忙（`_role_is_busy`）时跳过注入，事实本身仍在 bus 里。

## 降级

- socket 不可用 → 写入照常（SQLite 是主路径），只是没人收到实时通知。
- 历史设计稿里的「20/20 到达、43-51ms」是 2026-07-07 的观测，当前代码下无法复验。

## 不做的

| 方案 | 理由 |
|------|------|
| Redis Pub/Sub | 单机场景 Unix Socket 够用，零外部依赖 |
| WebSocket | 无外部客户端 |
| HTTP callback | 多一层协议栈 |
