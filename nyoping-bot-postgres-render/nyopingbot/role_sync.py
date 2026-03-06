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
    """Ensure the member's roles match expected, but only for managed roles."""
    # Current role ids
    cur_ids = {r.id for r in member.roles}

    # Roles to remove: any managed role currently on member but not expected
    to_remove = [r for r in member.roles if r.id in managed and r.id not in expected]

    # Roles to add: expected roles missing on member
    to_add: list[discord.Role] = []
    for rid in expected:
        if rid in cur_ids:
            continue
        role = member.guild.get_role(int(rid))
        if role:
            to_add.append(role)

    try:
        if to_remove:
            await member.remove_roles(*to_remove, reason=reason)
        if to_add:
            await member.add_roles(*to_add, reason=reason)
    except discord.Forbidden:
        # Bot role hierarchy / permissions issue
        return
    except discord.HTTPException:
        return
