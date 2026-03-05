from __future__ import annotations

import os
import secrets
import time
from pathlib import Path

import asyncpg
import requests
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

# ---- Env ----
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
    missing: list[str] = []
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

    # Cooldown when Discord blocks/rate-limits token exchange.
    app.state.discord_blocked_until = 0.0

    @app.on_event("startup")
    async def startup() -> None:
        if DATABASE_URL:
            app.state.pool = await asyncpg.create_pool(
                dsn=DATABASE_URL,
                min_size=1,
                max_size=5,
                command_timeout=30,
            )

    @app.on_event("shutdown")
    async def shutdown() -> None:
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
            blocked_for = max(0, int(getattr(app.state, "discord_blocked_until", 0.0) - time.time()))
            return templates.TemplateResponse(
                "index.html",
                {
                    "request": request,
                    "admin_enabled": bool(ADMIN_PASSWORD),
                    "blocked_for": blocked_for,
                },
            )

        guilds = request.session.get("guilds", [])
        manageable = [
            g
            for g in guilds
            if (int(g.get("permissions", 0)) & MANAGE_GUILD_BIT) == MANAGE_GUILD_BIT
        ]
        return templates.TemplateResponse(
            "dashboard.html",
            {"request": request, "user": user, "guilds": manageable},
        )

    def _is_blocked() -> bool:
        return time.time() < float(getattr(app.state, "discord_blocked_until", 0.0))

    @app.get("/login")
    async def login(request: Request):
        if _is_blocked():
            wait_s = int(getattr(app.state, "discord_blocked_until", 0.0) - time.time())
            return HTMLResponse(
                f"Discord API 제한에 걸려 잠시 로그인할 수 없습니다. {wait_s}초 후 다시 시도하세요.",
                status_code=429,
            )

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
        pool: asyncpg.Pool | None = getattr(app.state, "pool", None)
        guild_ids: list[int] = []
        if pool:
            rows = await pool.fetch("SELECT guild_id FROM guild_settings ORDER BY guild_id ASC")
            guild_ids = [int(r["guild_id"]) for r in rows]
        return templates.TemplateResponse(
            "admin.html",
            {"request": request, "guild_ids": guild_ids},
        )

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

        if _is_blocked():
            wait_s = int(getattr(app.state, "discord_blocked_until", 0.0) - time.time())
            return HTMLResponse(
                f"Discord API 제한에 걸려 잠시 로그인할 수 없습니다. {wait_s}초 후 다시 시도하세요.",
                status_code=429,
            )

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

        # Global rate limit / temporary block
        if r.status_code == 429 or "exceeding global rate limits" in r.text:
            # back off for a minute
            app.state.discord_blocked_until = time.time() + 60
            return HTMLResponse(
                "Discord API 글로벌 레이트리밋에 걸렸습니다. 1분 후 다시 시도하세요.",
                status_code=429,
            )

        if r.status_code != 200:
            return HTMLResponse(f"token error: {r.text}", status_code=400)

        token = r.json().get("access_token")
        if not token:
            return HTMLResponse("no access_token", status_code=400)

        headers = {"Authorization": f"Bearer {token}"}
        me_resp = requests.get(f"{DISCORD_API}/users/@me", headers=headers, timeout=20)
        guilds_resp = requests.get(f"{DISCORD_API}/users/@me/guilds", headers=headers, timeout=20)

        if me_resp.status_code == 429 or guilds_resp.status_code == 429:
            app.state.discord_blocked_until = time.time() + 60
            return HTMLResponse(
                "Discord API 레이트리밋에 걸렸습니다. 1분 후 다시 시도하세요.",
                status_code=429,
            )

        me = me_resp.json()
        guilds = guilds_resp.json()

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
            # Allow admin fallback
            if not request.session.get("admin"):
                return RedirectResponse("/")

        if "user" in request.session and guild_id not in _manageable_ids(request):
            return HTMLResponse("권한이 없습니다(서버 관리 권한 필요).", status_code=403)

        pool: asyncpg.Pool = app.state.pool
        await pool.execute(
            "INSERT INTO guild_settings (guild_id) VALUES ($1) ON CONFLICT (guild_id) DO NOTHING",
            guild_id,
        )
        row = await pool.fetchrow("SELECT * FROM guild_settings WHERE guild_id=$1", guild_id)
        return templates.TemplateResponse(
            "guild.html",
            {"request": request, "guild_id": guild_id, "s": dict(row)},
        )

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
        if "user" not in request.session and not request.session.get("admin"):
            return RedirectResponse("/")
        if "user" in request.session and guild_id not in _manageable_ids(request):
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
            guild_id,
            int(checkin_xp),
            bool(enabled),
            int(message_xp),
            int(message_cooldown_sec),
            int(voice_xp_per_min),
        )
        return RedirectResponse(f"/guild/{guild_id}", status_code=303)

    @app.get("/guild/{guild_id}/leaderboard", response_class=HTMLResponse)
    async def leaderboard(request: Request, guild_id: int):
        if "user" not in request.session and not request.session.get("admin"):
            return RedirectResponse("/")
        if "user" in request.session and guild_id not in _manageable_ids(request):
            return HTMLResponse("권한이 없습니다.", status_code=403)

        pool: asyncpg.Pool = app.state.pool
        rows = await pool.fetch(
            "SELECT user_id, xp FROM user_stats WHERE guild_id=$1 ORDER BY xp DESC LIMIT 50",
            guild_id,
        )
        return templates.TemplateResponse(
            "leaderboard.html",
            {"request": request, "guild_id": guild_id, "rows": rows},
        )

    return app


app = create_app()
