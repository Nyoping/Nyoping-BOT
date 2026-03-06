from __future__ import annotations

from typing import Iterable, Tuple, Set
import discord

def _as_int_list(v) -> list[int]:
    if v is None:
        return []
    if isinstance(v, list):
        return [int(x) for x in v if x is not None]
    # asyncpg may return array as list already; fallback
    try:
        return [int(x) for x in list(v)]
    except Exception:
        return []

def compute_expected_and_managed_roles(rules: list[dict], level: int) -> tuple[set[int], set[int]]:
    """Apply rules cumulatively up to current level.
    - expected: roles the member should have among managed roles
    - managed: all roles that rules can add/remove
    """
    expected: set[int] = set()
    managed: set[int] = set()

    for r in sorted(rules, key=lambda x: int(x.get('level', 0))):
        add_ids = _as_int_list(r.get('add_role_ids') or r.get('add_role_id'))
        rem_ids = _as_int_list(r.get('remove_role_ids') or r.get('remove_role_id'))
        managed.update(add_ids)
        managed.update(rem_ids)
        if int(r.get('level', 0)) <= int(level):
            expected.update(add_ids)
            expected.difference_update(rem_ids)

    return expected, managed


async def sync_member_roles(member: discord.Member, expected: set[int], managed: set[int], *, reason: str) -> None:
    """Ensure the member's roles match expected, but only for managed roles.

    Uses a single member.edit call, but skips roles the bot cannot manage so that
    one high role does not make the whole sync fail.
    """
    try:
        me = member.guild.me
        top_pos = int(getattr(getattr(me, 'top_role', None), 'position', -1) or -1)

        def manageable(role: discord.Role | None) -> bool:
            if role is None:
                return False
            if getattr(role, 'is_default', lambda: False)():
                return False
            return int(getattr(role, 'position', -1) or -1) < top_pos

        # Keep non-managed roles, and also keep managed roles that the bot cannot touch.
        keep_roles = [r for r in member.roles if r.id not in managed or not manageable(r)]

        # Add expected managed roles that the bot can actually grant.
        managed_roles = []
        for rid in expected:
            role = member.guild.get_role(int(rid))
            if manageable(role):
                managed_roles.append(role)

        uniq = {r.id: r for r in (keep_roles + managed_roles)}
        final_roles = sorted(uniq.values(), key=lambda r: r.position)

        await member.edit(roles=final_roles, reason=reason)
    except discord.Forbidden:
        return
    except discord.HTTPException:
        return
