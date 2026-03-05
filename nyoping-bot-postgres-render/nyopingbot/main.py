from __future__ import annotations

import asyncio
import logging
import discord
from discord.ext import commands

from .config import load_env_config
from .db import create_pool

EXTENSIONS = [
    "nyopingbot.cogs.leveling",
    "nyopingbot.cogs.admin_settings",
    "nyopingbot.cogs.level_roles",
    "nyopingbot.cogs.moderation",
]

class NyopingBot(commands.Bot):
    def __init__(self, *, guild_id: int | None, db_pool, log_level: str):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.messages = True
        intents.voice_states = True
        intents.message_content = False

        super().__init__(command_prefix="!", intents=intents)
        self.target_guild_id = guild_id
        self.db_pool = db_pool
        self.log_level = log_level
        self._voice_joined_at: dict[tuple[int,int], object] = {}

    async def setup_hook(self) -> None:
        for ext in EXTENSIONS:
            try:
                await self.load_extension(ext)
            except Exception:
                logging.exception("Failed to load extension %s", ext)

        if self.target_guild_id:
            guild = discord.Object(id=self.target_guild_id)
            await self.tree.sync(guild=guild)
            logging.info("Synced commands to guild %s", self.target_guild_id)
        else:
            await self.tree.sync()
            logging.info("Synced commands globally (may take time to appear)")

def main() -> None:
    cfg = load_env_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    )

    async def runner():
        pool = await create_pool(cfg.database_url)
        bot = NyopingBot(guild_id=cfg.guild_id, db_pool=pool, log_level=cfg.log_level)
        async with bot:
            await bot.start(cfg.discord_token)

    asyncio.run(runner())
