from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from neo4j import GraphDatabase
from pydantic import BaseModel, Field
import redis
from sqlalchemy import text
import weaviate
import weaviate.classes as wvc

from rag_service.api.deps import RequestContext, get_request_context
from rag_service.config.settings import settings
from rag_service.db.session import engine
from rag_service.retrieval.vector_search import VectorSearch


router = APIRouter(tags=["admin"])


WORKERS_PAUSED_KEY = "rag_service:workers_paused_at"
WORKERS_CONCURRENCY_KEY = "rag_service:workers_concurrency"


class WorkersStatus(BaseModel):
    paused: bool
    paused_since: str | None
    queue_depth: int
    concurrency: int
    processing: int


@router.get("/admin/workers/status", response_model=WorkersStatus)
def workers_status() -> WorkersStatus:
    r = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    paused_since = r.get(WORKERS_PAUSED_KEY)
    paused = bool(paused_since)
    queue_depth = int(r.llen(settings.redis_queue) or 0)
    raw_conc = (r.get(WORKERS_CONCURRENCY_KEY) or "").strip()
    try:
        concurrency = int(raw_conc) if raw_conc else 1
    except Exception:
        concurrency = 1
    concurrency = max(1, min(32, concurrency))

    processing = 0
    try:
        with engine.begin() as conn:
            processing = int(conn.execute(text("SELECT COUNT(*) FROM documents WHERE status = 'processing'")).scalar() or 0)
    except Exception:
        processing = 0

    return WorkersStatus(paused=paused, paused_since=paused_since, queue_depth=queue_depth, concurrency=concurrency, processing=processing)


class WorkersConcurrencyRequest(BaseModel):
    concurrency: int = Field(ge=1, le=32)


class WorkersConcurrencyResponse(BaseModel):
    ok: bool
    concurrency: int


@router.post("/admin/workers/concurrency", response_model=WorkersConcurrencyResponse)
def workers_set_concurrency(req: WorkersConcurrencyRequest) -> WorkersConcurrencyResponse:
    r = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    v = max(1, min(32, int(req.concurrency)))
    r.set(WORKERS_CONCURRENCY_KEY, str(v))
    return WorkersConcurrencyResponse(ok=True, concurrency=v)


class WorkersActionResponse(BaseModel):
    ok: bool
    paused: bool
    paused_since: str | None


@router.post("/admin/workers/stop", response_model=WorkersActionResponse)
def workers_stop() -> WorkersActionResponse:
    r = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    ts = datetime.now(timezone.utc).isoformat()
    r.set(WORKERS_PAUSED_KEY, ts)
    return WorkersActionResponse(ok=True, paused=True, paused_since=ts)


@router.post("/admin/workers/start", response_model=WorkersActionResponse)
def workers_start() -> WorkersActionResponse:
    r = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    r.delete(WORKERS_PAUSED_KEY)
    return WorkersActionResponse(ok=True, paused=False, paused_since=None)


class ResetAllRequest(BaseModel):
    confirm: str


class ResetAllResponse(BaseModel):
    ok: bool
    paused: bool
    paused_since: str
    redis_cleared: bool
    postgres_cleared: bool
    weaviate_cleared: bool
    neo4j_cleared: bool
    uploads_cleared: bool
    errors: list[str] = Field(default_factory=list)


@router.post("/admin/reset/all", response_model=ResetAllResponse)
def reset_all(req: ResetAllRequest) -> ResetAllResponse:
    if not settings.admin_auth_enabled():
        raise HTTPException(status_code=403, detail="Admin login is not configured.")
    if (req.confirm or "").strip() != "RESET ALL":
        raise HTTPException(status_code=400, detail="Confirmation required. Type RESET ALL to proceed.")

    paused_since = datetime.now(timezone.utc).isoformat()

    errors: list[str] = []
    redis_cleared = False
    postgres_cleared = False
    weaviate_cleared = False
    neo4j_cleared = False
    uploads_cleared = False

    r = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        # Pause workers during the reset.
        r.set(WORKERS_PAUSED_KEY, paused_since)
    except Exception as e:
        errors.append(f"redis(pause): {e}")

    # Redis (queue/progress)
    try:
        r.flushdb()
        redis_cleared = True
    except Exception as e:
        errors.append(f"redis(clear): {e}")
    finally:
        # Keep workers paused after reset; user can click Start workers.
        try:
            r.set(WORKERS_PAUSED_KEY, paused_since)
        except Exception:
            pass

    # Postgres (document metadata/history)
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM documents"))
        postgres_cleared = True
    except Exception as e:
        errors.append(f"postgres(clear): {e}")

    # Weaviate (vector index)
    try:
        client = weaviate.connect_to_local(host=settings.weaviate_host, port=settings.weaviate_port)
        try:
            if client.collections.exists(settings.weaviate_collection):
                client.collections.delete(settings.weaviate_collection)
        finally:
            client.close()

        vs = VectorSearch()
        try:
            vs.ensure_schema()
        finally:
            vs.close()

        weaviate_cleared = True
    except Exception as e:
        errors.append(f"weaviate(clear): {e}")

    # Neo4j (graph)
    try:
        driver = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))
        try:
            with driver.session(database=settings.neo4j_database) as session:
                session.run("MATCH (n) DETACH DELETE n").consume()
        finally:
            driver.close()
        neo4j_cleared = True
    except Exception as e:
        errors.append(f"neo4j(clear): {e}")

    # Uploaded files on disk (/data/uploads)
    try:
        data_root = Path(settings.rag_data_dir).expanduser().resolve()
        uploads_root = (data_root / "uploads").resolve()
        uploads_root.relative_to(data_root)

        if uploads_root.exists():
            for child in uploads_root.iterdir():
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink(missing_ok=True)

        uploads_root.mkdir(parents=True, exist_ok=True)
        uploads_cleared = True
    except Exception as e:
        errors.append(f"uploads(clear): {e}")

    ok = not errors
    return ResetAllResponse(
        ok=ok,
        paused=True,
        paused_since=paused_since,
        redis_cleared=redis_cleared,
        postgres_cleared=postgres_cleared,
        weaviate_cleared=weaviate_cleared,
        neo4j_cleared=neo4j_cleared,
        uploads_cleared=uploads_cleared,
        errors=errors,
    )


class ResetTenantRequest(BaseModel):
    confirm: str


class ResetTenantResponse(BaseModel):
    ok: bool
    tenant_id: str
    paused: bool
    paused_since: str
    redis_progress_deleted: int
    redis_queue_removed: int
    postgres_documents_deleted: int
    weaviate_objects_deleted: int
    neo4j_nodes_deleted: int
    uploads_deleted: bool
    errors: list[str] = Field(default_factory=list)


