from __future__ import annotations

import asyncpg
from typing import Any

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
  avatar_url TEXT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (guild_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_guild_members_cache_lookup
  ON guild_members_cache (guild_id, user_id);

CREATE INDEX IF NOT EXISTS idx_guild_members_cache_search
  ON guild_members_cache (guild_id, display_name);

CREATE TABLE IF NOT EXISTS level_roles (
  guild_id BIGINT NOT NULL,
  level INTEGER NOT NULL,
  add_role_id BIGINT NOT NULL,
  remove_role_id BIGINT NULL,
  PRIMARY KEY (guild_id, level)
);
"""

async def create_pool(database_url: str) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(dsn=database_url, min_size=1, max_size=5, command_timeout=30)
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)

        # ---- Migrations for newer features ----
        # guild_settings: streak bonus settings
        await conn.execute("ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS checkin_streak_bonus_per_day INTEGER NOT NULL DEFAULT 0;")
        await conn.execute("ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS checkin_streak_bonus_cap INTEGER NOT NULL DEFAULT 0;")

        # checkin streak tracking
        await conn.execute("""CREATE TABLE IF NOT EXISTS checkin_streaks (
          guild_id BIGINT NOT NULL,
          user_id BIGINT NOT NULL,
          last_ymd TEXT NULL,
          streak INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (guild_id, user_id)
        );""")

        # guild_members_cache: track current membership + role ids for role-based selection
        await conn.execute("ALTER TABLE guild_members_cache ADD COLUMN IF NOT EXISTS in_guild BOOLEAN NOT NULL DEFAULT TRUE;")
        await conn.execute("ALTER TABLE guild_members_cache ADD COLUMN IF NOT EXISTS role_ids BIGINT[] NOT NULL DEFAULT '{}'::BIGINT[];")
        await conn.execute("ALTER TABLE guild_members_cache ADD COLUMN IF NOT EXISTS avatar_url TEXT NULL;")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_guild_members_cache_role_ids ON guild_members_cache USING GIN (role_ids);")

        # level role rules v2 (multiple add/remove roles)
        await conn.execute("""CREATE TABLE IF NOT EXISTS level_role_sets (
          guild_id BIGINT NOT NULL,
          level INTEGER NOT NULL,
          add_role_ids BIGINT[] NOT NULL DEFAULT '{}'::BIGINT[],
          remove_role_ids BIGINT[] NOT NULL DEFAULT '{}'::BIGINT[],
          PRIMARY KEY (guild_id, level)
        );""")

        # migrate legacy level_roles into level_role_sets once (idempotent)
        await conn.execute("""INSERT INTO level_role_sets (guild_id, level, add_role_ids, remove_role_ids)
          SELECT guild_id, level,
                 ARRAY[add_role_id]::BIGINT[],
                 CASE WHEN remove_role_id IS NULL THEN '{}'::BIGINT[] ELSE ARRAY[remove_role_id]::BIGINT[] END
          FROM level_roles
          ON CONFLICT (guild_id, level) DO NOTHING;""")

                # reaction lock (block specific roles from reacting on specific messages)
        await conn.execute("""CREATE TABLE IF NOT EXISTS reaction_blocks (
          guild_id BIGINT NOT NULL,
          channel_id BIGINT NOT NULL,
          message_id BIGINT NOT NULL,
          blocked_role_id BIGINT NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          PRIMARY KEY (guild_id, message_id, blocked_role_id)
        );""")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_reaction_blocks_guild ON reaction_blocks (guild_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_reaction_blocks_message ON reaction_blocks (guild_id, message_id);")
        # legacy compatibility
        await conn.execute("ALTER TABLE reaction_blocks ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();")

        
        # reaction role rules (reaction -> add/remove roles)
        await conn.execute("""CREATE TABLE IF NOT EXISTS reaction_role_rules (
          guild_id BIGINT NOT NULL,
          channel_id BIGINT NOT NULL,
          message_id BIGINT NOT NULL,
          emoji_key TEXT NOT NULL,
          add_role_ids BIGINT[] NOT NULL DEFAULT '{}'::BIGINT[],
          remove_role_ids BIGINT[] NOT NULL DEFAULT '{}'::BIGINT[],
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          PRIMARY KEY (guild_id, message_id, emoji_key)
        );""")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_reaction_role_rules_guild ON reaction_role_rules (guild_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_reaction_role_rules_message ON reaction_role_rules (guild_id, message_id);")

# channels cache (for dashboard dropdown)
        await conn.execute("""CREATE TABLE IF NOT EXISTS guild_channels_cache (
          guild_id BIGINT NOT NULL,
          channel_id BIGINT NOT NULL,
          channel_name TEXT NOT NULL,
          channel_type INTEGER NOT NULL DEFAULT 0,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          PRIMARY KEY (guild_id, channel_id)
        );""")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_guild_channels_cache_guild ON guild_channels_cache (guild_id);")

        # welcome/goodbye message variable support
        await conn.execute("ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_message_template TEXT NOT NULL DEFAULT '환영합니다 [user]!';")
        await conn.execute("ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS goodbye_message_template TEXT NOT NULL DEFAULT '[user] 님이 서버를 떠났습니다.';")
        await conn.execute("ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_message_channel_id BIGINT NOT NULL DEFAULT 0;")
        await conn.execute("ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS goodbye_message_channel_id BIGINT NOT NULL DEFAULT 0;")
        await conn.execute("ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_enabled BOOLEAN NOT NULL DEFAULT FALSE;")
        await conn.execute("ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS welcome_channel_id BIGINT NOT NULL DEFAULT 0;")
        await conn.execute("ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS goodbye_enabled BOOLEAN NOT NULL DEFAULT FALSE;")
        await conn.execute("ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS goodbye_channel_id BIGINT NOT NULL DEFAULT 0;")

# dashboard/bot coordination: request role sync after admin bulk edits
        await conn.execute("""CREATE TABLE IF NOT EXISTS role_sync_queue (
          guild_id BIGINT NOT NULL,
          user_id BIGINT NOT NULL,
          requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          processed_at TIMESTAMPTZ NULL,
          PRIMARY KEY (guild_id, user_id)
        );""")
    return pool

async def ensure_guild_settings(pool: asyncpg.Pool, guild_id: int) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO guild_settings (guild_id) VALUES ($1) ON CONFLICT (guild_id) DO NOTHING",
            guild_id,
        )

async def get_guild_settings(pool: asyncpg.Pool, guild_id: int) -> dict[str, Any]:
    await ensure_guild_settings(pool, guild_id)
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM guild_settings WHERE guild_id=$1", guild_id)
    return dict(row) if row else {}

async def update_guild_settings(pool: asyncpg.Pool, guild_id: int, **updates: Any) -> None:
    if not updates:
        return
    await ensure_guild_settings(pool, guild_id)
    cols = list(updates.keys())
    vals = list(updates.values())
    sets = ", ".join([f"{c}=${i+2}" for i, c in enumerate(cols)])
    sql = f"UPDATE guild_settings SET {sets} WHERE guild_id=$1"
    async with pool.acquire() as conn:
        await conn.execute(sql, guild_id, *vals)

async def get_user_xp(pool: asyncpg.Pool, guild_id: int, user_id: int) -> int:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT xp FROM user_stats WHERE guild_id=$1 AND user_id=$2", guild_id, user_id)
    return int(row["xp"]) if row else 0

async def add_user_xp(pool: asyncpg.Pool, guild_id: int, user_id: int, delta: int) -> int:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_stats (guild_id, user_id, xp)
            VALUES ($1,$2,GREATEST($3,0))
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET xp = GREATEST(user_stats.xp + $3, 0)
            """,
            guild_id, user_id, int(delta)
        )
        row = await conn.fetchrow("SELECT xp FROM user_stats WHERE guild_id=$1 AND user_id=$2", guild_id, user_id)
    return int(row["xp"]) if row else 0


