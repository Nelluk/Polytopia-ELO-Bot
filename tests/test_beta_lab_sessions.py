"""Tier-3 coverage for tester-owned Beta Lab game lanes."""

import asyncio
from contextlib import AbstractContextManager
from dataclasses import FrozenInstanceError
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.test_newgame_worker import import_offline_runtime


sessions = import_offline_runtime('modules.beta_lab_sessions')
manifest_module = import_offline_runtime('modules.beta_lab_manifest')


class FakeDatabase:
    def __init__(self):
        self.connections = 0
        self.commits = 0
        self.rollbacks = 0

    def connection_context(self):
        database = self

        class Context(AbstractContextManager):
            def __enter__(self):
                database.connections += 1

            def __exit__(self, exc_type, exc_value, traceback):
                return False

        return Context()

    def atomic(self):
        database = self

        class Atomic(AbstractContextManager):
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                if exc_type is None:
                    database.commits += 1
                else:
                    database.rollbacks += 1
                return False

        return Atomic()


def manifest(**overrides):
    values = {
        'guild_id': sessions.beta_readiness.BETA_GUILD_ID,
        'tester_role_id': sessions.beta_readiness.BETA_PINNED_TESTER_ROLE_ID,
        'opponent_user_ids': (20, 30),
        'maximum_active_game_lanes': 3,
        'lease_minutes': 30,
    }
    values.update(overrides)
    return manifest_module.BetaLabManifest(**values)


def request(requester_id=10, roles=None):
    if roles is None:
        roles = (sessions.beta_readiness.BETA_PINNED_TESTER_ROLE_ID,)
    return sessions.BetaLabSessionRequest(
        guild_id=sessions.beta_readiness.BETA_GUILD_ID,
        requester_id=requester_id,
        requester_name='Tester',
        role_ids=tuple(roles),
    )


def scenario_game(session_id, scenario, game_id, *, owner=10, opponent=20):
    code = sessions._SCENARIO_CODE[scenario]
    completed = scenario != 'ready'
    confirmed = scenario == 'completed'
    winner = SimpleNamespace(position=1) if completed else None
    lineup = tuple(
        SimpleNamespace(
            player=SimpleNamespace(
                discord_member=SimpleNamespace(discord_id=user_id)
            )
        )
        for user_id in (owner, opponent)
    )
    marker = sessions._Marker(
        session_id=session_id,
        owner_id=owner,
        opponent_id=opponent,
        expires_epoch=2_000,
        scenario=scenario,
    )
    return SimpleNamespace(
        id=game_id,
        guild_id=sessions.beta_readiness.BETA_GUILD_ID,
        name=f'{sessions.NAME_PREFIX}{session_id}-{code}',
        notes=sessions._notes(marker),
        lineup=lineup,
        winner=winner,
        is_pending=False,
        is_completed=completed,
        is_confirmed=confirmed,
        completed_ts=None,
    )


def snapshot(session_id='abcdef123456', *, owner=10, state='ready'):
    return sessions.BetaLabSessionSnapshot(
        session_id=session_id,
        guild_id=sessions.beta_readiness.BETA_GUILD_ID,
        requester_id=owner,
        requester_name='Tester',
        opponent_id=20,
        opponent_name='Fixture Opponent',
        expires_epoch=2_000,
        state=state,
        scenarios=tuple(
            sessions.BetaLabSessionScenario(name, number, 'ready')
            for number, name in enumerate(sessions.SCENARIOS, start=1)
        ),
        fingerprint='fingerprint',
    )


class BetaLabManifestTests(unittest.TestCase):
    def test_manifest_is_exact_and_bounded(self):
        value = {
            'schema_version': 1,
            'guild_id': sessions.beta_readiness.BETA_GUILD_ID,
            'tester_role_id': sessions.beta_readiness.BETA_PINNED_TESTER_ROLE_ID,
            'opponent_user_ids': [20, 30],
            'maximum_active_game_lanes': 3,
            'lease_minutes': 30,
        }
        loaded = manifest_module.validate(value)
        self.assertEqual(loaded.opponent_user_ids, (20, 30))
        with self.assertRaises(manifest_module.BetaLabManifestError):
            manifest_module.validate({**value, 'unexpected': True})
        with self.assertRaises(manifest_module.BetaLabManifestError):
            manifest_module.validate({**value, 'maximum_active_game_lanes': 4})
        with self.assertRaises(manifest_module.BetaLabManifestError):
            manifest_module.validate({**value, 'lease_minutes': 61})


