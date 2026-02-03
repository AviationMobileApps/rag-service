from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
import time
from urllib.parse import urlparse

import redis
import structlog

from rag_service.config.settings import settings
from rag_service.db.models import Base, Document, DocumentStatus
from rag_service.db.session import SessionLocal, engine
from rag_service.ingestion.dynamic_chunker import chunk_pdf_file, chunk_text_file
from rag_service.ingestion.entity_extractor import EntityExtractor
from rag_service.ingestion.graph_loader import GraphLoader
from rag_service.llm.client import LLMClient
from rag_service.retrieval.vector_search import VectorSearch


logger = structlog.get_logger()


STAGE_PROGRESS = {
    "queued": 0,
    "processing": 5,
    "reading": 10,
    "chunking": 35,
    "embedding": 55,
    "weaviate": 75,
    "entities": 85,
    "neo4j": 95,
    "indexed": 100,
    "failed": 0,
}

WORKERS_PAUSED_KEY = "rag_service:workers_paused_at"
WORKERS_CONCURRENCY_KEY = "rag_service:workers_concurrency"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def publish_progress(r: redis.Redis, doc: Document, stage: str, message: str) -> None:
    payload = {
        "doc_id": doc.doc_id,
        "tenant_id": doc.tenant_id,
        "scope": doc.scope.value,
        "workspace_id": doc.workspace_id,
        "principal_id": doc.principal_id,
        "filename": doc.filename,
        "stage": stage,
        "progress": STAGE_PROGRESS.get(stage, 0),
        "message": message,
        "timestamp": _now_iso(),
    }
    r.setex(f"progress:{doc.doc_id}", 3600, json.dumps(payload))
    r.publish(settings.redis_progress_channel, json.dumps(payload))


def _desired_worker_concurrency(r: redis.Redis, *, max_workers: int) -> int:
    raw = (r.get(WORKERS_CONCURRENCY_KEY) or "").strip()
    if not raw:
        raw = (os.getenv("WORKER_CONCURRENCY") or "").strip()
    try:
        v = int(raw)
    except Exception:
        v = 1
    return max(1, min(max_workers, v))


def _worker_pool_size() -> int:
    raw = (os.getenv("WORKER_POOL_SIZE") or "").strip()
    try:
        v = int(raw)
    except Exception:
        v = 8
    return max(1, min(32, v))


_tls = threading.local()


def _thread_clients() -> tuple[LLMClient, EntityExtractor]:
    llm = getattr(_tls, "llm", None)
    entity_extractor = getattr(_tls, "entity_extractor", None)
    if llm is None or entity_extractor is None:
        llm = LLMClient(timeout_s=settings.llm_timeout_s)
        entity_extractor = EntityExtractor(llm=llm, max_entities=settings.entity_extraction_max_entities)
        _tls.llm = llm
        _tls.entity_extractor = entity_extractor
    return llm, entity_extractor


