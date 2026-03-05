# -*- coding: utf-8 -*-
"""
Nyoping Dashboard (Render)
- Admin dashboard UI for managing bot settings stored in Postgres (shared with bot).
- Safe Discord OAuth handling with global rate-limit cooldown persisted in DB.
- Admin password login to use dashboard even when Discord OAuth is blocked.

This dashboard expects the bot schema (guild_settings, user_stats, checkins, level_roles).
It will also create tables if missing.

Env:
  DATABASE_URL (required for real dashboard; otherwise runs with in-memory cooldown only)
  DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET (optional if DISABLE_DISCORD_OAUTH=1)
  DASHBOARD_BASE_URL (https://nyoping-bot.onrender.com)
  DASHBOARD_SESSION_SECRET
Optional:
  DASHBOARD_ADMIN_PASSWORD
  DISABLE_DISCORD_OAUTH=1
"""
from __future__ import annotations

import os
import time
import json
import secrets
from typing import Optional, Dict, Any

import asyncpg
import requests
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates
from pathlib import Path

# ----------------- Config -----------------
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
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "") or os.getenv("DISCORD_TOKEN", "")

# Bot schema (same as nyopingbot/db/pg.py)
SCHEMA_SQL = """
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

CREATE TABLE IF NOT EXISTS dashboard_kv (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);
"""

KV_COOLDOWN_UNTIL = "discord_oauth_cooldown_until"
KV_COOLDOWN_REASON = "discord_oauth_cooldown_reason"

# ----------------- App -----------------
app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, https_only=BASE_URL.startswith("https://"))
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_mem_kv: Dict[str, str] = {}

def _is_admin(request: Request) -> bool:
    return bool(request.session.get("admin"))

def _render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    return templates.TemplateResponse(name, {"request": request, **ctx})

def kst_today_ymd() -> str:
    # KST = UTC+9 (no DST)
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc) + timedelta(hours=9)
    return now.strftime("%Y-%m-%d")

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
    seconds = min(max(float(seconds), 30.0), 3600.0)
    new_until = time.time() + seconds + 3.0
    cur_raw = await _kv_get(KV_COOLDOWN_UNTIL)
    try:
        cur_until = float(cur_raw) if cur_raw else 0.0
    except Exception:
        cur_until = 0.0
    if new_until > cur_until:
        await _kv_set(KV_COOLDOWN_UNTIL, str(new_until))
        await _kv_set(KV_COOLDOWN_REASON, reason)


# ----------------- Discord (bot token) helpers -----------------
import re as _re

def _parse_snowflake(raw: str) -> Optional[int]:
    if not raw:
        return None
    m = _re.search(r"(\d{15,25})", str(raw))
    return int(m.group(1)) if m else None

def _discord_headers() -> Dict[str, str]:
    if not BOT_TOKEN:
        return {}
    return {"Authorization": f"Bot {BOT_TOKEN}"}

