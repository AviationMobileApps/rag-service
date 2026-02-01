from __future__ import annotations

import json
import time
from typing import Iterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
import redis
from sqlalchemy import or_, and_

from rag_service.config.settings import settings
from rag_service.api.deps import RequestContext, get_request_context
from rag_service.db.models import Document, DocumentScope, DocumentStatus
from rag_service.db.session import SessionLocal


router = APIRouter(prefix="/v1/ingestions", tags=["ingestion-progress"])


@router.get("/active")
def active(ctx: RequestContext = Depends(get_request_context)):
    session = SessionLocal()
    try:
        access = [and_(Document.tenant_id == ctx.tenant_id, Document.scope == DocumentScope.tenant)]
        if ctx.workspace_id:
            access.append(
                and_(
                    Document.tenant_id == ctx.tenant_id,
                    Document.scope == DocumentScope.workspace,
                    Document.workspace_id == ctx.workspace_id,
                )
            )
            if ctx.principal_id:
                access.append(
                    and_(
                        Document.tenant_id == ctx.tenant_id,
                        Document.scope == DocumentScope.user,
                        Document.workspace_id == ctx.workspace_id,
                        Document.principal_id == ctx.principal_id,
                    )
                )

        docs = (
            session.query(Document)
            .filter(
                and_(
                    or_(*access),
                    or_(Document.status == DocumentStatus.queued, Document.status == DocumentStatus.processing),
                )
            )
            .order_by(Document.created_at.desc())
            .limit(500)
            .all()
        )
    finally:
        session.close()

    r = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    out = []
    for d in docs:
        cached = r.get(f"progress:{d.doc_id}")
        if cached:
            try:
                out.append(json.loads(cached))
                continue
            except Exception:
                pass
        out.append(
            {
                "doc_id": d.doc_id,
                "stage": d.stage,
                "progress": d.progress,
                "message": "In progress…",
                "timestamp": d.updated_at.isoformat() if d.updated_at else None,
            }
        )
    return {"active": out}


@router.get("/stream")
def stream(ctx: RequestContext = Depends(get_request_context)):
    def allowed(event: dict) -> bool:
        if event.get("tenant_id") != ctx.tenant_id:
            return False
        scope = event.get("scope")
        if scope == "tenant":
            return True
        if scope == "workspace":
            return bool(ctx.workspace_id and event.get("workspace_id") == ctx.workspace_id)
        if scope == "user":
            return bool(
                ctx.workspace_id
                and ctx.principal_id
                and event.get("workspace_id") == ctx.workspace_id
                and event.get("principal_id") == ctx.principal_id
            )
        return False

    def gen() -> Iterator[str]:
        r = redis.Redis.from_url(settings.redis_url, decode_responses=True)
        pubsub = r.pubsub()
        pubsub.subscribe(settings.redis_progress_channel)
        yield f"data: {json.dumps({'type': 'connected'})}\n\n"
        try:
            while True:
                msg = pubsub.get_message(timeout=1.0)
                if msg and msg.get("type") == "message":
                    try:
                        data = json.loads(msg.get("data") or "{}")
                        if allowed(data):
                            yield f"data: {json.dumps(data)}\n\n"
                    except Exception:
                        pass
                time.sleep(0.1)
        finally:
            try:
                pubsub.close()
            except Exception:
                pass

    return StreamingResponse(gen(), media_type="text/event-stream")
