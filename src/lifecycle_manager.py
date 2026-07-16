"""lifecycle_manager — confirm_step bridge for CLAUDE.md references.

Referenced in 12+ CCS role files.  Delegates to lifecycle.manager.LifecycleManager.
"""
from typing import Optional
from lifecycle.manager import LifecycleManager as _RealLM

class LifecycleManager:
    """Thin wrapper matching CLAUDE.md usage. Delegates to lifecycle.manager."""

    def __init__(self, role: str):
        self._lm = _RealLM(role)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._lm.close()
        return False

    def confirm_step(self, wf_id: str, step_id: str) -> None:
        self._lm.confirm_step(wf_id, step_id)

    def start_wf(self, wf_id: str) -> bool:
        return self._lm.start_wf(wf_id)

    def complete_step(self, wf_id: str, step_id: str) -> str:
        return self._lm.complete_step(wf_id, step_id)

    def fail_step(self, wf_id: str, step_id: str,
                  reason: str = "", allow_retry: bool = False) -> bool:
        return self._lm.fail_step(wf_id, step_id, reason, allow_retry)

    def close_wf(self, wf_id: str):
        self._lm.close_wf(wf_id)

    def get_wf(self, wf_id: str) -> Optional[dict]:
        return self._lm.get_wf(wf_id)

    def get_step(self, wf_id: str, step_id: str) -> Optional[dict]:
        return self._lm.get_step(wf_id, step_id)

    def close(self):
        self._lm.close()

__all__ = ["LifecycleManager"]
