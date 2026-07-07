# Bus Push Architecture 设计 V1

## 整体架构

```
                    ┌─────────────────────────────────────┐
                    │      sister_socket_server.py         │
                    │  (已存在, asyncio Unix Socket Server) │
                    │                                     │
                    │  AGENT_NAMES = [dkk, ssk, cron,     │
                    │                  FEED ← 新增]       │
                    │                                     │
                    │  /tmp/sister_bus_dkk.sock           │
                    │  /tmp/sister_bus_ssk.sock           │
                    │  /tmp/sister_bus_cron.sock          │
                    │  /tmp/sister_bus_feed.sock ← 新增   │
                    └──────────────────┬──────────────────┘
                                       │
          ┌────────────────────────────┼────────────────────────────┐
          │                            │                            │
          ▼                            ▼                            ▼
┌──────────────────┐    ┌─────────────────────────┐    ┌──────────────────┐
│ bus_protocol.py  │    │  ccs-verifier (监听器)    │    │ ccs-monitor      │
│ Blackboard.write │    │  subscribe feed socket    │    │ (监听器)         │
│  └→ INSERT facts  │    │ 收到 push → 实时反应      │    │ 收到 push → 检测 │
│  └→ PUSH to feed │    │                         │    │ 终局标记         │
└──────────────────┘    └─────────────────────────┘    └──────────────────┘
```

## 核心数据流

```
bus_client.py write debate "<消息>" --src pro
  │
  ├─ Blackboard.write() → INSERT INTO facts (SQLite)
  │
  └─ _notify_feed() → 通过临时 socket 连接
       │              发送 {"event":"new_fact",
       │                    "cat":"debate",
       Press│                    "id": 7563,
       │                     "src": "pro",
       │                     "title": "正方第39轮: ..."}
       │
       ▼
  /tmp/sister_bus_feed.sock
       │
       ▼
  socket_server.py 转发给所有 feed 订阅者
       │
       ├── ccs-verifier → 收到 push → 处理
       ├── ccs-monitor  → 收到 push → 检查终局关键词
       └── ...其他订阅方
```

## 改动范围

### 1. `socket_server.py` — 加 `FEED` agent + broadcast 行为

```python
AGENT_NAMES = ("dkk", "ssk", "cron", "feed")

# FEED 特殊处理: PUSH 到所有订阅者（broadcast）
if agent == "feed":
    for name in AGENT_NAMES:
        await self._push_to(name, cmd)
else:
    # 原有单播逻辑
    await self._push_to(target, cmd)
```

### 2. `bus_protocol.py` — 写完后 socket push

```python
_FEED_SOCKET = "/tmp/sister_bus_feed.sock"

def _notify_feed(cat: str, fact_id: int, text: str, src: str):
    """Best-effort push to feed socket after Blackboard write."""
    import socket
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(_FEED_SOCKET)
        payload = json.dumps({
            "cmd": "PUBLISH",
            "to": "feed",
            "msg": {"event": "new_fact", "cat": cat, "id": fact_id,
                    "src": src, "title": text[:200]},
        }) + "\n"
        s.sendall(payload.encode())
        s.shutdown(socket.SHUT_WR)
        s.close()
    except Exception:
        pass  # feed socket 不可用 = 无监听者, 静默忽略

# Blackboard.write() 末尾加:
_notify_feed(cat, fid, text, src)
```

### 3. `ccs-monitor` 侧监听脚本

```python
# ~/.hermes/scripts/feed_listener.py
import json, socket, subprocess

s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect("/tmp/sister_bus_feed.sock")
s.sendall(b'{"cmd":"SUBSCRIBE","agent":"feed"}\n')

while True:
    line = s.readline()  # 阻塞, 有消息立即返回
    event = json.loads(line)
    if event.get("event") == "new_fact":
        cat = event.get("cat")
        title = event.get("title", "")
        # 检测终局关键词
        if any(k in title for k in ("终局","最终","收束","结束","收官")):
            subprocess.run([
                "python3", "~/.hermes/scripts/bus_client.py", "write",
                "notice", f"[monitor] 辩论已结束: {title}", "--src", "monitor"
            ])
```

## 零轮询保证

| 场景 | 延迟 | 系统调用 |
|------|------|---------|
| 新消息写入 → CCS 收到 | ~1-5ms | 1 次 socket write |
| CCS 监听 | 实时阻塞 | 0（有消息才唤醒） |
| socket 不可用 | — | 静默降级, 不丢数据 |
| socket 恢复 | — | 自动重连 |

## 降级保障

- **链路**: Blackboard.write() → _notify_feed() (failure is silent)
- **核心不变**: SQLite 写入是主路径，socket push 是辅路径
- **CCS 端**: socket 断线不影响 bus 正常工作，只是收不到实时通知
- **补充**: CCS 仍可定期 `read --cat debate --limit 1` 兜底（回退到轮询）

## 文件清单

| 文件 | 改动 |
|------|------|
| `hermes_core/sister_bus/socket_server.py` | 加 `feed` agent + broadcast |
| `hermes/scripts/bus_protocol.py` | `write()` 末尾加 `_notify_feed()` |
| `~/.hermes/scripts/feed_listener.py` | 新增: feed 监听脚本 |
| CCS_COLLAB_PROTOCOL.md | 更新: 推送模式说明 |

## 不做的

| 方案 | 不做理由 |
|------|----------|
| Redis Pub/Sub | 零外部依赖, 单机场景 sock 够用 |
| WebSocket | 无外部客户端, 不需要 WS 协议 |
| HTTP callback | 多一层协议栈, 增加延迟 |
| 修改现有 sister_bus_*.sock | 不影响 DKK/SSK 现有通信路 |
