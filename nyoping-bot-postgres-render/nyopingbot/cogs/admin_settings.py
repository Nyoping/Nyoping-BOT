from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..db import get_guild_settings, update_guild_settings

def is_admin(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    return interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.administrator

class SettingsGroup(app_commands.Group):
    def __init__(self, bot: commands.Bot):
        super().__init__(name="settings", description="뇨핑봇 설정(관리자)")
        self.bot = bot

    @app_commands.command(name="view", description="현재 설정 보기")
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

    @app_commands.command(name="set_checkin_xp", description="출석 XP 설정")
    async def set_checkin_xp(self, interaction: discord.Interaction, xp: app_commands.Range[int, 0, 10000]):
        if not is_admin(interaction):
            await interaction.response.send_message("서버 관리 권한이 필요해요.", ephemeral=True)
            return
        await update_guild_settings(self.bot.db_pool, interaction.guild.id, checkin_xp=int(xp))
        await interaction.response.send_message(f"✅ 출석 XP = {xp}", ephemeral=True)

    @app_commands.command(name="toggle_checkin_limit", description="출석 제한 ON/OFF (테스트용)")
    async def toggle_checkin_limit(self, interaction: discord.Interaction, enabled: bool):
        if not is_admin(interaction):
            await interaction.response.send_message("서버 관리 권한이 필요해요.", ephemeral=True)
            return
        await update_guild_settings(self.bot.db_pool, interaction.guild.id, checkin_limit_enabled=bool(enabled))
        await interaction.response.send_message(f"✅ 출석 제한 = {'ON' if enabled else 'OFF'}", ephemeral=True)

    @app_commands.command(name="set_message_xp", description="채팅 XP/쿨다운 설정")
    async def set_message_xp(self, interaction: discord.Interaction, xp: app_commands.Range[int, 0, 1000], cooldown_sec: app_commands.Range[int, 0, 3600] = 60):
        if not is_admin(interaction):
            await interaction.response.send_message("서버 관리 권한이 필요해요.", ephemeral=True)
            return
        await update_guild_settings(self.bot.db_pool, interaction.guild.id, message_xp=int(xp), message_cooldown_sec=int(cooldown_sec))
        await interaction.response.send_message(f"✅ 채팅 XP={xp}, 쿨다운={cooldown_sec}s", ephemeral=True)

    @app_commands.command(name="set_voice_xp", description="음성 XP(분당) 설정")
    async def set_voice_xp(self, interaction: discord.Interaction, xp_per_min: app_commands.Range[int, 0, 1000]):
        if not is_admin(interaction):
            await interaction.response.send_message("서버 관리 권한이 필요해요.", ephemeral=True)
            return
        await update_guild_settings(self.bot.db_pool, interaction.guild.id, voice_xp_per_min=int(xp_per_min))
        await interaction.response.send_message(f"✅ 음성 XP(분당)={xp_per_min}", ephemeral=True)

class AdminSettingsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.tree.add_command(SettingsGroup(bot))

async def setup(bot: commands.Bot):
    await bot.add_cog(AdminSettingsCog(bot))
