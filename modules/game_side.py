"""Shared application service for the focused game-side attribute."""

from __future__ import annotations

from dataclasses import dataclass
import logging

import discord

import settings
from modules import (
    exceptions,
    game_metadata_presentation,
    game_workers,
    models,
    utilities,
)


logger = logging.getLogger('polybot.' + __name__)


@dataclass(frozen=True)
class GameSideActor:
    """Safe, event-loop-captured identity for public native output."""

    discord_id: int
    mention: str
    identity: str

    @property
    def label(self) -> str:
        return f'{self.mention} / {self.identity}'


def capture_actor(member) -> GameSideActor:
    """Capture stable identity text before submitting side work."""

    discord_id = int(member.id)
    raw_name = str(
        getattr(member, 'display_name', None)
        or getattr(member, 'name', None)
        or f'user-{discord_id}'
    )
    safe_name = discord.utils.escape_mentions(
        discord.utils.escape_markdown(raw_name),
    )
    mention = getattr(member, 'mention', None)
    if callable(mention):
        mention = mention()
    return GameSideActor(
        discord_id=discord_id,
        mention=str(mention or f'<@{discord_id}>'),
        identity=f'**{safe_name}** (`{discord_id}`)',
    )


def _requester_is_staff(member) -> bool:
    try:
        return bool(settings.is_staff(member))
    except (AttributeError, TypeError, exceptions.CheckFailedError):
        return False


def build_read_request(
    *,
    member,
    guild_id: int,
    channel_id: int,
    game_id: int,
    side_lookup: str,
) -> game_workers.GameSideReadRequest:
    """Capture a read request as primitive values only."""

    return game_workers.GameSideReadRequest(
        game_id=int(game_id),
        guild_id=int(guild_id),
        channel_id=int(channel_id),
        requester_id=int(member.id),
        side_lookup=str(side_lookup),
    )


def build_mutation_request(
    *,
    member,
    guild_id: int,
    channel_id: int,
    game_id: int,
    side_lookup: str,
    side_name: str | None = None,
    role_id: int | None = None,
    role_name: str | None = None,
    role_guild_id: int | None = None,
    clear: bool = False,
    native: bool = True,
    invoked_with: str = 'gameside',
) -> game_workers.GameSideMutationRequest:
    """Capture Discord/member values into an immutable worker request."""

    return game_workers.GameSideMutationRequest(
        game_id=int(game_id),
        guild_id=int(guild_id),
        channel_id=int(channel_id),
        requester_id=int(member.id),
        requester_is_staff=_requester_is_staff(member),
        requester_description=models.GameLog.member_string(member),
        side_lookup=str(side_lookup),
        side_name=(str(side_name) if side_name is not None else None),
        role_id=(int(role_id) if role_id is not None else None),
        role_name=(str(role_name) if role_name is not None else None),
        role_guild_id=(
            int(role_guild_id) if role_guild_id is not None else None
        ),
        clear=bool(clear),
        native=bool(native),
        invoked_with=str(invoked_with),
    )


async def run_side_mutation(
    request: game_workers.GameSideMutationRequest,
    *,
    after_commit=None,
) -> game_workers.GameSideMutationResult:
    """Run one side change under the existing keyed game claim."""

    game_id = int(request.game_id)
    locked = False
    try:
        utilities.lock_game(game_id)
        locked = True
        result = await game_workers.run_game_side_mutation(request)
    finally:
        if locked:
            # The worker wrapper drains a canceled synchronous transaction
            # before returning, so the claim cannot release early.
            utilities.unlock_game(game_id)
    if after_commit is not None:
        await after_commit(result)
    return result


async def run_side_read(
    request: game_workers.GameSideReadRequest,
) -> game_workers.GameSideReadResult:
    """Run the separately bounded current-value read."""

    return await game_workers.run_game_side_read(request)


def _display(value: str | None) -> str:
    if value is None or value == '':
        return 'None'
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value)),
    )


def _role_display(
    role_id: int | None,
    role_name: str | None,
    *,
    guild=None,
) -> str:
    if role_id is None:
        return 'None'
    role = None
    get_role = getattr(guild, 'get_role', None) if guild is not None else None
    if callable(get_role):
        role = get_role(int(role_id))
    mention = getattr(role, 'mention', None) if role is not None else None
    mention = str(mention or f'<@&{int(role_id)}>')
    resolved_name = getattr(role, 'name', None) if role is not None else None
    resolved_name = resolved_name or role_name or f'role {int(role_id)}'
    return (
        f'{mention} (**{_display(str(resolved_name))}**)'
    )


def read_message(
    result: game_workers.GameSideReadResult,
    *,
    actor: GameSideActor | None = None,
    guild=None,
) -> str:
    """Render a public current-side read."""

    message = (
        f'Current configuration for side {result.position} of game '
        f'{result.game_id}:'
        f'\nName: **{_display(result.side_name)}**'
        f'\nRole restriction: '
        f'{_role_display(result.required_role_id, result.required_role_name, guild=guild)}'
    )
    if actor is not None:
        message += f'\nRequested by {actor.label}.'
    return message


