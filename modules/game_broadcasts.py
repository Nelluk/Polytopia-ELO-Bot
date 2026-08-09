"""Discord boundary for retained external open-game broadcasts."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import discord

import settings
from modules import exceptions, game_broadcast_workers, utilities


logger = logging.getLogger('polybot.' + __name__)

STARTED_MARKER = '(This game has started and can no longer be joined.)'
DELETED_MARKER = '(This game has been deleted and can no longer be joined.)'

RECONCILED = 'reconciled'
RETAINED = 'retained'
DEFERRED = 'deferred'


class _ConfirmedDiscordAbsence(RuntimeError):
    pass


@dataclass(frozen=True)
class BroadcastReconciliationOutcome:
    target: game_broadcast_workers.ExternalBroadcastTarget
    status: str
    detail: str


@dataclass(frozen=True)
class BroadcastReconciliationCycle:
    guild_id: int
    outcomes: tuple[BroadcastReconciliationOutcome, ...]
    truncated: bool


def _started_content(content: str) -> str:
    content = str(content or '')
    if STARTED_MARKER in content:
        return content
    return f'~~{content}~~\n{STARTED_MARKER}'


async def _load_message(*, bot, target):
    channel = bot.get_channel(target.channel_id)
    if channel is None and hasattr(bot, 'fetch_channel'):
        try:
            channel = await bot.fetch_channel(target.channel_id)
        except discord.NotFound as exc:
            raise _ConfirmedDiscordAbsence('channel no longer exists') from exc
    if channel is None:
        raise _ConfirmedDiscordAbsence('channel no longer exists')
    try:
        message = await channel.fetch_message(target.message_id)
    except discord.NotFound as exc:
        raise _ConfirmedDiscordAbsence('message no longer exists') from exc
    if message is None:
        raise _ConfirmedDiscordAbsence('message no longer exists')
    return message


async def _update_started_message(*, bot, target) -> str:
    message = await _load_message(bot=bot, target=target)
    content = str(getattr(message, 'content', '') or '')
    if DELETED_MARKER in content:
        return 'message already records deletion'
    updated = _started_content(content)
    if updated != content:
        try:
            await message.edit(content=updated)
        except discord.NotFound as exc:
            raise _ConfirmedDiscordAbsence(
                'message disappeared during update'
            ) from exc
    member = (
        getattr(getattr(message, 'guild', None), 'me', None)
        or getattr(bot, 'user', None)
    )
    if member is None:
        raise RuntimeError('bot member could not be resolved for reaction removal')
    try:
        await message.remove_reaction(settings.emoji_join_game, member)
    except discord.NotFound:
        # The message or the bot's reaction is already absent. Either state is
        # terminal for an invitation that must no longer be joinable.
        return 'message or join reaction already absent'
    return 'message marked started and join reaction removed'


async def _finalize(target, *, detail: str):
    try:
        result = await game_broadcast_workers.run_finalize_started_broadcast(
            target
        )
    except Exception as exc:
        logger.exception(
            'Could not finalize started-game broadcast row %s for game %s '
            'target %s/%s',
            target.row_id,
            target.game_id,
            target.channel_id,
            target.message_id,
        )
        return BroadcastReconciliationOutcome(
            target=target,
            status=RETAINED,
            detail=f'{detail}; tracking-row finalization failed: {exc}',
        )
    if result.status == game_broadcast_workers.STALE:
        return BroadcastReconciliationOutcome(
            target=target,
            status=RETAINED,
            detail=f'{detail}; tracking row changed before finalization',
        )
    return BroadcastReconciliationOutcome(
        target=target,
        status=RECONCILED,
        detail=detail,
    )


async def reconcile_started_broadcast(*, bot, target):
    """Reconcile one exact target while excluding concurrent game mutation."""

    lock_acquired = False
    try:
        utilities.lock_game(target.game_id)
        lock_acquired = True
    except exceptions.RecordLocked:
        return BroadcastReconciliationOutcome(
            target=target,
            status=DEFERRED,
            detail='game is locked by another operation',
        )

    try:
        try:
            prepared = (
                await game_broadcast_workers.run_prepare_started_broadcast(
                    target
                )
            )
        except Exception as exc:
            logger.exception(
                'Could not prepare started-game broadcast row %s for game %s',
                target.row_id,
                target.game_id,
            )
            return BroadcastReconciliationOutcome(
                target=target,
                status=RETAINED,
                detail=f'database revalidation failed: {exc}',
            )
        if prepared.status == game_broadcast_workers.GONE:
            return BroadcastReconciliationOutcome(
                target=target,
                status=RECONCILED,
                detail='tracking row is already absent',
            )
        if prepared.status == game_broadcast_workers.STALE:
            return BroadcastReconciliationOutcome(
                target=target,
                status=DEFERRED,
                detail='game or tracking target changed before publication',
            )
        target = prepared.target
        try:
            detail = await _update_started_message(bot=bot, target=target)
        except _ConfirmedDiscordAbsence as exc:
            detail = str(exc)
        except Exception as exc:
            logger.exception(
                'Could not reconcile started-game broadcast row %s for game '
                '%s target %s/%s',
                target.row_id,
                target.game_id,
                target.channel_id,
                target.message_id,
            )
            return BroadcastReconciliationOutcome(
                target=target,
                status=RETAINED,
                detail=f'Discord update failed: {exc}',
            )
        return await _finalize(target, detail=detail)
    finally:
        if lock_acquired:
            utilities.unlock_game(target.game_id)


async def reconcile_started_broadcasts(*, bot, targets):
    outcomes = []
    for target in tuple(targets):
        try:
            outcomes.append(
                await reconcile_started_broadcast(bot=bot, target=target)
            )
        except Exception as exc:
            logger.exception(
                'Unexpected started-game broadcast reconciliation failure for '
                'row %s game %s',
                target.row_id,
                target.game_id,
            )
            outcomes.append(BroadcastReconciliationOutcome(
                target=target,
                status=RETAINED,
                detail=f'unexpected reconciliation failure: {exc}',
            ))
    return tuple(outcomes)


def _target_label(outcome) -> str:
    target = outcome.target
    return (
        f'game {target.game_id} row {target.row_id} '
        f'`{target.channel_id}/{target.message_id}`'
    )


async def reconcile_started_broadcasts_for_guild(*, bot, guild):
    """Run one bounded hourly recovery cycle for a guild."""

    try:
        discovered = await (
            game_broadcast_workers.run_discover_started_broadcasts(
                game_broadcast_workers.BroadcastDiscoveryRequest(
                    guild_id=int(guild.id)
                )
            )
        )
    except Exception:
        logger.exception(
            'Started-game broadcast discovery failed for guild %s', guild.id
        )
        return BroadcastReconciliationCycle(
            guild_id=int(guild.id),
            outcomes=(),
            truncated=False,
        )

    outcomes = await reconcile_started_broadcasts(
        bot=bot,
        targets=discovered.targets,
    )
    retained = tuple(
        outcome for outcome in outcomes if outcome.status == RETAINED
    )
    if retained or discovered.truncated:
        labels = '; '.join(_target_label(item) for item in retained[:12])
        extra = len(retained) - min(len(retained), 12)
        detail = labels or 'none in the displayed subset'
        if extra:
            detail += f'; plus {extra} additional retained target(s)'
        if discovered.truncated:
            detail += (
                '; discovery reached the '
                f'{game_broadcast_workers.MAX_STARTED_BROADCASTS_PER_GUILD}'
                '-row bound'
            )
        logger.warning(
            'Started-game broadcast reconciliation remains pending in guild '
            '%s: %s',
            guild.id,
            detail,
        )
        log_channel_id = settings.guild_setting(guild.id, 'log_channel')
        channel = guild.get_channel(log_channel_id) if log_channel_id else None
        if channel is not None:
            try:
                await channel.send(
                    ':warning: Started-game external-broadcast '
                    f'reconciliation remains pending: {detail}.'
                )
            except Exception:
                logger.exception(
                    'Could not publish started-broadcast reconciliation '
                    'summary for guild %s',
                    guild.id,
                )
    return BroadcastReconciliationCycle(
        guild_id=int(guild.id),
        outcomes=outcomes,
        truncated=discovered.truncated,
    )
