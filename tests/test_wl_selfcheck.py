#!/usr/bin/env python3
"""
Self-check: 4 WL user stories — all implemented in code.
No runtime deps (no DB, no imports). Just code validation.
Run: python3 tests/test_wl_selfcheck.py
"""

from pathlib import Path

SRC = Path.home() / "session-launcher/src"
files = {}
for p in SRC.rglob("*.py"):
    files[str(p.relative_to(SRC))] = p.read_text()

errors = []

# P1-02: create_task 强制关联 template_id
cl = files.get("workflow/client.py", "")
if "if template_id is None:" not in cl:
    errors.append("WL-P1-02: create_task missing template_id guard")
if "gate.validate_create_task" not in cl:
    errors.append("WL-P1-02: create_task_v2 missing Gate call")

gw = files.get("workflow/gateway.py", "")
for fn in ["is_template_exists","is_template_active","check_can_initiate","check_can_execute","is_valid_role","validate_create_task"]:
    if f"def {fn}" not in gw:
        errors.append(f"WL-P1-02: missing Gate.{fn}")

# P1-04: step_done_ready 中间态
mg = files.get("lifecycle/manager.py", "")
if "step_done_ready" not in mg:
    errors.append("WL-P1-04: step_done_ready missing from LifecycleManager")
if 'current.get("status") != "step_done_ready"' not in mg:
    errors.append("WL-P1-04: confirm_step missing step_done_ready validation")

en = files.get("lifecycle/engine.py", "")
if '"status": "step_done_ready"' not in en:
    errors.append("WL-P1-04: StepEngine missing step_done_ready returns")

# kanban_board — find the method, capture 400 chars
ki = cl.find("def kanban_board")
if ki == -1 or "step_done_ready" not in cl[ki:ki+800]:
    errors.append("WL-P1-04: kanban_board missing step_done_ready")

# P0-03: ccs send 路由门禁
rt = files.get("routing/router.py", "")
for key in ["class CrossRoleRouter","WORKGROUP_MATRIX","SENSITIVE_KEYWORDS_RED","def classify_message_content","SENSITIVE_RATE_LIMIT"]:
    if key not in rt:
        errors.append(f"WL-P0-03: {key} missing from router.py")

co = files.get("core.py", "")
if "CrossRoleRouter().intercept" not in co:
    errors.append("WL-P0-03: intercept not wired into core.send")
sd = co[co.find("def send"):co.find("def send")+400]
if "check_ccs_command_permission" not in sd:
    errors.append("WL-P0-03: check_ccs_command_permission not in send")

pa = files.get("routing/partner.py", "")
if "check_send_permission" in pa:
    errors.append("WL-P0-03: ADR-003 dead code remains in partner.py")

# P0-01: 分配者链
if "def _get_assigner_for_step" not in mg:
    errors.append("WL-P0-01: _get_assigner_for_step missing")
ab = mg[mg.find("def _get_assigner_for_step"):mg.find("def _get_assigner_for_step")+800]
if 'wf.get("assigner"' not in ab:
    errors.append("WL-P0-01: handoff step1 assigner = wf.assigner")
if 'steps[i - 1].get("target_role"' not in ab:
    errors.append("WL-P0-01: handoff stepN = prev.target_role")
cs = mg[mg.find("def confirm_step"):mg.find("def confirm_step")+800]
if "self.role != assigner" not in cs:
    errors.append("WL-P0-01: confirm_step missing caller==assigner check")

if errors:
    print(f"FAIL ({len(errors)}):")
    for e in errors:
        print(f"  [ ] {e}")
    raise SystemExit(1)
else:
    print("PASS: All 4 WL user stories validated in code.")