class BetaLabSessionWorkerTests(unittest.TestCase):
    def test_requests_and_publication_snapshots_are_immutable_and_model_free(self):
        item = request()
        result = snapshot()
        with self.assertRaises(FrozenInstanceError):
            item.guild_id = 999
        with self.assertRaises(FrozenInstanceError):
            result.state = 'changed'
        self.assertNotIn('Game:', repr(result))
        self.assertEqual(result.game_ids, (1, 2, 3))

    def test_permission_and_bot_identity_refuse_before_database(self):
        with mock.patch.object(
                sessions,
                '_validate_profile',
                return_value=sessions.beta_readiness.BETA_GUILD_ID,
        ), \
                mock.patch.object(sessions, '_manifest', return_value=manifest()), \
                mock.patch.object(sessions.settings, 'owner_id', 99), \
                mock.patch.object(sessions.settings, 'bot_id', 10), \
                mock.patch.object(sessions.settings, 'bot_id_beta', 11):
            with self.assertRaises(sessions.BetaLabSessionPermissionError):
                sessions._validate_request(request(10))
            with self.assertRaises(sessions.BetaLabSessionPermissionError):
                sessions._validate_request(request(12, roles=()))
            guild_id, _loaded = sessions._validate_request(
                request(12, roles=()),
                require_tester=False,
            )
            self.assertEqual(guild_id, sessions.beta_readiness.BETA_GUILD_ID)

    def test_snapshot_requires_dual_markers_exact_graph_and_names(self):
        session_id = 'abcdef123456'
        records = tuple(
            sessions._record(scenario_game(session_id, name, number))
            for number, name in enumerate(sessions.SCENARIOS, start=1)
        )
        with mock.patch.object(
            sessions,
            '_names',
            return_value={10: 'Human Tester', 20: 'Fixture Friend'},
        ):
            value = sessions._snapshot(session_id, records, now_epoch=1_000)
        self.assertEqual(value.requester_name, 'Human Tester')
        self.assertEqual(value.opponent_name, 'Fixture Friend')
        self.assertEqual(value.game_ids, (1, 2, 3))

        damaged = scenario_game(session_id, 'ready', 1)
        damaged.name = 'ordinary game'
        with self.assertRaises(sessions.BetaLabSessionValidationError):
            sessions._snapshot(
                session_id,
                (sessions._record(damaged), *records[1:]),
                now_epoch=1_000,
            )
        with self.assertRaises(sessions.BetaLabSessionValidationError):
            sessions._snapshot(session_id, records[:2], now_epoch=1_000)

    def test_claim_refuses_capacity_before_loading_players(self):
        database = FakeDatabase()
        active = tuple(
            snapshot(f'abcdef12345{index}', owner=11 + index)
            for index in range(3)
        )
        with mock.patch.object(sessions, '_validate_request', return_value=(1, manifest())), \
                mock.patch.object(sessions.models, 'db', database), \
                mock.patch.object(sessions, '_live_identity'), \
                mock.patch.object(sessions, '_load_all', return_value=({}, active)), \
                mock.patch.object(sessions.dev_fixtures, '_load_players') as load_players:
            with self.assertRaisesRegex(
                sessions.BetaLabSessionValidationError,
                'currently in use',
            ):
                sessions.claim_session(request(), now_epoch=1_000)
        load_players.assert_not_called()
        self.assertEqual(database.rollbacks, 1)

    def test_claim_audit_and_terminal_snapshot_failures_roll_back(self):
        database = FakeDatabase()
        players = (object(), object())
        common = (
            mock.patch.object(sessions, '_validate_request', return_value=(1, manifest())),
            mock.patch.object(sessions.models, 'db', database),
            mock.patch.object(sessions, '_live_identity'),
            mock.patch.object(sessions, '_load_all', return_value=({}, ())),
            mock.patch.object(sessions.dev_fixtures, '_load_players', return_value=players),
            mock.patch.object(sessions, '_create_game'),
        )
        with common[0], common[1], common[2], common[3], common[4], common[5] as create, \
                mock.patch.object(
                    sessions.models.GameLog,
                    'write',
                    side_effect=RuntimeError('audit failed'),
                ):
            with self.assertRaisesRegex(RuntimeError, 'audit failed'):
                sessions.claim_session(
                    request(),
                    now_epoch=1_000,
                    session_id_factory=lambda _size: 'abcdef123456',
                )
        self.assertEqual(create.call_count, 3)
        self.assertEqual(database.rollbacks, 1)

        games = tuple(
            scenario_game('abcdef123456', name, number)
            for number, name in enumerate(sessions.SCENARIOS, start=1)
        )
        with mock.patch.object(sessions, '_validate_request', return_value=(1, manifest())), \
                mock.patch.object(sessions.models, 'db', database), \
                mock.patch.object(sessions, '_live_identity'), \
                mock.patch.object(sessions, '_load_all', return_value=({}, ())), \
                mock.patch.object(sessions.dev_fixtures, '_load_players', return_value=players), \
                mock.patch.object(sessions, '_create_game'), \
                mock.patch.object(sessions.models.GameLog, 'write'), \
                mock.patch.object(sessions, '_find_games', return_value=games), \
                mock.patch.object(
                    sessions,
                    '_snapshot',
                    side_effect=sessions.BetaLabSessionValidationError('snapshot failed'),
                ):
            with self.assertRaisesRegex(
                sessions.BetaLabSessionValidationError,
                'snapshot failed',
            ):
                sessions.claim_session(
                    request(),
                    now_epoch=1_000,
                    session_id_factory=lambda _size: 'abcdef123456',
                )
        self.assertEqual(database.rollbacks, 2)

    def test_release_is_exact_owner_only_and_audited_atomically(self):
        database = FakeDatabase()
        records = (object(), object(), object())
        existing = snapshot(owner=11)
        release = sessions.BetaLabSessionReleaseRequest(
            guild_id=1,
            requester_id=10,
            requester_name='Tester',
            role_ids=(1,),
            session_id=existing.session_id,
            outcome='finished',
        )
        with mock.patch.object(sessions, '_validate_request', return_value=(1, manifest())), \
                mock.patch.object(sessions.models, 'db', database), \
                mock.patch.object(sessions, '_live_identity'), \
                mock.patch.object(
                    sessions,
                    '_load_all',
                    return_value=({existing.session_id: records}, (existing,)),
                ), mock.patch.object(sessions, '_snapshot', return_value=existing), \
                mock.patch.object(sessions, '_delete_records') as delete:
            with self.assertRaises(sessions.BetaLabSessionPermissionError):
                sessions.release_session(release, now_epoch=1_000)
        delete.assert_not_called()

        owned = snapshot(owner=10)
        with mock.patch.object(sessions, '_validate_request', return_value=(1, manifest())), \
                mock.patch.object(sessions.models, 'db', database), \
                mock.patch.object(sessions, '_live_identity'), \
                mock.patch.object(
                    sessions,
                    '_load_all',
                    return_value=({owned.session_id: records}, (owned,)),
                ), mock.patch.object(sessions, '_snapshot', return_value=owned), \
                mock.patch.object(sessions, '_delete_records', return_value=(1, 2, 3)), \
                mock.patch.object(sessions.models.GameLog, 'write') as audit:
            result = sessions.release_session(release, now_epoch=1_000)
        self.assertTrue(result.released)
        self.assertEqual(result.removed_game_ids, (1, 2, 3))
        self.assertTrue(audit.call_args.kwargs['is_protected'])


class BetaLabSessionAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_worker_owns_thread_and_event_loop_stays_responsive(self):
        main_thread = threading.get_ident()
        started = threading.Event()
        release = threading.Event()

        def blocking():
            worker_thread = threading.get_ident()
            started.set()
            release.wait(2)
            return worker_thread

        task = asyncio.create_task(sessions._run_read(blocking))
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.001)
        ticked = False
        await asyncio.sleep(0)
        ticked = True
        release.set()
        await asyncio.sleep(0.01)
        worker_thread = await task
        self.assertTrue(ticked)
        self.assertNotEqual(worker_thread, main_thread)

    async def test_cancelled_read_drains_before_propagating(self):
        started = threading.Event()
        release = threading.Event()

        def blocking():
            started.set()
            release.wait(2)
            return 'done'

        task = asyncio.create_task(sessions._run_read(blocking))
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_mutation_uses_coordinator_and_cancellation_returns_truth(self):
        started = asyncio.Event()
        release = asyncio.Event()
        expected = snapshot()

        async def coordinated(**kwargs):
            self.assertEqual(kwargs['operation'], 'beta_lab_lane_claim')
            self.assertIs(kwargs['worker'], sessions.claim_session)
            started.set()
            await release.wait()
            return expected

        coordinator = SimpleNamespace(run=mock.AsyncMock(side_effect=coordinated))
        with mock.patch.object(sessions.settings, 'elo_job_coordinator', coordinator):
            task = asyncio.create_task(sessions.run_claim_session(request()))
            await started.wait()
            task.cancel()
            release.set()
            result = await task
        self.assertIs(result, expected)


if __name__ == '__main__':
    unittest.main()
