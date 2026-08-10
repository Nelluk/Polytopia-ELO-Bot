"""Application service for completed-game channel cleanup."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import datetime
import logging

from modules import channels, completed_game_channel_purge_workers as workers


logger = logging.getLogger('polybot.' + __name__)


@dataclass(frozen=True)
class CompletedChannelPurgeOutcome:
    planned_games: int
    planned_targets: int
    deleted_targets: int
    reconciled_targets: int
    failed_targets: int
    reconciliation_targets: int
    truncated: bool


@dataclass(frozen=True)
class _TargetOutcome:
    deleted: int = 0
    reconciled: int = 0
    failed: int = 0
    reconciliation: int = 0


async def _purge_target(*, plan, target, target_guild):
    try:
        deleted = await channels.delete_game_channel(
            target_guild,
            channel_id=target.channel_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            'Completed-game channel deletion raised for game %s channel %s',
            plan.game_id,
            target.channel_id,
        )
        deleted = False
    if deleted is not True:
        logger.warning(
            'Completed-game channel %s for game %s was not deleted; '
            'the database reference remains retryable',
            target.channel_id,
            plan.game_id,
        )
        return _TargetOutcome(failed=1)

    try:
        result = await workers.run_reconcile_deleted_channel(
            workers.CompletedChannelReconcileRequest(
                game_id=plan.game_id,
                source_guild_id=plan.guild_id,
                target=target,
            )
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            'Completed-game channel %s for game %s was deleted, but its '
            'database reference requires reconciliation',
            target.channel_id,
            plan.game_id,
        )
        return _TargetOutcome(deleted=1, reconciliation=1)
    if result.status == workers.TARGET_CHANGED:
        logger.warning(
            'Completed-game channel %s for game %s was deleted, but the '
            'database target changed before reconciliation',
            target.channel_id,
            plan.game_id,
        )
        return _TargetOutcome(deleted=1, reconciliation=1)
    if result.status not in {
        workers.RECONCILED,
        workers.ALREADY_RECONCILED,
    }:
        logger.error(
            'Completed-game channel %s for game %s returned unknown '
            'reconciliation status %r',
            target.channel_id,
            plan.game_id,
            result.status,
        )
        return _TargetOutcome(deleted=1, reconciliation=1)
    return _TargetOutcome(deleted=1, reconciled=1)


async def _drain_target(task):
    cancellation = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = exc
        except Exception:
            break
    result = task.result()
    if cancellation is not None:
        raise cancellation
    return result


async def purge_completed_game_channels(*, bot, as_of=None):
    """Delete and reconcile one bounded cycle of completed-game channels."""

    as_of = as_of or datetime.datetime.now()
    request = workers.CompletedPurgeDiscoveryRequest(
        guild_ids=tuple(int(guild.id) for guild in bot.guilds),
        as_of=as_of,
    )
    discovery = await workers.run_discover_completed_game_channels(request)
    logger.info(
        'Running completed-game channel purge on %s game(s) and %s target(s)',
        len(discovery.plans),
        sum(len(plan.targets) for plan in discovery.plans),
    )
    if discovery.truncated:
        logger.warning(
            'Completed-game channel discovery reached the %s-game bound; '
            'remaining candidates are deferred',
            workers.MAX_COMPLETED_PURGE_GAMES,
        )

    deleted_targets = 0
    reconciled_targets = 0
    failed_targets = 0
    reconciliation_targets = 0
    for plan in discovery.plans:
        source_guild = bot.get_guild(plan.guild_id)
        if source_guild is None:
            failed_targets += len(plan.targets)
            logger.warning(
                'Completed-game channel purge could not resolve source guild '
                '%s for game %s; %s target(s) remain retryable',
                plan.guild_id,
                plan.game_id,
                len(plan.targets),
            )
            continue

        for target in plan.targets:
            target_guild = (
                source_guild
                if target.guild_id == plan.guild_id
                else bot.get_guild(target.guild_id)
            )
            if target_guild is None:
                failed_targets += 1
                logger.warning(
                    'Completed-game channel purge could not resolve guild '
                    '%s for game %s channel %s; the database reference '
                    'remains retryable',
                    target.guild_id,
                    plan.game_id,
                    target.channel_id,
                )
                continue
            outcome = await _drain_target(asyncio.create_task(_purge_target(
                plan=plan,
                target=target,
                target_guild=target_guild,
            )))
            deleted_targets += outcome.deleted
            reconciled_targets += outcome.reconciled
            failed_targets += outcome.failed
            reconciliation_targets += outcome.reconciliation

    return CompletedChannelPurgeOutcome(
        planned_games=len(discovery.plans),
        planned_targets=sum(len(plan.targets) for plan in discovery.plans),
        deleted_targets=deleted_targets,
        reconciled_targets=reconciled_targets,
        failed_targets=failed_targets,
        reconciliation_targets=reconciliation_targets,
        truncated=discovery.truncated,
    )
