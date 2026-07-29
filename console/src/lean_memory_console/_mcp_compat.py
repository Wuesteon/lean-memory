"""Dual-path mcp SDK compatibility (WP12): 2.0 renamed FastMCP → MCPServer
(same decorator API) and moved transport params from the constructor to the
`streamable_http_app(...)` call. Deliberately duplicated from
`lean_memory._mcp_compat` rather than imported: console↔core version skew
makes cross-package private imports fragile."""

from __future__ import annotations

from typing import Any

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as MCPServerType

    MCP_V2 = True
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServerType  # type: ignore[assignment]

    MCP_V2 = False


def make_http_server_and_app(name: str, **transport_kwargs: Any):
    """Build a streamable-HTTP server + its ASGI app under either SDK major.

    1.x takes the transport kwargs (stateless_http, json_response,
    streamable_http_path, transport_security) in the FastMCP constructor and a
    bare `streamable_http_app()`; 2.0 takes a bare constructor and passes them
    to `streamable_http_app(...)`. Returns (server, build_app) where build_app
    is a zero-arg callable — tools must be registered on `server` BEFORE
    calling it (both majors snapshot the tool set into the app).
    """
    if MCP_V2:
        server = MCPServerType(name)
        return server, lambda: server.streamable_http_app(**transport_kwargs)
    server = MCPServerType(name, **transport_kwargs)
    return server, lambda: server.streamable_http_app()
