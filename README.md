# rag-service

Self-hosted RAG service (FastAPI) with Weaviate (hybrid search), Neo4j (graph expansion), Redis (queue/progress), and Postgres (document metadata/history).

## Local quick start

```bash
cd rag-service
cp compose/.env.example compose/.env
docker compose -f compose/docker-compose.yml --env-file compose/.env up --build -d
```

Then:

- Health: `curl -sS http://localhost:8021/health`
- Admin status page (polling): `http://localhost:8021/admin/status`

## LM Studio (embeddings + LLM)

`compose/.env` defaults to talking to LM Studio via `http://host.docker.internal:1234`.

- If LM Studio runs on the same machine as Docker: keep `host.docker.internal`.
- If LM Studio runs on another box (e.g. `mac-studio-002`): set `EMBEDDINGS_BASE_URL` and `LLM_BASE_URL` to `http://mac-studio-002:1234` (or its LAN IP).

## Upload ingest (PDF / Markdown)

Ingestion pipeline:

- Dynamic chunking via LLM (Signal305-style; falls back to fixed-size chunking if the LLM fails)
- Embeddings via LM Studio → Weaviate
- Entity extraction via LLM → Neo4j (`Chunk`/`Entity` + `MENTIONS`)

Tenant-scoped (default):

```bash
curl -sS -X POST \
  -H 'Authorization: Bearer dev-signal305-key' \
  -F 'scope=tenant' \
  -F 'file=@README.md;type=text/markdown' \
  http://localhost:8021/v1/ingest/document
```

Workspace-scoped:

```bash
curl -sS -X POST \
  -H 'Authorization: Bearer dev-signal305-key' \
  -H 'X-Workspace-Id: ws-alpha' \
  -F 'scope=workspace' \
  -F 'file=@README.md;type=text/markdown' \
  http://localhost:8021/v1/ingest/document
```

User-scoped:

```bash
curl -sS -X POST \
  -H 'Authorization: Bearer dev-signal305-key' \
  -H 'X-Workspace-Id: ws-alpha' \
  -H 'X-Principal-Id: user-007' \
  -F 'scope=user' \
  -F 'file=@README.md;type=text/markdown' \
  http://localhost:8021/v1/ingest/document
```

## Ingestion progress

Active ingestions:

```bash
curl -sS -H 'Authorization: Bearer dev-signal305-key' \
  http://localhost:8021/v1/ingestions/active
```

Live SSE stream:

```bash
curl -N -H 'Authorization: Bearer dev-signal305-key' \
  http://localhost:8021/v1/ingestions/stream
```

## Retrieve

Retrieval pipeline:

- Weaviate hybrid search (BM25 + vectors) → candidate chunks
- Cross-encoder rerank
- Neo4j graph expansion (shared entities) + rerank again

```bash
curl -sS -X POST \
  -H 'Authorization: Bearer dev-signal305-key' \
  -H 'Content-Type: application/json' \
  -d '{"query":"What is the capital of France?","limit":5,"alpha":0.5}' \
  http://localhost:8021/v1/retrieve
```

Some result objects may include graph fields:
- `also_from_graph: true`
- `graph_entities: [...]`
- `graph_shared_entities: <int>`

Note: the first retrieval may take longer while the cross-encoder reranker weights download; they persist in the `rag_models` Docker volume.

## Bulk ingest (10k `.md` files)

The simplest bulk-ingest path is the included CLI wrapper (uses `curl`, so no extra Python deps):

```bash
./scripts/ragctl.py ingest-dir \
  --api-url http://localhost:8021 \
  --api-key dev-newproj-key \
  --root /path/to/markdown \
  --glob '**/*.md' \
  --scope tenant \
  --concurrency 4
```
