"""Shared application adapters for pending-game roster mutations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging

import discord

import settings
from modules import (
    game_detail_views,
    game_detail_workers,
    game_join_workers,
    game_kick_workers,
)


logger = logging.getLogger('polybot.' + __name__)


POST_COMMIT_GAME_CARD_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class PostCommitGameCard:
    """One immutable, production-style card loaded after a mutation commits."""

    snapshot: game_detail_workers.GameDetailSnapshot
    rendered: game_detail_views.ClassicGameDetailRender


async def load_post_commit_game_card(
    *,
    game_id: int,
    guild,
    bot,
    prefix: str,
    presentation: str,
    requester_id: int,
    channel_id: int = 0,
) -> PostCommitGameCard:
    """Load and render one committed game card without event-loop DB work."""

    request = game_detail_workers.GameDetailRequest(
        guild_id=int(guild.id),
        channel_id=int(channel_id or 0),
        requester_discord_id=int(requester_id),
        game_id=int(game_id),
    )
    snapshot = await asyncio.wait_for(
        game_detail_workers.run_game_detail(request),
        timeout=POST_COMMIT_GAME_CARD_TIMEOUT_SECONDS,
    )
    display = game_detail_views.resolve_display(
        snapshot,
        guild=guild,
        bot=bot,
        prefix=prefix,
        join_emoji=getattr(settings, 'emoji_join_game', ''),
        presentation=presentation,
    )
    return PostCommitGameCard(
        snapshot=snapshot,
        rendered=game_detail_views.render_classic_game_detail(display),
    )


async def send_post_commit_game_card(
    destination,
    card: PostCommitGameCard,
    *,
    content,
):
    """Send one immutable card, opening a fresh attachment for this send."""

    kwargs = {
        'embed': card.rendered.embed,
        'content': content,
    }
    attachment = card.rendered.new_file()
    if attachment is not None:
        kwargs['file'] = attachment
    return await destination.send(**kwargs)


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


def build_kick_request(
    *,
    game_id: int,
    author_member,
    target_member=None,
    target_query: str | None = None,
    prefix: str | None = None,
    invoked_with: str = 'kick',
) -> game_kick_workers.KickRequest:
    """Build a frozen kick request from event-loop-owned Discord values."""

    return game_kick_workers.KickRequest(
        game_id=int(game_id),
        guild_id=author_member.guild.id,
        prefix=(
            prefix
            if prefix is not None
            else settings.guild_setting(
                author_member.guild.id,
                'command_prefix',
            )
        ),
        author=snapshot_member(author_member),
        target=(
            snapshot_member(target_member)
            if target_member is not None
            else None
        ),
        target_query=(str(target_query) if target_query is not None else None),
        invoked_with=invoked_with,
    )


async def join(request: game_join_workers.JoinRequest):
    """Shared join application service used by every invocation adapter."""

    return await game_join_workers.run_join(request)


async def leave(request: game_join_workers.LeaveRequest):
    """Shared leave application service used by every invocation adapter."""

    return await game_join_workers.run_leave(request)


async def kick(request: game_kick_workers.KickRequest):
    """Run a kick through the shared pending-game coordinator."""

    return await game_kick_workers.run_kick(request)


async def publish_kick_result(
    result: game_kick_workers.KickResult,
    *,
    send,
    card_destination,
    guild,
    bot,
    channel_id: int,
    prefix: str,
    presentation: str = 'prefix',
) -> None:
    """Publish committed kick effects while retaining later effects on error."""

    card_warning = None
    try:
        card = await load_post_commit_game_card(
            game_id=result.game_id,
            guild=guild,
            bot=bot,
            prefix=prefix,
            presentation=presentation,
            requester_id=result.author_id,
            channel_id=channel_id,
        )
    except Exception:
        logger.exception(
            'Committed kick %s could not reload its game card',
            result.game_id,
        )
        card_warning = (
            f':warning: Game {result.game_id} was changed successfully, but '
            'its game card could not be updated. An operator must reconcile '
            'the announcement.'
        )
    else:
        try:
            await send_post_commit_game_card(
                card_destination,
                card,
                content=card.rendered.content,
            )
        except Exception:
            logger.exception(
                'Committed kick %s game card update failed',
                result.game_id,
            )
            card_warning = (
                f':warning: Game {result.game_id} was changed successfully, '
                'but its game card could not be updated. An operator must '
                'reconcile the announcement.'
            )

    if card_warning:
        await send_post_commit_message(
            send,
            card_warning,
            game_id=result.game_id,
            effect='game card update',
        )

    await send_post_commit_message(
        send,
        result.removal_message,
        game_id=result.game_id,
        effect='kick output',
    )
    if result.expiration_message:
        await send_post_commit_message(
            send,
            result.expiration_message,
            game_id=result.game_id,
            effect='expiration-reset output',
        )


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
