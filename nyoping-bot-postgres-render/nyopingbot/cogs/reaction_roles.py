from __future__ import annotations

import asyncio
import logging
import discord
from discord.ext import commands

try:
    import emoji as emoji_lib
except Exception:
    emoji_lib = None

from ..db import list_reaction_role_rules, list_reaction_blocks

log = logging.getLogger(__name__)


def _normalize_unicode_emoji(text: str) -> str:
    return str(text or '').strip().replace('\ufe0f', '').replace('\ufe0e', '')

def _normalize_emoji_text(text: str) -> str:
    t = str(text or '').strip()
    if not t:
        return ''
    # custom emoji key from Discord payload / DB
    m = __import__('re').search(r"<?a?:?([\w\-]+):(\d{10,25})>?", t)
    if m and ':' in t:
        return f"{m.group(1)}:{m.group(2)}"
    if emoji_lib is not None and __import__('re').fullmatch(r":[^:\s]+:", t):
        try:
            converted = emoji_lib.emojize(t, language='alias')
            if converted and converted != t:
                return _normalize_unicode_emoji(converted)
        except Exception:
            pass
    return _normalize_unicode_emoji(t)

def _emoji_key(emoji: discord.PartialEmoji) -> str:
    if getattr(emoji, 'id', None):
        return f"{getattr(emoji, 'name', '')}:{int(emoji.id)}"
    return _normalize_unicode_emoji(str(getattr(emoji, 'name', '') or ''))


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
                    key = (int(r['guild_id']), int(r['message_id']), _normalize_emoji_text(str(r['emoji_key'])))
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

    async def _fetch_rule_from_db(self, guild_id: int, message_id: int, emoji_key: str) -> dict[str, object] | None:
        pool = getattr(self.bot, 'db_pool', None)
        if not pool:
            return None
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT channel_id, add_role_ids, remove_role_ids
                   FROM reaction_role_rules
                   WHERE guild_id=$1 AND message_id=$2 AND emoji_key=$3""",
                int(guild_id), int(message_id), _normalize_emoji_text(str(emoji_key)),
            )
        if not row:
            self._rules.pop((int(guild_id), int(message_id), _normalize_emoji_text(str(emoji_key))), None)
            return None
        entry = {
            'channel_id': int(row['channel_id']),
            'add_ids': set(int(x) for x in (row['add_role_ids'] or [])),
            'rem_ids': set(int(x) for x in (row['remove_role_ids'] or [])),
        }
        self._rules[(int(guild_id), int(message_id), _normalize_emoji_text(str(emoji_key)))] = entry
        return entry

    async def _fetch_blocks_from_db(self, guild_id: int, message_id: int) -> set[int]:
        pool = getattr(self.bot, 'db_pool', None)
        if not pool:
            return set()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT blocked_role_id
                   FROM reaction_blocks
                   WHERE guild_id=$1 AND message_id=$2""",
                int(guild_id), int(message_id),
            )
        blocked = {int(r['blocked_role_id']) for r in rows}
        self._blocked[(int(guild_id), int(message_id))] = blocked
        return blocked

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

    def _is_manageable_role(self, guild: discord.Guild, role: discord.Role | None) -> bool:
        if role is None:
            return False
        if role.is_default() or getattr(role, 'managed', False):
            return False
        me = guild.me
        if me is None:
            return False
        try:
            return me.guild_permissions.manage_roles and me.top_role > role
        except Exception:
            return False

    async def _apply_role_change(self, member: discord.Member, add_ids: set[int], rem_ids: set[int]) -> None:
        guild = member.guild
        current_ids = {int(r.id) for r in getattr(member, 'roles', [])}

        remove_roles: list[discord.Role] = []
        for rid in rem_ids:
            if rid not in current_ids:
                continue
            role = guild.get_role(int(rid))
            if self._is_manageable_role(guild, role):
                remove_roles.append(role)  # type: ignore[arg-type]

        add_roles: list[discord.Role] = []
        for rid in add_ids:
            if rid in current_ids:
                continue
            role = guild.get_role(int(rid))
            if self._is_manageable_role(guild, role):
                add_roles.append(role)  # type: ignore[arg-type]

        if remove_roles:
            try:
                await member.remove_roles(*remove_roles, reason='반응 역할 규칙 적용(제거)')
                log.info('Reaction role: removed roles guild=%s user=%s roles=%s', guild.id, member.id, [int(r.id) for r in remove_roles])
            except discord.Forbidden:
                log.warning('Reaction role: forbidden removing roles guild=%s user=%s roles=%s', guild.id, member.id, [int(r.id) for r in remove_roles])
                return
            except discord.HTTPException:
                log.warning('Reaction role: HTTP error removing roles guild=%s user=%s roles=%s', guild.id, member.id, [int(r.id) for r in remove_roles])
                return

        if add_roles:
            try:
                await member.add_roles(*add_roles, reason='반응 역할 규칙 적용(추가)')
                log.info('Reaction role: added roles guild=%s user=%s roles=%s', guild.id, member.id, [int(r.id) for r in add_roles])
            except discord.Forbidden:
                log.warning('Reaction role: forbidden adding roles guild=%s user=%s roles=%s', guild.id, member.id, [int(r.id) for r in add_roles])
                return
            except discord.HTTPException:
                log.warning('Reaction role: HTTP error adding roles guild=%s user=%s roles=%s', guild.id, member.id, [int(r.id) for r in add_roles])
                return

        if not remove_roles and not add_roles:
            log.info('Reaction role: no manageable role changes guild=%s user=%s add=%s remove=%s', guild.id, member.id, sorted(add_ids), sorted(rem_ids))

    async def _resolve_member(self, payload: discord.RawReactionActionEvent) -> discord.Member | None:
        member = getattr(payload, 'member', None)
        if member is not None and not getattr(member, 'bot', False):
            return member

        guild = self.bot.get_guild(int(payload.guild_id)) if payload.guild_id is not None else None
        if guild is None:
            return None

        member = guild.get_member(int(payload.user_id))
        if member is not None and not getattr(member, 'bot', False):
            return member

        try:
            member = await guild.fetch_member(int(payload.user_id))
            if member is not None and not getattr(member, 'bot', False):
                return member
        except Exception:
            return None
        return None

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        try:
            if payload.guild_id is None:
                return
            if self.bot.user and int(payload.user_id) == int(self.bot.user.id):
                return

            ekey = _emoji_key(payload.emoji)
            entry = await self._fetch_rule_from_db(int(payload.guild_id), int(payload.message_id), str(ekey))
            if not entry:
                log.info('Reaction role: no rule matched guild=%s message=%s emoji=%r user=%s', payload.guild_id, payload.message_id, ekey, payload.user_id)
                return
            log.info('Reaction role: matched guild=%s message=%s emoji=%r user=%s', payload.guild_id, payload.message_id, ekey, payload.user_id)
            if int(payload.channel_id) != int(entry.get('channel_id', 0)):
                return

            member = await self._resolve_member(payload)
            if member is None:
                return

            blocked = await self._fetch_blocks_from_db(int(payload.guild_id), int(payload.message_id))
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


