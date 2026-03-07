from __future__ import annotations

import asyncio
import logging
import discord
from discord.ext import commands

from ..db import list_reaction_blocks

log = logging.getLogger(__name__)

class ReactionLockCog(commands.Cog):
    """특정 역할이 특정 메시지의 '기존 반응'을 누르지 못하게 막습니다.
    - 해당 역할이 반응을 누르면, 봇이 그 반응을 즉시 제거합니다.
    - 봇에게 채널 권한: **메시지 관리(Manage Messages)** 필요.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # key: (guild_id, message_id) -> {"channel_id": int, "blocked_roles": set[int]}
        self._locks: dict[tuple[int,int], dict[str, object]] = {}
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
            await asyncio.sleep(120)  # 2분마다 갱신

    async def refresh_all(self) -> None:
        self._locks.clear()
        pool = getattr(self.bot, "db_pool", None)
        if not pool:
            return
        for g in getattr(self.bot, "guilds", []):
            try:
                await self.refresh_guild(int(g.id))
            except Exception:
                pass

    async def refresh_guild(self, guild_id: int) -> None:
        pool = getattr(self.bot, "db_pool", None)
        if not pool:
            return
        rows = await list_reaction_blocks(pool, int(guild_id))
        for r in rows:
            gid = int(r["guild_id"])
            mid = int(r["message_id"])
            key = (gid, mid)
            entry = self._locks.get(key)
            if not entry:
                entry = {"channel_id": int(r["channel_id"]), "blocked_roles": set()}
                self._locks[key] = entry
            entry["channel_id"] = int(r["channel_id"])
            entry["blocked_roles"].add(int(r["blocked_role_id"]))

    def _member_has_blocked_role(self, member: discord.Member, blocked: set[int]) -> bool:
        try:
            return any(int(getattr(role, "id", 0)) in blocked for role in getattr(member, "roles", []))
        except Exception:
            return False


async def _add_reaction_back(self, channel: discord.abc.Messageable, message_id: int, emoji: discord.PartialEmoji) -> None:
    """Best-effort fallback: 사용자의 반응 취소를 완전히 막을 수는 없지만,
    마지막 반응이 사라졌다면 봇 반응으로 이모지를 다시 남겨둘 수 있습니다."""
    try:
        partial = channel.get_partial_message(int(message_id))  # type: ignore[attr-defined]
        await partial.add_reaction(emoji)
    except Exception:
        return

    async def _remove_reaction(self, channel: discord.abc.Messageable, message_id: int, emoji: discord.PartialEmoji, member: discord.Member) -> None:
        try:
            partial = channel.get_partial_message(int(message_id))  # type: ignore[attr-defined]
            await partial.remove_reaction(emoji, member)  # REST call
        except discord.Forbidden:
            log.warning("Reaction lock: missing Manage Messages permission in channel %s", getattr(channel, "id", "?"))
        except discord.HTTPException:
            # rate limited / unknown message / etc.
            return
        except Exception:
            log.exception("Reaction lock: failed to remove reaction")


@commands.Cog.listener()
async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
    try:
        if payload.guild_id is None:
            return
        if self.bot.user and int(payload.user_id) == int(self.bot.user.id):
            return

        key = (int(payload.guild_id), int(payload.message_id))
        entry = self._locks.get(key)
        if not entry:
            return
        if int(payload.channel_id) != int(entry.get("channel_id", 0)):
            return

        guild = self.bot.get_guild(int(payload.guild_id))
        member = guild.get_member(int(payload.user_id)) if guild else None
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

@commands.Cog.listener()
async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        try:
            if payload.guild_id is None:
                return
            if self.bot.user and int(payload.user_id) == int(self.bot.user.id):
                return

            key = (int(payload.guild_id), int(payload.message_id))
            entry = self._locks.get(key)
            if not entry:
                return
            if int(payload.channel_id) != int(entry.get("channel_id", 0)):
                return

            # Resolve member
            member = getattr(payload, "member", None)
            if member is None:
                guild = self.bot.get_guild(int(payload.guild_id))
                if guild:
                    member = guild.get_member(int(payload.user_id))
            if member is None or getattr(member, "bot", False):
                return

            blocked: set[int] = entry.get("blocked_roles", set())  # type: ignore[assignment]
            if not blocked:
                return
            if not self._member_has_blocked_role(member, blocked):
                return

            channel = self.bot.get_channel(int(payload.channel_id))
            if channel is None:
                return

            # Remove reaction in background to keep event handler fast
            asyncio.create_task(self._remove_reaction(channel, int(payload.message_id), payload.emoji, member))
        except Exception:
            log.exception("Reaction lock listener crashed")

async def setup(bot: commands.Bot):
    await bot.add_cog(ReactionLockCog(bot))
