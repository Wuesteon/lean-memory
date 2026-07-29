"""Dual-path mcp SDK compatibility (WP12): the 2.0 SDK renamed FastMCP →
MCPServer and moved the ctor `version=` in; 1.x needs the private
`_mcp_server.version` poke. Marker import is the v2-only module — cheap and
unambiguous. Duplicated in lean_memory_console._mcp_compat on purpose:
console↔core version skew makes cross-package private imports fragile."""

from __future__ import annotations

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as MCPServerType

    MCP_V2 = True
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServerType  # type: ignore[assignment]

    MCP_V2 = False


def make_stdio_server(name: str, *, version: str) -> MCPServerType:
    if MCP_V2:
        return MCPServerType(name, version=version)
    server = MCPServerType(name)
    # 1.x FastMCP doesn't take a version; unset, the SDK reports ITS OWN
    # version in the initialize handshake.
    server._mcp_server.version = version
    return server