async def _kv_get_json(key: str) -> Optional[Dict[str, Any]]:
    raw = await _kv_get(key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None

async def _kv_set_json(key: str, obj: Any) -> None:
    await _kv_set(key, json.dumps(obj, ensure_ascii=False))

async def _cached(key: str, ttl_sec: int) -> Optional[Any]:
    ts_key = key + ":ts"
    ts_raw = await _kv_get(ts_key)
    if ts_raw:
        try:
            if time.time() - float(ts_raw) < ttl_sec:
                return await _kv_get_json(key)
        except Exception:
            pass
    return None

async def _cache_put(key: str, obj: Any) -> None:
    await _kv_set_json(key, obj)
    await _kv_set(key + ":ts", str(time.time()))

def _discord_get(url: str, params: Optional[Dict[str, Any]] = None) -> Optional[requests.Response]:
    if not BOT_TOKEN:
        return None
    try:
        return requests.get(url, headers=_discord_headers(), params=params, timeout=15)
    except requests.RequestException:
        return None

async def _fetch_roles(guild_id: int) -> list[Dict[str, Any]]:
    cache_key = f"guild:{guild_id}:roles"
    cached = await _cached(cache_key, 300)
    if cached is not None:
        return cached  # type: ignore
    r = _discord_get(f"{DISCORD_API_BASE}/guilds/{guild_id}/roles")
    if r is None or not r.ok:
        return []
    roles = [{"id": int(role["id"]), "name": role.get("name","")} for role in r.json()]
    await _cache_put(cache_key, roles)
    return roles

async def _fetch_members(guild_id: int) -> list[Dict[str, Any]]:
    cache_key = f"guild:{guild_id}:members"
    cached = await _cached(cache_key, 300)
    if cached is not None:
        return cached  # type: ignore
    members: list[Dict[str, Any]] = []
    after = 0
    while True:
        r = _discord_get(f"{DISCORD_API_BASE}/guilds/{guild_id}/members", params={"limit": 1000, "after": after})
        if r is None:
            break
        if r.status_code == 429:
            # don't escalate; just stop fetching
            break
        if not r.ok:
            break
        batch = r.json()
        if not batch:
            break
        for m in batch:
            u = m.get("user") or {}
            uid = int(u.get("id"))
            username = u.get("username") or ""
            discrim = u.get("discriminator") or "0"
            global_name = u.get("global_name") or ""
            nick = m.get("nick") or ""
            label = nick or global_name or username
            members.append({"id": uid, "label": label, "username": username, "discriminator": discrim, "nick": nick})
        if len(batch) < 1000:
            break
        after = int((batch[-1].get("user") or {}).get("id") or 0)
        if after == 0:
            break
    await _cache_put(cache_key, members)
    return members

# ----------------- DB helpers -----------------
async def _ensure_guild_settings(conn: asyncpg.Connection, guild_id: int) -> None:
    await conn.execute("INSERT INTO guild_settings (guild_id) VALUES ($1) ON CONFLICT (guild_id) DO NOTHING", guild_id)

async def _get_guild_settings(guild_id: int) -> Dict[str, Any]:
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        return {}
    async with pool.acquire() as conn:
        await _ensure_guild_settings(conn, guild_id)
        row = await conn.fetchrow("SELECT * FROM guild_settings WHERE guild_id=$1", guild_id)
        return dict(row) if row else {}

async def _update_guild_settings(guild_id: int, updates: Dict[str, Any]) -> None:
    pool = getattr(app.state, "pg_pool", None)
    if pool is None or not updates:
        return
    cols = list(updates.keys())
    vals = list(updates.values())
    sets = ", ".join([f"{c}=${i+2}" for i, c in enumerate(cols)])
    sql = f"UPDATE guild_settings SET {sets} WHERE guild_id=$1"
    async with pool.acquire() as conn:
        await _ensure_guild_settings(conn, guild_id)
        await conn.execute(sql, guild_id, *vals)

async def _list_level_roles(guild_id: int) -> list[Dict[str, Any]]:
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT level, add_role_id, remove_role_id FROM level_roles WHERE guild_id=$1 ORDER BY level", guild_id)
        return [dict(r) for r in rows]

async def _set_level_role(guild_id: int, level: int, add_role_id: int, remove_role_id: Optional[int]) -> None:
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO level_roles (guild_id, level, add_role_id, remove_role_id)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (guild_id, level)
            DO UPDATE SET add_role_id=$3, remove_role_id=$4
            """,
            guild_id, int(level), int(add_role_id), remove_role_id
        )

async def _delete_level_role(guild_id: int, level: int) -> None:
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        return
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM level_roles WHERE guild_id=$1 AND level=$2", guild_id, int(level))

async def _reset_checkin(guild_id: int, user_id: int, ymd: str) -> int:
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        return 0
    async with pool.acquire() as conn:
        r = await conn.execute("DELETE FROM checkins WHERE guild_id=$1 AND user_id=$2 AND ymd=$3", guild_id, user_id, ymd)
    try:
        return int(str(r).split()[-1])
    except Exception:
        return 0

async def _set_user_xp(guild_id: int, user_id: int, xp: int) -> None:
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        return
    xp = max(int(xp), 0)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_stats (guild_id, user_id, xp)
            VALUES ($1,$2,$3)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET xp=$3
            """,
            guild_id, user_id, xp
        )

async def _top_users(guild_id: int, limit: int = 10) -> list[Dict[str, Any]]:
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, xp FROM user_stats WHERE guild_id=$1 ORDER BY xp DESC LIMIT $2", guild_id, int(limit))
        return [dict(r) for r in rows]

# ----------------- Lifecycle -----------------
@app.on_event("startup")
async def _startup() -> None:
    if DATABASE_URL:
        app.state.pg_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3, command_timeout=20)
        async with app.state.pg_pool.acquire() as conn:
            await conn.execute(SCHEMA_SQL)
    else:
        app.state.pg_pool = None

@app.on_event("shutdown")
async def _shutdown() -> None:
    pool = getattr(app.state, "pg_pool", None)
    if pool:
        await pool.close()