@router.post("/admin/reset/tenant", response_model=ResetTenantResponse)
def reset_tenant(req: ResetTenantRequest, ctx: RequestContext = Depends(get_request_context)) -> ResetTenantResponse:
    if not settings.admin_auth_enabled():
        raise HTTPException(status_code=403, detail="Admin login is not configured.")
    if (req.confirm or "").strip() != "RESET":
        raise HTTPException(status_code=400, detail="Confirmation required. Type RESET to proceed.")

    tenant_id = ctx.tenant_id
    paused_since = datetime.now(timezone.utc).isoformat()

    errors: list[str] = []
    uploads_deleted = False

    r = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        r.set(WORKERS_PAUSED_KEY, paused_since)
    except Exception as e:
        errors.append(f"redis(pause): {e}")

    # Collect doc_ids first (used to prune Redis queue/progress).
    doc_ids: list[str] = []
    try:
        with engine.begin() as conn:
            rows = conn.execute(text("SELECT doc_id FROM documents WHERE tenant_id = :tenant_id"), {"tenant_id": tenant_id}).fetchall()
            doc_ids = [str(row[0]) for row in rows if row and row[0]]
    except Exception as e:
        errors.append(f"postgres(list_docs): {e}")

    doc_id_set = set(doc_ids)

    # Redis progress keys.
    redis_progress_deleted = 0
    try:
        pipe = r.pipeline()
        for doc_id in doc_id_set:
            pipe.delete(f"progress:{doc_id}")
        if doc_id_set:
            res = pipe.execute()
            redis_progress_deleted += sum(int(x or 0) for x in res)

        # Best-effort cleanup for any lingering progress keys (parse payload for tenant_id).
        cursor = 0
        keys_to_delete: list[str] = []
        while True:
            cursor, keys = r.scan(cursor=cursor, match="progress:*", count=500)
            for k in keys:
                try:
                    raw = r.get(k)
                    if not raw:
                        continue
                    data = json.loads(raw)
                    if isinstance(data, dict) and data.get("tenant_id") == tenant_id:
                        keys_to_delete.append(k)
                except Exception:
                    continue
            if cursor == 0:
                break

        if keys_to_delete:
            pipe = r.pipeline()
            for k in keys_to_delete:
                pipe.delete(k)
            res = pipe.execute()
            redis_progress_deleted += sum(int(x or 0) for x in res)
    except Exception as e:
        errors.append(f"redis(progress): {e}")

    # Redis queue items (doc_id only).
    redis_queue_removed = 0
    try:
        if doc_id_set:
            raw_items = r.lrange(settings.redis_queue, 0, -1)
            keep: list[str] = []
            for raw in raw_items:
                try:
                    job = json.loads(raw)
                    doc_id = str(job.get("doc_id") or "")
                    if doc_id and doc_id in doc_id_set:
                        redis_queue_removed += 1
                        continue
                except Exception:
                    pass
                keep.append(raw)

            if redis_queue_removed:
                pipe = r.pipeline()
                pipe.delete(settings.redis_queue)
                if keep:
                    pipe.rpush(settings.redis_queue, *keep)
                pipe.execute()
    except Exception as e:
        errors.append(f"redis(queue): {e}")

    # Postgres documents.
    postgres_documents_deleted = 0
    try:
        with engine.begin() as conn:
            result = conn.execute(text("DELETE FROM documents WHERE tenant_id = :tenant_id"), {"tenant_id": tenant_id})
            postgres_documents_deleted = int(result.rowcount or 0)
    except Exception as e:
        errors.append(f"postgres(clear): {e}")

    # Weaviate tenant data.
    weaviate_objects_deleted = 0
    try:
        client = weaviate.connect_to_local(host=settings.weaviate_host, port=settings.weaviate_port)
        try:
            if client.collections.exists(settings.weaviate_collection):
                coll = client.collections.get(settings.weaviate_collection)
                flt = wvc.query.Filter.by_property("tenantId").equal(tenant_id)
                res = coll.data.delete_many(where=flt)
                weaviate_objects_deleted = int(getattr(res, "successful", 0) or 0)
        finally:
            client.close()
    except Exception as e:
        errors.append(f"weaviate(clear): {e}")

    # Neo4j tenant data.
    neo4j_nodes_deleted = 0
    try:
        driver = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))
        try:
            with driver.session(database=settings.neo4j_database) as session:
                rec = session.run("MATCH (n) WHERE n.tenantId = $tenant_id RETURN count(n) AS c", tenant_id=tenant_id).single()
                neo4j_nodes_deleted = int((rec or {}).get("c") or 0)
                session.run("MATCH (n) WHERE n.tenantId = $tenant_id DETACH DELETE n", tenant_id=tenant_id).consume()
        finally:
            driver.close()
    except Exception as e:
        errors.append(f"neo4j(clear): {e}")

    # Uploaded files on disk (/data/uploads/<tenant_id>)
    try:
        data_root = Path(settings.rag_data_dir).expanduser().resolve()
        tenant_uploads = (data_root / "uploads" / tenant_id).resolve()
        tenant_uploads.relative_to(data_root)
        if tenant_uploads.exists():
            shutil.rmtree(tenant_uploads)
        uploads_deleted = True
    except Exception as e:
        errors.append(f"uploads(clear): {e}")

    ok = not errors
    return ResetTenantResponse(
        ok=ok,
        tenant_id=tenant_id,
        paused=True,
        paused_since=paused_since,
        redis_progress_deleted=redis_progress_deleted,
        redis_queue_removed=redis_queue_removed,
        postgres_documents_deleted=postgres_documents_deleted,
        weaviate_objects_deleted=weaviate_objects_deleted,
        neo4j_nodes_deleted=neo4j_nodes_deleted,
        uploads_deleted=uploads_deleted,
        errors=errors,
    )