async def set_user_xp(pool: asyncpg.Pool, guild_id: int, user_id: int, xp: int) -> int:
    """Force set user's XP (>=0)."""
    xp = max(int(xp), 0)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_stats (guild_id, user_id, xp)
            VALUES ($1,$2,$3)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET xp = $3
            """,
            guild_id, user_id, xp
        )
    return xp

async def can_gain_message_xp(pool: asyncpg.Pool, guild_id: int, user_id: int, cooldown_sec: int) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT last_message_at FROM user_stats WHERE guild_id=$1 AND user_id=$2", guild_id, user_id)
        if not row or row["last_message_at"] is None:
            return True
        last = row["last_message_at"]
        now = await conn.fetchval("SELECT NOW()")
    return (now - last).total_seconds() >= cooldown_sec

async def touch_last_message(pool: asyncpg.Pool, guild_id: int, user_id: int) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_stats (guild_id, user_id, last_message_at)
            VALUES ($1,$2,NOW())
            ON CONFLICT (guild_id, user_id) DO UPDATE SET last_message_at=NOW()
            """,
            guild_id, user_id
        )

async def record_checkin(pool: asyncpg.Pool, guild_id: int, user_id: int, ymd: str) -> bool:
    async with pool.acquire() as conn:
        try:
            await conn.execute("INSERT INTO checkins (guild_id, user_id, ymd) VALUES ($1,$2,$3)", guild_id, user_id, ymd)
            return True
        except asyncpg.UniqueViolationError:
            return False


