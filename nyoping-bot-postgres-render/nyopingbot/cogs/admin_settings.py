from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..db import get_guild_settings, update_guild_settings, reset_checkin, set_user_xp, get_level_role_rule
from ..utils import kst_today_ymd, xp_to_level

def is_admin(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    return interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.administrator


def is_owner(interaction: discord.Interaction) -> bool:
    """Server owner only."""
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    return int(interaction.guild.owner_id or 0) == int(interaction.user.id)

class SettingsGroup(app_commands.Group):
    def __init__(self, bot: commands.Bot):
        super().__init__(name=app_commands.locale_str("settings", key="grp_settings_name"), description=app_commands.locale_str("뇨핑봇 설정(관리자)", key="grp_settings_desc"))
        self.bot = bot

    @app_commands.command(name=app_commands.locale_str("view", key="settings_view_name"), description=app_commands.locale_str("현재 설정 보기", key="settings_view_desc"))
    async def view(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("서버 관리 권한이 필요해요.", ephemeral=True)
            return
        s = await get_guild_settings(self.bot.db_pool, interaction.guild.id)
        msg = (
            f"⚙️ 설정\n"
            f"- 출석 XP: {s.get('checkin_xp')}\n"
            f"- 출석 제한: {'ON' if s.get('checkin_limit_enabled') else 'OFF'}\n"
            f"- 채팅 XP: {s.get('message_xp')}\n"
            f"- 채팅 쿨다운: {s.get('message_cooldown_sec')}s\n"
            f"- 음성 XP(분당): {s.get('voice_xp_per_min')}\n"
        )
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name=app_commands.locale_str("set_checkin_xp", key="settings_set_checkin_xp_name"), description=app_commands.locale_str("출석 XP 설정", key="settings_set_checkin_xp_desc"))
    async def set_checkin_xp(self, interaction: discord.Interaction, xp: app_commands.Range[int, 0, 10000]):
        if not is_admin(interaction):
            await interaction.response.send_message("서버 관리 권한이 필요해요.", ephemeral=True)
            return
        await update_guild_settings(self.bot.db_pool, interaction.guild.id, checkin_xp=int(xp))
        await interaction.response.send_message(f"✅ 출석 XP = {xp}", ephemeral=True)

    @app_commands.command(name=app_commands.locale_str("toggle_checkin_limit", key="settings_toggle_checkin_limit_name"), description=app_commands.locale_str("출석 제한 ON/OFF (테스트용)", key="settings_toggle_checkin_limit_desc"))
    async def toggle_checkin_limit(self, interaction: discord.Interaction, enabled: bool):
        if not is_admin(interaction):
            await interaction.response.send_message("서버 관리 권한이 필요해요.", ephemeral=True)
            return
        await update_guild_settings(self.bot.db_pool, interaction.guild.id, checkin_limit_enabled=bool(enabled))
        await interaction.response.send_message(f"✅ 출석 제한 = {'ON' if enabled else 'OFF'}", ephemeral=True)

    @app_commands.command(name=app_commands.locale_str("set_message_xp", key="settings_set_message_xp_name"), description=app_commands.locale_str("채팅 XP/쿨다운 설정", key="settings_set_message_xp_desc"))
    async def set_message_xp(self, interaction: discord.Interaction, xp: app_commands.Range[int, 0, 1000], cooldown_sec: app_commands.Range[int, 0, 3600] = 60):
        if not is_admin(interaction):
            await interaction.response.send_message("서버 관리 권한이 필요해요.", ephemeral=True)
            return
        await update_guild_settings(self.bot.db_pool, interaction.guild.id, message_xp=int(xp), message_cooldown_sec=int(cooldown_sec))
        await interaction.response.send_message(f"✅ 채팅 XP={xp}, 쿨다운={cooldown_sec}s", ephemeral=True)

    @app_commands.command(name=app_commands.locale_str("set_voice_xp", key="settings_set_voice_xp_name"), description=app_commands.locale_str("음성 XP(분당) 설정", key="settings_set_voice_xp_desc"))
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
        ymd = kst_today_ymd()
        deleted = await reset_checkin(self.bot.db_pool, interaction.guild.id, member.id, ymd)
        if deleted:
            await interaction.response.send_message(f"✅ {member.mention} 오늘({ymd}) 출석 기록을 초기화했어요.", ephemeral=True)
        else:
            await interaction.response.send_message(f"ℹ️ {member.mention} 오늘({ymd}) 출석 기록이 없어요.", ephemeral=True)

    @app_commands.command(
        name=app_commands.locale_str("set_level", key="settings_set_level_name"),
        description=app_commands.locale_str("특정 유저의 레벨(경험치)을 강제로 설정", key="settings_set_level_desc"),
    )
    async def force_set_level(self, interaction: discord.Interaction, member: discord.Member, level: app_commands.Range[int, 0, 100000]):
        """Force set a member's level (owner only)."""
        if not is_owner(interaction):
            await interaction.response.send_message("서버장(서버 소유자)만 사용할 수 있어요.", ephemeral=True)
            return
        target_xp = int(level) * 100
        await set_user_xp(self.bot.db_pool, interaction.guild.id, member.id, target_xp)
        new_lv = xp_to_level(target_xp)

        # apply level role rule for this level (if any)
        rule = await get_level_role_rule(self.bot.db_pool, interaction.guild.id, new_lv)
        if rule:
            add_role_id = int(rule["add_role_id"])
            rem_role_id = int(rule["remove_role_id"]) if rule.get("remove_role_id") else None
            add_role = interaction.guild.get_role(add_role_id)
            rem_role = interaction.guild.get_role(rem_role_id) if rem_role_id else None
            try:
                if rem_role and rem_role in member.roles:
                    await member.remove_roles(rem_role, reason=f"Force set level {new_lv}")
                if add_role and add_role not in member.roles:
                    await member.add_roles(add_role, reason=f"Force set level {new_lv}")
            except discord.Forbidden:
                pass

        await interaction.response.send_message(
            f"✅ {member.mention} 레벨을 Lv.{new_lv}로 설정했어요. (XP={target_xp})",
            ephemeral=True,
        )

class AdminSettingsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.tree.add_command(SettingsGroup(bot))

async def setup(bot: commands.Bot):
    await bot.add_cog(AdminSettingsCog(bot))
