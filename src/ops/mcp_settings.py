"""MCP server 注册表 + 角色级 settings.json 生成。

从 core.py 提取的独立模块，减少 core.py 体积。
"""
import json
import logging
from pathlib import Path

_log = logging.getLogger("core.mcp_settings")

# ── 已知 MCP server 注册表（name → {command, args, env}）──
# ponytail: 从全局 settings.json 和所有插件 .mcp.json 加载
_MCP_SERVER_REGISTRY: dict[str, dict] = {}


def _load_mcp_registry() -> dict[str, dict]:
    """扫描全局 settings.json + 所有插件 .mcp.json，构建 MCP server 注册表。"""
    registry: dict[str, dict] = {}
    # 1. 全局 settings.json 的 mcpServers
    gs = Path.home() / ".claude" / "settings.json"
    if gs.exists():
        try:
            import json as _json
            gcfg = _json.loads(gs.read_text(encoding="utf-8"))
            registry.update(gcfg.get("mcpServers", {}))
        except Exception as e:
            _log.warning("全局 settings.json MCP 加载失败: %s", e)
    # 2. 扫描所有插件目录下的 .mcp.json（marketplace 注册 + 缓存）
    for base in [Path.home() / ".claude/plugins/marketplaces",
                 Path.home() / ".claude/plugins/cache"]:
        if not base.is_dir():
            continue
        for fpath in sorted(base.rglob(".mcp.json")):
            try:
                data = json.loads(fpath.read_text(encoding="utf-8"))
                # 两种格式：{"mcpServers": {...}} 或直接 {"server_name": {...}}
                entries = data.get("mcpServers", data)
                if not isinstance(entries, dict):
                    continue
                for name, cfg in entries.items():
                    if not isinstance(cfg, dict):
                        continue
                    s = json.dumps(cfg)
                    if "${CLAUDE_PLUGIN_ROOT}" in s:
                        s = s.replace("${CLAUDE_PLUGIN_ROOT}",
                                      str(fpath.parent.resolve()))
                        cfg = json.loads(s)
                    if name not in registry:
                        registry[name] = cfg
            except Exception:
                continue
    return registry


_MCP_SERVER_REGISTRY = _load_mcp_registry()

def _write_mcp_settings(ws_path: Path, role_def: dict | None) -> None:
    """写 workspace 专属 .claude/settings.json，MCP + 权限按角色隔离。

    继承全局 settings.json 的 mcpServers/hooks/model 等配置，
    覆写 mcpServers（按 persona 声明）和 permissions（按角色最小权限）。
    角色无 permissions 字段时写空 allow list → 所有操作需人工确认。
    """
    claude_dir = ws_path / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path = claude_dir / "settings.json"
    gs = Path.home() / ".claude" / "settings.json"
    cfg = json.loads(gs.read_text(encoding="utf-8")) if gs.exists() else {}
    mcp_names = (role_def or {}).get("mcp_servers", [])
    # 继承全局 mcpServers（如 hex-line 等基础设施），再按角色增补
    base_mcp = cfg.get("mcpServers", {})
    role_mcp = {n: _MCP_SERVER_REGISTRY[n] for n in mcp_names if n in _MCP_SERVER_REGISTRY}
    base_mcp.update(role_mcp)
    cfg["mcpServers"] = base_mcp
    # 权限：角色级 allowlist 替换全局 bypass
    # ponytail: mcp_tools 中的工具级限制尚未映射到 permissions.allow，
    # 因为 Claude Code 无"per-server MCP tool"粒度的权限条目。
    # 需要时添加到对应的 MCP server 层（server 自身的 auth）或 hook 层。
    role_perms = (role_def or {}).get("permissions", {})
    if role_perms:
        cfg["permissions"] = role_perms
    else:
        # 严格默认：无一预授权 → 所有操作弹出确认
        cfg["permissions"] = {"allow": []}
    settings_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    _log.info("写入 %s mcp_servers=%s perms_rules=%d",
              settings_path, list(cfg["mcpServers"].keys()),
              len(cfg["permissions"].get("allow", [])))
