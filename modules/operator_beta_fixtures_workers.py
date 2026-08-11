"""Bounded development-beta fixture readiness and operator mutations."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace

import settings
from modules import dev_fixtures, elo_jobs, models


PREPARE = 'prepare'
RESET = 'reset'


class BetaFixtureError(RuntimeError):
    """Base class for an expected, privately publishable fixture refusal."""


class BetaFixturePermissionError(BetaFixtureError):
    """The requester is not the configured owner."""


class BetaFixtureValidationError(BetaFixtureError):
    """The request or current owned bundle cannot be changed safely."""


class BetaFixtureStaleError(BetaFixtureError):
    """The owned fixture state changed after preview."""


@dataclass(frozen=True)
class BetaFixtureReadRequest:
    guild_id: int


@dataclass(frozen=True)
class BetaFixturePreviewRequest:
    operation: str
    guild_id: int
    requester_id: int
    user_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class BetaFixtureCommitRequest:
    operation: str
    guild_id: int
    requester_id: int
    requester_description: str
    user_ids: tuple[int, ...]
    expected_game_ids: tuple[int, ...]
    expected_fingerprint: str


@dataclass(frozen=True)
class BetaFixtureScenario:
    scenario: str
    game_id: int
    status: str


@dataclass(frozen=True)
class BetaFixtureParticipant:
    user_id: int
    display_name: str


@dataclass(frozen=True)
class BetaFixtureSnapshot:
    guild_id: int
    user_ids: tuple[int, ...]
    scenarios: tuple[BetaFixtureScenario, ...]
    game_ids: tuple[int, ...]
    readiness: str
    detail: str
    resettable: bool
    fingerprint: str
    participants: tuple[BetaFixtureParticipant, ...] = ()


@dataclass(frozen=True)
class BetaFixturePreview:
    operation: str
    snapshot: BetaFixtureSnapshot
    user_ids: tuple[int, ...]
    can_commit: bool
    participants: tuple[BetaFixtureParticipant, ...] = ()


@dataclass(frozen=True)
class BetaFixtureResult:
    operation: str
    guild_id: int
    user_ids: tuple[int, ...]
    scenarios: tuple[BetaFixtureScenario, ...]
    old_game_ids: tuple[int, ...]
    new_game_ids: tuple[int, ...]


def _validate_runtime(guild_id: int) -> int:
    try:
        dev_fixtures.validate_profile(settings.runtime_profile)
        return dev_fixtures.validate_guild_id(
            settings.runtime_profile,
            int(guild_id),
        )
    except (dev_fixtures.FixtureSafetyError, ValueError) as exc:
        raise BetaFixtureValidationError(str(exc)) from exc


def _validate_owner(requester_id: int) -> None:
    if int(requester_id) != int(settings.owner_id):
        raise BetaFixturePermissionError(
            'Only the configured bot owner can prepare or reset beta fixtures.'
        )


def _scenario_status(game: dev_fixtures.FixtureGame) -> str:
    if game.scenario == 'ready':
        valid = (
            game.is_ranked
            and not game.is_completed
            and not game.is_confirmed
            and not game.is_pending
            and game.winner_position is None
            and game.league_season is None
            and game.league_tier is None
        )
    elif game.scenario == 'unconfirmed':
        valid = (
            game.is_ranked
            and game.is_completed
            and not game.is_confirmed
            and not game.is_pending
            and game.winner_position == 1
            and game.league_season
            == dev_fixtures.FIXTURE_CURRENT_LEAGUE_SEASON
            and game.league_tier == dev_fixtures.FIXTURE_LEAGUE_TIER
        )
    elif game.scenario == 'completed':
        valid = (
            game.is_ranked
            and game.is_completed
            and game.is_confirmed
            and not game.is_pending
            and game.winner_position == 1
            and game.league_season
            == dev_fixtures.FIXTURE_COMPLETED_LEAGUE_SEASON
            and game.league_tier == dev_fixtures.FIXTURE_LEAGUE_TIER
        )
    else:
        valid = False
    return 'ready' if valid else 'needs reset'


def _fingerprint_payload(state: dev_fixtures.FixtureState) -> dict:
    return {
        'guild_id': int(state.guild_id),
        'user_ids': tuple(int(value) for value in state.user_ids),
        'games': tuple(asdict(game) for game in state.games),
    }


def _fingerprint(state: dev_fixtures.FixtureState) -> str:
    encoded = json.dumps(
        _fingerprint_payload(state),
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _snapshot(state: dev_fixtures.FixtureState) -> BetaFixtureSnapshot:
    games = tuple(state.games)
    scenarios = tuple(
        BetaFixtureScenario(
            scenario=str(game.scenario),
            game_id=int(game.game_id),
            status=_scenario_status(game),
        )
        for game in games
    )
    game_ids = tuple(int(game.game_id) for game in games)
    if not games:
        readiness = 'needs preparation'
        detail = 'No owned result-scenario bundle exists.'
        resettable = False
    else:
        names = tuple(game.scenario for game in games)
        exact_names = (
            len(names) <= len(dev_fixtures.SCENARIOS)
            and len(names) == len(set(names))
            and set(names).issubset(set(dev_fixtures.SCENARIOS))
        )
        exact_users = (
            len(state.user_ids) == 2
            and all(game.participant_ids == state.user_ids for game in games)
        )
        resettable = exact_names and exact_users
        complete = set(names) == set(dev_fixtures.SCENARIOS)
        all_ready = complete and all(
            scenario.status == 'ready' for scenario in scenarios
        )
        if all_ready and resettable:
            readiness = 'ready'
            detail = 'All three owned result scenarios are ready for testing.'
        elif resettable:
            readiness = 'needs reset'
            detail = (
                'The exact owned bundle is incomplete or has been exercised; '
                'an owner reset can restore it.'
            )
        else:
            readiness = 'manual review required'
            detail = (
                'The owned rows are ambiguous, oversized, or do not resolve '
                'to exactly two participants.'
            )
    return BetaFixtureSnapshot(
        guild_id=int(state.guild_id),
        user_ids=tuple(int(value) for value in state.user_ids),
        scenarios=scenarios,
        game_ids=game_ids,
        readiness=readiness,
        detail=detail,
        resettable=resettable,
        fingerprint=_fingerprint(state),
    )


def _load_state(guild_id: int) -> dev_fixtures.FixtureState:
    try:
        return dev_fixtures.fixture_status(
            profile=settings.runtime_profile,
            models_module=models,
            guild_id=guild_id,
            maximum_games=len(dev_fixtures.SCENARIOS) + 1,
        )
    except (
        dev_fixtures.FixtureSafetyError,
        dev_fixtures.FixtureValidationError,
    ) as exc:
        raise BetaFixtureValidationError(str(exc)) from exc


def _load_participants(
    guild_id: int,
    user_ids: tuple[int, ...],
) -> tuple[BetaFixtureParticipant, ...]:
    if not user_ids:
        return ()
    with models.db.connection_context():
        try:
            dev_fixtures.validate_live_identity(
                *dev_fixtures._live_identity(models)
            )
        except dev_fixtures.FixtureSafetyError as exc:
            raise BetaFixtureValidationError(str(exc)) from exc
        rows = tuple(
            models.Player
            .select(models.Player, models.DiscordMember)
            .join(models.DiscordMember)
            .where(
                (models.Player.guild_id == guild_id)
                & (models.DiscordMember.discord_id.in_(user_ids))
            )
        )
    names = {
        int(row.discord_member.discord_id): str(
            row.name or row.nick or row.discord_member.name
        )
        for row in rows
    }
    return tuple(
        BetaFixtureParticipant(
            user_id=user_id,
            display_name=names.get(user_id, f'User {user_id}'),
        )
        for user_id in user_ids
    )


def load_readiness(request: BetaFixtureReadRequest) -> BetaFixtureSnapshot:
    guild_id = _validate_runtime(request.guild_id)
    snapshot = _snapshot(_load_state(guild_id))
    return replace(
        snapshot,
        participants=_load_participants(guild_id, snapshot.user_ids),
    )


def _validate_registered_users(guild_id: int, user_ids: tuple[int, ...]) -> None:
    try:
        normalized = dev_fixtures.validate_user_ids(user_ids)
    except dev_fixtures.FixtureValidationError as exc:
        raise BetaFixtureValidationError(str(exc)) from exc
    if len(normalized) != 2:
        raise BetaFixtureValidationError(
            'The Discord operator workflow currently requires exactly two '
            'registered development members.'
        )
    protected_bot_ids = {
        int(settings.bot_id),
        int(settings.bot_id_beta),
    }
    if protected_bot_ids.intersection(normalized):
        raise BetaFixtureValidationError(
            'Bot identities cannot be used as beta fixture participants.'
        )
    with models.db.connection_context():
        try:
            dev_fixtures.validate_live_identity(
                *dev_fixtures._live_identity(models)
            )
            dev_fixtures._load_players(models, guild_id, normalized)
        except (
            dev_fixtures.FixtureSafetyError,
            dev_fixtures.FixtureValidationError,
        ) as exc:
            raise BetaFixtureValidationError(str(exc)) from exc


def load_preview(request: BetaFixturePreviewRequest) -> BetaFixturePreview:
    _validate_owner(request.requester_id)
    guild_id = _validate_runtime(request.guild_id)
    snapshot = load_readiness(BetaFixtureReadRequest(guild_id=guild_id))
    if request.operation == PREPARE:
        user_ids = tuple(sorted(int(value) for value in request.user_ids))
        _validate_registered_users(guild_id, user_ids)
        if snapshot.game_ids:
            raise BetaFixtureValidationError(
                'An owned fixture bundle already exists. Use reset after '
                'reviewing its current participants and scenario IDs.'
            )
    elif request.operation == RESET:
        if not snapshot.game_ids:
            raise BetaFixtureValidationError(
                'No owned fixture bundle exists. Use prepare with two '
                'registered development members.'
            )
        if not snapshot.resettable:
            raise BetaFixtureValidationError(snapshot.detail)
        user_ids = snapshot.user_ids
        _validate_registered_users(guild_id, user_ids)
    else:
        raise BetaFixtureValidationError('Unknown beta fixture operation.')
    return BetaFixturePreview(
        operation=request.operation,
        snapshot=snapshot,
        user_ids=user_ids,
        can_commit=True,
        participants=(
            snapshot.participants
            if user_ids == snapshot.user_ids
            else _load_participants(guild_id, user_ids)
        ),
    )


def commit_fixtures(request: BetaFixtureCommitRequest) -> BetaFixtureResult:
    _validate_owner(request.requester_id)
    guild_id = _validate_runtime(request.guild_id)
    current_state = _load_state(guild_id)
    current = _snapshot(current_state)
    if current.fingerprint != request.expected_fingerprint:
        raise BetaFixtureStaleError(
            'The owned fixture state changed after preview. Run the command '
            'again and review fresh state.'
        )
    if current.game_ids != tuple(
        int(value) for value in request.expected_game_ids
    ):
        raise BetaFixtureStaleError(
            'The confirmed fixture game IDs do not match the current bundle.'
        )
    _validate_registered_users(guild_id, request.user_ids)
    audit_message = (
        f'{request.requester_description} ran /operator beta '
        f'{request.operation}; participants='
        + ','.join(str(value) for value in request.user_ids)
    )
    try:
        if request.operation == PREPARE:
            if current.game_ids:
                raise BetaFixtureStaleError(
                    'An owned fixture bundle appeared after preview.'
                )
            state = dev_fixtures.prepare_fixtures_in_process(
                profile=settings.runtime_profile,
                models_module=models,
                guild_id=guild_id,
                user_ids=request.user_ids,
                audit_message=audit_message,
            )
        elif request.operation == RESET:
            if not current.resettable:
                raise BetaFixtureValidationError(current.detail)
            state = dev_fixtures.reset_fixtures_in_process(
                profile=settings.runtime_profile,
                models_module=models,
                guild_id=guild_id,
                user_ids=request.user_ids,
                expected_state=current_state,
                audit_message=audit_message,
            )
        else:
            raise BetaFixtureValidationError(
                'Unknown beta fixture operation.'
            )
    except dev_fixtures.FixtureSafetyError as exc:
        raise BetaFixtureValidationError(str(exc)) from exc
    except dev_fixtures.FixtureValidationError as exc:
        raise BetaFixtureStaleError(str(exc)) from exc
    final = _snapshot(state)
    return BetaFixtureResult(
        operation=request.operation,
        guild_id=guild_id,
        user_ids=final.user_ids,
        scenarios=final.scenarios,
        old_game_ids=current.game_ids,
        new_game_ids=final.game_ids,
    )


_read_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='polybot-beta-fixture-read',
)


async def _run_read(function, request):
    loop = asyncio.get_running_loop()
    concurrent_future = _read_executor.submit(
        functools.partial(function, request)
    )
    future = asyncio.wrap_future(concurrent_future, loop=loop)
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


async def run_readiness(
    request: BetaFixtureReadRequest,
) -> BetaFixtureSnapshot:
    return await _run_read(load_readiness, request)


async def run_preview(
    request: BetaFixturePreviewRequest,
) -> BetaFixturePreview:
    return await _run_read(load_preview, request)


async def run_commit(
    request: BetaFixtureCommitRequest,
) -> BetaFixtureResult:
    try:
        return await settings.elo_job_coordinator.run(
            operation=f'beta_fixture_{request.operation}',
            game_id=None,
            requester_id=int(request.requester_id),
            requester_name=str(request.requester_description),
            worker=commit_fixtures,
            worker_args=(request,),
        )
    except elo_jobs.EloJobConflict as exc:
        raise BetaFixtureValidationError(
            f'ELO operation `{exc.active_job.operation}` is already active. '
            'Wait for it to finish, then load a fresh fixture preview.'
        ) from exc