@commands.Cog.listener()
async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
    try:
        if payload.guild_id is None:
            return
        if self.bot.user and int(payload.user_id) == int(self.bot.user.id):
            return

        ekey = _emoji_key(payload.emoji)
        entry = await self._fetch_rule_from_db(int(payload.guild_id), int(payload.message_id), str(ekey))
        if not entry:
            log.info('Reaction role(remove): no rule matched guild=%s message=%s emoji=%r user=%s', payload.guild_id, payload.message_id, ekey, payload.user_id)
            return
        if int(payload.channel_id) != int(entry.get('channel_id', 0)):
            return

        member = await self._resolve_member(payload)
        if member is None:
            return

        # 반응 취소 시에는 "추가 역할"을 제거하고, "제거 역할"은 다시 부여합니다.
        add_ids: set[int] = entry.get('add_ids', set())  # type: ignore[assignment]
        rem_ids: set[int] = entry.get('rem_ids', set())  # type: ignore[assignment]
        if not add_ids and not rem_ids:
            return

        log.info('Reaction role(remove): matched guild=%s message=%s emoji=%r user=%s', payload.guild_id, payload.message_id, ekey, payload.user_id)
        asyncio.create_task(self._apply_role_change(member, rem_ids, add_ids))
    except Exception:
        log.exception('Reaction roles remove-listener crashed')


async def setup(bot: commands.Bot):
    await bot.add_cog(ReactionRolesCog(bot))
