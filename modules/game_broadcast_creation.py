"""Discord publication boundary for new external game invitations."""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass

import discord

import settings
from modules import game_broadcast_creation_workers as workers


logger = logging.getLogger('polybot.' + __name__)

TRACKED = 'tracked'
SKIPPED = 'skipped'
COMPENSATED = 'compensated'
ORPHANED = 'orphaned'
UNCERTAIN = 'uncertain'

_active_games: set[int] = set()
_active_games_lock = threading.Lock()


@dataclass(frozen=True)
class ExternalBroadcastCreationRequest:
    game_id: int
    guild_id: int
    jump_url: str
    role_locks: tuple[workers.BroadcastRoleSnapshot, ...]
    channel_name: str


@dataclass(frozen=True)
class ExternalBroadcastCreationOutcome:
    external_server_id: int | None
    channel_id: int | None
    message_id: int | None
    status: str
    detail: str


@dataclass(frozen=True)
class ExternalBroadcastCreationResult:
    game_id: int
    outcomes: tuple[ExternalBroadcastCreationOutcome, ...]
    warnings: tuple[str, ...]


def _claim_game(game_id: int) -> bool:
    with _active_games_lock:
        if game_id in _active_games:
            return False
        _active_games.add(game_id)
        return True


def _release_game(game_id: int) -> None:
    with _active_games_lock:
        _active_games.discard(game_id)


def _target_text(*, game_id, server_id, channel_id=None, message_id=None):
    target = f'game {game_id}, external server {server_id}'
    if channel_id is not None:
        target += f', channel {channel_id}'
    if message_id is not None:
        target += f', message {message_id}'
    return target


def _warning(detail: str) -> str:
    return f':warning: External game broadcast reconciliation: {detail}'


def _bot_member(*, bot, guild):
    return (
        getattr(guild, 'me', None)
        or guild.get_member(getattr(getattr(bot, 'user', None), 'id', 0))
    )


async def _compensate_message(*, message, game_id, server_id, channel_id, reason):
    message_id = int(message.id)
    target = _target_text(
        game_id=game_id,
        server_id=server_id,
        channel_id=channel_id,
        message_id=message_id,
    )
    try:
        await message.delete()
    except discord.NotFound:
        return ExternalBroadcastCreationOutcome(
            external_server_id=server_id,
            channel_id=channel_id,
            message_id=message_id,
            status=COMPENSATED,
            detail=f'{target}: {reason}; the untracked message was already absent',
        )
    except Exception as exc:
        logger.exception(
            'Could not delete untracked external invitation for game %s '
            'target %s/%s',
            game_id,
            channel_id,
            message_id,
        )
        return ExternalBroadcastCreationOutcome(
            external_server_id=server_id,
            channel_id=channel_id,
            message_id=message_id,
            status=ORPHANED,
            detail=(
                f'{target}: {reason}; untracked message deletion was '
                f'uncertain: {exc}'
            ),
        )
    return ExternalBroadcastCreationOutcome(
        external_server_id=server_id,
        channel_id=channel_id,
        message_id=message_id,
        status=COMPENSATED,
        detail=f'{target}: {reason}; the untracked message was deleted',
    )


async def _persist_or_compensate(*, message, request, server_id, channel_id):
    persisted_request = workers.BroadcastTargetRequest(
        game_id=request.game_id,
        guild_id=request.guild_id,
        channel_id=channel_id,
        message_id=int(message.id),
    )
    try:
        persisted = await workers.run_persist_broadcast_target(
            persisted_request
        )
    except Exception as exc:
        logger.exception(
            'External invitation persistence failed for game %s target %s/%s',
            request.game_id,
            channel_id,
            message.id,
        )
        return await _compensate_message(
            message=message,
            game_id=request.game_id,
            server_id=server_id,
            channel_id=channel_id,
            reason=f'tracking-row persistence failed: {exc}',
        )
    if persisted.status != workers.TRACKED:
        return await _compensate_message(
            message=message,
            game_id=request.game_id,
            server_id=server_id,
            channel_id=channel_id,
            reason=(
                f'tracking-row persistence returned {persisted.status}'
            ),
        )
    return ExternalBroadcastCreationOutcome(
        external_server_id=server_id,
        channel_id=channel_id,
        message_id=int(message.id),
        status=TRACKED,
        detail='external invitation sent and tracked',
    )


