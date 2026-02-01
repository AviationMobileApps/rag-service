from __future__ import annotations

from functools import lru_cache
from typing import Any

from neo4j import GraphDatabase

from rag_service.api.deps import RequestContext
from rag_service.config.settings import settings


@lru_cache(maxsize=1)
def _driver():
    return GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))


def _scope_filter_cypher(var: str = "c") -> str:
    # Keep in sync with Weaviate scope filter logic:
    # - tenant-scope always allowed
    # - workspace-scope allowed only with workspace_id
    # - user-scope allowed only with workspace_id + principal_id
    return f"""(
  {var}.scope = 'tenant'
  OR ($workspace_id IS NOT NULL AND {var}.scope = 'workspace' AND {var}.workspaceId = $workspace_id)
  OR ($workspace_id IS NOT NULL AND $principal_id IS NOT NULL AND {var}.scope = 'user' AND {var}.workspaceId = $workspace_id AND {var}.principalId = $principal_id)
)"""


class GraphSearch:
    def expand(
        self,
        *,
        seed_chunk_ids: list[str],
        ctx: RequestContext,
        limit: int = 20,
        entity_limit: int = 25,
    ) -> list[dict[str, Any]]:
        if not seed_chunk_ids:
            return []

        query = f"""
MATCH (seed:Chunk)
WHERE seed.tenantId = $tenant_id AND seed.chunkId IN $seed_chunk_ids AND {_scope_filter_cypher('seed')}
MATCH (seed)-[:MENTIONS]->(e:Entity)
WHERE e.tenantId = $tenant_id
WITH e, count(*) AS freq
ORDER BY freq DESC
LIMIT $entity_limit
MATCH (e)<-[:MENTIONS]-(c:Chunk)
WHERE c.tenantId = $tenant_id AND NOT (c.chunkId IN $seed_chunk_ids) AND {_scope_filter_cypher('c')}
WITH c, collect(DISTINCT e.name) AS via_entities, count(DISTINCT e) AS shared_count
RETURN
  c.chunkId AS chunk_id,
  c.parentDocId AS doc_id,
  c.scope AS scope,
  c.workspaceId AS workspace_id,
  c.principalId AS principal_id,
  c.title AS title,
  c.section AS section,
  c.summary AS summary,
  c.pages AS pages,
  c.text AS text,
  shared_count AS graph_shared_entities,
  via_entities[0..5] AS graph_entities
ORDER BY graph_shared_entities DESC
LIMIT $limit
"""

        params = {
            "tenant_id": ctx.tenant_id,
            "workspace_id": ctx.workspace_id,
            "principal_id": ctx.principal_id,
            "seed_chunk_ids": seed_chunk_ids,
            "limit": int(limit),
            "entity_limit": int(entity_limit),
        }

        with _driver().session(database=settings.neo4j_database) as session:
            rows = session.run(query, **params)
            return [r.data() for r in rows]
