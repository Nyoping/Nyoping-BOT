from __future__ import annotations

import math
import logging
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
    get_current_member_rank,
)
from ..utils import kst_today_ymd, kst_yesterday_ymd, xp_to_level
from ..role_sync import compute_expected_and_managed_roles, sync_member_roles

log = logging.getLogger(__name__)


def _member_role_ids(member: discord.abc.User) -> list[int]:
    try:
        if isinstance(member, discord.Member):
            return [int(r.id) for r in member.roles if r and r.id]
    except Exception:
        pass
    return []



async def _resolve_notify_channel(bot: commands.Bot, guild: discord.Guild | None):
    if guild is None:
        return None
    try:
        s = await get_guild_settings(bot.db_pool, guild.id)
        cid = int(s.get("notify_channel_id") or 0)
        if cid <= 0:
            return None
        ch = guild.get_channel(cid) or bot.get_channel(cid)
        return ch
    except Exception:
        return None

def _normalize_command_delivery_mode(value: str | None, default: str = "ephemeral") -> str:
    mode = str(value or default).strip().lower()
    return mode if mode in {"ephemeral", "channel", "dm"} else default

def _normalize_auto_delivery_mode(value: str | None, default: str = "channel") -> str:
    mode = str(value or default).strip().lower()
    return mode if mode in {"channel", "dm", "off"} else default

async def _deliver_command_result(
    bot: commands.Bot,
    interaction: discord.Interaction,
    *,
    mode: str,
    content: str | None = None,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
) -> None:
    mode = _normalize_command_delivery_mode(mode, "ephemeral")
    if mode == "ephemeral":
        await interaction.followup.send(content=content, embed=embed, view=view, ephemeral=True)
        return

    if mode == "dm":
        try:
            await interaction.user.send(content=content, embed=embed, view=view)
            await interaction.followup.send("DM으로 보냈어요.", ephemeral=True)
            return
        except Exception:
            await interaction.followup.send(content=content or "DM 전송에 실패해서 본인만 보기로 대신 보여드려요.", embed=embed, view=view, ephemeral=True)
            return

    ch = await _resolve_notify_channel(bot, interaction.guild)
    if ch is None:
        await interaction.followup.send(content=content or "공개 알림 채널이 설정되지 않아서 본인만 보기로 대신 보여드려요.", embed=embed, view=view, ephemeral=True)
        return

    await ch.send(content=content, embed=embed, view=view)
    try:
        mention = getattr(ch, "mention", "#알림채널")
    except Exception:
        mention = "#알림채널"
    await interaction.followup.send(f"{mention} 채널로 보냈어요.", ephemeral=True)