async def _drain_concrete_message_finalization(operation):
    """Do not abandon persistence/compensation after a concrete send."""

    task = asyncio.create_task(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        current = asyncio.current_task()
        while not task.done():
            if current is not None:
                while current.cancelling():
                    current.uncancel()
            try:
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                continue
        try:
            task.result()
        except BaseException:
            logger.exception(
                'Cancelled external invitation finalization completed with '
                'an error'
            )
        raise asyncio.CancelledError


async def _publish_destination(*, bot, request, plan):
    server_id = int(plan.external_server_id)
    guild = bot.get_guild(server_id)
    if guild is None:
        return ExternalBroadcastCreationOutcome(
            external_server_id=server_id,
            channel_id=None,
            message_id=None,
            status=SKIPPED,
            detail=(
                f'{_target_text(game_id=request.game_id, server_id=server_id)} '
                'is not available in the bot guild cache'
            ),
        )
    channel = discord.utils.get(
        getattr(guild, 'text_channels', ()),
        name=request.channel_name,
    )
    if channel is None:
        return ExternalBroadcastCreationOutcome(
            external_server_id=server_id,
            channel_id=None,
            message_id=None,
            status=SKIPPED,
            detail=(
                f'{_target_text(game_id=request.game_id, server_id=server_id)} '
                f'has no #{request.channel_name} channel'
            ),
        )

    channel_id = int(channel.id)
    target_request = workers.BroadcastTargetRequest(
        game_id=request.game_id,
        guild_id=request.guild_id,
        channel_id=channel_id,
    )
    try:
        preflight = await workers.run_preflight_broadcast_target(
            target_request
        )
    except Exception as exc:
        logger.exception(
            'External invitation preflight failed for game %s channel %s',
            request.game_id,
            channel_id,
        )
        return ExternalBroadcastCreationOutcome(
            external_server_id=server_id,
            channel_id=channel_id,
            message_id=None,
            status=SKIPPED,
            detail=(
                f'{_target_text(game_id=request.game_id, server_id=server_id, channel_id=channel_id)} '
                f'failed database preflight: {exc}'
            ),
        )
    if preflight.status != workers.READY:
        return ExternalBroadcastCreationOutcome(
            external_server_id=server_id,
            channel_id=channel_id,
            message_id=preflight.message_id,
            status=SKIPPED,
            detail=(
                f'{_target_text(game_id=request.game_id, server_id=server_id, channel_id=channel_id)} '
                f'was not sent because preflight returned {preflight.status}'
            ),
        )

    try:
        member = _bot_member(bot=bot, guild=guild)
        add_reactions = bool(
            member is not None
            and channel.permissions_for(member).add_reactions
        )
    except Exception as exc:
        logger.exception(
            'Could not inspect external invitation permissions for game %s '
            'channel %s',
            request.game_id,
            channel_id,
        )
        return ExternalBroadcastCreationOutcome(
            external_server_id=server_id,
            channel_id=channel_id,
            message_id=None,
            status=SKIPPED,
            detail=(
                f'{_target_text(game_id=request.game_id, server_id=server_id, channel_id=channel_id)} '
                f'permission inspection failed: {exc}'
            ),
        )
    content = (
        plan.content_with_join if add_reactions else plan.content_without_join
    )
    try:
        message = await channel.send(content)
    except Exception as exc:
        logger.exception(
            'External invitation send failed or was ambiguous for game %s '
            'channel %s',
            request.game_id,
            channel_id,
        )
        return ExternalBroadcastCreationOutcome(
            external_server_id=server_id,
            channel_id=channel_id,
            message_id=None,
            status=UNCERTAIN,
            detail=(
                f'{_target_text(game_id=request.game_id, server_id=server_id, channel_id=channel_id)} '
                f'had an uncertain Discord send result and was not retried: {exc}'
            ),
        )

    return await _drain_concrete_message_finalization(
        _persist_or_compensate(
            message=message,
            request=request,
            server_id=server_id,
            channel_id=channel_id,
        )
    )


async def create_external_broadcasts(*, bot, request):
    """Create and track every planned destination independently."""

    if not _claim_game(request.game_id):
        detail = f'game {request.game_id} already has a broadcast attempt active'
        return ExternalBroadcastCreationResult(
            game_id=request.game_id,
            outcomes=(),
            warnings=(_warning(detail),),
        )
    try:
        try:
            plan = await workers.run_build_broadcast_plan(
                workers.BroadcastPlanRequest(
                    game_id=request.game_id,
                    guild_id=request.guild_id,
                    jump_url=request.jump_url,
                    role_locks=request.role_locks,
                )
            )
        except Exception as exc:
            logger.exception(
                'External invitation planning failed for game %s',
                request.game_id,
            )
            return ExternalBroadcastCreationResult(
                game_id=request.game_id,
                outcomes=(),
                warnings=(_warning(
                    f'game {request.game_id} planning failed: {exc}'
                ),),
            )
        if plan.status != workers.READY:
            return ExternalBroadcastCreationResult(
                game_id=request.game_id,
                outcomes=(),
                warnings=(_warning(
                    f'game {request.game_id} was not broadcast because '
                    f'planning returned {plan.status}'
                ),),
            )

        outcomes = []
        for destination in plan.destinations:
            try:
                outcomes.append(await _publish_destination(
                    bot=bot,
                    request=request,
                    plan=destination,
                ))
            except Exception as exc:
                logger.exception(
                    'Unexpected external invitation failure for game %s '
                    'server %s',
                    request.game_id,
                    destination.external_server_id,
                )
                outcomes.append(ExternalBroadcastCreationOutcome(
                    external_server_id=destination.external_server_id,
                    channel_id=None,
                    message_id=None,
                    status=UNCERTAIN,
                    detail=f'unexpected destination failure: {exc}',
                ))
        warnings = list(_warning(item) for item in plan.warnings)
        warnings.extend(
            _warning(outcome.detail)
            for outcome in outcomes
            if outcome.status != TRACKED
        )
        return ExternalBroadcastCreationResult(
            game_id=request.game_id,
            outcomes=tuple(outcomes),
            warnings=tuple(warnings),
        )
    finally:
        _release_game(request.game_id)


def channel_name_for_bot(bot) -> str:
    if int(getattr(getattr(bot, 'user', None), 'id', 0)) == settings.bot_id_beta:
        return 'beta-bot-tests'
    return 'polychamps-game-announcements'
