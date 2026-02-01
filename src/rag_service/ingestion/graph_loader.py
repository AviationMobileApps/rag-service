from __future__ import annotations

import hashlib
from typing import Any, Optional

import structlog
from neo4j import GraphDatabase

from rag_service.config.settings import settings
from rag_service.ingestion.entity_extractor import Entity


logger = structlog.get_logger()


def _entity_id(*, tenant_id: str, entity_type: str, name: str) -> str:
    h = hashlib.sha1()
    h.update(f"{tenant_id}|{entity_type}|{name.lower()}".encode("utf-8"))
    return h.hexdigest()


class GraphLoader:
    def __init__(
        self,
        *,
        uri: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
    ):
        self.uri = uri or settings.neo4j_uri
        self.user = user or settings.neo4j_user
        self.password = password or settings.neo4j_password
        self.driver = GraphDatabase.driver(self.uri, auth=(self.user, self.password))

    def close(self) -> None:
        self.driver.close()

    def ensure_constraints(self) -> None:
        cypher = [
            "CREATE CONSTRAINT chunk_chunk_id IF NOT EXISTS FOR (c:Chunk) REQUIRE c.chunkId IS UNIQUE",
            "CREATE CONSTRAINT entity_entity_id IF NOT EXISTS FOR (e:Entity) REQUIRE e.entityId IS UNIQUE",
        ]
        with self.driver.session(database=settings.neo4j_database) as session:
            for stmt in cypher:
                session.run(stmt).consume()
        logger.info("neo4j_constraints_ready")

    def upsert_chunks(
        self,
        *,
        tenant_id: str,
        scope: str,
        workspace_id: Optional[str],
        principal_id: Optional[str],
        parent_doc_id: str,
        chunks: list[dict[str, Any]],
        entities_by_chunk_id: dict[str, list[Entity]],
    ) -> int:
        payload: list[dict[str, Any]] = []
        for ch in chunks:
            chunk_id = str(ch.get("chunk_id") or "")
            if not chunk_id:
                continue
            ents = []
            for e in entities_by_chunk_id.get(chunk_id, []):
                ents.append(
                    {
                        "entity_id": _entity_id(tenant_id=tenant_id, entity_type=e.type, name=e.name),
                        "type": e.type,
                        "name": e.name,
                    }
                )
            payload.append(
                {
                    "chunk_id": chunk_id,
                    "tenant_id": tenant_id,
                    "scope": scope,
                    "workspace_id": workspace_id,
                    "principal_id": principal_id,
                    "parent_doc_id": parent_doc_id,
                    "title": ch.get("title") or "",
                    "section": ch.get("section") or "",
                    "summary": ch.get("summary") or "",
                    "pages": ch.get("pages") or [],
                    "text": ch.get("text") or "",
                    "entities": ents,
                }
            )

        if not payload:
            return 0

        query = """
UNWIND $chunks AS ch
MERGE (c:Chunk {chunkId: ch.chunk_id})
SET c.tenantId = ch.tenant_id,
    c.scope = ch.scope,
    c.workspaceId = ch.workspace_id,
    c.principalId = ch.principal_id,
    c.parentDocId = ch.parent_doc_id,
    c.title = ch.title,
    c.section = ch.section,
    c.summary = ch.summary,
    c.pages = ch.pages,
    c.text = ch.text,
    c.updatedAt = datetime()
WITH c, ch
UNWIND ch.entities AS ent
MERGE (e:Entity {entityId: ent.entity_id})
SET e.tenantId = ch.tenant_id,
    e.type = ent.type,
    e.name = ent.name,
    e.updatedAt = datetime()
MERGE (c)-[:MENTIONS]->(e)
"""

        with self.driver.session(database=settings.neo4j_database) as session:
            session.run(query, chunks=payload).consume()

        logger.info("neo4j_upsert_complete", chunks=len(payload))
        return len(payload)

