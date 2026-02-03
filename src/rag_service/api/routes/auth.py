from __future__ import annotations

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from rag_service.config.settings import settings


router = APIRouter(tags=["auth"])

SESSION_KEY = "rag_admin_authenticated"


def _is_logged_in(request: Request) -> bool:
    return bool(request.session.get(SESSION_KEY))


def _login_page(*, error: bool) -> str:
    err = ""
    if error:
        err = '<div class="err">Invalid username or password.</div>'

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>rag.airialabs.com • Login</title>
    <style>
      body {{
        font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial;
        margin: 0;
        padding: 72px 18px;
        background: #0b1020;
        color: #e6e8ef;
      }}
      .card {{
        max-width: 520px;
        margin: 0 auto;
        padding: 28px;
        border-radius: 16px;
        background: rgba(255, 255, 255, 0.06);
        border: 1px solid rgba(255, 255, 255, 0.12);
        box-shadow: 0 10px 30px rgba(0,0,0,0.35);
      }}
      h1 {{ margin: 0 0 8px; font-size: 22px; }}
      p {{ margin: 0 0 18px; color: rgba(230,232,239,0.8); line-height: 1.5; }}
      label {{ display: block; font-size: 12px; color: rgba(230,232,239,0.85); margin: 14px 0 6px; }}
      input {{
        width: 100%;
        box-sizing: border-box;
        padding: 12px 12px;
        border-radius: 10px;
        border: 1px solid rgba(255,255,255,0.16);
        background: rgba(0,0,0,0.22);
        color: #e6e8ef;
        outline: none;
      }}
      input:focus {{ border-color: rgba(128, 168, 255, 0.75); }}
      button {{
        margin-top: 18px;
        width: 100%;
        padding: 12px 14px;
        border: 0;
        border-radius: 12px;
        background: #1b5cff;
        color: #fff;
        font-weight: 600;
        cursor: pointer;
      }}
      .err {{
        margin-top: 12px;
        color: #ffb4b4;
        background: rgba(255, 0, 0, 0.10);
        border: 1px solid rgba(255, 0, 0, 0.20);
        padding: 10px 12px;
        border-radius: 12px;
      }}
      .muted {{
        margin-top: 14px;
        font-size: 12px;
        color: rgba(230,232,239,0.65);
      }}
    </style>
  </head>
  <body>
    <div class="card">
      <h1>Admin login</h1>
      <p>Sign in to access rag-service admin tools for Airia Labs.</p>
      <form method="post" action="/login">
        <label for="username">Username</label>
        <input id="username" name="username" type="text" autocomplete="username" required />
        <label for="password">Password</label>
        <input id="password" name="password" type="password" autocomplete="current-password" required />
        <button type="submit">Sign in</button>
        {err}
      </form>
      <div class="muted">If you need access, contact the site administrator.</div>
    </div>
  </body>
</html>
"""


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def root_login(request: Request, error: str | None = None) -> HTMLResponse:
    if not settings.admin_auth_enabled():
        return HTMLResponse(
            content="Admin login is not configured.",
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )

    if _is_logged_in(request):
        return RedirectResponse(url="/admin/status", status_code=303)

    return HTMLResponse(content=_login_page(error=bool(error)), headers={"Cache-Control": "no-store"})


@router.head("/", include_in_schema=False)
def root_login_head(request: Request) -> Response:
    if not settings.admin_auth_enabled():
        return Response(status_code=503)

    if _is_logged_in(request):
        return RedirectResponse(url="/admin/status", status_code=303)

    return Response(status_code=200, headers={"Cache-Control": "no-store"})


@router.post("/login", include_in_schema=False)
def login(request: Request, username: str = Form(...), password: str = Form(...)) -> RedirectResponse:
    if not settings.admin_auth_enabled():
        return RedirectResponse(url="/", status_code=303)

    if username == settings.rag_admin_username and password == settings.rag_admin_password:
        request.session.clear()
        request.session[SESSION_KEY] = True
        request.session["rag_admin_username"] = username
        return RedirectResponse(url="/admin/status", status_code=303)

    return RedirectResponse(url="/?error=1", status_code=303)


@router.get("/logout", include_in_schema=False)
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)
