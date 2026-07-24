# 一次性审批密钥机制 设计文档

## 问题

当前 `confirm_step` 的 assigner 检查可以被伪造 — 同一个 AI 可以声明任意 role 来审批自己的工作，流程纪律形同虚设。

## 解决方案

用**一次性密钥**替代身份检查。步骤完成时生成密钥，发送给创建者，创建者用密钥审批。没有密钥就无法伪造。

## 数据流

```
executor (product_architect)          system                    assigner (pm)
        │                               │                           │
        ├── complete_step(s1) ──────────►│                           │
        │                               │ 生成 token (32字节)       │
        │                               │ 存储到 step_results       │
        │                               │ approval_prompt + token ──►│
        │                               │                           │
        │                               │◄── confirm_step(s1, token)│
        │                               │ 验证 token + 标记已消费    │
        │                               │ 审批通过，推进 s2          │
        │◄───────────────────────────────│                           │
```

## 模板变更

步骤增加 `approval_prompt` 字段：

```json
{
  "step_id": "s1",
  "title": "方案设计",
  "type": "handoff",
  "target_role": "product_architect",
  "approval_prompt": "请审查方案设计质量，确认是否通过。检查项：技术选型合理性、实现路径可行性、风险点覆盖。",
  "prompt_template": "做什么: 编写技术方案文档...",
  "completion_check": {"output_exists": ["DESIGN.md"]}
}
```

`approval_prompt` 为可选字段。缺失时使用默认提示词。

## 数据库变更

`workflow_instances.step_results` JSON 增加字段：

```json
{
  "s1": {
    "status": "step_done_ready",
    "completed_at": 1234567890,
    "completed_by": "product_architect",
    "approval_token": "abc123def456",
    "approval_token_ttl": 1234567950,
    "approval_consumed_at": null
  }
}
```

## Token 规范

| 属性 | 值 |
|------|-----|
| 长度 | 32 字节 URL-safe (`secrets.token_urlsafe(32)`) |
| 存储 | 明文存 DB（本地 SQLite，威胁模型为本地进程） |
| 有效期 | 1 小时（`ttl = completed_at + 3600`） |
| 消费 | 验证通过后 `approval_consumed_at = now()`，`approval_token = null` |
| 重放 | consumed_at 非空 → 拒绝 |

## API 变更

### complete_step（不变）

调用方式不变。内部行为变更：
1. handoff/review 类型步骤完成时生成一次性密钥
2. 密钥存储到 step_results（含 TTL）
3. ccs send-safe 给 assigner，附带 approval_prompt + 密钥
4. CCS 发送失败 → 记录日志，不阻塞流程（assigner 可主动查询）

### confirm_step

新增 `token` 参数：

```python
def confirm_step(self, wf_id: str, step_id: str, token: str = "") -> bool:
```

验证逻辑：
1. 从 step_results 读取存储的 approval_token 和 approval_token_ttl
2. 检查 TTL：过期 → PermissionError("token expired")
3. 检查消费：consumed_at 非空 → PermissionError("token already used")
4. 比对 token：匹配 → 审批通过，标记 consumed
5. 不匹配 → PermissionError("invalid token")
6. token 为空（旧调用）→ 降级到 assigner 检查

## CCS 消息格式

```
[workflow] 步骤审批请求

工作流: {wf_id}
步骤: {step_id} — {step_title}
完成者: {completed_by}
密钥: {token}
有效期: 1小时

审批提示:
{approval_prompt}

用密钥审批:
python3 src/ccs.py send {assignee} {executor} "confirm {wf_id} {step_id} {token}"
```

## 安全模型

| 攻击 | 防御 |
|------|------|
| AI 伪造 role | 密钥不在 role 里，在消息里 |
| AI 自己审批自己 | 密钥发给 assigner，executor 拿不到 |
| 重放攻击 | 一次性密钥，consumed_at 标记已消费 |
| 过期 token | TTL 1 小时，过期拒绝 |
| 中间人截获 | 密钥在 CCS socket 里传输，不走公网 |
| CCS socket 劫持 | socket mode 600/660，专用 group |

## 向后兼容

- `approval_prompt` 可选，缺失时用默认提示词
- `confirm_step` 的 `token` 参数默认空字符串，兼容旧调用
- 旧 workflow（无 token）用 assigner 检查兜底
- `approval_token_ttl` 和 `approval_consumed_at` 为可选字段

## 测试计划

| 测试 | 验证 |
|------|------|
| 有 approval_prompt 的步骤 | token 正确生成和验证 |
| 无 approval_prompt 的步骤 | 使用默认提示词 |
| token 不匹配 | PermissionError |
| token 过期 | PermissionError("token expired") |
| token 已消费 | PermissionError("token already used") |
| token 为空（旧调用） | 降级到 assigner 检查 |
| CCS 消息发送失败 | 不阻塞流程，记日志 |
| 并发审批 | 原子验证+消费，只有一个成功 |
