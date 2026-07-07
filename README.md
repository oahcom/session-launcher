# Session Launcher（CCS 生命周期管理器）

Session 生态的**执行层**——创建 CCS 进程、管理生命周期、提供跨 session 协作基础设施。

> ⚠️ **本项目实现所有协作逻辑（主从/对等/仲裁），代码层面而非 prompt 文本。**

---

## 在整体架构中的位置

```
┌─────────────────────────────────────────────────────────────────────────┐
│                      Hermes Session Ecosystem                          │
│                                                                         │
│  ┌───────────────────────┐                                             │
│  │  hermes-session-roles │                                             │
│  │  身份模板 JSON          │                                            │
│  └───────────┬───────────┘                                             │
│              │ 读取角色定义                                              │
│              ▼                                                          │
│  ┌──────────────────────────────────────────────────────────────┐      │
│  │  session-launcher  ← 本项目（执行层）                         │      │
│  │                                                               │      │
│  │  ┌─────────────────────────────────────────────────────┐    │      │
│  │  │ ccs.py                                              │    │      │
│  │  │ - start_ccs()     创建 tmux + claude 进程           │    │      │
│  │  │ - stop_ccs()      终止 CCS 并清理哨兵              │    │      │
│  │  │ - ccs_status()    列出所有 CCS 运行状态             │    │      │
│  │  │ - send_to_ccs()   向 CCS 发送消息                  │    │      │
│  │  │ - inject_prompt() 注入角色 prompt 到 CLAUDE.md      │    │      │
│  │  └─────────────────────────────────────────────────────┘    │      │
│  │                                                               │      │
│  │  ┌─────────────────────────────────────────────────────┐    │      │
│  │  │ ccs_socket.py（新增）                                │    │      │
│  │  │ - CSSocketServer    独立 Unix Socket Server         │    │      │
│  │  │ - PUBLISH/SUBSCRIBE <1ms 直连（不走 bus SQLite）    │    │      │
│  │  │ - CCSStreamer       tmux 流式输出                   │    │      │
│  │  │ - CCS_SOCKET_TOKEN  认证保护                        │    │      │
│  │  └─────────────────────────────────────────────────────┘    │      │
│  │                                                               │      │
│  │  ┌─────────────────────────────────────────────────────┐    │      │
│  │  │ 协作基础设施（代码内置，不是 prompt 文本）           │    │      │
│  │  │ - watchdog 线程：伙伴存活检查 + 自动重启            │    │      │
│  │  │ - turn_tracker：轮次追踪 + 死锁检测 + 提醒         │    │      │
│  │  │ - health_check：tmux 存活 + PID 验证               │    │      │
│  │  │ - sentinel 系统：/tmp/ccs-sentinels/ 哨兵文件       │    │      │
│  │  └─────────────────────────────────────────────────────┘    │      │
│  └──────────────────────────┬───────────────────────────────────┘      │
│                              │                                          │
│              ┌───────────────┴───────────────┐                         │
│              ▼                               ▼                         │
│  ┌──────────────────────┐       ┌──────────────────────────────────┐   │
│  │ session-pipeline     │       │        Sister Bus (SQLite)       │   │
│  │ 路由层               │       │  消息传递 / FTS5 全文检索         │   │
│  └──────────────────────┘       └──────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 三种协作模式（CLI 内置，非 prompt）

### 主从模式（Primary-Replica）

一个 CCS 守护另一个 CCS，挂了自动重启。

```bash
python3 ccs.py start verifier "辩论正方" \
  --partner rebutter \
  --auto-restart \
  --no-attach
