"""Model-free ban checks for native Discord interaction adapters."""

from __future__ import annotations

from collections.abc import Iterable


ELO_BANNED_ROLE_NAME = 'ELO Banned'
ELO_BAN_DENIAL_MESSAGE = 'You are banned from using this bot. :kissing_heart:'


def elo_ban_denial(
    member,
    *,
    configured_discord_ids: Iterable[int],
) -> str | None:
    """Return the legacy ban denial for current model-free Discord facts."""

    member_id = int(member.id)
    if member_id in {int(value) for value in configured_discord_ids}:
        return ELO_BAN_DENIAL_MESSAGE
    if any(
        str(getattr(role, 'name', '')) == ELO_BANNED_ROLE_NAME
        for role in (getattr(member, 'roles', ()) or ())
    ):
        return ELO_BAN_DENIAL_MESSAGE
    return None
