#!/usr/bin/env python3
"""thin wrapper — re-export from routing.roles"""

from routing.roles import (
    SESSION_ROLES_ROOT, SESSION_MARKER_START, SESSION_MARKER_END,
    load_roles, get_role, check_wake_permission,
    inject_role_knowledge_into_workspace, inject_prompt_into_claudemd,
    clear_injected_prompt, validate_ccs_execution,
    # Private helpers used by core.py
    _invalidate_role_cache, _forbidden_list, _action_templates,
    _build_role_prompt, _resolve_ws_paths, _validate_role_name,
    _ensure_bus_aliases_in_bashrc, _ROLE_NAME_RE,
    _WS_MARKER_START, _WS_MARKER_END,
    _FORBIDDEN_MAP, _FORBIDDEN_DISPLAY, _WAKE_PERMISSION_MAP, _CLAUDE_MD,
)

__all__ = [
    'SESSION_ROLES_ROOT', 'SESSION_MARKER_START', 'SESSION_MARKER_END',
    'load_roles', 'get_role', 'check_wake_permission',
    'inject_role_knowledge_into_workspace', 'inject_prompt_into_claudemd',
    'clear_injected_prompt', 'validate_ccs_execution',
    '_invalidate_role_cache', '_forbidden_list', '_action_templates',
    '_build_role_prompt', '_resolve_ws_paths', '_validate_role_name',
    '_ensure_bus_aliases_in_bashrc', '_ROLE_NAME_RE',
    '_WS_MARKER_START', '_WS_MARKER_END',
    '_FORBIDDEN_MAP', '_FORBIDDEN_DISPLAY', '_WAKE_PERMISSION_MAP', '_CLAUDE_MD',
]
