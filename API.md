# rag-service API

Base URL:

- Local: `http://localhost:8021`
- Hosted: `https://rag.airialabs.com`

All `v1` endpoints require `Authorization: Bearer <api_key>`.

## Authentication + scoping

The Bearer token maps to a `tenant_id` (configured server-side via `RAG_TENANTS_JSON`).

Optional scoping headers:

- `X-Workspace-Id: <workspace_id>`
- `X-Principal-Id: <principal_id>`

Scopes:

- `tenant`: visible to anyone using the tenant API key (default)
- `workspace`: visible only when `X-Workspace-Id` matches
- `user`: visible only when `X-Workspace-Id` + `X-Principal-Id` match

Important: **documents and retrieval always include `tenant` scope results**, plus `workspace`/`user` scope results only when you provide the matching headers.

## Quickstart (typical client flow)

1) Upload a document → get `doc_id`
2) Poll document status (or subscribe to progress events) until `status=indexed`
3) Call retrieve with a query → get best-matching chunks (hybrid + rerank + optional graph expansion)

## Endpoints (developer-facing)

### Meta

#### `GET /v1/whoami`

Returns the resolved tenant + the scope headers you provided.

Response:

```json
{ "tenant_id": "airia", "workspace_id": null, "principal_id": null }
```

---

### Ingest

#### `POST /v1/ingest/document`

Upload a file for async ingestion (dynamic chunking → embeddings/Weaviate → entities/Neo4j).

Headers:

- `Authorization: Bearer <api_key>`
- `X-Workspace-Id` (required if `scope=workspace|user`)
- `X-Principal-Id` (required if `scope=user`)

Multipart form fields:

- `scope`: `tenant|workspace|user` (default `tenant`)
- `file`: the uploaded file (`.md`, `.txt`, `.pdf` supported; other types treated as PDF)

Example:

```bash
curl -sS -X POST \
  -H 'Authorization: Bearer <api_key>' \
  -F 'scope=tenant' \
  -F 'file=@/path/to/doc.md;type=text/markdown' \
  http://localhost:8021/v1/ingest/document
```

Response:

```json
{ "doc_id": "ebbd39cf-a68a-4f20-800c-4e5c60efb969", "status": "queued" }
```

Errors:

- `401` missing/invalid Bearer token
- `400` invalid scope, missing scope headers, or empty upload

---

### Documents

#### `GET /v1/documents/{doc_id}`

Fetch a document’s current status + counts.

Response (shape):

```json
{
  "doc_id": "...",
  "tenant_id": "...",
  "scope": "tenant|workspace|user",
  "workspace_id": null,
  "principal_id": null,
  "filename": "...",
  "content_type": "text/markdown",
  "status": "queued|processing|indexed|failed",
  "stage": "queued|processing|indexed|failed",
  "progress": 0,
  "error_message": null,
  "chunk_count": 0,
  "entity_count": 0,
  "created_at": "2026-02-03T00:19:46.865739+00:00",
  "updated_at": "2026-02-03T00:19:46.865739+00:00"
}
```

Note: the `stage/progress` fields in Postgres are **coarse** (queued/processing/indexed/failed). For fine-grained stages (reading/chunking/embedding/etc), use the ingestion progress endpoints below.

#### `GET /v1/documents`

List documents visible under your current scope headers.

Query params:

- `status` (optional): `queued|processing|indexed|failed`
- `limit`: `1..500` (default `100`)
- `offset`: `>=0` (default `0`)
- `sort`: `created_at|updated_at|filename|status|stage|progress|chunk_count|entity_count` (default `created_at`)
- `order`: `asc|desc` (default `desc`)

#### `GET /v1/documents/counts`

Near real-time counts by status for the current scope headers.

Response:

```json
{ "total": 10, "queued": 0, "processing": 0, "indexed": 10, "failed": 0 }
```

---

### Ingestion progress

#### `GET /v1/ingestions/active`

Returns active ingestions (`queued` + `processing`) with the most recent progress event per `doc_id` (from Redis).

Response:

