# -*- coding: utf-8 -*-
"""
Nyoping Dashboard - OAuth backoff patch
- Prevents hammering Discord OAuth token endpoint when global rate-limited (429).
- Respects retry_after and applies server-side cooldown so refreshes won't extend the block.
- Optional admin password login (DASHBOARD_ADMIN_PASSWORD) to bypass Discord OAuth temporarily.
"""
from __future__ import annotations

import os
import time
import secrets
from typing import Optional, Dict, Any

import requests
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates

from pathlib import Path

# --- Config ---
DISCORD_API_BASE = "https://discord.com/api"
OAUTH_AUTHORIZE = f"{DISCORD_API_BASE}/oauth2/authorize"
OAUTH_TOKEN = f"{DISCORD_API_BASE}/oauth2/token"
OAUTH_SCOPES = "identify guilds"

CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
BASE_URL = os.getenv("DASHBOARD_BASE_URL", "").rstrip("/")
SESSION_SECRET = os.getenv("DASHBOARD_SESSION_SECRET", "") or secrets.token_urlsafe(32)
ADMIN_PASSWORD = os.getenv("DASHBOARD_ADMIN_PASSWORD", "")
# If set to 1, disables Discord OAuth button entirely (use admin login only)
DISABLE_DISCORD_OAUTH = os.getenv("DISABLE_DISCORD_OAUTH", "0") == "1"

# --- App ---
app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, https_only=BASE_URL.startswith("https://"))

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# --- Global rate limit guard (process-local) ---
# NOTE: Discord's global RL can be shared by IP/app. This prevents our service from extending it by repeated calls.
_DISCORD_OAUTH_COOLDOWN_UNTIL: float = 0.0
_DISCORD_OAUTH_LAST_REASON: str = ""

def _cooldown_active() -> bool:
    return time.time() < _DISCORD_OAUTH_COOLDOWN_UNTIL

def _set_cooldown(seconds: float, reason: str = "") -> None:
    global _DISCORD_OAUTH_COOLDOWN_UNTIL, _DISCORD_OAUTH_LAST_REASON
    _DISCORD_OAUTH_COOLDOWN_UNTIL = max(_DISCORD_OAUTH_COOLDOWN_UNTIL, time.time() + max(1.0, seconds))
    _DISCORD_OAUTH_LAST_REASON = reason

def _cooldown_remaining() -> int:
    return max(0, int(_DISCORD_OAUTH_COOLDOWN_UNTIL - time.time()))

def _render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    return templates.TemplateResponse(name, {"request": request, **ctx})

@app.get("/healthz")
async def healthz() -> Dict[str, Any]:
    return {"ok": True, "cooldown": _cooldown_active(), "cooldown_remaining": _cooldown_remaining()}

@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return _render(
        request,
        "index.html",
        base_url=BASE_URL,
        disable_discord_oauth=DISABLE_DISCORD_OAUTH,
        admin_enabled=bool(ADMIN_PASSWORD),
        cooldown=_cooldown_active(),
        cooldown_seconds=_cooldown_remaining(),
        cooldown_reason=_DISCORD_OAUTH_LAST_REASON,
    )

@app.post("/admin-login")
async def admin_login(request: Request, password: str = Form(...)) -> Response:
    if not ADMIN_PASSWORD or password != ADMIN_PASSWORD:
        return _render(request, "index.html", base_url=BASE_URL, disable_discord_oauth=DISABLE_DISCORD_OAUTH,
                       admin_enabled=bool(ADMIN_PASSWORD), cooldown=_cooldown_active(),
                       cooldown_seconds=_cooldown_remaining(), cooldown_reason=_DISCORD_OAUTH_LAST_REASON,
                       admin_error="비밀번호가 올바르지 않습니다.")
    request.session["admin"] = True
    return RedirectResponse(url="/admin", status_code=302)

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request) -> HTMLResponse:
    if not request.session.get("admin"):
        return RedirectResponse(url="/", status_code=302)
    return _render(request, "admin.html", base_url=BASE_URL)