```

**内部行为：**
- 启动 `ccs-verifier` tmux session
- 写哨兵 `/tmp/ccs-sentinels/verifier.json`（含 `partner: rebutter`）
- 启动 watchdog 线程，每 30 秒检查 `ccs-rebutter` 存活
- rebutter 挂了 → 自动重启 `python3 ccs.py start rebutter --no-attach`

### 对等模式（Peer-to-Peer）

两个 CCS 独立工作，通过 bus 轮次标记协调。

```bash
python3 ccs.py start pro "正方" --bus-track debate --no-attach
python3 ccs.py start rebutter "反方" --bus-track debate --no-attach
```

**内部行为：**
- 两个 CCS 各自启动
- 各启动 turn_tracker 线程，每 10 秒 poll bus 最新时间戳
- 超过 5 分钟无新消息 → 自动发 bus 提醒

### 仲裁模式（Arbitrator）

第三方 CCS 监控所有参与者的存活和进展。

```bash
python3 ccs.py start monitor "仲裁" \
  --partner verifier \
  --partner rebutter \
  --bus-track debate \
  --no-attach
```

**内部行为：**
- 启动 monitor CCS
- watchdog 同时守护两个伙伴
- turn_tracker 监控 bus 辩论进展
- 死锁时自动发唤醒指令或重启卡住方

---

## 命令行接口

```bash
# 启动 CCS（默认 attach）
python3 ccs.py start <role> [title]

# 后台启动
python3 ccs.py start <role> --no-attach

# 启动 + 自动注入初始 prompt
python3 ccs.py start <role> --prompt "你的指令"

# 启动 + 守护伙伴（主从模式）
python3 ccs.py start <role> --partner <partner> --auto-restart

# 启动 + 轮次追踪（对等模式）
python3 ccs.py start <role> --bus-track <cat>

# 查看所有 CCS 状态
python3 ccs.py status

# 停止 CCS
python3 ccs.py stop <role>

# 向 CCS 发消息
python3 ccs.py send <role> "消息内容"

# 查看 CCS 输出
python3 ccs.py output <role> --tail 30

# 流式输出（实时跟踪）
python3 ccs.py stream <role>

# CCS 直接通信（不走 bus，<1ms）
python3 ccs.py send-direct <from_role> <to_role> "消息"

# CCS Socket 管理
python3 ccs.py socket start    # 启动 CCS Socket Server
python3 ccs.py socket status   # 查看已注册 agent
```

---

## 哨兵系统

每个 CCS 启动时写入哨兵文件：

```json
{
  "role": "verifier",
  "title": "辩论正方",
  "tmux_session": "ccs-verifier",
  "pid": 12345,
  "started_at": 1783338345.4655178,
  "lifecycle": "infinite",
  "partner": "rebutter",          // 主从模式：被守护方
  "bus_track": "debate"           // 对等模式：追踪的 bus 分类
}
```

**哨兵文件位置：** `/tmp/ccs-sentinels/<role>.json`

**哨兵用途：**
- `ccs.py status` 扫描哨兵列所有 CCS
- watchdog 线程读哨兵获取 partner 信息
- turn_tracker 读哨兵获取 bus_track 信息
- 停止 CCS 时自动删除哨兵

---

## Bus Push 实时推送

**零轮询、毫秒级延迟**的实时消息推送机制。

### 数据流

```
bus_client.py write debate "<消息>"
  └─ bus_protocol.Blackboard.write()
       ├─ INSERT INTO facts (SQLite)  ← 主路径，持久化
       └─ _notify_feed()              ← 辅路径，实时推送
            └─ /tmp/sister_bus_feed.sock
                 └─ socket_server.py → broadcast
                      ├── ccs-verifier → 实时收到
                      ├── ccs-monitor  → 实时收到
                      └── feed_listener.py → 检测辩论结束
```

### 依赖服务

| 服务 | 路径 | 职责 |
|------|------|------|
| socket_server.py | ~/.hermes/scripts/hermes_core/sister_bus/ | asyncio Unix Socket Server，含 feed agent |
| bus_protocol.py | ~/.hermes/scripts/bus_protocol.py | Blackboard write() 末尾调用 _notify_feed() |
| feed_listener.py | session-launcher/feed_listener.py | 实时监听 feed socket，检测关键词 |

### 降级保障

- **feed socket 不可用** → `_notify_feed()` 静默异常，**不丢 SQLite 写入**
- **监听脚本断线** → 自动重连 + 指数退避
- **CCS 兜底** → 仍可用 `read --cat debate --watch` 回退到轮询模式

---

## CCS Socket 直连通信

**独立 Unix Socket Server**，支持动态角色注册。不走 SQLite bus，<1ms 延迟。

### 架构

```
ccs.py send-direct alice bob "hi"
  └─ CCSClient("alice").connect()
       └─ SUBSCRIBE to /tmp/ccs-sockets/ccs.sock
            └─ CSSocketServer 路由
                 ├─ PUBLISH → bob ← CCSClient("bob").listen()  <1ms
                 └─ agents.json 注册所有在线 agent
