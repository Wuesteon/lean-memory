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
   default allow-list, extendable via ``LM_MCP_ALLOWED_HOSTS``.
   FastMCP validates Host and Origin headers to defeat DNS rebinding. The SDK
   couples both checks behind a single ``enable_dns_rebinding_protection`` flag
   and offers no host wildcard — only exact matches and ``base:*`` port patterns
   (see ``mcp/server/transport_security.py``). We enable protection and enumerate
   the loopback host/origin values a legitimate local client presents. This gives
   real Origin restriction (a cross-site Origin → 403) while the bearer gate in
   front remains the primary access control.

   The shipped ``deploy/docker-compose.yml`` publishes 8377 directly (no reverse
   proxy in the stack), so a container reached over the LAN presents a Host like
   ``192.168.1.10:8377`` or ``myserver:8377`` that the loopback default does not
   cover — it would 421. Remote-host deployments therefore set
   ``LM_MCP_ALLOWED_HOSTS`` (comma-separated host patterns, e.g.
   ``192.168.1.10:*,myserver:*``, parsed in ``config.py``); those patterns are
   ADDED to the loopback defaults here. Origins stay restricted to loopback. We
   extend rather than disable the flag, because disabling it also disables the
   Origin check — losing the one control that matters for a browser-originated
   DNS-rebinding attack. The bearer gate remains the primary control regardless.
"""

from __future__ import annotations

import secrets
from typing import Any

from mcp.server.transport_security import TransportSecuritySettings

from .._mcp_compat import MCPServerType, make_http_server_and_app
from ..config import ConsoleConfig
from ..engine import EngineGateway
from ..mcp_tools import register_maintenance_tools, register_memory_tools

# Loopback host/origin default allow-list for the inner MCP transport-security
# check. Host entries use the SDK's exact + ``base:*`` port-wildcard matching;
# there is no host wildcard, so remote deployments extend this list via
# LM_MCP_ALLOWED_HOSTS (see the module docstring, decision 2).
_DEFAULT_ALLOWED_HOSTS = [
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


def _build_http_mcp(
    gateway: EngineGateway, extra_allowed_hosts: list[str] | None = None
) -> tuple[MCPServerType, Any]:
    # Loopback defaults + any operator-supplied patterns (LM_MCP_ALLOWED_HOSTS),
    # de-duplicated while preserving order.
    allowed_hosts = list(_DEFAULT_ALLOWED_HOSTS)
    for host in extra_allowed_hosts or []:
        if host not in allowed_hosts:
            allowed_hosts.append(host)
    # The compat factory places the transport kwargs where the installed SDK
    # major wants them (1.x: constructor; 2.x: streamable_http_app call).
    mcp, build_app = make_http_server_and_app(
        "lean-memory-console",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=_ALLOWED_ORIGINS,
        ),
    )

    # The same two memory tools and the same four maintenance tools as the stdio
    # surface (§6.3) — registered from the shared module so names, signatures,
    # return shapes, descriptions and annotations are identical by construction
    # (pinned by tests/test_mcp_tool_metadata.py). No review prompt here — MCP
    # prompts are a stdio-client capability; HTTP clients use the tools.
    register_memory_tools(mcp, gateway)
    register_maintenance_tools(mcp, gateway)

    return mcp, build_app


def build_mcp_mount(gateway: EngineGateway, config: ConsoleConfig):
    """Return an ASGI app: bearer gate -> streamable-HTTP MCP app.

    Mount at "/mcp"; the inner MCP app serves at its own root "/" once mounted
    (FastMCP's ``streamable_http_path`` is "/" so it resolves at exactly the
    mount point). The returned app exposes ``session_manager``; the caller MUST
    enter its ``run()`` in the app lifespan (see module docstring, decision 1) —
    there is no per-request fallback, because ``run()`` is once-only per instance.
    """
    mcp, build_app = _build_http_mcp(gateway, config.mcp_allowed_hosts)
    inner = build_app()
    session_manager = mcp.session_manager  # initializes it on the inner app
    # None when no api_key is configured, so the constant-time compare below
    # always fails (compare_digest is skipped for a None expected value).
    expected = f"Bearer {config.api_key}" if config.api_key else None

    async def gated(scope, receive, send):
        if scope["type"] != "http":
            await inner(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode()
        # Constant-time bearer compare; both sides must be non-None strings.
        if expected is None or not secrets.compare_digest(auth, expected):
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
