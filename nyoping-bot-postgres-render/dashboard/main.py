# -*- coding: utf-8 -*-
"""
Nyoping Dashboard Admin UI v3
- Uses DISCORD_BOT_TOKEN (Bot auth) to fetch members/roles for pickers and for Top10 name resolution.
- Keeps admin-only access via DASHBOARD_ADMIN_PASSWORD session.
- Avoids redirecting to JSON error pages; always redirects back to /admin with msg.
"""
from __future__ import annotations

import os
import time
import secrets
import re
from typing import Optional, Dict, Any, List, Tuple

import asyncpg
import requests
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates
from pathlib import Path

BASE_URL = os.getenv("DASHBOARD_BASE_URL", "").rstrip("/")
SESSION_SECRET = os.getenv("DASHBOARD_SESSION_SECRET", "") or secrets.token_urlsafe(32)
ADMIN_PASSWORD = os.getenv("DASHBOARD_ADMIN_PASSWORD", "")
DISABLE_DISCORD_OAUTH = os.getenv("DISABLE_DISCORD_OAUTH", "0") == "1"
DATABASE_URL = os.getenv("DATABASE_URL", "")
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_API_BASE = "https://discord.com/api/v10"

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, https_only=BASE_URL.startswith("https://"))
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

@app.on_event("startup")
async def _startup() -> None:
    if not DATABASE_URL:
        app.state.pg_pool = None
        return
    app.state.pg_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3, command_timeout=20)
    async with app.state.pg_pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS guild_settings(
            guild_id BIGINT PRIMARY KEY,
            checkin_xp INT NOT NULL DEFAULT 20,
            message_xp INT NOT NULL DEFAULT 2,
            message_cooldown_sec INT NOT NULL DEFAULT 60,
            voice_xp_per_min INT NOT NULL DEFAULT 1,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS level_role_rules(
            guild_id BIGINT NOT NULL,
            level INT NOT NULL,
            add_role_id BIGINT NOT NULL,
            remove_role_id BIGINT,
            PRIMARY KEY (guild_id, level)
        );
        """)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS user_xp(
            guild_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            xp BIGINT NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (guild_id, user_id)
        );
        """)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS checkins(
            guild_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            ymd TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id, ymd)
        );
        """)

@app.on_event("shutdown")
async def _shutdown() -> None:
    pool = getattr(app.state, "pg_pool", None)
    if pool:
        await pool.close()

def _render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    return templates.TemplateResponse(name, {"request": request, **ctx})

def _is_admin(request: Request) -> bool:
    return bool(request.session.get("admin"))

def _bot_headers() -> Dict[str, str]:
    return {"Authorization": f"Bot {BOT_TOKEN}"}

_cache: Dict[str, Tuple[float, Any]] = {}
CACHE_TTL_SEC = 120

def _cache_get(key: str):
    v = _cache.get(key)
    if not v:
        return None
    ts, payload = v
    if time.time() - ts > CACHE_TTL_SEC:
        _cache.pop(key, None)
        return None
    return payload

def _cache_set(key: str, payload: Any):
    _cache[key] = (time.time(), payload)

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

async def _db_fetch_settings(guild_id: int) -> Dict[str, Any]:
    pool = app.state.pg_pool
    if pool is None:
        return {"checkin_xp": 20, "message_xp": 2, "message_cooldown_sec": 60, "voice_xp_per_min": 1}
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT checkin_xp, message_xp, message_cooldown_sec, voice_xp_per_min FROM guild_settings WHERE guild_id=$1",
            guild_id,
        )
        if not row:
            await conn.execute("INSERT INTO guild_settings(guild_id) VALUES ($1) ON CONFLICT DO NOTHING", guild_id)
            return {"checkin_xp": 20, "message_xp": 2, "message_cooldown_sec": 60, "voice_xp_per_min": 1}
        return dict(row)

async def _db_save_settings(guild_id: int, checkin_xp: int, message_xp: int, message_cd: int, voice_xp: int) -> None:
    pool = app.state.pg_pool
    if pool is None:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO guild_settings(guild_id, checkin_xp, message_xp, message_cooldown_sec, voice_xp_per_min, updated_at)
            VALUES ($1,$2,$3,$4,$5,NOW())
            ON CONFLICT (guild_id) DO UPDATE SET
              checkin_xp=EXCLUDED.checkin_xp,
              message_xp=EXCLUDED.message_xp,
              message_cooldown_sec=EXCLUDED.message_cooldown_sec,
              voice_xp_per_min=EXCLUDED.voice_xp_per_min,
              updated_at=NOW()
            """,
            guild_id, checkin_xp, message_xp, message_cd, voice_xp,
        )

async def _db_fetch_rules(guild_id: int) -> List[Dict[str, Any]]:
    pool = app.state.pg_pool
    if pool is None:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT level, add_role_id, remove_role_id FROM level_role_rules WHERE guild_id=$1 ORDER BY level ASC",
            guild_id,
        )
        return [dict(r) for r in rows]

