"""FastAPI factory for the console (both modes).

Local mode: 127.0.0.1 bind, per-launch session token (query param or
X-Console-Token header). Docker mode: Authorization: Bearer <api_key>.
Referrer-Policy: no-referrer on everything; local mode also validates the
Host header (DNS-rebinding belt-and-suspenders).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import ConsoleConfig
from .engine import EngineGateway
from .events import EventLog
from .routes.views import build_views_router

# Loopback hostnames accepted in local mode (DNS-rebinding guard). "testserver"
# is Starlette's synthetic in-process test host: it never reaches a real network
# socket, so it carries no rebinding risk and must be allowed for the harness to
# drive the local app.
_ALLOWED_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "testserver"})


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
    app = FastAPI(title="lean-memory-console")
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

    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="spa")

    return app
