"""Worker-owned status and refresh operations for the development Beta Lab."""

from __future__ import annotations

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

import settings
from modules import (
    beta_lab_manifest,
    beta_lab_personas,
    beta_lab_sessions,
    beta_readiness,
    beta_wider_setup,
    dev_fixtures,
    models,
    operator_beta_fixtures_workers as result_workers,
)


STRUCTURE = 'server-structure'
LEADERBOARD = 'leaderboard-showcase'
RESULTS = 'game-results'
SESSION_LANES = 'self-service-game-lanes'
GUIDED_PERSONAS = 'guided-personas'
PACKS = (STRUCTURE, LEADERBOARD, RESULTS, SESSION_LANES, GUIDED_PERSONAS)
REFRESH_CONFIRMATION = 'REFRESH-game-results'


class BetaLabError(RuntimeError):
    """A bounded Beta Lab status or refresh operation was refused."""


@dataclass(frozen=True)
class BetaLabPackStatus:
    key: str
    title: str
    state: str
    detail: str
    action: str


@dataclass(frozen=True)
class BetaLabStatus:
    guild_id: int
    overall: str
    packs: tuple[BetaLabPackStatus, ...]
    result_snapshot: result_workers.BetaFixtureSnapshot | None

    def as_dict(self) -> dict:
        return asdict(self)

    def plan_dict(self) -> dict:
        value = self.as_dict()
        value['actions'] = [
            {
                'pack': pack.key,
                'state': pack.state,
                'action': pack.action,
            }
            for pack in self.packs
        ]
        value['live_apply_supported'] = [RESULTS]
        value['tester_apply_supported'] = [SESSION_LANES]
        value['discord_resource_mutation_supported'] = False
        return value


@dataclass(frozen=True)
class BetaLabRefreshResult:
    pack: str
    committed: bool
    old_game_ids: tuple[int, ...]
    new_game_ids: tuple[int, ...]
    status: BetaLabStatus | None
    warning: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _validate(guild_id: int) -> int:
    try:
        return beta_readiness.validate_database_profile(
            settings.runtime_profile,
            int(guild_id),
        )
    except (beta_readiness.ReadinessError, ValueError) as exc:
        raise BetaLabError(str(exc)) from exc


def _result_status(guild_id: int):
    snapshot = result_workers.load_readiness(
        result_workers.BetaFixtureReadRequest(guild_id=guild_id)
    )
    if snapshot.readiness == 'ready':
        state = 'ready'
        action = 'Use the listed game IDs; refresh after the scenarios are exercised.'
    elif snapshot.readiness == 'needs reset':
        state = 'refreshable'
        action = 'Refresh the game-results pack to create three fresh IDs.'
    elif snapshot.readiness == 'needs preparation':
        state = 'missing'
        action = 'Use /operator beta prepare with two registered members.'
    else:
        state = 'blocked'
        action = 'Review the ambiguous owned rows before any mutation.'
    return BetaLabPackStatus(
        key=RESULTS,
        title='Game results',
        state=state,
        detail=snapshot.detail,
        action=action,
    ), snapshot


