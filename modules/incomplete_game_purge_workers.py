"""Bounded workers for warning and purging old started games.

All public functions accept and return frozen primitive values.  Discord I/O
belongs to :mod:`modules.incomplete_game_purge`; Peewee connections and
transactions remain worker-local here.
"""

from __future__ import annotations

import asyncio
import datetime
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import peewee
from peewee import fn

import settings
from modules import game_deletion_workers, models, utilities


MAX_PURGE_CANDIDATES = 500
PURGE_WARNING_DAYS = 3
PURGE_WARNING_MARKER = 'AUTO_PURGE_WARNING_SENT'
PURGED = 'purged'
SKIPPED_STATE_CHANGED = 'skipped_state_changed'
WARNING_RECORDED = 'warning_recorded'
WARNING_ALREADY_RECORDED = 'warning_already_recorded'


@dataclass(frozen=True)
class IncompleteGameDiscoveryRequest:
    guild_id: int
    as_of: datetime.date
    limit: int = MAX_PURGE_CANDIDATES


@dataclass(frozen=True)
class IncompleteGameDiscoveryResult:
    warning_game_ids: tuple[int, ...]
    purge_game_ids: tuple[int, ...]
    truncated: bool


@dataclass(frozen=True)
class _DiscoveredGame:
    is_pending: bool
    is_completed: bool
    is_confirmed: bool
    league_season: int | None
    is_ranked: bool
    date: datetime.date
    cleanup_deferred_until: datetime.date | None = None


@dataclass(frozen=True)
class WarningTarget:
    guild_id: int
    channel_id: int
    mentions: tuple[str, ...]


@dataclass(frozen=True)
class WarningPlan:
    game_id: int
    threshold_days: int
    message: str
    targets: tuple[WarningTarget, ...]
    protected_through: datetime.date | None = None


@dataclass(frozen=True)
class WarningDeliveryRequest:
    game_id: int
    guild_id: int
    target_guild_id: int
    channel_id: int
    as_of: datetime.date
    protected_through: datetime.date | None = None


@dataclass(frozen=True)
class WarningDeliveryResult:
    game_id: int
    channel_id: int
    status: str


@dataclass(frozen=True)
class IncompleteGamePurgeRequest:
    game_id: int
    guild_id: int
    as_of: datetime.date


@dataclass(frozen=True)
class IncompleteGamePurgeResult:
    game_id: int
    status: str
    summary: str | None = None
    effect_plan: game_deletion_workers.DeletionEffectPlan | None = None


_read_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix='polybot-incomplete-purge-read',
)
_warning_write_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='polybot-incomplete-warning-write',
)


def purge_threshold_days(player_count: int, is_ranked: bool) -> int | None:
    """Return the accepted P5.14 age threshold for a started game."""

    if player_count == 2:
        return 60
    if player_count == 3:
        return 90
    if player_count == 4:
        return 120 if is_ranked else 90
    if player_count in (5, 6) and is_ranked:
        return 150
    if player_count >= 5 and not is_ranked:
        return 120
    return None


def base_protected_through(game, *, player_count: int | None = None):
    if player_count is None:
        player_count = len(tuple(getattr(game, 'lineup', ()) or ()))
    threshold = purge_threshold_days(
        int(player_count), bool(getattr(game, 'is_ranked', False))
    )
    game_date = getattr(game, 'date', None)
    if threshold is None or game_date is None:
        return None
    return game_date + datetime.timedelta(days=threshold)


def effective_protected_through(game, *, player_count: int | None = None):
    base = base_protected_through(game, player_count=player_count)
    if base is None:
        return None
    deferred = getattr(game, 'cleanup_deferred_until', None)
    return max(base, deferred) if deferred is not None else base


def _is_started_incomplete(game) -> bool:
    if bool(getattr(game, 'is_pending', False)):
        return False
    if bool(getattr(game, 'is_completed', False)):
        return False
    if bool(getattr(game, 'is_confirmed', False)):
        return False
    try:
        if game.is_season_game():
            return False
    except AttributeError:
        if getattr(game, 'league_season', None):
            return False
    return True