async def _db_upsert_rule(guild_id: int, level: int, add_role_id: int, remove_role_id: Optional[int]) -> None:
    pool = app.state.pg_pool
    if pool is None:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO level_role_rules(guild_id, level, add_role_id, remove_role_id)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (guild_id, level) DO UPDATE
              SET add_role_id=EXCLUDED.add_role_id, remove_role_id=EXCLUDED.remove_role_id
            """,
            guild_id, level, add_role_id, remove_role_id,
        )

async def _db_delete_rule(guild_id: int, level: int) -> None:
    pool = app.state.pg_pool
    if pool is None:
        return
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM level_role_rules WHERE guild_id=$1 AND level=$2", guild_id, level)

async def _db_top10(guild_id: int) -> List[Dict[str, Any]]:
    pool = app.state.pg_pool
    if pool is None:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, xp FROM user_xp WHERE guild_id=$1 ORDER BY xp DESC LIMIT 10", guild_id)
        out = []
        for r in rows:
            xp = int(r["xp"])
            out.append({"user_id": int(r["user_id"]), "xp": xp, "level": xp // 100})
        return out

async def _db_reset_checkin(guild_id: int, user_id: int, ymd: str) -> int:
    pool = app.state.pg_pool
    if pool is None:
        return 0
    async with pool.acquire() as conn:
        res = await conn.execute("DELETE FROM checkins WHERE guild_id=$1 AND user_id=$2 AND ymd=$3", guild_id, user_id, ymd)
        return int(res.split()[-1])

async def _db_set_level(guild_id: int, user_id: int, level: int) -> None:
    pool = app.state.pg_pool
    if pool is None:
        return
    xp = max(0, int(level)) * 100
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_xp(guild_id, user_id, xp, updated_at)
            VALUES ($1,$2,$3,NOW())
            ON CONFLICT (guild_id, user_id) DO UPDATE SET xp=EXCLUDED.xp, updated_at=NOW()
            """,
            guild_id, user_id, xp,
        )

def _member_label(m: Dict[str, Any]) -> str:
    user = m.get("user") or {}
    username = user.get("username") or ""
    disc = user.get("discriminator") or ""
    global_name = user.get("global_name") or ""
    nick = m.get("nick") or ""
    uid = user.get("id") or ""
    if nick:
        return f"{nick} ({username}#{disc}) ({uid})"
    if global_name:
        return f"{global_name} ({username}#{disc}) ({uid})"
    return f"{username}#{disc} ({uid})"

def _role_label(r: Dict[str, Any]) -> str:
    return f"{r.get('name','')} ({r.get('id','')})"

def _fetch_roles(guild_id: int):
    if not BOT_TOKEN:
        return [], "Render 환경변수 DISCORD_BOT_TOKEN이 비어 있습니다."
    ck = f"roles:{guild_id}"
    cached = _cache_get(ck)
    if cached is not None:
        return cached, None
    try:
        r = requests.get(f"{DISCORD_API_BASE}/guilds/{guild_id}/roles", headers=_bot_headers(), timeout=15)
    except Exception as e:
        return [], f"역할 목록 요청 실패: {e}"
    if r.status_code == 429:
        return [], "Discord API 레이트리밋(roles). 잠시 후 다시 시도하세요."
    if not r.ok:
        return [], f"역할 목록 요청 실패: HTTP {r.status_code} {r.text[:200]}"
    roles = r.json()
    roles.sort(key=lambda x: x.get("position", 0), reverse=True)
    _cache_set(ck, roles)
    return roles, None

def _fetch_members(guild_id: int, limit: int = 1000):
    if not BOT_TOKEN:
        return [], "Render 환경변수 DISCORD_BOT_TOKEN이 비어 있습니다."
    ck = f"members:{guild_id}"
    cached = _cache_get(ck)
    if cached is not None:
        return cached, None
    members = []
    after = 0
    remaining = limit
    try:
        while remaining > 0:
            page = min(1000, remaining)
            url = f"{DISCORD_API_BASE}/guilds/{guild_id}/members?limit={page}&after={after}"
            r = requests.get(url, headers=_bot_headers(), timeout=20)
            if r.status_code == 429:
                return [], "Discord API 레이트리밋(members). 잠시 후 다시 시도하세요."
            if not r.ok:
                return [], f"멤버 목록 요청 실패: HTTP {r.status_code} {r.text[:200]}"
            batch = r.json()
            if not batch:
                break
            members.extend(batch)
            remaining -= len(batch)
            after = int(batch[-1].get("user", {}).get("id", "0") or "0")
            if len(batch) < page:
                break
    except Exception as e:
        return [], f"멤버 목록 요청 실패: {e}"
    _cache_set(ck, members)
    return members, None

@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return _render(request, "index.html", base_url=BASE_URL, admin_enabled=bool(ADMIN_PASSWORD), disable_discord_oauth=DISABLE_DISCORD_OAUTH)

