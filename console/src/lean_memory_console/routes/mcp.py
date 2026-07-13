"""Docker-mode streamable-HTTP MCP mount (/mcp) with bearer auth.

The wrapper FastMCP is built stateless (json_response) so its Starlette app
mounts into FastAPI without a long-lived session-manager lifespan of its own. A
thin ASGI wrapper enforces ``Authorization: Bearer <api_key>`` before delegating;
full MCP-over-HTTP round-trips are exercised in the manual E2E, not here.

Two SDK-1.28.1 adaptations from the brief's skeleton, both minimal and disclosed:

1. Session-manager task group. ``streamable_http_app().handle_request`` raises
   ``RuntimeError("Task group is not initialized. Make sure to use run().")``
   unless ``StreamableHTTPSessionManager.run()`` has been entered — even in
   stateless mode. When this ASGI app is *mounted* (``app.mount``) rather than
   included, FastAPI does not forward lifespan events to it, so the inner app's
   own lifespan never fires. We therefore start the manager per request, inside
   the same task that serves it, when its task group is not already running (an
   anyio task group must be entered and exited in the same task, so a persistent
   cross-task start is not viable under a bare/portal ASGI driver). Under a real
   ASGI server the console's lifespan (``create_app``) enters ``run()`` once and
   the per-request guard becomes a no-op.

2. Transport security. FastMCP defaults to DNS-rebinding protection that rejects
   any Host header not on its allow-list (127.0.0.1/localhost) with 421. The
   console applies its own Host guard in local mode and Docker mode runs behind
   the deployment's own boundary, so we disable the inner check to keep the mount
   reachable through the mount point's rewritten Host.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from ..config import ConsoleConfig
from ..engine import EngineGateway


def _build_http_mcp(gateway: EngineGateway) -> FastMCP:
    mcp = FastMCP(
        "lean-memory-console",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )

    @mcp.tool()
    async def memory_add(
        namespace: str,
        text: str,
        source: str = "user",
        t_ref: int | None = None,
    ) -> dict[str, Any]:
        """Ingest text into the namespace's memory (HTTP wrapper)."""
        res = await gateway.add(namespace, text, source=source, t_ref=t_ref)
        return {
            "fact_ids": res.fact_ids,
            "superseded_count": res.superseded_count,
        }

    @mcp.tool()
    async def memory_search(
        namespace: str, query: str, k: int = 5
    ) -> dict[str, Any]:
        """Search a namespace's memory (HTTP wrapper); always latest-only."""
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


def build_mcp_mount(gateway: EngineGateway, config: ConsoleConfig):
    """Return an ASGI app: bearer gate -> streamable-HTTP MCP app.

    Mount at "/mcp"; the inner MCP app serves at its own root "/" once mounted
    (FastMCP's streamable_http_path is set to "/" so it resolves at exactly the
    mount point). The returned app exposes ``session_manager`` so the caller can
    enter its ``run()`` in the app lifespan; absent that, each request starts the
    manager for its own duration.
    """
    mcp = _build_http_mcp(gateway)
    inner = mcp.streamable_http_app()
    session_manager = mcp.session_manager  # initializes it on the inner app
    expected = f"Bearer {config.api_key}"

    async def gated(scope, receive, send):
        if scope["type"] != "http":
            await inner(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode()
        if not config.api_key or auth != expected:
            await _send_401(send)
            return
        # Start the stateless session manager for this request if the app
        # lifespan has not already started it (mounted apps get no lifespan).
        if session_manager._task_group is None:
            async with session_manager.run():
                await inner(scope, receive, send)
        else:
            await inner(scope, receive, send)

    # Expose the manager so create_app can drive it via the app lifespan under a
    # real ASGI server (uvicorn); the per-request start above covers drivers
    # that do not forward lifespan to mounted sub-apps (e.g. bare TestClient).
    gated.session_manager = session_manager
    return gated


async def _send_401(send):
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"referrer-policy", b"no-referrer"),
            ],
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": b'{"detail":"unauthorized"}',
        }
    )