def classify_game(
    game,
    *,
    as_of: datetime.date,
    player_count: int | None = None,
) -> str | None:
    """Classify a reloaded game as a warning or purge candidate."""

    if not _is_started_incomplete(game):
        return None
    if player_count is None:
        player_count = len(tuple(getattr(game, 'lineup', ()) or ()))
    threshold = purge_threshold_days(
        int(player_count),
        bool(getattr(game, 'is_ranked', False)),
    )
    game_date = getattr(game, 'date', None)
    if threshold is None or game_date is None:
        return None
    effective_date = effective_protected_through(
        game, player_count=player_count,
    )
    if effective_date is None:
        return None
    if as_of > effective_date:
        return PURGED
    if as_of >= effective_date - datetime.timedelta(days=PURGE_WARNING_DAYS):
        return 'warning'
    return None


def discovery_deadline_filter(game_model, as_of: datetime.date):
    """Build the bounded SQL age predicate without starving deferred rows."""

    earliest_warning = as_of - datetime.timedelta(days=57)
    latest_deferred_warning = as_of + datetime.timedelta(
        days=PURGE_WARNING_DAYS,
    )
    return (
        (
            game_model.cleanup_deferred_until.is_null(True)
            & (game_model.date <= earliest_warning)
        )
        | (
            game_model.cleanup_deferred_until.is_null(False)
            & (game_model.cleanup_deferred_until <= latest_deferred_warning)
        )
    )


def discover_incomplete_games(
    request: IncompleteGameDiscoveryRequest,
) -> IncompleteGameDiscoveryResult:
    """Return deterministic, bounded warning and purge candidate IDs."""

    limit = int(request.limit)
    if limit < 1 or limit > MAX_PURGE_CANDIDATES:
        raise ValueError(
            f'Incomplete-game discovery limit must be between 1 and '
            f'{MAX_PURGE_CANDIDATES}.'
        )
    player_count = fn.COUNT(models.Lineup.id)
    with models.db.connection_context():
        query = (
            models.Game
            .select(
                models.Game.id,
                models.Game.date,
                models.Game.is_ranked,
                models.Game.cleanup_deferred_until,
                player_count.alias('player_count'),
            )
            .join(
                models.Lineup,
                on=(models.Lineup.game == models.Game.id),
            )
            .where(
                (models.Game.guild_id == request.guild_id)
                & (models.Game.is_pending == 0)
                & (models.Game.is_completed == 0)
                & (models.Game.is_confirmed == 0)
                & (
                    models.Game.league_season.is_null(True)
                    | (models.Game.league_season == 0)
                )
                & discovery_deadline_filter(models.Game, request.as_of)
            )
            .group_by(models.Game.id)
            .having(
                player_count.between(2, 6)
                | (
                    (models.Game.is_ranked == 0)
                    & (player_count >= 7)
                )
            )
            .order_by(models.Game.date, models.Game.id)
            .limit(limit + 1)
        )
        rows = tuple(query.dicts())

    warning_ids = []
    purge_ids = []
    for row in rows[:limit]:
        candidate = _DiscoveredGame(
            is_pending=False,
            is_completed=False,
            is_confirmed=False,
            league_season=None,
            is_ranked=bool(row['is_ranked']),
            date=row['date'],
            cleanup_deferred_until=row.get('cleanup_deferred_until'),
        )
        action = classify_game(
            candidate,
            as_of=request.as_of,
            player_count=int(row['player_count']),
        )
        if action == 'warning':
            warning_ids.append(int(row['id']))
        elif action == PURGED:
            purge_ids.append(int(row['id']))
    return IncompleteGameDiscoveryResult(
        warning_game_ids=tuple(warning_ids),
        purge_game_ids=tuple(purge_ids),
        truncated=len(rows) > limit,
    )


async def _await_executor(executor, function, *args):
    future = executor.submit(function, *args)
    cancellation = None
    while not future.done():
        try:
            await asyncio.sleep(0.001)
        except asyncio.CancelledError as exc:
            cancellation = exc
    if cancellation is not None:
        raise cancellation
    return future.result()


async def run_discover_incomplete_games(request):
    return await _await_executor(
        _read_executor,
        discover_incomplete_games,
        request,
    )


def _load_game(game_id: int, guild_id: int, *, lock: bool = False):
    query = models.Game.select().where(
        (models.Game.id == game_id)
        & (models.Game.guild_id == guild_id)
    )
    if lock:
        query = query.for_update()
    try:
        return query.get()
    except peewee.DoesNotExist:
        return None


def _warning_marker(channel_id: int, protected_through=None) -> str:
    marker = f'{PURGE_WARNING_MARKER} channel_id={int(channel_id)}'
    if protected_through is not None:
        marker += f' deadline={protected_through.isoformat()}'
    return marker