@router.get("/admin/status", response_class=HTMLResponse)
def admin_status() -> str:
    return """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>rag-service • Diagnostics</title>
    <style>
      body { font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, "Apple Color Emoji", "Segoe UI Emoji"; margin: 24px; }
      h1 { margin: 0 0 4px 0; }
      h2 { margin: 0 0 10px 0; font-size: 16px; }
      .row { display: flex; gap: 12px; flex-wrap: wrap; align-items: end; }
      label { display: flex; flex-direction: column; gap: 6px; font-size: 12px; color: #444; }
      input, select { padding: 10px 12px; border: 1px solid #ccc; border-radius: 8px; min-width: 220px; }
      button { padding: 10px 14px; border: 0; border-radius: 10px; background: #111; color: #fff; cursor: pointer; transition: filter 120ms ease, opacity 120ms ease; }
      button.secondary { background: #666; }
      button.danger { background: #b00020; }
      button:not(:disabled):hover { filter: brightness(0.95); }
      button:disabled { opacity: 0.45; cursor: not-allowed; }
      table { border-collapse: collapse; width: 100%; margin-top: 16px; }
      th, td { border-bottom: 1px solid #eee; padding: 10px 8px; text-align: left; font-size: 13px; }
      th { color: #555; font-weight: 600; }
      #docsTbody tr { cursor: pointer; }
      #docsTbody tr:hover { background: #fafafa; }
      .muted { color: #777; font-size: 12px; margin-top: 8px; }
      .err { color: #b00020; white-space: pre-wrap; margin-top: 12px; }
      code { background: #f5f5f5; padding: 2px 6px; border-radius: 6px; }
      .card { border: 1px solid #eee; border-radius: 14px; padding: 14px; margin: 14px 0; }
      .log { background: #0b1020; color: #e6e6e6; border-radius: 12px; padding: 12px; overflow: auto; max-height: 220px; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; font-size: 12px; }
      .pill { display: inline-block; font-size: 11px; padding: 3px 8px; border-radius: 999px; background: #f0f0f0; color: #333; }
      .logout { font-size: 12px; padding: 8px 10px; border-radius: 10px; border: 1px solid #ddd; color: #111; text-decoration: none; }
      details > summary { cursor: pointer; color: #333; }
      pre { white-space: pre-wrap; margin: 8px 0 0 0; }
      .stats { display: grid; grid-template-columns: repeat(4, minmax(160px, 1fr)); gap: 12px; margin-top: 12px; }
      .stat { border: 1px solid #eee; border-radius: 14px; padding: 12px; background: #fff; }
      .stat .label { font-size: 11px; text-transform: uppercase; letter-spacing: .08em; color: #666; }
      .stat .value { font-size: 28px; font-weight: 700; margin-top: 4px; }
      .stat.queued { background: linear-gradient(135deg, #f8fafc, #fff); }
      .stat.processing { background: linear-gradient(135deg, #fff7ed, #fff); }
      .stat.indexed { background: linear-gradient(135deg, #ecfdf5, #fff); }
      .stat.failed { background: linear-gradient(135deg, #fef2f2, #fff); }
      .stat.active { border-color: #111; box-shadow: 0 0 0 2px rgba(17,17,17,0.08) inset; }
      @media (max-width: 900px) { .stats { grid-template-columns: repeat(2, minmax(160px, 1fr)); } }
    </style>
  </head>
  <body>
    <div class="row" style="justify-content: space-between; align-items: center;">
      <h1>rag-service • Diagnostics</h1>
      <a class="logout" href="/logout">Logout</a>
    </div>
    <p class="muted">
      This page helps you validate ingestion + retrieval end-to-end (chunks, entities, graph expansion).
      It’s served by the API container and is intended for trusted/local use.
    </p>

    <div class="card">
      <h2>Auth + Scope</h2>
      <div class="row">
        <label>API key (Bearer)
          <input id="apiKey" type="password" placeholder="dev-signal305-key" autocomplete="off" />
          <span class="pill" id="tenantPill">tenant: —</span>
        </label>
        <label>Workspace ID (optional)
          <input id="workspaceId" type="text" placeholder="ws-alpha" />
        </label>
        <label>Principal ID (optional)
          <input id="principalId" type="text" placeholder="user-007" />
        </label>
        <label>Poll (ms)
          <input id="pollMs" type="number" min="200" step="100" value="1000" />
        </label>
        <button id="pollToggleBtn" class="secondary">Start polling</button>
        <div>
          <span class="pill" id="pollPill">STOPPED</span>
          <div class="muted" id="pollMeta"></div>
        </div>
        <button id="refreshDocsBtn" class="secondary">Refresh documents</button>
      </div>
      <p class="muted">
        API keys are configured in <code>compose/.env</code> via <code>RAG_TENANTS_JSON</code>.
        Workspace/user scoping is controlled by <code>X-Workspace-Id</code> / <code>X-Principal-Id</code>.
      </p>
      <div id="error" class="err"></div>
    </div>

    <div class="card">
      <h2>Workers</h2>
      <div class="row">
        <div>
          <span class="pill" id="workersPill">…</span>
          <div class="muted" id="workersMeta"></div>
        </div>
        <label>Concurrency
          <input id="workersConcurrency" type="number" min="1" max="32" value="1" />
        </label>
        <button id="workersConcurrencyBtn" class="secondary">Apply</button>
        <button id="workersStartBtn" class="secondary">Start workers</button>
        <button id="workersStopBtn" class="danger">Stop workers</button>
      </div>
      <p class="muted">
        “Stop workers” pauses new ingestion jobs (existing in-flight jobs finish). Uploads still queue while paused.
      </p>
    </div>

    <div class="card">
      <h2>Reset (start testing over)</h2>
      <div class="row">
        <button id="resetTenantBtn" class="danger">Reset tenant data</button>
        <button id="resetAllBtn" class="danger">Reset ALL data</button>
        <div class="muted" id="resetMeta"></div>
      </div>
      <p class="muted">
        Reset tenant data uses the API key above to identify the tenant. Reset ALL data wipes all tenants.
        Uploaded files on disk are deleted.
        Workers are left stopped after reset.
      </p>
    </div>

    <div class="card">
      <h2>Upload (PDF / Markdown)</h2>
      <div class="row">
        <label>Scope
          <select id="uploadScope">
            <option value="tenant">tenant</option>
            <option value="workspace">workspace</option>
            <option value="user">user</option>
          </select>
        </label>
        <label>Files
          <input id="filePicker" type="file" multiple accept=".pdf,.md,.txt,application/pdf,text/markdown,text/plain" />
        </label>
        <label>Folder (Chrome)
          <input id="folderPicker" type="file" webkitdirectory directory multiple />
        </label>
        <button id="uploadBtn">Upload</button>
      </div>
      <p class="muted">
        Folder upload uses browser directory selection and uploads each file individually via <code>/v1/ingest/document</code>.
        For very large corpora (e.g. 10k files), prefer the CLI (<code>scripts/ragctl.py ingest-dir</code>).
      </p>
      <div id="uploadLog" class="log"></div>
    </div>

    <div class="card">
      <h2>Active ingestions <span class="pill">polls /v1/ingestions/active</span></h2>
      <div class="row">
        <label>Sort
          <select id="activeSortBy">
            <option value="timestamp">timestamp</option>
            <option value="progress">progress</option>
            <option value="stage">stage</option>
            <option value="filename">filename</option>
            <option value="doc_id">doc_id</option>
          </select>
        </label>
        <label>Order
          <select id="activeSortDir">
            <option value="desc">desc</option>
            <option value="asc">asc</option>
          </select>
        </label>
        <label>Page size
          <input id="activePageSize" type="number" min="1" max="200" value="25" />
        </label>
        <button id="activePrevBtn" class="secondary" disabled>Prev</button>
        <button id="activeNextBtn" class="secondary" disabled>Next</button>
        <div class="muted" id="activePageMeta"></div>
      </div>
      <table>
        <thead>
          <tr>
            <th>doc_id</th>
            <th>filename</th>
            <th>stage</th>
            <th>progress</th>
            <th>message</th>
            <th>timestamp</th>
          </tr>
        </thead>
        <tbody id="activeTbody"></tbody>
      </table>
    </div>

    <div class="card">
      <h2>Documents <span class="pill">GET /v1/documents</span></h2>
      <div class="stats" id="docStatusStats">
        <div class="stat queued" id="docStatQueued">
          <div class="label">Queued</div>
          <div class="value" id="docCountQueued">—</div>
        </div>
        <div class="stat processing" id="docStatProcessing">
          <div class="label">Processing</div>
          <div class="value" id="docCountProcessing">—</div>
        </div>
        <div class="stat indexed" id="docStatIndexed">
          <div class="label">Indexed</div>
          <div class="value" id="docCountIndexed">—</div>
        </div>
        <div class="stat failed" id="docStatFailed">
          <div class="label">Failed</div>
          <div class="value" id="docCountFailed">—</div>
        </div>
      </div>
      <div class="muted" id="docCountsMeta"></div>
      <div class="row">
        <label>Status filter
          <select id="docStatus">
            <option value="">all</option>
            <option value="queued">queued</option>
            <option value="processing">processing</option>
            <option value="indexed">indexed</option>
            <option value="failed">failed</option>
          </select>
        </label>
        <label>Sort
          <select id="docSortBy">
            <option value="created_at">created</option>
            <option value="updated_at">updated</option>
            <option value="filename">filename</option>
            <option value="status">status</option>
            <option value="stage">stage</option>
            <option value="progress">progress</option>
            <option value="chunk_count">chunks</option>
            <option value="entity_count">entities</option>
          </select>
        </label>
        <label>Order
          <select id="docSortDir">
            <option value="desc">desc</option>
            <option value="asc">asc</option>
          </select>
        </label>
        <label>Page size
          <input id="docLimit" type="number" min="1" max="500" value="100" />
        </label>
        <button id="docPrevBtn" class="secondary" disabled>Prev</button>
        <button id="docNextBtn" class="secondary" disabled>Next</button>
        <button id="refreshDocsBtn2" class="secondary">Refresh</button>
        <div class="muted" id="docPageMeta"></div>
      </div>
      <table>
        <thead>
          <tr>
            <th>doc_id</th>
            <th>filename</th>
            <th>scope</th>
            <th>status</th>
            <th>stage</th>
            <th>chunks</th>
            <th>entities</th>
            <th>updated</th>
            <th>actions</th>
          </tr>
        </thead>
        <tbody id="docsTbody"></tbody>
      </table>
      <div id="docDetail" class="muted">Tip: click a document row (or doc_id) to view details and the exact failure reason.</div>
      <div id="docEntities" class="muted"></div>
    </div>

    <div class="card">
      <h2>Retrieve <span class="pill">POST /v1/retrieve</span></h2>
      <div class="row">
        <label>Query
          <input id="query" type="text" placeholder="Ask a question…" />
        </label>
        <label>Limit
          <input id="limit" type="number" min="1" max="50" value="10" />
        </label>
        <label>Alpha
          <input id="alpha" type="number" min="0" max="1" step="0.05" value="0.5" />
        </label>
        <button id="searchBtn">Search</button>
      </div>
      <div id="retrieveMeta" class="muted"></div>
      <table>
        <thead>
          <tr>
            <th>source</th>
            <th>doc_id</th>
            <th>chunk_id</th>
            <th>rerank</th>
            <th>graph_shared</th>
            <th>title / section</th>
            <th>text</th>
          </tr>
        </thead>
        <tbody id="retrieveTbody"></tbody>
      </table>
    </div>

    <div class="card">
      <h2>Entities <span class="pill">/v1/graph/*</span></h2>
      <div class="row">
        <label>Search (name)
          <input id="entityQ" type="text" placeholder="e.g. ORBITAL-PENGUIN-742" />
        </label>
        <label>Type (optional)
          <input id="entityType" type="text" placeholder="e.g. ticker" />
        </label>
        <label>Limit
          <input id="entityLimit" type="number" min="1" max="500" value="50" />
        </label>
        <button id="entityBtn">List entities</button>
      </div>
      <table>
        <thead>
          <tr>
            <th>type</th>
            <th>name</th>
            <th>mentions</th>
            <th>entity_id</th>
          </tr>
        </thead>
        <tbody id="entitiesTbody"></tbody>
      </table>
      <div id="entityChunks" class="muted"></div>
    </div>

    <script>
      const LS_KEY = 'ragServiceAdmin.v1';

      const apiKeyEl = document.getElementById('apiKey');
      const tenantPillEl = document.getElementById('tenantPill');
      const wsEl = document.getElementById('workspaceId');
      const prEl = document.getElementById('principalId');
      const pollEl = document.getElementById('pollMs');
      const pollToggleBtn = document.getElementById('pollToggleBtn');
      const pollPillEl = document.getElementById('pollPill');
      const pollMetaEl = document.getElementById('pollMeta');
      const refreshDocsBtn = document.getElementById('refreshDocsBtn');
      const refreshDocsBtn2 = document.getElementById('refreshDocsBtn2');
      const errEl = document.getElementById('error');

      const activeTbody = document.getElementById('activeTbody');
      const activeSortByEl = document.getElementById('activeSortBy');
      const activeSortDirEl = document.getElementById('activeSortDir');
      const activePageSizeEl = document.getElementById('activePageSize');
      const activePrevBtn = document.getElementById('activePrevBtn');
      const activeNextBtn = document.getElementById('activeNextBtn');
      const activePageMetaEl = document.getElementById('activePageMeta');

      const workersPillEl = document.getElementById('workersPill');
      const workersMetaEl = document.getElementById('workersMeta');
      const workersStartBtn = document.getElementById('workersStartBtn');
      const workersStopBtn = document.getElementById('workersStopBtn');
      const workersConcurrencyEl = document.getElementById('workersConcurrency');
      const workersConcurrencyBtn = document.getElementById('workersConcurrencyBtn');

      const resetTenantBtn = document.getElementById('resetTenantBtn');
      const resetAllBtn = document.getElementById('resetAllBtn');
      const resetMetaEl = document.getElementById('resetMeta');

      const uploadScopeEl = document.getElementById('uploadScope');
      const filePickerEl = document.getElementById('filePicker');
      const folderPickerEl = document.getElementById('folderPicker');
      const uploadBtn = document.getElementById('uploadBtn');
      const uploadLogEl = document.getElementById('uploadLog');

      const docStatusEl = document.getElementById('docStatus');
      const docSortByEl = document.getElementById('docSortBy');
      const docSortDirEl = document.getElementById('docSortDir');
      const docLimitEl = document.getElementById('docLimit');
      const docPrevBtn = document.getElementById('docPrevBtn');
      const docNextBtn = document.getElementById('docNextBtn');
      const docPageMetaEl = document.getElementById('docPageMeta');
      const docsTbody = document.getElementById('docsTbody');
      const docDetailEl = document.getElementById('docDetail');
      const docEntitiesEl = document.getElementById('docEntities');
      const docCountQueuedEl = document.getElementById('docCountQueued');
      const docCountProcessingEl = document.getElementById('docCountProcessing');
      const docCountIndexedEl = document.getElementById('docCountIndexed');
      const docCountFailedEl = document.getElementById('docCountFailed');
      const docCountsMetaEl = document.getElementById('docCountsMeta');
      const docStatQueuedEl = document.getElementById('docStatQueued');
      const docStatProcessingEl = document.getElementById('docStatProcessing');
      const docStatIndexedEl = document.getElementById('docStatIndexed');
      const docStatFailedEl = document.getElementById('docStatFailed');

      const queryEl = document.getElementById('query');
      const limitEl = document.getElementById('limit');
      const alphaEl = document.getElementById('alpha');
      const searchBtn = document.getElementById('searchBtn');
      const retrieveMetaEl = document.getElementById('retrieveMeta');
      const retrieveTbody = document.getElementById('retrieveTbody');

      const entityQEl = document.getElementById('entityQ');
      const entityTypeEl = document.getElementById('entityType');
      const entityLimitEl = document.getElementById('entityLimit');
      const entityBtn = document.getElementById('entityBtn');
      const entitiesTbody = document.getElementById('entitiesTbody');
      const entityChunksEl = document.getElementById('entityChunks');

      let timer = null;
      let pollIntervalMs = null;
      let activeRows = [];
      let activePage = 1;
      let docsPage = 1;
      let docCountsLastFetchedAt = 0;
      let docCountsInFlight = false;
      let docCountsLast = null;
      let whoamiLastFetchedAt = 0;
      let whoamiInFlight = false;
      let whoamiDebounceTimer = null;

      function syncActiveSortHeight() {
        if (!activePageSizeEl) return;
        const h = Math.round(activePageSizeEl.getBoundingClientRect().height || 0);
        if (h <= 0) return;
        if (activeSortByEl) activeSortByEl.style.height = `${h}px`;
        if (activeSortDirEl) activeSortDirEl.style.height = `${h}px`;
      }

      function syncUploadScopeHeight() {
        if (!uploadScopeEl || !filePickerEl) return;
        const fileH = Math.round(filePickerEl.getBoundingClientRect().height || 0);
        const folderH = folderPickerEl ? Math.round(folderPickerEl.getBoundingClientRect().height || 0) : 0;
        const h = Math.max(fileH, folderH);
        if (h > 0) uploadScopeEl.style.height = `${h}px`;
      }

      function syncDocSortHeight() {
        if (!docLimitEl) return;
        const h = Math.round(docLimitEl.getBoundingClientRect().height || 0);
        if (h <= 0) return;
        if (docSortByEl) docSortByEl.style.height = `${h}px`;
        if (docSortDirEl) docSortDirEl.style.height = `${h}px`;
      }

      function syncDocStatusHeight() {
        if (!docStatusEl || !docLimitEl) return;
        const h = Math.round(docLimitEl.getBoundingClientRect().height || 0);
        if (h > 0) docStatusEl.style.height = `${h}px`;
      }

      function syncControlHeights() {
        syncActiveSortHeight();
        syncUploadScopeHeight();
        syncDocSortHeight();
        syncDocStatusHeight();
      }

      window.addEventListener('load', syncControlHeights);
      window.addEventListener('resize', syncControlHeights);
      setTimeout(syncControlHeights, 0);

      function loadState() {
        try {
          const raw = localStorage.getItem(LS_KEY);
          if (!raw) return;
          const s = JSON.parse(raw);
          if (s.apiKey) apiKeyEl.value = s.apiKey;
          if (s.workspaceId) wsEl.value = s.workspaceId;
          if (s.principalId) prEl.value = s.principalId;
          if (s.pollMs) pollEl.value = s.pollMs;
        } catch {}
      }

      function saveState() {
        try {
          localStorage.setItem(LS_KEY, JSON.stringify({
            apiKey: apiKeyEl.value,
            workspaceId: wsEl.value,
            principalId: prEl.value,
            pollMs: pollEl.value,
          }));
        } catch {}
      }

      function headers() {
        const h = {};
        const k = apiKeyEl.value.trim();
        if (k) h['Authorization'] = 'Bearer ' + k;
        const ws = wsEl.value.trim();
        const pr = prEl.value.trim();
        if (ws) h['X-Workspace-Id'] = ws;
        if (pr) h['X-Principal-Id'] = pr;
        return h;
      }

      function requireApiKey() {
        if (!apiKeyEl.value.trim()) throw new Error('Enter an API key first.');
      }

      function renderTenantPill({ tenantId = null, error = null } = {}) {
        if (!tenantPillEl) return;
        if (!apiKeyEl.value.trim()) {
          tenantPillEl.textContent = 'tenant: —';
          tenantPillEl.style.background = '#f0f0f0';
          tenantPillEl.style.color = '#333';
          tenantPillEl.removeAttribute('title');
          return;
        }
        if (error) {
          tenantPillEl.textContent = 'tenant: invalid';
          tenantPillEl.style.background = '#ffe8e8';
          tenantPillEl.style.color = '#b00020';
          tenantPillEl.title = String(error);
          return;
        }
        tenantPillEl.textContent = `tenant: ${tenantId || '?'}`;
        tenantPillEl.style.background = '#e9f7ef';
        tenantPillEl.style.color = '#1e7b34';
        tenantPillEl.removeAttribute('title');
      }

      async function refreshWhoAmI({ force=false } = {}) {
        const now = Date.now();
        if (!force && (now - whoamiLastFetchedAt) < 1000) return;
        if (whoamiInFlight) return;

        const k = apiKeyEl.value.trim();
        if (!k) {
          renderTenantPill();
          return;
        }

        whoamiInFlight = true;
        try {
          const resp = await fetch('/v1/whoami', { headers: Object.assign({ 'Accept': 'application/json' }, headers()) });
          const ct = (resp.headers.get('content-type') || '').toLowerCase();
          const txt = await resp.text();
          if (!resp.ok) {
            renderTenantPill({ error: txt || (resp.status + ' ' + resp.statusText) });
            return;
          }
          if (!ct.includes('application/json')) {
            renderTenantPill({ error: 'Unexpected response.' });
            return;
          }
          const data = JSON.parse(txt);
          whoamiLastFetchedAt = Date.now();
          renderTenantPill({ tenantId: data.tenant_id || null });
        } catch (e) {
          renderTenantPill({ error: e });
        } finally {
          whoamiInFlight = false;
        }
      }

      function highlightDocStatusTile() {
        const selected = docStatusEl.value.trim();
        const map = {
          queued: docStatQueuedEl,
          processing: docStatProcessingEl,
          indexed: docStatIndexedEl,
          failed: docStatFailedEl,
        };
        for (const [k, el] of Object.entries(map)) {
          if (!el) continue;
          if (selected && selected === k) el.classList.add('active');
          else el.classList.remove('active');
        }
      }

      function setDocCountValues(counts) {
        const c = counts || {};
        docCountQueuedEl.textContent = String(c.queued ?? '—');
        docCountProcessingEl.textContent = String(c.processing ?? '—');
        docCountIndexedEl.textContent = String(c.indexed ?? '—');
        docCountFailedEl.textContent = String(c.failed ?? '—');
      }

      function renderDocCounts(counts) {
        setDocCountValues(counts);
        const c = counts || {};
        const total = c.total ?? ((c.queued ?? 0) + (c.processing ?? 0) + (c.indexed ?? 0) + (c.failed ?? 0));
        const ts = new Date().toLocaleString();
        docCountsMetaEl.textContent = `Total ${total} • last ${ts}`;
        highlightDocStatusTile();
      }

      async function refreshDocCounts({ force=false } = {}) {
        const now = Date.now();
        if (!force && (now - docCountsLastFetchedAt) < 1500) return;
        if (docCountsInFlight) return;

        try {
          docCountsInFlight = true;
          const data = await fetchJson('/v1/documents/counts', { headers: headers() });
          docCountsLastFetchedAt = Date.now();
          docCountsLast = data;
          renderDocCounts(data);
        } catch (e) {
          const msg = String(e || '');
          if (msg.includes('Enter an API key first')) {
            docCountsMetaEl.textContent = 'Enter an API key to load counts.';
          } else {
            docCountsMetaEl.textContent = 'Counts unavailable.';
          }
          setDocCountValues(docCountsLast);
          highlightDocStatusTile();
        } finally {
          docCountsInFlight = false;
        }
      }

      async function fetchAdminJson(path, opts={}) {
        const headers = Object.assign({ 'Accept': 'application/json' }, opts.headers || {});
        const resp = await fetch(path, Object.assign({}, opts, { headers }));
        const ct = (resp.headers.get('content-type') || '').toLowerCase();
        const txt = await resp.text();
        if (!resp.ok) throw new Error(resp.status + ' ' + resp.statusText + '\\n' + txt);
        if (!ct.includes('application/json')) throw new Error('Unexpected response (are you logged in?)');
        return JSON.parse(txt);
      }

      async function fetchJson(path, opts={}) {
        requireApiKey();
        errEl.textContent = '';
        const resp = await fetch(path, opts);
        if (!resp.ok) {
          const txt = await resp.text();
          throw new Error(resp.status + ' ' + resp.statusText + '\\n' + txt);
        }
        return await resp.json();
      }

      function renderWorkersStatus(s) {
        const paused = !!(s && s.paused);
        const q = (s && (s.queue_depth ?? s.queueDepth)) ?? 0;
        const conc = (s && (s.concurrency ?? s.worker_concurrency)) ?? 1;
        const processing = (s && (s.processing ?? s.inflight)) ?? 0;
        const pausedSince = (s && s.paused_since) ? String(s.paused_since) : '';

        workersPillEl.textContent = paused ? 'STOPPED' : 'RUNNING';
        workersPillEl.style.background = paused ? '#ffe8e8' : '#e9f7ef';
        workersPillEl.style.color = paused ? '#b00020' : '#1e7b34';

        workersMetaEl.textContent = paused
          ? `Queue depth: ${q} • processing: ${processing} • concurrency: ${conc} • paused since ${pausedSince || 'unknown'}`
          : `Queue depth: ${q} • processing: ${processing} • concurrency: ${conc}`;

        workersStartBtn.disabled = !paused;
        workersStopBtn.disabled = paused;
        if (workersConcurrencyEl) workersConcurrencyEl.value = String(conc);
      }

      async function refreshWorkers() {
        try {
          const s = await fetchAdminJson('/admin/workers/status');
          renderWorkersStatus(s);
        } catch (e) {
          workersMetaEl.textContent = String(e);
        }
      }

      workersStartBtn.addEventListener('click', async () => {
        try {
          await fetchAdminJson('/admin/workers/start', { method: 'POST' });
          await refreshWorkers();
        } catch (e) {
          errEl.textContent = String(e);
        }
      });

      workersStopBtn.addEventListener('click', async () => {
        try {
          await fetchAdminJson('/admin/workers/stop', { method: 'POST' });
          await refreshWorkers();
        } catch (e) {
          errEl.textContent = String(e);
        }
      });

      workersConcurrencyBtn.addEventListener('click', async () => {
        try {
          const raw = parseInt(workersConcurrencyEl.value || '1', 10);
          const v = Math.max(1, Math.min(32, isFinite(raw) ? raw : 1));
          workersConcurrencyEl.value = String(v);
          workersConcurrencyBtn.disabled = true;
          await fetchAdminJson('/admin/workers/concurrency', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ concurrency: v }),
          });
          await refreshWorkers();
        } catch (e) {
          errEl.textContent = String(e);
        } finally {
          workersConcurrencyBtn.disabled = false;
        }
      });

      resetTenantBtn.addEventListener('click', async () => {
        try {
          requireApiKey();
          const confirmation = prompt('This will DELETE data for the tenant associated with the API key above.\\n\\nType RESET to confirm.');
          if (confirmation !== 'RESET') return;
          resetTenantBtn.disabled = true;
          resetAllBtn.disabled = true;
          resetMetaEl.textContent = 'Resetting tenant…';

          const res = await fetchAdminJson('/admin/reset/tenant', {
            method: 'POST',
            headers: Object.assign({ 'Content-Type': 'application/json' }, headers()),
            body: JSON.stringify({ confirm: confirmation }),
          });

          const errs = (res && res.errors) ? String(res.errors.join(' | ')) : '';
          if (res && res.ok) {
            resetMetaEl.textContent = `Done for tenant=${res.tenant_id}. Uploads=${!!res.uploads_deleted} • Postgres=${res.postgres_documents_deleted} docs • Weaviate=${res.weaviate_objects_deleted} objs • Neo4j=${res.neo4j_nodes_deleted} nodes • Redis progress=${res.redis_progress_deleted} • Redis queue removed=${res.redis_queue_removed}. Workers are stopped.`;
          } else {
            resetMetaEl.textContent = `Completed with errors. ${errs || 'Check server logs.'}`;
          }
          await refreshWorkers();
        } catch (e) {
          errEl.textContent = String(e);
          resetMetaEl.textContent = String(e);
        } finally {
          resetTenantBtn.disabled = false;
          resetAllBtn.disabled = false;
        }
      });

      resetAllBtn.addEventListener('click', async () => {
        try {
          const confirmation = prompt('This will DELETE data for ALL tenants in Weaviate, Neo4j, Redis, and Postgres.\\n\\nType RESET ALL to confirm.');
          if (confirmation !== 'RESET ALL') return;
          resetAllBtn.disabled = true;
          resetTenantBtn.disabled = true;
          resetMetaEl.textContent = 'Resetting…';

          const res = await fetchAdminJson('/admin/reset/all', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ confirm: confirmation }),
          });

          const errs = (res && res.errors) ? String(res.errors.join(' | ')) : '';
          if (res && res.ok) {
            resetMetaEl.textContent = `Done. Uploads=${!!res.uploads_cleared} • Redis=${!!res.redis_cleared} Postgres=${!!res.postgres_cleared} Weaviate=${!!res.weaviate_cleared} Neo4j=${!!res.neo4j_cleared}. Workers are stopped.`;
          } else {
            resetMetaEl.textContent = `Completed with errors. ${errs || 'Check server logs.'}`;
          }
          await refreshWorkers();
        } catch (e) {
          errEl.textContent = String(e);
          resetMetaEl.textContent = String(e);
        } finally {
          resetAllBtn.disabled = false;
          resetTenantBtn.disabled = false;
        }
      });

      function cmpStr(a, b) {
        return String(a || '').localeCompare(String(b || ''), undefined, { numeric: true, sensitivity: 'base' });
      }

      function activePageSize() {
        const raw = parseInt(activePageSizeEl.value || '25', 10);
        const v = Math.max(1, Math.min(200, isFinite(raw) ? raw : 25));
        activePageSizeEl.value = String(v);
        return v;
      }

      function activeSortKey(row) {
        const by = (activeSortByEl.value || 'timestamp');
        if (by === 'progress') return Number(row.progress ?? 0) || 0;
        if (by === 'timestamp') return Date.parse(row.timestamp || '') || 0;
        if (by === 'stage') return String(row.stage || '');
        if (by === 'filename') return String(row.filename || '');
        if (by === 'doc_id') return String(row.doc_id || '');
        return String(row.timestamp || '');
      }

      function sortActiveRows(rows) {
        const dir = (activeSortDirEl.value || 'desc').toLowerCase() === 'asc' ? 1 : -1;
        const out = rows.slice();
        out.sort((a, b) => {
          const ka = activeSortKey(a);
          const kb = activeSortKey(b);
          let c = 0;
          if (typeof ka === 'number' && typeof kb === 'number') c = ka - kb;
          else c = cmpStr(ka, kb);
          if (c === 0) c = cmpStr(a.doc_id, b.doc_id);
          return dir * c;
        });
        return out;
      }

      function renderActiveTable() {
        const pageSize = activePageSize();
        const rows = sortActiveRows(activeRows || []);
        const total = rows.length;
        const totalPages = Math.max(1, Math.ceil(total / pageSize));
        if (activePage > totalPages) activePage = totalPages;
        if (activePage < 1) activePage = 1;

        const start = (activePage - 1) * pageSize;
        const end = Math.min(total, start + pageSize);
        const slice = rows.slice(start, end);

        activeTbody.innerHTML = '';
        for (const r of slice) {
          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td><code>${r.doc_id || ''}</code></td>
            <td>${r.filename || ''}</td>
            <td>${r.stage || ''}</td>
            <td>${r.progress ?? ''}</td>
            <td>${r.message || ''}</td>
            <td class="muted">${r.timestamp || ''}</td>
          `;
          activeTbody.appendChild(tr);
        }

        activePrevBtn.disabled = activePage <= 1;
        activeNextBtn.disabled = end >= total;
        activePageMetaEl.textContent = total
          ? `Page ${activePage}/${totalPages} • ${total} total`
          : 'No active ingestions';
      }

      async function tickActive() {
        try {
          const data = await fetchJson('/v1/ingestions/active', { headers: headers() });
          activeRows = data.active || [];
          renderActiveTable();
          const ts = new Date().toLocaleString();
          if (timer && pollIntervalMs) pollMetaEl.textContent = `Every ${pollIntervalMs}ms • last ${ts}`;
          else pollMetaEl.textContent = `Last ${ts}`;
          refreshDocCounts();
        } catch (e) {
          errEl.textContent = String(e);
          if (timer && pollIntervalMs) pollMetaEl.textContent = `Every ${pollIntervalMs}ms • error`;
        }
      }

      activePrevBtn.addEventListener('click', () => {
        if (activePage > 1) activePage -= 1;
        renderActiveTable();
      });

      activeNextBtn.addEventListener('click', () => {
        activePage += 1;
        renderActiveTable();
      });

      for (const el of [activeSortByEl, activeSortDirEl, activePageSizeEl]) {
        el.addEventListener('change', () => {
          activePage = 1;
          renderActiveTable();
        });
      }

      function log(line) {
        const ts = new Date().toISOString();
        uploadLogEl.textContent += `[${ts}] ${line}\\n`;
        uploadLogEl.scrollTop = uploadLogEl.scrollHeight;
      }

      function pickFiles() {
        const out = [];
        if (filePickerEl.files) out.push(...filePickerEl.files);
        if (folderPickerEl.files) out.push(...folderPickerEl.files);
        return out;
      }

      function isAllowedFile(name) {
        const n = (name || '').toLowerCase();
        return n.endsWith('.pdf') || n.endsWith('.md') || n.endsWith('.txt');
      }

      async function uploadOne(file) {
        const scope = uploadScopeEl.value;
        const ws = wsEl.value.trim();
        const pr = prEl.value.trim();
        if ((scope === 'workspace' || scope === 'user') && !ws) throw new Error('Workspace scope requires Workspace ID.');
        if (scope === 'user' && !pr) throw new Error('User scope requires Principal ID.');

        const fd = new FormData();
        fd.append('scope', scope);

        const rel = file.webkitRelativePath ? file.webkitRelativePath : file.name;
        fd.append('file', file, rel);

        const resp = await fetch('/v1/ingest/document', { method: 'POST', headers: headers(), body: fd });
        if (!resp.ok) {
          const txt = await resp.text();
          throw new Error(resp.status + ' ' + resp.statusText + '\\n' + txt);
        }
        return await resp.json();
      }

      uploadBtn.addEventListener('click', async () => {
        try {
          requireApiKey();
          saveState();
          const files = pickFiles().filter(f => isAllowedFile(f.name || f.webkitRelativePath));
          if (!files.length) {
            log('No files selected (supported: .pdf, .md, .txt).');
            return;
          }
          log(`Uploading ${files.length} file(s)…`);
          uploadBtn.disabled = true;
          for (const f of files) {
            const label = f.webkitRelativePath ? f.webkitRelativePath : f.name;
            log(`→ ${label}`);
            const res = await uploadOne(f);
            log(`  queued doc_id=${res.doc_id}`);
          }
          log('Done.');
          await tickActive();
          await refreshDocuments();
        } catch (e) {
          errEl.textContent = String(e);
          log(`ERROR: ${String(e)}`);
        } finally {
          uploadBtn.disabled = false;
        }
      });

      function fmtDt(v) {
        if (!v) return '';
        try { return String(v).replace('T', ' ').replace('Z', ''); } catch { return String(v); }
      }

      async function refreshDocuments() {
        try {
          const status = docStatusEl.value.trim();
          const limit = Math.max(1, Math.min(500, parseInt(docLimitEl.value || '100', 10)));
          const sort = (docSortByEl.value || 'created_at').trim();
          const order = (docSortDirEl.value || 'desc').trim();
          const offset = Math.max(0, (docsPage - 1) * limit);

          let url = `/v1/documents?limit=${limit}&offset=${offset}&sort=${encodeURIComponent(sort)}&order=${encodeURIComponent(order)}`;
          if (status) url += `&status=${encodeURIComponent(status)}`;
          const docs = await fetchJson(url, { headers: headers() });

          if (docsPage > 1 && docs.length === 0) {
            docsPage -= 1;
            return await refreshDocuments();
          }

          docsTbody.innerHTML = '';
          docDetailEl.textContent = '';
          docEntitiesEl.textContent = '';
          for (const d of docs) {
            const tr = document.createElement('tr');
            const scope = d.scope || '';
            tr.innerHTML = `
              <td><a href="#" data-docdetail="${d.doc_id}"><code>${d.doc_id}</code></a></td>
              <td>${d.filename || ''}</td>
              <td>${scope}</td>
              <td>${d.status || ''}</td>
              <td>${d.stage || ''}</td>
              <td>${d.chunk_count ?? ''}</td>
              <td>${d.entity_count ?? ''}</td>
              <td class="muted">${fmtDt(d.updated_at)}</td>
              <td><button class="secondary" data-doc="${d.doc_id}">Entities</button></td>
            `;
            tr.addEventListener('click', async (ev) => {
              const t = ev.target;
              if (t && t.closest && t.closest('a[data-docdetail],button[data-doc]')) return;
              await showDocDetail(d.doc_id);
            });
            docsTbody.appendChild(tr);
          }
          for (const a of docsTbody.querySelectorAll('a[data-docdetail]')) {
            a.addEventListener('click', async (ev) => {
              ev.preventDefault();
              ev.stopPropagation();
              const docId = ev.target.getAttribute('data-docdetail') || ev.target.closest('[data-docdetail]')?.getAttribute('data-docdetail');
              if (!docId) return;
              await showDocDetail(docId);
            });
          }
          for (const btn of docsTbody.querySelectorAll('button[data-doc]')) {
            btn.addEventListener('click', async (ev) => {
              ev.stopPropagation();
              const docId = ev.target.getAttribute('data-doc');
              if (!docId) return;
              await showDocDetail(docId);
              await showDocEntities(docId);
            });
          }

          docPrevBtn.disabled = docsPage <= 1;
          docNextBtn.disabled = docs.length < limit;
          docPageMetaEl.textContent = `Page ${docsPage} • showing ${docs.length} • sort ${sort} ${order}`;
          await refreshDocCounts();
        } catch (e) {
          errEl.textContent = String(e);
        }
      }

      function escHtml(v) {
        return String(v ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#039;');
      }

      async function showDocDetail(docId) {
        try {
          const d = await fetchJson(`/v1/documents/${encodeURIComponent(docId)}`, { headers: headers() });
          const status = String(d.status || '');
          const stage = String(d.stage || '');
          const progress = (d.progress ?? '');
          const updated = fmtDt(d.updated_at);
          const chunks = (d.chunk_count ?? '');
          const entities = (d.entity_count ?? '');
          const err = d.error_message ? String(d.error_message) : '';

          let badge = '';
          if (status === 'failed') badge = '<span class="pill" style="background:#ffe8e8;color:#b00020;">FAILED</span>';
          else if (status === 'processing') badge = '<span class="pill" style="background:#fff7ed;color:#9a3412;">PROCESSING</span>';
          else if (status === 'queued') badge = '<span class="pill" style="background:#f8fafc;color:#334155;">QUEUED</span>';
          else if (status === 'indexed') badge = '<span class="pill" style="background:#e9f7ef;color:#1e7b34;">INDEXED</span>';

          docDetailEl.innerHTML = `
            <div class="row" style="align-items:center; gap:10px;">
              <div class="muted">Selected <code>${escHtml(docId)}</code></div>
              ${badge}
              <div class="muted">stage=${escHtml(stage)} progress=${escHtml(progress)} • chunks=${escHtml(chunks)} entities=${escHtml(entities)} • updated=${escHtml(updated)}</div>
            </div>
          `;
          if (err) {
            docDetailEl.innerHTML += `<details open style="margin-top:10px;"><summary>Failure reason</summary><pre>${escHtml(err)}</pre></details>`;
          }
        } catch (e) {
          errEl.textContent = String(e);
        }
      }

      async function showDocEntities(docId) {
        try {
          const data = await fetchJson(`/v1/graph/documents/${encodeURIComponent(docId)}/entities?limit=100`, { headers: headers() });
          const rows = data.entities || [];
          docEntitiesEl.innerHTML = `<div class="muted">Entities for <code>${docId}</code> (${rows.length})</div>`;
          if (!rows.length) return;
          const lines = rows.map(r => `${r.type}: ${r.name} (mentions=${r.chunk_mentions})`);
          docEntitiesEl.innerHTML += `<pre>${lines.join('\\n')}</pre>`;
        } catch (e) {
          errEl.textContent = String(e);
        }
      }

      refreshDocsBtn.addEventListener('click', async () => { saveState(); await refreshDocuments(); });
      refreshDocsBtn2.addEventListener('click', async () => { saveState(); await refreshDocuments(); });

      docPrevBtn.addEventListener('click', async () => {
        if (docsPage <= 1) return;
        docsPage -= 1;
        await refreshDocuments();
      });

      docNextBtn.addEventListener('click', async () => {
        docsPage += 1;
        await refreshDocuments();
      });

      for (const el of [docStatusEl, docSortByEl, docSortDirEl, docLimitEl]) {
        el.addEventListener('change', async () => {
          docsPage = 1;
          saveState();
          highlightDocStatusTile();
          await refreshDocuments();
        });
      }

      searchBtn.addEventListener('click', async () => {
        try {
          requireApiKey();
          saveState();
          const q = queryEl.value.trim();
          if (!q) throw new Error('Enter a query.');
          const limit = Math.max(1, Math.min(50, parseInt(limitEl.value || '10', 10)));
          const alpha = Math.max(0, Math.min(1, parseFloat(alphaEl.value || '0.5')));
          retrieveMetaEl.textContent = 'Searching…';
          retrieveTbody.innerHTML = '';
          const data = await fetchJson('/v1/retrieve', {
            method: 'POST',
            headers: Object.assign({ 'Content-Type': 'application/json' }, headers()),
            body: JSON.stringify({ query: q, limit: limit, alpha: alpha }),
          });

          const graph = data.graph || {};
          retrieveMetaEl.textContent = `graph.enabled=${graph.enabled} seed_chunk_ids=${(graph.seed_chunk_ids || []).length} expanded=${graph.expanded_count ?? 0} error=${graph.error ?? 'none'}`;

          const rows = data.results || [];
          for (const r of rows) {
            const tr = document.createElement('tr');
            const text = r.text || '';
            const preview = text.length > 220 ? text.slice(0, 220) + '…' : text;
            const title = (r.title || '') + (r.section ? ' / ' + r.section : '');
            tr.innerHTML = `
              <td>${r.source || ''}</td>
              <td><code>${r.doc_id || ''}</code></td>
              <td><code>${r.chunk_id || ''}</code></td>
              <td>${r.rerank_score != null ? Number(r.rerank_score).toFixed(3) : ''}</td>
              <td>${r.graph_shared_entities != null ? r.graph_shared_entities : ''}</td>
              <td>${title}</td>
              <td>
                <details>
                  <summary>${preview.replaceAll('<','&lt;')}</summary>
                  <pre>${text.replaceAll('<','&lt;')}</pre>
                </details>
              </td>
            `;
            retrieveTbody.appendChild(tr);
          }
        } catch (e) {
          errEl.textContent = String(e);
          retrieveMetaEl.textContent = '';
        }
      });

      entityBtn.addEventListener('click', async () => {
        try {
          requireApiKey();
          saveState();
          const q = entityQEl.value.trim();
          const t = entityTypeEl.value.trim();
          const limit = Math.max(1, Math.min(500, parseInt(entityLimitEl.value || '50', 10)));
          let url = `/v1/graph/entities?limit=${limit}`;
          if (q) url += `&q=${encodeURIComponent(q)}`;
          if (t) url += `&entity_type=${encodeURIComponent(t)}`;
          const data = await fetchJson(url, { headers: headers() });
          const rows = data.entities || [];
          entitiesTbody.innerHTML = '';
          entityChunksEl.textContent = '';
          for (const r of rows) {
            const tr = document.createElement('tr');
            tr.innerHTML = `
              <td>${r.type || ''}</td>
              <td><a href="#" data-entity="${r.entity_id}">${r.name || ''}</a></td>
              <td>${r.chunk_mentions ?? ''}</td>
              <td class="muted"><code>${r.entity_id || ''}</code></td>
            `;
            entitiesTbody.appendChild(tr);
          }
          for (const a of entitiesTbody.querySelectorAll('a[data-entity]')) {
            a.addEventListener('click', async (ev) => {
              ev.preventDefault();
              const id = ev.target.getAttribute('data-entity');
              if (!id) return;
              await showEntityChunks(id);
            });
          }
        } catch (e) {
          errEl.textContent = String(e);
        }
      });

      async function showEntityChunks(entityId) {
        try {
          const data = await fetchJson(`/v1/graph/entities/${encodeURIComponent(entityId)}/chunks?limit=25`, { headers: headers() });
          const rows = data.chunks || [];
          const lines = rows.map(r => `doc=${r.doc_id} chunk=${r.chunk_id} title=${r.title || ''} / ${r.section || ''}`);
          entityChunksEl.innerHTML = `<div class="muted">Chunks mentioning <code>${entityId}</code> (${rows.length})</div><pre>${lines.join('\\n')}</pre>`;
        } catch (e) {
          errEl.textContent = String(e);
        }
      }

      function setPolling(on) {
        if (on) {
          requireApiKey();
          saveState();
          const ms = Math.max(200, parseInt(pollEl.value || '1000', 10));
          pollIntervalMs = ms;

          if (timer) clearInterval(timer);
          timer = setInterval(tickActive, ms);

          pollToggleBtn.textContent = 'Stop polling';
          pollToggleBtn.classList.remove('secondary');
          pollToggleBtn.classList.add('danger');
          pollPillEl.textContent = 'POLLING';
          pollPillEl.style.background = '#e9f7ef';
          pollPillEl.style.color = '#1e7b34';
          pollMetaEl.textContent = `Every ${ms}ms`;
          tickActive();
          return;
        }

        if (timer) clearInterval(timer);
        timer = null;
        pollIntervalMs = null;

        pollToggleBtn.textContent = 'Start polling';
        pollToggleBtn.classList.add('secondary');
        pollToggleBtn.classList.remove('danger');
        pollPillEl.textContent = 'STOPPED';
        pollPillEl.style.background = '#f0f0f0';
        pollPillEl.style.color = '#333';
        pollMetaEl.textContent = '';
      }

      pollToggleBtn.addEventListener('click', () => {
        try {
          setPolling(!timer);
        } catch (e) {
          errEl.textContent = String(e);
          setPolling(false);
        }
      });

      pollEl.addEventListener('change', () => {
        if (!timer) return;
        try {
          setPolling(true);
        } catch (e) {
          errEl.textContent = String(e);
          setPolling(false);
        }
      });

      for (const el of [apiKeyEl, wsEl, prEl, pollEl]) {
        el.addEventListener('change', saveState);
      }
      for (const el of [apiKeyEl, wsEl, prEl]) {
        el.addEventListener('change', () => { refreshDocCounts({ force: true }); });
      }
      apiKeyEl.addEventListener('input', () => {
        if (whoamiDebounceTimer) clearTimeout(whoamiDebounceTimer);
        whoamiDebounceTimer = setTimeout(() => { refreshWhoAmI({ force: true }); }, 350);
      });
      for (const el of [apiKeyEl, wsEl, prEl]) {
        el.addEventListener('change', () => { refreshWhoAmI({ force: true }); });
      }

      loadState();
      refreshWorkers();
      highlightDocStatusTile();
      renderTenantPill();
      if (apiKeyEl.value.trim()) {
        refreshWhoAmI({ force: true });
        refreshDocCounts({ force: true });
      }
    </script>
  </body>
</html>
"""
