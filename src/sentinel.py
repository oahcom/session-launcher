"""backward-compat: sentinel 已移至 ops.sentinel"""
import warnings
warnings.warn("sentinel 已移至 ops.sentinel，请改为 from ops.sentinel import ...", DeprecationWarning, stacklevel=2)
from ops import sentinel as _sentinel
from ops.sentinel import *

__all__ = _sentinel.__all__  # re-export the real module's API
