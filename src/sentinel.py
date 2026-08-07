"""backward-compat: sentinel 已移至 ops.sentinel"""
import warnings
warnings.warn("sentinel 已移至 ops.sentinel，请改为 from ops.sentinel import ...", DeprecationWarning, stacklevel=2)
from ops.sentinel import (
    CcsSentinel,
    write_sentinel,
    read_sentinel,
    delete_sentinel,
    list_sentinels,
    update_health,
    SENTINEL_DIR,
    record_cross_session_action,
    get_cross_session_memory,
    get_all_cross_session_memories,
)

__all__ = [
    "CcsSentinel",
    "write_sentinel",
    "read_sentinel",
    "delete_sentinel",
    "list_sentinels",
    "update_health",
    "SENTINEL_DIR",
    "record_cross_session_action",
    "get_cross_session_memory",
    "get_all_cross_session_memories",
]