# ----------------- Routes -----------------
@app.get("/healthz")
async def healthz() -> Dict[str, Any]:
    return {
        "ok": True,
        "cooldown": await _cooldown_active(),
        "cooldown_remaining": await _cooldown_remaining(),
        "db": bool(getattr(app.state, "pg_pool", None)),
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
        is_admin=_is_admin(request),
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
            is_admin=False,
        )
    request.session["admin"] = True
    return RedirectResponse(url="/admin", status_code=302)

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, guild_id: Optional[int] = None) -> HTMLResponse:
    if not _is_admin(request):
        return RedirectResponse(url="/", status_code=302)

    gid = int(guild_id) if guild_id else None
    settings: Dict[str, Any] = {}
    rules: list[Dict[str, Any]] = []
    top: list[Dict[str, Any]] = []
    members: list[Dict[str, Any]] = []
    roles: list[Dict[str, Any]] = []
    role_map: Dict[int, str] = {}
    member_map: Dict[int, Dict[str, Any]] = {}
    if gid:
        settings = await _get_guild_settings(gid)
        rules = await _list_level_roles(gid)
        top = await _top_users(gid, 10)
        if BOT_TOKEN:
            roles = await _fetch_roles(gid)
            members = await _fetch_members(gid)
            role_map = {int(r["id"]): r.get("name","") for r in roles}
            member_map = {int(m["id"]): m for m in members}
        # enrich top with names
        for u in top:
            m = member_map.get(int(u["user_id"])) if member_map else None
            if m:
                u["username"] = m.get("username","")
                u["discriminator"] = m.get("discriminator","0")
                u["nick"] = m.get("nick","")
                u["label"] = m.get("label","")

    return _render(
        request,
        "admin.html",
        base_url=BASE_URL,
        guild_id=gid,
        settings=settings,
        rules=rules,
        top=top,
        members=members,
        roles=roles,
        role_map=role_map,
        bot_token_enabled=bool(BOT_TOKEN),
        today_ymd=kst_today_ymd(),
        flash=request.session.pop("flash", ""),
        error=request.session.pop("error", ""),
        disable_discord_oauth=DISABLE_DISCORD_OAUTH,
    )

@app.post("/admin/settings")
async def admin_update_settings(
    request: Request,
    guild_id: int = Form(...),
    checkin_xp: int = Form(...),
    checkin_limit_enabled: Optional[str] = Form(None),
    message_xp: int = Form(...),
    message_cooldown_sec: int = Form(...),
    voice_xp_per_min: int = Form(...),
) -> Response:
    if not _is_admin(request):
        return RedirectResponse(url="/", status_code=302)
    gid = int(guild_id)
    await _update_guild_settings(
        gid,
        {
            "checkin_xp": int(checkin_xp),
            "checkin_limit_enabled": bool(checkin_limit_enabled),
            "message_xp": int(message_xp),
            "message_cooldown_sec": int(message_cooldown_sec),
            "voice_xp_per_min": int(voice_xp_per_min),
        },
    )
    request.session["flash"] = "서버 설정이 저장되었습니다."
    return RedirectResponse(url=f"/admin?guild_id={gid}", status_code=302)

@app.post("/admin/levelrole/set")
async def admin_set_level_role(
    request: Request,
    guild_id: int = Form(...),
    level: int = Form(...),
    add_role: str = Form(...),
    remove_role: Optional[str] = Form(None),
) -> Response:
    if not _is_admin(request):
        return RedirectResponse(url="/", status_code=302)
    gid = int(guild_id)
    add_id = _parse_snowflake(add_role)
    if add_id is None:
        request.session["error"] = "추가할 역할 선택/ID가 올바르지 않습니다."
        return RedirectResponse(url=f"/admin?guild_id={gid}", status_code=302)
    rid = _parse_snowflake(remove_role) if (remove_role and remove_role.strip()) else None
    await _set_level_role(gid, int(level), int(add_id), int(rid) if rid else None)
    request.session["flash"] = f"레벨역할 규칙 저장: 레벨 {level}"
    return RedirectResponse(url=f"/admin?guild_id={gid}", status_code=302)

@app.post("/admin/levelrole/delete")
async def admin_delete_level_role(
    request: Request,
    guild_id: int = Form(...),
    level: int = Form(...),
) -> Response:
    if not _is_admin(request):
        return RedirectResponse(url="/", status_code=302)
    gid = int(guild_id)
    await _delete_level_role(gid, int(level))
    request.session["flash"] = f"레벨역할 규칙 삭제: 레벨 {level}"
    return RedirectResponse(url=f"/admin?guild_id={gid}", status_code=302)