async def reset_checkin(pool: asyncpg.Pool, guild_id: int, user_id: int, ymd: str) -> int:
    """Delete a user's check-in record for a given ymd. Returns deleted rows."""
    async with pool.acquire() as conn:
        r = await conn.execute(
            "DELETE FROM checkins WHERE guild_id=$1 AND user_id=$2 AND ymd=$3",
            guild_id, user_id, ymd,
        )
    # asyncpg returns e.g. 'DELETE 1'
    try:
        return int(str(r).split()[-1])
    except Exception:
        return 0

async def get_checkin_count(pool: asyncpg.Pool, guild_id: int, user_id: int) -> int:
    async with pool.acquire() as conn:
        c = await conn.fetchval("SELECT COUNT(*) FROM checkins WHERE guild_id=$1 AND user_id=$2", guild_id, user_id)
    return int(c or 0)

async def top_users(pool: asyncpg.Pool, guild_id: int, limit: int = 10) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, xp FROM user_stats WHERE guild_id=$1 ORDER BY xp DESC LIMIT $2", guild_id, limit)
    return [dict(r) for r in rows]

async def set_level_role_rule(pool: asyncpg.Pool, guild_id: int, level: int, add_role_id: int, remove_role_id: int | None) -> None:
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

async def list_level_role_rules(pool: asyncpg.Pool, guild_id: int) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT level, add_role_id, remove_role_id FROM level_roles WHERE guild_id=$1 ORDER BY level", guild_id)
    return [dict(r) for r in rows]

async def remove_level_role_rule(pool: asyncpg.Pool, guild_id: int, level: int) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM level_roles WHERE guild_id=$1 AND level=$2", guild_id, int(level))

async def get_level_role_rule(pool: asyncpg.Pool, guild_id: int, level: int) -> dict[str, Any] | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT level, add_role_id, remove_role_id FROM level_roles WHERE guild_id=$1 AND level=$2", guild_id, int(level))
    return dict(row) if row else None


async def upsert_roles_cache(pool: asyncpg.Pool, guild_id: int, roles: list[dict[str, Any]]) -> None:
    """Upsert roles cache from discord.py Guild.roles (no REST required)."""
    async with pool.acquire() as conn:
        # Keep it simple: upsert each role
        for r in roles:
            await conn.execute(
                '''
                INSERT INTO guild_roles_cache (guild_id, role_id, role_name, position, updated_at)
                VALUES ($1,$2,$3,$4,NOW())
                ON CONFLICT (guild_id, role_id)
                DO UPDATE SET role_name=EXCLUDED.role_name, position=EXCLUDED.position, updated_at=NOW()
                ''',
                guild_id, int(r["role_id"]), str(r.get("role_name") or ""), int(r.get("position") or 0)
            )

async def list_roles_cache(pool: asyncpg.Pool, guild_id: int) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT role_id, role_name, position FROM guild_roles_cache WHERE guild_id=$1 ORDER BY position DESC, role_name ASC",
            guild_id
        )
    return [dict(r) for r in rows]