def native_mutation_message(
    result: game_workers.GameSideMutationResult,
    *,
    actor: GameSideActor,
    guild=None,
) -> str:
    if result.cleared:
        return (
            f'{actor.label} cleared the name and role restriction for side '
            f'{result.position} of game {result.game_id}.'
        )
    return (
        f'{actor.label} updated side {result.position} of game '
        f'{result.game_id}:\n'
        f'Name: **{_display(result.side_name)}**\n'
        f'Role restriction: '
        f'{_role_display(result.required_role_id, result.required_role_name, guild=guild)}'
    )


def legacy_mutation_message(
    result: game_workers.GameSideMutationResult,
) -> str:
    """Preserve the established `$gameside` success wording."""

    if result.required_role_id is not None:
        side_name = result.side_name or result.required_role_name or 'None'
        return (
            f'Side {result.position} for game {result.game_id} has been '
            f'locked to role **@{side_name}** and named **{side_name}**'
        )
    return (
        f'Side {result.position} for game {result.game_id} has been named '
        f'**{result.side_name}**'
    )


def public_interaction_sender(interaction):
    """Return a public sender that clears one private deferred response."""

    cleared = False

    async def send(content, **kwargs):
        nonlocal cleared
        if not cleared:
            cleared = True
            delete_original = getattr(
                interaction,
                'delete_original_response',
                None,
            )
            if delete_original is not None:
                try:
                    await delete_original()
                except Exception:
                    logger.exception(
                        'Could not clear the private deferred game-side '
                        'response before public output'
                    )
        channel = getattr(interaction, 'channel', None)
        channel_send = getattr(channel, 'send', None)
        if channel_send is None:
            raise RuntimeError('The interaction has no public channel sender.')
        return await channel_send(content, **kwargs)

    return send


async def _send_reconciliation_warning(send, content: str, game_id: int) -> None:
    try:
        await send(content)
    except Exception:
        logger.exception(
            'Committed game-side mutation %s reconciliation warning failed',
            game_id,
        )


async def reconcile_game_presentation(
    result: game_workers.GameSideMutationResult,
    *,
    send,
    destination,
    guild,
    bot,
    prefix: str,
    requester_id: int,
    channel_id: int,
    presentation: str = 'prefix',
    load_card=None,
    refresh_announcement=None,
    send_card=None,
) -> None:
    """Refresh the announcement and dense game card only after commit."""

    load_card = load_card or game_metadata_presentation.load_card
    refresh_announcement = (
        refresh_announcement
        or game_metadata_presentation.refresh_announcement
    )
    send_card = send_card or game_metadata_presentation.send_dense_card

    try:
        card = await load_card(
            game_id=result.game_id,
            guild=guild,
            bot=bot,
            prefix=prefix,
            presentation=presentation,
            requester_id=requester_id,
            channel_id=channel_id,
        )
    except Exception:
        logger.exception(
            'Committed game-side mutation %s could not reload its game '
            'presentation',
            result.game_id,
        )
        await _send_reconciliation_warning(
            send,
            f':warning: Game {result.game_id} side data was saved, but the '
            'post-commit game presentation could not be reloaded.',
            result.game_id,
        )
        return

    try:
        await refresh_announcement(
            card,
            guild=guild,
            channel_id=result.announcement_channel_id,
            message_id=result.announcement_message_id,
        )
    except Exception:
        logger.exception(
            'Committed game-side mutation %s announcement refresh failed',
            result.game_id,
        )
        await _send_reconciliation_warning(
            send,
            f':warning: Game {result.game_id} side data was saved, but the '
            'announcement refresh failed. An operator must reconcile the '
            'announcement.',
            result.game_id,
        )

    try:
        await send_card(destination, card)
    except Exception:
        logger.exception(
            'Committed game-side mutation %s dense game-card refresh failed',
            result.game_id,
        )
        await _send_reconciliation_warning(
            send,
            f':warning: Game {result.game_id} side data was saved, but the '
            'dense game-card refresh failed. An operator must reconcile the '
            'game card.',
            result.game_id,
        )


async def publish_mutation_result(
    result: game_workers.GameSideMutationResult,
    *,
    send,
    destination,
    guild,
    bot,
    prefix: str,
    requester_id: int,
    channel_id: int,
    presentation: str = 'prefix',
    actor: GameSideActor | None = None,
    load_card=None,
    refresh_announcement=None,
    send_card=None,
) -> None:
    """Publish committed output and reconcile the public presentation."""

    if actor is None:
        message = legacy_mutation_message(result)
    else:
        message = native_mutation_message(result, actor=actor, guild=guild)
    try:
        await send(message)
    except Exception:
        logger.exception(
            'Committed game-side mutation %s could not publish success',
            result.game_id,
        )
        await _send_reconciliation_warning(
            send,
            f':warning: Game {result.game_id} side data was saved, but the '
            'public success message could not be sent. An operator must '
            'reconcile the game card and audit trail.',
            result.game_id,
        )

    await reconcile_game_presentation(
        result,
        send=send,
        destination=destination,
        guild=guild,
        bot=bot,
        prefix=prefix,
        requester_id=requester_id,
        channel_id=channel_id,
        presentation=presentation,
        load_card=load_card,
        refresh_announcement=refresh_announcement,
        send_card=send_card,
    )
