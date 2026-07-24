# verification_rules 设计方案

## 1. 问题

角色可能偷懒绕过工作流——完成步骤但不产出实际工作。需要在 `complete_step` 前验证产出物是否真的存在。

## 2. 核心思路

在模板步骤中定义 `verification_rules`，`complete_step` 时自动执行验证，不通过则拒绝推进。

```
executor 调用 complete_step()
    → 系统读取该步骤的 verification_rules
    → 逐条执行验证规则
    → 全部通过 → 允许推进
    → 任一失败 → 拒绝推进，返回失败原因
```

## 3. 数据结构

### 3.1 模板定义（新增字段）

```json
{
  "step_id": "s1",
  "title": "文档产出",
  "type": "handoff",
  "target_role": "product_architect",
  "prompt_template": "...",
  "approval_prompt": "...",
  "failure_patterns": ["..."],
  "verification_rules": [
    {
      "type": "file_exists",
      "path": "docs/*.md",
      "min_count": 1,
      "description": "至少存在1个文档文件"
    },
    {
      "type": "content_check",
      "path": "docs/*.md",
      "pattern": "背景与目标",
      "required": true,
      "description": "文档包含'背景与目标'章节"
    },
    {
      "type": "min_lines",
      "path": "docs/*.md",
      "min_lines": 50,
      "description": "文档至少50行"
    },
    {
      "type": "command_check",
      "command": "python3 -m py_compile src/*.py",
      "description": "代码编译通过"
    }
  ]
}
```

### 3.2 验证规则类型

| type | 参数 | 说明 |
|------|------|------|
| `file_exists` | path, min_count | 文件是否存在 |
| `content_check` | path, pattern, required | 文件内容是否包含关键词 |
| `min_lines` | path, min_lines | 文件最少行数 |
| `command_check` | command | 命令执行是否成功 |
| `custom` | function | 自定义验证函数 |

## 4. 实现

### 4.1 模板注册（修改 template_registry.py）

```python
# 新增 verification_rules 到 schema
"verification_rules": {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["file_exists", "content_check", "min_lines", "command_check", "custom"]},
            "path": {"type": "string"},
            "pattern": {"type": "string"},
            "command": {"type": "string"},
            "min_count": {"type": "integer", "minimum": 1},
            "min_lines": {"type": "integer", "minimum": 1},
            "required": {"type": "boolean"},
            "description": {"type": "string"}
        },
        "required": ["type", "description"]
    }
}
```

### 4.2 验证执行器（新增 verification.py）

```python
"""verification.py — 步骤产出物验证"""
import glob
import subprocess
from pathlib import Path
from typing import List, Dict, Tuple

class VerificationResult:
    def __init__(self):
        self.passed = True
        self.errors = []
    
    def fail(self, rule: dict, reason: str):
        self.passed = False
        self.errors.append({"rule": rule, "reason": reason})

def verify_rules(rules: List[Dict], work_dir: str = ".") -> VerificationResult:
    """执行验证规则列表"""
    result = VerificationResult()
    for rule in rules:
        rule_type = rule.get("type", "")
        
        if rule_type == "file_exists":
            _check_file_exists(rule, work_dir, result)
        elif rule_type == "content_check":
            _check_content(rule, work_dir, result)
        elif rule_type == "min_lines":
            _check_min_lines(rule, work_dir, result)
        elif rule_type == "command_check":
            _check_command(rule, work_dir, result)
    
    return result

def _check_file_exists(rule: dict, work_dir: str, result: VerificationResult):
    """检查文件是否存在"""
    path_pattern = rule.get("path", "")
    min_count = rule.get("min_count", 1)
    files = glob.glob(f"{work_dir}/{path_pattern}", recursive=True)
    if len(files) < min_count:
        result.fail(rule, f"需要至少 {min_count} 个文件，实际 {len(files)} 个")

def _check_content(rule: dict, work_dir: str, result: VerificationResult):
    """检查文件内容"""
    path_pattern = rule.get("path", "")
    pattern = rule.get("pattern", "")
    required = rule.get("required", True)
    files = glob.glob(f"{work_dir}/{path_pattern}", recursive=True)
    found = False
    for f in files:
        try:
            content = Path(f).read_text()
            if pattern in content:
                found = True
                break
        except Exception:
            continue
    if required and not found:
        result.fail(rule, f"未找到包含 '{pattern}' 的文件")

def _check_min_lines(rule: dict, work_dir: str, result: VerificationResult):
    """检查文件最少行数"""
    path_pattern = rule.get("path", "")
    min_lines = rule.get("min_lines", 1)
    files = glob.glob(f"{work_dir}/{path_pattern}", recursive=True)
    for f in files:
        try:
            lines = len(Path(f).read_text().splitlines())
            if lines < min_lines:
                result.fail(rule, f"{f} 只有 {lines} 行，需要至少 {min_lines} 行")
        except Exception:
            continue

def _check_command(rule: dict, work_dir: str, result: VerificationResult):
    """检查命令执行"""
    command = rule.get("command", "")
    try:
        proc = subprocess.run(command, shell=True, cwd=work_dir, 
                            capture_output=True, timeout=30)
        if proc.returncode != 0:
            result.fail(rule, f"命令执行失败: {proc.stderr.decode()[:200]}")
    except subprocess.TimeoutExpired:
        result.fail(rule, "命令执行超时")
    except Exception as e:
        result.fail(rule, f"命令执行异常: {e}")
```

