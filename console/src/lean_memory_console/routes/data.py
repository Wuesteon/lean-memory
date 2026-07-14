"""/v1/* — the REST data plane mirror (Docker mode, non-MCP agents)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..config import is_reserved_namespace


class MemoryBody(BaseModel):
    text: str
    source: str = "user"
    t_ref: int | None = None


class SearchBody(BaseModel):
    query: str
    k: int = Field(default=5, ge=1, le=200)
    latest_only: bool = True


def build_data_router() -> APIRouter:
    router = APIRouter(prefix="/v1")

    @router.post("/{namespace}/memories")
    async def add_memory(request: Request, namespace: str, body: MemoryBody):
        if is_reserved_namespace(namespace):
            raise HTTPException(status_code=404, detail="unknown namespace")
        gateway = request.app.state.gateway
        res = await gateway.add(
            namespace, body.text, source=body.source, t_ref=body.t_ref
        )
        return {
            "fact_ids": res.fact_ids,
            "superseded_count": res.superseded_count,
        }

    @router.post("/{namespace}/search")
    async def search_memory(request: Request, namespace: str, body: SearchBody):
        if is_reserved_namespace(namespace):
            raise HTTPException(status_code=404, detail="unknown namespace")
        gateway = request.app.state.gateway
        res = await gateway.search(
            namespace,
            body.query,
            k=body.k,
            latest_only=body.latest_only,
            origin="agent",
        )
        return {"hits": res.hits, "duration_ms": res.duration_ms}

    return router
