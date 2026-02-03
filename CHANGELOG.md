# Changelog

## 2026-02-03 – Admin UX, worker concurrency, and API docs

- **Admin login:** Added session-protected admin UI (`/` login + `/admin/status`) with per-tenant reset controls and worker start/stop.
- **Worker concurrency:** Enabled processing multiple documents concurrently within a single worker (configurable via the admin UI).
- **Airia gateway hardening:** Improved OpenAI-compatible client behavior for `gateway.airia.ai` (retries/backoff, better error bodies, and safer request shaping).
- **Developer docs:** Added `API.md` and served it over HTTP (`/api.md` raw + `/api` rendered).
- **Bulk ingest CLI:** Improved `scripts/ragctl.py ingest-dir` startup time by uploading while scanning (optional `--prescan` for exact totals/ETA).

