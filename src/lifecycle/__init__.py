"""lifecycle 子包 —— 生命周期管理。"""

from lifecycle.manager import LifecycleManager, set_role_budget, get_role_budget, check_resource_constraints
from lifecycle.engine import StepEngine
from lifecycle.gate import LifecycleGate

__all__ = [
    "LifecycleManager",
    "set_role_budget",
    "get_role_budget",
    "check_resource_constraints",
    "StepEngine",
    "LifecycleGate",
]
