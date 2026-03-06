from __future__ import annotations

import discord
from discord import app_commands

# Keys used by locale_str(...) in commands/groups.
KO = {
    # top-level commands
    "cmd_checkin_name": "출석",
    "cmd_checkin_desc": "출석체크 (한국 기준 하루 1회)",
    "cmd_profile_name": "프로필",
    "cmd_profile_desc": "내 레벨/경험치 확인",
    "cmd_leaderboard_name": "랭킹",
    "cmd_leaderboard_desc": "서버 랭킹 TOP 10",
    # group names used by app_commands.Group
    "grp_settings_name": "설정",
    "grp_settings_desc": "관리자 설정",
    "grp_levelrole_name": "레벨역할",
    "grp_levelrole_desc": "레벨 도달 시 역할 부여/제거 규칙",
    "cmd_clean_name": "청소",
    "cmd_clean_desc": "최근 메시지를 지정한 개수만큼 삭제",

    # settings subcommands (keys used in cogs/admin_settings.py)
    "settings_view_name": "보기",
    "settings_view_desc": "현재 설정 보기",
    "settings_set_checkin_xp_name": "출석xp",
    "settings_set_checkin_xp_desc": "출석 체크 경험치 설정",
    "settings_toggle_checkin_limit_name": "출석제한",
    "settings_toggle_checkin_limit_desc": "출석 제한 ON/OFF (테스트용)",
    "settings_set_message_xp_name": "채팅xp",
    "settings_set_message_xp_desc": "채팅 경험치/쿨다운 설정",
    "settings_set_voice_xp_name": "통화xp",
    "settings_set_voice_xp_desc": "통화(음성) 분당 경험치 설정",

    # owner-only settings
    "settings_reset_checkin_name": "출석초기화",
    "settings_reset_checkin_desc": "특정 유저의 오늘 출석 기록을 초기화",
    "settings_set_level_name": "레벨조정",
    "settings_set_level_desc": "특정 유저의 레벨(경험치)을 강제로 설정",

    # reaction lock (block certain roles from reacting on a specific message)
    "settings_reactblock_add_name": "반응차단추가",
    "settings_reactblock_add_desc": "특정 메시지의 반응(이모지)을 특정 역할이 누르지 못하게 설정",
    "settings_reactblock_remove_name": "반응차단삭제",
    "settings_reactblock_remove_desc": "반응 차단 설정 삭제",
    "settings_reactblock_list_name": "반응차단목록",
    "settings_reactblock_list_desc": "현재 반응 차단 목록 보기",

    # levelrole subcommands (keys used in cogs/level_roles.py)
    "levelrole_set_name": "설정",
    "levelrole_set_desc": "특정 레벨에 도달하면 역할 추가/제거",
    "levelrole_list_name": "목록",
    "levelrole_list_desc": "레벨 역할 규칙 목록",
    "levelrole_remove_name": "삭제",
    "levelrole_remove_desc": "레벨 역할 규칙 삭제",
}


class NyopingTranslator(app_commands.Translator):
    async def translate(
        self,
        string: app_commands.locale_str,
        locale: discord.Locale,
        context: app_commands.TranslationContext,
    ) -> str | None:
        """Translate locale_str keys to Korean safely.

        discord.py의 locale_str는 `message`와 `extras`를 제공합니다.
        우리가 locale_str(..., key="...") 형태로 넣은 key는 `extras['key']`에 저장됩니다.
        일부 버전에서는 repr에 key=...가 보이지만 속성 `key`는 없을 수 있으므로
        `extras`를 우선 사용합니다.

        번역이 없으면 None을 반환해서 Discord 기본값(영문)을 사용하게 합니다.
        """
        try:
            if locale != discord.Locale.korean:
                return None

            extras = getattr(string, "extras", None)
            key = None
            if isinstance(extras, dict):
                key = extras.get("key")

            # 혹시 모를 구버전/호환
            if not key:
                key = getattr(string, "key", None) or getattr(string, "_key", None)

            if key and key in KO:
                return KO[key]

            return None
        except Exception:
            return None
