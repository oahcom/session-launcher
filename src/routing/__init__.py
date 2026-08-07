"""routing 子包 —— 跨角色路由。"""

from routing.gatekeeper import CrossRoleRouter
from routing.roles import load_roles, get_role, check_wake_permission, inject_role_knowledge_into_workspace, _validate_role_name
from routing.partner import PartnerClient, is_ccs_running, cli_resolve, cli_wake, cli_confirm, cli_send, main
from routing.policy import set_routing_policy, get_routing_policy, route_target

__all__ = [
    "CrossRoleRouter",
    "load_roles",
    "get_role",
    "check_wake_permission",
    "inject_role_knowledge_into_workspace",
    "_validate_role_name",
    "PartnerClient",
    "is_ccs_running",
    "cli_resolve",
    "cli_wake",
    "cli_confirm",
    "cli_send",
    "main",
    "set_routing_policy",
    "get_routing_policy",
    "route_target",
]