```

### 协议

JSON-over-newline（JSONL），端口无关：

| Cmd | 方向 | 说明 |
|-----|------|------|
| `SUBSCRIBE` | client → server | 注册 agent，传入 `token` 认证 |
| `PUBLISH` | client → server | 发消息给 `to` agent |
| `PING` / `STATS` | client → server | 健康/统计查询 |
| `message` | server → client | 收到的新消息 |
| `subscribed` | server → client | 确认注册成功 |

### 认证

```bash
export CCS_SOCKET_TOKEN=my_secret_key
python3 ccs.py socket start   # server 启用认证
python3 ccs.py send-direct ... # client 自动携带 token
```

不匹配的 token → server 断开连接并返回 `{"event": "error", "detail": "auth failed"}`。

### 异常处理

- 连接断开 → client 感知 EOF，停止 listen
- server 挂掉 → 异常日志化，不静默吞掉
- 消息过大 → asyncio limit=64KB 自动断开

---

## 流式输出

```bash
python3 ccs.py stream <role>
```

替代 `ccs.py output` 的单次截取，采用 **0.5s 增量轮询** `tmux capture-pane`，
只推送新增行。

```python
from ccs_socket import CCSStreamer

streamer = CCSStreamer('verifier')
streamer.start(lambda chunk: print(chunk, end=''))
# ... 持续输出 ...
streamer.stop()
```

---

## 系统别名（已注册到 ~/.bash_aliases）

```bash
ccs <role> [title]          # 启动 CCS 并 attach
ccs <role> --no-attach      # 后台启动
ccs-status                  # 列出所有 CCS
ccs-ls                      # tmux 列表（只看 ccs-）
ccs-stop <role>             # 停止 CCS
ccs-send <role> "消息"       # 向 CCS 发消息
ccs-out <role>              # 查看 CCS 输出
ccs-stream <role>           # 流式输出（实时跟踪）
```

---

## 项目结构

```
session-launcher/
  src/
    ccs.py               → CCS 核心（start/stop/status/send/socket/stream）
    ccs_socket.py        → CCS Socket Server + 流式输出（新增）
    launcher.py          → 旧版启动器（保留向后兼容）
    signals.py           → 8 种信号检查器
    ccs_start.py         → 旧版独立启动器（待重构为 ccs.py）
    worker_pool.py       → 并行 HTTP 调用 9Router
  tests/
    test_e2e_mock.py     → 19 个 mock 测试
  CCS_COLLAB_PROTOCOL.md → 协作协议文档（根因分析 + 规范）
  deploy_all.sh          → 全角色一键部署
  ccs_watchdog.sh        → 纯 bash 监控（零 LLM）
```

---

## 红线

1. **协作逻辑在代码层，不在 prompt 文本中**
2. **创建方必须守护被创建方**（watchdog 线程）
3. **轮次追踪必须内置**（turn_tracker 线程）
4. 绝不两个 CCS 同时处于被动监听（死锁）
5. 轮次号是唯一有序标识（防乱序）
6. CCS 重启后必须重建上下文（哨兵记录 partner/bus_track）
7. 死锁超时 > 15 分钟 → 写入 bus architecture 升级给人

---

## 架构设计文档

| 文档 | 内容 |
|------|------|
| `HERMES_SESSION_ARCHITECTURE.md` | 总体架构设计 V2：三层指令体系 + 双层监控 + 三驱动机制 |
| `BUS_PUSH_ARCH.md` | Bus Push 实时推送设计 V1 |
| `CCS_COLLAB_PROTOCOL.md` | CCS 协作协议（死锁预防 + 向后兼容设计） |