async def upsert_guild_cache(pool: asyncpg.Pool, guild_id: int, guild_name: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS guilds_cache (
                   guild_id BIGINT NOT NULL PRIMARY KEY,
                   guild_name TEXT NOT NULL,
                   updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
               )"""
        )
        await conn.execute(
            """INSERT INTO guilds_cache (guild_id, guild_name, updated_at)
               VALUES ($1,$2,NOW())
               ON CONFLICT (guild_id)
               DO UPDATE SET guild_name=EXCLUDED.guild_name, updated_at=NOW()""",
            int(guild_id), str(guild_name or "")
        )

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT role_id, role_name, position FROM guild_roles_cache WHERE guild_id=$1 ORDER BY position DESC, role_name ASC",
            guild_id
        )
    return [dict(r) for r in rows]

async def upsert_member_cache(
    pool: asyncpg.Pool,
    guild_id: int,
    user_id: int,
    username: str | None,
    discriminator: str | None,
    global_name: str | None,
    nick: str | None,
    display_name: str | None,
    avatar_url: str | None = None,
    role_ids: list[int] | None = None,
    in_guild: bool = True,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            '''
            INSERT INTO guild_members_cache (guild_id, user_id, username, discriminator, global_name, nick, display_name, avatar_url, in_guild, role_ids, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,NOW())
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET
              username=EXCLUDED.username,
              discriminator=EXCLUDED.discriminator,
              global_name=EXCLUDED.global_name,
              nick=EXCLUDED.nick,
              display_name=EXCLUDED.display_name,
              avatar_url=EXCLUDED.avatar_url,
              in_guild=EXCLUDED.in_guild,
              role_ids=EXCLUDED.role_ids,
              updated_at=NOW()
            ''',
            guild_id, int(user_id),
            username, discriminator, global_name, nick, display_name, avatar_url,
            bool(in_guild), (role_ids or [])
        )

async def search_members_cache(pool: asyncpg.Pool, guild_id: int, q: str, limit: int = 25) -> list[dict[str, Any]]:
    """Search members in cache by display/nick/global/username or user_id."""
    q = (q or "").strip()
    if len(q) < 2:
        return []
    q_like = f"%{q}%"
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            '''
            SELECT user_id, username, discriminator, global_name, nick, display_name, avatar_url
            FROM guild_members_cache
            WHERE guild_id=$1 AND (
              display_name ILIKE $2 OR
              nick ILIKE $2 OR
              global_name ILIKE $2 OR
              username ILIKE $2 OR
              CAST(user_id AS TEXT) LIKE $3
            )
            ORDER BY updated_at DESC
            LIMIT $4
            ''',
            guild_id, q_like, f"%{q}%", int(limit)
        )
    return [dict(r) for r in rows]


# ---- Check-in streak helpers ----
async def get_checkin_streak(pool: asyncpg.Pool, guild_id: int, user_id: int) -> dict[str, Any]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT last_ymd, streak FROM checkin_streaks WHERE guild_id=$1 AND user_id=$2", guild_id, user_id)
    return dict(row) if row else {"last_ymd": None, "streak": 0}

async def update_checkin_streak(pool: asyncpg.Pool, guild_id: int, user_id: int, today_ymd: str, yesterday_ymd: str) -> int:
    """Update streak if today's check-in is newly recorded. Returns current streak."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT last_ymd, streak FROM checkin_streaks WHERE guild_id=$1 AND user_id=$2", guild_id, user_id)
        last = row["last_ymd"] if row else None
        streak = int(row["streak"]) if row else 0
        if last == yesterday_ymd:
            streak = streak + 1
        else:
            streak = 1
        await conn.execute(
            """
            INSERT INTO checkin_streaks (guild_id, user_id, last_ymd, streak)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET last_ymd=$3, streak=$4
            """,
            guild_id, user_id, today_ymd, streak
        )
    return int(streak)


async def increment_checkin_streak_test_mode(pool: asyncpg.Pool, guild_id: int, user_id: int, today_ymd: str) -> int:
    """When daily check-in limit is OFF, allow repeated same-day check-ins to increment streak
    so admins can test streak bonuses immediately."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT last_ymd, streak FROM checkin_streaks WHERE guild_id=$1 AND user_id=$2",
            guild_id, user_id,
        )
        last = row["last_ymd"] if row else None
        streak = int(row["streak"]) if row else 0
        if last:
            streak = max(streak, 0) + 1
        else:
            streak = 1
        await conn.execute(
            """
            INSERT INTO checkin_streaks (guild_id, user_id, last_ymd, streak)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET last_ymd=$3, streak=$4
            """,
            guild_id, user_id, today_ymd, streak,
        )
    return int(streak)

# ---- Level role sets v2 ----
async def set_level_role_set(pool: asyncpg.Pool, guild_id: int, level: int, add_role_ids: list[int], remove_role_ids: list[int]) -> None:
    add_role_ids = [int(x) for x in add_role_ids if int(x) > 0]
    remove_role_ids = [int(x) for x in remove_role_ids if int(x) > 0]
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO level_role_sets (guild_id, level, add_role_ids, remove_role_ids)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (guild_id, level)
            DO UPDATE SET add_role_ids=$3, remove_role_ids=$4
            """,
            guild_id, int(level), add_role_ids, remove_role_ids
        )

