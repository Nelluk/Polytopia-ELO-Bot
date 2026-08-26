"""Application service for old started incomplete-game cleanup."""

from __future__ import annotations

import datetime
import logging

import settings
from modules import (
    channels,
    game_deletion,
    game_keep_active_views,
    incomplete_game_purge_workers,
)
from modules.elo_jobs import EloJobConflict


logger = logging.getLogger('polybot.' + __name__)


async def _warn_staff(channel, message: str, *, game_id: int | None) -> None:
    logger.warning(
        'Incomplete-game purge reconciliation%s: %s',
        f' for game {game_id}' if game_id else '',
        message,
    )
    if channel is None:
        return
    try:
        await channel.send(f':warning: {message}')
    except Exception:
        logger.exception(
            'Could not publish incomplete-game reconciliation warning%s',
            f' for game {game_id}' if game_id else '',
        )


def _staff_sender(staff_channel):
    async def send(message: str):
        if staff_channel is None:
            logger.warning('No staff channel for purge output: %s', message)
            return
        await staff_channel.send(message)

    return send


async def _resolve_channel(bot, target):
    target_guild = bot.get_guild(target.guild_id)
    if target_guild is None:
        raise LookupError(f'guild {target.guild_id} was not found')
    channel = target_guild.get_channel(target.channel_id)
    if channel is None and hasattr(bot, 'fetch_channel'):
        channel = await bot.fetch_channel(target.channel_id)
    if channel is None:
        raise LookupError(f'channel {target.channel_id} was not found')
    return channel


async def publish_warning_plan(
    plan,
    *,
    bot,
    source_guild_id: int,
    as_of: datetime.date,
    staff_channel,
) -> None:
    """Send and then record each still-unrecorded warning target."""

    for target in plan.targets:
        message = plan.message
        if target.mentions:
            message = f'{message}\n{" ".join(target.mentions)}'
        try:
            channel = await _resolve_channel(bot, target)
            if plan.protected_through is None:
                # Compatibility for an old in-memory plan during rolling
                # upgrades; all model-backed plans carry the deadline.
                await channel.send(message)
            else:
                await channel.send(
                    message,
                    view=game_keep_active_views.KeepActiveView(
                        plan.game_id,
                        plan.protected_through,
                    ),
                )
        except Exception:
            logger.exception(
                'Could not send incomplete-game warning for game %s to %s/%s',
                plan.game_id,
                target.guild_id,
                target.channel_id,
            )
            await _warn_staff(
                staff_channel,
                f'Warning for incomplete game {plan.game_id} could not be '
                f'delivered to `{target.guild_id}/{target.channel_id}`. The '
                'target remains retryable in the next cycle.',
                game_id=plan.game_id,
            )
            continue

        try:
            recorded = await (
                incomplete_game_purge_workers.run_record_warning_delivery(
                    incomplete_game_purge_workers.WarningDeliveryRequest(
                        game_id=plan.game_id,
                        guild_id=source_guild_id,
                        target_guild_id=target.guild_id,
                        channel_id=target.channel_id,
                        as_of=as_of,
                    )
                )
            )
        except Exception:
            logger.exception(
                'Warning delivered but marker failed for game %s channel %s',
                plan.game_id,
                target.channel_id,
            )
            await _warn_staff(
                staff_channel,
                f'Warning for incomplete game {plan.game_id} reached channel '
                f'`{target.channel_id}`, but its delivery marker could not be '
                'committed. The channel may receive the warning again.',
                game_id=plan.game_id,
            )
            continue
        if recorded.status == (
            incomplete_game_purge_workers.SKIPPED_STATE_CHANGED
        ):
            logger.info(
                'Warning marker skipped after state change for game %s '
                'channel %s',
                plan.game_id,
                target.channel_id,
            )


async def _delete_channels(plan, *, bot, guild, staff_channel) -> None:
    for target in plan.channel_targets:
        target_guild = (
            guild
            if target.guild_id == getattr(guild, 'id', None)
            else bot.get_guild(target.guild_id)
        )
        if target_guild is None:
            await _warn_staff(
                staff_channel,
                f'Game {plan.game_id} was purged, but guild '
                f'`{target.guild_id}` for channel `{target.channel_id}` could '
                'not be resolved. Reconcile the channel manually; do not '
                'retry the database purge.',
                game_id=plan.game_id,
            )
            continue
        try:
            deleted = await channels.delete_game_channel(
                target_guild,
                channel_id=target.channel_id,
            )
            if deleted is False:
                raise RuntimeError('channel deletion was not completed')
        except Exception:
            logger.exception(
                'Could not delete channel %s after purging game %s',
                target.channel_id,
                plan.game_id,
            )
            await _warn_staff(
                staff_channel,
                f'Game {plan.game_id} was purged, but channel '
                f'`{target.guild_id}/{target.channel_id}` could not be '
                'deleted. Reconcile it manually; do not retry the database '
                'purge.',
                game_id=plan.game_id,
            )


