#!/usr/bin/env python3
"""Deprecated — 此文件已迁移到 migration 子包。

请改用:
    from migration import pre_flight, dry_run_assessment, ...
"""
import warnings
warnings.warn(
    "migration_scripts is deprecated, use 'from migration import ...' instead.",
    DeprecationWarning, stacklevel=2,
)
from migration import (
    pre_flight,
    dry_run_assessment,
    export_backup,
    truncate_tables,
    restore_from_backup,
    run_migration,
    stale_task_scan,
)

# re-exports：backward-compat 向旧调用方暴露全部函数
__all__ = ["pre_flight", "dry_run_assessment", "export_backup",
           "truncate_tables", "restore_from_backup", "run_migration",
           "stale_task_scan"]
