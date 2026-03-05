from __future__ import annotations

import asyncio
import logging
import discord
from discord.ext import commands

from .config import load_env_config
from .db.pg import create_pool, upsert_roles_cache, upsert_member_cache
from .i18n import NyopingTranslator

EXTENSIONS = [
    "nyopingbot.cogs.leveling",
    "nyopingbot.cogs.admin_settings",
    "nyopingbot.cogs.level_roles",
    "nyopingbot.cogs.moderation",
]

class NyopingBot(commands.Bot):
    def __init__(self, *, guild_id: int | None, db_pool, log_level: str, force_resync: bool):
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
        self.force_resync = force_resync
        self._voice_joined_at: dict[tuple[int,int], object] = {}
        self._cache_synced = False


    async def on_ready(self) -> None:
        # Sync caches once per process start (avoids Discord REST from dashboard).
        if self._cache_synced:
            return
        self._cache_synced = True
        try:
            guilds = []
            if self.target_guild_id:
                g = self.get_guild(self.target_guild_id)
                if g:
                    guilds = [g]
            else:
                guilds = list(self.guilds)

            for g in guilds:
                # roles are available from gateway payload
                roles_payload = [{"role_id": r.id, "role_name": r.name, "position": getattr(r, "position", 0)} for r in g.roles]
                await upsert_roles_cache(self.db_pool, g.id, roles_payload)

                # members cache (may be partial; updated further on events)
                for m in getattr(g, "members", []):
                    try:
                        await upsert_member_cache(
                            self.db_pool, g.id, m.id,
                            getattr(m, "name", None),
                            getattr(m, "discriminator", None),
                            getattr(m, "global_name", None),
                            getattr(m, "nick", None),
                            getattr(m, "display_name", None),
                        )
                    except Exception:
                        pass
            logging.info("Synced guild caches (roles/members) to DB.")
        except Exception:
            logging.exception("Failed to sync guild caches to DB")
    async def setup_hook(self) -> None:
        # Optional: clear & re-sync guild commands to force the Discord client UI to refresh.
        if self.target_guild_id and self.force_resync:
            guild = discord.Object(id=self.target_guild_id)
            self.tree.clear_commands(guild=guild)
            await self.tree.sync(guild=guild)
            logging.info("FORCE_RESYNC enabled: cleared existing guild commands (%s)", self.target_guild_id)

        # Enable Korean UI localization for slash commands (Discord client locale).
        try:
            await self.tree.set_translator(NyopingTranslator())
        except Exception:
            logging.exception("Failed to set app command translator")
        for ext in EXTENSIONS:
            try:
                await self.load_extension(ext)
            except Exception:
                logging.exception("Failed to load extension %s", ext)

        # Debug: show what commands are registered in the tree.
        try:
            cmd_names = [c.qualified_name for c in self.tree.get_commands()]
            logging.info("App commands in tree: %s", ", ".join(cmd_names) if cmd_names else "<none>")
        except Exception:
            logging.exception("Failed to list app commands")

        if self.target_guild_id:
            guild = discord.Object(id=self.target_guild_id)
            # Copy global commands into the guild for instant visibility (avoids the 1h global propagation delay).
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            try:
                logging.info("Synced %d commands to guild %s: %s", len(synced), self.target_guild_id, ", ".join([c.name for c in synced]))
            except Exception:
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
        bot = NyopingBot(guild_id=cfg.guild_id, db_pool=pool, log_level=cfg.log_level, force_resync=cfg.force_resync)
        async with bot:
            await bot.start(cfg.discord_token)

    asyncio.run(runner())