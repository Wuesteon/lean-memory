"""Observing MCP wrapper — stdio server that writes through EngineGateway.

A deliberate superset of the core stdio server: memory_add gains source/t_ref
and a structured return; memory_clear is intentionally absent (no deletion
surface, §6). Parity is with the Memory API, not the core tool signatures.

Every tool — the two memory tools included — is defined ONCE in ``mcp_tools``
and registered from there, so this stdio surface and the Docker HTTP mount
(``routes/mcp.py``) cannot drift in signatures, return shapes, descriptions or
annotations (§6.3; metadata pinned by tests/test_mcp_tool_metadata.py).
"""

from __future__ import annotations

from ._mcp_compat import MCPServerType
from .config import ConsoleConfig
from .engine import EngineGateway
from .events import EventLog
from .mcp_tools import (
    register_maintenance_tools,
    register_memory_tools,
    register_review_prompt,
)


def build_mcp(gateway: EngineGateway) -> MCPServerType:
    mcp = MCPServerType("lean-memory-console")

    # memory_add + memory_search, then the four sleep-time maintenance tools —
    # all identical to the HTTP surface (§6.3) — plus the stdio-only
    # review-workflow prompt (§6.4).
    register_memory_tools(mcp, gateway)
    register_maintenance_tools(mcp, gateway)
    register_review_prompt(mcp)

    return mcp


def run_stdio(config: ConsoleConfig) -> None:
    """Build the gateway + wrapper for `config` and serve over stdio."""
    event_log = EventLog(config.data_root)
    gateway = EngineGateway(config, event_log)
    mcp = build_mcp(gateway)
    try:
        mcp.run()  # blocks on stdio until the client disconnects
    finally:
        gateway.close()
        event_log.close()
