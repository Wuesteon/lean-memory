"""FastAPI factory for the console (both modes).

Local mode: 127.0.0.1 bind, per-launch session token (query param or
X-Console-Token header). Docker mode: Authorization: Bearer <api_key>.
Referrer-Policy: no-referrer on everything; local mode also validates the
Host header (DNS-rebinding belt-and-suspenders).
"""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import ConsoleConfig
from .engine import EngineGateway
from .events import EventLog
from .routes.data import build_data_router
from .routes.mcp import build_mcp_mount
from .routes.views import build_views_router

# Loopback hostnames accepted in local mode (DNS-rebinding guard). Exact-match
# only: a startswith check would wrongly admit e.g. "localhost.attacker.com".
_ALLOWED_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})


def _static_dir() -> Path:
    """Directory of the built SPA (populated by the UI build). A seam so the
    static-mount-ordering regression can be tested hermetically regardless of
    whether the UI build has materialized ``static/`` on disk."""
    return Path(__file__).parent / "static"


def _is_authenticated(request: Request, config: ConsoleConfig) -> bool:
    if config.mode == "docker":
        header = request.headers.get("Authorization", "")
        expected = f"Bearer {config.api_key}"
        return bool(config.api_key) and header == expected
    # local mode: query token OR X-Console-Token header
    token = request.query_params.get("token") or request.headers.get(
        "X-Console-Token"
    )
    return bool(config.session_token) and token == config.session_token


def require_auth(request: Request) -> None:
    """FastAPI dependency: 401 unless valid credential is presented."""
    config: ConsoleConfig = request.app.state.config
    if not _is_authenticated(request, config):
        raise HTTPException(status_code=401, detail="unauthorized")


def create_app(
    config: ConsoleConfig,
    gateway: EngineGateway,
    event_log: EventLog,
) -> FastAPI:
    # In Docker mode, build the streamable-HTTP MCP mount up front so its
    # once-only session manager can be started by the app lifespan below — the
    # only supported start path (run() cannot be re-entered per request). Under a
    # real ASGI server (uvicorn) the lifespan runs; tests hitting /mcp must use a
    # context-managed TestClient so the lifespan runs there too.
    mcp_mount = (
        build_mcp_mount(gateway, config) if config.mode == "docker" else None
    )

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        async with AsyncExitStack() as stack:
            if mcp_mount is not None:
                await stack.enter_async_context(mcp_mount.session_manager.run())
            yield

    app = FastAPI(title="lean-memory-console", lifespan=_lifespan)
    app.state.config = config
    app.state.gateway = gateway
    app.state.event_log = event_log

    @app.middleware("http")
    async def _security(request: Request, call_next):
        # Local-mode Host guard (DNS-rebinding belt-and-suspenders).
        if config.mode == "local":
            host = request.headers.get("host", "")
            hostname = host.split(":")[0]
            if hostname not in _ALLOWED_LOCAL_HOSTS:
                resp = JSONResponse(
                    {"detail": "forbidden host"}, status_code=403
                )
                resp.headers["Referrer-Policy"] = "no-referrer"
                return resp
        response = await call_next(request)
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    app.include_router(build_views_router())
    # /v1/* are POST handlers reading app.state.gateway; gate the whole router
    # with the same auth dependency (local token / docker bearer).
    app.include_router(
        build_data_router(), dependencies=[Depends(require_auth)]
    )

    if mcp_mount is not None:
        # A Starlette Mount("/mcp") only matches "/mcp/..." — a bare "/mcp"
        # (what real MCP clients POST to, per the connect snippet) is NOT matched
        # by the mount. Without a static "/" mount, Starlette's slash-redirect
        # fallback used to bounce "/mcp" -> "/mcp/". But the greedy StaticFiles
        # mount at "/" matches "/mcp" first and 405s every non-GET method before
        # that fallback can run. Register an explicit 307 (method+body preserving)
        # from "/mcp" to "/mcp/" so a bare POST reaches the bearer gate whether or
        # not the SPA is present.
        @app.post("/mcp", include_in_schema=False)
        async def _mcp_slash_redirect() -> RedirectResponse:
            return RedirectResponse(url="/mcp/", status_code=307)

        app.mount("/mcp", mcp_mount)

    # The SPA catch-all MUST be registered LAST so its greedy "/" mount never
    # shadows /v1, /views, or /mcp (StaticFiles serves only GET/HEAD and 405s
    # every other method on any path it matches).
    static_dir = _static_dir()
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="spa")

    return app
