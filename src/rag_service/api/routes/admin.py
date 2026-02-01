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
    <title>rag-service • Ingestion Status</title>
    <style>
      body { font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, "Apple Color Emoji", "Segoe UI Emoji"; margin: 24px; }
      .row { display: flex; gap: 12px; flex-wrap: wrap; align-items: end; }
      label { display: flex; flex-direction: column; gap: 6px; font-size: 12px; color: #444; }
      input { padding: 10px 12px; border: 1px solid #ccc; border-radius: 8px; min-width: 220px; }
      button { padding: 10px 14px; border: 0; border-radius: 10px; background: #111; color: #fff; cursor: pointer; }
      button.secondary { background: #666; }
      table { border-collapse: collapse; width: 100%; margin-top: 16px; }
      th, td { border-bottom: 1px solid #eee; padding: 10px 8px; text-align: left; font-size: 13px; }
      th { color: #555; font-weight: 600; }
      .muted { color: #777; font-size: 12px; margin-top: 8px; }
      .err { color: #b00020; white-space: pre-wrap; margin-top: 12px; }
      code { background: #f5f5f5; padding: 2px 6px; border-radius: 6px; }
    </style>
  </head>
  <body>
    <h1>rag-service • Ingestion Status</h1>
    <p class="muted">Polls <code>/v1/ingestions/active</code>. Enter an API key (tenant) and optional scope headers.</p>

    <div class="row">
      <label>API key (Bearer)
        <input id="apiKey" type="password" placeholder="dev-signal305-key" />
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
      <button id="startBtn">Start</button>
      <button id="stopBtn" class="secondary" disabled>Stop</button>
    </div>

    <div id="error" class="err"></div>

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
      <tbody id="tbody"></tbody>
    </table>

    <script>
      const apiKeyEl = document.getElementById('apiKey');
      const wsEl = document.getElementById('workspaceId');
      const prEl = document.getElementById('principalId');
      const pollEl = document.getElementById('pollMs');
      const startBtn = document.getElementById('startBtn');
      const stopBtn = document.getElementById('stopBtn');
      const tbody = document.getElementById('tbody');
      const errEl = document.getElementById('error');

      let timer = null;

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

      function render(rows) {
        tbody.innerHTML = '';
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
          tbody.appendChild(tr);
        }
      }

      async function tick() {
        errEl.textContent = '';
        try {
          const resp = await fetch('/v1/ingestions/active', { headers: headers() });
          if (!resp.ok) {
            const txt = await resp.text();
            throw new Error(resp.status + ' ' + resp.statusText + '\\n' + txt);
          }
          const data = await resp.json();
          render(data.active || []);
        } catch (e) {
          errEl.textContent = String(e);
        }
      }

      startBtn.addEventListener('click', async () => {
        if (!apiKeyEl.value.trim()) {
          errEl.textContent = 'Enter an API key first.';
          return;
        }
        startBtn.disabled = true;
        stopBtn.disabled = false;
        await tick();
        const ms = Math.max(200, parseInt(pollEl.value || '1000', 10));
        timer = setInterval(tick, ms);
      });

      stopBtn.addEventListener('click', () => {
        if (timer) clearInterval(timer);
        timer = null;
        startBtn.disabled = false;
        stopBtn.disabled = true;
      });
    </script>
  </body>
</html>
"""