def _warning_was_recorded(
    *,
    game_id: int,
    guild_id: int,
    channel_id: int,
    protected_through=None,
    allow_legacy: bool = True,
) -> bool:
    prefix = f'__{int(game_id)}__ - '
    current_marker = _warning_marker(channel_id, protected_through)
    legacy_marker = _warning_marker(channel_id)
    base = (
        (models.GameLog.guild_id == guild_id)
        & models.GameLog.message.startswith(prefix)
    )
    channel_marker = models.GameLog.message.contains(current_marker + ' ')
    if allow_legacy:
        channel_marker |= (
            models.GameLog.message.contains(legacy_marker + ' ')
            & ~models.GameLog.message.contains(' deadline=')
        )
    return (
        models.GameLog
        .select(models.GameLog.id)
        .where(base & channel_marker)
        .exists()
        or (
            allow_legacy
            and models.GameLog.select(models.GameLog.id).where(
                base
                & models.GameLog.message.contains(PURGE_WARNING_MARKER)
                & ~models.GameLog.message.contains('channel_id=')
            )
            .exists()
        )
    )


def _warning_targets(game) -> tuple[WarningTarget, ...]:
    targets: dict[tuple[int, int], WarningTarget] = {}
    for side in tuple(getattr(game, 'gamesides', ()) or ()):
        channel_id = getattr(side, 'team_chan', None)
        if not channel_id:
            continue
        target_guild_id = int(
            getattr(side, 'team_chan_external_server', None)
            or game.guild_id
        )
        try:
            mentions = tuple(str(value) for value in side.mentions())
        except Exception:
            mentions = ()
        target = WarningTarget(
            guild_id=target_guild_id,
            channel_id=int(channel_id),
            mentions=mentions,
        )
        targets[(target.guild_id, target.channel_id)] = target
    if getattr(game, 'game_chan', None):
        try:
            mentions = tuple(str(value) for value in game.mentions())
        except Exception:
            mentions = ()
        target = WarningTarget(
            guild_id=int(game.guild_id),
            channel_id=int(game.game_chan),
            mentions=mentions,
        )
        targets[(target.guild_id, target.channel_id)] = target
    return tuple(targets.values())


def load_warning_plan(
    request: IncompleteGamePurgeRequest,
) -> WarningPlan | None:
    """Freeze unrecorded warning targets for one still-eligible game."""

    with models.db.connection_context():
        game = _load_game(request.game_id, request.guild_id)
        if game is None:
            return None
        player_count = len(tuple(game.lineup))
        if classify_game(
            game,
            as_of=request.as_of,
            player_count=player_count,
        ) != 'warning':
            return None
        threshold = purge_threshold_days(player_count, bool(game.is_ranked))
        protected_through = effective_protected_through(
            game, player_count=player_count,
        )
        targets = tuple(
            target for target in _warning_targets(game)
            if not _warning_was_recorded(
                game_id=int(game.id),
                guild_id=int(request.guild_id),
                channel_id=target.channel_id,
                protected_through=protected_through,
                allow_legacy=getattr(game, 'cleanup_deferred_until', None) is None,
            )
        )
        rank_text = 'ranked' if game.is_ranked else 'unranked'
        return WarningPlan(
            game_id=int(game.id),
            threshold_days=int(threshold),
            protected_through=protected_through,
            message=(
                f'Warning: this incomplete {rank_text} {player_count}-player '
                'game is scheduled for cleanup soon. If the game is still '
                'active, finish or report it. Otherwise these game channels '
                f'may be deleted after {protected_through.isoformat()} '
                f'(the {threshold}-day cleanup deadline). Use `/game '
                f'keep-active game_id:{game.id}`, or press the **Keep active '
                'for 30 days** button to extend it.'
            ),
            targets=targets,
        )


async def run_load_warning_plan(request):
    return await _await_executor(_read_executor, load_warning_plan, request)


