"""/views/*/review + /views/*/maintenance — the maintenance review-path router.

Every route proxies a single ``EngineGateway`` maintenance method (WP10a): the
engine is never opened directly here. Existence + reserved-namespace guarding is
identical to the read-path router (``_ns_db``), so a review call against a
nonexistent namespace 404s rather than creating the ``.db`` as a side effect.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..config import is_reserved_namespace, ns_db_path


class DecideBody(BaseModel):
    decision: str
    edited_text: str | None = None


class PromoteBody(BaseModel):
    fact_id: str


class RunBody(BaseModel):
    apply: bool = False


# The lifecycle CAS reports a re-decided proposal as an outcome, not an
# exception (lifecycle.py:_already_decided) — surface those as 409 so the UI can
# refresh the row, with the informative body passed through verbatim.
_ALREADY_DECIDED = frozenset({"already_decided", "already_applied"})


def _ns_db(request: Request, namespace: str):
    if is_reserved_namespace(namespace):
        raise HTTPException(status_code=404, detail="unknown namespace")
    config = request.app.state.config
    path = ns_db_path(config.data_root, namespace)
    if not path.exists():
        raise HTTPException(status_code=404, detail="unknown namespace")
    return path


def build_review_router() -> APIRouter:
    # Imported here (not at module top) to break the app<->routes import cycle,
    # exactly as build_views_router does: app.py imports this builder last, so
    # require_auth is already defined by the time create_app calls it.
    from ..app import require_auth

    router = APIRouter(prefix="/views")

    @router.get(
        "/{namespace}/review/queue", dependencies=[Depends(require_auth)]
    )
    async def review_queue(
        request: Request,
        namespace: str,
        kind: str | None = None,
        limit: int = 20,
    ):
        _ns_db(request, namespace)
        gateway = request.app.state.gateway
        return await gateway.review_queue(namespace, kind=kind, limit=limit)

    @router.post(
        "/{namespace}/review/{proposal_id}/decide",
        dependencies=[Depends(require_auth)],
    )
    async def decide(
        request: Request,
        namespace: str,
        proposal_id: str,
        body: DecideBody,
    ):
        _ns_db(request, namespace)
        gateway = request.app.state.gateway
        result = await gateway.decide(
            namespace, proposal_id, body.decision,
            edited_text=body.edited_text,
        )
        if result.get("outcome") in _ALREADY_DECIDED:
            return JSONResponse(result, status_code=409)
        return result

    @router.post(
        "/{namespace}/review/promote", dependencies=[Depends(require_auth)]
    )
    async def promote(request: Request, namespace: str, body: PromoteBody):
        _ns_db(request, namespace)
        gateway = request.app.state.gateway
        return await gateway.promote(namespace, body.fact_id)

    @router.get(
        "/{namespace}/maintenance/status",
        dependencies=[Depends(require_auth)],
    )
    async def maintenance_status(request: Request, namespace: str):
        _ns_db(request, namespace)
        gateway = request.app.state.gateway
        return await gateway.maintenance_status(namespace)

    @router.post(
        "/{namespace}/maintenance/run", dependencies=[Depends(require_auth)]
    )
    async def maintenance_run(
        request: Request, namespace: str, body: RunBody
    ):
        _ns_db(request, namespace)
        gateway = request.app.state.gateway
        return await gateway.maintain(namespace, apply=body.apply)

    return router
