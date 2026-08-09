"""Worker-owned persistence for external open-game broadcast reconciliation."""

from __future__ import annotations

import asyncio
import functools
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from modules import models


MAX_STARTED_BROADCASTS_PER_GUILD = 100
MAX_STARTED_BROADCASTS_PER_GAME = 100

READY = 'ready'
GONE = 'gone'
STALE = 'stale'

FINALIZED = 'finalized'
ALREADY_ABSENT = 'already_absent'


@dataclass(frozen=True)
class ExternalBroadcastTarget:
    row_id: int
    game_id: int
    guild_id: int
    channel_id: int
    message_id: int


@dataclass(frozen=True)
class BroadcastDiscoveryRequest:
    guild_id: int
    limit: int = MAX_STARTED_BROADCASTS_PER_GUILD


@dataclass(frozen=True)
class BroadcastDiscoveryResult:
    guild_id: int
    targets: tuple[ExternalBroadcastTarget, ...]
    truncated: bool


@dataclass(frozen=True)
class BroadcastTargetState:
    status: str
    target: ExternalBroadcastTarget | None


@dataclass(frozen=True)
class BroadcastFinalizationResult:
    status: str
    target: ExternalBroadcastTarget


_broadcast_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix='polybot-started-broadcast',
)

_MISMATCH = object()
logger = logging.getLogger('polybot.' + __name__)


def _freeze_row(row) -> ExternalBroadcastTarget:
    game = row.game
    return ExternalBroadcastTarget(
        row_id=int(row.id),
        game_id=int(game.id),
        guild_id=int(game.guild_id),
        channel_id=int(row.channel_id),
        message_id=int(row.message_id),
    )


def freeze_game_broadcast_targets(game) -> tuple[ExternalBroadcastTarget, ...]:
    """Freeze exact tracking rows while the start worker owns the game."""

    broadcasts = game.broadcasts
    if hasattr(broadcasts, 'order_by'):
        broadcasts = broadcasts.order_by(
            models.TeamServerBroadcastMessage.id.asc()
        ).limit(MAX_STARTED_BROADCASTS_PER_GAME + 1)
    rows = sorted(tuple(broadcasts), key=lambda row: int(row.id))
    if len(rows) > MAX_STARTED_BROADCASTS_PER_GAME:
        logger.warning(
            'Game %s has more than %s external broadcast rows; remaining '
            'rows are deferred to the hourly reconciliation cycle.',
            game.id,
            MAX_STARTED_BROADCASTS_PER_GAME,
        )
    return tuple(
        _freeze_row(row)
        for row in rows[:MAX_STARTED_BROADCASTS_PER_GAME]
    )


def discover_started_broadcasts(
    request: BroadcastDiscoveryRequest,
) -> BroadcastDiscoveryResult:
    """Return a bounded deterministic set of retained started-game rows."""

    if request.limit < 1 or request.limit > MAX_STARTED_BROADCASTS_PER_GUILD:
        raise ValueError(
            'Started-broadcast discovery limit must be between 1 and '
            f'{MAX_STARTED_BROADCASTS_PER_GUILD}.'
        )
    with models.db.connection_context():
        rows = tuple(
            models.TeamServerBroadcastMessage
            .select()
            .join(models.Game)
            .where(
                (models.Game.guild_id == request.guild_id)
                & (models.Game.is_pending == False)
            )
            .order_by(
                models.TeamServerBroadcastMessage.message_ts.asc(),
                models.TeamServerBroadcastMessage.id.asc(),
            )
            .limit(request.limit + 1)
        )
        return BroadcastDiscoveryResult(
            guild_id=int(request.guild_id),
            targets=tuple(_freeze_row(row) for row in rows[:request.limit]),
            truncated=len(rows) > request.limit,
        )


def _matching_row(target: ExternalBroadcastTarget):
    row = models.TeamServerBroadcastMessage.get_or_none(id=target.row_id)
    if row is None:
        return None
    game = row.game
    if (
        int(game.id) != target.game_id
        or int(game.guild_id) != target.guild_id
        or int(row.channel_id) != target.channel_id
        or int(row.message_id) != target.message_id
    ):
        return _MISMATCH
    return row


def prepare_started_broadcast(
    target: ExternalBroadcastTarget,
) -> BroadcastTargetState:
    """Authoritatively revalidate one target before a Discord effect."""

    with models.db.connection_context():
        row = _matching_row(target)
        if row is None:
            return BroadcastTargetState(status=GONE, target=None)
        if row is _MISMATCH or bool(row.game.is_pending):
            return BroadcastTargetState(status=STALE, target=None)
        return BroadcastTargetState(status=READY, target=_freeze_row(row))


def finalize_started_broadcast(
    target: ExternalBroadcastTarget,
) -> BroadcastFinalizationResult:
    """Delete only the exact row whose Discord state is now terminal."""

    with models.db.connection_context():
        with models.db.atomic():
            row = _matching_row(target)
            if row is None:
                return BroadcastFinalizationResult(
                    status=ALREADY_ABSENT,
                    target=target,
                )
            if row is _MISMATCH or bool(row.game.is_pending):
                return BroadcastFinalizationResult(status=STALE, target=target)
            row.delete_instance()
            return BroadcastFinalizationResult(status=FINALIZED, target=target)


async def _run_worker(worker, *args):
    call = functools.partial(worker, *args)
    future = _broadcast_executor.submit(call)
    cancellation = None
    while not future.done():
        try:
            await asyncio.sleep(0.001)
        except asyncio.CancelledError as exc:
            cancellation = exc
    if cancellation is not None:
        raise cancellation
    return future.result()


async def run_discover_started_broadcasts(request):
    return await _run_worker(discover_started_broadcasts, request)


async def run_prepare_started_broadcast(target):
    return await _run_worker(prepare_started_broadcast, target)


async def run_finalize_started_broadcast(target):
    return await _run_worker(finalize_started_broadcast, target)
