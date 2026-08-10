"""Bounded database workers for completed-game channel cleanup."""

from __future__ import annotations

import asyncio
import datetime
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import logging

from modules import models


MAX_COMPLETED_PURGE_GUILDS = 100
MAX_COMPLETED_PURGE_GAMES = 100
MAX_COMPLETED_PURGE_TARGETS = 1000
SIDE_TARGET = 'side'
GAME_TARGET = 'game'
RECONCILED = 'reconciled'
ALREADY_RECONCILED = 'already_reconciled'
TARGET_CHANGED = 'target_changed'
logger = logging.getLogger('polybot.' + __name__)


class CompletedChannelPurgeWorkerError(RuntimeError):
    """A completed-channel purge request is invalid."""


@dataclass(frozen=True)
class CompletedPurgeDiscoveryRequest:
    guild_ids: tuple[int, ...]
    as_of: datetime.datetime
    limit: int = MAX_COMPLETED_PURGE_GAMES


@dataclass(frozen=True)
class CompletedChannelTarget:
    kind: str
    record_id: int
    guild_id: int
    channel_id: int


@dataclass(frozen=True)
class CompletedGameChannelPlan:
    game_id: int
    guild_id: int
    completed_ts: datetime.datetime
    targets: tuple[CompletedChannelTarget, ...]


@dataclass(frozen=True)
class CompletedPurgeDiscoveryResult:
    plans: tuple[CompletedGameChannelPlan, ...]
    truncated: bool


@dataclass(frozen=True)
class CompletedChannelReconcileRequest:
    game_id: int
    source_guild_id: int
    target: CompletedChannelTarget


@dataclass(frozen=True)
class CompletedChannelReconcileResult:
    game_id: int
    channel_id: int
    status: str


_read_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix='polybot-completed-purge-read',
)
_write_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='polybot-completed-purge-write',
)


def _normalise_discovery_request(request):
    guild_ids = tuple(dict.fromkeys(int(value) for value in request.guild_ids))
    limit = int(request.limit)
    if (
        not guild_ids
        or len(guild_ids) > MAX_COMPLETED_PURGE_GUILDS
        or any(value <= 0 for value in guild_ids)
    ):
        raise CompletedChannelPurgeWorkerError(
            'Completed-channel discovery requires between 1 and '
            f'{MAX_COMPLETED_PURGE_GUILDS} positive guild IDs.'
        )
    if not isinstance(request.as_of, datetime.datetime):
        raise CompletedChannelPurgeWorkerError(
            'Completed-channel discovery requires a datetime boundary.'
        )
    if limit < 1 or limit > MAX_COMPLETED_PURGE_GAMES:
        raise CompletedChannelPurgeWorkerError(
            'Completed-channel discovery limit must be between 1 and '
            f'{MAX_COMPLETED_PURGE_GAMES}.'
        )
    return CompletedPurgeDiscoveryRequest(guild_ids, request.as_of, limit)


def _is_recent_nova_game(row, *, as_of: datetime.datetime) -> bool:
    notes = str(row.get('notes') or '').upper()
    return (
        'NOVA RED' in notes
        and 'NOVA BLUE' in notes
        and row['completed_ts'] > as_of - datetime.timedelta(days=4)
    )