async def list_level_role_sets(pool: asyncpg.Pool, guild_id: int) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT level, add_role_ids, remove_role_ids FROM level_role_sets WHERE guild_id=$1 ORDER BY level", guild_id)
    return [dict(r) for r in rows]

async def remove_level_role_set(pool: asyncpg.Pool, guild_id: int, level: int) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM level_role_sets WHERE guild_id=$1 AND level=$2", guild_id, int(level))

# ---- Queue for Discord role re-sync after dashboard edits ----
async def enqueue_role_sync(pool: asyncpg.Pool, guild_id: int, user_ids: list[int]) -> int:
    if not user_ids:
        return 0
    user_ids = [int(x) for x in set(user_ids) if int(x) > 0]
    async with pool.acquire() as conn:
        await conn.executemany(
            """INSERT INTO role_sync_queue (guild_id, user_id, requested_at, processed_at)
                 VALUES ($1,$2,NOW(),NULL)
                 ON CONFLICT (guild_id, user_id) DO UPDATE SET requested_at=NOW(), processed_at=NULL""",
            [(guild_id, uid) for uid in user_ids]
        )
    return len(user_ids)

async def fetch_role_sync_batch(pool: asyncpg.Pool, guild_id: int, limit: int = 20) -> list[int]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT user_id FROM role_sync_queue
                 WHERE guild_id=$1 AND processed_at IS NULL
                 ORDER BY requested_at ASC
                 LIMIT $2""",
            guild_id, int(limit)
        )
        user_ids = [int(r["user_id"]) for r in rows]
        if user_ids:
            await conn.execute(
                """UPDATE role_sync_queue SET processed_at=NOW()
                     WHERE guild_id=$1 AND user_id = ANY($2::BIGINT[])""",
                guild_id, user_ids
            )
    return user_ids

# ---- Member listing helpers for dashboard ----
async def list_members_cache(pool: asyncpg.Pool, guild_id: int, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT user_id, username, discriminator, global_name, nick, display_name, role_ids
                 FROM guild_members_cache
                 WHERE guild_id=$1 AND in_guild=TRUE
                 ORDER BY display_name NULLS LAST, user_id
                 LIMIT $2 OFFSET $3""",
            guild_id, int(limit), int(offset)
        )
    return [dict(r) for r in rows]

