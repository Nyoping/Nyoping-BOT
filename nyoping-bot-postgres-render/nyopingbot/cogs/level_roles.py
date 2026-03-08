from __future__ import annotations

import re
import discord
from discord import app_commands
from discord.ext import commands

from ..db import set_level_role_set, list_level_role_sets, remove_level_role_set
from .admin_settings import is_admin


def _parse_role_ids(text: str) -> list[int]:
    """Accept role mentions (<@&id>), plain IDs, or comma/space separated."""
    if not text:
        return []
    ids = re.findall(r"(\d{10,25})", text)
    out = []
    for s in ids:
        try:
            out.append(int(s))
        except Exception:
            pass
    # unique preserve order
    seen=set()
    uniq=[]
    for i in out:
        if i not in seen:
            seen.add(i); uniq.append(i)
    return uniq


class LevelRoleGroup(app_commands.Group):
    def __init__(self, bot: commands.Bot):
        super().__init__(
            name=app_commands.locale_str("levelrole", key="grp_levelrole_name"),
            description=app_commands.locale_str("레벨 역할 설정(관리자)", key="grp_levelrole_desc"),
        )
        self.bot = bot

    @app_commands.command(
        name=app_commands.locale_str("set", key="levelrole_set_name"),
        description=app_commands.locale_str("레벨 구간 규칙 설정 (여러 역할 가능)", key="levelrole_set_desc"),
    )
    async def set_rule(
        self,
        interaction: discord.Interaction,
        level: app_commands.Range[int, 0, 100000],
        add_roles: str = "",
        remove_roles: str = "",
    ):
        if not is_admin(interaction):
            await interaction.response.send_message("서버 관리 권한이 필요해요.", ephemeral=True)
            return
        add_ids = _parse_role_ids(add_roles)
        rem_ids = _parse_role_ids(remove_roles)
        if not add_ids and not rem_ids:
            await interaction.response.send_message("추가/제거 역할 중 하나는 꼭 넣어야 해요.", ephemeral=True)
            return
        await set_level_role_set(self.bot.db_pool, interaction.guild.id, int(level), add_ids, rem_ids)
        await self.bot.db_pool.execute(
            """INSERT INTO role_sync_queue (guild_id, user_id, requested_at, processed_at)
               SELECT $1, user_id, NOW(), NULL
               FROM guild_members_cache
               WHERE guild_id=$1 AND in_guild=TRUE
               ON CONFLICT (guild_id, user_id) DO UPDATE SET requested_at=NOW(), processed_at=NULL""",
            int(interaction.guild.id)
        )
        msg = f"✅ Lv.{level} 규칙 저장\n"
        if add_ids:
            msg += "추가: " + " ".join([f"<@&{i}>" for i in add_ids]) + "\n"
        if rem_ids:
            msg += "제거: " + " ".join([f"<@&{i}>" for i in rem_ids]) + "\n"
        msg += "(규칙은 누적 적용됩니다: Lv.X~다음규칙-1 구간)"
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(
        name=app_commands.locale_str("list", key="levelrole_list_name"),
        description=app_commands.locale_str("규칙 목록", key="levelrole_list_desc"),
    )
    async def list_rules(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("서버 관리 권한이 필요해요.", ephemeral=True)
            return
        rules = await list_level_role_sets(self.bot.db_pool, interaction.guild.id)
        if not rules:
            await interaction.response.send_message("규칙이 없어요.", ephemeral=True)
            return
        lines = []
        for r in rules:
            lvl = int(r["level"])
            add_ids = r.get("add_role_ids") or []
            rem_ids = r.get("remove_role_ids") or []
            part = f"Lv.{lvl}: "
            if add_ids:
                part += "+ " + " ".join([f"<@&{int(i)}>" for i in add_ids]) + " "
            if rem_ids:
                part += "/ - " + " ".join([f"<@&{int(i)}>" for i in rem_ids])
            lines.append(part.strip())
        await interaction.response.send_message("📌 레벨 역할 규칙\n" + "\n".join(lines), ephemeral=True)

    @app_commands.command(
        name=app_commands.locale_str("remove", key="levelrole_remove_name"),
        description=app_commands.locale_str("규칙 삭제", key="levelrole_remove_desc"),
    )
    async def remove_rule(self, interaction: discord.Interaction, level: app_commands.Range[int, 0, 100000]):
        if not is_admin(interaction):
            await interaction.response.send_message("서버 관리 권한이 필요해요.", ephemeral=True)
            return
        await remove_level_role_set(self.bot.db_pool, interaction.guild.id, int(level))
        await self.bot.db_pool.execute(
            """INSERT INTO role_sync_queue (guild_id, user_id, requested_at, processed_at)
               SELECT $1, user_id, NOW(), NULL
               FROM guild_members_cache
               WHERE guild_id=$1 AND in_guild=TRUE
               ON CONFLICT (guild_id, user_id) DO UPDATE SET requested_at=NOW(), processed_at=NULL""",
            int(interaction.guild.id)
        )
        await interaction.response.send_message(f"✅ Lv.{level} 규칙 삭제 완료", ephemeral=True)


class LevelRolesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.tree.add_command(LevelRoleGroup(bot))


async def setup(bot: commands.Bot):
    await bot.add_cog(LevelRolesCog(bot))
