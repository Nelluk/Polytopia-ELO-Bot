"""Shared application service for the focused game-name attribute."""

from __future__ import annotations

from dataclasses import dataclass, replace
import logging

import discord

import settings
from modules import exceptions, game_workers, image_storage, models


logger = logging.getLogger('polybot.' + __name__)

GAME_NAME_MAX_LENGTH = 35


@dataclass(frozen=True)
class GameNameActor:
    """Safe, event-loop-captured identity for public native output."""

    discord_id: int
    mention: str
    identity: str

    @property
    def label(self) -> str:
        return f'{self.mention} / {self.identity}'


def capture_actor(member) -> GameNameActor:
    """Capture only stable identity text before submitting name work."""

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
    mention = str(mention or f'<@{discord_id}>')
    return GameNameActor(
        discord_id=discord_id,
        mention=mention,
        identity=f'**{safe_name}** (`{discord_id}`)',
    )


def _requester_level(member) -> int:
    try:
        level = int(settings.get_user_level(member))
    except (AttributeError, TypeError, ValueError, exceptions.CheckFailedError):
        level = 0
    try:
        if settings.is_staff(member):
            level = max(level, 5)
    except (AttributeError, TypeError, exceptions.CheckFailedError):
        pass
    return level


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
    allow_related_channel: bool = False,
) -> game_workers.GameNameReadRequest:
    """Capture a current-name read as primitive values only."""

    return game_workers.GameNameReadRequest(
        game_id=int(game_id),
        guild_id=int(guild_id),
        channel_id=int(channel_id),
        requester_id=int(member.id),
        allow_related_channel=bool(allow_related_channel),
    )


def build_mutation_request(
    *,
    member,
    guild_id: int,
    channel_id: int,
    game_id: int | None = None,
    name: str | None = None,
    clear: bool = False,
    expected_name: str | None = None,
    check_expected_name: bool = False,
    legacy_tokens: tuple[str, ...] = (),
    allow_related_channel: bool = False,
    invoked_with: str = 'rename',
    prefix: str = '$',
) -> game_workers.GameNameMutationRequest:
    """Capture Discord/member values into an immutable worker request."""

    return game_workers.GameNameMutationRequest(
        game_id=(int(game_id) if game_id is not None else None),
        guild_id=int(guild_id),
        channel_id=int(channel_id),
        requester_id=int(member.id),
        requester_level=_requester_level(member),
        requester_is_staff=_requester_is_staff(member),
        requester_description=models.GameLog.member_string(member),
        name=(str(name) if name is not None else None),
        clear=bool(clear),
        expected_name=(
            str(expected_name) if expected_name is not None else None
        ),
        check_expected_name=bool(check_expected_name),
        legacy_tokens=tuple(str(value) for value in legacy_tokens),
        allow_related_channel=bool(allow_related_channel),
        invoked_with=str(invoked_with),
        prefix=str(prefix or '$'),
    )


async def run_name_mutation(
    request: game_workers.GameNameMutationRequest,
    *,
    after_commit=None,
) -> game_workers.GameNameMutationResult:
    """Run a name mutation under the existing keyed game claim."""

    if request.game_id is None:
        target = await game_workers.run_prepare_legacy_game_name(request)
        request = replace(
            request,
            game_id=target.game_id,
            legacy_tokens=(),
            allow_related_channel=target.inferred_from_channel,
        )
    game_id = int(request.game_id)
    locked = False
    try:
        utilities = game_workers.utilities
        utilities.lock_game(game_id)
        locked = True
        result = await game_workers.run_game_name_mutation(request)
    finally:
        if locked:
            # Release immediately after the synchronous transaction, before
            # any public output or Discord reconciliation can await.
            game_workers.utilities.unlock_game(game_id)
    if after_commit is not None:
        await after_commit(result)
    return result


async def run_name_read(
    request: game_workers.GameNameReadRequest,
) -> game_workers.GameNameReadResult:
    """Run the separately bounded current-value read."""

    return await game_workers.run_game_name_read(request)


def _display_name(value: str | None) -> str:
    if value is None or value == '':
        return 'None'
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value)),
    )


def _normalization_detail(result: game_workers.GameNameMutationResult) -> str:
    if not result.normalized:
        return ''
    if result.truncated:
        behavior = (
            f'normalized by the game model and truncated to '
            f'{GAME_NAME_MAX_LENGTH} characters'
        )
    else:
        behavior = 'normalized by the game model (title case and quote rules)'
    return (
        f'\n:information_source: Stored as **{_display_name(result.name)}**; '
        f'the submitted value was {behavior}.'
    )


def read_message(
    result: game_workers.GameNameReadResult,
    *,
    actor: GameNameActor | None = None,
) -> str:
    message = (
        f'Current tracked Polytopia game name for game {result.game_id}: '
        f'**{_display_name(result.name)}**'
    )
    if actor is not None:
        message += f'\nRequested by {actor.label}.'
    if result.is_pending:
        message += '\nThis game has not started yet; its name cannot be edited.'
    return message


def workspace_message(
    game_id: int,
    name: str | None,
    *,
    actor: GameNameActor | None = None,
) -> str:
    message = (
        f'Current tracked Polytopia game name for game {int(game_id)}: '
        f'**{_display_name(name)}**'
    )
    if actor is not None:
        message += f'\nRequested by {actor.label}.'
    return message


