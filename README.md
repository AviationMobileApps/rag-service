# rag-service

Self-hosted RAG service (FastAPI) with Weaviate (hybrid search), Neo4j (graph expansion), Redis (queue/progress), and Postgres (document metadata/history).

- Developer API: `API.md`

## Local quick start

```bash
cd rag-service
cp compose/.env.example compose/.env
docker compose -f compose/docker-compose.yml --env-file compose/.env up --build -d
```

Then:

- Health: `curl -sS http://localhost:8021/health`
- Diagnostics page (upload/status/retrieve/entities): `http://localhost:8021/admin/status` (redirects to `/` login if admin auth is enabled)

## LM Studio (embeddings + LLM)

`compose/.env` defaults to talking to LM Studio via `http://host.docker.internal:1234`.

- If LM Studio runs on the same machine as Docker: keep `host.docker.internal`.
- If LM Studio runs on another box (e.g. `mac-studio-002`): set `EMBEDDINGS_BASE_URL` and `LLM_BASE_URL` to `http://mac-studio-002:1234` (or its LAN IP).

## Upload ingest (PDF / Markdown)

Ingestion pipeline:

- Dynamic chunking via LLM (LLM-driven, semantic chunking)
- Embeddings via LM Studio → Weaviate
- Entity extraction via LLM → Neo4j (`Chunk`/`Entity` + `MENTIONS`)

What “dynamic chunking via LLM” means:

- Instead of splitting text by a fixed character count, the worker asks an LLM to create **variable-sized, semantically coherent chunks** (aligned to headings/sections/ideas when possible).
- The worker extracts text (PDF → pages via PyMuPDF; `.md`/`.txt` → text “pages”), then processes it in **overlapping token windows** (`CHUNKER_WINDOW_TOKENS`, `CHUNKER_OVERLAP_TOKENS`) to stay within model context limits.
- For each window, the LLM returns a **JSON** structure describing chunks (chunk text plus metadata such as `title`, `section`, `summary`, and “why this chunk matters”), along with offsets/page hints when available.
- Those chunks become the unit that’s embedded and indexed (Weaviate) and used for entity extraction + graph linking (Neo4j).

Note: ingestion currently treats LLM chunking as required — if the chunking call fails or returns 0 chunks, the document is marked `failed`.

### Exactly what happens when you upload a file (step-by-step)

1. **Client** sends `POST /v1/ingest/document` with:
   - `Authorization: Bearer <api_key>`
   - optional `X-Workspace-Id`, `X-Principal-Id`
   - multipart form fields: `scope` (`tenant` | `workspace` | `user`) + `file`
2. **rag-api** authenticates the Bearer token by mapping `<api_key>` → `tenant_id` via `RAG_TENANTS_JSON` (rejects with `401` if missing/invalid).
3. **rag-api** validates scoping:
   - `scope=workspace|user` requires `X-Workspace-Id`
   - `scope=user` also requires `X-Principal-Id`
4. **rag-api** generates `doc_id` (UUIDv4).
5. **rag-api** persists the uploaded bytes to the shared data volume so the worker can read it:
   - directory: `${RAG_DATA_DIR}/uploads/<tenant_id>/<doc_id>/`
   - filename: the *basename* of the uploaded filename
   - the original uploaded filename is sanitized for display (drops traversal, normalizes `\\` → `/`) and stored as `documents.filename`
6. **rag-api** inserts a row into Postgres table `documents` with (among other fields):
   - `doc_id`, `tenant_id`, `scope`, `workspace_id`, `principal_id`
   - `filename`, `content_type`, `storage_path`
   - `status=queued`, `stage=queued`, `progress=0`
7. **rag-api** enqueues the job into Redis list `${REDIS_QUEUE}` using `LPUSH` with payload `{"doc_id":"..."}`.
8. **rag-api** writes + broadcasts initial progress:
   - sets `progress:<doc_id>` (JSON) with TTL 3600s
   - publishes the same JSON to Redis pub/sub channel `${REDIS_PROGRESS_CHANNEL}`