def record_warning_delivery(
    request: WarningDeliveryRequest,
) -> WarningDeliveryResult:
    """Record one successfully delivered game/channel warning target."""

    with models.db.connection_context():
        with models.db.atomic():
            game = _load_game(request.game_id, request.guild_id, lock=True)
            if game is None:
                return WarningDeliveryResult(
                    request.game_id,
                    request.channel_id,
                    SKIPPED_STATE_CHANGED,
                )
            player_count = len(tuple(game.lineup))
            if classify_game(
                game,
                as_of=request.as_of,
                player_count=player_count,
            ) != 'warning':
                return WarningDeliveryResult(
                    request.game_id,
                    request.channel_id,
                    SKIPPED_STATE_CHANGED,
                )
            current_deadline = effective_protected_through(
                game, player_count=player_count,
            )
            frozen_deadline = request.protected_through or current_deadline
            if current_deadline != frozen_deadline:
                return WarningDeliveryResult(
                    request.game_id,
                    request.channel_id,
                    SKIPPED_STATE_CHANGED,
                )
            current_targets = {
                (target.guild_id, target.channel_id)
                for target in _warning_targets(game)
            }
            target_key = (
                int(request.target_guild_id),
                int(request.channel_id),
            )
            if target_key not in current_targets:
                return WarningDeliveryResult(
                    request.game_id,
                    request.channel_id,
                    SKIPPED_STATE_CHANGED,
                )
            if _warning_was_recorded(
                game_id=request.game_id,
                guild_id=request.guild_id,
                channel_id=request.channel_id,
                protected_through=frozen_deadline,
                allow_legacy=(
                    getattr(game, 'cleanup_deferred_until', None) is None
                ),
            ):
                status = WARNING_ALREADY_RECORDED
            else:
                models.GameLog.write(
                    game_id=request.game_id,
                    guild_id=request.guild_id,
                    message=(
                        f'{_warning_marker(
                            request.channel_id,
                            frozen_deadline,
                        )} for '
                        f'incomplete game channel cleanup.'
                    ),
                    is_protected=True,
                )
                status = WARNING_RECORDED
            return WarningDeliveryResult(
                request.game_id,
                request.channel_id,
                status,
            )


async def run_record_warning_delivery(request):
    return await _await_executor(
        _warning_write_executor,
        record_warning_delivery,
        request,
    )


def purge_incomplete_game(
    request: IncompleteGamePurgeRequest,
) -> IncompleteGamePurgeResult:
    """Revalidate and atomically delete one old started incomplete game."""

    with models.db.connection_context():
        with models.db.atomic():
            game = _load_game(request.game_id, request.guild_id, lock=True)
            if game is None:
                return IncompleteGamePurgeResult(
                    game_id=request.game_id,
                    status=SKIPPED_STATE_CHANGED,
                )
            player_count = len(tuple(game.lineup))
            if classify_game(
                game,
                as_of=request.as_of,
                player_count=player_count,
            ) != PURGED:
                return IncompleteGamePurgeResult(
                    game_id=request.game_id,
                    status=SKIPPED_STATE_CHANGED,
                )
            plan = game_deletion_workers.build_effect_plan(
                game,
                guild_id=request.guild_id,
                state=game_deletion_workers.IN_PROGRESS,
            )
            target_text = ','.join(
                f'{target.guild_id}/{target.channel_id}'
                for target in plan.channel_targets
            ) or 'none'
            announcement_text = (
                f'{plan.announcement.channel_id}/'
                f'{plan.announcement.message_id}'
                if plan.announcement else 'none'
            )
            rank_text = 'ranked' if game.is_ranked else 'unranked'
            summary = (
                f'Game {game.id}: purged incomplete {rank_text} '
                f'{player_count}-player game started {game.date}.'
            )
            models.GameLog.write(
                game_id=int(game.id),
                guild_id=int(request.guild_id),
                message=(
                    'Bot purged the old started incomplete game. '
                    f'Reconciliation targets: announcement={announcement_text}; '
                    f'channels={target_text}.'
                ),
                is_protected=True,
            )
            game.delete_game()
            return IncompleteGamePurgeResult(
                game_id=request.game_id,
                status=PURGED,
                summary=summary,
                effect_plan=plan,
            )


async def run_purge_incomplete_game(request):
    lock_acquired = False

    def lock_game():
        nonlocal lock_acquired
        utilities.lock_game(request.game_id)
        lock_acquired = True

    def unlock_game():
        if lock_acquired:
            utilities.unlock_game(request.game_id)

    return await settings.elo_job_coordinator.run(
        operation='auto_purge_incomplete_game',
        game_id=request.game_id,
        requester_id=None,
        requester_name='automatic incomplete-game purge',
        worker=purge_incomplete_game,
        worker_args=(request,),
        before_submit=lock_game,
        after_complete=unlock_game,
    )