async def _send_auto_notice(
    bot: commands.Bot,
    guild: discord.Guild | None,
    user: discord.abc.User | None,
    *,
    mode: str,
    text: str,
) -> None:
    mode = _normalize_auto_delivery_mode(mode, "channel")
    if mode == "off":
        return

    if mode == "dm" and user is not None:
        try:
            await user.send(text)
            return
        except Exception:
            return

    ch = await _resolve_notify_channel(bot, guild)
    if ch is None:
        return
    try:
        await ch.send(text)
    except Exception:
        return


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
                getattr(getattr(author, "display_avatar", None), "url", None),
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
            added_ids, removed_ids = await self._sync_roles_for_level(message.guild, message.author.id, after_lv, reason=f"레벨 변경(Lv.{before_lv}→Lv.{after_lv})")
            role_bits = []
            if added_ids:
                role_bits.append("추가: " + " ".join(f"<@&{i}>" for i in added_ids))
            if removed_ids:
                role_bits.append("제거: " + " ".join(f"<@&{i}>" for i in removed_ids))
            extra = "\n" + "\n".join(role_bits) if role_bits else ""
            await _send_notify_message(self.bot, message.guild.id, f"📈 {message.author.mention} 레벨 업! Lv.{before_lv} → Lv.{after_lv}{extra}")

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
            settings = await get_guild_settings(self.bot.db_pool, member.guild.id)
            await _send_auto_notice(
                self.bot,
                member.guild,
                member,
                mode=str(settings.get("voice_xp_delivery_mode") or ("dm" if bool(settings.get("voice_dm_summary_enabled", True)) else "off")),
                text=f"🎧 {member.mention} 이번 통화로 +{delta}XP를 얻었어요. ({mins}분)",
            )
            after_lv = xp_to_level(xp)
            if after_lv != before_lv:
                added_ids, removed_ids = await self._sync_roles_for_level(member.guild, member.id, after_lv, reason=f"레벨 변경(Lv.{before_lv}→Lv.{after_lv})")
                role_bits = []
                if added_ids:
                    role_bits.append("추가: " + " ".join(f"<@&{i}>" for i in added_ids))
                if removed_ids:
                    role_bits.append("제거: " + " ".join(f"<@&{i}>" for i in removed_ids))
                extra = "\n" + "\n".join(role_bits) if role_bits else ""
                await _send_auto_notice(
                    self.bot,
                    member.guild,
                    member,
                    mode=str(settings.get("levelup_delivery_mode") or "channel"),
                    text=f"🎙️ {member.mention} 레벨 업! Lv.{before_lv} → Lv.{after_lv}{extra}",
                )

    @app_commands.command(
        name=app_commands.locale_str("checkin", key="cmd_checkin_name"),
        description=app_commands.locale_str("출석체크 (한국 기준 하루 1회)", key="cmd_checkin_desc"),
    )
    async def checkin(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("길드에서만 사용 가능해요.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
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
                streak = await increment_checkin_streak_test_mode(
                    self.bot.db_pool, interaction.guild.id, interaction.user.id, today
                )

            base_xp = int(settings.get("checkin_xp", 50))
            bonus_per_day = int(settings.get("checkin_streak_bonus_per_day", 0))
            bonus_cap = int(settings.get("checkin_streak_bonus_cap", 0))

            bonus = 0
            effective_bonus_per_day = int(bonus_per_day)
            if not limit_enabled and effective_bonus_per_day <= 0:
                effective_bonus_per_day = 10
            if effective_bonus_per_day > 0 and streak > 1:
                bonus = (streak - 1) * effective_bonus_per_day
                if bonus_cap > 0:
                    bonus = min(bonus, bonus_cap)

            delta = base_xp + bonus

            before_xp = await get_user_xp(self.bot.db_pool, interaction.guild.id, interaction.user.id)
            before_lv = xp_to_level(before_xp)
            xp = await add_user_xp(self.bot.db_pool, interaction.guild.id, interaction.user.id, delta)
            after_lv = xp_to_level(xp)

            if after_lv != before_lv:
                added_ids = []
                removed_ids = []
                try:
                    res = await self._sync_roles_for_level(
                        interaction.guild,
                        interaction.user.id,
                        after_lv,
                        reason=f"레벨 변경(Lv.{before_lv}→Lv.{after_lv})",
                    )
                    if res:
                        added_ids, removed_ids = res
                except Exception:
                    log.exception("checkin role sync failed guild=%s user=%s", interaction.guild.id, interaction.user.id)

                role_bits = []
                if added_ids:
                    role_bits.append("추가: " + " ".join(f"<@&{i}>" for i in added_ids))
                if removed_ids:
                    role_bits.append("제거: " + " ".join(f"<@&{i}>" for i in removed_ids))
                extra = "\n" + "\n".join(role_bits) if role_bits else ""
                try:
                    await _send_auto_notice(
                        self.bot,
                        interaction.guild,
                        interaction.user,
                        mode=str(settings.get("levelup_delivery_mode") or "channel"),
                        text=f"✅ {interaction.user.mention} 레벨 업! Lv.{before_lv} → Lv.{after_lv}{extra}",
                    )
                except Exception:
                    log.exception("checkin notify failed guild=%s user=%s", interaction.guild.id, interaction.user.id)

            msg = f"✅ 출석 완료! +{delta}XP"
            if bonus > 0:
                msg += f" (연속 {streak}일 +{bonus}XP 보너스)"
                if not limit_enabled and bonus_per_day <= 0:
                    msg += " [테스트 모드 기본 보너스 적용]"
            msg += f"\n현재 {xp}XP / Lv.{after_lv}"
            await _deliver_command_result(
                self.bot,
                interaction,
                mode=str(settings.get("checkin_delivery_mode") or "ephemeral"),
                content=msg,
            )
        except Exception:
            log.exception("checkin command failed guild=%s user=%s", getattr(interaction.guild, 'id', None), getattr(interaction.user, 'id', None))
            try:
                await interaction.followup.send("출석 처리 중 오류가 발생했어요. 다시 한 번 시도해 주세요.", ephemeral=True)
            except Exception:
                pass


    @app_commands.command(
        name=app_commands.locale_str("profile", key="cmd_profile_name"),
        description=app_commands.locale_str("내 프로필 보기", key="cmd_profile_desc"),
    )
    async def profile(self, interaction: discord.Interaction, user: discord.Member | None = None):
        if not interaction.guild:
            await interaction.response.send_message("길드에서만 사용 가능해요.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        target = user or interaction.user
        try:
            await upsert_member_cache(
                self.bot.db_pool,
                interaction.guild.id,
                target.id,
                getattr(target, "name", None),
                getattr(target, "discriminator", None),
                getattr(target, "global_name", None),
                getattr(target, "nick", None),
                getattr(target, "display_name", None),
                getattr(getattr(target, "display_avatar", None), "url", None),
                role_ids=_member_role_ids(target),
                in_guild=True,
            )
        except Exception:
            pass

        settings = await get_guild_settings(self.bot.db_pool, interaction.guild.id)
        xp = await get_user_xp(self.bot.db_pool, interaction.guild.id, target.id)
        lvl = xp_to_level(xp)
        c = await get_checkin_count(self.bot.db_pool, interaction.guild.id, target.id)
        streak_info = await get_checkin_streak(self.bot.db_pool, interaction.guild.id, target.id)
        streak = int(streak_info.get("streak") or 0)
        rank = await get_current_member_rank(self.bot.db_pool, interaction.guild.id, target.id)
        total = await count_ranked_members(self.bot.db_pool, interaction.guild.id)
        rank_line = f"현재 랭킹: {rank}위 / {total}명" if rank is not None and total > 0 else "현재 랭킹: 집계 전"
        await _deliver_command_result(
            self.bot,
            interaction,
            mode=str(settings.get("profile_delivery_mode") or "ephemeral"),
            content=f"👤 {target.mention}\nXP: {xp}\n레벨: Lv.{lvl}\n{rank_line}\n출석: {c}회\n연속 출석: {streak}일",
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
        settings = await get_guild_settings(self.bot.db_pool, interaction.guild.id)
        await _deliver_command_result(
            self.bot,
            interaction,
            mode=str(settings.get("leaderboard_delivery_mode") or "ephemeral"),
            embed=embed,
            view=view,
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(LevelingCog(bot))
