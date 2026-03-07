from __future__ import annotations

import os
import re
import math
import io
import uuid
import logging
from typing import Any

import asyncpg
import requests
from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from PIL import Image

try:
    import emoji as emoji_lib
except Exception:
    emoji_lib = None

KST = ZoneInfo("Asia/Seoul")
log = logging.getLogger(__name__)


def kst_today_ymd() -> str:
    return datetime.now(tz=KST).strftime("%Y-%m-%d")

def _parse_bigint_list_text(text: str | None) -> list[int]:
    ids = re.findall(r"(\d{10,25})", str(text or ""))
    out = []
    seen = set()
    for s in ids:
        try:
            v = int(s)
        except Exception:
            continue
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _safe_image_bytes(raw: bytes, *, max_bytes: int = 8 * 1024 * 1024) -> bytes:
    if not raw:
        raise ValueError("이미지 데이터가 비어 있어요.")
    if len(raw) > max_bytes:
        raise ValueError("이미지 파일이 너무 커요. 8MB 이하만 올려주세요.")
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im.verify()
    except Exception as e:
        raise ValueError("지원하지 않는 이미지 파일이에요.") from e
    return raw


def _build_public_url(request: Request, path: str) -> str:
    base = str(request.base_url).rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return base + path


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

CREATE TABLE IF NOT EXISTS guilds_cache (
  guild_id BIGINT NOT NULL PRIMARY KEY,
  guild_name TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
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
"ALTER TABLE guild_members_cache ADD COLUMN IF NOT EXISTS avatar_url TEXT NULL;",
    "CREATE INDEX IF NOT EXISTS idx_guild_members_cache_role_ids ON guild_members_cache USING GIN (role_ids);",
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
"""CREATE TABLE IF NOT EXISTS guilds_cache (
    guild_id BIGINT NOT NULL PRIMARY KEY,
    guild_name TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);""",
"CREATE INDEX IF NOT EXISTS idx_reaction_blocks_message ON reaction_blocks (guild_id, message_id);",

# community feature settings
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_channel_id BIGINT NOT NULL DEFAULT 0;",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS goodbye_channel_id BIGINT NOT NULL DEFAULT 0;",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_enabled BOOLEAN NOT NULL DEFAULT FALSE;",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS goodbye_enabled BOOLEAN NOT NULL DEFAULT FALSE;",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_message_template TEXT NOT NULL DEFAULT '환영합니다 [user]!';",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS goodbye_message_template TEXT NOT NULL DEFAULT '[user] 님이 서버를 떠났습니다.';",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_image_enabled BOOLEAN NOT NULL DEFAULT FALSE;",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_background_url TEXT NOT NULL DEFAULT '';",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_avatar_shape TEXT NOT NULL DEFAULT 'circle';",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_avatar_x INTEGER NOT NULL DEFAULT 40;",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_avatar_y INTEGER NOT NULL DEFAULT 40;",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_avatar_w INTEGER NOT NULL DEFAULT 128;",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_avatar_h INTEGER NOT NULL DEFAULT 128;",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_text_template TEXT NOT NULL DEFAULT '[user]';",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_text_x INTEGER NOT NULL DEFAULT 200;",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_text_y INTEGER NOT NULL DEFAULT 80;",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_text_font_size INTEGER NOT NULL DEFAULT 40;",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_text_color TEXT NOT NULL DEFAULT '#ffffff';",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_text_align TEXT NOT NULL DEFAULT 'left';",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_text_font_name TEXT NOT NULL DEFAULT 'default';",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_text_box_width INTEGER NOT NULL DEFAULT 500;",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_text2_template TEXT NOT NULL DEFAULT '';",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_text2_x INTEGER NOT NULL DEFAULT 200;",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_text2_y INTEGER NOT NULL DEFAULT 140;",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_text2_font_size INTEGER NOT NULL DEFAULT 32;",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_text2_color TEXT NOT NULL DEFAULT '#ffffff';",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_text2_align TEXT NOT NULL DEFAULT 'left';",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_text2_font_name TEXT NOT NULL DEFAULT 'default';",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_text2_box_width INTEGER NOT NULL DEFAULT 500;",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_text3_template TEXT NOT NULL DEFAULT '';",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_text3_x INTEGER NOT NULL DEFAULT 200;",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_text3_y INTEGER NOT NULL DEFAULT 200;",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_text3_font_size INTEGER NOT NULL DEFAULT 28;",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_text3_color TEXT NOT NULL DEFAULT '#ffffff';",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_text3_align TEXT NOT NULL DEFAULT 'left';",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_text3_font_name TEXT NOT NULL DEFAULT 'default';",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_text3_box_width INTEGER NOT NULL DEFAULT 500;",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS invite_block_channel_ids BIGINT[] NOT NULL DEFAULT '{}'::BIGINT[];",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS bot_only_channel_ids BIGINT[] NOT NULL DEFAULT '{}'::BIGINT[];",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS notify_channel_id BIGINT NOT NULL DEFAULT 0;",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS voice_afk_disconnect_enabled BOOLEAN NOT NULL DEFAULT FALSE;",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS voice_afk_disconnect_delay_sec INTEGER NOT NULL DEFAULT 60;",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS leaderboard_channel_id BIGINT NOT NULL DEFAULT 0;",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS leaderboard_message_id BIGINT NOT NULL DEFAULT 0;",
"ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS voice_dm_summary_enabled BOOLEAN NOT NULL DEFAULT TRUE;",
"""CREATE TABLE IF NOT EXISTS invite_cache (
    guild_id BIGINT NOT NULL,
    code TEXT NOT NULL,
    inviter_id BIGINT NOT NULL DEFAULT 0,
    uses INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, code)
);""",
    """CREATE TABLE IF NOT EXISTS reaction_role_rules (
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    emoji_key TEXT NOT NULL,
    add_role_ids BIGINT[] NOT NULL DEFAULT '{}'::BIGINT[],
    remove_role_ids BIGINT[] NOT NULL DEFAULT '{}'::BIGINT[],
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, message_id, emoji_key)
);""",
    "CREATE INDEX IF NOT EXISTS idx_reaction_role_rules_guild ON reaction_role_rules (guild_id);",
    "CREATE INDEX IF NOT EXISTS idx_reaction_role_rules_message ON reaction_role_rules (guild_id, message_id);",
"""CREATE TABLE IF NOT EXISTS dashboard_media (
    media_id TEXT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    data BYTEA NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);""",
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



def _normalize_unicode_emoji(text: str) -> str:
    t = str(text or "").strip()
    # Remove variation selectors so ❄ and ❄️ compare the same.
    return t.replace("\ufe0f", "").replace("\ufe0e", "")

def _parse_emoji_key(text: str) -> str:
    """Return canonical emoji key.
    - Custom: <a:name:id> or <:name:id> -> name:id
    - Discord/Slack style alias: :snowflake: -> actual unicode emoji when possible
    - Unicode: normalized (variation selectors removed)
    """
    t = (text or "").strip()
    m = re.search(r"<a?:([\w\-]+):(\d{10,25})>", t)
    if m:
        return f"{m.group(1)}:{m.group(2)}"
    if emoji_lib is not None and re.fullmatch(r":[^:\s]+:", t):
        try:
            converted = emoji_lib.emojize(t, language='alias')
            if converted and converted != t:
                return _normalize_unicode_emoji(converted)
        except Exception:
            pass
    return _normalize_unicode_emoji(t)


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


def _safe_selected_guild_name(guilds: Sequence[Mapping[str, Any]], gid: int | None) -> str:
    if not gid:
        return ""
    for g in guilds or []:
        try:
            if int((g.get("guild_id") if hasattr(g, "get") else g["guild_id"])) == int(gid):
                name = g.get("guild_name") if hasattr(g, "get") else g["guild_name"]
                return str(name or gid)
        except Exception:
            continue
    return str(gid)

def _textish_channels(channels: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Best-effort filter for message-capable channels.
    Falls back to all channels if type info is missing.
    """
    out: list[Mapping[str, Any]] = []
    for c in channels or []:
        try:
            t = c.get("channel_type") if hasattr(c, "get") else c["channel_type"]
        except Exception:
            out.append(c)
            continue
        try:
            ti = int(t)
        except Exception:
            ti = None
        if ti in (0, 5, 10, 11, 12, 15, 16):
            out.append(c)
    return out or list(channels or [])

async def _enqueue_all_members_role_sync(pool: asyncpg.Pool, guild_id: int) -> int:
    try:
        return int(await pool.execute(
            """INSERT INTO role_sync_queue (guild_id, user_id, requested_at, processed_at)
                   SELECT $1, user_id, NOW(), NULL
                   FROM guild_members_cache
                   WHERE guild_id=$1 AND in_guild=TRUE
                   ON CONFLICT (guild_id, user_id)
                   DO UPDATE SET requested_at=NOW(), processed_at=NULL""",
            int(guild_id)
        ).split()[-1] or 0)
    except Exception:
        # fallback: just avoid breaking dashboard save flow
        return 0


async def _ensure_pool(app: FastAPI) -> asyncpg.Pool:
    pool = app.state.pool
    if pool:
        return pool
    raise RuntimeError("DB pool not initialized")


async def _list_guilds_for_admin(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    rows: list[Any] = []
    candidates = [
        "SELECT guild_id, guild_name FROM guilds_cache",
        "SELECT guild_id, NULL::TEXT AS guild_name FROM guild_settings",
        "SELECT DISTINCT guild_id, NULL::TEXT AS guild_name FROM guild_roles_cache",
        "SELECT DISTINCT guild_id, NULL::TEXT AS guild_name FROM guild_members_cache",
        "SELECT DISTINCT guild_id, NULL::TEXT AS guild_name FROM guild_channels_cache",
        "SELECT DISTINCT guild_id, NULL::TEXT AS guild_name FROM level_roles",
        "SELECT DISTINCT guild_id, NULL::TEXT AS guild_name FROM reaction_blocks",
        "SELECT DISTINCT guild_id, NULL::TEXT AS guild_name FROM reaction_role_rules",
        "SELECT DISTINCT guild_id, NULL::TEXT AS guild_name FROM user_stats",
    ]
    out_map: dict[int, dict[str, Any]] = {}
    for sql in candidates:
        try:
            rows = await _fetch_optional(pool, sql)
        except Exception:
            rows = []
        for r in rows or []:
            try:
                rd = dict(r)
            except Exception:
                try:
                    rd = {k: r[k] for k in r.keys()}  # asyncpg.Record
                except Exception:
                    rd = {}
            try:
                gid = int(rd.get("guild_id"))
            except Exception:
                continue
            name = str(rd.get("guild_name") or "").strip()
            item = out_map.get(gid) or {"guild_id": gid, "guild_name": ""}
            if name and not item.get("guild_name"):
                item["guild_name"] = name
            out_map[gid] = item

    out = list(out_map.values())
    out.sort(key=lambda x: (0 if x.get("guild_name") else 1, str(x.get("guild_name") or ""), int(x["guild_id"])))
    return out

async def _ensure_guild_settings(pool: asyncpg.Pool, guild_id: int) -> None:
    await pool.execute("INSERT INTO guild_settings (guild_id) VALUES ($1) ON CONFLICT (guild_id) DO NOTHING", guild_id)

async def _get_settings(pool: asyncpg.Pool, guild_id: int) -> dict[str, Any]:
    try:
        await _ensure_guild_settings(pool, guild_id)
        row = await _fetchrow_optional(pool, "SELECT * FROM guild_settings WHERE guild_id=$1", guild_id)
        return dict(row) if row else {}
    except Exception as e:
        log.exception("Failed to load guild settings for guild=%s: %s", guild_id, e)
        return {}

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

def _is_missing_db_schema_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    needles = [
        "undefinedcolumn", "undefinedtable", "does not exist", "column", "relation",
        "in_guild", "avatar_url", "guild_channels_cache", "dashboard_media", "level_role_sets",
        "reaction_role_rules", "role_sync_queue"
    ]
    return any(n in msg for n in needles)

async def _fetch_optional(pool: asyncpg.Pool, sql: str, *args):
    try:
        return await pool.fetch(sql, *args)
    except Exception as e:
        if _is_missing_db_schema_error(e):
            log.warning("Optional query failed, returning empty rows: %s | sql=%s", e, sql)
            return []
        raise

async def _fetchrow_optional(pool: asyncpg.Pool, sql: str, *args):
    try:
        return await pool.fetchrow(sql, *args)
    except Exception as e:
        if _is_missing_db_schema_error(e):
            log.warning("Optional fetchrow failed, returning None: %s | sql=%s", e, sql)
            return None
        raise

async def _fetchval_optional(pool: asyncpg.Pool, sql: str, *args, default=None):
    try:
        return await pool.fetchval(sql, *args)
    except Exception as e:
        if _is_missing_db_schema_error(e):
            log.warning("Optional fetchval failed, returning default: %s | sql=%s", e, sql)
            return default
        raise

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
async def admin_page(request: Request, guild_id: str | None = None, msg: str | None = None, rank_page: int = 1):
    if not _require_admin(request):
        return RedirectResponse(url="/", status_code=302)

    pool = await _ensure_pool(app)

    guilds = await _list_guilds_for_admin(pool)
    gid = int(guild_id) if guild_id and str(guild_id).isdigit() else None
    log.info("Admin page open guild_id=%s", gid)
    settings: dict[str, Any] | None = None
    roles = []
    channels = []
    reaction_blocks = []
    reaction_role_rules = []
    rules = []

    # ranking
    rank_rows = []
    rank_total = 0
    rank_page_size = 50

    if gid:
        settings = await _get_settings(pool, gid)
        roles = await _fetch_optional(
            pool,
            "SELECT role_id, role_name, position FROM guild_roles_cache WHERE guild_id=$1 ORDER BY position DESC, role_name ASC",
            gid,
        )
        channels = await _fetch_optional(
            pool,
            "SELECT channel_id, channel_name, channel_type FROM guild_channels_cache WHERE guild_id=$1 ORDER BY channel_type, channel_name",
            gid,
        )
        reaction_blocks = await _fetch_optional(
            pool,
            "SELECT channel_id, message_id, blocked_role_id FROM reaction_blocks WHERE guild_id=$1 ORDER BY message_id, blocked_role_id",
            gid,
        )
        reaction_role_rules = await _fetch_optional(
            pool,
            "SELECT channel_id, message_id, emoji_key, add_role_ids, remove_role_ids FROM reaction_role_rules WHERE guild_id=$1 ORDER BY message_id, emoji_key",
            gid,
        )
        rules = await _fetch_optional(
            pool,
            "SELECT level, add_role_ids, remove_role_ids FROM level_role_sets WHERE guild_id=$1 ORDER BY level",
            gid,
        )

        rp = max(1, int(rank_page or 1))
        off = (rp - 1) * rank_page_size
        rank_total = int(await _fetchval_optional(
            pool,
            "SELECT COUNT(*) FROM guild_members_cache WHERE guild_id=$1 AND in_guild=TRUE",
            gid,
            default=0,
        ) or 0)
        rank_rows = await _fetch_optional(
            pool,
            """SELECT m.user_id,
                      COALESCE(s.xp,0) AS xp,
                      m.display_name, m.nick, m.global_name, m.username, m.discriminator
               FROM guild_members_cache m
               LEFT JOIN user_stats s ON s.guild_id=m.guild_id AND s.user_id=m.user_id
               WHERE m.guild_id=$1 AND m.in_guild=TRUE
               ORDER BY COALESCE(s.xp,0) DESC, m.user_id
               LIMIT $2 OFFSET $3""",
            gid, rank_page_size, off,
        )

    # Normalize stored emoji keys for display so :snowflake: shows as actual emoji when possible.
    display_reaction_role_rules = []
    for rr in reaction_role_rules:
        item = dict(rr)
        item["emoji_display"] = _parse_emoji_key(str(item.get("emoji_key") or ""))
        display_reaction_role_rules.append(item)

    role_name_map = {int(r["role_id"]): str(r["role_name"]) for r in roles}
    channel_name_map = {int(c["channel_id"]): str(c["channel_name"]) for c in channels}
    text_channels = _textish_channels(channels)

    # date suggestions (last 90 days)
    recent_dates = []
    try:
        today = datetime.now(tz=KST).date()
        for i in range(0, 90):
            recent_dates.append((today - timedelta(days=i)).strftime("%Y-%m-%d"))
    except Exception:
        recent_dates = []

    pending_role_sync = 0
    if gid:
        try:
            pending_role_sync = int(await _fetchval_optional(
                pool,
                "SELECT COUNT(*) FROM role_sync_queue WHERE guild_id=$1 AND processed_at IS NULL",
                gid,
                default=0,
            ) or 0)
        except Exception:
            pending_role_sync = 0

    return TEMPLATES.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "guilds": guilds,
            "guild_id": gid,
            "settings": settings,
            "roles": roles,
            "channels": channels,
            "text_channels": text_channels,
            "guild_name": _safe_selected_guild_name(guilds, gid),
            "reaction_blocks": reaction_blocks,
            "reaction_role_rules": display_reaction_role_rules,
            "rules": rules,
            "role_name_map": role_name_map,
            "channel_name_map": channel_name_map,
            "rank_rows": rank_rows,
            "rank_total": rank_total,
            "rank_page": max(1, int(rank_page or 1)),
            "rank_page_size": rank_page_size,
            "pending_role_sync": pending_role_sync,
            "recent_dates": recent_dates,
            "msg": msg,
        },
    )



@app.post("/admin/api/welcome-image-upload")
async def api_welcome_image_upload(request: Request, guild_id: int = Form(...), image: UploadFile = File(...)):
    if not _require_admin(request):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    pool = await _ensure_pool(app)
    raw = await image.read()
    try:
        raw = _safe_image_bytes(raw)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    ctype = str(image.content_type or "application/octet-stream")
    if not ctype.startswith("image/"):
        ctype = "image/png"
    media_id = uuid.uuid4().hex
    filename = str(image.filename or "welcome-image")
    await pool.execute(
        "INSERT INTO dashboard_media (media_id, guild_id, filename, content_type, data) VALUES ($1,$2,$3,$4,$5)",
        media_id, int(guild_id), filename, ctype, raw
    )
    url = _build_public_url(request, f"/media/{media_id}")
    return {"ok": True, "media_id": media_id, "url": url, "filename": filename}


@app.get("/media/{media_id}")
async def media_get(media_id: str):
    pool = await _ensure_pool(app)
    row = await pool.fetchrow(
        "SELECT content_type, data, filename FROM dashboard_media WHERE media_id=$1",
        str(media_id)
    )
    if not row:
        return Response(status_code=404)
    headers = {"Cache-Control": "public, max-age=86400"}
    return Response(content=bytes(row["data"]), media_type=str(row["content_type"] or "application/octet-stream"), headers=headers)


@app.get("/admin/api/welcome-preview-member")
async def api_welcome_preview_member(request: Request, guild_id: int, user_id: str | None = None):
    if not _require_admin(request):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    pool = await _ensure_pool(app)
    row = None
    if user_id and str(user_id).isdigit():
        row = await pool.fetchrow(
            """SELECT user_id, username, discriminator, global_name, nick, display_name, avatar_url
               FROM guild_members_cache
               WHERE guild_id=$1 AND user_id=$2
               LIMIT 1""",
            int(guild_id), int(user_id)
        )
    if row is None:
        row = await pool.fetchrow(
            """SELECT user_id, username, discriminator, global_name, nick, display_name, avatar_url
               FROM guild_members_cache
               WHERE guild_id=$1
               ORDER BY updated_at DESC
               LIMIT 1""",
            int(guild_id)
        )
    if row is None:
        return {"ok": False, "error": "member_not_found"}
    item = dict(row)
    item["user_id"] = str(item["user_id"])
    return {"ok": True, "member": item}

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
    welcome_enabled: str = Form("off"),
    welcome_channel_id: str = Form(""),
    welcome_message_template: str = Form("환영합니다 [user]!"),
    goodbye_enabled: str = Form("off"),
    goodbye_channel_id: str = Form(""),
    goodbye_message_template: str = Form("[user] 님이 서버를 떠났습니다."),
    welcome_image_enabled: str = Form("off"),
    welcome_background_url: str = Form(""),
    welcome_avatar_shape: str = Form("circle"),
    welcome_avatar_x: int = Form(40),
    welcome_avatar_y: int = Form(40),
    welcome_avatar_w: int = Form(128),
    welcome_avatar_h: int = Form(128),
    welcome_text_template: str = Form("[user]"),
    welcome_text_x: int = Form(200),
    welcome_text_y: int = Form(80),
    welcome_text_font_size: int = Form(40),
    welcome_text_color: str = Form("#ffffff"),
    welcome_text_align: str = Form("left"),
    welcome_text_font_name: str = Form("default"),
    welcome_text_box_width: int = Form(500),
welcome_text2_template: str = Form(""),
welcome_text2_x: int = Form(200),
welcome_text2_y: int = Form(140),
welcome_text2_font_size: int = Form(32),
welcome_text2_color: str = Form("#ffffff"),
welcome_text2_align: str = Form("left"),
welcome_text2_font_name: str = Form("default"),
welcome_text2_box_width: int = Form(500),
welcome_text3_template: str = Form(""),
welcome_text3_x: int = Form(200),
welcome_text3_y: int = Form(200),
welcome_text3_font_size: int = Form(28),
welcome_text3_color: str = Form("#ffffff"),
welcome_text3_align: str = Form("left"),
welcome_text3_font_name: str = Form("default"),
welcome_text3_box_width: int = Form(500),
    invite_block_channel_ids: str = Form(""),
    bot_only_channel_ids: str = Form(""),
    notify_channel_id: str = Form(""),
    voice_afk_disconnect_enabled: str = Form("off"),
    voice_afk_disconnect_delay_sec: int = Form(60),
    leaderboard_channel_id: str = Form(""),
    voice_dm_summary_enabled: str = Form("on"),
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
        welcome_enabled=(welcome_enabled == "on"),
        welcome_channel_id=int(welcome_channel_id or 0),
        welcome_message_template=str(welcome_message_template or ""),
        goodbye_enabled=(goodbye_enabled == "on"),
        goodbye_channel_id=int(goodbye_channel_id or 0),
        goodbye_message_template=str(goodbye_message_template or ""),
        welcome_image_enabled=(welcome_image_enabled == "on"),
        welcome_background_url=str(welcome_background_url or ""),
        welcome_avatar_shape=str(welcome_avatar_shape or "circle"),
        welcome_avatar_x=int(welcome_avatar_x or 40),
        welcome_avatar_y=int(welcome_avatar_y or 40),
        welcome_avatar_w=int(welcome_avatar_w or 128),
        welcome_avatar_h=int(welcome_avatar_h or 128),
        welcome_text_template=str(welcome_text_template or ""),
        welcome_text_x=int(welcome_text_x or 200),
        welcome_text_y=int(welcome_text_y or 80),
        welcome_text_font_size=int(welcome_text_font_size or 40),
        welcome_text_color=str(welcome_text_color or "#ffffff"),
        welcome_text_align=str(welcome_text_align or "left"),
        welcome_text_font_name=str(welcome_text_font_name or "default"),
        welcome_text_box_width=int(welcome_text_box_width or 500),
welcome_text2_template=str(welcome_text2_template or ""),
welcome_text2_x=int(welcome_text2_x or 200),
welcome_text2_y=int(welcome_text2_y or 140),
welcome_text2_font_size=int(welcome_text2_font_size or 32),
welcome_text2_color=str(welcome_text2_color or "#ffffff"),
welcome_text2_align=str(welcome_text2_align or "left"),
welcome_text2_font_name=str(welcome_text2_font_name or "default"),
welcome_text2_box_width=int(welcome_text2_box_width or 500),
welcome_text3_template=str(welcome_text3_template or ""),
welcome_text3_x=int(welcome_text3_x or 200),
welcome_text3_y=int(welcome_text3_y or 200),
welcome_text3_font_size=int(welcome_text3_font_size or 28),
welcome_text3_color=str(welcome_text3_color or "#ffffff"),
welcome_text3_align=str(welcome_text3_align or "left"),
welcome_text3_font_name=str(welcome_text3_font_name or "default"),
welcome_text3_box_width=int(welcome_text3_box_width or 500),
        invite_block_channel_ids=_parse_bigint_list_text(invite_block_channel_ids),
        bot_only_channel_ids=_parse_bigint_list_text(bot_only_channel_ids),
        notify_channel_id=int(notify_channel_id or 0),
        voice_afk_disconnect_enabled=(voice_afk_disconnect_enabled == "on"),
        voice_afk_disconnect_delay_sec=int(voice_afk_disconnect_delay_sec or 60),
        leaderboard_channel_id=int(leaderboard_channel_id or 0),
        voice_dm_summary_enabled=(voice_dm_summary_enabled == "on"),
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

    queued = await _enqueue_all_members_role_sync(pool, int(guild_id))
    return RedirectResponse(url=f"/admin?guild_id={guild_id}&msg=규칙+저장+완료(역할동기화+큐+{queued}건)", status_code=302)

@app.post("/admin/rules-delete")
async def rules_delete(request: Request, guild_id: int = Form(...), level: int = Form(...)):
    if not _require_admin(request):
        return RedirectResponse(url="/", status_code=302)
    pool = await _ensure_pool(app)
    await pool.execute("DELETE FROM level_role_sets WHERE guild_id=$1 AND level=$2", int(guild_id), int(level))
    queued = await _enqueue_all_members_role_sync(pool, int(guild_id))
    return RedirectResponse(url=f"/admin?guild_id={guild_id}&msg=규칙+삭제+완료(역할동기화+큐+{queued}건)", status_code=302)

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



@app.post("/admin/reaction-role-upsert")
async def reaction_role_upsert(
    request: Request,
    guild_id: int = Form(...),
    channel_id: str = Form(...),
    message: str = Form(...),
    emoji: str = Form(...),
    add_role_ids: str = Form(""),
    remove_role_ids: str = Form(""),
):
    if not _require_admin(request):
        return RedirectResponse(url="/", status_code=302)
    pool = await _ensure_pool(app)
    mids = _parse_ids(message)
    if not mids:
        return RedirectResponse(url=f"/admin?guild_id={guild_id}&msg=메시지+ID(또는+링크)를+입력하세요", status_code=302)
    msg_id = int(mids[0])
    if not str(channel_id).isdigit():
        return RedirectResponse(url=f"/admin?guild_id={guild_id}&msg=채널을+선택하세요", status_code=302)
    cid = int(channel_id)
    ek = _parse_emoji_key(emoji)
    if not ek:
        return RedirectResponse(url=f"/admin?guild_id={guild_id}&msg=이모지를+입력하세요", status_code=302)

    add_ids = _parse_ids(add_role_ids)
    rem_ids = _parse_ids(remove_role_ids)
    if not add_ids and not rem_ids:
        return RedirectResponse(url=f"/admin?guild_id={guild_id}&msg=추가/제거+역할+중+하나는+필수", status_code=302)

    await pool.execute(
        """INSERT INTO reaction_role_rules (guild_id, channel_id, message_id, emoji_key, add_role_ids, remove_role_ids, updated_at)
           VALUES ($1,$2,$3,$4,$5,$6,NOW())
           ON CONFLICT (guild_id, message_id, emoji_key)
           DO UPDATE SET channel_id=EXCLUDED.channel_id, add_role_ids=EXCLUDED.add_role_ids, remove_role_ids=EXCLUDED.remove_role_ids, updated_at=NOW()""",
        int(guild_id), int(cid), int(msg_id), str(ek), add_ids, rem_ids
    )

    return RedirectResponse(url=f"/admin?guild_id={guild_id}&msg=반응역할+저장+완료", status_code=302)


@app.post("/admin/reaction-role-delete")
async def reaction_role_delete(
    request: Request,
    guild_id: int = Form(...),
    message_id: int = Form(...),
    emoji_key: str = Form(...),
):
    if not _require_admin(request):
        return RedirectResponse(url="/", status_code=302)
    pool = await _ensure_pool(app)
    await pool.execute(
        "DELETE FROM reaction_role_rules WHERE guild_id=$1 AND message_id=$2 AND emoji_key=$3",
        int(guild_id), int(message_id), str(emoji_key)
    )
    return RedirectResponse(url=f"/admin?guild_id={guild_id}&msg=반응역할+삭제+완료", status_code=302)

# ---- APIs for UI (DB cache only, no Discord REST) ----
@app.get("/admin/api/roles_search")
async def api_roles_search(request: Request, guild_id: int, q: str = ""):
    if not _require_admin(request):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    pool = await _ensure_pool(app)
    qq = (q or "").strip()
    if qq:
        rows = await pool.fetch(
            "SELECT role_id, role_name, position FROM guild_roles_cache WHERE guild_id=$1 AND role_name ILIKE $2 ORDER BY position DESC, role_name ASC LIMIT 80",
            int(guild_id), f"%{qq}%",
        )
    else:
        rows = await pool.fetch(
            "SELECT role_id, role_name, position FROM guild_roles_cache WHERE guild_id=$1 ORDER BY position DESC, role_name ASC LIMIT 60",
            int(guild_id)
        )
    return {"ok": True, "roles": [
        {
            "role_id": str(r["role_id"]),
            "role_name": str(r["role_name"]),
            "position": int(r["position"]),
        }
        for r in rows
    ]}


@app.get("/admin/api/members_search")
async def api_members_search(request: Request, guild_id: int, q: str = ""):
    if not _require_admin(request):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    pool = await _ensure_pool(app)
    q = (q or "").strip()

    # If empty query: return recent members for quick picker
    if len(q) < 1:
        try:
            rows = await pool.fetch(
                """SELECT user_id, username, discriminator, global_name, nick, display_name, avatar_url
                   FROM guild_members_cache
                   WHERE guild_id=$1 AND in_guild=TRUE
                   ORDER BY updated_at DESC
                   LIMIT 60""",
                int(guild_id)
            )
        except Exception as e:
            log.warning("members_search fallback used (recent members): %s", e)
            rows = await pool.fetch(
                """SELECT user_id, username, discriminator, global_name, nick, display_name, NULL::TEXT AS avatar_url
                   FROM guild_members_cache
                   WHERE guild_id=$1
                   ORDER BY updated_at DESC
                   LIMIT 60""",
                int(guild_id)
            )
        return {"ok": True, "members": [{**dict(r), "user_id": str(r["user_id"])} for r in rows]}

    q_like = f"%{q}%"
    try:
        rows = await pool.fetch(
            """SELECT user_id, username, discriminator, global_name, nick, display_name, avatar_url
               FROM guild_members_cache
               WHERE guild_id=$1 AND in_guild=TRUE AND (
                    display_name ILIKE $2 OR nick ILIKE $2 OR global_name ILIKE $2 OR username ILIKE $2 OR CAST(user_id AS TEXT) LIKE $3
               )
               ORDER BY updated_at DESC
               LIMIT 50""",
            int(guild_id), q_like, f"%{q}%"
        )
    except Exception as e:
        log.warning("members_search fallback used (search): %s", e)
        rows = await pool.fetch(
            """SELECT user_id, username, discriminator, global_name, nick, display_name, NULL::TEXT AS avatar_url
               FROM guild_members_cache
               WHERE guild_id=$1 AND (
                    display_name ILIKE $2 OR nick ILIKE $2 OR global_name ILIKE $2 OR username ILIKE $2 OR CAST(user_id AS TEXT) LIKE $3
               )
               ORDER BY updated_at DESC
               LIMIT 50""",
            int(guild_id), q_like, f"%{q}%"
        )
    return {"ok": True, "members": [{**dict(r), "user_id": str(r["user_id"])} for r in rows]}

@app.get("/admin/api/members_list")
async def api_members_list(request: Request, guild_id: int, limit: int = 200, offset: int = 0):
    if not _require_admin(request):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    pool = await _ensure_pool(app)
    try:
        rows = await pool.fetch(
            """SELECT user_id, username, discriminator, global_name, nick, display_name, avatar_url
               FROM guild_members_cache
               WHERE guild_id=$1 AND in_guild=TRUE
               ORDER BY display_name NULLS LAST, user_id
               LIMIT $2 OFFSET $3""",
            int(guild_id), int(limit), int(offset)
        )
        total = int(await pool.fetchval("SELECT COUNT(*) FROM guild_members_cache WHERE guild_id=$1 AND in_guild=TRUE", int(guild_id)) or 0)
    except Exception as e:
        log.warning("members_list fallback used: %s", e)
        rows = await pool.fetch(
            """SELECT user_id, username, discriminator, global_name, nick, display_name, NULL::TEXT AS avatar_url
               FROM guild_members_cache
               WHERE guild_id=$1
               ORDER BY display_name NULLS LAST, user_id
               LIMIT $2 OFFSET $3""",
            int(guild_id), int(limit), int(offset)
        )
        total = int(await pool.fetchval("SELECT COUNT(*) FROM guild_members_cache WHERE guild_id=$1", int(guild_id)) or 0)
    return {"ok": True, "members": [dict(r) for r in rows], "total": total}

@app.get("/admin/api/members_by_role")
async def api_members_by_role(request: Request, guild_id: int, role_id: int):
    if not _require_admin(request):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    pool = await _ensure_pool(app)
    try:
        rows = await pool.fetch(
            "SELECT user_id, username, discriminator, global_name, nick, display_name FROM guild_members_cache WHERE guild_id=$1 AND in_guild=TRUE AND $2 = ANY(role_ids) ORDER BY display_name NULLS LAST, user_id LIMIT 500",
            int(guild_id), int(role_id)
        )
    except Exception as e:
        log.warning("members_by_role fallback used: %s", e)
        rows = []
    return {"ok": True, "members": [{**dict(r), "user_id": str(r["user_id"])} for r in rows]}

# Health check
@app.get("/healthz")
async def healthz():
    return {"ok": True}