def _leaderboard_status(guild_id: int) -> BetaLabPackStatus:
    try:
        state = dev_fixtures.leaderboard_fixture_status(
            profile=settings.runtime_profile,
            models_module=models,
            guild_id=guild_id,
        )
    except (dev_fixtures.FixtureSafetyError, dev_fixtures.FixtureValidationError) as exc:
        return BetaLabPackStatus(
            key=LEADERBOARD,
            title='Player leaderboard showcase',
            state='blocked',
            detail=str(exc),
            action='Inspect the separately owned leaderboard rows.',
        )
    expected_games = (dev_fixtures.LEADERBOARD_FIXTURE_COUNT // 2) * 4
    ready = (
        len(state.players) == dev_fixtures.LEADERBOARD_FIXTURE_COUNT
        and len(state.game_ids) == expected_games
    )
    return BetaLabPackStatus(
        key=LEADERBOARD,
        title='Player leaderboard showcase',
        state='ready' if ready else 'missing',
        detail=(
            f'{len(state.players)} synthetic players and '
            f'{len(state.game_ids)} completed games are available.'
        ),
        action=(
            'Use player leaderboard filters, pagination, graphs, and activity views.'
            if ready else
            'The stopped-writer leaderboard seed is required before this pack is usable.'
        ),
    )


def _structure_status(guild_id: int) -> BetaLabPackStatus:
    project_root = Path(__file__).resolve().parents[1]
    try:
        raw = beta_readiness.load_json_path(
            project_root,
            beta_wider_setup.DEFAULT_MANIFEST,
            label='reviewed Beta Lab structure manifest',
            max_bytes=beta_readiness.MAX_MANIFEST_BYTES,
        )
        status = beta_wider_setup.status_wider_beta_setup(
            profile=settings.runtime_profile,
            manifest=raw,
            guild_id=guild_id,
        )
    except (beta_readiness.ReadinessError, beta_wider_setup.WiderBetaSetupError) as exc:
        return BetaLabPackStatus(
            key=STRUCTURE,
            title='PolyChamps-shaped server structure',
            state='blocked',
            detail=str(exc),
            action='Inspect the reviewed House/Team setup ownership evidence.',
        )
    conflicts = tuple(status.get('conflicts', ()))
    houses = tuple(status.get('houses', ()))
    teams = tuple(status.get('teams', ()))
    ready = (
        not conflicts
        and len(houses) == 2
        and len(teams) == 3
        and all(bool(item.get('owned')) for item in houses)
        and all(bool(item.get('owned')) for item in teams)
        and all(int(item.get('role_id', 0)) > 0 for item in teams)
    )
    return BetaLabPackStatus(
        key=STRUCTURE,
        title='PolyChamps-shaped server structure',
        state='ready' if ready else 'blocked',
        detail=(
            f'{len(houses)} owned Houses and {len(teams)} showcase Teams '
            'are reconciled with their reviewed role bindings.'
            if ready else '; '.join(conflicts) or 'The exact structure is incomplete.'
        ),
        action=(
            'Use Team, House, tier, roster, and team-leaderboard workflows.'
            if ready else 'Run the stopped-writer structure plan before repair.'
        ),
    )


def _session_status(guild_id: int) -> BetaLabPackStatus:
    try:
        summary = beta_lab_sessions.load_summary(guild_id)
    except (
        beta_lab_sessions.BetaLabSessionError,
        beta_lab_manifest.BetaLabManifestError,
    ) as exc:
        return BetaLabPackStatus(
            key=SESSION_LANES,
            title='Self-service game lanes',
            state='blocked',
            detail=str(exc),
            action='An operator must reconcile the tracked lane manifest.',
        )
    if summary.ambiguous_game_ids:
        return BetaLabPackStatus(
            key=SESSION_LANES,
            title='Self-service game lanes',
            state='blocked',
            detail=(
                'Damaged ownership markers require review on games '
                + ', '.join(str(value) for value in summary.ambiguous_game_ids)
                + '.'
            ),
            action='Do not create or release lanes until the rows are reviewed.',
        )
    available = summary.capacity - summary.active
    return BetaLabPackStatus(
        key=SESSION_LANES,
        title='Self-service game lanes',
        state='ready',
        detail=(
            f'{available} of {summary.capacity} mutable lanes are available; '
            f'{summary.expired} expired lane(s) will be reclaimed on claim.'
        ),
        action='Testers may claim one private 30-minute result-workflow lane.',
    )


def _persona_status() -> BetaLabPackStatus:
    status = beta_lab_personas.database_status(settings.runtime_profile)
    return BetaLabPackStatus(
        key=GUIDED_PERSONAS,
        title='Guided Team/House persona',
        state='ready' if status.ready else 'blocked',
        detail=status.detail,
        action=(
            'Start a guided session to receive the owned Team and staff roles.'
            if status.ready else
            'An operator must prepare the exact owned persona resources.'
        ),
    )


def load_status(guild_id: int) -> BetaLabStatus:
    guild_id = _validate(guild_id)
    structure = _structure_status(guild_id)
    leaderboard = _leaderboard_status(guild_id)
    try:
        results, snapshot = _result_status(guild_id)
    except (result_workers.BetaFixtureError, dev_fixtures.FixtureValidationError) as exc:
        results = BetaLabPackStatus(
            key=RESULTS,
            title='Game results',
            state='blocked',
            detail=str(exc),
            action='Inspect the exact result-scenario ownership state.',
        )
        snapshot = None
    sessions = _session_status(guild_id)
    personas = _persona_status()
    packs = (structure, leaderboard, results, sessions, personas)
    states = {pack.state for pack in packs}
    if states == {'ready'}:
        overall = 'ready'
    elif 'blocked' in states:
        overall = 'blocked'
    elif states.intersection({'missing', 'refreshable'}):
        overall = 'needs attention'
    else:
        overall = 'unknown'
    return BetaLabStatus(
        guild_id=guild_id,
        overall=overall,
        packs=packs,
        result_snapshot=snapshot,
    )


def with_persona_role_status(
    status: BetaLabStatus,
    *,
    ready: bool,
    detail: str,
) -> BetaLabStatus:
    """Fold live Discord role readiness into the worker-loaded DB pack."""

    packs = tuple(
        (
            BetaLabPackStatus(
                key=pack.key,
                title=pack.title,
                state='ready' if ready and pack.state == 'ready' else 'blocked',
                detail=(
                    f'{pack.detail} {detail}'
                    if ready and pack.state == 'ready'
                    else detail if not ready else pack.detail
                ),
                action=(
                    pack.action
                    if ready and pack.state == 'ready'
                    else 'An operator must prepare the exact owned persona resources.'
                ),
            )
            if pack.key == GUIDED_PERSONAS else pack
        )
        for pack in status.packs
    )
    states = {pack.state for pack in packs}
    if states == {'ready'}:
        overall = 'ready'
    elif 'blocked' in states:
        overall = 'blocked'
    elif states.intersection({'missing', 'refreshable'}):
        overall = 'needs attention'
    else:
        overall = 'unknown'
    return BetaLabStatus(
        guild_id=status.guild_id,
        overall=overall,
        packs=packs,
        result_snapshot=status.result_snapshot,
    )


_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='polybot-beta-lab-read',
)


