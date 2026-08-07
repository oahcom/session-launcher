"""routing/policy.py — 跨角色路由策略（从 core.py 提取）。"""

__all__ = ['set_routing_policy', 'get_routing_policy', 'route_target']

import time

# ── 路由策略状态（MCP Gateway 模式）──
# sticky: 同角色消息路由到同一 CCS session
# round-robin: 轮询分发
# priority: 按消息优先级路由
_ROUTING_POLICIES: dict[str, str] = {}  # role -> policy


def set_routing_policy(role: str, policy: str = "sticky") -> None:
    if policy not in ("sticky", "round-robin", "priority"):
        policy = "sticky"
    _ROUTING_POLICIES[role] = policy


def get_routing_policy(role: str) -> str:
    return _ROUTING_POLICIES.get(role, "sticky")


def route_target(role: str, candidates: list[str]) -> str:
    policy = get_routing_policy(role)
    if policy == "sticky":
        return candidates[0] if candidates else role
    elif policy == "round-robin":
        idx = hash(role + str(int(time.time() / 60))) % max(len(candidates), 1)
        return candidates[idx] if candidates else role
    return candidates[0] if candidates else role