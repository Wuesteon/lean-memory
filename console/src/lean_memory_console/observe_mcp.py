"""Observing MCP wrapper — stdio server that writes through EngineGateway.

A deliberate superset of the core stdio server: memory_add gains source/t_ref
and a structured return; memory_clear is intentionally absent (no deletion
surface, §6). Parity is with the Memory API, not the core tool signatures.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import ConsoleConfig
from .engine import EngineGateway
from .events import EventLog


def build_mcp(gateway: EngineGateway) -> FastMCP:
    mcp = FastMCP("lean-memory-console")

    @mcp.tool()
    async def memory_add(
        namespace: str,
        text: str,
        source: str = "user",
        t_ref: int | None = None,
    ) -> dict[str, Any]:
        """Ingest text into the namespace's memory (observing wrapper).

        Returns the new fact ids and how many prior facts were superseded.
        """
        res = await gateway.add(namespace, text, source=source, t_ref=t_ref)
        return {
            "fact_ids": res.fact_ids,
            "superseded_count": res.superseded_count,
        }

    @mcp.tool()
    async def memory_search(namespace: str, query: str, k: int = 5) -> dict[str, Any]:
        """Search a namespace's memory; returns top-k fact texts + scores.

        Always latest-only (the latest_only flag is REST-only, §6).
        """
        res = await gateway.search(
            namespace, query, k=k, latest_only=True, origin="agent"
        )
        return {
            "hits": [
                {"fact_text": h["fact_text"], "final_score": h["final_score"]}
                for h in res.hits
            ]
        }

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