@app.post("/admin/checkin/reset")
async def admin_reset_checkin(
    request: Request,
    guild_id: int = Form(...),
    user: str = Form(...),
    ymd: str = Form(...),
) -> Response:
    if not _is_admin(request):
        return RedirectResponse(url="/", status_code=302)
    gid = int(guild_id)
    uid = _parse_snowflake(user)
    if uid is None:
        request.session["error"] = "유저 선택/ID가 올바르지 않습니다."
        return RedirectResponse(url=f"/admin?guild_id={gid}", status_code=302)
    ymd = ymd.strip()
    deleted = await _reset_checkin(gid, uid, ymd)
    request.session["flash"] = f"출석 초기화 완료: 삭제 {deleted}건 (ymd={ymd})"
    return RedirectResponse(url=f"/admin?guild_id={gid}", status_code=302)

@app.post("/admin/level/set")
async def admin_set_level(
    request: Request,
    guild_id: int = Form(...),
    user: str = Form(...),
    level: int = Form(...),
) -> Response:
    if not _is_admin(request):
        return RedirectResponse(url="/", status_code=302)
    gid = int(guild_id)
    uid = _parse_snowflake(user)
    if uid is None:
        request.session["error"] = "유저 선택/ID가 올바르지 않습니다."
        return RedirectResponse(url=f"/admin?guild_id={gid}", status_code=302)
    lvl = max(int(level), 0)
    xp = lvl * 100
    await _set_user_xp(gid, uid, xp)
    request.session["flash"] = f"레벨 조정 완료: 유저 {uid} → 레벨 {lvl} (XP={xp})"
    return RedirectResponse(url=f"/admin?guild_id={gid}", status_code=302)

# ------------- Discord OAuth (optional) -------------
@app.get("/login")
async def discord_login(request: Request) -> Response:
    if DISABLE_DISCORD_OAUTH:
        return RedirectResponse(url="/", status_code=302)

    if await _cooldown_active():
        return _render(request, "rate_limited.html", cooldown_seconds=await _cooldown_remaining(),
                       reason=(await _kv_get(KV_COOLDOWN_REASON)) or "Discord API 글로벌 레이트리밋 상태입니다.")

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
    )
    return RedirectResponse(url=url, status_code=302)

@app.get("/callback")
async def callback(request: Request, code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None) -> Response:
    if await _cooldown_active():
        return _render(request, "rate_limited.html", cooldown_seconds=await _cooldown_remaining(),
                       reason=(await _kv_get(KV_COOLDOWN_REASON)) or "Discord API 글로벌 레이트리밋 상태입니다.")

    if error:
        return _render(request, "error.html", message=f"Discord 로그인 에러: {error}")
    if not code:
        return _render(request, "error.html", message="Discord 로그인 코드가 없습니다. 다시 시도해주세요.")

    expected = request.session.get("oauth_state")
    if expected and state and state != expected:
        return _render(request, "error.html", message="OAuth state가 일치하지 않습니다. 다시 시도해주세요.")

    # Prevent double exchange for same code
    if request.session.get("oauth_last_code") == code:
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

    try:
        r = requests.post(OAUTH_TOKEN, data=data, timeout=15)
    except requests.RequestException as e:
        return _render(request, "error.html", message=f"토큰 요청 실패: {e}")

    if r.status_code == 429:
        try:
            payload = r.json()
        except Exception:
            payload = {}
        retry_after = float(payload.get("retry_after", 60))
        await _set_cooldown(retry_after, "Discord OAuth 토큰 요청이 글로벌 레이트리밋에 걸렸습니다.")
        return _render(request, "rate_limited.html", cooldown_seconds=await _cooldown_remaining(),
                       reason="Discord API 글로벌 레이트리밋에 걸렸습니다. 잠시 후 다시 시도하세요.")

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
        r = requests.get(f"{DISCORD_API_BASE}/users/@me", headers={"Authorization": f"Bearer {token}"}, timeout=10)
    except requests.RequestException as e:
        return _render(request, "error.html", message=f"Discord API 요청 실패: {e}")
    if r.status_code == 429:
        try:
            payload = r.json()
        except Exception:
            payload = {}
        retry_after = float(payload.get("retry_after", 60))
        await _set_cooldown(retry_after, "Discord API 글로벌 레이트리밋 상태입니다.")
        return _render(request, "rate_limited.html", cooldown_seconds=await _cooldown_remaining(),
                       reason="Discord API 글로벌 레이트리밋에 걸렸습니다. 잠시 후 다시 시도하세요.")
    if not r.ok:
        return _render(request, "error.html", message=f"Discord API 에러: {r.status_code} {r.text[:500]}")
    user = r.json()
    return _render(request, "me.html", user=user, base_url=BASE_URL)
