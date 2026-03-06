from __future__ import annotations

import logging
import discord
from discord.ext import commands

from ..db import list_reaction_blocks

log = logging.getLogger(__name__)

class ReactionLockCog(commands.Cog):
    """Blocks specific roles from adding reactions to configured messages.

    NOTE:
    - Bot needs **Manage Messages** permission in the channel to remove other users' reactions.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._locks: dict[int, dict[str, object]] = {}  # message_id -> {channel_id:int, blocked_roles:set[int], guild_id:int}

    async def cog_load(self) -> None:
        await self.refresh_all()

    async def refresh_all(self) -> None:
        self._locks.clear()
        if not getattr(self.bot, "db_pool", None):
            return
        for g in getattr(self.bot, "guilds", []):
            await self.refresh_guild(int(g.id))

    async def refresh_guild(self, guild_id: int) -> None:
        # remove old entries for this guild
        for mid in [k for k,v in self._locks.items() if int(v.get("guild_id",0)) == int(guild_id)]:
            self._locks.pop(mid, None)

        rows = await list_reaction_blocks(self.bot.db_pool, int(guild_id))
        for r in rows:
            mid = int(r["message_id"])
            entry = self._locks.get(mid)
            if not entry:
                entry = {"guild_id": int(guild_id), "channel_id": int(r["channel_id"]), "blocked_roles": set()}
                self._locks[mid] = entry
            entry["channel_id"] = int(r["channel_id"])
            entry["blocked_roles"].add(int(r["blocked_role_id"]))

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        try:
            if payload.guild_id is None:
                return
            if self.bot.user and payload.user_id == self.bot.user.id:
                return

            entry = self._locks.get(int(payload.message_id))
            if not entry:
                return

            if int(payload.channel_id) != int(entry.get("channel_id", 0)):
                return

            guild = self.bot.get_guild(int(payload.guild_id))
            member = getattr(payload, "member", None)
            if member is None and guild is not None:
                member = guild.get_member(int(payload.user_id))
            if member is None:
                return
            if getattr(member, "bot", False):
                return

            blocked: set[int] = entry.get("blocked_roles", set())
            if not blocked:
                return

            has_block = any(int(getattr(role, "id", 0)) in blocked for role in getattr(member, "roles", []))
            if not has_block:
                return

            # Remove the reaction immediately (prevents using reaction-role messages).
            channel = self.bot.get_channel(int(payload.channel_id))
            if channel is None:
                # Avoid fetch if possible (rate-limit). If not found, give up.
                return

            try:
                partial = channel.get_partial_message(int(payload.message_id))
                await partial.remove_reaction(payload.emoji, member)
            except discord.Forbidden:
                # Missing Manage Messages permission
                log.warning("Reaction lock: missing permission to remove reactions in channel %s", payload.channel_id)
            except Exception:
                log.exception("Reaction lock: failed to remove reaction")
        except Exception:
            log.exception("Reaction lock listener crashed")

async def setup(bot: commands.Bot):
    await bot.add_cog(ReactionLockCog(bot))
