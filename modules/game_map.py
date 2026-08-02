"""Shared application service for the focused game-map attribute."""

from __future__ import annotations

from dataclasses import replace
import logging

import settings
from modules import exceptions, game_workers, models, utilities


logger = logging.getLogger('polybot.' + __name__)


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


def build_mutation_request(
    *,
    member,
    guild_id: int,
    channel_id: int,
    game_id: int | None = None,
    map_type: str | None = None,
    clear: bool = False,
    legacy_tokens: tuple[str, ...] = (),
    allow_related_channel: bool = False,
    invoked_with: str = 'setmap',
) -> game_workers.GameMapMutationRequest:
    """Capture Discord/member values into the immutable worker request."""

    return game_workers.GameMapMutationRequest(
        game_id=(int(game_id) if game_id is not None else None),
        guild_id=int(guild_id),
        channel_id=int(channel_id),
        requester_id=int(member.id),
        requester_level=_requester_level(member),
        requester_description=models.GameLog.member_string(member),
        map_type=(str(map_type) if map_type is not None else None),
        clear=bool(clear),
        legacy_tokens=tuple(str(value) for value in legacy_tokens),
        allow_related_channel=bool(allow_related_channel),
        invoked_with=str(invoked_with),
    )


def build_read_request(
    *,
    member,
    guild_id: int,
    channel_id: int,
    game_id: int,
    allow_related_channel: bool = False,
) -> game_workers.GameMapReadRequest:
    """Capture a native read request without passing Discord objects onward."""

    return game_workers.GameMapReadRequest(
        game_id=int(game_id),
        guild_id=int(guild_id),
        channel_id=int(channel_id),
        requester_id=int(member.id),
        allow_related_channel=bool(allow_related_channel),
    )


async def run_map_mutation(
    request: game_workers.GameMapMutationRequest,
    *,
    after_commit=None,
) -> game_workers.GameMapMutationResult:
    """Run a map change under the existing keyed game claim.

    Prefix channel inference is resolved in a bounded read worker first so
    the claim can be keyed by the actual game ID.  The mutation worker then
    reloads and authoritatively validates that target again.
    """

    if request.game_id is None:
        target = await game_workers.run_prepare_legacy_game_map(request)
        request = replace(
            request,
            game_id=target.game_id,
            map_type=target.map_type,
            clear=target.clear,
            legacy_tokens=(),
        )

    game_id = int(request.game_id)
    locked = False
    try:
        utilities.lock_game(game_id)
        locked = True
        result = await game_workers.run_game_map_mutation(request)
    finally:
        if locked:
            utilities.unlock_game(game_id)
    if after_commit is not None:
        await after_commit(result)
    return result


async def run_map_read(
    request: game_workers.GameMapReadRequest,
) -> game_workers.GameMapReadResult:
    """Run the separately bounded current-value read."""

    return await game_workers.run_game_map_read(request)


def read_message(result: game_workers.GameMapReadResult) -> str:
    value = result.map_type or 'None'
    return f'Current map type for game {result.game_id}: "{value}".'


def mutation_message(result: game_workers.GameMapMutationResult) -> str:
    """Keep the established successful prefix output wording."""

    return f'Map type for game {result.game_id} set to "{result.map_type}".'


async def publish_mutation_result(
    result: game_workers.GameMapMutationResult,
    *,
    send,
    guild,
    prefix: str,
    load_game=None,
) -> None:
    """Publish committed output and refresh the legacy announcement card.

    Database success is never represented as a rollback when a later Discord
    effect fails.  The success message remains public, and a best-effort
    public warning plus an exception log makes reconciliation visible.
    """

    if load_game is None:
        load_game = models.Game.load_full_game

    output = mutation_message(result)
    try:
        await send(output)
    except Exception:
        logger.exception(
            'Committed map mutation %s could not publish its success output',
            result.game_id,
        )
        try:
            await send(
                f':warning: Game {result.game_id} map data was saved, but '
                'the public success message could not be sent. An operator '
                'must reconcile the game card and audit trail.'
            )
        except Exception:
            logger.exception(
                'Committed map mutation %s reconciliation warning failed',
                result.game_id,
            )

    try:
        game = load_game(game_id=result.game_id)
        refreshed = await game.update_announcement(
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
            'Committed map mutation %s announcement refresh failed',
            result.game_id,
        )
        try:
            await send(
                f':warning: Game {result.game_id} map data was saved, but '
                'the announcement/card refresh failed. An operator must '
                'reconcile the game card.'
            )
        except Exception:
            logger.exception(
                'Committed map mutation %s card reconciliation warning '
                'failed',
                result.game_id,
            )