def _process_doc(*, r: redis.Redis, graph: GraphLoader | None, doc_id: str) -> None:
    llm, entity_extractor = _thread_clients()

    session = SessionLocal()
    try:
        doc = session.get(Document, doc_id)
        if not doc:
            logger.warning("document_not_found", doc_id=doc_id)
            return

        doc.status = DocumentStatus.processing
        doc.stage = "processing"
        doc.progress = STAGE_PROGRESS["processing"]
        session.commit()
        publish_progress(r, doc, "processing", "Starting ingestion…")

        # Read file
        publish_progress(r, doc, "reading", "Reading file…")
        path = Path(doc.storage_path)
        if not path.exists():
            raise FileNotFoundError(f"Missing file: {path}")
        content_type = doc.content_type.lower()

        # Chunk
        publish_progress(r, doc, "chunking", "Chunking…")
        if not settings.dynamic_chunking_enabled:
            raise RuntimeError("Dynamic chunking is required (set DYNAMIC_CHUNKING_ENABLED=1)")

        max_window_tokens = settings.chunker_window_tokens
        if not (os.getenv("CHUNKER_WINDOW_TOKENS") or "").strip():
            try:
                host = urlparse(settings.llm_base_url).hostname or ""
            except Exception:
                host = ""
            if host.endswith("airia.ai"):
                max_window_tokens = min(max_window_tokens, 6000)

        if content_type == "text/markdown" or path.suffix.lower() in {".md", ".txt"}:
            dyn_chunks = chunk_text_file(
                doc_id=doc.doc_id,
                text_path=str(path),
                llm=llm,
                doc_type="document",
                max_window_tokens=max_window_tokens,
                overlap_tokens=settings.chunker_overlap_tokens,
                llm_max_tokens=settings.chunker_llm_max_tokens,
                tokenizer_model=settings.chunker_tokenizer_model,
            )
        else:
            dyn_chunks = chunk_pdf_file(
                doc_id=doc.doc_id,
                pdf_path=str(path),
                llm=llm,
                doc_type="document",
                max_window_tokens=max_window_tokens,
                overlap_tokens=settings.chunker_overlap_tokens,
                llm_max_tokens=settings.chunker_llm_max_tokens,
                tokenizer_model=settings.chunker_tokenizer_model,
            )

        if not dyn_chunks:
            raise RuntimeError("Dynamic chunking produced 0 chunks; check LLM connectivity/output and document text extraction")

        # Store chunks in Weaviate
        publish_progress(r, doc, "embedding", "Embedding + indexing…")
        vs = VectorSearch()
        try:
            vs.ensure_schema()
            created_at = _now_iso()
            batch = []
            for ch in dyn_chunks:
                chunk_text = ch.text
                chunk_id = ch.chunk_id
                props = {
                    "text": chunk_text,
                    "title": getattr(ch, "title", doc.filename) or doc.filename,
                    "section": getattr(ch, "section", "unknown") or "unknown",
                    "summary": getattr(ch, "summary", "") or "",
                    "pages": getattr(ch, "pages", []) or [],
                    "whyThisChunk": getattr(ch, "why_this_chunk", "") or "",
                    "docType": "document",
                    "chunkId": chunk_id,
                    "parentDocId": doc.doc_id,
                    "createdAt": created_at,
                    "metadata": "{}",
                    "startChar": int(getattr(ch, "start_char", 0) or 0),
                    "endChar": int(getattr(ch, "end_char", 0) or 0),
                    "tenantId": doc.tenant_id,
                    "scope": doc.scope.value,
                    "workspaceId": doc.workspace_id,
                    "principalId": doc.principal_id,
                }
                batch.append({"text": chunk_text, "properties": props})

            vs.add_chunks(batch)
        finally:
            vs.close()

        entity_count = 0
        if graph is not None:
            publish_progress(r, doc, "entities", "Extracting entities…")
            entities_by_chunk_id = {}
            unique_entities: set[tuple[str, str]] = set()
            for ch in dyn_chunks:
                ents = entity_extractor.extract(ch.text)
                entities_by_chunk_id[ch.chunk_id] = ents
                for e in ents:
                    unique_entities.add((e.type, e.name.lower()))
            entity_count = len(unique_entities)

            publish_progress(r, doc, "neo4j", "Writing graph…")
            graph.upsert_chunks(
                tenant_id=doc.tenant_id,
                scope=doc.scope.value,
                workspace_id=doc.workspace_id,
                principal_id=doc.principal_id,
                parent_doc_id=doc.doc_id,
                chunks=[
                    {
                        "chunk_id": ch.chunk_id,
                        "title": getattr(ch, "title", doc.filename) or doc.filename,
                        "section": getattr(ch, "section", "unknown") or "unknown",
                        "summary": getattr(ch, "summary", "") or "",
                        "pages": getattr(ch, "pages", []) or [],
                        "text": ch.text,
                    }
                    for ch in dyn_chunks
                ],
                entities_by_chunk_id=entities_by_chunk_id,
            )

        doc.status = DocumentStatus.indexed
        doc.stage = "indexed"
        doc.progress = STAGE_PROGRESS["indexed"]
        doc.chunk_count = len(dyn_chunks)
        doc.entity_count = entity_count
        session.commit()
        publish_progress(r, doc, "indexed", f"Indexed {len(dyn_chunks)} chunks")

    except Exception as e:
        logger.exception("ingestion_failed", doc_id=doc_id)
        try:
            doc = session.get(Document, doc_id)
            if doc:
                doc.status = DocumentStatus.failed
                doc.stage = "failed"
                doc.progress = STAGE_PROGRESS["failed"]
                doc.error_message = str(e)
                session.commit()
                publish_progress(r, doc, "failed", str(e))
        except Exception:
            logger.exception("failed_to_mark_failed", doc_id=doc_id)
    finally:
        session.close()


def main() -> None:
    Base.metadata.create_all(bind=engine)

    r = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    logger.info("worker_started", queue=settings.redis_queue)

    graph: GraphLoader | None = None
    if settings.graph_enabled:
        try:
            graph = GraphLoader()
            graph.ensure_constraints()
        except Exception as e:
            logger.warning("neo4j_unavailable_graph_disabled", error=str(e))
            graph = None

    max_workers = _worker_pool_size()
    logger.info("worker_concurrency_ready", max_workers=max_workers)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = set()
        while True:
            # Reap completed tasks.
            done = {f for f in futures if f.done()}
            for fut in done:
                futures.remove(fut)
                try:
                    fut.result()
                except Exception:
                    logger.exception("worker_task_failed")

            paused = bool(r.get(WORKERS_PAUSED_KEY))
            desired = _desired_worker_concurrency(r, max_workers=max_workers)

            if paused:
                time.sleep(0.5)
                continue

            # Fill available slots.
            while len(futures) < desired:
                item = r.brpop(settings.redis_queue, timeout=1)
                if not item:
                    break
                _, raw = item
                try:
                    job = json.loads(raw)
                    doc_id = str(job["doc_id"])
                except Exception:
                    logger.exception("invalid_job_payload", raw=raw[:300])
                    continue

                futures.add(ex.submit(_process_doc, r=r, graph=graph, doc_id=doc_id))

            if futures:
                wait(futures, timeout=0.5, return_when=FIRST_COMPLETED)
            else:
                time.sleep(0.2)


if __name__ == "__main__":
    main()
