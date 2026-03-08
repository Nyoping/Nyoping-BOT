from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..db import (
    get_guild_settings,
    update_guild_settings,
    reset_checkin,
    set_user_xp,
    list_level_role_sets,
)
from ..utils import kst_today_ymd, xp_to_level
from ..role_sync import compute_expected_and_managed_roles, sync_member_roles


def is_admin(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    return interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.administrator


def is_owner(interaction: discord.Interaction) -> bool:
    """Server owner only."""
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    return int(interaction.guild.owner_id or 0) == int(interaction.user.id)


async def _sync_roles_for_level(pool, guild: discord.Guild, member: discord.Member, level: int, *, reason: str) -> None:
    rules = await list_level_role_sets(pool, guild.id)
    if not rules:
        return
    expected, managed = compute_expected_and_managed_roles(rules, level)
    if not managed:
        return
    await sync_member_roles(member, expected, managed, reason=reason)


class SettingsGroup(app_commands.Group):
    def __init__(self, bot: commands.Bot):
        super().__init__(
            name=app_commands.locale_str("settings", key="grp_settings_name"),
            description=app_commands.locale_str("뇨핑봇 설정(관리자)", key="grp_settings_desc"),
        )
        self.bot = bot

    @app_commands.command(
        name=app_commands.locale_str("view", key="settings_view_name"),
        description=app_commands.locale_str("현재 설정 보기", key="settings_view_desc"),
    )
    async def view(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("서버 관리 권한이 필요해요.", ephemeral=True)
            return
        s = await get_guild_settings(self.bot.db_pool, interaction.guild.id)
        msg = (
            f"⚙️ 설정\n"
            f"- 출석 XP: {s.get('checkin_xp')}\n"
            f"- 출석 제한: {'ON' if s.get('checkin_limit_enabled') else 'OFF'}\n"
            f"- 연속 출석 보너스/일: {s.get('checkin_streak_bonus_per_day', 0)}\n"
            f"- 연속 출석 보너스 상한: {s.get('checkin_streak_bonus_cap', 0)}\n"
            f"- 채팅 XP: {s.get('message_xp')}\n"
            f"- 채팅 쿨다운: {s.get('message_cooldown_sec')}s\n"
            f"- 음성 XP(분당): {s.get('voice_xp_per_min')}\n"
        )
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(
        name=app_commands.locale_str("set_checkin_xp", key="settings_set_checkin_xp_name"),
        description=app_commands.locale_str("출석 XP 설정", key="settings_set_checkin_xp_desc"),
    )
    async def set_checkin_xp(self, interaction: discord.Interaction, xp: app_commands.Range[int, 0, 10000]):
        if not is_admin(interaction):
            await interaction.response.send_message("서버 관리 권한이 필요해요.", ephemeral=True)
            return
        await update_guild_settings(self.bot.db_pool, interaction.guild.id, checkin_xp=int(xp))
        await interaction.response.send_message(f"✅ 출석 XP = {xp}", ephemeral=True)

    @app_commands.command(
        name=app_commands.locale_str("set_checkin_streak_bonus", key="settings_set_checkin_streak_bonus_name"),
        description=app_commands.locale_str("연속 출석 보너스(일당/상한) 설정", key="settings_set_checkin_streak_bonus_desc"),
    )
    async def set_checkin_streak_bonus(
        self,
        interaction: discord.Interaction,
        bonus_per_day: app_commands.Range[int, 0, 10000],
        cap: app_commands.Range[int, 0, 100000] = 0,
    ):
        if not is_admin(interaction):
            await interaction.response.send_message("서버 관리 권한이 필요해요.", ephemeral=True)
            return
        await update_guild_settings(
            self.bot.db_pool,
            interaction.guild.id,
            checkin_streak_bonus_per_day=int(bonus_per_day),
            checkin_streak_bonus_cap=int(cap),
        )
        await interaction.response.send_message(f"✅ 연속 보너스/일={bonus_per_day}, 상한={cap}", ephemeral=True)

    @app_commands.command(
        name=app_commands.locale_str("toggle_checkin_limit", key="settings_toggle_checkin_limit_name"),
        description=app_commands.locale_str("출석 제한 ON/OFF (테스트용)", key="settings_toggle_checkin_limit_desc"),
    )
    async def toggle_checkin_limit(self, interaction: discord.Interaction, enabled: bool):
        if not is_admin(interaction):
            await interaction.response.send_message("서버 관리 권한이 필요해요.", ephemeral=True)
            return
        await update_guild_settings(self.bot.db_pool, interaction.guild.id, checkin_limit_enabled=bool(enabled))
        await interaction.response.send_message(f"✅ 출석 제한 = {'ON' if enabled else 'OFF'}", ephemeral=True)

    @app_commands.command(
        name=app_commands.locale_str("set_message_xp", key="settings_set_message_xp_name"),
        description=app_commands.locale_str("채팅 XP/쿨다운 설정", key="settings_set_message_xp_desc"),
    )
    async def set_message_xp(
        self,
        interaction: discord.Interaction,
        xp: app_commands.Range[int, 0, 1000],
        cooldown_sec: app_commands.Range[int, 0, 3600] = 60,
    ):
        if not is_admin(interaction):
            await interaction.response.send_message("서버 관리 권한이 필요해요.", ephemeral=True)
            return
        await update_guild_settings(
            self.bot.db_pool,
            interaction.guild.id,
            message_xp=int(xp),
            message_cooldown_sec=int(cooldown_sec),
        )
        await interaction.response.send_message(f"✅ 채팅 XP={xp}, 쿨다운={cooldown_sec}s", ephemeral=True)

    @app_commands.command(
        name=app_commands.locale_str("set_voice_xp", key="settings_set_voice_xp_name"),
        description=app_commands.locale_str("음성 XP(분당) 설정", key="settings_set_voice_xp_desc"),
    )
    async def set_voice_xp(self, interaction: discord.Interaction, xp_per_min: app_commands.Range[int, 0, 1000]):
        if not is_admin(interaction):
            await interaction.response.send_message("서버 관리 권한이 필요해요.", ephemeral=True)
            return
        await update_guild_settings(self.bot.db_pool, interaction.guild.id, voice_xp_per_min=int(xp_per_min))
        await interaction.response.send_message(f"✅ 음성 XP(분당)={xp_per_min}", ephemeral=True)

    @app_commands.command(
        name=app_commands.locale_str("reset_checkin", key="settings_reset_checkin_name"),
        description=app_commands.locale_str("특정 유저의 오늘 출석 기록을 초기화", key="settings_reset_checkin_desc"),
    )
    async def reset_checkin_today(self, interaction: discord.Interaction, member: discord.Member):
        """Reset today's check-in for a member (owner only)."""
        if not is_owner(interaction):
            await interaction.response.send_message("서버장(서버 소유자)만 사용할 수 있어요.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        ymd = kst_today_ymd()
        deleted = await reset_checkin(self.bot.db_pool, interaction.guild.id, member.id, ymd)
        if deleted:
            await interaction.followup.send(f"✅ {member.mention} 오늘({ymd}) 출석 기록을 초기화했어요.", ephemeral=True)
        else:
            await interaction.followup.send(f"ℹ️ {member.mention} 오늘({ymd}) 출석 기록이 없어요.", ephemeral=True)

    @app_commands.command(
        name=app_commands.locale_str("set_level", key="settings_set_level_name"),
        description=app_commands.locale_str("특정 유저의 레벨(경험치)을 강제로 설정", key="settings_set_level_desc"),
    )
    async def force_set_level(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        level: app_commands.Range[int, 0, 100000],
    ):
        """Force set a member's level (owner only)."""
        if not is_owner(interaction):
            await interaction.response.send_message("서버장(서버 소유자)만 사용할 수 있어요.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        target_xp = int(level) * 100
        await set_user_xp(self.bot.db_pool, interaction.guild.id, member.id, target_xp)
        new_lv = xp_to_level(target_xp)

        # Re-sync managed level roles for this member (works for level down too)
        await _sync_roles_for_level(
            self.bot.db_pool,
            interaction.guild,
            member,
            new_lv,
            reason=f"강제 레벨 설정(Lv.{new_lv})",
        )

        await interaction.followup.send(
            f"✅ {member.mention} 레벨을 Lv.{new_lv}로 설정했어요. (XP={target_xp})",
            ephemeral=True,
        )


class AdminSettingsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.tree.add_command(SettingsGroup(bot))


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminSettingsCog(bot))
