from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..db import set_level_role_rule, list_level_role_rules, remove_level_role_rule
from .admin_settings import is_admin

class LevelRoleGroup(app_commands.Group):
    def __init__(self, bot: commands.Bot):
        super().__init__(name=app_commands.locale_str("levelrole", key="grp_levelrole_name"), description=app_commands.locale_str("레벨 역할 설정(관리자)", key="grp_levelrole_desc"))
        self.bot = bot

    @app_commands.command(name=app_commands.locale_str("set", key="levelrole_set_name"), description=app_commands.locale_str("레벨 도달 시 역할 추가/제거 규칙 설정", key="levelrole_set_desc"))
    async def set_rule(self, interaction: discord.Interaction, level: app_commands.Range[int, 0, 100000], add_role: discord.Role, remove_role: discord.Role | None = None):
        if not is_admin(interaction):
            await interaction.response.send_message("서버 관리 권한이 필요해요.", ephemeral=True)
            return
        await set_level_role_rule(self.bot.db_pool, interaction.guild.id, int(level), int(add_role.id), int(remove_role.id) if remove_role else None)
        await interaction.response.send_message(f"✅ Lv.{level}: +{add_role.mention}" + (f" / -{remove_role.mention}" if remove_role else ""), ephemeral=True)

    @app_commands.command(name=app_commands.locale_str("list", key="levelrole_list_name"), description=app_commands.locale_str("규칙 목록", key="levelrole_list_desc"))
    async def list_rules(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("서버 관리 권한이 필요해요.", ephemeral=True)
            return
        rules = await list_level_role_rules(self.bot.db_pool, interaction.guild.id)
        if not rules:
            await interaction.response.send_message("규칙이 없어요.", ephemeral=True)
            return
        lines = []
        for r in rules:
            lvl = int(r["level"])
            add = int(r["add_role_id"])
            rem = r.get("remove_role_id")
            lines.append(f"Lv.{lvl}: +<@&{add}>" + (f" / -<@&{int(rem)}>" if rem else ""))
        await interaction.response.send_message("📌 레벨 역할 규칙\n" + "\n".join(lines), ephemeral=True)

    @app_commands.command(name=app_commands.locale_str("remove", key="levelrole_remove_name"), description=app_commands.locale_str("규칙 삭제", key="levelrole_remove_desc"))
    async def remove_rule(self, interaction: discord.Interaction, level: app_commands.Range[int, 0, 100000]):
        if not is_admin(interaction):
            await interaction.response.send_message("서버 관리 권한이 필요해요.", ephemeral=True)
            return
        await remove_level_role_rule(self.bot.db_pool, interaction.guild.id, int(level))
        await interaction.response.send_message(f"✅ Lv.{level} 규칙 삭제 완료", ephemeral=True)

class LevelRolesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.tree.add_command(LevelRoleGroup(bot))

async def setup(bot: commands.Bot):
    await bot.add_cog(LevelRolesCog(bot))