### 4.3 修改 complete_step（lifecycle/manager.py）

```python
def complete_step(self, wf_id: str, step_id: str) -> str:
    with self._lock:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            # ... 现有逻辑 ...
            
            step = self.get_step(wf_id, step_id)
            if not step:
                raise ValueError(f"步骤定义不存在: {step_id}")
            
            # 新增：验证产出物
            verification_rules = step.get("verification_rules", [])
            if verification_rules:
                from verification import verify_rules
                work_dir = self._get_work_dir(wf_id)
                v_result = verify_rules(verification_rules, work_dir)
                if not v_result.passed:
                    error_msg = "; ".join([e["reason"] for e in v_result.errors])
                    self._log_unsafe(wf_id, task_id, "verification_failed",
                                     detail=f"{step_id} 验证失败: {error_msg}")
                    self._conn.commit()
                    return f"verification_failed: {error_msg}"
            
            # ... 继续现有逻辑 ...
```

## 5. 使用示例

### 5.1 注册带验证规则的模板

```python
reg.register({
    "workflow_id": "WL-09",
    "name": "文档驱动5阶段工作流",
    "steps": [
        {
            "step_id": "s1",
            "title": "文档产出",
            "type": "handoff",
            "target_role": "product_architect",
            "verification_rules": [
                {"type": "file_exists", "path": "docs/*.md", "min_count": 1},
                {"type": "content_check", "path": "docs/*.md", "pattern": "背景与目标", "required": True},
                {"type": "content_check", "path": "docs/*.md", "pattern": "验收标准", "required": True},
                {"type": "min_lines", "path": "docs/*.md", "min_lines": 50},
            ]
        },
        # ...
    ]
})
```

### 5.2 complete_step 自动验证

```python
# executor 调用
result = lm.complete_step(wf_id, "s1")

# 如果验证失败
if result.startswith("verification_failed"):
    print(f"验证失败: {result}")
    # executor 需要补充产出物后重试
```

## 6. 测试计划

| 测试 | 验证 |
|------|------|
| 文件存在检查 | 文件不存在时返回错误 |
| 内容检查 | 内容不包含关键词时返回错误 |
| 行数检查 | 行数不足时返回错误 |
| 命令检查 | 命令执行失败时返回错误 |
| 无验证规则 | 跳过验证，正常推进 |
| 验证通过 | 正常推进到下一步 |
| 验证失败 | 拒绝推进，返回错误原因 |

## 7. 部署步骤

1. 新增 `verification.py` 模块
2. 修改 `template_registry.py` 添加 `verification_rules` schema
3. 修改 `lifecycle/manager.py` 的 `complete_step` 方法
4. 更新模板 WL-09 添加验证规则
5. 运行测试验证
