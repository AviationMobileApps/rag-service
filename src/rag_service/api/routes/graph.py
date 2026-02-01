from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from rag_service.api.deps import RequestContext, get_request_context
from rag_service.retrieval.graph_search import GraphSearch


router = APIRouter(prefix="/v1/graph", tags=["graph"])


@router.get("/entities")
def list_entities(
    ctx: RequestContext = Depends(get_request_context),
    q: Optional[str] = Query(default=None, description="Case-insensitive substring match on entity name"),
    entity_type: Optional[str] = Query(default=None, description="Exact match on entity type"),
    limit: int = Query(default=50, ge=1, le=500),
):
    gs = GraphSearch()
    rows = gs.list_entities(ctx=ctx, q=q, entity_type=entity_type, limit=limit)
    return {"count": len(rows), "entities": rows}


@router.get("/entities/{entity_id}/chunks")
def entity_chunks(
    entity_id: str,
    ctx: RequestContext = Depends(get_request_context),
    limit: int = Query(default=25, ge=1, le=200),
):
    gs = GraphSearch()
    rows = gs.entity_chunks(entity_id=entity_id, ctx=ctx, limit=limit)
    return {"entity_id": entity_id, "count": len(rows), "chunks": rows}


@router.get("/documents/{doc_id}/entities")
def document_entities(
    doc_id: str,
    ctx: RequestContext = Depends(get_request_context),
    limit: int = Query(default=50, ge=1, le=500),
):
    gs = GraphSearch()
    rows = gs.document_entities(doc_id=doc_id, ctx=ctx, limit=limit)
    return {"doc_id": doc_id, "count": len(rows), "entities": rows}