async def publish_purge_result(result, *, bot, guild, staff_channel) -> None:
    """Apply the frozen announcement/channel effects after commit."""

    plan = result.effect_plan
    if (
        result.status != incomplete_game_purge_workers.PURGED
        or plan is None
    ):
        return
    await game_deletion._publish_announcement(
        plan,
        guild=guild,
        bot=bot,
        prefix=settings.guild_setting(guild.id, 'command_prefix'),
        send=_staff_sender(staff_channel),
    )
    await _delete_channels(
        plan,
        bot=bot,
        guild=guild,
        staff_channel=staff_channel,
    )


async def _publish_summary(staff_channel, summaries: list[str]) -> None:
    if not summaries:
        return
    if staff_channel is None:
        logger.info('Purged %s old incomplete games:\n%s',
                    len(summaries), '\n'.join(summaries))
        return
    header = f'Old incomplete-game cleanup purged {len(summaries)} game(s):\n'
    chunk = header
    for line in summaries:
        candidate = f'{chunk}{line}\n'
        if len(candidate) > 1900 and chunk != header:
            try:
                await staff_channel.send(chunk.rstrip())
            except Exception:
                logger.exception('Could not publish incomplete-purge summary')
                return
            chunk = f'{line}\n'
        else:
            chunk = candidate
    if chunk.strip():
        try:
            await staff_channel.send(chunk.rstrip())
        except Exception:
            logger.exception('Could not publish incomplete-purge summary')


async def purge_incomplete_games_for_guild(
    *,
    bot,
    guild,
    as_of: datetime.date | None = None,
):
    """Warn, purge, and reconcile one guild's old started games."""

    as_of = as_of or datetime.date.today()
    log_channel_id = settings.guild_setting(guild.id, 'log_channel')
    staff_channel = guild.get_channel(log_channel_id) if log_channel_id else None
    try:
        discovered = await (
            incomplete_game_purge_workers.run_discover_incomplete_games(
                incomplete_game_purge_workers.IncompleteGameDiscoveryRequest(
                    guild_id=int(guild.id),
                    as_of=as_of,
                )
            )
        )
    except Exception:
        logger.exception(
            'Incomplete-game discovery failed for guild %s',
            guild.id,
        )
        await _warn_staff(
            staff_channel,
            'Old incomplete-game discovery failed. No candidate was changed '
            'in this cycle.',
            game_id=None,
        )
        return ()

    if discovered.truncated:
        await _warn_staff(
            staff_channel,
            f'Old incomplete-game discovery reached the '
            f'{incomplete_game_purge_workers.MAX_PURGE_CANDIDATES}-game '
            'bound. Remaining candidates are deferred.',
            game_id=None,
        )

    for game_id in discovered.warning_game_ids:
        request = incomplete_game_purge_workers.IncompleteGamePurgeRequest(
            game_id=game_id,
            guild_id=int(guild.id),
            as_of=as_of,
        )
        try:
            plan = await (
                incomplete_game_purge_workers.run_load_warning_plan(request)
            )
        except Exception:
            logger.exception('Warning load failed for game %s', game_id)
            await _warn_staff(
                staff_channel,
                f'Warning state for incomplete game {game_id} could not be '
                'loaded. No warning marker was written.',
                game_id=game_id,
            )
            continue
        if plan is not None:
            await publish_warning_plan(
                plan,
                bot=bot,
                source_guild_id=int(guild.id),
                as_of=as_of,
                staff_channel=staff_channel,
            )

    results = []
    summaries = []
    for game_id in discovered.purge_game_ids:
        request = incomplete_game_purge_workers.IncompleteGamePurgeRequest(
            game_id=game_id,
            guild_id=int(guild.id),
            as_of=as_of,
        )
        try:
            result = await (
                incomplete_game_purge_workers.run_purge_incomplete_game(
                    request
                )
            )
        except EloJobConflict as exc:
            await _warn_staff(
                staff_channel,
                f'Old incomplete-game cleanup deferred game {game_id} and '
                'the remaining candidates because ELO job '
                f'`{exc.active_job.operation}` is active.',
                game_id=game_id,
            )
            break
        except Exception:
            logger.exception(
                'Incomplete-game purge transaction failed for game %s',
                game_id,
            )
            await _warn_staff(
                staff_channel,
                f'Old incomplete game {game_id} could not be purged. Its '
                'transaction rolled back and a later cycle may retry it.',
                game_id=game_id,
            )
            continue
        results.append(result)
        if result.summary:
            summaries.append(result.summary)
        try:
            await publish_purge_result(
                result,
                bot=bot,
                guild=guild,
                staff_channel=staff_channel,
            )
        except Exception:
            logger.exception(
                'Unexpected post-commit reconciliation failure for game %s',
                result.game_id,
            )
            await _warn_staff(
                staff_channel,
                f'Game {result.game_id} was purged, but its post-commit '
                'Discord reconciliation stopped unexpectedly. Reconcile its '
                'announcement/channels manually; do not retry the database '
                'purge.',
                game_id=result.game_id,
            )

    await _publish_summary(staff_channel, summaries)
    return tuple(results)
