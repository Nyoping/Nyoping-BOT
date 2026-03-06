from __future__ import annotations

import os
import re
import math
from typing import Any

import asyncpg
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

def kst_today_ymd() -> str:
    return datetime.now(tz=KST).strftime("%Y-%m-%d")

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# ---- DB bootstrap (compatible with bot schema) ----
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

CREATE TABLE IF NOT EXISTS level_roles (
  guild_id BIGINT NOT NULL,
  level INTEGER NOT NULL,
  add_role_id BIGINT NOT NULL,
  remove_role_id BIGINT NULL,
  PRIMARY KEY (guild_id, level)
);
"""

MIGRATIONS_SQL = [
    # streak settings
    "ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS checkin_streak_bonus_per_day INTEGER NOT NULL DEFAULT 0;",
    "ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS checkin_streak_bonus_cap INTEGER NOT NULL DEFAULT 0;",
    # streak table
    """CREATE TABLE IF NOT EXISTS checkin_streaks (
        guild_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        last_ymd TEXT NULL,
        streak INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id)
    );""",
    # member cache extensions
    "ALTER TABLE guild_members_cache ADD COLUMN IF NOT EXISTS in_guild BOOLEAN NOT NULL DEFAULT TRUE;",
    "ALTER TABLE guild_members_cache ADD COLUMN IF NOT EXISTS role_ids BIGINT[] NOT NULL DEFAULT '{}'::BIGINT[];",
# channels cache (for reaction lock UI)
"""CREATE TABLE IF NOT EXISTS guild_channels_cache (
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    channel_name TEXT NOT NULL,
    channel_type INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, channel_id)
);""",
# reaction blocks
"""CREATE TABLE IF NOT EXISTS reaction_blocks (
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    blocked_role_id BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, message_id, blocked_role_id)
);""",
"CREATE INDEX IF NOT EXISTS idx_reaction_blocks_guild ON reaction_blocks (guild_id);",
"CREATE INDEX IF NOT EXISTS idx_reaction_blocks_message ON reaction_blocks (guild_id, message_id);",
    # v2 rules
    """CREATE TABLE IF NOT EXISTS level_role_sets (
        guild_id BIGINT NOT NULL,
        level INTEGER NOT NULL,
        add_role_ids BIGINT[] NOT NULL DEFAULT '{}'::BIGINT[],
        remove_role_ids BIGINT[] NOT NULL DEFAULT '{}'::BIGINT[],
        PRIMARY KEY (guild_id, level)
    );""",
    """INSERT INTO level_role_sets (guild_id, level, add_role_ids, remove_role_ids)
       SELECT guild_id, level,
              ARRAY[add_role_id]::BIGINT[],
              CASE WHEN remove_role_id IS NULL THEN '{}'::BIGINT[] ELSE ARRAY[remove_role_id]::BIGINT[] END
       FROM level_roles
       ON CONFLICT (guild_id, level) DO NOTHING;""",
    # role sync queue
    """CREATE TABLE IF NOT EXISTS role_sync_queue (
        guild_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        processed_at TIMESTAMPTZ NULL,
        PRIMARY KEY (guild_id, user_id)
    );""",
]

def _env(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or default)

def _parse_ids(text: str) -> list[int]:
    if not text:
        return []
    ids = re.findall(r"(\d{10,25})", text)
    out: list[int] = []
    for s in ids:
        try:
            out.append(int(s))
        except Exception:
            pass
    # unique preserve order
    seen=set()
    uniq=[]
    for i in out:
        if i not in seen:
            seen.add(i); uniq.append(i)
    return uniq

async def _ensure_pool(app: FastAPI) -> asyncpg.Pool:
    pool = app.state.pool
    if pool:
        return pool
    raise RuntimeError("DB pool not initialized")

async def _ensure_guild_settings(pool: asyncpg.Pool, guild_id: int) -> None:
    await pool.execute("INSERT INTO guild_settings (guild_id) VALUES ($1) ON CONFLICT (guild_id) DO NOTHING", guild_id)

async def _get_settings(pool: asyncpg.Pool, guild_id: int) -> dict[str, Any]:
    await _ensure_guild_settings(pool, guild_id)
    row = await pool.fetchrow("SELECT * FROM guild_settings WHERE guild_id=$1", guild_id)
    return dict(row) if row else {}

async def _update_settings(pool: asyncpg.Pool, guild_id: int, **updates: Any) -> None:
    if not updates:
        return
    await _ensure_guild_settings(pool, guild_id)
    cols = list(updates.keys())
    vals = list(updates.values())
    sets = ", ".join([f"{c}=${i+2}" for i, c in enumerate(cols)])
    sql = f"UPDATE guild_settings SET {sets} WHERE guild_id=$1"
    await pool.execute(sql, guild_id, *vals)

def _require_admin(request: Request) -> bool:
    return bool(request.session.get("admin_ok"))

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=_env("DASHBOARD_SESSION_SECRET", "change-me-please"))

@app.on_event("startup")
async def startup():
    db_url = _env("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL 환경변수가 필요합니다.")
    pool = await asyncpg.create_pool(dsn=db_url, min_size=1, max_size=5, command_timeout=30)
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
        for q in MIGRATIONS_SQL:
            try:
                await conn.execute(q)
            except Exception:
                # some ALTERs may fail if permissions; ignore
                pass
    app.state.pool = pool

@app.on_event("shutdown")
async def shutdown():
    pool = getattr(app.state, "pool", None)
    if pool:
        await pool.close()

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return TEMPLATES.TemplateResponse("index.html", {"request": request, "admin_ok": _require_admin(request)})

@app.post("/admin-login")
async def admin_login(request: Request, password: str = Form(...)):
    pw = _env("DASHBOARD_ADMIN_PASSWORD", "")
    if not pw:
        return JSONResponse({"ok": False, "msg": "서버에 DASHBOARD_ADMIN_PASSWORD가 설정되어 있지 않아요."}, status_code=500)
    if password != pw:
        return JSONResponse({"ok": False, "msg": "비밀번호가 틀렸어요."}, status_code=401)
    request.session["admin_ok"] = True
    return RedirectResponse(url="/admin", status_code=302)

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, guild_id: str | None = None, msg: str | None = None):
    if not _require_admin(request):
        return RedirectResponse(url="/", status_code=302)

    pool = await _ensure_pool(app)

    gid = int(guild_id) if guild_id and str(guild_id).isdigit() else None
    settings = None
    roles = []
    channels = []
    reaction_blocks = []
    rules = []
    top_rows = []
    total_members = 0

    if gid:
        settings = await _get_settings(pool, gid)
        roles = await pool.fetch(
            "SELECT role_id, role_name, position FROM guild_roles_cache WHERE guild_id=$1 ORDER BY position DESC, role_name ASC",
            gid,
        )

        channels = await pool.fetch(
            "SELECT channel_id, channel_name, channel_type FROM guild_channels_cache WHERE guild_id=$1 ORDER BY channel_type, channel_name",
            gid,
        )
        reaction_blocks = await pool.fetch(
            "SELECT channel_id, message_id, blocked_role_id FROM reaction_blocks WHERE guild_id=$1 ORDER BY message_id, blocked_role_id",
            gid,
        )

        rules = await pool.fetch(
            "SELECT level, add_role_ids, remove_role_ids FROM level_role_sets WHERE guild_id=$1 ORDER BY level",
            gid,
        )
        # top 10 (include 0 XP members too)
        top_rows = await pool.fetch(
            """SELECT m.user_id,
                      COALESCE(s.xp,0) AS xp,
                      m.display_name, m.nick, m.global_name, m.username, m.discriminator
               FROM guild_members_cache m
               LEFT JOIN user_stats s ON s.guild_id=m.guild_id AND s.user_id=m.user_id
               WHERE m.guild_id=$1 AND m.in_guild=TRUE
               ORDER BY COALESCE(s.xp,0) DESC, m.user_id
               LIMIT 10""",
            gid,
        )
        total_members = int(await pool.fetchval("SELECT COUNT(*) FROM guild_members_cache WHERE guild_id=$1 AND in_guild=TRUE", gid) or 0)

    # role name map for rule rendering
    role_name_map = {int(r["role_id"]): str(r["role_name"]) for r in roles}
    channel_name_map = {int(c["channel_id"]): str(c["channel_name"]) for c in channels}

    return TEMPLATES.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "guild_id": gid,
            "settings": settings,
            "roles": roles,
            "channels": channels,
            "reaction_blocks": reaction_blocks,
            "rules": rules,
            "role_name_map": role_name_map,
            "channel_name_map": channel_name_map,
            "top_rows": top_rows,
            "total_members": total_members,
            "msg": msg,
        },
    )

@app.post("/admin/save-settings")
async def save_settings(
    request: Request,
    guild_id: int = Form(...),
    checkin_xp: int = Form(...),
    checkin_limit_enabled: str = Form("on"),
    message_xp: int = Form(...),
    message_cooldown_sec: int = Form(...),
    voice_xp_per_min: int = Form(...),
    checkin_streak_bonus_per_day: int = Form(0),
    checkin_streak_bonus_cap: int = Form(0),
):
    if not _require_admin(request):
        return RedirectResponse(url="/", status_code=302)
    pool = await _ensure_pool(app)
    await _update_settings(
        pool,
        int(guild_id),
        checkin_xp=int(checkin_xp),
        checkin_limit_enabled=(checkin_limit_enabled == "on"),
        message_xp=int(message_xp),
        message_cooldown_sec=int(message_cooldown_sec),
        voice_xp_per_min=int(voice_xp_per_min),
        checkin_streak_bonus_per_day=int(checkin_streak_bonus_per_day),
        checkin_streak_bonus_cap=int(checkin_streak_bonus_cap),
    )
    return RedirectResponse(url=f"/admin?guild_id={guild_id}&msg=설정+저장+완료", status_code=302)

async def _resolve_target_user_ids(pool: asyncpg.Pool, guild_id: int, target_type: str, user_id: str, role_id: str) -> list[int]:
    if target_type == "role":
        rid = int(role_id)
        rows = await pool.fetch(
            "SELECT user_id FROM guild_members_cache WHERE guild_id=$1 AND in_guild=TRUE AND $2 = ANY(role_ids)",
            int(guild_id), int(rid)
        )
        return [int(r["user_id"]) for r in rows]
    # default user
    if not str(user_id).isdigit():
        return []
    return [int(user_id)]

@app.post("/admin/quick-reset-checkin")
async def quick_reset_checkin(
    request: Request,
    guild_id: int = Form(...),
    target_type: str = Form("user"),
    user_id: str = Form(""),
    role_id: str = Form(""),
    ymd: str = Form(""),
):
    if not _require_admin(request):
        return RedirectResponse(url="/", status_code=302)
    pool = await _ensure_pool(app)
    ymd = ymd.strip() or kst_today_ymd()
    user_ids = await _resolve_target_user_ids(pool, int(guild_id), target_type, user_id, role_id)
    if not user_ids:
        return RedirectResponse(url=f"/admin?guild_id={guild_id}&msg=대상+유저가+없어요", status_code=302)

    await pool.execute(
        "DELETE FROM checkins WHERE guild_id=$1 AND ymd=$2 AND user_id = ANY($3::BIGINT[])",
        int(guild_id), ymd, user_ids
    )
    return RedirectResponse(url=f"/admin?guild_id={guild_id}&msg=출석+초기화+완료({len(user_ids)}명)", status_code=302)

@app.post("/admin/quick-set-level")
async def quick_set_level(
    request: Request,
    guild_id: int = Form(...),
    target_type: str = Form("user"),
    user_id: str = Form(""),
    role_id: str = Form(""),
    level: int = Form(...),
):
    if not _require_admin(request):
        return RedirectResponse(url="/", status_code=302)
    pool = await _ensure_pool(app)
    user_ids = await _resolve_target_user_ids(pool, int(guild_id), target_type, user_id, role_id)
    if not user_ids:
        return RedirectResponse(url=f"/admin?guild_id={guild_id}&msg=대상+유저가+없어요", status_code=302)

    xp = max(0, int(level)) * 100

    # Bulk upsert XP
    await pool.execute(
        """INSERT INTO user_stats (guild_id, user_id, xp)
           SELECT $1, uid, $3 FROM UNNEST($2::BIGINT[]) AS uid
           ON CONFLICT (guild_id, user_id) DO UPDATE SET xp=EXCLUDED.xp""",
        int(guild_id), user_ids, int(xp)
    )

    # Request Discord role re-sync (bot worker will process)
    await pool.execute(
        """INSERT INTO role_sync_queue (guild_id, user_id, requested_at, processed_at)
           SELECT $1, uid, NOW(), NULL FROM UNNEST($2::BIGINT[]) AS uid
           ON CONFLICT (guild_id, user_id) DO UPDATE SET requested_at=NOW(), processed_at=NULL""",
        int(guild_id), user_ids
    )

    return RedirectResponse(url=f"/admin?guild_id={guild_id}&msg=레벨+적용+완료({len(user_ids)}명)+-+역할동기화+대기", status_code=302)

@app.post("/admin/rules-upsert")
async def rules_upsert(
    request: Request,
    guild_id: int = Form(...),
    level: int = Form(...),
    add_role_ids: str = Form(""),
    remove_role_ids: str = Form(""),
):
    if not _require_admin(request):
        return RedirectResponse(url="/", status_code=302)
    pool = await _ensure_pool(app)
    add_ids = _parse_ids(add_role_ids)
    rem_ids = _parse_ids(remove_role_ids)
    if not add_ids and not rem_ids:
        return RedirectResponse(url=f"/admin?guild_id={guild_id}&msg=추가/제거+역할+중+하나는+필수", status_code=302)

    await pool.execute(
        """INSERT INTO level_role_sets (guild_id, level, add_role_ids, remove_role_ids)
           VALUES ($1,$2,$3,$4)
           ON CONFLICT (guild_id, level) DO UPDATE SET add_role_ids=$3, remove_role_ids=$4""",
        int(guild_id), int(level), add_ids, rem_ids
    )

    # optional: request role sync for all members? too heavy; keep manual
    return RedirectResponse(url=f"/admin?guild_id={guild_id}&msg=규칙+저장+완료", status_code=302)

@app.post("/admin/rules-delete")
async def rules_delete(request: Request, guild_id: int = Form(...), level: int = Form(...)):
    if not _require_admin(request):
        return RedirectResponse(url="/", status_code=302)
    pool = await _ensure_pool(app)
    await pool.execute("DELETE FROM level_role_sets WHERE guild_id=$1 AND level=$2", int(guild_id), int(level))
    return RedirectResponse(url=f"/admin?guild_id={guild_id}&msg=규칙+삭제+완료", status_code=302)

@app.post("/admin/reaction-block-add")
async def reaction_block_add(
    request: Request,
    guild_id: int = Form(...),
    channel_id: str = Form(...),
    message: str = Form(...),
    blocked_role_ids: str = Form(""),
):
    if not _require_admin(request):
        return RedirectResponse(url="/", status_code=302)
    pool = await _ensure_pool(app)
    # parse
    mids = _parse_ids(message)
    if not mids:
        return RedirectResponse(url=f"/admin?guild_id={guild_id}&msg=메시지+ID(또는+링크)를+입력하세요", status_code=302)
    msg_id = int(mids[0])
    if not str(channel_id).isdigit():
        return RedirectResponse(url=f"/admin?guild_id={guild_id}&msg=채널을+선택하세요", status_code=302)
    cid = int(channel_id)
    rids = _parse_ids(blocked_role_ids)
    if not rids:
        return RedirectResponse(url=f"/admin?guild_id={guild_id}&msg=차단할+역할을+선택하세요", status_code=302)

    await pool.execute(
        """INSERT INTO reaction_blocks (guild_id, channel_id, message_id, blocked_role_id, updated_at)
           SELECT $1, $2, $3, rid, NOW() FROM UNNEST($4::BIGINT[]) AS rid
           ON CONFLICT (guild_id, message_id, blocked_role_id)
           DO UPDATE SET channel_id=EXCLUDED.channel_id, updated_at=NOW()""",
        int(guild_id), int(cid), int(msg_id), rids
    )
    return RedirectResponse(url=f"/admin?guild_id={guild_id}&msg=반응차단+저장+완료({len(rids)}개)", status_code=302)

@app.post("/admin/reaction-block-delete")
async def reaction_block_delete(
    request: Request,
    guild_id: int = Form(...),
    message_id: int = Form(...),
    role_id: int = Form(...),
):
    if not _require_admin(request):
        return RedirectResponse(url="/", status_code=302)
    pool = await _ensure_pool(app)
    await pool.execute(
        "DELETE FROM reaction_blocks WHERE guild_id=$1 AND message_id=$2 AND blocked_role_id=$3",
        int(guild_id), int(message_id), int(role_id)
    )
    return RedirectResponse(url=f"/admin?guild_id={guild_id}&msg=반응차단+삭제+완료", status_code=302)

# ---- APIs for UI (DB cache only, no Discord REST) ----
@app.get("/admin/api/roles_cache")
async def api_roles_cache(request: Request, guild_id: int):
    if not _require_admin(request):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    pool = await _ensure_pool(app)
    rows = await pool.fetch(
        "SELECT role_id, role_name, position FROM guild_roles_cache WHERE guild_id=$1 ORDER BY position DESC, role_name ASC",
        int(guild_id)
    )
    return {"ok": True, "roles": [dict(r) for r in rows]}

@app.get("/admin/api/members_search")
async def api_members_search(request: Request, guild_id: int, q: str):
    if not _require_admin(request):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    q = (q or "").strip()
    if len(q) < 1:
        return {"ok": True, "members": []}
    pool = await _ensure_pool(app)
    q_like = f"%{q}%"
    rows = await pool.fetch(
        """SELECT user_id, username, discriminator, global_name, nick, display_name
           FROM guild_members_cache
           WHERE guild_id=$1 AND in_guild=TRUE AND (
                display_name ILIKE $2 OR nick ILIKE $2 OR global_name ILIKE $2 OR username ILIKE $2 OR CAST(user_id AS TEXT) LIKE $3
           )
           ORDER BY updated_at DESC
           LIMIT 50""",
        int(guild_id), q_like, f"%{q}%"
    )
    return {"ok": True, "members": [dict(r) for r in rows]}

@app.get("/admin/api/members_list")
async def api_members_list(request: Request, guild_id: int, limit: int = 200, offset: int = 0):
    if not _require_admin(request):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    pool = await _ensure_pool(app)
    rows = await pool.fetch(
        """SELECT user_id, username, discriminator, global_name, nick, display_name
           FROM guild_members_cache
           WHERE guild_id=$1 AND in_guild=TRUE
           ORDER BY display_name NULLS LAST, user_id
           LIMIT $2 OFFSET $3""",
        int(guild_id), int(limit), int(offset)
    )
    total = int(await pool.fetchval("SELECT COUNT(*) FROM guild_members_cache WHERE guild_id=$1 AND in_guild=TRUE", int(guild_id)) or 0)
    return {"ok": True, "members": [dict(r) for r in rows], "total": total}

@app.get("/admin/api/members_by_role")
async def api_members_by_role(request: Request, guild_id: int, role_id: int):
    if not _require_admin(request):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    pool = await _ensure_pool(app)
    rows = await pool.fetch(
        "SELECT user_id, username, discriminator, global_name, nick, display_name FROM guild_members_cache WHERE guild_id=$1 AND in_guild=TRUE AND $2 = ANY(role_ids) ORDER BY display_name NULLS LAST, user_id LIMIT 500",
        int(guild_id), int(role_id)
    )
    return {"ok": True, "members": [dict(r) for r in rows]}

# Health check
@app.get("/healthz")
async def healthz():
    return {"ok": True}
