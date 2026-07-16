
"""
routing/gateway.py — Thin wrapper delegating to workflow.gateway.Gate.
Kept for backwards compat; all logic lives in workflow/gateway.py.
"""
import sys
from pathlib import Path

# Ensure launcher src is on path for import
_src = Path(__file__).resolve().parent.parent
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from workflow.gateway import Gate  # noqa: F401 - re-export
