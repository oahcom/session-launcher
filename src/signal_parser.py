#!/usr/bin/env python3
"""thin wrapper - re-export from events.parser (backward compat)"""

from events.parser import (
    parse_signal,
    _check_bus,
    _check_memory,
    _check_disk,
    _check_custom,
    _check_signal_legacy,
    _check_shell,
    _check_http,
    _check_journalctl,
    BUS_CLIENT,
)

__all__ = [
    "parse_signal",
    "_check_bus",
    "_check_memory",
    "_check_disk",
    "_check_custom",
    "_check_signal_legacy",
    "_check_shell",
    "_check_http",
    "_check_journalctl",
    "BUS_CLIENT",
]
