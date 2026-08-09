"""Focused offline coverage for configured vacant-lobby maintenance."""

import asyncio
from contextlib import AbstractContextManager
from dataclasses import FrozenInstanceError
import datetime
import inspect
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.game_lobby_workers')
service = import_offline_runtime('modules.game_lobbies')
matchmaking = import_offline_runtime('modules.matchmaking')

NOW = datetime.datetime(2026, 8, 9, 12, 0, 0)


def lobby_request(*, remake_partial=False, notes='Open to all'):
    return workers.EnsureLobbyRequest(
        guild_id=10,
        size=(2, 2),
        size_display='2v2',
        is_ranked=False,
        remake_partial=remake_partial,
        notes=notes,
        notes_log_display=f'*{notes}*' if notes else '',
        expiration_at=NOW + datetime.timedelta(hours=30),
        role_locks=(
            workers.LobbySideLock(100, 'Red'),
            workers.LobbySideLock(None, None),
        ),
    )


class Database:
    def __init__(self, state):
        self.state = state
        self.connection_opened = 0
        self.connection_closed = 0
        self.commits = 0
        self.rollbacks = 0

    def connection_context(self):
        database = self

        class Connection(AbstractContextManager):
            def __enter__(self):
                database.connection_opened += 1
                return self

            def __exit__(self, *_args):
                database.connection_closed += 1

        return Connection()

    def atomic(self):
        database = self

        class Atomic(AbstractContextManager):
            def __enter__(self):
                self.lengths = {
                    key: len(value) for key, value in database.state.items()
                }
                return self

            def __exit__(self, exc_type, *_args):
                if exc_type is None:
                    database.commits += 1
                    return False
                database.rollbacks += 1
                for key, length in self.lengths.items():
                    del database.state[key][length:]
                return False

        return Atomic()


class LobbyWorkerTests(unittest.IsolatedAsyncioTestCase):
    def test_request_and_nested_role_locks_are_frozen_primitives(self):
        request = lobby_request()
        with self.assertRaises(FrozenInstanceError):
            request.guild_id = 11
        with self.assertRaises(FrozenInstanceError):
            request.role_locks[0].role_name = 'Blue'
        self.assertEqual(request.size, (2, 2))

    def test_remake_partial_preserves_legacy_matching_rule(self):
        empty = SimpleNamespace(
            id=1,
            size_string=lambda: '2v2',
            capacity=lambda: (0, 4),
        )
        partial = SimpleNamespace(
            id=2,
            size_string=lambda: '2v2',
            capacity=lambda: (1, 4),
        )
        with mock.patch.object(
            workers,
            '_candidate_lobbies',
            return_value=(partial,),
        ):
            self.assertIs(partial, workers._find_existing_lobby(
                lobby_request(remake_partial=False)
            ))
            self.assertIsNone(workers._find_existing_lobby(
                lobby_request(remake_partial=True)
            ))
        with mock.patch.object(
            workers,
            '_candidate_lobbies',
            return_value=(partial, empty),
        ):
            self.assertIs(empty, workers._find_existing_lobby(
                lobby_request(remake_partial=True)
            ))

    def _fake_models(self, *, fail_side=False):
        state = {'games': [], 'logs': [], 'sides': []}
        database = Database(state)

        def create_game(**kwargs):
            game = SimpleNamespace(id=88, **kwargs)
            state['games'].append(game)
            return game

        def create_side(**kwargs):
            if fail_side and kwargs['position'] == 2:
                raise RuntimeError('side insert failed')
            state['sides'].append(kwargs)

        models = SimpleNamespace(
            db=database,
            Game=SimpleNamespace(create=create_game),
            GameLog=SimpleNamespace(
                write=lambda **kwargs: state['logs'].append(kwargs),
            ),
            GameSide=SimpleNamespace(create=create_side),
        )
        return models, database, state

    def test_worker_commits_complete_vacant_graph_and_audit(self):
        models, database, state = self._fake_models()
        with mock.patch.object(workers, 'models', models), mock.patch.object(
            workers,
            '_find_existing_lobby',
            return_value=None,
        ):
            result = workers.ensure_configured_lobby(lobby_request())

        self.assertEqual(result.status, workers.CREATED)
        self.assertEqual(result.game_id, 88)
        self.assertEqual(len(state['games']), 1)
        self.assertEqual(len(state['logs']), 1)
        self.assertEqual(
            state['logs'][0]['message'],
            'I created an unranked empty 2v2 lobby. *Open to all*',
        )
        self.assertEqual(len(state['sides']), 2)
        self.assertEqual(state['sides'][0]['required_role_id'], 100)
        self.assertEqual(state['sides'][0]['sidename'], 'Red')
        self.assertEqual(database.commits, 1)
        self.assertEqual(database.rollbacks, 0)
        self.assertEqual(database.connection_opened, 1)
        self.assertEqual(database.connection_closed, 1)

    def test_worker_rolls_back_game_audit_and_sides_together(self):
        models, database, state = self._fake_models(fail_side=True)
        with mock.patch.object(workers, 'models', models), mock.patch.object(
            workers,
            '_find_existing_lobby',
            return_value=None,
        ):
            with self.assertRaisesRegex(RuntimeError, 'side insert failed'):
                workers.ensure_configured_lobby(lobby_request())

        self.assertEqual(state, {'games': [], 'logs': [], 'sides': []})
        self.assertEqual(database.commits, 0)
        self.assertEqual(database.rollbacks, 1)

    def test_existing_lobby_is_typed_noop(self):
        models, database, state = self._fake_models()
        existing = SimpleNamespace(id=77)
        with mock.patch.object(workers, 'models', models), mock.patch.object(
            workers,
            '_find_existing_lobby',
            return_value=existing,
        ):
            result = workers.ensure_configured_lobby(lobby_request())
        self.assertEqual(result.status, workers.EXISTING)
        self.assertEqual(result.game_id, 77)
        self.assertEqual(state, {'games': [], 'logs': [], 'sides': []})
        self.assertEqual(database.commits, 1)

    async def test_runner_uses_pending_game_coordinator(self):
        expected = workers.EnsureLobbyResult(workers.CREATED, 88, 10)
        coordinator = mock.AsyncMock(return_value=expected)
        with mock.patch.object(
            workers.game_open_workers.pending_game_coordinator,
            'run_worker',
            coordinator,
        ):
            result = await workers.run_ensure_configured_lobby(lobby_request())
        self.assertEqual(result, expected)
        coordinator.assert_awaited_once_with(
            workers.ensure_configured_lobby,
            mock.ANY,
        )


