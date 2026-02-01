from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse


router = APIRouter(tags=["admin"])


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
      button { padding: 10px 14px; border: 0; border-radius: 10px; background: #111; color: #fff; cursor: pointer; }
      button.secondary { background: #666; }
      table { border-collapse: collapse; width: 100%; margin-top: 16px; }
      th, td { border-bottom: 1px solid #eee; padding: 10px 8px; text-align: left; font-size: 13px; }
      th { color: #555; font-weight: 600; }
      .muted { color: #777; font-size: 12px; margin-top: 8px; }
      .err { color: #b00020; white-space: pre-wrap; margin-top: 12px; }
      code { background: #f5f5f5; padding: 2px 6px; border-radius: 6px; }
      .card { border: 1px solid #eee; border-radius: 14px; padding: 14px; margin: 14px 0; }
      .log { background: #0b1020; color: #e6e6e6; border-radius: 12px; padding: 12px; overflow: auto; max-height: 220px; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; font-size: 12px; }
      .pill { display: inline-block; font-size: 11px; padding: 3px 8px; border-radius: 999px; background: #f0f0f0; color: #333; }
      details > summary { cursor: pointer; color: #333; }
      pre { white-space: pre-wrap; margin: 8px 0 0 0; }
    </style>
  </head>
  <body>
    <h1>rag-service • Diagnostics</h1>
    <p class="muted">
      This page helps you validate ingestion + retrieval end-to-end (chunks, entities, graph expansion).
      It’s served by the API container and is intended for trusted/local use.
    </p>

    <div class="card">
      <h2>Auth + Scope</h2>
      <div class="row">
        <label>API key (Bearer)
          <input id="apiKey" type="password" placeholder="dev-signal305-key" autocomplete="off" />
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
        <button id="startPollBtn">Start polling</button>
        <button id="stopPollBtn" class="secondary" disabled>Stop</button>
        <button id="refreshDocsBtn" class="secondary">Refresh documents</button>
      </div>
      <p class="muted">
        API keys are configured in <code>compose/.env</code> via <code>RAG_TENANTS_JSON</code>.
        Workspace/user scoping is controlled by <code>X-Workspace-Id</code> / <code>X-Principal-Id</code>.
      </p>
      <div id="error" class="err"></div>
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
        <label>Limit
          <input id="docLimit" type="number" min="1" max="500" value="100" />
        </label>
        <button id="refreshDocsBtn2" class="secondary">Refresh</button>
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
        <button id="entityBtn" class="secondary">List entities</button>
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
      const wsEl = document.getElementById('workspaceId');
      const prEl = document.getElementById('principalId');
      const pollEl = document.getElementById('pollMs');
      const startPollBtn = document.getElementById('startPollBtn');
      const stopPollBtn = document.getElementById('stopPollBtn');
      const refreshDocsBtn = document.getElementById('refreshDocsBtn');
      const refreshDocsBtn2 = document.getElementById('refreshDocsBtn2');
      const errEl = document.getElementById('error');

      const activeTbody = document.getElementById('activeTbody');

      const uploadScopeEl = document.getElementById('uploadScope');
      const filePickerEl = document.getElementById('filePicker');
      const folderPickerEl = document.getElementById('folderPicker');
      const uploadBtn = document.getElementById('uploadBtn');
      const uploadLogEl = document.getElementById('uploadLog');

      const docStatusEl = document.getElementById('docStatus');
      const docLimitEl = document.getElementById('docLimit');
      const docsTbody = document.getElementById('docsTbody');
      const docEntitiesEl = document.getElementById('docEntities');

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

      function renderActive(rows) {
        activeTbody.innerHTML = '';
        for (const r of rows) {
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
      }

      async function tickActive() {
        try {
          const data = await fetchJson('/v1/ingestions/active', { headers: headers() });
          renderActive(data.active || []);
        } catch (e) {
          errEl.textContent = String(e);
        }
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
          let url = `/v1/documents?limit=${limit}`;
          if (status) url += `&status=${encodeURIComponent(status)}`;
          const docs = await fetchJson(url, { headers: headers() });
          docsTbody.innerHTML = '';
          docEntitiesEl.textContent = '';
          for (const d of docs) {
            const tr = document.createElement('tr');
            const scope = d.scope || '';
            tr.innerHTML = `
              <td><code>${d.doc_id}</code></td>
              <td>${d.filename || ''}</td>
              <td>${scope}</td>
              <td>${d.status || ''}</td>
              <td>${d.stage || ''}</td>
              <td>${d.chunk_count ?? ''}</td>
              <td>${d.entity_count ?? ''}</td>
              <td class="muted">${fmtDt(d.updated_at)}</td>
              <td><button class="secondary" data-doc="${d.doc_id}">Entities</button></td>
            `;
            docsTbody.appendChild(tr);
          }
          for (const btn of docsTbody.querySelectorAll('button[data-doc]')) {
            btn.addEventListener('click', async (ev) => {
              const docId = ev.target.getAttribute('data-doc');
              if (!docId) return;
              await showDocEntities(docId);
            });
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

      startPollBtn.addEventListener('click', async () => {
        try {
          requireApiKey();
          saveState();
          startPollBtn.disabled = true;
          stopPollBtn.disabled = false;
          await tickActive();
          const ms = Math.max(200, parseInt(pollEl.value || '1000', 10));
          timer = setInterval(tickActive, ms);
        } catch (e) {
          errEl.textContent = String(e);
        }
      });

      stopPollBtn.addEventListener('click', () => {
        if (timer) clearInterval(timer);
        timer = null;
        startPollBtn.disabled = false;
        stopPollBtn.disabled = true;
      });

      for (const el of [apiKeyEl, wsEl, prEl, pollEl]) {
        el.addEventListener('change', saveState);
      }

      loadState();
    </script>
  </body>
</html>
"""
