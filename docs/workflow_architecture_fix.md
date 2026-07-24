# AI 工作流架构问题修复方案

## 问题总结

| # | 问题 | 现状 | 风险 |
|---|------|------|------|
| 1 | 交接过程没有保障 | complete_step 仅更新DB，无确认机制 | 下一角色不知道有新任务 |
| 2 | 角色没按工作流走通 | 角色启动时不知道自己在哪个步骤 | 跳过步骤或做无关的事 |
| 3 | 角色不知道工作流怎么走 | 工作流定义在DB里，角色无法实时感知 | 需要主动查询才知道下一步 |
| 4 | 角色偷懒绕过工作流 | 无验证机制 | 质量门禁形同虚设 |

## 解决方案

### 方案1: 交接确认机制（解决交接无保障）

**核心思路**: complete_step 后，被交接方必须确认收到，交接方等待确认。

```
executor 完成步骤
    → system 更新DB状态为 "handoff_pending"
    → system 发送 CCS 消息给下一角色，附带工作流上下文
    → 下一角色收到消息后，必须执行 check_task 确认
    → 确认后状态变为 "running"
    → 超时未确认 → 升级给 coordinator
```

**实现**:
- 新增 `handoff_status` 字段：`pending` / `acknowledged` / `timeout`
- complete_step 后启动确认计时器
- 下一角色 check_task 时自动确认
- 超时检查由 watchdog 执行

### 方案2: 工作流上下文注入（解决角色不知道工作流）

**核心思路**: 角色启动时和每次循环时，自动注入当前工作流上下文。

```
角色启动
    → 查询 assigned_workflows（分配给自己的工作流）
    → 注入到 CLAUDE.md 的 KNOWLEDGE 块
    → 每次循环时刷新上下文
```

**实现**:
- 新增 `get_assigned_workflows(role)` 方法
- 在 `inject_role_knowledge_into_workspace` 中注入工作流上下文
- 工作流上下文包含：当前步骤、下一步、审批状态、密钥

### 方案3: 工作流可见性（解决角色不知道怎么走）

**核心思路**: 工作流进度实时可见，角色随时知道自己的位置。

```
工作流状态面板（实时更新）
    ├── 当前步骤: s2 (编码实现)
    ├── 执行者: engineer
    ├── 审批状态: 等待 PM 审批
    ├── 密钥: 已生成，等待验证
    └── 下一步: s3 (代码审查) → reviewer
```

**实现**:
- 新增 `get_workflow_progress(wf_id)` 方法
- 在控制面板展示工作流进度
- 角色可通过 bus 查询实时状态

### 方案4: 工作流执行验证（解决偷懒绕过）

**核心思路**: 每个步骤必须通过验证才能推进，防止跳过。

```
验证点:
1. complete_step 前检查: 是否真的做了工作（产出物检查）
2. confirm_step 前检查: 审批人是否真的审查了
3. 工作流完成后检查: 所有步骤是否都有产出物
```

**实现**:
- 在模板中定义 `completion_check`（已有）
- 新增 `verification_rules` 字段
- 每个步骤必须满足 verification_rules 才能推进
- 最终检查：所有步骤的产出物必须存在

## 实现优先级

| 优先级 | 方案 | 影响范围 | 实现复杂度 |
|--------|------|----------|------------|
| P0 | 工作流上下文注入 | 角色启动流程 | 低 |
| P1 | 交接确认机制 | lifecycle manager | 中 |
| P2 | 工作流可见性 | 控制面板 + bus | 中 |
| P3 | 执行验证 | 模板定义 + 校验 | 高 |

## 详细设计

### P0: 工作流上下文注入

