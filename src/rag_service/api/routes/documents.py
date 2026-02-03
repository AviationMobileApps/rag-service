from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from pydantic import ConfigDict
from sqlalchemy import func, or_, and_
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from rag_service.api.deps import RequestContext, get_request_context
from rag_service.db.models import Document, DocumentScope, DocumentStatus
from rag_service.db.session import SessionLocal


router = APIRouter(prefix="/v1", tags=["documents"])


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    doc_id: str
    tenant_id: str
    scope: str
    workspace_id: Optional[str]
    principal_id: Optional[str]
    filename: str
    content_type: str
    status: str
    stage: str
    progress: int
    error_message: Optional[str]
    chunk_count: int
    entity_count: int
    created_at: datetime
    updated_at: datetime


def _doc_access_predicate(ctx: RequestContext):
    clauses = [and_(Document.tenant_id == ctx.tenant_id, Document.scope == DocumentScope.tenant)]
    if ctx.workspace_id:
        clauses.append(
            and_(
                Document.tenant_id == ctx.tenant_id,
                Document.scope == DocumentScope.workspace,
                Document.workspace_id == ctx.workspace_id,
            )
        )
        if ctx.principal_id:
            clauses.append(
                and_(
                    Document.tenant_id == ctx.tenant_id,
                    Document.scope == DocumentScope.user,
                    Document.workspace_id == ctx.workspace_id,
                    Document.principal_id == ctx.principal_id,
                )
            )
    return or_(*clauses)


class DocumentStatusCountsOut(BaseModel):
    total: int
    queued: int
    processing: int
    indexed: int
    failed: int


@router.get("/documents/counts", response_model=DocumentStatusCountsOut)
def documents_counts(ctx: RequestContext = Depends(get_request_context)) -> DocumentStatusCountsOut:
    session: Session = SessionLocal()
    try:
        rows = (
            session.query(Document.status, func.count(Document.doc_id))
            .filter(_doc_access_predicate(ctx))
            .group_by(Document.status)
            .all()
        )
        counts = {s.value: 0 for s in DocumentStatus}
        for status, n in rows:
            if status is None:
                continue
            key = status.value if isinstance(status, DocumentStatus) else str(status)
            if key in counts:
                counts[key] = int(n or 0)
        total = sum(counts.values())
        return DocumentStatusCountsOut(total=total, **counts)
    finally:
        session.close()


@router.get("/documents", response_model=list[DocumentOut])
def list_documents(
    ctx: RequestContext = Depends(get_request_context),
    status: Optional[str] = Query(default=None, description="queued|processing|indexed|failed"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    sort: str = Query(default="created_at", description="created_at|updated_at|filename|status|stage|progress|chunk_count|entity_count"),
    order: str = Query(default="desc", description="asc|desc"),
) -> list[DocumentOut]:
    session: Session = SessionLocal()
    try:
        sort_map: dict[str, ColumnElement] = {
            "created_at": Document.created_at,
            "updated_at": Document.updated_at,
            "filename": Document.filename,
            "status": Document.status,
            "stage": Document.stage,
            "progress": Document.progress,
            "chunk_count": Document.chunk_count,
            "entity_count": Document.entity_count,
        }

        sort_key = (sort or "created_at").strip().lower()
        col = sort_map.get(sort_key)
        if col is None:
            raise HTTPException(status_code=400, detail=f"Invalid sort: {sort}")

        order_key = (order or "desc").strip().lower()
        if order_key == "asc":
            order_by = col.asc()
        elif order_key == "desc":
            order_by = col.desc()
        else:
            raise HTTPException(status_code=400, detail=f"Invalid order: {order}")

        q = session.query(Document).filter(_doc_access_predicate(ctx)).order_by(order_by, Document.doc_id.asc())
        if status:
            try:
                q = q.filter(Document.status == DocumentStatus(status))
            except Exception:
                raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
        docs = q.offset(offset).limit(limit).all()
        return [DocumentOut.model_validate(d) for d in docs]
    finally:
        session.close()


@router.get("/documents/{doc_id}", response_model=DocumentOut)
def get_document(doc_id: str, ctx: RequestContext = Depends(get_request_context)) -> DocumentOut:
    session: Session = SessionLocal()
    try:
        doc = session.query(Document).filter(Document.doc_id == doc_id, _doc_access_predicate(ctx)).one_or_none()
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        return DocumentOut.model_validate(doc)
    finally:
        session.close()