async def _run(function, *args):
    loop = asyncio.get_running_loop()
    concurrent_future = _executor.submit(functools.partial(function, *args))
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


async def run_status(guild_id: int) -> BetaLabStatus:
    return await _run(load_status, guild_id)


async def _finish_started(task: asyncio.Task):
    """Drain an already-started mutation despite caller cancellation.

    The protected control caller needs the committed IDs more than it needs
    cancellation once the ELO-coordinated write has begun. Repeated task
    cancellation is cleared only for this bounded drain.
    """

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


async def refresh_results(*, guild_id: int, actor: str) -> BetaLabRefreshResult:
    """Refresh the existing result pack through the running ELO coordinator."""

    guild_id = _validate(guild_id)
    requester_id = int(settings.owner_id)
    try:
        preview = await result_workers.run_preview(
            result_workers.BetaFixturePreviewRequest(
                operation=result_workers.RESET,
                guild_id=guild_id,
                requester_id=requester_id,
            )
        )
        commit_task = asyncio.create_task(result_workers.run_commit(
            result_workers.BetaFixtureCommitRequest(
                operation=preview.operation,
                guild_id=guild_id,
                requester_id=requester_id,
                requester_description=str(actor),
                user_ids=preview.user_ids,
                expected_game_ids=preview.snapshot.game_ids,
                expected_fingerprint=preview.snapshot.fingerprint,
            )
        ))
        result = await _finish_started(commit_task)
    except result_workers.BetaFixtureError as exc:
        raise BetaLabError(str(exc)) from exc
    try:
        status = await _finish_started(asyncio.create_task(run_status(guild_id)))
    except Exception:
        return BetaLabRefreshResult(
            pack=RESULTS,
            committed=True,
            old_game_ids=result.old_game_ids,
            new_game_ids=result.new_game_ids,
            status=None,
            warning=(
                'The result pack committed, but the terminal status reload '
                'failed. Do not retry; run Beta Lab status to reconcile.'
            ),
        )
    return BetaLabRefreshResult(
        pack=RESULTS,
        committed=True,
        old_game_ids=result.old_game_ids,
        new_game_ids=result.new_game_ids,
        status=status,
    )
