from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..db import (
    get_guild_settings, add_user_xp, get_user_xp, can_gain_message_xp, touch_last_message,
    record_checkin, get_checkin_count, top_users, get_level_role_rule
)
from ..utils import kst_today_ymd, xp_to_level

class LevelingCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        settings = await get_guild_settings(self.bot.db_pool, message.guild.id)
        msg_xp = int(settings.get("message_xp", 5))
        cooldown = int(settings.get("message_cooldown_sec", 60))
        if msg_xp <= 0:
            return
        if not await can_gain_message_xp(self.bot.db_pool, message.guild.id, message.author.id, cooldown):
            return

        before = xp_to_level(await get_user_xp(self.bot.db_pool, message.guild.id, message.author.id))
        xp = await add_user_xp(self.bot.db_pool, message.guild.id, message.author.id, msg_xp)
        await touch_last_message(self.bot.db_pool, message.guild.id, message.author.id)
        after = xp_to_level(xp)
        if after > before:
            await self._apply_level_roles(message.guild, message.author.id, after)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.guild is None or member.bot:
            return
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
            before_lv = xp_to_level(await get_user_xp(self.bot.db_pool, member.guild.id, member.id))
            xp = await add_user_xp(self.bot.db_pool, member.guild.id, member.id, delta)
            after_lv = xp_to_level(xp)
            if after_lv > before_lv:
                await self._apply_level_roles(member.guild, member.id, after_lv)

    async def _apply_level_roles(self, guild: discord.Guild, user_id: int, new_level: int):
        rule = await get_level_role_rule(self.bot.db_pool, guild.id, new_level)
        if not rule:
            return
        add_role_id = int(rule["add_role_id"])
        rem_role_id = int(rule["remove_role_id"]) if rule.get("remove_role_id") else None

        member = guild.get_member(user_id)
        if member is None:
            return
        add_role = guild.get_role(add_role_id)
        rem_role = guild.get_role(rem_role_id) if rem_role_id else None
        try:
            if rem_role and rem_role in member.roles:
                await member.remove_roles(rem_role, reason=f"Reached level {new_level}")
            if add_role and add_role not in member.roles:
                await member.add_roles(add_role, reason=f"Reached level {new_level}")
        except discord.Forbidden:
            pass

    @app_commands.command(name="checkin", description="출석체크 (한국 기준 하루 1회)")
    async def checkin(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("길드에서만 사용 가능해요.", ephemeral=True)
            return
        settings = await get_guild_settings(self.bot.db_pool, interaction.guild.id)
        limit_enabled = bool(settings.get("checkin_limit_enabled", True))
        ymd = kst_today_ymd()
        if limit_enabled:
            ok = await record_checkin(self.bot.db_pool, interaction.guild.id, interaction.user.id, ymd)
            if not ok:
                await interaction.response.send_message("오늘은 이미 출석했어요! ❄️", ephemeral=True)
                return

        delta = int(settings.get("checkin_xp", 50))
        before = xp_to_level(await get_user_xp(self.bot.db_pool, interaction.guild.id, interaction.user.id))
        xp = await add_user_xp(self.bot.db_pool, interaction.guild.id, interaction.user.id, delta)
        after = xp_to_level(xp)
        if after > before:
            await self._apply_level_roles(interaction.guild, interaction.user.id, after)

        await interaction.response.send_message(f"✅ 출석 완료! +{delta}XP (현재 {xp}XP / Lv.{after})", ephemeral=True)

    @app_commands.command(name="profile", description="내 레벨/경험치 확인")
    async def profile(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("길드에서만 사용 가능해요.", ephemeral=True)
            return
        xp = await get_user_xp(self.bot.db_pool, interaction.guild.id, interaction.user.id)
        lvl = xp_to_level(xp)
        c = await get_checkin_count(self.bot.db_pool, interaction.guild.id, interaction.user.id)
        await interaction.response.send_message(f"👤 {interaction.user.mention}\nXP: {xp}\n레벨: Lv.{lvl}\n출석: {c}회", ephemeral=True)

    @app_commands.command(name="leaderboard", description="서버 랭킹 TOP 10")
    async def leaderboard(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("길드에서만 사용 가능해요.", ephemeral=True)
            return
        rows = await top_users(self.bot.db_pool, interaction.guild.id, 10)
        if not rows:
            await interaction.response.send_message("아직 데이터가 없어요.", ephemeral=True)
            return
        lines = []
        for i, r in enumerate(rows, start=1):
            uid = int(r["user_id"])
            xp = int(r["xp"])
            lines.append(f"{i}. <@{uid}> — {xp}XP (Lv.{xp_to_level(xp)})")
        await interaction.response.send_message("🏆 TOP 10\n" + "\n".join(lines))

async def setup(bot: commands.Bot):
    await bot.add_cog(LevelingCog(bot))
