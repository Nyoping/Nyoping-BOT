from __future__ import annotations

import os
from dataclasses import dataclass
from dotenv import load_dotenv

@dataclass(frozen=True)
class Config:
    discord_token: str
    guild_id: int | None
    database_url: str
    log_level: str

def _get_int(name: str) -> int | None:
    v = os.getenv(name, "").strip()
    return int(v) if v else None

def load_env_config() -> Config:
    load_dotenv(override=False)

    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_TOKEN 환경변수가 비어 있습니다. .env를 설정하세요.")

    db = os.getenv("DATABASE_URL", "").strip()
    if not db:
        raise RuntimeError("DATABASE_URL(Postgres) 환경변수가 비어 있습니다. .env를 설정하세요.")

    return Config(
        discord_token=token,
        guild_id=_get_int("GUILD_ID"),
        database_url=db,
        log_level=(os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"),
    )
