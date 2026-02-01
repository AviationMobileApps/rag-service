from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
import redis
from sqlalchemy.orm import Session

from rag_service.api.deps import RequestContext, get_request_context
from rag_service.config.settings import settings
from rag_service.db.models import Document, DocumentScope, DocumentStatus
from rag_service.db.session import SessionLocal


router = APIRouter(prefix="/v1", tags=["ingest"])


class IngestResponse(BaseModel):
    doc_id: str
    status: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_display_filename(raw: str, *, default: str) -> str:
    name = (raw or "").replace("\x00", "").strip()
    if not name:
        name = default

    # Normalize Windows separators to "/".
    name = name.replace("\\", "/")

    # Force relative-ish display (avoid leading "/").
    name = name.lstrip("/")

    # Drop traversal components; keep remaining components for user-friendly display.
    parts: list[str] = []
    for part in name.split("/"):
        part = part.strip()
        if not part or part == "." or part == "..":
            continue
        parts.append(part)

    name = "/".join(parts) if parts else default
    return name[:512]


def _publish_queued(
    r: redis.Redis,
    *,
    doc_id: str,
    tenant_id: str,
    scope: str,
    workspace_id: str | None,
    principal_id: str | None,
    filename: str,
) -> None:
    payload = {
        "doc_id": doc_id,
        "tenant_id": tenant_id,
        "scope": scope,
        "workspace_id": workspace_id,
        "principal_id": principal_id,
        "filename": filename,
        "stage": "queued",
        "progress": 0,
        "message": "Queued for ingestion",
        "timestamp": _now_iso(),
    }
    r.setex(f"progress:{doc_id}", 3600, json.dumps(payload))
    r.publish(settings.redis_progress_channel, json.dumps(payload))


@router.post("/ingest/document", response_model=IngestResponse)
def ingest_document(
    ctx: RequestContext = Depends(get_request_context),
    file: UploadFile = File(...),
    scope: str = Form(default="tenant"),
):
    # Resolve scope from form + headers.
    try:
        doc_scope = DocumentScope(scope)
    except Exception:
        raise HTTPException(status_code=400, detail=f"Invalid scope: {scope}")

    workspace_id = ctx.workspace_id
    principal_id = ctx.principal_id

    if doc_scope in {DocumentScope.workspace, DocumentScope.user} and not workspace_id:
        raise HTTPException(status_code=400, detail="Missing X-Workspace-Id header for workspace/user scoped document")
    if doc_scope == DocumentScope.user and not principal_id:
        raise HTTPException(status_code=400, detail="Missing X-Principal-Id header for user scoped document")

    doc_id = str(uuid.uuid4())

    # Persist file to the shared volume so the worker can read it.
    uploads_dir = Path(settings.rag_data_dir) / "uploads" / ctx.tenant_id / doc_id
    uploads_dir.mkdir(parents=True, exist_ok=True)

    display_filename = _sanitize_display_filename(str(file.filename or ""), default=doc_id)
    storage_filename = Path(display_filename).name
    storage_path = uploads_dir / storage_filename

    content = file.file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty upload")
    storage_path.write_bytes(content)

    content_type = file.content_type or "application/octet-stream"

    session: Session = SessionLocal()
    try:
        doc = Document(
            doc_id=doc_id,
            tenant_id=ctx.tenant_id,
            scope=doc_scope,
            workspace_id=workspace_id if doc_scope != DocumentScope.tenant else None,
            principal_id=principal_id if doc_scope == DocumentScope.user else None,
            filename=display_filename,
            content_type=content_type,
            storage_path=str(storage_path),
            status=DocumentStatus.queued,
            stage="queued",
            progress=0,
        )
        session.add(doc)
        session.commit()
    finally:
        session.close()

    r = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    r.lpush(settings.redis_queue, json.dumps({"doc_id": doc_id}))
    _publish_queued(
        r,
        doc_id=doc_id,
        tenant_id=ctx.tenant_id,
        scope=doc_scope.value,
        workspace_id=workspace_id if doc_scope != DocumentScope.tenant else None,
        principal_id=principal_id if doc_scope == DocumentScope.user else None,
        filename=display_filename,
    )

    return IngestResponse(doc_id=doc_id, status="queued")
