from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from ..db import list_reaction_blocks

log = logging.getLogger(__name__)


class ReactionLockCog(commands.Cog):
    """특정 역할이 특정 메시지 반응을 누르거나 유지하지 못하게 막습니다."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._locks: dict[tuple[int, int], dict[str, object]] = {}
        self._refresh_task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def cog_unload(self) -> None:
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()

    async def _refresh_loop(self) -> None:
        await asyncio.sleep(2)
        while not self.bot.is_closed():
            try:
                await self.refresh_all()
            except Exception:
                log.exception("Reaction lock refresh failed")
            await asyncio.sleep(60)

    async def refresh_all(self) -> None:
        self._locks.clear()
        pool = getattr(self.bot, "db_pool", None)
        if not pool:
            return
        for guild in getattr(self.bot, "guilds", []):
            try:
                await self.refresh_guild(int(guild.id))
            except Exception:
                log.exception("Reaction lock refresh_guild failed guild=%s", getattr(guild, "id", "?"))

    async def refresh_guild(self, guild_id: int) -> None:
        pool = getattr(self.bot, "db_pool", None)
        if not pool:
            return
        rows = await list_reaction_blocks(pool, int(guild_id))
        for row in rows:
            key = (int(row["guild_id"]), int(row["message_id"]))
            entry = self._locks.get(key)
            if not entry:
                entry = {"channel_id": int(row["channel_id"]), "blocked_roles": set()}
                self._locks[key] = entry
            entry["channel_id"] = int(row["channel_id"])
            entry["blocked_roles"].add(int(row["blocked_role_id"]))

    def _member_has_blocked_role(self, member: discord.Member, blocked: set[int]) -> bool:
        try:
            return any(int(getattr(role, "id", 0)) in blocked for role in getattr(member, "roles", []))
        except Exception:
            return False

    async def _resolve_member(self, payload: discord.RawReactionActionEvent) -> discord.Member | None:
        guild = self.bot.get_guild(int(payload.guild_id)) if payload.guild_id is not None else None
        if guild is None:
            return None
        member = getattr(payload, "member", None)
        if member is not None and not getattr(member, "bot", False):
            return member
        member = guild.get_member(int(payload.user_id))
        if member is not None and not getattr(member, "bot", False):
            return member
        try:
            member = await guild.fetch_member(int(payload.user_id))
            if member is not None and not getattr(member, "bot", False):
                return member
        except Exception:
            return None
        return None

    async def _remove_reaction(self, channel: discord.abc.Messageable, message_id: int, emoji: discord.PartialEmoji, member: discord.Member) -> None:
        try:
            partial = channel.get_partial_message(int(message_id))  # type: ignore[attr-defined]
            await partial.remove_reaction(emoji, member)
            log.info("Reaction lock: removed blocked reaction guild_channel=%s message=%s user=%s", getattr(channel, "id", 0), message_id, member.id)
        except discord.Forbidden:
            log.warning("Reaction lock: missing Manage Messages permission in channel %s", getattr(channel, "id", "?"))
        except discord.HTTPException:
            return
        except Exception:
            log.exception("Reaction lock: failed to remove reaction")

    async def _add_reaction_back(self, channel: discord.abc.Messageable, message_id: int, emoji: discord.PartialEmoji) -> None:
        # Discord 구조상 사용자의 반응 취소 자체를 완전히 막을 수는 없습니다.
        # 더 이상 봇이 대신 반응을 남기지 않도록 no-op 처리합니다.
        log.info("Reaction lock: blocked user removed reaction channel=%s message=%s emoji=%r", getattr(channel, "id", 0), message_id, str(emoji))
        return

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        try:
            if payload.guild_id is None:
                return
            if self.bot.user and int(payload.user_id) == int(self.bot.user.id):
                return

            entry = self._locks.get((int(payload.guild_id), int(payload.message_id)))
            if not entry or int(payload.channel_id) != int(entry.get("channel_id", 0)):
                return

            member = await self._resolve_member(payload)
            if member is None or getattr(member, "bot", False):
                return

            blocked: set[int] = entry.get("blocked_roles", set())  # type: ignore[assignment]
            if not blocked or not self._member_has_blocked_role(member, blocked):
                return

            channel = self.bot.get_channel(int(payload.channel_id))
            if channel is None:
                return
            asyncio.create_task(self._remove_reaction(channel, int(payload.message_id), payload.emoji, member))
        except Exception:
            log.exception("Reaction lock add-listener crashed")

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        try:
            if payload.guild_id is None:
                return
            if self.bot.user and int(payload.user_id) == int(self.bot.user.id):
                return

            entry = self._locks.get((int(payload.guild_id), int(payload.message_id)))
            if not entry or int(payload.channel_id) != int(entry.get("channel_id", 0)):
                return

            member = await self._resolve_member(payload)
            if member is None or getattr(member, "bot", False):
                return

            blocked: set[int] = entry.get("blocked_roles", set())  # type: ignore[assignment]
            if not blocked or not self._member_has_blocked_role(member, blocked):
                return

            channel = self.bot.get_channel(int(payload.channel_id))
            if channel is None:
                return
            asyncio.create_task(self._add_reaction_back(channel, int(payload.message_id), payload.emoji))
        except Exception:
            log.exception("Reaction lock remove-listener crashed")


async def setup(bot: commands.Bot):
    await bot.add_cog(ReactionLockCog(bot))
