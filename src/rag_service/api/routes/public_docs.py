from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse, Response


router = APIRouter(tags=["docs"])


def _find_api_md() -> Path | None:
    env_path = (os.getenv("RAG_API_MD_PATH") or "").strip()
    if env_path:
        p = Path(env_path).expanduser()
        if p.is_file():
            return p

    # Common Docker location (see Dockerfile WORKDIR).
    for p in (Path("/app/API.md"), Path.cwd() / "API.md"):
        if p.is_file():
            return p

    # Dev checkout fallback: walk upward from this file looking for API.md.
    base = Path(__file__).resolve().parent
    for parent in (base, *base.parents):
        p = parent / "API.md"
        if p.is_file():
            return p

    return None


@router.get("/api.md", include_in_schema=False)
def api_md():
    path = _find_api_md()
    if not path:
        return Response("API.md not found\n", status_code=404, media_type="text/plain; charset=utf-8")
    return FileResponse(
        str(path),
        media_type="text/markdown; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'inline; filename="API.md"',
        },
    )


@router.head("/api.md", include_in_schema=False)
def api_md_head():
    path = _find_api_md()
    if not path:
        return Response(status_code=404, media_type="text/plain; charset=utf-8")
    return Response(
        status_code=200,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'inline; filename="API.md"',
        },
    )


def _api_html_page() -> str:
    return """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>rag-service • API</title>
    <style>
      :root { color-scheme: light; }
      body { font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial; margin: 0; background: #fff; color: #111; }
      header { position: sticky; top: 0; background: rgba(255,255,255,0.9); backdrop-filter: blur(8px); border-bottom: 1px solid #eee; }
      .bar { max-width: 980px; margin: 0 auto; padding: 14px 18px; display: flex; gap: 12px; align-items: center; justify-content: space-between; }
      .title { font-weight: 700; }
      .links { display: flex; gap: 10px; font-size: 13px; }
      .links a { color: #1b5cff; text-decoration: none; }
      .links a:hover { text-decoration: underline; }
      main { max-width: 980px; margin: 0 auto; padding: 20px 18px 56px; }
      .muted { color: #666; font-size: 13px; }
      #content h1, #content h2, #content h3 { margin: 22px 0 10px; }
      #content h1 { font-size: 28px; }
      #content h2 { font-size: 18px; border-top: 1px solid #eee; padding-top: 18px; }
      #content h3 { font-size: 15px; }
      #content p { line-height: 1.55; }
      #content ul, #content ol { padding-left: 20px; }
      #content li { margin: 6px 0; }
      #content code { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; font-size: 12.5px; }
      #content :not(pre) > code { background: #f5f5f5; padding: 2px 6px; border-radius: 6px; }
      #content pre { background: #0b1020; color: #e6e6e6; padding: 12px 14px; border-radius: 12px; overflow: auto; }
      #content pre code { color: inherit; }
      #content hr { border: 0; border-top: 1px solid #eee; margin: 22px 0; }
      #fallback { background: #0b1020; color: #e6e6e6; border-radius: 12px; padding: 12px 14px; overflow: auto; }
    </style>
  </head>
  <body>
    <header>
      <div class="bar">
        <div class="title">rag-service API</div>
        <nav class="links">
          <a href="/api.md">Raw markdown</a>
          <a href="/docs">OpenAPI</a>
          <a href="/admin/status">Admin</a>
        </nav>
      </div>
    </header>
    <main>
      <div id="status" class="muted">Loading…</div>
      <article id="content"></article>
      <pre id="fallback" hidden></pre>
    </main>
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <script>
      (async () => {
        const status = document.getElementById('status');
        const content = document.getElementById('content');
        const fallback = document.getElementById('fallback');
        try {
          const resp = await fetch('/api.md', { cache: 'no-store' });
          const md = await resp.text();
          if (window.marked && typeof window.marked.parse === 'function') {
            content.innerHTML = window.marked.parse(md, { mangle: false, headerIds: true });
            status.remove();
          } else {
            fallback.hidden = false;
            fallback.textContent = md;
            status.textContent = 'Markdown renderer failed to load; showing raw text.';
          }
        } catch (e) {
          fallback.hidden = false;
          fallback.textContent = 'Failed to load /api.md: ' + (e && e.message ? e.message : String(e));
          status.textContent = 'Error loading API docs.';
        }
      })();
    </script>
  </body>
</html>
"""


@router.get("/api", include_in_schema=False)
def api_html():
    return HTMLResponse(
        content=_api_html_page(),
        headers={"Cache-Control": "no-store"},
    )


@router.head("/api", include_in_schema=False)
def api_html_head():
    return Response(
        status_code=200,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )
