"""Self-service, exactly owned development game lanes for human testers."""

from __future__ import annotations

import asyncio
import datetime
import functools
import hashlib
import json
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

import peewee

import settings
from modules import beta_lab_manifest, beta_readiness, dev_fixtures, elo_jobs, models


SESSION_VERSION = 1
NOTES_PREFIX = f'polybot-dev-beta-lab-session:v{SESSION_VERSION};'
NAME_PREFIX = f'Bl{SESSION_VERSION}-'
SCENARIOS = ('ready', 'unconfirmed', 'completed')
MAX_TRACKED_SESSIONS = 12
_SESSION_ID = re.compile(r'^[0-9a-f]{12}$')
_NOTES = re.compile(
    rf'^{re.escape(NOTES_PREFIX)}'
    r'lease=([0-9a-f]{12});owner=([0-9]+);opponent=([0-9]+);'
    r'expires=([0-9]+);scenario=(ready|unconfirmed|completed)$'
)
_NAME = re.compile(
    rf'^{re.escape(NAME_PREFIX)}([0-9a-f]{{12}})-(R|U|C)$',
    re.IGNORECASE,
)
_SCENARIO_CODE = {'ready': 'R', 'unconfirmed': 'U', 'completed': 'C'}
_CODE_SCENARIO = {value: key for key, value in _SCENARIO_CODE.items()}


class BetaLabSessionError(RuntimeError):
    """An expected self-service lane refusal suitable for private display."""


class BetaLabSessionPermissionError(BetaLabSessionError):
    pass


class BetaLabSessionValidationError(BetaLabSessionError):
    pass


@dataclass(frozen=True)
class BetaLabSessionRequest:
    guild_id: int
    requester_id: int
    requester_name: str
    role_ids: tuple[int, ...]


@dataclass(frozen=True)
class BetaLabSessionReleaseRequest:
    guild_id: int
    requester_id: int
    requester_name: str
    role_ids: tuple[int, ...]
    session_id: str
    outcome: str


@dataclass(frozen=True)
class BetaLabSessionScenario:
    scenario: str
    game_id: int
    status: str


@dataclass(frozen=True)
class BetaLabSessionSnapshot:
    session_id: str
    guild_id: int
    requester_id: int
    requester_name: str
    opponent_id: int
    opponent_name: str
    expires_epoch: int
    state: str
    scenarios: tuple[BetaLabSessionScenario, ...]
    fingerprint: str

    @property
    def game_ids(self) -> tuple[int, ...]:
        return tuple(item.game_id for item in self.scenarios)


@dataclass(frozen=True)
class BetaLabSessionSummary:
    capacity: int
    active: int
    expired: int
    ambiguous_game_ids: tuple[int, ...]


@dataclass(frozen=True)
class BetaLabSessionReleaseResult:
    session_id: str
    released: bool
    removed_game_ids: tuple[int, ...]
    outcome: str


@dataclass(frozen=True)
class _Marker:
    session_id: str
    owner_id: int
    opponent_id: int
    expires_epoch: int
    scenario: str


@dataclass(frozen=True)
class _Record:
    game: Any
    marker: _Marker | None
    valid_ownership: bool


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _manifest() -> beta_lab_manifest.BetaLabManifest:
    return beta_lab_manifest.load(_project_root())


def _validate_profile(guild_id: int) -> int:
    try:
        return beta_readiness.validate_database_profile(
            settings.runtime_profile,
            int(guild_id),
        )
    except (beta_readiness.ReadinessError, ValueError) as exc:
        raise BetaLabSessionValidationError(str(exc)) from exc


def _validate_request(
    request: BetaLabSessionRequest | BetaLabSessionReleaseRequest,
    *,
    require_tester: bool = True,
) -> tuple[int, beta_lab_manifest.BetaLabManifest]:
    guild_id = _validate_profile(request.guild_id)
    try:
        manifest = _manifest()
    except beta_lab_manifest.BetaLabManifestError as exc:
        raise BetaLabSessionValidationError(str(exc)) from exc
    if guild_id != manifest.guild_id:
        raise BetaLabSessionValidationError('The lane manifest targets another guild.')
    requester_id = int(request.requester_id)
    if requester_id <= 0 or requester_id in {
        int(settings.bot_id),
        int(settings.bot_id_beta),
    }:
        raise BetaLabSessionPermissionError('Bot identities cannot claim a test lane.')
    role_ids = {int(value) for value in request.role_ids}
    if require_tester and (
        requester_id != int(settings.owner_id)
        and manifest.tester_role_id not in role_ids
    ):
        raise BetaLabSessionPermissionError(
            'Self-service game lanes are limited to the pinned testers role.'
        )
    return guild_id, manifest


def _live_identity() -> None:
    try:
        dev_fixtures.validate_live_identity(*dev_fixtures._live_identity(models))
    except dev_fixtures.FixtureSafetyError as exc:
        raise BetaLabSessionValidationError(str(exc)) from exc


def _notes(marker: _Marker) -> str:
    return (
        f'{NOTES_PREFIX}lease={marker.session_id};owner={marker.owner_id};'
        f'opponent={marker.opponent_id};expires={marker.expires_epoch};'
        f'scenario={marker.scenario}'
    )


def _name(session_id: str, scenario: str) -> str:
    return f'{NAME_PREFIX}{session_id}-{_SCENARIO_CODE[scenario]}'


def _parse_notes(value: Any) -> _Marker | None:
    match = _NOTES.fullmatch(str(value or ''))
    if match is None:
        return None
    return _Marker(
        session_id=match.group(1),
        owner_id=int(match.group(2)),
        opponent_id=int(match.group(3)),
        expires_epoch=int(match.group(4)),
        scenario=match.group(5),
    )


def _parse_name(value: Any) -> tuple[str, str] | None:
    match = _NAME.fullmatch(str(value or ''))
    if match is None:
        return None
    return match.group(1).lower(), _CODE_SCENARIO[match.group(2).upper()]


def _record(game: Any) -> _Record:
    marker = _parse_notes(game.notes)
    name_marker = _parse_name(game.name)
    valid = bool(
        marker is not None
        and name_marker is not None
        and name_marker == (marker.session_id, marker.scenario)
        and int(game.guild_id) == beta_readiness.BETA_GUILD_ID
    )
    return _Record(game=game, marker=marker, valid_ownership=valid)


def _find_games(*, for_update: bool = False) -> tuple[Any, ...]:
    query = (
        models.Game.select()
        .where(
            (models.Game.guild_id == beta_readiness.BETA_GUILD_ID)
            & (
                models.Game.notes.startswith(NOTES_PREFIX)
                | models.Game.name.startswith(NAME_PREFIX)
            )
        )
        .order_by(models.Game.id)
        .limit((MAX_TRACKED_SESSIONS * len(SCENARIOS)) + 1)
    )
    if for_update and isinstance(models.db, peewee.PostgresqlDatabase):
        query = query.for_update()
    return tuple(query)


def _records_by_session(
    games: Sequence[Any],
) -> tuple[dict[str, tuple[_Record, ...]], tuple[int, ...]]:
    grouped: dict[str, list[_Record]] = {}
    ambiguous: list[int] = []
    for game in games:
        record = _record(game)
        name_marker = _parse_name(game.name)
        session_id = (
            record.marker.session_id
            if record.marker is not None
            else name_marker[0] if name_marker is not None else None
        )
        if session_id is None:
            ambiguous.append(int(game.id))
            continue
        grouped.setdefault(session_id, []).append(record)
        if not record.valid_ownership:
            ambiguous.append(int(game.id))
    return (
        {key: tuple(value) for key, value in grouped.items()},
        tuple(sorted(ambiguous)),
    )


def _participant_ids(game: Any) -> tuple[int, ...]:
    return tuple(sorted(
        int(lineup.player.discord_member.discord_id)
        for lineup in game.lineup
    ))


def _scenario_status(game: Any, scenario: str) -> str:
    winner_position = int(game.winner.position) if game.winner is not None else None
    if scenario == 'ready':
        ready = (
            not game.is_pending
            and not game.is_completed
            and not game.is_confirmed
            and winner_position is None
        )
    elif scenario == 'unconfirmed':
        ready = (
            not game.is_pending
            and game.is_completed
            and not game.is_confirmed
            and winner_position == 1
        )
    else:
        ready = (
            not game.is_pending
            and game.is_completed
            and game.is_confirmed
            and winner_position == 1
        )
    return 'ready' if ready else 'exercised'


def _names(user_ids: Sequence[int]) -> dict[int, str]:
    rows = tuple(
        models.Player
        .select(models.Player, models.DiscordMember)
        .join(models.DiscordMember)
        .where(
            (models.Player.guild_id == beta_readiness.BETA_GUILD_ID)
            & (models.DiscordMember.discord_id.in_(tuple(user_ids)))
        )
    )
    return {
        int(row.discord_member.discord_id): str(
            row.name or row.nick or row.discord_member.name
        )
        for row in rows
    }


def _snapshot(
    session_id: str,
    records: Sequence[_Record],
    *,
    now_epoch: int,
) -> BetaLabSessionSnapshot:
    if not records or any(not item.valid_ownership for item in records):
        raise BetaLabSessionValidationError(
            'A Beta Lab lane has damaged ownership markers; an operator must review it.'
        )
    markers = tuple(item.marker for item in records)
    first = markers[0]
    if first is None:
        raise BetaLabSessionValidationError('A Beta Lab lane marker is missing.')
    if any(
        marker is None
        or marker.session_id != session_id
        or marker.owner_id != first.owner_id
        or marker.opponent_id != first.opponent_id
        or marker.expires_epoch != first.expires_epoch
        for marker in markers
    ):
        raise BetaLabSessionValidationError(
            'A Beta Lab lane has inconsistent ownership metadata.'
        )
    scenario_names = tuple(marker.scenario for marker in markers if marker is not None)
    if (
        len(records) != len(SCENARIOS)
        or len(scenario_names) != len(set(scenario_names))
        or set(scenario_names) != set(SCENARIOS)
    ):
        raise BetaLabSessionValidationError(
            'A Beta Lab lane does not contain exactly the three reviewed scenarios.'
        )
    expected_participants = tuple(sorted((first.owner_id, first.opponent_id)))
    if any(_participant_ids(item.game) != expected_participants for item in records):
        raise BetaLabSessionValidationError(
            'A Beta Lab lane participant graph no longer matches its marker.'
        )
    scenarios = tuple(sorted(
        (
            BetaLabSessionScenario(
                scenario=item.marker.scenario,
                game_id=int(item.game.id),
                status=_scenario_status(item.game, item.marker.scenario),
            )
            for item in records
            if item.marker is not None
        ),
        key=lambda item: SCENARIOS.index(item.scenario),
    ))
    people = _names((first.owner_id, first.opponent_id))
    if set(people) != {first.owner_id, first.opponent_id}:
        raise BetaLabSessionValidationError(
            'A Beta Lab lane participant is no longer registered.'
        )
    if first.expires_epoch <= now_epoch:
        state = 'expired'
    elif len(scenarios) == len(SCENARIOS) and all(
        item.status == 'ready' for item in scenarios
    ):
        state = 'ready'
    else:
        state = 'in progress'
    payload = {
        'session_id': session_id,
        'owner_id': first.owner_id,
        'opponent_id': first.opponent_id,
        'expires_epoch': first.expires_epoch,
        'scenarios': tuple(asdict(item) for item in scenarios),
    }
    fingerprint = hashlib.sha256(json.dumps(
        payload,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')).hexdigest()
    return BetaLabSessionSnapshot(
        session_id=session_id,
        guild_id=beta_readiness.BETA_GUILD_ID,
        requester_id=first.owner_id,
        requester_name=people[first.owner_id],
        opponent_id=first.opponent_id,
        opponent_name=people[first.opponent_id],
        expires_epoch=first.expires_epoch,
        state=state,
        scenarios=scenarios,
        fingerprint=fingerprint,
    )


def _load_all(*, for_update: bool, now_epoch: int):
    games = _find_games(for_update=for_update)
    if len(games) > MAX_TRACKED_SESSIONS * len(SCENARIOS):
        raise BetaLabSessionValidationError(
            'The Beta Lab lane row bound was exceeded; manual review is required.'
        )
    grouped, ambiguous = _records_by_session(games)
    if ambiguous:
        raise BetaLabSessionValidationError(
            'Damaged Beta Lab ownership markers require operator review: '
            + ', '.join(str(value) for value in ambiguous)
        )
    snapshots = tuple(
        _snapshot(session_id, records, now_epoch=now_epoch)
        for session_id, records in sorted(grouped.items())
    )
    return grouped, snapshots


def load_summary(
    guild_id: int,
    *,
    now_epoch: int | None = None,
) -> BetaLabSessionSummary:
    _validate_profile(guild_id)
    manifest = _manifest()
    now_epoch = int(time.time() if now_epoch is None else now_epoch)
    with models.db.connection_context():
        _live_identity()
        games = _find_games()
        if len(games) > MAX_TRACKED_SESSIONS * len(SCENARIOS):
            return BetaLabSessionSummary(
                capacity=manifest.maximum_active_game_lanes,
                active=0,
                expired=0,
                ambiguous_game_ids=tuple(int(game.id) for game in games),
            )
        grouped, ambiguous = _records_by_session(games)
        snapshots = []
        for session_id, records in grouped.items():
            try:
                snapshots.append(_snapshot(session_id, records, now_epoch=now_epoch))
            except BetaLabSessionValidationError:
                ambiguous = tuple(sorted({
                    *ambiguous,
                    *(int(item.game.id) for item in records),
                }))
    return BetaLabSessionSummary(
        capacity=manifest.maximum_active_game_lanes,
        active=sum(item.state != 'expired' for item in snapshots),
        expired=sum(item.state == 'expired' for item in snapshots),
        ambiguous_game_ids=tuple(ambiguous),
    )


def load_requester_session(
    request: BetaLabSessionRequest,
    *,
    now_epoch: int | None = None,
) -> BetaLabSessionSnapshot | None:
    guild_id, _manifest_value = _validate_request(
        request,
        require_tester=False,
    )
    now_epoch = int(time.time() if now_epoch is None else now_epoch)
    with models.db.connection_context():
        _live_identity()
        _grouped, snapshots = _load_all(for_update=False, now_epoch=now_epoch)
    owned = tuple(
        item for item in snapshots if item.requester_id == int(request.requester_id)
    )
    if len(owned) > 1:
        raise BetaLabSessionValidationError(
            'You have more than one owned lane; an operator must reconcile them.'
        )
    return owned[0] if owned else None


def _delete_records(records: Sequence[_Record]) -> tuple[int, ...]:
    game_ids = tuple(sorted(int(item.game.id) for item in records))
    for item in sorted(
        records,
        key=lambda value: (
            value.game.completed_ts.timestamp()
            if value.game.completed_ts is not None
            else float('-inf')
        ),
        reverse=True,
    ):
        item.game.delete_game()
    return game_ids


def _create_game(
    *,
    marker: _Marker,
    players: Sequence[Any],
) -> Any:
    game = models.Game.create(
        guild_id=beta_readiness.BETA_GUILD_ID,
        host=players[0],
        name=_name(marker.session_id, marker.scenario),
        notes=_notes(marker),
        is_pending=False,
        is_ranked=True,
        is_mobile=True,
        size=[1, 1],
    )
    first_side = models.GameSide.create(
        game=game,
        size=1,
        position=1,
        sidename='Tester',
    )
    second_side = models.GameSide.create(
        game=game,
        size=1,
        position=2,
        sidename='Fixture Opponent',
    )
    models.Lineup.create(game=game, gameside=first_side, player=players[0])
    models.Lineup.create(game=game, gameside=second_side, player=players[1])
    if marker.scenario == 'unconfirmed':
        game.win_claimed_ts = datetime.datetime.now()
        game.save()
        game.declare_winner(winning_side=first_side, confirm=False)
    elif marker.scenario == 'completed':
        game.declare_winner(winning_side=first_side, confirm=True)
    return game


def _actor(value: str, requester_id: int) -> str:
    normalized = ' '.join(str(value).split())[:80] or 'Tester'
    return f'{normalized} (`{int(requester_id)}`)'


def claim_session(
    request: BetaLabSessionRequest,
    *,
    now_epoch: int | None = None,
    session_id_factory=secrets.token_hex,
) -> BetaLabSessionSnapshot:
    guild_id, manifest = _validate_request(request)
    now_epoch = int(time.time() if now_epoch is None else now_epoch)
    with models.db.connection_context():
        _live_identity()
        with models.db.atomic():
            grouped, snapshots = _load_all(for_update=True, now_epoch=now_epoch)
            existing = tuple(
                item for item in snapshots
                if item.requester_id == int(request.requester_id)
                and item.state != 'expired'
            )
            if len(existing) > 1:
                raise BetaLabSessionValidationError(
                    'You have more than one active lane; an operator must reconcile them.'
                )
            if existing:
                return existing[0]
            for snapshot in snapshots:
                if snapshot.state == 'expired':
                    _delete_records(grouped[snapshot.session_id])
            active = sum(item.state != 'expired' for item in snapshots)
            if active >= manifest.maximum_active_game_lanes:
                raise BetaLabSessionValidationError(
                    f'All {manifest.maximum_active_game_lanes} mutable game '
                    'lanes are currently in use. '
                    'Try a read-only quick test or return in a few minutes.'
                )
            requester_id = int(request.requester_id)
            opponent_id = next(
                value for value in manifest.opponent_user_ids
                if value != requester_id
            )
            try:
                players = dev_fixtures._load_players(
                    models,
                    guild_id,
                    (requester_id, opponent_id),
                )
            except dev_fixtures.FixtureValidationError as exc:
                raise BetaLabSessionValidationError(
                    'You need a registered development Player before claiming '
                    'a game lane. Run `/player register`, then try again.'
                ) from exc
            session_id = str(session_id_factory(6)).lower()
            if not _SESSION_ID.fullmatch(session_id):
                raise BetaLabSessionValidationError(
                    'The generated lane identifier was invalid.'
                )
            if session_id in grouped:
                raise BetaLabSessionValidationError(
                    'The generated lane identifier collided; try again.'
                )
            expires = now_epoch + (manifest.lease_minutes * 60)
            for scenario in SCENARIOS:
                _create_game(
                    marker=_Marker(
                        session_id=session_id,
                        owner_id=requester_id,
                        opponent_id=opponent_id,
                        expires_epoch=expires,
                        scenario=scenario,
                    ),
                    players=players,
                )
            models.GameLog.write(
                guild_id=guild_id,
                game_id=0,
                is_protected=True,
                message=(
                    f'Beta Lab lane {session_id} claimed by '
                    f'{_actor(request.requester_name, requester_id)}; '
                    f'opponent={opponent_id}; expires={expires}'
                ),
            )
            new_games = []
            for game in _find_games(for_update=True):
                marker = _parse_notes(game.notes)
                if marker is not None and marker.session_id == session_id:
                    new_games.append(game)
            records = tuple(_record(game) for game in new_games)
            return _snapshot(session_id, records, now_epoch=now_epoch)


def release_session(
    request: BetaLabSessionReleaseRequest,
    *,
    now_epoch: int | None = None,
) -> BetaLabSessionReleaseResult:
    guild_id, _manifest_value = _validate_request(
        request,
        require_tester=False,
    )
    session_id = str(request.session_id).lower()
    if not _SESSION_ID.fullmatch(session_id):
        raise BetaLabSessionValidationError('The selected lane ID is invalid.')
    if request.outcome not in {'finished', 'released'}:
        raise BetaLabSessionValidationError('Unknown lane release outcome.')
    now_epoch = int(time.time() if now_epoch is None else now_epoch)
    with models.db.connection_context():
        _live_identity()
        with models.db.atomic():
            grouped, snapshots = _load_all(for_update=True, now_epoch=now_epoch)
            records = grouped.get(session_id)
            if records is None:
                return BetaLabSessionReleaseResult(
                    session_id=session_id,
                    released=False,
                    removed_game_ids=(),
                    outcome=request.outcome,
                )
            snapshot = next(
                item for item in snapshots if item.session_id == session_id
            )
            if snapshot.requester_id != int(request.requester_id):
                raise BetaLabSessionPermissionError(
                    'Only the tester who claimed this lane can release it.'
                )
            removed = _delete_records(records)
            models.GameLog.write(
                guild_id=guild_id,
                game_id=0,
                is_protected=True,
                message=(
                    f'Beta Lab lane {session_id} {request.outcome} by '
                    f'{_actor(request.requester_name, request.requester_id)}; '
                    f'removed_games={",".join(str(value) for value in removed)}'
                ),
            )
            return BetaLabSessionReleaseResult(
                session_id=session_id,
                released=True,
                removed_game_ids=removed,
                outcome=request.outcome,
            )


_read_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='polybot-beta-lab-session-read',
)


async def _run_read(function, *args):
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(
        _read_executor,
        functools.partial(function, *args),
    )
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        task = asyncio.current_task()
        while not future.done():
            if task is not None:
                while task.cancelling():
                    task.uncancel()
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                continue
        future.result()
        raise


async def _finish_started(task: asyncio.Task):
    current = asyncio.current_task()
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()
            if current is not None:
                while current.cancelling():
                    current.uncancel()


async def run_requester_session(
    request: BetaLabSessionRequest,
) -> BetaLabSessionSnapshot | None:
    return await _run_read(load_requester_session, request)


async def _run_mutation(operation: str, request, worker):
    try:
        task = asyncio.create_task(settings.elo_job_coordinator.run(
            operation=operation,
            game_id=None,
            requester_id=int(request.requester_id),
            requester_name=str(request.requester_name),
            worker=worker,
            worker_args=(request,),
        ))
        return await _finish_started(task)
    except elo_jobs.EloJobConflict as exc:
        raise BetaLabSessionValidationError(
            f'ELO operation `{exc.active_job.operation}` is already active. '
            'Wait for it to finish, then retry.'
        ) from exc


async def run_claim_session(
    request: BetaLabSessionRequest,
) -> BetaLabSessionSnapshot:
    return await _run_mutation('beta_lab_lane_claim', request, claim_session)


async def run_release_session(
    request: BetaLabSessionReleaseRequest,
) -> BetaLabSessionReleaseResult:
    return await _run_mutation('beta_lab_lane_release', request, release_session)
