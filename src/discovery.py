#!/usr/bin/env python3
"""
Layer 0: Message Discovery — 将 bus 消息转化为 workflow task。

每轮扫描：
  1. 读角色 persona JSON 的 input_signals
  2. 拉 bus 未消费消息，匹配 category + filter
  3. 创建 workflow task → mark_consumed

用法:
  from discovery import discover
  created = discover("pg")  # → [task_id, ...]
"""
import json, logging, os, time
from pathlib import Path

log = logging.getLogger("discovery")

_HERMES_SCRIPTS = Path(os.environ.get("HERMES_SCRIPTS_DIR", "~/.hermes/scripts")).expanduser()
_WORKFLOW_DB = Path.home() / ".hermes" / "state" / "workflows.db"
_SESSION_ROLES = Path(os.environ.get(
    "SESSION_ROLES_ROOT",
    Path.home() / "hermes-session-roles" / "personas" / "session-roles",
))


def _load_input_signals(role: str) -> list[dict]:
    for f in _SESSION_ROLES.glob(f"persona_*_{role}.json"):
        data = json.loads(f.read_text())
        return data.get("input_signals", [])
    return []


def _template_id_for(category: str) -> str:
    _MAP = {
        "bug_report": "WL-P1-01",
        "code_fix": "WL-P1-02",
        "task_spec": "WL-P0-01",
        "tech_decision": "WL-P2-01",
        "architecture": "WL-P2-02",
        "root_cause_analysis": "WL-P2-03",
        "test_report": "WL-P3-01",
    }
    return _MAP.get(category, "WL-P0-01")


def discover(role: str, mark_consumed: bool = True) -> list[str]:
    """执行一轮 Message Discovery，返回创建的任务 ID 列表。"""
    import sys
    sys.path.insert(0, str(_HERMES_SCRIPTS))
    sys.path.insert(0, str(_HERMES_SCRIPTS.parent / "session-pipeline" / "src"))

    from hermes_bus import Blackboard
    from workflow.client import WorkflowClient

    signals = _load_input_signals(role)
    bus_cats = set()
    for s in signals:
        if s.get("type") == "bus":
            cat = s.get("spec", {}).get("category")
            if cat and cat != "*":
                bus_cats.add(cat)

    if not bus_cats:
        return []

    bb = Blackboard()
    with WorkflowClient(role, db_path=str(_WORKFLOW_DB)) as wf:
        created = []
        for cat in bus_cats:
            facts = bb.unconsumed(cat=cat)
            for f in facts:
                tid, wid = wf.create_task_v2(
                    title=f.t[:200],
                    assignee=role,
                    template_id=_template_id_for(cat),
                    initiator_role="discovery",
                    description=(f.e or "")[:500],
                )
                created.append(tid)
                log.info("discover %s → task %s (cat=%s)", role, tid, cat)
                if mark_consumed:
                    bb.mark_consumed(f.id, consumer=f"discovery_{role}")
        return created
