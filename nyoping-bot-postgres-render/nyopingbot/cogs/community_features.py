from __future__ import annotations

import asyncio
import io
import logging
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone

import discord
import requests
import subprocess
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

from ..db import get_guild_settings, update_guild_settings, top_users_current_members

log = logging.getLogger(__name__)
INVITE_RE = re.compile(r"(https?://)?(www\.)?(discord\.gg|discord(app)?\.com/invite)/[A-Za-z0-9\-]+", re.I)

BASE_DIR = Path(__file__).resolve().parents[2]
BUNDLED_FONTS_DIR = BASE_DIR / "fonts"
BUNDLED_SANS_FONTS = [
    str(BUNDLED_FONTS_DIR / "NotoSansKR-Regular.ttf"),
    str(BUNDLED_FONTS_DIR / "NotoSansKR-Medium.ttf"),
    str(BUNDLED_FONTS_DIR / "NotoSansKR-Bold.ttf"),
    str(BUNDLED_FONTS_DIR / "NanumGothic.ttf"),
    str(BUNDLED_FONTS_DIR / "NanumBarunGothic.ttf"),
    str(BUNDLED_FONTS_DIR / "NanumSquareNeo-bRg.ttf"),
    str(BUNDLED_FONTS_DIR / "MaruBuri-Regular.ttf"),
]
BUNDLED_SERIF_FONTS = [
    str(BUNDLED_FONTS_DIR / "NanumMyeongjo.ttf"),
    str(BUNDLED_FONTS_DIR / "MaruBuri-Regular.ttf"),
    str(BUNDLED_FONTS_DIR / "MaruBuri-Bold.ttf"),
]
BUNDLED_MONO_FONTS = [
    str(BUNDLED_FONTS_DIR / "D2CodingLigature.ttf"),
    str(BUNDLED_FONTS_DIR / "NotoSansKR-Regular.ttf"),
]


def _as_int_list(v) -> list[int]:
    try:
        return [int(x) for x in (v or []) if int(x) > 0]
    except Exception:
        return []


