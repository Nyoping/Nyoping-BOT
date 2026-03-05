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
