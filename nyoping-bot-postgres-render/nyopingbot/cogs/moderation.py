from __future__ import annotations

from discord import app_commands
from discord.ext import commands

class ModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name=app_commands.locale_str("clean", key="cmd_clean_name"), description=app_commands.locale_str("최근 메시지 N개 삭제", key="cmd_clean_desc"))
    @app_commands.checks.has_permissions(manage_messages=True)
    async def clean(self, interaction, count: app_commands.Range[int, 1, 200]):
        if not interaction.channel:
            await interaction.response.send_message("채널에서만 가능해요.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(limit=int(count))
        await interaction.followup.send(f"🧹 {len(deleted)}개 삭제 완료!", ephemeral=True)

    @clean.error
    async def clean_error(self, interaction, error):
        from discord import app_commands as ac
        if isinstance(error, ac.errors.MissingPermissions):
            await interaction.response.send_message("메시지 관리 권한이 필요해요.", ephemeral=True)
        else:
            await interaction.response.send_message("오류가 발생했어요.", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(ModerationCog(bot))
