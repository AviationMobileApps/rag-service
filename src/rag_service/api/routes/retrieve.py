from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
import weaviate.classes as wvc
from pydantic import BaseModel, Field

from rag_service.api.deps import RequestContext, get_request_context
from rag_service.config.settings import settings
from rag_service.retrieval.graph_search import GraphSearch
from rag_service.retrieval.vector_search import VectorSearch
from rag_service.retrieval.rerank import rerank


router = APIRouter(prefix="/v1", tags=["retrieve"])


class RetrieveRequest(BaseModel):
    query: str
    limit: int = Field(default=10, ge=1, le=50)
    alpha: float = Field(default=0.5, ge=0.0, le=1.0)


def _build_scope_filter(ctx: RequestContext) -> wvc.query.Filter:
    base = wvc.query.Filter.by_property("tenantId").equal(ctx.tenant_id)

    branches: list[wvc.query.Filter] = [wvc.query.Filter.by_property("scope").equal("tenant")]

    if ctx.workspace_id:
        branches.append(
            wvc.query.Filter.all_of(
                [
                    wvc.query.Filter.by_property("scope").equal("workspace"),
                    wvc.query.Filter.by_property("workspaceId").equal(ctx.workspace_id),
                ]
            )
        )
        if ctx.principal_id:
            branches.append(
                wvc.query.Filter.all_of(
                    [
                        wvc.query.Filter.by_property("scope").equal("user"),
                        wvc.query.Filter.by_property("workspaceId").equal(ctx.workspace_id),
                        wvc.query.Filter.by_property("principalId").equal(ctx.principal_id),
                    ]
                )
            )

    return wvc.query.Filter.all_of([base, wvc.query.Filter.any_of(branches)])


@router.post("/retrieve")
def retrieve(req: RetrieveRequest, ctx: RequestContext = Depends(get_request_context)) -> dict[str, Any]:
    vs = VectorSearch()
    try:
        filters = _build_scope_filter(ctx)
        # Oversample for reranking.
        search_limit = min(50, max(req.limit, req.limit * settings.rerank_oversample))
        results = vs.search(query=req.query, limit=search_limit, alpha=req.alpha, filters=filters)

        candidates: list[dict[str, Any]] = []
        for r in results:
            props = r["properties"] or {}
            candidates.append(
                {
                    "source": "weaviate",
                    "weaviate_uuid": r["weaviate_uuid"],
                    "score": r.get("score"),
                    "chunk_id": props.get("chunkId"),
                    "text": props.get("text"),
                    "title": props.get("title"),
                    "section": props.get("section"),
                    "summary": props.get("summary"),
                    "pages": props.get("pages"),
                    "doc_id": props.get("parentDocId"),
                    "scope": props.get("scope"),
                    "workspace_id": props.get("workspaceId"),
                    "principal_id": props.get("principalId"),
                }
            )

        expanded: list[dict[str, Any]] = []
        if settings.graph_expansion_enabled:
            seed_ranked = rerank(req.query, candidates, text_key="text")
            seed_chunk_ids: list[str] = []
            for c in seed_ranked:
                chunk_id = c.get("chunk_id")
                if not chunk_id:
                    continue
                score = c.get("rerank_score")
                if score is not None and float(score) < settings.graph_seed_min_rerank_score:
                    break
                seed_chunk_ids.append(str(chunk_id))
                if len(seed_chunk_ids) >= settings.graph_seed_limit:
                    break
            try:
                gs = GraphSearch()
                graph_rows = gs.expand(
                    seed_chunk_ids=seed_chunk_ids,
                    ctx=ctx,
                    limit=settings.graph_expansion_limit,
                    entity_limit=settings.graph_entity_limit,
                )
                for row in graph_rows:
                    expanded.append(
                        {
                            "source": "graph",
                            "weaviate_uuid": None,
                            "score": None,
                            "chunk_id": row.get("chunk_id"),
                            "text": row.get("text"),
                            "title": row.get("title"),
                            "section": row.get("section"),
                            "summary": row.get("summary"),
                            "pages": row.get("pages"),
                            "doc_id": row.get("doc_id"),
                            "scope": row.get("scope"),
                            "workspace_id": row.get("workspace_id"),
                            "principal_id": row.get("principal_id"),
                            "graph_shared_entities": row.get("graph_shared_entities"),
                            "graph_entities": row.get("graph_entities"),
                        }
                    )
            except Exception:
                # Graph expansion is best-effort; retrieval must still work without it.
                expanded = []

        dedup: dict[str, dict[str, Any]] = {}
        for c in candidates:
            key = str(c.get("chunk_id") or c.get("weaviate_uuid") or "")
            if key:
                dedup[key] = c
        for g in expanded:
            key = str(g.get("chunk_id") or g.get("weaviate_uuid") or "")
            if not key:
                continue
            if key in dedup:
                existing = dedup[key]
                existing.setdefault("also_from_graph", True)
                if g.get("graph_shared_entities") is not None:
                    existing["graph_shared_entities"] = g.get("graph_shared_entities")
                if g.get("graph_entities") is not None:
                    existing["graph_entities"] = g.get("graph_entities")
            else:
                dedup[key] = g

        merged = list(dedup.values())
        ranked = rerank(req.query, merged, text_key="text")
        ranked = ranked[: req.limit]
        return {"query": req.query, "count": len(ranked), "results": ranked}
    finally:
        vs.close()
