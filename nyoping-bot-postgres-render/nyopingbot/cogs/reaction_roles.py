from __future__ import annotations

import asyncio
import logging
import discord
from discord.ext import commands

from ..db import list_reaction_role_rules, list_reaction_blocks

log = logging.getLogger(__name__)


def _emoji_key(emoji: discord.PartialEmoji) -> str:
    if getattr(emoji, 'id', None):
        return f"{getattr(emoji, 'name', '')}:{int(emoji.id)}"
    return str(getattr(emoji, 'name', '') or '')


class ReactionRolesCog(commands.Cog):
    """특정 메시지의 특정 반응(이모지)을 누르면 역할을 추가/제거합니다."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._rules: dict[tuple[int, int, str], dict[str, object]] = {}
        self._blocked: dict[tuple[int, int], set[int]] = {}
        self._task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        self._task = asyncio.create_task(self._refresh_loop())

    async def cog_unload(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    async def _refresh_loop(self) -> None:
        await asyncio.sleep(2)
        while not self.bot.is_closed():
            try:
                await self.refresh_all()
            except Exception:
                log.exception('Reaction roles refresh failed')
            await asyncio.sleep(60)

    async def refresh_all(self) -> None:
        self._rules.clear()
        self._blocked.clear()
        pool = getattr(self.bot, 'db_pool', None)
        if not pool:
            return

        for g in getattr(self.bot, 'guilds', []):
            gid = int(g.id)
            try:
                rows = await list_reaction_role_rules(pool, gid)
                for r in rows:
                    key = (int(r['guild_id']), int(r['message_id']), str(r['emoji_key']))
                    self._rules[key] = {
                        'channel_id': int(r['channel_id']),
                        'add_ids': set(int(x) for x in (r.get('add_role_ids') or [])),
                        'rem_ids': set(int(x) for x in (r.get('remove_role_ids') or [])),
                    }
            except Exception:
                pass

            try:
                blocks = await list_reaction_blocks(pool, gid)
                for b in blocks:
                    bkey = (int(b['guild_id']), int(b['message_id']))
                    self._blocked.setdefault(bkey, set()).add(int(b['blocked_role_id']))
            except Exception:
                pass

    def _member_has_blocked_role(self, member: discord.Member, blocked: set[int]) -> bool:
        try:
            return any(int(getattr(role, 'id', 0)) in blocked for role in getattr(member, 'roles', []))
        except Exception:
            return False

    async def _remove_reaction(self, channel: discord.abc.Messageable, message_id: int, emoji: discord.PartialEmoji, member: discord.Member) -> None:
        try:
            partial = channel.get_partial_message(int(message_id))  # type: ignore[attr-defined]
            await partial.remove_reaction(emoji, member)
        except Exception:
            return

    async def _apply_role_change(self, member: discord.Member, add_ids: set[int], rem_ids: set[int]) -> None:
        cur_roles = list(member.roles)
        cur_ids = {r.id for r in cur_roles}

        kept = [r for r in cur_roles if r.id not in rem_ids]

        for rid in add_ids:
            if rid in cur_ids:
                continue
            role = member.guild.get_role(int(rid))
            if role:
                kept.append(role)

        uniq = {r.id: r for r in kept}
        final_roles = sorted(uniq.values(), key=lambda r: r.position)

        try:
            await member.edit(roles=final_roles, reason='반응 역할 규칙 적용')
        except discord.Forbidden:
            return
        except discord.HTTPException:
            return

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        try:
            if payload.guild_id is None:
                return
            if self.bot.user and int(payload.user_id) == int(self.bot.user.id):
                return

            ekey = _emoji_key(payload.emoji)
            key = (int(payload.guild_id), int(payload.message_id), str(ekey))
            entry = self._rules.get(key)
            if not entry:
                return
            if int(payload.channel_id) != int(entry.get('channel_id', 0)):
                return

            member = getattr(payload, 'member', None)
            if member is None:
                guild = self.bot.get_guild(int(payload.guild_id))
                if guild:
                    member = guild.get_member(int(payload.user_id))
            if member is None or getattr(member, 'bot', False):
                return

            bkey = (int(payload.guild_id), int(payload.message_id))
            blocked = self._blocked.get(bkey, set())
            if blocked and self._member_has_blocked_role(member, blocked):
                channel = self.bot.get_channel(int(payload.channel_id))
                if channel:
                    asyncio.create_task(self._remove_reaction(channel, int(payload.message_id), payload.emoji, member))
                return

            add_ids: set[int] = entry.get('add_ids', set())  # type: ignore[assignment]
            rem_ids: set[int] = entry.get('rem_ids', set())  # type: ignore[assignment]
            if not add_ids and not rem_ids:
                return

            asyncio.create_task(self._apply_role_change(member, add_ids, rem_ids))
        except Exception:
            log.exception('Reaction roles listener crashed')


async def setup(bot: commands.Bot):
    await bot.add_cog(ReactionRolesCog(bot))
