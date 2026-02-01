from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class DocumentScope(str, enum.Enum):
    tenant = "tenant"
    workspace = "workspace"
    user = "user"


class DocumentStatus(str, enum.Enum):
    queued = "queued"
    processing = "processing"
    indexed = "indexed"
    failed = "failed"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Document(Base):
    __tablename__ = "documents"

    doc_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    scope: Mapped[DocumentScope] = mapped_column(Enum(DocumentScope), index=True)
    workspace_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    principal_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)

    filename: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[str] = mapped_column(String(128))
    storage_path: Mapped[str] = mapped_column(String(1024))

    status: Mapped[DocumentStatus] = mapped_column(Enum(DocumentStatus), index=True, default=DocumentStatus.queued)
    stage: Mapped[str] = mapped_column(String(64), default="queued")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    entity_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

