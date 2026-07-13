"""Docker-mode streamable-HTTP MCP mount (/mcp) with bearer auth.

The wrapper FastMCP is built stateless (json_response) so its Starlette app
mounts into FastAPI without needing to own the server process. A thin ASGI
wrapper enforces ``Authorization: Bearer <api_key>`` before delegating; full
MCP-over-HTTP round-trips are exercised in the manual E2E (and, since the review,
in a two-request automated test).

Two SDK-1.28.1 decisions, both disclosed:

1. Session-manager lifecycle — LIFESPAN ONLY.
   ``streamable_http_app().handle_request`` raises
   ``RuntimeError("Task group is not initialized. Make sure to use run().")``
   until ``StreamableHTTPSessionManager.run()`` has been entered — even in
   stateless mode. ``run()`` may be entered EXACTLY ONCE per instance: it sets
   ``_has_started`` permanently and raises on a second call, and on exit it
   resets ``_task_group`` to ``None`` (verified in the installed SDK at
   ``mcp/server/streamable_http_manager.py``). A per-request self-start is
   therefore broken beyond the first request. We expose ``session_manager`` and
   the console's app lifespan (``app.create_app``) enters ``run()`` once for the
   process lifetime. Consequence: tests that hit ``/mcp`` MUST drive the lifespan
   — use ``with TestClient(app) as client:`` (a bare ``TestClient(app)`` does not
   run the lifespan and would 500 on the first authenticated request).

2. Transport security — DNS-rebinding protection ENABLED with a loopback
   allow-list.
   FastMCP validates Host and Origin headers to defeat DNS rebinding. The SDK
   couples both checks behind a single ``enable_dns_rebinding_protection`` flag
   and offers no host wildcard — only exact matches and ``base:*`` port patterns
   (see ``mcp/server/transport_security.py``). We enable protection and
   enumerate the loopback host/origin values a legitimate console client
   presents. This gives real Origin restriction (a cross-site Origin → 403) while
   the bearer gate in front remains the primary access control.

   Docker deployments reached via an ARBITRARY published hostname will have that
   hostname in the Host header, which the SDK cannot allow-list by wildcard, so
   such requests are rejected with 421. Front the container with a reverse proxy
   that presents a known/loopback Host (or extend ``allowed_hosts`` for the
   deployment's fixed hostname). We keep the narrow loopback allow-list rather
   than disabling protection outright, because disabling the flag also disables
   the Origin check — losing the one control that actually matters for a
   browser-originated DNS-rebinding attack.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from ..config import ConsoleConfig
from ..engine import EngineGateway

# Loopback host/origin allow-list for the inner MCP transport-security check.
# Host entries use the SDK's exact + ``base:*`` port-wildcard matching; there is
# no host wildcard, so arbitrary external Docker hostnames are intentionally not
# covered (see the module docstring, decision 2).
_ALLOWED_HOSTS = [
    "127.0.0.1",
    "127.0.0.1:*",
    "localhost",
    "localhost:*",
    "[::1]",
    "[::1]:*",
]
_ALLOWED_ORIGINS = [
    "http://127.0.0.1",
    "http://127.0.0.1:*",
    "http://localhost",
    "http://localhost:*",
]


def _build_http_mcp(gateway: EngineGateway) -> FastMCP:
    mcp = FastMCP(
        "lean-memory-console",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=_ALLOWED_HOSTS,
            allowed_origins=_ALLOWED_ORIGINS,
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
    (FastMCP's ``streamable_http_path`` is "/" so it resolves at exactly the
    mount point). The returned app exposes ``session_manager``; the caller MUST
    enter its ``run()`` in the app lifespan (see module docstring, decision 1) —
    there is no per-request fallback, because ``run()`` is once-only per instance.
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
        await inner(scope, receive, send)

    # Exposed so create_app can drive the once-only session manager via the app
    # lifespan (the only supported start path).
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
