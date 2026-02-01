from __future__ import annotations

import time

from fastapi import APIRouter
import redis
import weaviate
from neo4j import GraphDatabase
from sqlalchemy import text

from rag_service.config.settings import settings
from rag_service.db.session import engine
from rag_service.llm.openai_compat import OpenAICompatClient


router = APIRouter()


@router.get("/health")
def health():
    t0 = time.time()

    checks: dict[str, object] = {}

    # Postgres
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["postgres"] = {"ok": True}
    except Exception as e:
        checks["postgres"] = {"ok": False, "error": str(e)}

    # Redis
    try:
        r = redis.Redis.from_url(settings.redis_url, decode_responses=True)
        checks["redis"] = {"ok": r.ping() is True}
    except Exception as e:
        checks["redis"] = {"ok": False, "error": str(e)}

    # Weaviate
    try:
        client = weaviate.connect_to_local(host=settings.weaviate_host, port=settings.weaviate_port)
        try:
            meta = client.get_meta()
        finally:
            client.close()
        checks["weaviate"] = {"ok": True, "version": meta.get("version")}
    except Exception as e:
        checks["weaviate"] = {"ok": False, "error": str(e)}

    # Neo4j
    try:
        driver = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))
        try:
            with driver.session(database=settings.neo4j_database) as session:
                session.run("RETURN 1").consume()
        finally:
            driver.close()
        checks["neo4j"] = {"ok": True}
    except Exception as e:
        checks["neo4j"] = {"ok": False, "error": str(e)}

    # Embeddings endpoint
    try:
        c = OpenAICompatClient(base_url=settings.embeddings_base_url, api_key=settings.embeddings_api_key, timeout_s=10.0)
        try:
            emb = c.embeddings(model=settings.embeddings_model, inputs=["test"])
        finally:
            c.close()
        checks["embeddings"] = {"ok": True, "dim": len(emb[0]) if emb else 0, "model": settings.embeddings_model}
    except Exception as e:
        checks["embeddings"] = {"ok": False, "error": str(e), "base_url": settings.embeddings_base_url}

    return {"ok": all(v.get("ok") for v in checks.values() if isinstance(v, dict)), "checks": checks, "latency_ms": int((time.time() - t0) * 1000)}