```json
{ "active": [ { "doc_id": "...", "stage": "chunking", "progress": 35, "message": "Chunking…", "timestamp": "..." } ] }
```

#### `GET /v1/ingestions/stream` (SSE)

Server-sent events stream of progress updates. First event is:

```json
{ "type": "connected" }
```

Then each event is a JSON object like:

```json
{
  "doc_id": "...",
  "tenant_id": "...",
  "scope": "tenant|workspace|user",
  "workspace_id": null,
  "principal_id": null,
  "filename": "...",
  "stage": "reading|chunking|embedding|entities|neo4j|indexed|failed",
  "progress": 55,
  "message": "Embedding + indexing…",
  "timestamp": "..."
}
```

Example:

```bash
curl -N -H 'Authorization: Bearer <api_key>' \
  http://localhost:8021/v1/ingestions/stream
```

---

### Retrieval (RAG)

#### `POST /v1/retrieve`

Hybrid retrieval (sparse + dense) from Weaviate, cross-encoder rerank, and optional graph expansion via Neo4j.

Body:

```json
{ "query": "…", "limit": 10, "alpha": 0.5 }
```

- `limit`: `1..50` (default `10`)
- `alpha`: `0..1` where `0` = sparse-only, `1` = dense-only, in-between = hybrid (default `0.5`)

Example:

```bash
curl -sS -X POST \
  -H 'Authorization: Bearer <api_key>' \
  -H 'Content-Type: application/json' \
  -d '{"query":"What is ORBITAL-PENGUIN-742?","limit":5,"alpha":0.5}' \
  http://localhost:8021/v1/retrieve
```

Response:

```json
{
  "query": "…",
  "count": 5,
  "graph": { "enabled": true, "seed_chunk_ids": [], "expanded_count": 0, "error": null },
  "results": [
    {
      "source": "weaviate|graph",
      "weaviate_uuid": "...",
      "score": 0.123,
      "rerank_score": 0.87,
      "chunk_id": "...",
      "doc_id": "...",
      "scope": "tenant|workspace|user",
      "workspace_id": null,
      "principal_id": null,
      "title": "...",
      "section": "...",
      "summary": "...",
      "pages": [1, 2],
      "text": "...",
      "also_from_graph": true,
      "graph_shared_entities": 3,
      "graph_entities": ["..."]
    }
  ]
}
```

---

### Graph exploration

All graph endpoints are best-effort (Neo4j must be enabled + reachable).

#### `GET /v1/graph/entities`

List top entities (by chunk mentions).

Query params:

- `q` (optional): case-insensitive substring match on entity name
- `entity_type` (optional): exact match on entity type
- `limit`: `1..500` (default `50`)

Response:

```json
{
  "count": 2,
  "entities": [
    { "entity_id": "...", "type": "Person", "name": "Ada Lovelace", "chunk_mentions": 7 }
  ]
}
```

#### `GET /v1/graph/entities/{entity_id}/chunks`

Get recent chunks mentioning an entity.

Query params:

- `limit`: `1..200` (default `25`)

#### `GET /v1/graph/documents/{doc_id}/entities`

Get entities extracted from a document.

Query params:

- `limit`: `1..500` (default `50`)

---

### Health

#### `GET /health`

Aggregated dependency health checks (Postgres/Redis/Weaviate/Neo4j/Embeddings).

---

## Admin / operator endpoints

These endpoints are intended for trusted/local use. If `RAG_ADMIN_USERNAME` + `RAG_ADMIN_PASSWORD` are set, `/admin/*` (and `/docs`, `/openapi.json`) require an admin session cookie.

- `GET /admin/status` (HTML)
- `GET /admin/workers/status`
- `POST /admin/workers/start`
- `POST /admin/workers/stop`
- `POST /admin/workers/concurrency` `{ "concurrency": 1..32 }`
- `POST /admin/reset/tenant` `{ "confirm": "RESET" }` (tenant-scoped via Bearer token)
- `POST /admin/reset/all` `{ "confirm": "RESET ALL" }`

## OpenAPI

- `GET /openapi.json`
- `GET /docs`

When admin auth is enabled, you must log in at `/` first to access these.

