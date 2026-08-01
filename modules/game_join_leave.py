"""Shared application adapters for pending-game joins and leaves."""

from __future__ import annotations

import logging

import discord

import settings
from modules import game_join_workers


logger = logging.getLogger('polybot.' + __name__)


def _member_description(member) -> str:
    return (
        f'**{discord.utils.escape_markdown(member.display_name)}** '
        f'(`{member.id}`)'
    )


def snapshot_member(member) -> game_join_workers.MemberSnapshot:
    """Capture only immutable Discord values before worker submission."""

    roles = tuple(getattr(member, 'roles', ()) or ())
    guild_id = member.guild.id
    inactive_role_name = settings.guild_setting(guild_id, 'inactive_role')
    role_names = tuple(role.name for role in roles)
    return game_join_workers.MemberSnapshot(
        guild_id=guild_id,
        discord_id=member.id,
        discord_name=member.name,
        discord_nick=getattr(member, 'nick', None),
        display_name=member.display_name,
        role_ids=tuple(role.id for role in roles),
        role_names=role_names,
        level=settings.get_user_level(member),
        is_mod=settings.is_mod(member),
        is_staff=settings.is_staff(member),
        description=_member_description(member),
        inactive_role_name=inactive_role_name,
        inactive_role_present=bool(
            inactive_role_name and inactive_role_name in role_names
        ),
    )


def build_join_request(
    *,
    game_id: int,
    member,
    author_member=None,
    prefix: str | None = None,
    side_arg=None,
    log_note: str = '',
    invoked_with: str = 'join',
    notification_member_id: int | None = None,
) -> game_join_workers.JoinRequest:
    """Build the shared worker request from event-loop-owned Discord data."""

    author_member = author_member or member
    member_snapshot = snapshot_member(member)
    author_snapshot = snapshot_member(author_member)
    return game_join_workers.JoinRequest(
        game_id=int(game_id),
        guild_id=member.guild.id,
        prefix=(
            prefix
            if prefix is not None
            else settings.guild_setting(member.guild.id, 'command_prefix')
        ),
        member=member_snapshot,
        author=author_snapshot,
        side_arg=(str(side_arg) if side_arg is not None else None),
        log_note=log_note,
        invoked_with=invoked_with,
        notification_member_id=notification_member_id,
    )


def build_leave_request(
    *,
    game_id: int,
    member,
    author_member=None,
    prefix: str | None = None,
    log_note: str = '',
    invoked_with: str = 'leave',
) -> game_join_workers.LeaveRequest:
    """Build the shared leave worker request from primitive snapshots."""

    author_member = author_member or member
    return game_join_workers.LeaveRequest(
        game_id=int(game_id),
        guild_id=member.guild.id,
        prefix=(
            prefix
            if prefix is not None
            else settings.guild_setting(member.guild.id, 'command_prefix')
        ),
        member=snapshot_member(member),
        author=snapshot_member(author_member),
        log_note=log_note,
        invoked_with=invoked_with,
    )


async def join(request: game_join_workers.JoinRequest):
    """Shared join application service used by every invocation adapter."""

    return await game_join_workers.run_join(request)


async def leave(request: game_join_workers.LeaveRequest):
    """Shared leave application service used by every invocation adapter."""

    return await game_join_workers.run_leave(request)


async def send_post_commit_message(
    sender,
    content: str,
    *,
    game_id: int,
    effect: str,
):
    """Send public committed-state text without hiding later reconciliation.

    A database worker has already committed before adapters call this helper.
    A Discord failure therefore must be logged with the committed game ID and
    followed by a best-effort public warning, while allowing the caller to
    continue publishing other post-commit effects.
    """

    try:
        return await sender(content)
    except Exception:
        logger.exception(
            'Committed game %s public %s failed',
            game_id,
            effect,
        )
        warning = (
            f':warning: Game {game_id} was changed successfully, but the '
            f'{effect} could not be published. An operator must reconcile '
            'the public Discord state.'
        )
        try:
            await sender(warning)
        except Exception:
            logger.exception(
                'Committed game %s reconciliation warning failed after %s '
                'send failure',
                game_id,
                effect,
            )
        return None


async def remove_inactive_role_after_commit(result, member):
    """Apply the inactive-role effect only after a committed join."""

    if not result.remove_inactive_role:
        return None
    role_name = result.inactive_role_name
    role = discord.utils.get(member.guild.roles, name=role_name)
    if role is None:
        logger.warning(
            'Committed join for game %s requested inactive-role removal, '
            'but role %r was not found.',
            result.game_id,
            role_name,
        )
        return (
            f':warning: Game {result.game_id} was joined successfully, but '
            f'the inactive role **{role_name}** could not be found for '
            f'<@{result.member_id}>. An operator must reconcile the role.'
        )
    try:
        await member.remove_roles(
            role,
            reason='Player joined a game so should no longer be inactive',
        )
    except Exception:
        logger.exception(
            'Committed join for game %s could not remove inactive role from '
            '%s.',
            result.game_id,
            result.member_id,
        )
        return (
            f':warning: Game {result.game_id} was joined successfully, but '
            f'the inactive role could not be removed from '
            f'<@{result.member_id}>. An operator must reconcile the role.'
        )
    return None
