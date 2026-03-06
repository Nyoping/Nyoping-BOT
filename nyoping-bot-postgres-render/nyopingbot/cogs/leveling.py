from __future__ import annotations

import math
import discord
from discord import app_commands
from discord.ext import commands

from ..db import (
    get_guild_settings,
    add_user_xp,
    get_user_xp,
    set_user_xp,
    can_gain_message_xp,
    touch_last_message,
    upsert_member_cache,
    record_checkin,
    get_checkin_count,
    update_checkin_streak,
    get_checkin_streak,
    increment_checkin_streak_test_mode,
    list_level_role_sets,
    top_users_current_members,
    count_ranked_members,
)
from ..utils import kst_today_ymd, kst_yesterday_ymd, xp_to_level
from ..role_sync import compute_expected_and_managed_roles, sync_member_roles


def _member_role_ids(member: discord.abc.User) -> list[int]:
    try:
        if isinstance(member, discord.Member):
            return [int(r.id) for r in member.roles if r and r.id]
    except Exception:
        pass
    return []


class LeaderboardView(discord.ui.View):
    def __init__(self, cog: "LevelingCog", guild_id: int, author_id: int, page: int, per_page: int, total: int):
        super().__init__(timeout=120)
        self.cog = cog
        self.guild_id = guild_id
        self.author_id = author_id
        self.page = page
        self.per_page = per_page
        self.total = total

    async def _render(self) -> discord.Embed:
        offset = self.page * self.per_page
        rows = await top_users_current_members(self.cog.bot.db_pool, self.guild_id, self.per_page, offset)
        embed = discord.Embed(title="🏆 서버 랭킹", description=f"{offset+1}~{min(offset+self.per_page, self.total)} / {self.total}")
        if not rows:
            embed.description = "아직 데이터가 없어요."
            return embed
        lines = []
        for i, r in enumerate(rows, start=offset + 1):
            uid = int(r["user_id"])
            xp = int(r["xp"])
            lv = xp_to_level(xp)
            lines.append(f"**{i}.** <@{uid}> — {xp}XP (Lv.{lv})")
        embed.add_field(name="순위", value="\n".join(lines), inline=False)
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return int(interaction.user.id) == int(self.author_id)

    @discord.ui.button(label="◀ 이전", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page <= 0:
            await interaction.response.defer()
            return
        self.page -= 1
        embed = await self._render()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="다음 ▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        max_page = max(0, math.ceil(self.total / self.per_page) - 1)
        if self.page >= max_page:
            await interaction.response.defer()
            return
        self.page += 1
        embed = await self._render()
        await interaction.response.edit_message(embed=embed, view=self)


class LevelingCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _sync_roles_for_level(self, guild: discord.Guild, user_id: int, level: int, *, reason: str):
        rules = await list_level_role_sets(self.bot.db_pool, guild.id)
        if not rules:
            return
        member = guild.get_member(user_id)
        if member is None:
            return
        expected, managed = compute_expected_and_managed_roles(rules, level)
        if not managed:
            return
        await sync_member_roles(member, expected, managed, reason=reason)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return

        # Cache member info + roles for dashboard (no Discord REST)
        try:
            author = message.author
            await upsert_member_cache(
                self.bot.db_pool,
                message.guild.id,
                author.id,
                getattr(author, "name", None),
                getattr(author, "discriminator", None),
                getattr(author, "global_name", None),
                getattr(author, "nick", None),
                getattr(author, "display_name", None),
                role_ids=_member_role_ids(author),
                in_guild=True,
            )
        except Exception:
            pass

        settings = await get_guild_settings(self.bot.db_pool, message.guild.id)
        msg_xp = int(settings.get("message_xp", 5))
        cooldown = int(settings.get("message_cooldown_sec", 60))
        if msg_xp <= 0:
            return
        if not await can_gain_message_xp(self.bot.db_pool, message.guild.id, message.author.id, cooldown):
            return

        before_xp = await get_user_xp(self.bot.db_pool, message.guild.id, message.author.id)
        before_lv = xp_to_level(before_xp)

        xp = await add_user_xp(self.bot.db_pool, message.guild.id, message.author.id, msg_xp)
        await touch_last_message(self.bot.db_pool, message.guild.id, message.author.id)

        after_lv = xp_to_level(xp)
        if after_lv != before_lv:
            await self._sync_roles_for_level(message.guild, message.author.id, after_lv, reason=f"레벨 변경(Lv.{before_lv}→Lv.{after_lv})")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.guild is None or member.bot:
            return

        # Cache member info + roles
        try:
            await upsert_member_cache(
                self.bot.db_pool,
                member.guild.id,
                member.id,
                getattr(member, "name", None),
                getattr(member, "discriminator", None),
                getattr(member, "global_name", None),
                getattr(member, "nick", None),
                getattr(member, "display_name", None),
                role_ids=_member_role_ids(member),
                in_guild=True,
            )
        except Exception:
            pass

        key = (member.guild.id, member.id)
        from datetime import datetime, timezone

        # join
        if before.channel is None and after.channel is not None:
            self.bot._voice_joined_at[key] = datetime.now(tz=timezone.utc)
            return

        # leave
        if before.channel is not None and after.channel is None:
            joined = self.bot._voice_joined_at.pop(key, None)
            if not joined:
                return
            secs = int((datetime.now(tz=timezone.utc) - joined).total_seconds())
            mins = secs // 60
            if mins <= 0:
                return
            settings = await get_guild_settings(self.bot.db_pool, member.guild.id)
            per_min = int(settings.get("voice_xp_per_min", 2))
            if per_min <= 0:
                return
            delta = mins * per_min
            before_xp = await get_user_xp(self.bot.db_pool, member.guild.id, member.id)
            before_lv = xp_to_level(before_xp)
            xp = await add_user_xp(self.bot.db_pool, member.guild.id, member.id, delta)
            after_lv = xp_to_level(xp)
            if after_lv != before_lv:
                await self._sync_roles_for_level(member.guild, member.id, after_lv, reason=f"레벨 변경(Lv.{before_lv}→Lv.{after_lv})")

    @app_commands.command(
        name=app_commands.locale_str("checkin", key="cmd_checkin_name"),
        description=app_commands.locale_str("출석체크 (한국 기준 하루 1회)", key="cmd_checkin_desc"),
    )
    async def checkin(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.followup.send("길드에서만 사용 가능해요.", ephemeral=True)
            return

        # Quick ack to avoid "응답하지 않았어요" (in case DB/network is slow)
        await interaction.response.defer(ephemeral=True)

        # Cache member info + roles
        try:
            m = interaction.user
            await upsert_member_cache(
                self.bot.db_pool,
                interaction.guild.id,
                m.id,
                getattr(m, "name", None),
                getattr(m, "discriminator", None),
                getattr(m, "global_name", None),
                getattr(m, "nick", None),
                getattr(m, "display_name", None),
                role_ids=_member_role_ids(m),
                in_guild=True,
            )
        except Exception:
            pass

        settings = await get_guild_settings(self.bot.db_pool, interaction.guild.id)
        limit_enabled = bool(settings.get("checkin_limit_enabled", True))

        today = kst_today_ymd()
        yesterday = kst_yesterday_ymd()

        if limit_enabled:
            ok = await record_checkin(self.bot.db_pool, interaction.guild.id, interaction.user.id, today)
            if not ok:
                await interaction.followup.send("오늘은 이미 출석했어요! ❄️", ephemeral=True)
                return
            streak = await update_checkin_streak(self.bot.db_pool, interaction.guild.id, interaction.user.id, today, yesterday)
        else:
            # 테스트 모드(제한 OFF)에서는 같은 날 여러 번 출석해도 streak/보너스가 오르도록 허용
            streak = await increment_checkin_streak_test_mode(
                self.bot.db_pool, interaction.guild.id, interaction.user.id, today
            )

        base_xp = int(settings.get("checkin_xp", 50))
        bonus_per_day = int(settings.get("checkin_streak_bonus_per_day", 0))
        bonus_cap = int(settings.get("checkin_streak_bonus_cap", 0))

        bonus = 0
        if bonus_per_day > 0 and streak > 1:
            bonus = (streak - 1) * bonus_per_day
            if bonus_cap > 0:
                bonus = min(bonus, bonus_cap)

        delta = base_xp + bonus

        before_xp = await get_user_xp(self.bot.db_pool, interaction.guild.id, interaction.user.id)
        before_lv = xp_to_level(before_xp)
        xp = await add_user_xp(self.bot.db_pool, interaction.guild.id, interaction.user.id, delta)
        after_lv = xp_to_level(xp)

        if after_lv != before_lv:
            await self._sync_roles_for_level(interaction.guild, interaction.user.id, after_lv, reason=f"레벨 변경(Lv.{before_lv}→Lv.{after_lv})")

        msg = f"✅ 출석 완료! +{delta}XP"
        if bonus > 0:
            msg += f" (연속 {streak}일 +{bonus}XP 보너스)"
        msg += f"\n현재 {xp}XP / Lv.{after_lv}"
        await interaction.followup.send(msg, ephemeral=True)

    @app_commands.command(
        name=app_commands.locale_str("profile", key="cmd_profile_name"),
        description=app_commands.locale_str("내 레벨/경험치 확인", key="cmd_profile_desc"),
    )
    async def profile(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("길드에서만 사용 가능해요.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        xp = await get_user_xp(self.bot.db_pool, interaction.guild.id, interaction.user.id)
        lvl = xp_to_level(xp)
        c = await get_checkin_count(self.bot.db_pool, interaction.guild.id, interaction.user.id)
        streak_info = await get_checkin_streak(self.bot.db_pool, interaction.guild.id, interaction.user.id)
        streak = int(streak_info.get("streak") or 0)
        await interaction.response.send_message(
            f"👤 {interaction.user.mention}\nXP: {xp}\n레벨: Lv.{lvl}\n출석: {c}회\n연속 출석: {streak}일",
            ephemeral=True,
        )

    @app_commands.command(
        name=app_commands.locale_str("leaderboard", key="cmd_leaderboard_name"),
        description=app_commands.locale_str("서버 랭킹 (전체)", key="cmd_leaderboard_desc"),
    )
    async def leaderboard(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("길드에서만 사용 가능해요.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        total = await count_ranked_members(self.bot.db_pool, interaction.guild.id)
        if total <= 0:
            await interaction.followup.send("아직 데이터가 없어요.", ephemeral=True)
            return
        view = LeaderboardView(self, interaction.guild.id, interaction.user.id, page=0, per_page=10, total=total)
        embed = await view._render()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(LevelingCog(bot))
