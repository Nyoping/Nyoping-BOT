from __future__ import annotations

import os
import secrets
import asyncpg
import requests
from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from pathlib import Path

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DASHBOARD_BASE_URL = os.getenv("DASHBOARD_BASE_URL", "").strip()
CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "").strip()
SESSION_SECRET = os.getenv("DASHBOARD_SESSION_SECRET", "").strip()

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

DISCORD_API = "https://discord.com/api"
AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
TOKEN_URL = f"{DISCORD_API}/oauth2/token"
SCOPE = "identify guilds"
MANAGE_GUILD_BIT = 0x20  # Manage Server

def _missing_env() -> list[str]:
    missing = []
    for k, v in [
        ("DATABASE_URL", DATABASE_URL),
        ("DASHBOARD_BASE_URL", DASHBOARD_BASE_URL),
        ("DISCORD_CLIENT_ID", CLIENT_ID),
        ("DISCORD_CLIENT_SECRET", CLIENT_SECRET),
        ("DASHBOARD_SESSION_SECRET", SESSION_SECRET),
    ]:
        if not v:
            missing.append(k)
    return missing

def create_app() -> FastAPI:
    app = FastAPI(title="Nyoping Dashboard")
    app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET or "dev-secret")

    @app.on_event("startup")
    async def startup():
        if DATABASE_URL:
            app.state.pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=5, command_timeout=30)

    @app.on_event("shutdown")
    async def shutdown():
        pool = getattr(app.state, "pool", None)
        if pool:
            await pool.close()

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        missing = _missing_env()
        if missing:
            return HTMLResponse("<h3>환경변수 누락</h3><pre>" + "\n".join(missing) + "</pre>", status_code=500)

        user = request.session.get("user")
        if not user:
            return templates.TemplateResponse("index.html", {"request": request})

        guilds = request.session.get("guilds", [])
        manageable = [g for g in guilds if (int(g.get("permissions", 0)) & MANAGE_GUILD_BIT) == MANAGE_GUILD_BIT]
        return templates.TemplateResponse("dashboard.html", {"request": request, "user": user, "guilds": manageable})

    @app.get("/login")
    async def login(request: Request):
        state = secrets.token_urlsafe(16)
        request.session["oauth_state"] = state
        params = {
            "client_id": CLIENT_ID,
            "redirect_uri": f"{DASHBOARD_BASE_URL}/callback",
            "response_type": "code",
            "scope": SCOPE,
            "state": state,
        }
        url = requests.Request("GET", AUTHORIZE_URL, params=params).prepare().url
        return RedirectResponse(url)

    @app.get("/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/")

    @app.get("/callback")
    async def callback(request: Request, code: str | None = None, state: str | None = None):
        if not code:
            return RedirectResponse("/")
        if state != request.session.get("oauth_state"):
            return HTMLResponse("state mismatch", status_code=400)

        data = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": f"{DASHBOARD_BASE_URL}/callback",
        }
        r = requests.post(TOKEN_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=20)
        if r.status_code != 200:
            return HTMLResponse(f"token error: {r.text}", status_code=400)
        token = r.json().get("access_token")
        if not token:
            return HTMLResponse("no access_token", status_code=400)

        me = requests.get(f"{DISCORD_API}/users/@me", headers={"Authorization": f"Bearer {token}"}, timeout=20).json()
        guilds = requests.get(f"{DISCORD_API}/users/@me/guilds", headers={"Authorization": f"Bearer {token}"}, timeout=20).json()

        request.session["user"] = me
        request.session["guilds"] = guilds
        request.session["access_token"] = token
        return RedirectResponse("/")

    def _manageable_ids(request: Request) -> set[int]:
        ids: set[int] = set()
        for g in request.session.get("guilds", []):
            try:
                if (int(g.get("permissions", 0)) & MANAGE_GUILD_BIT) == MANAGE_GUILD_BIT:
                    ids.add(int(g["id"]))
            except Exception:
                pass
        return ids

    @app.get("/guild/{guild_id}", response_class=HTMLResponse)
    async def guild_page(request: Request, guild_id: int):
        if "user" not in request.session:
            return RedirectResponse("/")
        if guild_id not in _manageable_ids(request):
            return HTMLResponse("권한이 없습니다(서버 관리 권한 필요).", status_code=403)

        pool: asyncpg.Pool = app.state.pool
        await pool.execute("INSERT INTO guild_settings (guild_id) VALUES ($1) ON CONFLICT (guild_id) DO NOTHING", guild_id)
        row = await pool.fetchrow("SELECT * FROM guild_settings WHERE guild_id=$1", guild_id)
        return templates.TemplateResponse("guild.html", {"request": request, "guild_id": guild_id, "s": dict(row)})

    @app.post("/guild/{guild_id}/settings")
    async def save_settings(
        request: Request,
        guild_id: int,
        checkin_xp: int = Form(...),
        checkin_limit_enabled: str = Form("on"),
        message_xp: int = Form(...),
        message_cooldown_sec: int = Form(...),
        voice_xp_per_min: int = Form(...),
    ):
        if "user" not in request.session:
            return RedirectResponse("/")
        if guild_id not in _manageable_ids(request):
            return HTMLResponse("권한이 없습니다.", status_code=403)

        pool: asyncpg.Pool = app.state.pool
        enabled = checkin_limit_enabled.lower() in ["on", "true", "1", "yes"]
        await pool.execute(
            "INSERT INTO guild_settings (guild_id, checkin_xp, checkin_limit_enabled, message_xp, message_cooldown_sec, voice_xp_per_min) "
            "VALUES ($1,$2,$3,$4,$5,$6) "
            "ON CONFLICT (guild_id) DO UPDATE SET "
            "checkin_xp=EXCLUDED.checkin_xp, "
            "checkin_limit_enabled=EXCLUDED.checkin_limit_enabled, "
            "message_xp=EXCLUDED.message_xp, "
            "message_cooldown_sec=EXCLUDED.message_cooldown_sec, "
            "voice_xp_per_min=EXCLUDED.voice_xp_per_min",
            guild_id, int(checkin_xp), bool(enabled), int(message_xp), int(message_cooldown_sec), int(voice_xp_per_min)
        )
        return RedirectResponse(f"/guild/{guild_id}", status_code=303)

    @app.get("/guild/{guild_id}/leaderboard", response_class=HTMLResponse)
    async def leaderboard(request: Request, guild_id: int):
        if "user" not in request.session:
            return RedirectResponse("/")
        if guild_id not in _manageable_ids(request):
            return HTMLResponse("권한이 없습니다.", status_code=403)

        pool: asyncpg.Pool = app.state.pool
        rows = await pool.fetch("SELECT user_id, xp FROM user_stats WHERE guild_id=$1 ORDER BY xp DESC LIMIT 50", guild_id)
        return templates.TemplateResponse("leaderboard.html", {"request": request, "guild_id": guild_id, "rows": rows})

    return app

app = create_app()