class LobbyServiceTests(unittest.IsolatedAsyncioTestCase):
    def test_freeze_resolves_role_names_and_missing_roles_before_worker(self):
        red = SimpleNamespace(id=100, name='Red')
        guild = SimpleNamespace(
            id=10,
            name='Guild',
            get_role=lambda role_id: red if role_id == 100 else None,
        )
        config = {
            'guild': 10,
            'size': [2, 2, 2],
            'size_str': '2v2v2',
            'ranked': True,
            'remake_partial': False,
            'notes': '*Notes*',
            'exp': 48,
            'role_locks': [100, 999, None],
        }
        request = service._freeze_request(config, guild=guild, as_of=NOW)
        self.assertEqual(request.expiration_at, NOW + datetime.timedelta(hours=48))
        self.assertEqual(
            request.role_locks,
            (
                workers.LobbySideLock(100, 'Red'),
                workers.LobbySideLock(None, None),
                workers.LobbySideLock(None, None),
            ),
        )
        self.assertEqual(request.notes_log_display, r'*\*Notes\**')

    async def test_cycle_contains_failure_and_processes_later_definition(self):
        guild = SimpleNamespace(id=10, name='Guild', get_role=lambda _id: None)
        bot = SimpleNamespace(get_guild=lambda _id: guild)
        configs = [
            {
                'guild': 10, 'size': [1, 1], 'size_str': '1v1',
                'ranked': True, 'remake_partial': False, 'notes': 'one',
            },
            {
                'guild': 10, 'size': [1, 1, 1], 'size_str': 'FFA',
                'ranked': False, 'remake_partial': True, 'notes': 'two',
            },
        ]
        success = workers.EnsureLobbyResult(workers.CREATED, 88, 10)
        runner = mock.AsyncMock(side_effect=(RuntimeError('first failed'), success))
        with mock.patch.object(
            service.game_lobby_workers,
            'run_ensure_configured_lobby',
            runner,
        ):
            result = await service.ensure_configured_lobbies(
                bot=bot,
                lobbies=configs,
                as_of=NOW,
            )
        self.assertEqual(result.outcomes, (success,))
        self.assertEqual(result.skipped_indexes, (0,))
        self.assertEqual(runner.await_count, 2)

    async def test_cycle_is_bounded_and_skips_missing_guild(self):
        configs = tuple(
            {
                'guild': index + 1,
                'size': [1, 1],
                'size_str': '1v1',
                'ranked': True,
                'remake_partial': False,
                'notes': '',
            }
            for index in range(service.MAX_CONFIGURED_LOBBIES + 1)
        )
        result = await service.ensure_configured_lobbies(
            bot=SimpleNamespace(get_guild=lambda _id: None),
            lobbies=configs,
            as_of=NOW,
        )
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.skipped_indexes), service.MAX_CONFIGURED_LOBBIES)
        self.assertEqual(result.outcomes, ())

    def test_task_delegates_without_direct_database_access(self):
        source = inspect.getsource(
            matchmaking.matchmaking.task_create_empty_matchmaking_lobbies
        )
        self.assertIn('ensure_configured_lobbies', source)
        self.assertNotIn('models.', source)
        self.assertNotIn('utilities.connect', source)
        self.assertNotIn('db.atomic', source)


if __name__ == '__main__':
    unittest.main()
