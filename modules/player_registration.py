"""Event-loop service and presentation helpers for player registration."""

from __future__ import annotations

import logging

import discord

from modules import interaction_lifecycle
from modules import player_registration_workers as workers
import settings


logger = logging.getLogger('polybot.' + __name__)


def _is_staff(member) -> bool:
    try:
        return bool(settings.is_staff(member))
    except Exception:
        return False


def capture_member_snapshot(member) -> workers.MemberSnapshot:
    """Capture only primitive Discord values before a worker is submitted."""

    discord_id = int(member.id)
    discord_name = str(
        getattr(member, 'name', None)
        or getattr(member, 'display_name', None)
        or f'user-{discord_id}'
    )
    discord_nick = getattr(member, 'nick', None)
    discord_nick = str(discord_nick) if discord_nick is not None else None
    display_name = str(
        getattr(member, 'display_name', None)
        or discord_nick
        or discord_name
    )
    role_names = tuple(
        sorted(
            str(role.name)
            for role in (getattr(member, 'roles', None) or ())
            if getattr(role, 'name', None) is not None
        )
    )
    role_ids = tuple(
        sorted(
            int(role.id)
            for role in (getattr(member, 'roles', None) or ())
            if getattr(role, 'id', None) is not None
        )
    )
    return workers.MemberSnapshot(
        discord_id=discord_id,
        discord_name=discord_name,
        discord_nick=discord_nick,
        display_name=display_name,
        role_names=role_names,
        role_ids=role_ids,
    )


def build_request(
    *,
    actor,
    guild_id: int,
    canonical_name: str,
    target=None,
    target_snapshot: workers.MemberSnapshot | None = None,
    invoked_with: str = 'register',
) -> workers.PlayerRegistrationRequest:
    """Validate permission and return a primitive-only write request."""

    actor_snapshot = capture_member_snapshot(actor)
    if target is not None:
        target_snapshot = capture_member_snapshot(target)
    if target_snapshot is None:
        target_snapshot = actor_snapshot

    requester_is_staff = _is_staff(actor)
    if (
        target_snapshot.discord_id != actor_snapshot.discord_id
        and not requester_is_staff
    ):
        raise workers.PlayerRegistrationPermissionError(
            'Only server staff can register another member.'
        )

    return workers.PlayerRegistrationRequest(
        guild_id=int(guild_id),
        requester_id=actor_snapshot.discord_id,
        actor=actor_snapshot,
        target=target_snapshot,
        canonical_name=workers.validate_canonical_name(canonical_name),
        requester_is_staff=requester_is_staff,
        invoked_with=str(invoked_with),
    )


def safe_public_name(value: str) -> str:
    return discord.utils.escape_markdown(
        workers.safe_public_name(value),
        as_needed=True,
    )


def success_message(
    request: workers.PlayerRegistrationRequest,
    result: workers.PlayerRegistrationResult,
) -> str:
    """Build public, actor-attributed post-commit output."""

    actor_label = (
        f'<@{request.requester_id}> / {request.actor.description}'
    )
    target_label = (
        f'<@{result.target_id}> / {request.target.description}'
    )
    action = 'registered' if result.player_created else 'updated'
    if result.target_id == request.requester_id:
        subject = 'their'
    else:
        subject = f'{target_label}’s'
    message = (
        f'{actor_label} {action} {subject} account-wide Polytopia name '
        f'to **{safe_public_name(result.canonical_name)}**. '
        'This canonical name applies across all Discord servers.'
    )
    if result.warnings:
        message += '\n:warning: ' + ' '.join(result.warnings)
    return message


def deprecation_message(command_name: str, prefix: str = '$') -> str:
    """Explain the narrow legacy boundary without changing old data."""

    return (
        f'`{prefix}{command_name}` is deprecated and no longer writes '
        'Steam usernames or legacy player codes. Existing legacy values are '
        'preserved. Use `'
        f'{prefix}setname YOUR POLYTOPIA NAME` or `/player register` for the '
        'account-wide canonical Polytopia name.'
    )


public_interaction_sender = interaction_lifecycle.public_interaction_sender