@app.get("/login")
async def discord_login(request: Request) -> Response:
    if DISABLE_DISCORD_OAUTH:
        return RedirectResponse(url="/", status_code=302)

    if _cooldown_active():
        # Don't hit Discord again.
        return _render(
            request,
            "rate_limited.html",
            cooldown_seconds=_cooldown_remaining(),
            reason=_DISCORD_OAUTH_LAST_REASON or "Discord API 글로벌 레이트리밋 상태입니다.",
        )

    if not (CLIENT_ID and CLIENT_SECRET and BASE_URL):
        return _render(request, "error.html", message="서버 환경변수가 설정되지 않았습니다. (CLIENT_ID/SECRET/BASE_URL)")

    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    redirect_uri = f"{BASE_URL}/callback"
    url = (
        f"{OAUTH_AUTHORIZE}"
        f"?client_id={CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={requests.utils.quote(redirect_uri, safe='')}"
        f"&scope={requests.utils.quote(OAUTH_SCOPES)}"
        f"&state={state}"
        f"&prompt=none"
    )
    return RedirectResponse(url=url, status_code=302)

@app.get("/callback")
async def callback(request: Request, code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None) -> Response:
    # If we are cooldowned, do NOT attempt token exchange again.
    if _cooldown_active():
        return _render(
            request,
            "rate_limited.html",
            cooldown_seconds=_cooldown_remaining(),
            reason=_DISCORD_OAUTH_LAST_REASON or "Discord API 글로벌 레이트리밋 상태입니다.",
        )

    if error:
        return _render(request, "error.html", message=f"Discord 로그인 에러: {error}")

    if not code:
        return _render(request, "error.html", message="Discord 로그인 코드가 없습니다. 다시 시도해주세요.")

    expected = request.session.get("oauth_state")
    if expected and state and state != expected:
        return _render(request, "error.html", message="OAuth state가 일치하지 않습니다. 다시 시도해주세요.")

    redirect_uri = f"{BASE_URL}/callback"
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    try:
        r = requests.post(OAUTH_TOKEN, data=data, headers=headers, timeout=15)
    except requests.RequestException as e:
        return _render(request, "error.html", message=f"토큰 요청 실패: {e}")

    # Handle rate limit explicitly
    if r.status_code == 429:
        try:
            payload = r.json()
        except Exception:
            payload = {}
        retry_after = float(payload.get("retry_after", 60))
        # Add buffer; also cap to a sane window to avoid absurd values
        retry_after = min(max(retry_after, 30.0), 3600.0)
        _set_cooldown(retry_after, reason="Discord OAuth 토큰 요청이 글로벌 레이트리밋에 걸렸습니다.")
        return _render(
            request,
            "rate_limited.html",
            cooldown_seconds=_cooldown_remaining(),
            reason="Discord API 글로벌 레이트리밋에 걸렸습니다. 잠시 후 다시 시도하세요.",
        )

    if not r.ok:
        # Some other error; don't loop.
        msg = ""
        try:
            msg = r.text[:500]
        except Exception:
            msg = "<no body>"
        return _render(request, "error.html", message=f"token error: HTTP {r.status_code} {msg}")

    token = r.json().get("access_token")
    if not token:
        return _render(request, "error.html", message="토큰 응답에 access_token이 없습니다.")

    request.session["discord_access_token"] = token
    return RedirectResponse(url="/me", status_code=302)

@app.get("/me", response_class=HTMLResponse)
async def me(request: Request) -> Response:
    token = request.session.get("discord_access_token")
    if not token:
        return RedirectResponse(url="/", status_code=302)

    # Minimal profile call (should be within limits after cooldown handling)
    try:
        r = requests.get(f"{DISCORD_API_BASE}/users/@me", headers={"Authorization": f"Bearer {token}"}, timeout=10)
    except requests.RequestException as e:
        return _render(request, "error.html", message=f"Discord API 요청 실패: {e}")

    if r.status_code == 429:
        try:
            payload = r.json()
        except Exception:
            payload = {}
        retry_after = float(payload.get("retry_after", 60))
        retry_after = min(max(retry_after, 30.0), 3600.0)
        _set_cooldown(retry_after, reason="Discord API 글로벌 레이트리밋 상태입니다.")
        return _render(request, "rate_limited.html", cooldown_seconds=_cooldown_remaining(),
                       reason="Discord API 글로벌 레이트리밋에 걸렸습니다. 잠시 후 다시 시도하세요.")

    if not r.ok:
        return _render(request, "error.html", message=f"Discord API 에러: {r.status_code} {r.text[:500]}")

    user = r.json()
    return _render(request, "me.html", user=user, base_url=BASE_URL)