```python
# lifecycle/manager.py 新增方法
def get_assigned_workflows(self, role: str) -> List[dict]:
    """获取分配给指定角色的所有活跃工作流"""
    rows = self._conn.execute(
        """SELECT wi.*, wt.name as template_name, wt.steps_json
           FROM workflow_instances wi
           JOIN workflow_templates wt ON wi.template_id = wt.template_id
           WHERE wi.status = 'running'
           AND wi.assigner = ?
           ORDER BY wi.created_at DESC""",
        (role,)
    ).fetchall()
    return [dict(r) for r in rows]

def get_workflow_context(self, wf_id: str) -> dict:
    """获取工作流完整上下文（用于注入角色 prompt）"""
    wf = self._get_wf_unsafe(wf_id)
    if not wf:
        return {}
    
    steps = self._get_all_steps_unsafe(wf.get("template_id"))
    current_step = wf.get("current_step_id", "")
    results = self._parse_results(wf)
    
    # 构建上下文
    context = {
        "wf_id": wf_id,
        "template_name": wf.get("template_id"),
        "current_step": current_step,
        "status": wf.get("status"),
        "assigner": wf.get("assigner"),
        "steps": [],
    }
    
    for step in steps:
        step_result = results.get(step["step_id"], {})
        context["steps"].append({
            "step_id": step["step_id"],
            "title": step["title"],
            "type": step["type"],
            "target_role": step.get("target_role"),
            "status": step_result.get("status", "pending"),
            "approval_prompt": step.get("approval_prompt", ""),
        })
    
    return context
```

### P1: 交接确认机制

```python
# lifecycle/manager.py 新增方法
def acknowledge_handoff(self, wf_id: str, step_id: str, role: str) -> bool:
    """被交接方确认收到任务"""
    wf = self._get_wf_unsafe(wf_id)
    if not wf:
        return False
    
    # 验证角色是否是下一步的执行者
    next_step = self._get_next_step(wf, step_id)
    if not next_step or next_step.get("target_role") != role:
        return False
    
    # 更新状态
    results = self._parse_results(wf)
    step_result = results.get(step_id, {})
    step_result["handoff_status"] = "acknowledged"
    step_result["acknowledged_at"] = time.time()
    step_result["acknowledged_by"] = role
    results[step_id] = step_result
    
    self._conn.execute(
        "UPDATE workflow_instances SET step_results=? WHERE instance_id=?",
        (json.dumps(results, ensure_ascii=False), wf_id))
    self._conn.commit()
    
    return True
```

### P2: 工作流可见性

```python
# lifecycle/manager.py 新增方法
def get_workflow_progress(self, wf_id: str) -> dict:
    """获取工作流实时进度"""
    wf = self._get_wf_unsafe(wf_id)
    if not wf:
        return {}
    
    steps = self._get_all_steps_unsafe(wf.get("template_id"))
    current_step = wf.get("current_step_id", "")
    results = self._parse_results(wf)
    
    progress = {
        "wf_id": wf_id,
        "status": wf.get("status"),
        "current_step": current_step,
        "steps": [],
    }
    
    for i, step in enumerate(steps):
        step_result = results.get(step["step_id"], {})
        progress["steps"].append({
            "index": i + 1,
            "step_id": step["step_id"],
            "title": step["title"],
            "target_role": step.get("target_role"),
            "status": step_result.get("status", "pending"),
            "is_current": step["step_id"] == current_step,
            "handoff_status": step_result.get("handoff_status", "N/A"),
        })
    
    return progress
```

### P3: 执行验证

```python
# 模板定义中新增 verification_rules
{
    "step_id": "s1",
    "title": "文档产出",
    "verification_rules": [
        {"type": "file_exists", "path": "docs/*.md", "min_count": 1},
        {"type": "content_check", "pattern": "背景与目标", "required": True},
        {"type": "content_check", "pattern": "验收标准", "required": True},
    ]
}
```

## 测试计划

| 测试 | 验证 |
|------|------|
| 交接确认 | complete_step 后，被交接方能收到并确认 |
| 上下文注入 | 角色启动时能获取工作流上下文 |
| 进度可见 | 能查询到工作流实时进度 |
| 执行验证 | 未完成验证的步骤无法推进 |
| 超时升级 | 超时未确认自动升级给 coordinator |