async def top_users_current_members(pool: asyncpg.Pool, guild_id: int, limit: int = 300, offset: int = 0) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT m.user_id, COALESCE(s.xp,0) AS xp
                 FROM guild_members_cache m
                 LEFT JOIN user_stats s ON s.guild_id=m.guild_id AND s.user_id=m.user_id
                 WHERE m.guild_id=$1 AND m.in_guild=TRUE
                 ORDER BY COALESCE(s.xp,0) DESC, m.user_id
                 LIMIT $2 OFFSET $3""",
            guild_id, int(limit), int(offset)
        )
    return [dict(r) for r in rows]

async def count_ranked_members(pool: asyncpg.Pool, guild_id: int) -> int:
    async with pool.acquire() as conn:
        c = await conn.fetchval(
            """SELECT COUNT(*)
                 FROM guild_members_cache m
                 LEFT JOIN user_stats s ON s.guild_id=m.guild_id AND s.user_id=m.user_id
                 WHERE m.guild_id=$1 AND m.in_guild=TRUE""",
            guild_id
        )
    return int(c or 0)

async def set_member_in_guild(pool: asyncpg.Pool, guild_id: int, user_id: int, in_guild: bool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE guild_members_cache SET in_guild=$3, updated_at=NOW() WHERE guild_id=$1 AND user_id=$2",
            guild_id, user_id, bool(in_guild)
        )

async def upsert_channels_cache(pool: asyncpg.Pool, guild_id: int, channels: list[dict[str, Any]]) -> None:
    if not channels:
        return
    async with pool.acquire() as conn:
        rows = []
        for c in channels:
            cid = int(c.get("channel_id") or 0)
            name = str(c.get("channel_name") or "")
            ctype = int(c.get("channel_type") or 0)
            if cid <= 0 or not name:
                continue
            rows.append((guild_id, cid, name, ctype))
        if not rows:
            return
        await conn.executemany(
            """INSERT INTO guild_channels_cache (guild_id, channel_id, channel_name, channel_type, updated_at)
               VALUES ($1,$2,$3,$4,NOW())
               ON CONFLICT (guild_id, channel_id)
               DO UPDATE SET channel_name=EXCLUDED.channel_name, channel_type=EXCLUDED.channel_type, updated_at=NOW()""",
            rows
        )

async def list_channels_cache(pool: asyncpg.Pool, guild_id: int) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT channel_id, channel_name, channel_type FROM guild_channels_cache WHERE guild_id=$1 ORDER BY channel_type, channel_name",
            guild_id
        )
    return [dict(r) for r in rows]

async def add_reaction_blocks(pool: asyncpg.Pool, guild_id: int, channel_id: int, message_id: int, role_ids: list[int]) -> int:
    if not role_ids:
        return 0
    async with pool.acquire() as conn:
        rows = [(guild_id, channel_id, message_id, int(rid)) for rid in role_ids if int(rid) > 0]
        if not rows:
            return 0
        await conn.executemany(
            """INSERT INTO reaction_blocks (guild_id, channel_id, message_id, blocked_role_id, updated_at)
               VALUES ($1,$2,$3,$4,NOW())
               ON CONFLICT (guild_id, message_id, blocked_role_id)
               DO UPDATE SET channel_id=EXCLUDED.channel_id, updated_at=NOW()""",
            rows
        )
    return len(rows)

async def delete_reaction_block(pool: asyncpg.Pool, guild_id: int, message_id: int, role_id: int) -> int:
    async with pool.acquire() as conn:
        res = await conn.execute(
            "DELETE FROM reaction_blocks WHERE guild_id=$1 AND message_id=$2 AND blocked_role_id=$3",
            guild_id, message_id, role_id
        )
    try:
        return int(str(res).split()[-1])
    except Exception:
        return 0

async def list_reaction_blocks(pool: asyncpg.Pool, guild_id: int) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT guild_id, channel_id, message_id, blocked_role_id FROM reaction_blocks WHERE guild_id=$1 ORDER BY message_id, blocked_role_id",
            guild_id
        )
    return [dict(r) for r in rows]


# ---- Reaction role rules ----

async def upsert_reaction_role_rule(pool: asyncpg.Pool, guild_id: int, channel_id: int, message_id: int, emoji_key: str, add_ids: list[int], remove_ids: list[int]) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO reaction_role_rules (guild_id, channel_id, message_id, emoji_key, add_role_ids, remove_role_ids, updated_at)
               VALUES ($1,$2,$3,$4,$5,$6,NOW())
               ON CONFLICT (guild_id, message_id, emoji_key)
               DO UPDATE SET channel_id=EXCLUDED.channel_id, add_role_ids=EXCLUDED.add_role_ids, remove_role_ids=EXCLUDED.remove_role_ids, updated_at=NOW()""",
            int(guild_id), int(channel_id), int(message_id), str(emoji_key), list(map(int, add_ids)), list(map(int, remove_ids))
        )

async def delete_reaction_role_rule(pool: asyncpg.Pool, guild_id: int, message_id: int, emoji_key: str) -> int:
    async with pool.acquire() as conn:
        r = await conn.execute(
            "DELETE FROM reaction_role_rules WHERE guild_id=$1 AND message_id=$2 AND emoji_key=$3",
            int(guild_id), int(message_id), str(emoji_key)
        )
    try:
        return int(str(r).split()[-1])
    except Exception:
        return 0

async def list_reaction_role_rules(pool: asyncpg.Pool, guild_id: int) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT guild_id, channel_id, message_id, emoji_key, add_role_ids, remove_role_ids FROM reaction_role_rules WHERE guild_id=$1 ORDER BY message_id, emoji_key",
            int(guild_id)
        )
    return [dict(r) for r in rows]
