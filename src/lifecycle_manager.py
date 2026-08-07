"""backward-compat: LifecycleManager 已移至 pipeline 的 lifecycle.manager"""
import sys, warnings
from pathlib import Path

_pipeline_src = str(Path(__file__).resolve().parent.parent.parent / "session-pipeline" / "src")
if _pipeline_src not in sys.path:
    sys.path.append(_pipeline_src)

try:
    import lifecycle.manager as _lm
    set_role_budget = _lm.set_role_budget
    get_role_budget = _lm.get_role_budget
    check_resource_constraints = _lm.check_resource_constraints
    LifecycleManager = _lm.LifecycleManager
    __all__ = ["set_role_budget", "get_role_budget",
               "check_resource_constraints", "LifecycleManager"]
except ImportError:
    warnings.warn(f"无法从 {_pipeline_src}/lifecycle/manager.py 导入 LifecycleManager。"
                  "请迁移到 from lifecycle.manager import ...", DeprecationWarning, stacklevel=2)
    raise