9. **rag-api** returns `200` JSON: `{"doc_id":"...","status":"queued"}` (ingestion continues asynchronously).
10. **rag-worker** blocks on Redis `BRPOP ${REDIS_QUEUE}`; when it receives the job, it loads the `documents` row and marks it `status=processing`, `stage=processing`, `progress=5`, then publishes a progress event (`stage=processing`, `progress=5`). (Intermediate stages are emitted via Redis progress events; the Postgres row stays at `stage=processing` until completion.)
11. **rag-worker** reads the file from `documents.storage_path` and publishes `stage=reading` (`progress=10`).
12. **rag-worker** performs LLM-driven dynamic chunking (publishes `stage=chunking`, `progress=35`):
   - if `content_type == text/markdown` **or** file extension is `.md`/`.txt`: reads as text; otherwise treats it as PDF
   - extracts text into “pages” (PDF via PyMuPDF; text files are split into pseudo-pages)
   - builds token windows w/ overlap (`CHUNKER_WINDOW_TOKENS`, `CHUNKER_OVERLAP_TOKENS`) and calls the LLM (`LLM_BASE_URL`/`LLM_MODEL`) to return a JSON array of chunk objects
   - converts each chunk into an internal chunk record with a UUID `chunk_id`, plus `start_char`, `end_char`, `pages`, `title`, `section`, `summary`, `why_this_chunk`
13. **rag-worker** embeds + indexes the chunks into Weaviate (publishes `stage=embedding`, `progress=55`):
   - ensures the Weaviate collection `${WEAVIATE_COLLECTION}` exists (vectorizer = none)
   - calls the embeddings endpoint (`EMBEDDINGS_BASE_URL`/`EMBEDDINGS_MODEL`) to get vectors for each chunk text
   - inserts each chunk into Weaviate with properties including `chunkId`, `parentDocId`, `tenantId`, `scope`, `workspaceId`, `principalId`, `startChar`, `endChar`, etc.
14. **rag-worker** (if `GRAPH_ENABLED=1` and Neo4j is reachable) extracts entities + writes the graph:
   - publishes `stage=entities` (`progress=85`), calls the LLM to extract entities per chunk
   - publishes `stage=neo4j` (`progress=95`), `MERGE`s `(:Chunk {chunkId})` and `(:Entity {entityId})`, then creates `(Chunk)-[:MENTIONS]->(Entity)`
15. **rag-worker** finalizes the document:
   - updates Postgres `documents` row: `status=indexed`, `stage=indexed`, `progress=100`, plus `chunk_count` and `entity_count`
   - publishes a final progress event (`stage=indexed`)
16. If any exception occurs in steps 10–15, **rag-worker** marks the document `status=failed`, stores `documents.error_message`, publishes `stage=failed`, and stops processing that job.

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

Note: reranker weights are baked into the Docker image by default (see `Dockerfile` build args `BAKE_RERANKER` / `BAKE_RERANKER_MODEL`) so runtime can be fully offline.

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

## Roadmap (towards “RAG as a Service”)

API / product:

- Add chunk inspection APIs: `GET /v1/documents/{doc_id}/chunks` + `GET /v1/chunks/{chunk_id}` (include `startChar/endChar`, `whyThisChunk`, metadata).
- Add document lifecycle APIs: delete, re-ingest/re-index, cancel ingestion, idempotency keys, and dedup by content hash.
- Add an “answer” API (`POST /v1/answer`) that returns a grounded answer + citations (and optionally streams tokens), built on `POST /v1/retrieve`.
- Add metadata filtering to retrieval (by `doc_id`, filename, tags, date ranges, etc) and support “search within a document”.

Multi-tenancy + security:

- Move from `RAG_TENANTS_JSON` (static) to first-class API key management (create/revoke/rotate; per-tenant quotas; rate limits).
- Enforce stronger isolation in the vector store (Weaviate multi-tenancy or per-tenant collections) and add per-tenant encryption/at-rest options where needed.
- Add RBAC for admin endpoints and audit logging.

Reliability + performance:

- Add adaptive backpressure for LLM/embeddings (per-tenant concurrency caps, retries with jitter, and circuit breaking).
- Provide a deterministic fallback chunker when LLM chunking is unavailable (avoid hard ingestion failures).
- Improve bulk-ingest throughput (streaming uploads, gzip, batching, client-side retries, resumable ingest).

Observability + ops:

- Persist fine-grained ingestion stage history per document (beyond Redis TTL), and expose it via API.
- Add Prometheus metrics + tracing (latency, error rates, queue depth, worker utilization, ingestion throughput).
- Add doc-level “failure reason” taxonomy + remediation hints (vs opaque exception strings).

Retrieval quality:

- Add optional MMR/diversity, field-aware scoring, and query-time controls (graph expansion on/off, rerank on/off).
- Add eval harness + regression tests for retrieval quality (golden queries, recall@k, rerank lift).
