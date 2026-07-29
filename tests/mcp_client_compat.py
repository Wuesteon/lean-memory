"""Dual-path in-memory MCP client for tests (WP12).

mcp 2.0 replaced `mcp.shared.memory.create_connected_server_and_client_session`
with `mcp.client.Client(server)`. Both yield an object whose
`.call_tool(name, args)` returns a CallToolResult with `.content[0].text`,
so call sites are identical across majors — only session construction
differs. Same v2 marker import as `lean_memory._mcp_compat`."""

from contextlib import asynccontextmanager

try:  # mcp >= 2
    from mcp.client import Client

    @asynccontextmanager
    async def client_session(mcp_obj):
        async with Client(mcp_obj) as client:
            yield client

except ImportError:  # mcp 1.x
    from mcp.shared.memory import create_connected_server_and_client_session

    @asynccontextmanager
    async def client_session(mcp_obj):
        async with create_connected_server_and_client_session(
            mcp_obj._mcp_server
        ) as session:
            yield session
