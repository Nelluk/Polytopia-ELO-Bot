"""Application service for the automatic expired open-game purge."""

from __future__ import annotations

import datetime
import logging

import settings
from modules import game_expiration_workers


logger = logging.getLogger('polybot.' + __name__)


async def _warn_staff(channel, message: str, *, game_id: int | None) -> None:
    logger.warning('Expired-game purge reconciliation%s: %s',
                   f' for game {game_id}' if game_id else '', message)
    if channel is None:
        return
    try:
        await channel.send(f':warning: {message}')
    except Exception:
        logger.exception(
            'Could not publish expired-game purge reconciliation warning%s',
            f' for game {game_id}' if game_id else '',
        )


async def _publish_broadcasts(plan, *, bot, staff_channel) -> None:
    for target in plan.broadcast_targets:
        try:
            channel = bot.get_channel(target.channel_id)
            if channel is None and hasattr(bot, 'fetch_channel'):
                channel = await bot.fetch_channel(target.channel_id)
            if channel is None:
                raise LookupError('broadcast channel was not found')
            message = await channel.fetch_message(target.message_id)
            if message is None:
                raise LookupError('broadcast message was not found')
            await message.edit(
                content=(
                    f'~~{message.content}~~\n'
                    '(This game has been deleted and can no longer be joined.)'
                )
            )
            await message.clear_reactions()
        except Exception:
            logger.exception(
                'Could not reconcile expired game %s broadcast %s/%s',
                plan.game_id,
                target.channel_id,
                target.message_id,
            )
            await _warn_staff(
                staff_channel,
                f'Expired game {plan.game_id} was purged, but external '
                f'broadcast `{target.channel_id}/{target.message_id}` could '
                'not be updated. Reconcile it manually; do not recreate or '
                'retry the database purge.',
                game_id=plan.game_id,
            )


async def publish_purge_result(result, *, bot, guild, staff_channel) -> None:
    """Attempt every frozen Discord effect after the database commit."""

    plan = result.effect_plan
    if result.status != game_expiration_workers.PURGED or plan is None:
        return
    await _publish_broadcasts(plan, bot=bot, staff_channel=staff_channel)
    if not plan.public_message:
        return
    channel = (
        bot.get_channel(plan.announcement_channel_id)
        if plan.announcement_channel_id else None
    )
    if channel is None:
        await _warn_staff(
            staff_channel,
            f'Expired game {plan.game_id} was purged, but its configured '
            'game announcement channel could not be resolved. Reconcile the '
            'public state manually; do not retry the database purge.',
            game_id=plan.game_id,
        )
        return
    try:
        await channel.send(plan.public_message)
    except Exception:
        logger.exception(
            'Could not announce committed expired-game purge %s in guild %s',
            plan.game_id,
            getattr(guild, 'id', plan.guild_id),
        )
        await _warn_staff(
            staff_channel,
            f'Expired game {plan.game_id} was purged, but its public purge '
            'notice could not be sent. Reconcile it manually; do not retry '
            'the database purge.',
            game_id=plan.game_id,
        )


async def purge_expired_games_for_guild(
    *,
    bot,
    guild,
    as_of: datetime.datetime | None = None,
):
    """Discover, atomically purge, and publish one guild's expired games."""

    as_of = as_of or datetime.datetime.now()
    announcement_channel_id = settings.guild_setting(
        guild.id,
        'game_announce_channel',
    )
    log_channel_id = settings.guild_setting(guild.id, 'log_channel')
    staff_channel = guild.get_channel(log_channel_id) if log_channel_id else None
    try:
        discovered = await game_expiration_workers.run_discover_expired_game_ids(
            game_expiration_workers.ExpiredGameDiscoveryRequest(
                guild_id=int(guild.id),
                as_of=as_of,
            )
        )
    except Exception:
        logger.exception('Expired-game discovery failed for guild %s', guild.id)
        await _warn_staff(
            staff_channel,
            'Expired-game discovery failed. No candidate was purged in this '
            'cycle; the next scheduled cycle may try discovery again.',
            game_id=None,
        )
        return ()

    if discovered.truncated:
        await _warn_staff(
            staff_channel,
            f'Expired-game discovery reached the '
            f'{game_expiration_workers.MAX_PURGE_CANDIDATES}-game bound. '
            'Remaining candidates are deferred to the next cycle.',
            game_id=None,
        )

    results = []
    for game_id in discovered.game_ids:
        request = game_expiration_workers.ExpiredGamePurgeRequest(
            game_id=int(game_id),
            guild_id=int(guild.id),
            as_of=as_of,
            announcement_channel_id=(
                int(announcement_channel_id)
                if announcement_channel_id else None
            ),
        )
        try:
            result = await game_expiration_workers.run_purge_expired_game(
                request
            )
        except Exception:
            logger.exception(
                'Expired-game purge transaction failed for game %s guild %s',
                game_id,
                guild.id,
            )
            await _warn_staff(
                staff_channel,
                f'Expired game candidate {game_id} could not be purged. Its '
                'database transaction rolled back; a later cycle may retry '
                'after the cause is corrected.',
                game_id=int(game_id),
            )
            continue
        results.append(result)
        await publish_purge_result(
            result,
            bot=bot,
            guild=guild,
            staff_channel=staff_channel,
        )
    return tuple(results)