def _safe_font(name: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    lname = str(name or "default").lower()
    if lname in ("default", "sans", "sans-serif", "korean", "kr"):
        candidates = [
            *BUNDLED_SANS_FONTS,
            "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
            "/usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf",
            "/usr/share/fonts/truetype/nanum/NanumSquareR.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    elif lname == "serif":
        candidates = [
            *BUNDLED_SERIF_FONTS,
            "/usr/share/fonts/truetype/nanum/NanumMyeongjo.ttf",
            "/usr/share/fonts/truetype/noto/NotoSerifCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
            "DejaVuSerif.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
        ]
    elif lname in ("mono", "monospace"):
        candidates = [
            *BUNDLED_MONO_FONTS,
            "/usr/share/fonts/truetype/nanum/NanumGothicCoding.ttf",
            "DejaVuSansMono.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        ]
    else:
        custom_path = str(BUNDLED_FONTS_DIR / name)
        candidates = [
            custom_path,
            name,
            *BUNDLED_SANS_FONTS,
            "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
            "/usr/share/fonts/truetype/nanum/NanumSquareR.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    for query in ("sans:lang=ko", "Noto Sans CJK KR", "NanumGothic", "sans"):
        try:
            out = subprocess.check_output(["fc-match", "-f", "%{file}\n", query], text=True, timeout=2).strip()
            if out and out not in candidates:
                candidates.append(out)
        except Exception:
            pass
    seen = set()
    for c in candidates:
        if not c or c in seen:
            continue
        seen.add(c)
        try:
            return ImageFont.truetype(c, size=max(8, int(size)))
        except Exception:
            continue
    return ImageFont.load_default()


def _wrap_text_lines(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    raw_lines = str(text or "").split("\n")
    out: list[str] = []
    for raw in raw_lines:
        if not raw:
            out.append("")
            continue
        cur = ""
        for ch in raw:
            trial = cur + ch
            bbox = draw.textbbox((0, 0), trial, font=font)
            if cur and (bbox[2] - bbox[0]) > max_width:
                out.append(cur)
                cur = ch
            else:
                cur = trial
        out.append(cur)
    return out


def _contains_korean_text(text: str) -> bool:
    for ch in str(text or ""):
        o = ord(ch)
        if 0xAC00 <= o <= 0xD7A3 or 0x3131 <= o <= 0x318E or 0x1100 <= o <= 0x11FF:
            return True
    return False

def _replace_vars(
    template: str,
    *,
    member: discord.Member | None = None,
    guild: discord.Guild | None = None,
    inviter: discord.User | discord.Member | None = None,
    mode: str = "message",
    leave_reason: str | None = None,
) -> str:
    t = str(template or "")
    display_user = ""
    mention_user = ""
    discord_id = ""
    if member is not None:
        display_user = member.display_name
        mention_user = member.mention
        discord_id = str(member.id)
    server_name = guild.name if guild else ""
    inviter_mention = inviter.mention if inviter else "알 수 없음"
    inviter_name = getattr(inviter, "display_name", None) or getattr(inviter, "name", None) or "알 수 없음"
    mapping = {
        "[user]": mention_user if mode == "message" else display_user,
        "[server]": server_name,
        "[inviter]": inviter_mention if mode == "message" else inviter_name,
        "[discord]": discord_id,
        "[reason]": str(leave_reason or ""),
    }
    for k, v in mapping.items():
        t = t.replace(k, v)
    return t


class CommunityFeaturesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._leader_task: asyncio.Task | None = None
        self._invite_task: asyncio.Task | None = None
        self._afk_tasks: dict[tuple[int, int], asyncio.Task] = {}

    async def cog_load(self) -> None:
        self._leader_task = asyncio.create_task(self._leaderboard_loop())
        self._invite_task = asyncio.create_task(self._invite_sync_loop())

    async def cog_unload(self) -> None:
        for t in (self._leader_task, self._invite_task):
            if t and not t.done():
                t.cancel()
        for t in self._afk_tasks.values():
            if t and not t.done():
                t.cancel()

    async def _invite_sync_loop(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(3)
        while not self.bot.is_closed():
            try:
                for guild in self.bot.guilds:
                    await self._snapshot_invites(guild)
            except Exception:
                log.exception("invite snapshot loop failed")
            await asyncio.sleep(300)

    async def _leaderboard_loop(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(5)
        while not self.bot.is_closed():
            try:
                for guild in self.bot.guilds:
                    await self._refresh_leaderboard_for_guild(guild)
            except Exception:
                log.exception("leaderboard loop failed")
            await asyncio.sleep(300)

    async def _snapshot_invites(self, guild: discord.Guild) -> None:
        me = guild.me
        if me is None or not me.guild_permissions.manage_guild:
            return
        try:
            invites = await guild.invites()
        except Exception:
            return
        pool = self.bot.db_pool
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM invite_cache WHERE guild_id=$1", int(guild.id))
            for inv in invites:
                inviter_id = int(inv.inviter.id) if inv.inviter else 0
                uses = int(inv.uses or 0)
                await conn.execute(
                    "INSERT INTO invite_cache (guild_id, code, inviter_id, uses) VALUES ($1,$2,$3,$4)",
                    int(guild.id), str(inv.code), inviter_id, uses,
                )

    async def _detect_used_inviter(self, guild: discord.Guild):
        me = guild.me
        if me is None or not me.guild_permissions.manage_guild:
            return None
        try:
            invites = await guild.invites()
        except Exception:
            return None
        pool = self.bot.db_pool
        winner = None
        async with pool.acquire() as conn:
            old_rows = await conn.fetch(
                "SELECT code, inviter_id, uses FROM invite_cache WHERE guild_id=$1",
                int(guild.id),
            )
            old_map = {
                str(r["code"]): {"uses": int(r["uses"]), "inviter_id": int(r["inviter_id"])}
                for r in old_rows
            }
            await conn.execute("DELETE FROM invite_cache WHERE guild_id=$1", int(guild.id))
            for inv in invites:
                inviter_id = int(inv.inviter.id) if inv.inviter else 0
                uses = int(inv.uses or 0)
                await conn.execute(
                    "INSERT INTO invite_cache (guild_id, code, inviter_id, uses) VALUES ($1,$2,$3,$4)",
                    int(guild.id), str(inv.code), inviter_id, uses,
                )
                prev = old_map.get(str(inv.code), {}).get("uses", 0)
                if uses > int(prev or 0):
                    winner = inv.inviter
        return winner

    async def _resolve_text_channel(self, guild: discord.Guild, channel_id: int):
        ch = guild.get_channel(int(channel_id)) or self.bot.get_channel(int(channel_id))
        if ch is not None:
            return ch
        try:
            return await guild.fetch_channel(int(channel_id))
        except Exception:
            return None

    async def _build_welcome_image_bytes(
        self, member: discord.Member, guild: discord.Guild, settings: dict, inviter
    ) -> bytes | None:
        bg_url = str(settings.get("welcome_background_url") or "").strip()
        if not bg_url:
            return None

        def _iter_text_layers() -> list[dict]:
            layers: list[dict] = []
            defaults = [
                ("welcome_text", "[user]", 200, 80, 40),
                ("welcome_text2", "", 200, 140, 32),
                ("welcome_text3", "", 200, 200, 28),
            ]
            for prefix, default_text, default_x, default_y, default_size in defaults:
                template = str(settings.get(f"{prefix}_template") or default_text)
                if not template.strip():
                    continue
                layers.append(
                    {
                        "template": template,
                        "x": int(settings.get(f"{prefix}_x") or default_x),
                        "y": int(settings.get(f"{prefix}_y") or default_y),
                        "font_size": int(settings.get(f"{prefix}_font_size") or default_size),
                        "color": str(settings.get(f"{prefix}_color") or "#ffffff"),
                        "align": str(settings.get(f"{prefix}_align") or "left"),
                        "font_name": str(settings.get(f"{prefix}_font_name") or "default"),
                        "box_width": max(40, int(settings.get(f"{prefix}_box_width") or 500)),
                    }
                )
            return layers

        def _work():
            res = requests.get(bg_url, timeout=20)
            res.raise_for_status()
            bg = Image.open(io.BytesIO(res.content)).convert("RGBA")
            canvas = bg.copy()

            avatar_res = requests.get(member.display_avatar.replace(size=256).url, timeout=20)
            avatar_res.raise_for_status()
            avatar = Image.open(io.BytesIO(avatar_res.content)).convert("RGBA")
            aw = max(16, int(settings.get("welcome_avatar_w") or 128))
            ah = max(16, int(settings.get("welcome_avatar_h") or 128))
            avatar = avatar.resize((aw, ah))
            shape = str(settings.get("welcome_avatar_shape") or "circle").lower()
            if shape == "circle":
                mask = Image.new("L", (aw, ah), 0)
                d = ImageDraw.Draw(mask)
                d.ellipse((0, 0, aw, ah), fill=255)
                avatar.putalpha(mask)
            canvas.alpha_composite(
                avatar,
                (
                    int(settings.get("welcome_avatar_x") or 40),
                    int(settings.get("welcome_avatar_y") or 40),
                ),
            )

            draw = ImageDraw.Draw(canvas)
            for layer in _iter_text_layers():
                rendered = _replace_vars(
                    str(layer["template"]),
                    member=member,
                    guild=guild,
                    inviter=inviter,
                    mode="image",
                )
                font = _safe_font(str(layer["font_name"]), int(layer["font_size"]))
                lines = _wrap_text_lines(draw, rendered, font, int(layer["box_width"]))
                line_gap = int(layer["font_size"]) + 8
                for i, line in enumerate(lines):
                    bbox = draw.textbbox((0, 0), line, font=font)
                    line_w = bbox[2] - bbox[0]
                    tx = int(layer["x"])
                    if str(layer["align"]).lower() == "center":
                        tx = int(layer["x"]) + (int(layer["box_width"]) - line_w) // 2
                    elif str(layer["align"]).lower() == "right":
                        tx = int(layer["x"]) + max(0, int(layer["box_width"]) - line_w)
                    draw.text(
                        (tx, int(layer["y"]) + i * line_gap),
                        line,
                        font=font,
                        fill=str(layer["color"]),
                    )

            bio = io.BytesIO()
            canvas.save(bio, format="PNG")
            return bio.getvalue()

        try:
            return await asyncio.to_thread(_work)
        except Exception:
            log.exception("welcome image build failed")
            return None

    async def _send_welcome_or_goodbye(
        self,
        *,
        member: discord.Member,
        kind: str,
        leave_reason: str | None = None,
    ):
        guild = member.guild
        settings = await get_guild_settings(self.bot.db_pool, guild.id)
        enabled = bool(settings.get("welcome_enabled" if kind == "welcome" else "goodbye_enabled", False))
        if not enabled:
            return

        channel_id = int(settings.get("welcome_channel_id" if kind == "welcome" else "goodbye_channel_id") or 0)
        if channel_id <= 0:
            return

        channel = await self._resolve_text_channel(guild, channel_id)
        if channel is None:
            log.warning("%s channel not found guild=%s channel_id=%s", kind, guild.id, channel_id)
            return

        inviter = await self._detect_used_inviter(guild) if kind == "welcome" else None
        template = str(
            settings.get("welcome_message_template" if kind == "welcome" else "goodbye_message_template") or ""
        )
        content = _replace_vars(
            template,
            member=member,
            guild=guild,
            inviter=inviter,
            mode="message",
            leave_reason=leave_reason,
        )

        file = None
        if kind == "welcome" and bool(settings.get("welcome_image_enabled", False)):
            raw = await self._build_welcome_image_bytes(member, guild, settings, inviter)
            if raw:
                file = discord.File(io.BytesIO(raw), filename="welcome.png")
            else:
                log.warning(
                    "welcome image enabled but image build returned empty guild=%s member=%s",
                    guild.id,
                    member.id,
                )

        try:
            if file:
                await channel.send(content=content, file=file)
            else:
                await channel.send(content=content)
            log.info(
                "%s message sent guild=%s channel=%s member=%s file=%s",
                kind,
                guild.id,
                getattr(channel, "id", 0),
                member.id,
                bool(file),
            )
        except Exception:
            log.exception("%s message send failed", kind)

    async def _refresh_leaderboard_for_guild(self, guild: discord.Guild):
        settings = await get_guild_settings(self.bot.db_pool, guild.id)
        cid = int(settings.get("leaderboard_channel_id") or 0)
        if cid <= 0:
            return
        channel = await self._resolve_text_channel(guild, cid)
        if channel is None:
            return
        rows = await top_users_current_members(self.bot.db_pool, guild.id, limit=10, offset=0)
        if not rows:
            return

        lines = []
        for idx, r in enumerate(rows, start=1):
            uid = int(r.get("user_id") or 0)
            member = guild.get_member(uid)
            name = member.display_name if member else str(uid)
            xp = int(r.get("xp", 0) or 0)
            level = int(xp // 100)
            lines.append(f"{idx}. **{name}** — Lv.{level} ({xp}XP)")

        embed = discord.Embed(title="🏆 서버 랭킹", description="\n".join(lines))
        mid = int(settings.get("leaderboard_message_id") or 0)
        msg = None
        if mid > 0:
            try:
                msg = await channel.fetch_message(mid)
            except Exception:
                msg = None
        try:
            if msg is None:
                msg = await channel.send(embed=embed)
                await update_guild_settings(self.bot.db_pool, guild.id, leaderboard_message_id=int(msg.id))
            else:
                await msg.edit(embed=embed)
        except Exception:
            log.exception("leaderboard refresh failed for guild=%s", guild.id)

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            try:
                await self._snapshot_invites(guild)
            except Exception:
                pass

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await self._send_welcome_or_goodbye(member=member, kind="welcome")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        reason = "스스로 나감"
        try:
            me = member.guild.me
            if me and me.guild_permissions.view_audit_log:
                cutoff = datetime.now(tz=timezone.utc) - timedelta(seconds=20)
                async for entry in member.guild.audit_logs(limit=5, action=discord.AuditLogAction.kick):
                    if entry.target and int(entry.target.id) == int(member.id) and entry.created_at >= cutoff:
                        reason = "강퇴"
                        break
        except Exception:
            pass
        await self._send_welcome_or_goodbye(member=member, kind="goodbye", leave_reason=reason)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return

        settings = await get_guild_settings(self.bot.db_pool, message.guild.id)

        invite_channels = set(_as_int_list(settings.get("invite_block_channel_ids")))
        if int(message.channel.id) in invite_channels and INVITE_RE.search(message.content or ""):
            try:
                await message.delete()
                await message.channel.send(
                    f"{message.author.mention} 이 채널에는 서버 초대 링크를 올릴 수 없어요.",
                    delete_after=5,
                )
            except Exception:
                pass
            return

        # '봇/명령어 전용 채널' 기능은 Discord 기본 권한/앱 명령 설정으로 대체 가능하므로 사용하지 않습니다.

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        # AFK 자동 퇴장 기능은 Discord 기본 서버 설정으로 대체 가능하므로 비활성화합니다.
        return


async def setup(bot: commands.Bot):
    await bot.add_cog(CommunityFeaturesCog(bot))
