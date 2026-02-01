from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from pydantic import ConfigDict
from sqlalchemy import or_, and_
from sqlalchemy.orm import Session

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


@router.get("/documents", response_model=list[DocumentOut])
def list_documents(
    ctx: RequestContext = Depends(get_request_context),
    status: Optional[str] = Query(default=None, description="queued|processing|indexed|failed"),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[DocumentOut]:
    session: Session = SessionLocal()
    try:
        q = session.query(Document).filter(_doc_access_predicate(ctx)).order_by(Document.created_at.desc())
        if status:
            try:
                q = q.filter(Document.status == DocumentStatus(status))
            except Exception:
                raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
        docs = q.limit(limit).all()
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
