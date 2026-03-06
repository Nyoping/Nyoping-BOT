from __future__ import annotations

import asyncio
import logging
import discord
from discord.ext import commands

from .config import load_env_config
from .db.pg import (
    create_pool,
    upsert_roles_cache,
    upsert_member_cache,
    upsert_channels_cache,
    set_member_in_guild,
    fetch_role_sync_batch,
    get_user_xp,
    list_level_role_sets,
)
from .i18n import NyopingTranslator
from .utils import xp_to_level
from .role_sync import compute_expected_and_managed_roles, sync_member_roles

EXTENSIONS = [
    "nyopingbot.cogs.leveling",
    "nyopingbot.cogs.admin_settings",
    "nyopingbot.cogs.level_roles",
    "nyopingbot.cogs.moderation",
    "nyopingbot.cogs.reaction_lock",
]

def _role_ids(member: discord.Member) -> list[int]:
    return [int(r.id) for r in getattr(member, "roles", []) if r and r.id]


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
        self._voice_joined_at: dict[tuple[int, int], object] = {}
        self._cache_task: asyncio.Task | None = None
        self._role_sync_task: asyncio.Task | None = None

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
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            try:
                logging.info("Synced %d commands to guild %s", len(synced), self.target_guild_id)
            except Exception:
                logging.info("Synced commands to guild %s", self.target_guild_id)
        else:
            await self.tree.sync()
            logging.info("Synced commands globally (may take time to appear)")

        # background tasks
        self._cache_task = asyncio.create_task(self._sync_caches_background())
        self._role_sync_task = asyncio.create_task(self._role_sync_worker())

    async def close(self) -> None:
        for t in (self._cache_task, self._role_sync_task):
            if t and not t.done():
                t.cancel()
        await super().close()

    async def _sync_caches_background(self) -> None:
        # Run once shortly after login, then periodically refresh role cache.
        await self.wait_until_ready()
        await asyncio.sleep(2)

        while not self.is_closed():
            try:
                guilds: list[discord.Guild] = []
                if self.target_guild_id:
                    g = self.get_guild(self.target_guild_id)
                    if g:
                        guilds = [g]
                else:
                    guilds = list(self.guilds)

                for g in guilds:
                    # ensure members cache is populated (Server Members Intent required)
                    try:
                        await asyncio.wait_for(g.chunk(cache=True), timeout=25)
                    except Exception:
                        pass
                    # roles from gateway payload
                    roles_payload = [{"role_id": r.id, "role_name": r.name, "position": getattr(r, "position", 0)} for r in g.roles]
                    await upsert_roles_cache(self.db_pool, g.id, roles_payload)

                    # channels cache (for dashboard dropdown)
                    try:
                        ch_payload = []
                        for ch in getattr(g, 'channels', []):
                            try:
                                ctype = int(getattr(getattr(ch,'type',None), 'value', 0) or 0)
                            except Exception:
                                ctype = 0
                            name = getattr(ch, 'name', None)
                            if name is None:
                                continue
                            ch_payload.append({'channel_id': int(ch.id), 'channel_name': str(name), 'channel_type': ctype})
                        await upsert_channels_cache(self.db_pool, g.id, ch_payload)
                    except Exception:
                        pass

                    # members cache: chunked to avoid blocking (first 300 is ok)
                    members = list(getattr(g, "members", []))
                    for idx, m in enumerate(members):
                        try:
                            await upsert_member_cache(
                                self.db_pool, g.id, m.id,
                                getattr(m, "name", None),
                                getattr(m, "discriminator", None),
                                getattr(m, "global_name", None),
                                getattr(m, "nick", None),
                                getattr(m, "display_name", None),
                                role_ids=_role_ids(m),
                                in_guild=True,
                            )
                        except Exception:
                            pass
                        if idx % 25 == 0:
                            await asyncio.sleep(0)  # yield

                    # mark cached members who left the guild (keeps XP but excludes from ranking)
                    try:
                        member_ids = [int(m.id) for m in members]
                        async with self.db_pool.acquire() as conn:
                            await conn.execute(
                                "UPDATE guild_members_cache SET in_guild=FALSE, updated_at=NOW() WHERE guild_id=$1 AND in_guild=TRUE AND NOT (user_id = ANY($2::BIGINT[]))",
                                g.id, member_ids,
                            )
                    except Exception:
                        pass
                logging.info("Synced guild caches (roles/members) to DB.")
            except Exception:
                logging.exception("Failed to sync guild caches to DB")

            # refresh every 10 minutes (roles/names)
            await asyncio.sleep(600)

    async def on_member_join(self, member: discord.Member) -> None:
        try:
            await upsert_member_cache(
                self.db_pool, member.guild.id, member.id,
                getattr(member, "name", None),
                getattr(member, "discriminator", None),
                getattr(member, "global_name", None),
                getattr(member, "nick", None),
                getattr(member, "display_name", None),
                role_ids=_role_ids(member),
                in_guild=True,
            )
        except Exception:
            pass

    async def on_member_remove(self, member: discord.Member) -> None:
        try:
            await set_member_in_guild(self.db_pool, member.guild.id, member.id, False)
        except Exception:
            pass

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        # keep role_ids/display_name fresh
        try:
            await upsert_member_cache(
                self.db_pool, after.guild.id, after.id,
                getattr(after, "name", None),
                getattr(after, "discriminator", None),
                getattr(after, "global_name", None),
                getattr(after, "nick", None),
                getattr(after, "display_name", None),
                role_ids=_role_ids(after),
                in_guild=True,
            )
        except Exception:
            pass


    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        try:
            g = channel.guild
            ch_payload = []
            for ch in getattr(g, "channels", []):
                try:
                    ctype = int(getattr(getattr(ch, "type", None), "value", 0) or 0)
                except Exception:
                    ctype = 0
                name = getattr(ch, "name", None)
                if name is None:
                    continue
                ch_payload.append({"channel_id": int(ch.id), "channel_name": str(name), "channel_type": ctype})
            await upsert_channels_cache(self.db_pool, g.id, ch_payload)
        except Exception:
            pass

    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel) -> None:
        try:
            g = after.guild
            ch_payload = []
            for ch in getattr(g, "channels", []):
                try:
                    ctype = int(getattr(getattr(ch, "type", None), "value", 0) or 0)
                except Exception:
                    ctype = 0
                name = getattr(ch, "name", None)
                if name is None:
                    continue
                ch_payload.append({"channel_id": int(ch.id), "channel_name": str(name), "channel_type": ctype})
            await upsert_channels_cache(self.db_pool, g.id, ch_payload)
        except Exception:
            pass

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        # next background sync will refresh; keep simple
        return

    async def on_guild_role_create(self, role: discord.Role) -> None:
        try:
            g = role.guild
            roles_payload = [{"role_id": r.id, "role_name": r.name, "position": getattr(r, "position", 0)} for r in g.roles]
            await upsert_roles_cache(self.db_pool, g.id, roles_payload)
        except Exception:
            pass

    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        try:
            g = after.guild
            roles_payload = [{"role_id": r.id, "role_name": r.name, "position": getattr(r, "position", 0)} for r in g.roles]
            await upsert_roles_cache(self.db_pool, g.id, roles_payload)
        except Exception:
            pass

    async def on_guild_role_delete(self, role: discord.Role) -> None:
        try:
            g = role.guild
            roles_payload = [{"role_id": r.id, "role_name": r.name, "position": getattr(r, "position", 0)} for r in g.roles]
            await upsert_roles_cache(self.db_pool, g.id, roles_payload)
        except Exception:
            pass

    async def _role_sync_worker(self) -> None:
        """Process role_sync_queue written by the dashboard (bulk level edits).
        Runs slowly to avoid Discord rate limits and to avoid blocking slash commands.
        """
        await self.wait_until_ready()
        await asyncio.sleep(3)

        while not self.is_closed():
            try:
                guilds: list[discord.Guild] = []
                if self.target_guild_id:
                    g = self.get_guild(self.target_guild_id)
                    if g:
                        guilds = [g]
                else:
                    guilds = list(self.guilds)

                for g in guilds:
                    batch = await fetch_role_sync_batch(self.db_pool, g.id, limit=3)
                    if not batch:
                        continue
                    rules = await list_level_role_sets(self.db_pool, g.id)
                    if not rules:
                        continue
                    for uid in batch:
                        member = g.get_member(int(uid))
                        if not member:
                            continue
                        xp = await get_user_xp(self.db_pool, g.id, int(uid))
                        level = xp_to_level(xp)
                        expected, managed = compute_expected_and_managed_roles(rules, level)
                        if managed:
                            await sync_member_roles(member, expected, managed, reason="대시보드 변경 반영")
                        await asyncio.sleep(1.0)  # gentler (avoid global rate limits)
            except Exception:
                logging.exception("role sync worker error")

            await asyncio.sleep(10)

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