def discover_completed_game_channels(
    request: CompletedPurgeDiscoveryRequest,
) -> CompletedPurgeDiscoveryResult:
    """Freeze a deterministic, bounded set of eligible channel targets."""

    request = _normalise_discovery_request(request)
    before = request.as_of - datetime.timedelta(hours=24)
    after = request.as_of - datetime.timedelta(days=14)
    side_channel_games = (
        models.GameSide
        .select(models.GameSide.game)
        .where(models.GameSide.team_chan.is_null(False))
    )
    with models.db.connection_context():
        rows = tuple(
            models.Game
            .select(
                models.Game.id,
                models.Game.guild_id,
                models.Game.completed_ts,
                models.Game.notes,
                models.Game.game_chan,
            )
            .where(
                (models.Game.guild_id.in_(request.guild_ids))
                & (models.Game.is_confirmed == 1)
                & (models.Game.completed_ts < before)
                & (models.Game.completed_ts > after)
                & (
                    models.Game.league_season.is_null(True)
                    | (models.Game.league_season == 0)
                )
                & (
                    models.Game.game_chan.is_null(False)
                    | models.Game.id.in_(side_channel_games)
                )
            )
            .order_by(models.Game.completed_ts, models.Game.id)
            .limit(request.limit + 1)
            .dicts()
        )
        selected = tuple(
            row for row in rows[:request.limit]
            if not _is_recent_nova_game(row, as_of=request.as_of)
        )
        game_ids = tuple(int(row['id']) for row in selected)
        side_rows = ()
        if game_ids:
            side_rows = tuple(
                models.GameSide
                .select(
                    models.GameSide.id,
                    models.GameSide.game,
                    models.GameSide.team_chan,
                    models.GameSide.team_chan_external_server,
                )
                .where(
                    (models.GameSide.game.in_(game_ids))
                    & models.GameSide.team_chan.is_null(False)
                )
                .order_by(models.GameSide.game, models.GameSide.id)
                .limit(MAX_COMPLETED_PURGE_TARGETS + 1)
                .dicts()
            )
            if len(side_rows) > MAX_COMPLETED_PURGE_TARGETS:
                raise CompletedChannelPurgeWorkerError(
                    'Completed-channel discovery exceeded the '
                    f'{MAX_COMPLETED_PURGE_TARGETS}-target bound.'
                )

    sides_by_game: dict[int, list[CompletedChannelTarget]] = {}
    for side in side_rows:
        game_id = int(side['game'])
        source_guild_id = next(
            int(row['guild_id']) for row in selected
            if int(row['id']) == game_id
        )
        sides_by_game.setdefault(game_id, []).append(
            CompletedChannelTarget(
                kind=SIDE_TARGET,
                record_id=int(side['id']),
                guild_id=int(
                    side['team_chan_external_server'] or source_guild_id
                ),
                channel_id=int(side['team_chan']),
            )
        )

    plans = []
    for row in selected:
        game_id = int(row['id'])
        guild_id = int(row['guild_id'])
        targets = list(sides_by_game.get(game_id, ()))
        if row['game_chan'] is not None:
            targets.append(CompletedChannelTarget(
                kind=GAME_TARGET,
                record_id=game_id,
                guild_id=guild_id,
                channel_id=int(row['game_chan']),
            ))
        plans.append(CompletedGameChannelPlan(
            game_id=game_id,
            guild_id=guild_id,
            completed_ts=row['completed_ts'],
            targets=tuple(targets),
        ))
    if sum(len(plan.targets) for plan in plans) > MAX_COMPLETED_PURGE_TARGETS:
        raise CompletedChannelPurgeWorkerError(
            'Completed-channel discovery exceeded the '
            f'{MAX_COMPLETED_PURGE_TARGETS}-target bound.'
        )
    return CompletedPurgeDiscoveryResult(
        plans=tuple(plans),
        truncated=len(rows) > request.limit,
    )


def reconcile_deleted_channel(
    request: CompletedChannelReconcileRequest,
) -> CompletedChannelReconcileResult:
    """Clear one exact channel reference after Discord confirms deletion."""

    game_id = int(request.game_id)
    source_guild_id = int(request.source_guild_id)
    target = request.target
    if (
        game_id <= 0
        or source_guild_id <= 0
        or target.record_id <= 0
        or target.guild_id <= 0
        or target.channel_id <= 0
    ):
        raise CompletedChannelPurgeWorkerError(
            'Completed-channel reconciliation requires positive IDs.'
        )
    if target.kind not in {SIDE_TARGET, GAME_TARGET}:
        raise CompletedChannelPurgeWorkerError(
            'Completed-channel reconciliation target kind is invalid.'
        )
    if target.kind == GAME_TARGET and int(target.record_id) != game_id:
        raise CompletedChannelPurgeWorkerError(
            'A central-channel target must identify its game record.'
        )

    with models.db.connection_context():
        with models.db.atomic():
            if target.kind == SIDE_TARGET:
                row = (
                    models.GameSide
                    .select(models.GameSide, models.Game)
                    .join(models.Game)
                    .where(
                        (models.GameSide.id == target.record_id)
                        & (models.GameSide.game == game_id)
                        & (models.Game.guild_id == source_guild_id)
                    )
                    .for_update()
                    .first()
                )
                field = models.GameSide.team_chan
                current = None if row is None else row.team_chan
            else:
                row = (
                    models.Game
                    .select()
                    .where(
                        (models.Game.id == game_id)
                        & (models.Game.guild_id == source_guild_id)
                    )
                    .for_update()
                    .first()
                )
                field = models.Game.game_chan
                current = None if row is None else row.game_chan

            if row is None or current is None:
                status = ALREADY_RECONCILED
            elif int(current) != int(target.channel_id):
                status = TARGET_CHANGED
            else:
                setattr(row, field.name, None)
                row.save(only=(field,))
                status = RECONCILED
    return CompletedChannelReconcileResult(
        game_id=game_id,
        channel_id=int(target.channel_id),
        status=status,
    )


async def _drain_future(future: Future):
    cancellation = None
    while not future.done():
        try:
            await asyncio.sleep(0.001)
        except asyncio.CancelledError as exc:
            cancellation = exc
    if cancellation is not None:
        try:
            future.result()
        except BaseException:
            logger.exception(
                'Cancelled completed-channel worker finished with an error'
            )
        raise cancellation
    return future.result()


async def run_discover_completed_game_channels(request):
    return await _drain_future(
        _read_executor.submit(discover_completed_game_channels, request)
    )


async def run_reconcile_deleted_channel(request):
    return await _drain_future(
        _write_executor.submit(reconcile_deleted_channel, request)
    )
