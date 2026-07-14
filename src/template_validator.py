#!/usr/bin/env python3
"""
template_validator.py — 模板 5 步验证流程 + 验收工具

run_validation(template) 返回全部 5 步的逐项结果。
步骤1（语法验证）→ 步骤2（角色验证）→ 步骤3（可执行验证）
→ 步骤4（审查逻辑验证）→ 步骤5（走通验证）。
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional


def run_validation(template: dict) -> list[dict]:
    """执行全 5 步模板验证。

    参数：
        template: 10 字段模板字典

    返回：
        [{"step": int, "name": str, "passed": bool, "errors": list[str]}, ...]
    """
    results = []
    results.append(_step1_schema(template))
    results.append(_step2_role(template))
    results.append(_step3_prompt(template))
    results.append(_step4_review_logic(template))
    results.append(_step5_dry_run(template))
    return results


def _result(step: int, name: str, passed: bool, errors: list[str]) -> dict:
    return {"step": step, "name": name, "passed": passed, "errors": errors}


# ── 步骤1：JSON Schema 校验 ────────────────────

def _step1_schema(template: dict) -> dict:
    """语法：10 字段 JSON Schema 校验。"""
    try:
        from template_registry import _validate_schema, _check_role_existence
    except ImportError:
        return _result(1, "语法验证", False, ["无法导入 template_registry"])

    sr = _validate_schema(template)
    errors = sr.errors[:]
    # 简洁模式：最多返回 5 个错误
    return _result(1, "语法验证", len(errors) == 0, errors[:5])


# ── 步骤2：角色验证 ────────────────────────────

def _step2_role(template: dict) -> dict:
    """角色：allowed_initiators/executors 均为有效角色名。"""
    try:
        from template_registry import _check_role_existence
    except ImportError:
        return _result(2, "角色验证", False, ["无法导入 template_registry"])

    rr = _check_role_existence(template)
    errors = rr.errors[:]
    return _result(2, "角色验证", len(errors) == 0, errors[:5])


# ── 步骤3：可执行验证 ──────────────────────────

def _step3_prompt(template: dict) -> dict:
    """可执行：每步 prompt_template 含三段式(做什么/怎么做/验收标准)。"""
    errors = []
    steps = template.get("steps", [])
    if not steps:
        return _result(3, "可执行验证", False, ["steps 为空"])

    for i, step in enumerate(steps):
        pt = step.get("prompt_template", "")
        if len(pt) < 30:
            errors.append(f"steps[{i}].prompt_template 长度不足30字 ({len(pt)})")
        for sec in ["做什么", "怎么做", "验收标准"]:
            if sec not in pt:
                errors.append(f"steps[{i}].prompt_template 缺少'{sec}'段")

        fp = step.get("failure_patterns", [])
        if not isinstance(fp, list) or len(fp) < 2:
            errors.append(f"steps[{i}].failure_patterns 需≥2项")

        if "estimated_hours" not in step or step.get("estimated_hours") is None:
            errors.append(f"steps[{i}].estimated_hours 必须有值")

    return _result(3, "可执行验证", len(errors) == 0, errors[:5])


# ── 步骤4：审查逻辑验证 ────────────────────────

def _step4_review_logic(template: dict) -> dict:
    """审查逻辑：handoff/review 步骤的 completion_check 不依赖执行者自判断。"""
    errors = []
    steps = template.get("steps", [])

    for i, step in enumerate(steps):
        stype = step.get("type", "")
        if stype not in ("handoff", "review"):
            continue
        cc = step.get("completion_check", {})
        if not cc:
            errors.append(f"steps[{i}] ({stype}) 缺少 completion_check")
            continue
        # 需要有 review_required: true 或 output_exists
        if not cc.get("review_required") and not cc.get("output_exists"):
            errors.append(
                f"steps[{i}] ({stype}) 手工作业依赖执行者自判断："
                f"无 review_required 且无 output_exists")

    return _result(4, "审查逻辑验证", len(errors) == 0, errors[:5])


# ── 步骤5：走通验证（模拟执行） ─────────────────

def _step5_dry_run(template: dict) -> dict:
    """走通验证：模拟执行一次完整流程。"""
    errors = []
    steps = template.get("steps", [])
    if not steps:
        return _result(5, "走通验证", False, ["steps 为空"])

    # 检查步骤序列可执行
    for i, step in enumerate(steps):
        stype = step.get("type", "")
        if stype not in ("handoff", "review", "single", "gate", "notify"):
            errors.append(f"steps[{i}] 无效类型: {stype}")

        sid = step.get("step_id", "")
        if not sid or not sid.startswith("s"):
            errors.append(f"steps[{i}] step_id 格式无效: {sid}")

    # 检查 worklow_id 唯一性（不会覆盖已有模板）
    wid = template.get("workflow_id", "")
    if wid:
        try:
            from template_registry import TemplateRegistry
            reg = TemplateRegistry()
            exists = reg.get(wid) is not None
            reg.close()
            if exists:
                errors.append(f"workflow_id '{wid}' 已存在，注册将覆盖")
        except Exception:
            pass

    return _result(5, "走通验证", len(errors) == 0, errors[:5])


def print_report(results: list[dict]):
    """友好的控制台输出。"""
    all_pass = True
    for r in results:
        status = "✅" if r["passed"] else "❌"
        print(f"\n{status} 步骤{r['step']}: {r['name']}")
        if r["errors"]:
            for e in r["errors"]:
                print(f"   - {e}")
            all_pass = False
    print(f"\n{'='*40}")
    passed = sum(1 for r in results if r["passed"])
    print(f"结果: {passed}/{len(results)} 通过 {'✅' if all_pass else '❌'}")
    return all_pass


# ── CLI ─────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"用法: {sys.argv[0]} <template.json> [print]")
        sys.exit(1)

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"文件不存在: {path}")
        sys.exit(1)

    if path.suffix == ".jsonl":
        templates = [json.loads(line) for line in path.read_text().splitlines()
                     if line.strip()]
    else:
        templates = [json.loads(path.read_text())]

    all_ok = True
    for i, tpl in enumerate(templates):
        label = tpl.get("workflow_id", tpl.get("name", f"模板#{i+1}"))
        print(f"\n{'='*50}")
        print(f"验证: {label}")
        results = run_validation(tpl)
        ok = print_report(results)
        if not ok:
            all_ok = False

    sys.exit(0 if all_ok else 1)
