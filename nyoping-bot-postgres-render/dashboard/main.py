# -*- coding: utf-8 -*-
"""
Nyoping Dashboard (Render) - Discord OAuth global rate-limit safe

Fixes:
- Persists Discord OAuth cooldown in Postgres (DATABASE_URL) so service restarts won't reset cooldown.
- Prevents repeated /callback token exchange for the same code.
- Provides optional admin password login to access dashboard when Discord OAuth is blocked.

Env required:
- DISCORD_CLIENT_ID
- DISCORD_CLIENT_SECRET
- DASHBOARD_BASE_URL (e.g. https://nyoping-bot.onrender.com)
- DASHBOARD_SESSION_SECRET
- DATABASE_URL (Postgres, e.g. Neon)
Optional:
- DASHBOARD_ADMIN_PASSWORD
- DISABLE_DISCORD_OAUTH=1 (hide Discord login button)
"""

from __future__ import annotations

import os
import time
import secrets
from typing import Optional, Dict, Any

import asyncpg
import requests
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates
from pathlib import Path

DISCORD_API_BASE = "https://discord.com/api"
OAUTH_AUTHORIZE = f"{DISCORD_API_BASE}/oauth2/authorize"
OAUTH_TOKEN = f"{DISCORD_API_BASE}/oauth2/token"
OAUTH_SCOPES = "identify guilds"

CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
BASE_URL = os.getenv("DASHBOARD_BASE_URL", "").rstrip("/")
SESSION_SECRET = os.getenv("DASHBOARD_SESSION_SECRET", "") or secrets.token_urlsafe(32)
ADMIN_PASSWORD = os.getenv("DASHBOARD_ADMIN_PASSWORD", "")
DISABLE_DISCORD_OAUTH = os.getenv("DISABLE_DISCORD_OAUTH", "0") == "1"
DATABASE_URL = os.getenv("DATABASE_URL", "")

KV_COOLDOWN_UNTIL = "discord_oauth_cooldown_until"
KV_COOLDOWN_REASON = "discord_oauth_cooldown_reason"

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, https_only=BASE_URL.startswith("https://"))
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Fallback in-memory KV (only used if DATABASE_URL missing)
_mem_kv: Dict[str, str] = {}

async def _kv_get(key: str) -> Optional[str]:
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        return _mem_kv.get(key)
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT v FROM dashboard_kv WHERE k=$1", key)

async def _kv_set(key: str, value: str) -> None:
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        _mem_kv[key] = value
        return
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO dashboard_kv (k, v) VALUES ($1, $2) "
            "ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v",
            key, value
        )

async def _cooldown_remaining() -> int:
    raw = await _kv_get(KV_COOLDOWN_UNTIL)
    if not raw:
        return 0
    try:
        until = float(raw)
    except Exception:
        return 0
    return max(0, int(until - time.time()))

async def _cooldown_active() -> bool:
    return (await _cooldown_remaining()) > 0

async def _set_cooldown(seconds: float, reason: str) -> None:
    seconds = float(seconds)
    seconds = min(max(seconds, 30.0), 3600.0)  # 30s ~ 1h
    new_until = time.time() + seconds + 3.0  # small buffer
    cur_raw = await _kv_get(KV_COOLDOWN_UNTIL)
    try:
        cur_until = float(cur_raw) if cur_raw else 0.0
    except Exception:
        cur_until = 0.0
    if new_until > cur_until:
        await _kv_set(KV_COOLDOWN_UNTIL, str(new_until))
        await _kv_set(KV_COOLDOWN_REASON, reason)

def _render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    return templates.TemplateResponse(name, {"request": request, **ctx})

@app.on_event("startup")
async def _startup() -> None:
    # Create PG pool + KV table if DATABASE_URL provided
    if DATABASE_URL:
        app.state.pg_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3, command_timeout=20)
        async with app.state.pg_pool.acquire() as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS dashboard_kv ("
                "k TEXT PRIMARY KEY, "
                "v TEXT NOT NULL)"
            )
    else:
        app.state.pg_pool = None

@app.on_event("shutdown")
async def _shutdown() -> None:
    pool = getattr(app.state, "pg_pool", None)
    if pool:
        await pool.close()

@app.get("/healthz")
async def healthz() -> Dict[str, Any]:
    return {
        "ok": True,
        "cooldown": await _cooldown_active(),
        "cooldown_remaining": await _cooldown_remaining(),
    }

@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return _render(
        request,
        "index.html",
        base_url=BASE_URL,
        disable_discord_oauth=DISABLE_DISCORD_OAUTH,
        admin_enabled=bool(ADMIN_PASSWORD),
        cooldown=await _cooldown_active(),
        cooldown_seconds=await _cooldown_remaining(),
        cooldown_reason=(await _kv_get(KV_COOLDOWN_REASON)) or "",
    )