@app.post("/admin-login")
async def admin_login(request: Request, password: str = Form(...)) -> Response:
    if not ADMIN_PASSWORD or password != ADMIN_PASSWORD:
        return _render(request, "index.html", base_url=BASE_URL, admin_enabled=bool(ADMIN_PASSWORD), disable_discord_oauth=DISABLE_DISCORD_OAUTH,
                       admin_error="비밀번호가 올바르지 않습니다.")
    request.session["admin"] = True
    return RedirectResponse(url="/admin", status_code=302)

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, guild_id: Optional[int] = None, msg: str = "") -> HTMLResponse:
    if not _is_admin(request):
        return RedirectResponse(url="/", status_code=302)
    ctx = {"settings": None, "rules": [], "top10": [], "members": [], "roles": [], "error": "", "msg": msg}
    if guild_id:
        ctx["settings"] = await _db_fetch_settings(guild_id)
        ctx["rules"] = await _db_fetch_rules(guild_id)
        ctx["top10"] = await _db_top10(guild_id)
        members, err_m = _fetch_members(guild_id, limit=1000)
        roles, err_r = _fetch_roles(guild_id)
        if err_m:
            ctx["error"] += err_m + " "
        if err_r:
            ctx["error"] += err_r + " "
        ctx["members"] = [{"id": int(m["user"]["id"]), "label": _member_label(m)} for m in members if m.get("user") and m["user"].get("id")]
        ctx["roles"] = [{"id": int(r["id"]), "label": _role_label(r)} for r in roles if r.get("id")]
        m_map = {m["id"]: m["label"] for m in ctx["members"]}
        for row in ctx["top10"]:
            row["name"] = m_map.get(row["user_id"], "-")
    return _render(request, "admin.html", base_url=BASE_URL, guild_id=guild_id or "", **ctx)

@app.post("/admin/save-settings")
async def save_settings(request: Request, guild_id: str = Form(...), checkin_xp: int = Form(...), message_xp: int = Form(...),
                        message_cooldown_sec: int = Form(...), voice_xp_per_min: int = Form(...)) -> Response:
    if not _is_admin(request):
        return RedirectResponse(url="/", status_code=302)
    gid = _parse_id(guild_id)
    if not gid:
        return RedirectResponse(url="/admin?msg=Guild+ID가+올바르지+않습니다", status_code=302)
    await _db_save_settings(gid, checkin_xp, message_xp, message_cooldown_sec, voice_xp_per_min)
    return RedirectResponse(url=f"/admin?guild_id={gid}&msg=저장+완료", status_code=302)

@app.post("/admin/quick-checkin-reset")
async def quick_checkin_reset(request: Request, guild_id: str = Form(...), user_pick: str = Form(...), ymd: str = Form(...)) -> Response:
    if not _is_admin(request):
        return RedirectResponse(url="/", status_code=302)
    gid = _parse_id(guild_id); uid = _parse_id(user_pick)
    if not gid or not uid:
        return RedirectResponse(url=f"/admin?guild_id={gid or ''}&msg=유저/서버+ID가+올바르지+않습니다", status_code=302)
    deleted = await _db_reset_checkin(gid, uid, ymd)
    return RedirectResponse(url=f"/admin?guild_id={gid}&msg=출석기록+삭제:{deleted}", status_code=302)

@app.post("/admin/quick-set-level")
async def quick_set_level(request: Request, guild_id: str = Form(...), user_pick: str = Form(...), level: int = Form(...)) -> Response:
    if not _is_admin(request):
        return RedirectResponse(url="/", status_code=302)
    gid = _parse_id(guild_id); uid = _parse_id(user_pick)
    if not gid or not uid:
        return RedirectResponse(url=f"/admin?guild_id={gid or ''}&msg=유저/서버+ID가+올바르지+않습니다", status_code=302)
    await _db_set_level(gid, uid, level)
    return RedirectResponse(url=f"/admin?guild_id={gid}&msg=레벨+적용+완료", status_code=302)

@app.post("/admin/rules-upsert")
async def rules_upsert(request: Request, guild_id: str = Form(...), level: int = Form(...), add_role_pick: str = Form(...), remove_role_pick: str = Form("")) -> Response:
    if not _is_admin(request):
        return RedirectResponse(url="/", status_code=302)
    gid = _parse_id(guild_id); add_id = _parse_id(add_role_pick); rem_id = _parse_id(remove_role_pick) if remove_role_pick else None
    if not gid or not add_id:
        return RedirectResponse(url=f"/admin?guild_id={gid or ''}&msg=역할/서버+ID가+올바르지+않습니다", status_code=302)
    await _db_upsert_rule(gid, int(level), add_id, rem_id)
    return RedirectResponse(url=f"/admin?guild_id={gid}&msg=규칙+저장+완료", status_code=302)

@app.post("/admin/rules-delete")
async def rules_delete(request: Request, guild_id: str = Form(...), level: int = Form(...)) -> Response:
    if not _is_admin(request):
        return RedirectResponse(url="/", status_code=302)
    gid = _parse_id(guild_id)
    if not gid:
        return RedirectResponse(url="/admin?msg=Guild+ID가+올바르지+않습니다", status_code=302)
    await _db_delete_rule(gid, int(level))
    return RedirectResponse(url=f"/admin?guild_id={gid}&msg=규칙+삭제+완료", status_code=302)
