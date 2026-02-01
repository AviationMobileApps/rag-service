from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
import structlog

from rag_service.api.routes.health import router as health_router
from rag_service.api.routes.retrieve import router as retrieve_router
from rag_service.api.routes.ingest import router as ingest_router
from rag_service.api.routes.documents import router as documents_router
from rag_service.api.routes.ingestion_progress import router as ingestion_progress_router
from rag_service.api.routes.graph import router as graph_router
from rag_service.api.routes.admin import router as admin_router
from rag_service.config.settings import settings
from rag_service.db.models import Base
from rag_service.db.session import engine
from rag_service.retrieval.vector_search import VectorSearch


logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # DB tables (bootstrap; Alembic later)
    Base.metadata.create_all(bind=engine)

    # Ensure Weaviate schema exists
    try:
        vs = VectorSearch()
        vs.ensure_schema()
    finally:
        try:
            vs.close()
        except Exception:
            pass

    logger.info("rag_service_started", port=settings.rag_api_port)
    yield


app = FastAPI(title="rag-service", version="0.1.0", lifespan=lifespan)
app.include_router(health_router)
app.include_router(retrieve_router)
app.include_router(ingest_router)
app.include_router(documents_router)
app.include_router(ingestion_progress_router)
app.include_router(graph_router)
app.include_router(admin_router)