@app.post("/admin-login")
async def admin_login(request: Request, password: str = Form(...)) -> Response:
    if not ADMIN_PASSWORD or password != ADMIN_PASSWORD:
        return _render(
            request,
            "index.html",
            base_url=BASE_URL,
            disable_discord_oauth=DISABLE_DISCORD_OAUTH,
            admin_enabled=bool(ADMIN_PASSWORD),
            cooldown=await _cooldown_active(),
            cooldown_seconds=await _cooldown_remaining(),
            cooldown_reason=(await _kv_get(KV_COOLDOWN_REASON)) or "",
            admin_error="비밀번호가 올바르지 않습니다.",
        )
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

    if await _cooldown_active():
        return _render(
            request,
            "rate_limited.html",
            cooldown_seconds=await _cooldown_remaining(),
            reason=(await _kv_get(KV_COOLDOWN_REASON)) or "Discord API 글로벌 레이트리밋 상태입니다.",
        )

    if not (CLIENT_ID and CLIENT_SECRET and BASE_URL):
        return _render(request, "error.html", message="서버 환경변수가 설정되지 않았습니다. (CLIENT_ID/SECRET/BASE_URL)")

    # Throttle button spam from same browser session
    last = float(request.session.get("oauth_last_attempt", 0.0) or 0.0)
    now = time.time()
    if now - last < 5.0:
        return _render(request, "error.html", message="너무 빠르게 시도하고 있어요. 5초 후 다시 시도하세요.")
    request.session["oauth_last_attempt"] = now

    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    redirect_uri = f"{BASE_URL}/callback"

    # NOTE: Do NOT set prompt=none to avoid silent loops; let Discord show login/consent UI.
    url = (
        f"{OAUTH_AUTHORIZE}"
        f"?client_id={CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={requests.utils.quote(redirect_uri, safe='')}"
        f"&scope={requests.utils.quote(OAUTH_SCOPES)}"
        f"&state={state}"
    )
    return RedirectResponse(url=url, status_code=302)

@app.get("/callback")
async def callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
) -> Response:
    if await _cooldown_active():
        return _render(
            request,
            "rate_limited.html",
            cooldown_seconds=await _cooldown_remaining(),
            reason=(await _kv_get(KV_COOLDOWN_REASON)) or "Discord API 글로벌 레이트리밋 상태입니다.",
        )

    if error:
        return _render(request, "error.html", message=f"Discord 로그인 에러: {error}")
    if not code:
        return _render(request, "error.html", message="Discord 로그인 코드가 없습니다. 다시 시도해주세요.")

    expected = request.session.get("oauth_state")
    if expected and state and state != expected:
        return _render(request, "error.html", message="OAuth state가 일치하지 않습니다. 다시 시도해주세요.")

    # Prevent double token exchange for same code
    if request.session.get("oauth_last_code") == code:
        # If we already have an access token, go to /me
        if request.session.get("discord_access_token"):
            return RedirectResponse(url="/me", status_code=302)
        return _render(request, "error.html", message="중복 콜백 감지됨. 메인으로 돌아가 다시 시도해주세요.")
    request.session["oauth_last_code"] = code

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

    if r.status_code == 429:
        try:
            payload = r.json()
        except Exception:
            payload = {}
        retry_after = float(payload.get("retry_after", 60))
        await _set_cooldown(retry_after, "Discord OAuth 토큰 요청이 글로벌 레이트리밋에 걸렸습니다.")
        return _render(
            request,
            "rate_limited.html",
            cooldown_seconds=await _cooldown_remaining(),
            reason="Discord API 글로벌 레이트리밋에 걸렸습니다. 잠시 후 다시 시도하세요.",
        )

    if not r.ok:
        return _render(request, "error.html", message=f"token error: HTTP {r.status_code} {r.text[:500]}")

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

    try:
        r = requests.get(
            f"{DISCORD_API_BASE}/users/@me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    except requests.RequestException as e:
        return _render(request, "error.html", message=f"Discord API 요청 실패: {e}")

    if r.status_code == 429:
        try:
            payload = r.json()
        except Exception:
            payload = {}
        retry_after = float(payload.get("retry_after", 60))
        await _set_cooldown(retry_after, "Discord API 글로벌 레이트리밋 상태입니다.")
        return _render(
            request,
            "rate_limited.html",
            cooldown_seconds=await _cooldown_remaining(),
            reason="Discord API 글로벌 레이트리밋에 걸렸습니다. 잠시 후 다시 시도하세요.",
        )

    if not r.ok:
        return _render(request, "error.html", message=f"Discord API 에러: {r.status_code} {r.text[:500]}")

    user = r.json()
    return _render(request, "me.html", user=user, base_url=BASE_URL)
