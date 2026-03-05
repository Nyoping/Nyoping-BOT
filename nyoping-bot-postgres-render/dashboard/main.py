from __future__ import annotations

import os
import secrets
import time
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
ADMIN_PASSWORD = os.getenv("DASHBOARD_ADMIN_PASSWORD", "").strip()

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
    app.state.discord_blocked_until = 0.0  # unix ts, cooldown for discord API

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

# ---- Admin password fallback (no Discord OAuth) ----
@app.post("/admin-login")
async def admin_login(request: Request, password: str = Form(...)):
    if not ADMIN_PASSWORD:
        return HTMLResponse("admin password not configured", status_code=404)
    if not secrets.compare_digest(password.strip(), ADMIN_PASSWORD):
        return HTMLResponse("wrong password", status_code=403)
    request.session["admin"] = True
    return RedirectResponse("/admin", status_code=303)

@app.get("/admin", response_class=HTMLResponse)
async def admin_home(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse("/")
    pool: asyncpg.Pool = getattr(app.state, "pool", None)
    guild_ids = []
    if pool:
        rows = await pool.fetch("SELECT guild_id FROM guild_settings ORDER BY guild_id ASC")
        guild_ids = [int(r["guild_id"]) for r in rows]
    return templates.TemplateResponse("admin.html", {"request": request, "guild_ids": guild_ids})

@app.post("/admin-goto")
async def admin_goto(request: Request, guild_id: int = Form(...)):
    if not request.session.get("admin"):
        return RedirectResponse("/")
    return RedirectResponse(f"/guild/{guild_id}", status_code=303)

@app.get("/admin-logout")
async def admin_logout(request: Request):
    request.session.pop("admin", None)
    return RedirectResponse("/")

    @app.get("/callback")
    async def callback(request: Request, code: str | None = None, state: str | None = None):
        if not code:
            return RedirectResponse("/")
        if state != request.session.get("oauth_state"):
            return HTMLResponse("state mismatch", status_code=400)

        # Simple cooldown to avoid hammering Discord when rate-limited
        now = time.time()
        blocked_until = float(getattr(app.state, "discord_blocked_until", 0.0) or 0.0)
        if blocked_until and now < blocked_until:
            return HTMLResponse("token error: Discord API is rate-limited. Try again later.", status_code=429)

        data = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": f"{DASHBOARD_BASE_URL}/callback",
        }

        r = requests.post(
            TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=20,
        )

        if r.status_code != 200:
            # Discord may return 429 or a global rate limit block. Store a short cooldown to prevent spam.
            retry_after = 0
            try:
                retry_after = int(float(r.headers.get("Retry-After", "0")))
            except Exception:
                retry_after = 0
            # If no header, use a conservative cooldown window (Discord resets automatically).
            cooldown = retry_after if retry_after > 0 else 120
            app.state.discord_blocked_until = time.time() + cooldown
            return HTMLResponse(
                f"token error: {r.text}",
                status_code=400 if r.status_code != 429 else 429,
            )

        token = r.json().get("access_token")
        if not token:
            return HTMLResponse("no access_token", status_code=400)

        me = requests.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        ).json()
        guilds = requests.get(
            f"{DISCORD_API}/users/@me/guilds",
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        ).json()

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
        if not request.session.get("admin") and guild_id not in _manageable_ids(request):
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
