
"""
routing/gateway.py — Thin wrapper delegating to workflow.gateway.Gate.
Kept for backwards compat; all logic lives in workflow/gateway.py.
"""
import sys
from pathlib import Path

# Ensure launcher + pipeline src are on path (workflow.gateway 在 pipeline 中)
_src = Path(__file__).resolve().parent.parent
for _p in [_src, _src.parent / "session-pipeline" / "src"]:
    _ps = str(_p.resolve())
    if _ps not in sys.path:
        sys.path.insert(0, _ps)

from workflow.gateway import Gate  # noqa: F401 - re-export
