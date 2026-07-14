"""lifecycle_manager — confirm_step bridge for CLAUDE.md references.

Referenced in 12+ CCS role files.  Delegates to WorkflowClient.
"""
from typing import Optional
from workflow.client import WorkflowClient

class LifecycleManager:
    """Thin wrapper matching CLAUDE.md usage."""

    def __init__(self, role: str):
        self._wf = WorkflowClient(role)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._wf.close()
        return False

    def confirm_step(self, wf_id: str, step_id: str) -> None:
        self._wf._conn.execute(
            "UPDATE workflow_steps SET status='completed', completed_at=strftime('%s','now') "
            "WHERE instance_id=? AND step_id=?",
            (wf_id, step_id),
        )
        self._wf._conn.commit()
        self._wf._log(wf_id=wf_id, action="step_confirmed", detail=f"step={step_id}")

__all__ = ["LifecycleManager"]
