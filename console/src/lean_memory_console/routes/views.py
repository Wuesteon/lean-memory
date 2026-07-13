"""/views/* — the human read-path router."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..config import is_reserved_namespace, ns_db_path
from .. import inspect_sql


class TestSearchBody(BaseModel):
    query: str
    k: int = Field(default=5, ge=1, le=200)


def _ns_db(request: Request, namespace: str):
    if is_reserved_namespace(namespace):
        raise HTTPException(status_code=404, detail="unknown namespace")
    config = request.app.state.config
    path = ns_db_path(config.data_root, namespace)
    if not path.exists():
        raise HTTPException(status_code=404, detail="unknown namespace")
    return path


def build_views_router() -> APIRouter:
    # Imported here (not at module top) to break the app<->views import cycle:
    # app.py imports build_views_router as its last import, so require_auth is
    # already defined by the time this function runs (create_app calls it).
    from ..app import require_auth

    router = APIRouter(prefix="/views")

    @router.get("/whoami")
    def whoami(request: Request):
        config = request.app.state.config
        from ..app import _is_authenticated

        auth = "bearer" if config.mode == "docker" else "token"
        return {
            "mode": config.mode,
            "auth": auth,
            "authenticated": _is_authenticated(request, config),
            "data_root": str(config.data_root),
        }

    @router.get("/namespaces", dependencies=[Depends(require_auth)])
    def namespaces(request: Request):
        config = request.app.state.config
        event_log = request.app.state.event_log
        return inspect_sql.list_namespaces(config.data_root, event_log)

    @router.get("/{namespace}/facts", dependencies=[Depends(require_auth)])
    def facts(
        request: Request,
        namespace: str,
        latest_only: bool = True,
        predicate: str | None = None,
        entity: str | None = None,
        min_salience: float | None = None,
        q: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ):
        path = _ns_db(request, namespace)
        return inspect_sql.list_facts(
            path,
            latest_only=latest_only,
            predicate=predicate,
            entity=entity,
            min_salience=min_salience,
            q=q,
            page=page,
            page_size=page_size,
        )

    @router.get(
        "/{namespace}/facts/{fact_id}", dependencies=[Depends(require_auth)]
    )
    def fact_detail(request: Request, namespace: str, fact_id: str):
        path = _ns_db(request, namespace)
        fact = inspect_sql.get_fact(path, fact_id)
        if fact is None:
            raise HTTPException(status_code=404, detail="unknown fact")
        return fact

    @router.get("/{namespace}/episodes", dependencies=[Depends(require_auth)])
    def episodes(
        request: Request, namespace: str, page: int = 1, page_size: int = 50
    ):
        path = _ns_db(request, namespace)
        return inspect_sql.list_episodes(path, page=page, page_size=page_size)

    @router.get(
        "/{namespace}/episodes/{episode_id}",
        dependencies=[Depends(require_auth)],
    )
    def episode_detail(request: Request, namespace: str, episode_id: str):
        path = _ns_db(request, namespace)
        ep = inspect_sql.get_episode(path, episode_id)
        if ep is None:
            raise HTTPException(status_code=404, detail="unknown episode")
        return ep

    @router.get("/{namespace}/entities", dependencies=[Depends(require_auth)])
    def entities(
        request: Request, namespace: str, page: int = 1, page_size: int = 50
    ):
        path = _ns_db(request, namespace)
        return inspect_sql.list_entities(path, page=page, page_size=page_size)

    @router.get("/{namespace}/events", dependencies=[Depends(require_auth)])
    def events(
        request: Request,
        namespace: str,
        kind: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ):
        if kind is not None and kind not in ("add", "search"):
            raise HTTPException(status_code=422, detail="invalid kind")
        # events live in the sidecar, not a namespace .db — no _ns_db guard,
        # but reserved namespaces are still rejected.
        if is_reserved_namespace(namespace):
            raise HTTPException(status_code=404, detail="unknown namespace")
        event_log = request.app.state.event_log
        return event_log.list_events(
            namespace, kind=kind, page=page, page_size=page_size
        )

    @router.post(
        "/{namespace}/test-search", dependencies=[Depends(require_auth)]
    )
    async def test_search(
        request: Request, namespace: str, body: TestSearchBody
    ):
        if is_reserved_namespace(namespace):
            raise HTTPException(status_code=404, detail="unknown namespace")
        gateway = request.app.state.gateway
        result = await gateway.search(
            namespace, body.query, k=body.k, latest_only=True, origin="ui"
        )
        return {"hits": result.hits, "duration_ms": result.duration_ms}

    return router
