# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import re
import secrets
from typing import Optional, Any, Dict, List

import asyncpg
from fastapi import FastAPI, Request, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates
from pathlib import Path

BASE_URL = os.getenv("DASHBOARD_BASE_URL", "").rstrip("/")
SESSION_SECRET = os.getenv("DASHBOARD_SESSION_SECRET", "") or secrets.token_urlsafe(32)
ADMIN_PASSWORD = os.getenv("DASHBOARD_ADMIN_PASSWORD", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, https_only=BASE_URL.startswith("https://"))
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

def _is_admin(request: Request) -> bool:
    return bool(request.session.get("admin"))

def _parse_id(value: str) -> Optional[int]:
    if not value:
        return None
    m = re.search(r"(\d{10,25})", value)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None

@app.on_event("startup")
async def _startup():
    if not DATABASE_URL:
        app.state.pg_pool = None
        return
    app.state.pg_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=4, command_timeout=30)
    # Ensure cache tables exist (created by bot too, but keep safe).
    async with app.state.pg_pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS guild_settings (
          guild_id BIGINT PRIMARY KEY,
          checkin_xp INTEGER NOT NULL DEFAULT 50,
          checkin_limit_enabled BOOLEAN NOT NULL DEFAULT TRUE,
          message_xp INTEGER NOT NULL DEFAULT 5,
          message_cooldown_sec INTEGER NOT NULL DEFAULT 60,
          voice_xp_per_min INTEGER NOT NULL DEFAULT 2
        );

        CREATE TABLE IF NOT EXISTS user_stats (
          guild_id BIGINT NOT NULL,
          user_id BIGINT NOT NULL,
          xp BIGINT NOT NULL DEFAULT 0,
          last_message_at TIMESTAMPTZ NULL,
          PRIMARY KEY (guild_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS checkins (
          guild_id BIGINT NOT NULL,
          user_id BIGINT NOT NULL,
          ymd TEXT NOT NULL,
          PRIMARY KEY (guild_id, user_id, ymd)
        );

        CREATE TABLE IF NOT EXISTS level_roles (
          guild_id BIGINT NOT NULL,
          level INTEGER NOT NULL,
          add_role_id BIGINT NOT NULL,
          remove_role_id BIGINT NULL,
          PRIMARY KEY (guild_id, level)
        );

        CREATE TABLE IF NOT EXISTS guild_roles_cache (
          guild_id BIGINT NOT NULL,
          role_id BIGINT NOT NULL,
          role_name TEXT NOT NULL,
          position INTEGER NOT NULL DEFAULT 0,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          PRIMARY KEY (guild_id, role_id)
        );

        CREATE TABLE IF NOT EXISTS guild_members_cache (
          guild_id BIGINT NOT NULL,
          user_id BIGINT NOT NULL,
          username TEXT NULL,
          discriminator TEXT NULL,
          global_name TEXT NULL,
          nick TEXT NULL,
          display_name TEXT NULL,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          PRIMARY KEY (guild_id, user_id)
        );

        CREATE INDEX IF NOT EXISTS idx_guild_members_cache_lookup
          ON guild_members_cache (guild_id, user_id);

        CREATE INDEX IF NOT EXISTS idx_guild_members_cache_search
          ON guild_members_cache (guild_id, display_name);
        """)

@app.on_event("shutdown")
async def _shutdown():
    pool = getattr(app.state, "pg_pool", None)
    if pool:
        await pool.close()

def _render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    return templates.TemplateResponse(name, {"request": request, **ctx})

# ---- Pages ----
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return _render(
        request,
        "index.html",
        base_url=BASE_URL,
        admin_enabled=bool(ADMIN_PASSWORD),
        disable_discord_oauth=True,
    )

@app.post("/admin-login")
async def admin_login(request: Request, password: str = Form(...)):
    if not ADMIN_PASSWORD or password != ADMIN_PASSWORD:
        return _render(
            request,
            "index.html",
            base_url=BASE_URL,
            admin_enabled=bool(ADMIN_PASSWORD),
            disable_discord_oauth=True,
            admin_error="비밀번호가 올바르지 않습니다.",
        )
    request.session["admin"] = True
    return RedirectResponse(url="/admin", status_code=302)

# ---- DB helpers (uses bot tables) ----
async def _db_fetch_settings(conn: asyncpg.Connection, gid: int) -> Dict[str, Any]:
    row = await conn.fetchrow("SELECT * FROM guild_settings WHERE guild_id=$1", gid)
    if not row:
        # Insert defaults if missing
        await conn.execute("INSERT INTO guild_settings (guild_id) VALUES ($1) ON CONFLICT (guild_id) DO NOTHING", gid)
        row = await conn.fetchrow("SELECT * FROM guild_settings WHERE guild_id=$1", gid)
    return dict(row) if row else {}

async def _db_fetch_rules(conn: asyncpg.Connection, gid: int) -> List[Dict[str, Any]]:
    rows = await conn.fetch("SELECT level, add_role_id, remove_role_id FROM level_roles WHERE guild_id=$1 ORDER BY level ASC", gid)
    return [dict(r) for r in rows]

async def _db_top10(conn: asyncpg.Connection, gid: int) -> List[Dict[str, Any]]:
    rows = await conn.fetch("""
        SELECT us.user_id, us.xp,
               gm.display_name, gm.username, gm.discriminator
        FROM user_stats us
        LEFT JOIN guild_members_cache gm
          ON gm.guild_id = us.guild_id AND gm.user_id = us.user_id
        WHERE us.guild_id=$1
        ORDER BY us.xp DESC
        LIMIT 10
    """, gid)
    out = []
    for r in rows:
        xp = int(r["xp"])
        name = r["display_name"] or (f"{r['username']}#{r['discriminator']}" if r["username"] else "-")
        out.append({"user_id": int(r["user_id"]), "xp": xp, "level": xp // 100, "name": name})
    return out

@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request, guild_id: Optional[int] = None, msg: str = ""):
    if not _is_admin(request):
        return RedirectResponse(url="/", status_code=302)

    ctx = {"settings": None, "rules": [], "top10": [], "error": "", "msg": msg, "guild_id": guild_id or ""}

    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        ctx["error"] = "DATABASE_URL이 설정되지 않았습니다."
        return _render(request, "admin.html", base_url=BASE_URL, **ctx)

    if guild_id:
        async with pool.acquire() as conn:
            ctx["settings"] = await _db_fetch_settings(conn, guild_id)
            ctx["rules"] = await _db_fetch_rules(conn, guild_id)
            ctx["top10"] = await _db_top10(conn, guild_id)

    return _render(request, "admin.html", base_url=BASE_URL, **ctx)

@app.post("/admin/save-settings")
async def save_settings(
    request: Request,
    guild_id: str = Form(...),
    checkin_xp: int = Form(...),
    message_xp: int = Form(...),
    message_cooldown_sec: int = Form(...),
    voice_xp_per_min: int = Form(...),
):
    if not _is_admin(request):
        return RedirectResponse(url="/", status_code=302)

    gid = _parse_id(guild_id)
    if not gid:
        return RedirectResponse(url="/admin?msg=Guild+ID가+올바르지+않습니다", status_code=302)

    pool = getattr(app.state, "pg_pool", None)
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO guild_settings (guild_id, checkin_xp, message_xp, message_cooldown_sec, voice_xp_per_min)
            VALUES ($1,$2,$3,$4,$5)
            ON CONFLICT (guild_id) DO UPDATE SET
              checkin_xp=EXCLUDED.checkin_xp,
              message_xp=EXCLUDED.message_xp,
              message_cooldown_sec=EXCLUDED.message_cooldown_sec,
              voice_xp_per_min=EXCLUDED.voice_xp_per_min
        """, gid, int(checkin_xp), int(message_xp), int(message_cooldown_sec), int(voice_xp_per_min))

    return RedirectResponse(url=f"/admin?guild_id={gid}&msg=저장+완료", status_code=302)

@app.post("/admin/quick-checkin-reset")
async def quick_checkin_reset(request: Request, guild_id: str = Form(...), user_pick: str = Form(...), ymd: str = Form(...)):
    if not _is_admin(request):
        return RedirectResponse(url="/", status_code=302)
    gid = _parse_id(guild_id)
    uid = _parse_id(user_pick)
    if not gid or not uid:
        return RedirectResponse(url=f"/admin?guild_id={gid or ''}&msg=유저/서버+ID가+올바르지+않습니다", status_code=302)

    pool = getattr(app.state, "pg_pool", None)
    async with pool.acquire() as conn:
        res = await conn.execute("DELETE FROM checkins WHERE guild_id=$1 AND user_id=$2 AND ymd=$3", gid, uid, ymd)
    try:
        deleted = int(str(res).split()[-1])
    except Exception:
        deleted = 0
    return RedirectResponse(url=f"/admin?guild_id={gid}&msg=출석기록+삭제:{deleted}", status_code=302)

@app.post("/admin/quick-set-level")
async def quick_set_level(request: Request, guild_id: str = Form(...), user_pick: str = Form(...), level: int = Form(...)):
    if not _is_admin(request):
        return RedirectResponse(url="/", status_code=302)
    gid = _parse_id(guild_id)
    uid = _parse_id(user_pick)
    if not gid or not uid:
        return RedirectResponse(url=f"/admin?guild_id={gid or ''}&msg=유저/서버+ID가+올바르지+않습니다", status_code=302)

    xp = max(int(level), 0) * 100
    pool = getattr(app.state, "pg_pool", None)
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO user_stats (guild_id, user_id, xp)
            VALUES ($1,$2,$3)
            ON CONFLICT (guild_id, user_id) DO UPDATE SET xp=$3
        """, gid, uid, xp)

    return RedirectResponse(url=f"/admin?guild_id={gid}&msg=레벨+적용+완료", status_code=302)

@app.post("/admin/rules-upsert")
async def rules_upsert(request: Request, guild_id: str = Form(...), level: int = Form(...), add_role_pick: str = Form(...), remove_role_pick: str = Form("")):
    if not _is_admin(request):
        return RedirectResponse(url="/", status_code=302)
    gid = _parse_id(guild_id)
    add_id = _parse_id(add_role_pick)
    rem_id = _parse_id(remove_role_pick) if remove_role_pick else None
    if not gid or not add_id:
        return RedirectResponse(url=f"/admin?guild_id={gid or ''}&msg=역할/서버+ID가+올바르지+않습니다", status_code=302)

    pool = getattr(app.state, "pg_pool", None)
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO level_roles (guild_id, level, add_role_id, remove_role_id)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (guild_id, level) DO UPDATE SET add_role_id=$3, remove_role_id=$4
        """, gid, int(level), int(add_id), int(rem_id) if rem_id else None)

    return RedirectResponse(url=f"/admin?guild_id={gid}&msg=규칙+저장+완료", status_code=302)