def mutation_message(result: game_workers.GameNameMutationResult) -> str:
    """Preserve the established prefix rename success wording."""

    message = (
        f'Game ID {result.game_id} has been renamed to '
        f'"**{_display_name(result.name)}**" from '
        f'"**{_display_name(result.old_name)}**"'
        f'{result.league_warning}'
    )
    return message + _normalization_detail(result)


def native_mutation_message(
    result: game_workers.GameNameMutationResult,
    *,
    actor: GameNameActor,
) -> str:
    if result.cleared or result.name is None:
        message = (
            f'{actor.label} cleared the tracked Polytopia game name for '
            f'game {result.game_id}.'
        )
    else:
        message = (
            f'{actor.label} renamed game {result.game_id} to '
            f'**{_display_name(result.name)}**.'
        )
        message += _normalization_detail(result)
    return message + result.league_warning


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
                        'Could not clear the private deferred game-name '
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
            'Committed game-name mutation %s reconciliation warning failed',
            game_id,
        )


async def refresh_game_card(
    result: game_workers.GameNameMutationResult,
    *,
    destination,
    guild,
    prefix: str,
    load_game=None,
    send_game_embed=None,
) -> None:
    """Send the established dense game card after a committed name write."""

    if load_game is None:
        load_game = models.Game.load_full_game
    if send_game_embed is None:
        send_game_embed = image_storage.send_game_embed
    game = load_game(game_id=result.game_id)
    embed, content = game.embed(guild=guild, prefix=prefix)
    await send_game_embed(
        destination,
        game,
        embed=embed,
        content=content,
    )


async def publish_mutation_result(
    result: game_workers.GameNameMutationResult,
    *,
    send,
    destination,
    guild,
    guild_list=None,
    prefix: str,
    actor: GameNameActor | None = None,
    load_game=None,
    send_game_embed=None,
) -> None:
    """Publish committed output and reconcile all established presentations.

    Every Discord effect is post-commit and independently observable. A later
    failure never turns a committed name/audit transaction into a rollback.
    """

    success_message = (
        native_mutation_message(result, actor=actor)
        if actor is not None
        else mutation_message(result)
    )
    try:
        await send(success_message)
    except Exception:
        logger.exception(
            'Committed game-name mutation %s could not publish success',
            result.game_id,
        )
        await _send_reconciliation_warning(
            send,
            f':warning: Game {result.game_id} name data was saved, but the '
            'public success message could not be sent. An operator must '
            'reconcile the game card and audit trail.',
            result.game_id,
        )

    for warning in (result.name_warning,):
        if not warning:
            continue
        try:
            await send(warning)
        except Exception:
            logger.exception(
                'Committed game-name mutation %s validation warning failed',
                result.game_id,
            )

    if guild_list is None:
        guild_list = (guild,)

    committed_game = None
    if load_game is None:
        load_game = models.Game.load_full_game
    try:
        committed_game = load_game(game_id=result.game_id)
    except Exception:
        logger.exception(
            'Committed game-name mutation %s could not reload its model for '
            'Discord reconciliation',
            result.game_id,
        )
        await _send_reconciliation_warning(
            send,
            f':warning: Game {result.game_id} name data was saved, but the '
            'post-commit game presentation could not be reloaded.',
            result.game_id,
        )
        return

    try:
        update_squad_channels = getattr(
            committed_game,
            'update_squad_channels',
            None,
        )
        if callable(update_squad_channels):
            await update_squad_channels(guild_list, guild.id)
    except Exception:
        logger.exception(
            'Committed game-name mutation %s squad-channel reconciliation failed',
            result.game_id,
        )
        await _send_reconciliation_warning(
            send,
            f':warning: Game {result.game_id} name data was saved, but squad '
            'and game-channel reconciliation failed.',
            result.game_id,
        )

    try:
        update_announcement = getattr(
            committed_game,
            'update_announcement',
            None,
        )
        if callable(update_announcement):
            refreshed = await update_announcement(
                guild=guild,
                prefix=prefix,
            )
            if (
                refreshed is False
                and result.announcement_channel_id is not None
                and result.announcement_message_id is not None
            ):
                raise RuntimeError('the announcement refresh reported failure')
    except Exception:
        logger.exception(
            'Committed game-name mutation %s announcement refresh failed',
            result.game_id,
        )
        await _send_reconciliation_warning(
            send,
            f':warning: Game {result.game_id} name data was saved, but the '
            'announcement refresh failed. An operator must reconcile the '
            'announcement.',
            result.game_id,
        )

    try:
        await refresh_game_card(
            result,
            destination=destination,
            guild=guild,
            prefix=prefix,
            load_game=lambda **_kwargs: committed_game,
            send_game_embed=send_game_embed,
        )
    except Exception:
        logger.exception(
            'Committed game-name mutation %s dense game-card refresh failed',
            result.game_id,
        )
        await _send_reconciliation_warning(
            send,
            f':warning: Game {result.game_id} name data was saved, but the '
            'dense game-card refresh failed. An operator must reconcile the '
            'game card.',
            result.game_id,
        )