@app.post("/admin/rules-delete")
async def rules_delete(request: Request, guild_id: str = Form(...), level: int = Form(...)):
    if not _is_admin(request):
        return RedirectResponse(url="/", status_code=302)
    gid = _parse_id(guild_id)
    if not gid:
        return RedirectResponse(url="/admin?msg=Guild+ID가+올바르지+않습니다", status_code=302)

    pool = getattr(app.state, "pg_pool", None)
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM level_roles WHERE guild_id=$1 AND level=$2", gid, int(level))
    return RedirectResponse(url=f"/admin?guild_id={gid}&msg=규칙+삭제+완료", status_code=302)

# ---- Admin API: roles from DB cache (no Discord API) ----
@app.get("/admin/api/roles")
async def api_roles(request: Request, guild_id: int = Query(...)):
    if not _is_admin(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    pool = getattr(app.state, "pg_pool", None)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT role_id, role_name, position FROM guild_roles_cache WHERE guild_id=$1 ORDER BY position DESC, role_name ASC",
            int(guild_id),
        )
    payload = [{"id": int(r["role_id"]), "label": f'{r["role_name"]} ({r["role_id"]})'} for r in rows]
    return JSONResponse(payload)

# ---- Admin API: members search from DB cache (no Discord API) ----
@app.get("/admin/api/members_search")
async def api_members_search(request: Request, guild_id: int = Query(...), q: str = Query(...)):
    if not _is_admin(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    q = (q or "").strip()
    if len(q) < 2:
        return JSONResponse([], status_code=200)

    pool = getattr(app.state, "pg_pool", None)
    q_like = f"%{q}%"
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT user_id, username, discriminator, global_name, nick, display_name
            FROM guild_members_cache
            WHERE guild_id=$1 AND (
              display_name ILIKE $2 OR nick ILIKE $2 OR global_name ILIKE $2 OR username ILIKE $2 OR CAST(user_id AS TEXT) LIKE $3
            )
            ORDER BY updated_at DESC
            LIMIT 25
            """,
            int(guild_id), q_like, f"%{q}%"
        )
    out = []
    for r in rows:
        display = r["display_name"] or r["nick"] or r["global_name"] or (f'{r["username"]}#{r["discriminator"]}' if r["username"] else "-")
        out.append({"id": int(r["user_id"]), "label": f"{display} ({r['user_id']})"})
    return JSONResponse(out)

# Kept for compatibility with template; now just redirects.
@app.post("/admin/api/resolve_top10")
async def resolve_top10(request: Request, guild_id: int = Form(...)):
    if not _is_admin(request):
        return RedirectResponse(url="/", status_code=302)
    gid = int(guild_id)
    return RedirectResponse(url=f"/admin?guild_id={gid}&msg=닉네임은+봇이+DB캐시로+자동+갱신합니다", status_code=302)
