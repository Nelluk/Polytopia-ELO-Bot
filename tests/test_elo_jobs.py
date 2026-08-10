"""Offline tests for serialized ELO jobs and the unwin pilot."""

import asyncio
from contextlib import AbstractContextManager
import datetime
import importlib
import inspect
from types import SimpleNamespace
import threading
import unittest
from unittest import mock
import warnings

warnings.filterwarnings(
    'ignore',
    message="'audioop' is deprecated and slated for removal in Python 3.13",
    category=DeprecationWarning,
)

import discord
import peewee
from discord.ext import commands
from peewee import SchemaManager
from playhouse.postgres_ext import PostgresqlExtDatabase

from modules.elo_jobs import EloJob, EloJobConflict, EloJobCoordinator


def import_offline_runtime(module_name):
    """Import a model-dependent module without touching PostgreSQL."""

    with mock.patch.object(
        PostgresqlExtDatabase, 'connect', return_value=True
    ), mock.patch.object(
        PostgresqlExtDatabase, 'close', return_value=True
    ), mock.patch.object(
        PostgresqlExtDatabase, 'create_tables'
    ), mock.patch.object(
        SchemaManager, 'create_foreign_key'
    ):
        return importlib.import_module(module_name)


class EloJobCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.coordinator = EloJobCoordinator()

    async def asyncTearDown(self):
        self.coordinator.shutdown()

    async def test_slow_worker_does_not_block_event_loop_and_conflict_is_prompt(self):
        worker_started = threading.Event()
        worker_release = threading.Event()

        def slow_worker():
            worker_started.set()
            worker_release.wait(timeout=2)
            return 'complete'

        first = asyncio.create_task(
            self.coordinator.run(
                operation='unwin',
                game_id=123,
                requester_id=456,
                requester_name='Offline Tester',
                worker=slow_worker,
            )
        )
        for _ in range(100):
            if worker_started.is_set():
                break
            await asyncio.sleep(0.005)
        self.assertTrue(worker_started.is_set())

        heartbeat = asyncio.Event()

        async def pulse():
            await asyncio.sleep(0.01)
            heartbeat.set()

        await asyncio.wait_for(pulse(), timeout=0.2)
        self.assertTrue(heartbeat.is_set())
        active = self.coordinator.active_job
        self.assertEqual(active.operation, 'unwin')
        self.assertEqual(active.game_id, 123)
        self.assertEqual(active.requester_id, 456)
        self.assertEqual(active.requester_name, 'Offline Tester')
        self.assertIsNotNone(active.started_at.tzinfo)

        started_at = asyncio.get_running_loop().time()
        with self.assertRaises(EloJobConflict):
            await self.coordinator.run(
                operation='recalc_games_from',
                game_id=789,
                requester_id=999,
                requester_name='Second Tester',
                worker=lambda: None,
            )
        self.assertLess(
            asyncio.get_running_loop().time() - started_at,
            0.1,
        )

        worker_release.set()
        # Give the loop one timer wake-up so restricted headless runners that
        # suppress the executor callback's self-pipe wake-up can deliver it.
        await asyncio.sleep(0.05)
        self.assertEqual(await first, 'complete')
        self.assertFalse(self.coordinator.is_active)

    async def test_worker_exception_cleans_up_state_and_callbacks(self):
        callbacks = []

        def fail():
            raise RuntimeError('simulated worker failure')

        with self.assertRaisesRegex(RuntimeError, 'simulated worker failure'):
            await self.coordinator.run(
                operation='unwin',
                game_id=12,
                requester_id=34,
                requester_name='Failure Tester',
                worker=fail,
                before_submit=lambda: callbacks.append('before'),
                after_complete=lambda: callbacks.append('after'),
            )

        self.assertEqual(callbacks, ['before', 'after'])
        self.assertFalse(self.coordinator.is_active)

    async def test_cancelled_caller_keeps_job_reserved_until_worker_finishes(self):
        worker_started = threading.Event()
        worker_release = threading.Event()

        def slow_worker():
            worker_started.set()
            worker_release.wait(timeout=2)

        task = asyncio.create_task(
            self.coordinator.run(
                operation='unwin',
                game_id=55,
                requester_id=66,
                requester_name='Cancellation Tester',
                worker=slow_worker,
            )
        )
        for _ in range(100):
            if worker_started.is_set():
                break
            await asyncio.sleep(0.005)

        task.cancel()
        await asyncio.sleep(0)
        self.assertTrue(self.coordinator.is_active)
        worker_release.set()
        await asyncio.sleep(0.05)
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.coordinator.is_active)

    async def test_repeated_cancellation_keeps_job_reserved_until_worker_finishes(self):
        worker_started = threading.Event()
        worker_release = threading.Event()

        def slow_worker():
            worker_started.set()
            worker_release.wait(timeout=2)

        task = asyncio.create_task(
            self.coordinator.run(
                operation='unwin',
                game_id=77,
                requester_id=88,
                requester_name='Repeated Cancellation Tester',
                worker=slow_worker,
            )
        )
        for _ in range(100):
            if worker_started.is_set():
                break
            await asyncio.sleep(0.005)

        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        self.assertTrue(self.coordinator.is_active)

        with self.assertRaises(EloJobConflict):
            await self.coordinator.run(
                operation='delete_game',
                game_id=99,
                requester_id=100,
                requester_name='Conflicting Tester',
                worker=lambda: None,
            )

        worker_release.set()
        await asyncio.sleep(0.05)
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.coordinator.is_active)


class FakeDatabase:
    def __init__(self, game, logs):
        self.game = game
        self.logs = logs
        self.connection_opened = 0
        self.connection_closed = 0
        self.commits = 0
        self.rollbacks = 0

    def connection_context(self):
        database = self

        class ConnectionContext(AbstractContextManager):
            def __enter__(self):
                database.connection_opened += 1
                return database

            def __exit__(self, exc_type, exc_value, traceback):
                database.connection_closed += 1

        return ConnectionContext()

    def atomic(self):
        database = self

        class AtomicContext(AbstractContextManager):
            def __enter__(self):
                self.snapshot = (
                    database.game.completed_ts,
                    database.game.is_confirmed,
                    database.game.is_completed,
                    database.game.winner,
                    database.game.reversed_count,
                    list(database.logs),
                )

            def __exit__(self, exc_type, exc_value, traceback):
                if exc_type is None:
                    database.commits += 1
                    return False
                database.rollbacks += 1
                (
                    database.game.completed_ts,
                    database.game.is_confirmed,
                    database.game.is_completed,
                    database.game.winner,
                    database.game.reversed_count,
                    logs,
                ) = self.snapshot
                database.logs[:] = logs
                return False

        return AtomicContext()


class FakeGame:
    def __init__(self, *, ranked):
        self.id = 42
        self.guild_id = 100
        self.is_pending = False
        self.is_completed = True
        self.is_confirmed = True
        self.is_ranked = ranked
        self.completed_ts = 'original timestamp'
        self.winner = object()
        self.reversed_count = 0

    def confirmations_reset(self):
        pass

    def reverse_elo_changes(self):
        self.reversed_count += 1

    def save(self):
        pass


class FakeWinSide:
    def __init__(self, side_id, label):
        self.id = side_id
        self.label = label
        self.win_confirmed = False
        self.game = None

    def name(self):
        return self.label

    def save(self):
        pass


class FakeWinGame:
    def __init__(self, *, declare_error=None):
        self.id = 84
        self.guild_id = 100
        self.is_pending = False
        self.is_completed = False
        self.is_confirmed = False
        self.completed_ts = None
        self.win_claimed_ts = None
        self.winner = None
        self.declare_error = declare_error
        self.first_side = FakeWinSide(1, 'Alpha')
        self.second_side = FakeWinSide(2, 'Beta')
        self.gamesides = [self.first_side, self.second_side]
        for side in self.gamesides:
            side.game = self

    def has_player(self, *, discord_id):
        if discord_id == 200:
            return True, self.first_side
        if discord_id == 201:
            return True, self.second_side
        return False, None

    def confirmations_reset(self):
        for side in self.gamesides:
            side.win_confirmed = False
        self.win_claimed_ts = None

    def confirmations_count(self):
        confirmed = sum(side.win_confirmed for side in self.gamesides)
        return confirmed, len(self.gamesides), confirmed == len(self.gamesides)

    def declare_winner(self, *, winning_side, confirm):
        self.winner = winning_side
        self.is_completed = True
        self.is_confirmed = confirm
        if self.declare_error is not None:
            raise self.declare_error

    def save(self):
        pass


class FakeWinDatabase:
    def __init__(self, game, logs):
        self.game = game
        self.logs = logs
        self.connection_opened = 0
        self.connection_closed = 0
        self.commits = 0
        self.rollbacks = 0

    def connection_context(self):
        database = self

        class ConnectionContext(AbstractContextManager):
            def __enter__(self):
                database.connection_opened += 1

            def __exit__(self, exc_type, exc_value, traceback):
                database.connection_closed += 1

        return ConnectionContext()

    def atomic(self):
        database = self

        class AtomicContext(AbstractContextManager):
            def __enter__(self):
                game = database.game
                self.snapshot = (
                    game.is_completed,
                    game.is_confirmed,
                    game.completed_ts,
                    game.win_claimed_ts,
                    game.winner,
                    [side.win_confirmed for side in game.gamesides],
                    list(database.logs),
                )

            def __exit__(self, exc_type, exc_value, traceback):
                if exc_type is None:
                    database.commits += 1
                    return False
                database.rollbacks += 1
                game = database.game
                (
                    game.is_completed,
                    game.is_confirmed,
                    game.completed_ts,
                    game.win_claimed_ts,
                    game.winner,
                    confirmations,
                    logs,
                ) = self.snapshot
                for side, confirmed in zip(
                    game.gamesides, confirmations
                ):
                    side.win_confirmed = confirmed
                database.logs[:] = logs
                return False

        return AtomicContext()


class FakeDeleteGame:
    def __init__(self, *, delete_error=None):
        self.id = 126
        self.guild_id = 100
        self.is_pending = False
        self.winner = object()
        self.is_confirmed = True
        self.is_ranked = True
        self.deleted = False
        self.delete_error = delete_error

    def delete_game(self):
        self.deleted = True
        if self.delete_error is not None:
            raise self.delete_error


class FakeDeleteDatabase:
    def __init__(self, game, logs):
        self.game = game
        self.logs = logs
        self.connection_opened = 0
        self.connection_closed = 0
        self.commits = 0
        self.rollbacks = 0

    def connection_context(self):
        database = self

        class ConnectionContext(AbstractContextManager):
            def __enter__(self):
                database.connection_opened += 1

            def __exit__(self, exc_type, exc_value, traceback):
                database.connection_closed += 1

        return ConnectionContext()

    def atomic(self):
        database = self

        class AtomicContext(AbstractContextManager):
            def __enter__(self):
                self.snapshot = (
                    database.game.deleted,
                    list(database.logs),
                )

            def __exit__(self, exc_type, exc_value, traceback):
                if exc_type is None:
                    database.commits += 1
                    return False
                database.rollbacks += 1
                database.game.deleted, logs = self.snapshot
                database.logs[:] = logs
                return False

        return AtomicContext()


def fake_models(game, database, logs, *, recalculation_error=None):
    class GameTable:
        @staticmethod
        def get_by_id(game_id):
            if game_id != game.id:
                raise peewee.DoesNotExist()
            return game

        @staticmethod
        def recalculate_elo_since(timestamp):
            if recalculation_error is not None:
                raise recalculation_error

    class GameLog:
        @staticmethod
        def write(**kwargs):
            logs.append(kwargs)

    return SimpleNamespace(db=database, Game=GameTable, GameLog=GameLog)


def fake_win_models(game, database, logs):
    class GameTable:
        @staticmethod
        def get_by_id(game_id):
            if game_id != game.id:
                raise peewee.DoesNotExist()
            return game

    class GameLog:
        @staticmethod
        def write(**kwargs):
            logs.append(kwargs)

    return SimpleNamespace(db=database, Game=GameTable, GameLog=GameLog)


def fake_delete_models(game, database, logs):
    class GameTable:
        @staticmethod
        def get_by_id(game_id):
            if game_id != game.id:
                raise peewee.DoesNotExist()
            return game

    class GameLog:
        @staticmethod
        def write(**kwargs):
            logs.append(kwargs)

    return SimpleNamespace(db=database, Game=GameTable, GameLog=GameLog)


class EloWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # models.py creates missing tables at import time. Suppress those
        # import-time database actions for this strictly offline test module.
        cls.workers = import_offline_runtime('modules.elo_workers')
        cls.games = import_offline_runtime('modules.games')

    def test_worker_uses_and_closes_connection_and_commits(self):
        game = FakeGame(ranked=False)
        logs = []
        database = FakeDatabase(game, logs)
        models = fake_models(game, database, logs)

        publication = object()
        with mock.patch.object(self.workers, 'models', models), mock.patch.object(
            self.workers.game_result_publication_workers,
            'build_unwin_publication_snapshot',
            return_value=publication,
        ):
            result = self.workers.unwin_game(
                42, 100, 200, '**Tester** (`200`)', True
            )

        self.assertEqual(result.game_id, 42)
        self.assertTrue(result.post_unwin_messaging)
        self.assertIs(result.publication, publication)
        self.assertEqual(database.connection_opened, 1)
        self.assertEqual(database.connection_closed, 1)
        self.assertEqual(database.commits, 1)
        self.assertEqual(database.rollbacks, 0)
        self.assertFalse(game.is_completed)
        self.assertIsNone(game.winner)

    def test_database_failure_rolls_back_entire_unwin(self):
        game = FakeGame(ranked=True)
        logs = []
        database = FakeDatabase(game, logs)
        models = fake_models(
            game,
            database,
            logs,
            recalculation_error=peewee.OperationalError('slow calc failed'),
        )
        original_state = (
            game.completed_ts,
            game.is_confirmed,
            game.is_completed,
            game.winner,
            game.reversed_count,
        )

        with mock.patch.object(self.workers, 'models', models):
            with self.assertRaises(peewee.OperationalError):
                self.workers.unwin_game(
                    42, 100, 200, '**Tester** (`200`)', True
                )

        self.assertEqual(
            (
                game.completed_ts,
                game.is_confirmed,
                game.is_completed,
                game.winner,
                game.reversed_count,
            ),
            original_state,
        )
        self.assertEqual(logs, [])
        self.assertEqual(database.commits, 0)
        self.assertEqual(database.rollbacks, 1)
        self.assertEqual(database.connection_opened, 1)
        self.assertEqual(database.connection_closed, 1)

    def test_unwin_snapshot_failure_is_post_commit_and_closes_connection(self):
        game = FakeGame(ranked=False)
        logs = []
        database = FakeDatabase(game, logs)
        models = fake_models(game, database, logs)

        with mock.patch.object(self.workers, 'models', models), mock.patch.object(
            self.workers.game_result_publication_workers,
            'build_unwin_publication_snapshot',
            side_effect=peewee.OperationalError('snapshot failed'),
        ):
            with self.assertRaises(self.workers.UnwinSnapshotError) as raised:
                self.workers.unwin_game(
                    42, 100, 200, '**Tester** (`200`)', True
                )

        self.assertEqual(raised.exception.result.game_id, 42)
        self.assertFalse(game.is_completed)
        self.assertIsNone(game.winner)
        self.assertEqual(database.commits, 1)
        self.assertEqual(database.rollbacks, 0)
        self.assertEqual(database.connection_closed, 1)

    def test_worker_interface_accepts_only_primitive_job_inputs(self):
        parameters = inspect.signature(self.workers.unwin_game).parameters
        self.assertEqual(
            list(parameters),
            [
                'game_id',
                'guild_id',
                'requester_id',
                'requester_description',
                'is_staff',
                'publication_context',
            ],
        )
        win_parameters = inspect.signature(
            self.workers.record_win
        ).parameters
        self.assertEqual(
            list(win_parameters),
            [
                'game_id',
                'guild_id',
                'winning_side_id',
                'requester_id',
                'requester_description',
                'is_staff',
                'publication_context',
            ],
        )
        self.assertEqual(
            list(
                inspect.signature(
                    self.workers.confirm_game
                ).parameters
            ),
            [
                'game_id',
                'guild_id',
                'requester_description',
                'publication_context',
                'auto_policy',
            ],
        )
        self.assertEqual(
            list(
                inspect.signature(
                    self.workers.delete_game
                ).parameters
            ),
            ['game_id', 'guild_id', 'requester_description'],
        )
        self.assertEqual(
            list(
                inspect.signature(
                    self.workers.recalculate_games_from
                ).parameters
            ),
            ['game_id'],
        )

    def test_recalculation_worker_owns_connection_and_transaction(self):
        game = FakeGame(ranked=True)
        logs = []
        database = FakeDatabase(game, logs)
        models = fake_models(game, database, logs)

        with mock.patch.object(self.workers, 'models', models):
            timestamp = self.workers.recalculate_games_from(game.id)

        self.assertEqual(timestamp, game.completed_ts)
        self.assertEqual(database.connection_opened, 1)
        self.assertEqual(database.connection_closed, 1)
        self.assertEqual(database.commits, 1)
        self.assertEqual(database.rollbacks, 0)

    def test_recalculation_worker_rolls_back_and_closes_on_failure(self):
        game = FakeGame(ranked=True)
        logs = []
        database = FakeDatabase(game, logs)
        models = fake_models(
            game,
            database,
            logs,
            recalculation_error=peewee.OperationalError(
                'simulated recalculation failure'
            ),
        )

        with mock.patch.object(self.workers, 'models', models):
            with self.assertRaisesRegex(
                peewee.OperationalError,
                'simulated recalculation failure',
            ):
                self.workers.recalculate_games_from(game.id)

        self.assertEqual(database.connection_opened, 1)
        self.assertEqual(database.connection_closed, 1)
        self.assertEqual(database.commits, 0)
        self.assertEqual(database.rollbacks, 1)

    def test_record_win_commits_confirmation_bookkeeping_and_claim(self):
        game = FakeWinGame()
        logs = []
        database = FakeWinDatabase(game, logs)
        models = fake_win_models(game, database, logs)

        publication = object()
        with mock.patch.object(self.workers, 'models', models), mock.patch.object(
            self.workers.game_result_publication_workers,
            'build_win_publication_snapshot',
            return_value=publication,
        ):
            result = self.workers.record_win(
                84, 100, 1, 200, '**Tester** (`200`)', False
            )

        self.assertFalse(result.confirmed)
        self.assertTrue(result.first_claim)
        self.assertTrue(result.new_confirmation)
        self.assertIs(result.publication, publication)
        self.assertEqual(result.confirmed_count, 1)
        self.assertEqual(result.side_count, 2)
        self.assertTrue(game.is_completed)
        self.assertFalse(game.is_confirmed)
        self.assertIs(game.winner, game.first_side)
        self.assertIsNotNone(game.win_claimed_ts)
        self.assertEqual(len(logs), 1)
        self.assertEqual(database.commits, 1)
        self.assertEqual(database.rollbacks, 0)
        self.assertEqual(database.connection_opened, 1)
        self.assertEqual(database.connection_closed, 1)

    def test_record_win_rolls_back_flags_and_log_on_finalization_failure(self):
        game = FakeWinGame(
            declare_error=peewee.OperationalError('ELO failure')
        )
        game.first_side.win_confirmed = True
        logs = []
        database = FakeWinDatabase(game, logs)
        models = fake_win_models(game, database, logs)

        with mock.patch.object(self.workers, 'models', models):
            with self.assertRaises(peewee.OperationalError):
                self.workers.record_win(
                    84, 100, 1, 201, '**Opponent** (`201`)', False
                )

        self.assertFalse(game.is_completed)
        self.assertFalse(game.is_confirmed)
        self.assertIsNone(game.winner)
        self.assertTrue(game.first_side.win_confirmed)
        self.assertFalse(game.second_side.win_confirmed)
        self.assertEqual(logs, [])
        self.assertEqual(database.commits, 0)
        self.assertEqual(database.rollbacks, 1)
        self.assertEqual(database.connection_closed, 1)

    def test_record_win_confirm_audits_before_post_commit_snapshot(self):
        game = FakeWinGame()
        game.first_side.win_confirmed = True
        logs = []
        database = FakeWinDatabase(game, logs)
        models = fake_win_models(game, database, logs)
        publication = object()
        snapshot_events = []

        def build_snapshot(*_args, **_kwargs):
            snapshot_events.append((database.commits, len(logs)))
            return publication

        with mock.patch.object(self.workers, 'models', models), mock.patch.object(
            self.workers.game_result_publication_workers,
            'build_win_publication_snapshot',
            side_effect=build_snapshot,
        ):
            result = self.workers.record_win(
                84, 100, 1, 201, '**Opponent** (`201`)', False
            )

        self.assertTrue(result.confirmed)
        self.assertIs(result.publication, publication)
        self.assertEqual(database.commits, 1)
        self.assertEqual(len(logs), 2)
        self.assertIn('Win confirm logged', logs[0]['message'])
        self.assertEqual(
            logs[1]['message'],
            'Win is confirmed and ELO changes processed.',
        )
        self.assertEqual(snapshot_events, [(1, 2)])

    def test_record_win_confirm_audit_failure_rolls_back_before_snapshot(self):
        game = FakeWinGame()
        game.first_side.win_confirmed = True
        logs = []
        database = FakeWinDatabase(game, logs)
        models = fake_win_models(game, database, logs)
        write_count = 0

        def fail_second_audit(**kwargs):
            nonlocal write_count
            write_count += 1
            if write_count == 2:
                raise peewee.OperationalError('confirmed audit failed')
            logs.append(kwargs)

        models.GameLog.write = fail_second_audit
        snapshot_builder = mock.Mock()
        with mock.patch.object(self.workers, 'models', models), mock.patch.object(
            self.workers.game_result_publication_workers,
            'build_win_publication_snapshot',
            new=snapshot_builder,
        ):
            with self.assertRaisesRegex(
                peewee.OperationalError,
                'confirmed audit failed',
            ):
                self.workers.record_win(
                    84, 100, 1, 201, '**Opponent** (`201`)', False
                )

        self.assertFalse(game.is_completed)
        self.assertFalse(game.is_confirmed)
        self.assertIsNone(game.winner)
        self.assertEqual(logs, [])
        self.assertEqual(database.commits, 0)
        self.assertEqual(database.rollbacks, 1)
        self.assertEqual(database.connection_closed, 1)
        snapshot_builder.assert_not_called()

    def test_record_win_snapshot_failure_is_post_commit(self):
        game = FakeWinGame()
        logs = []
        database = FakeWinDatabase(game, logs)
        models = fake_win_models(game, database, logs)

        with mock.patch.object(self.workers, 'models', models), mock.patch.object(
            self.workers.game_result_publication_workers,
            'build_win_publication_snapshot',
            side_effect=peewee.OperationalError('snapshot failed'),
        ):
            with self.assertRaises(self.workers.WinSnapshotError) as raised:
                self.workers.record_win(
                    84, 100, 1, 200, '**Tester** (`200`)', False
                )

        self.assertEqual(raised.exception.result.game_id, 84)
        self.assertTrue(game.is_completed)
        self.assertFalse(game.is_confirmed)
        self.assertIs(game.winner, game.first_side)
        self.assertEqual(len(logs), 1)
        self.assertEqual(database.commits, 1)
        self.assertEqual(database.rollbacks, 0)
        self.assertEqual(database.connection_closed, 1)

    def test_confirm_game_uses_worker_transaction(self):
        game = FakeWinGame()
        game.is_completed = True
        game.winner = game.first_side
        logs = []
        database = FakeWinDatabase(game, logs)
        models = fake_win_models(game, database, logs)

        publication = object()
        with mock.patch.object(
            self.workers,
            'models',
            models,
        ), mock.patch.object(
            self.workers.confirmation_publication_workers,
            'build_confirmation_publication_snapshot',
            return_value=publication,
        ):
            result = self.workers.confirm_game(
                84,
                100,
                '**Staff Tester** (`300`)',
            )

        self.assertEqual(result.game_id, 84)
        self.assertEqual(result.winner_name, 'Alpha')
        self.assertIs(result.publication, publication)
        self.assertTrue(game.is_confirmed)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]['game_id'], 84)
        self.assertEqual(logs[0]['guild_id'], 100)
        self.assertIn('**Staff Tester** (`300`)', logs[0]['message'])
        self.assertIn('**Alpha**', logs[0]['message'])
        self.assertEqual(database.commits, 1)
        self.assertEqual(database.connection_closed, 1)

    def test_confirm_game_rolls_back_when_transactional_audit_fails(self):
        game = FakeWinGame()
        game.is_completed = True
        game.winner = game.first_side
        logs = []
        database = FakeWinDatabase(game, logs)
        models = fake_win_models(game, database, logs)
        models.GameLog.write = mock.Mock(
            side_effect=peewee.OperationalError('audit write failed')
        )

        with mock.patch.object(self.workers, 'models', models):
            with self.assertRaisesRegex(
                peewee.OperationalError,
                'audit write failed',
            ):
                self.workers.confirm_game(
                    84,
                    100,
                    '**Staff Tester** (`300`)',
                )

        self.assertTrue(game.is_completed)
        self.assertFalse(game.is_confirmed)
        self.assertIs(game.winner, game.first_side)
        self.assertEqual(logs, [])
        self.assertEqual(database.commits, 0)
        self.assertEqual(database.rollbacks, 1)
        self.assertEqual(database.connection_closed, 1)

    def test_auto_confirm_revalidates_stale_candidate_before_mutation(self):
        game = FakeWinGame()
        game.is_completed = True
        game.is_ranked = True
        game.win_claimed_ts = datetime.datetime(2026, 8, 10, 11, 0)
        game.winner = game.first_side
        logs = []
        database = FakeWinDatabase(game, logs)
        models = fake_win_models(game, database, logs)
        policy = self.workers.auto_confirmation_workers.AutoConfirmationPolicy(
            as_of=datetime.datetime(2026, 8, 10, 12, 0),
        )

        with mock.patch.object(self.workers, 'models', models):
            with self.assertRaises(
                self.workers.AutoConfirmationIneligible
            ):
                self.workers.confirm_game(
                    84,
                    100,
                    'Automatic confirmation task',
                    auto_policy=policy,
                )

        self.assertFalse(game.is_confirmed)
        self.assertEqual(logs, [])
        self.assertEqual(database.commits, 0)
        self.assertEqual(database.rollbacks, 1)
        self.assertEqual(database.connection_closed, 1)

    def test_auto_confirm_returns_authoritative_confirmation_evidence(self):
        game = FakeWinGame()
        game.is_completed = True
        game.is_ranked = False
        game.win_claimed_ts = datetime.datetime(2026, 8, 10, 11, 30)
        game.winner = game.first_side
        game.first_side.win_confirmed = True
        game.second_side.win_confirmed = True
        logs = []
        database = FakeWinDatabase(game, logs)
        models = fake_win_models(game, database, logs)
        policy = self.workers.auto_confirmation_workers.AutoConfirmationPolicy(
            as_of=datetime.datetime(2026, 8, 10, 12, 0),
        )

        with mock.patch.object(
            self.workers,
            'models',
            models,
        ), mock.patch.object(
            self.workers.confirmation_publication_workers,
            'build_confirmation_publication_snapshot',
            return_value=object(),
        ):
            result = self.workers.confirm_game(
                84,
                100,
                'Automatic confirmation task',
                auto_policy=policy,
            )

        self.assertTrue(game.is_confirmed)
        self.assertEqual(
            result.auto_confirmation,
            self.workers.auto_confirmation_workers.AutoConfirmationEvidence(
                reason='Due to partial confirmations.',
                confirmed_count=2,
                side_count=2,
            ),
        )
        self.assertEqual(database.commits, 1)

    def test_delete_game_commits_in_worker_connection(self):
        game = FakeDeleteGame()
        logs = []
        database = FakeDeleteDatabase(game, logs)
        models = fake_delete_models(game, database, logs)

        with mock.patch.object(self.workers, 'models', models):
            result = self.workers.delete_game(
                126, 100, '**Moderator** (`300`)',
            )

        self.assertEqual(result.game_id, 126)
        self.assertTrue(result.recalculated)
        self.assertTrue(game.deleted)
        self.assertEqual(len(logs), 1)
        self.assertEqual(database.commits, 1)
        self.assertEqual(database.rollbacks, 0)
        self.assertEqual(database.connection_opened, 1)
        self.assertEqual(database.connection_closed, 1)

    def test_delete_game_rolls_back_log_and_deletion_on_failure(self):
        game = FakeDeleteGame(
            delete_error=peewee.OperationalError('recalculation failed')
        )
        logs = []
        database = FakeDeleteDatabase(game, logs)
        models = fake_delete_models(game, database, logs)

        with mock.patch.object(self.workers, 'models', models):
            with self.assertRaises(peewee.OperationalError):
                self.workers.delete_game(
                    126, 100, '**Moderator** (`300`)',
                )

        self.assertFalse(game.deleted)
        self.assertEqual(logs, [])
        self.assertEqual(database.commits, 0)
        self.assertEqual(database.rollbacks, 1)
        self.assertEqual(database.connection_closed, 1)


class ResultSnapshotCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.workers = import_offline_runtime('modules.elo_workers')

    async def asyncSetUp(self):
        self.coordinator = EloJobCoordinator()

    async def asyncTearDown(self):
        self.coordinator.shutdown()

    async def _wait_for_thread_event(self, event):
        for _ in range(100):
            if event.is_set():
                return
            await asyncio.sleep(0.005)
        self.fail('worker snapshot did not start')

    async def test_snapshot_load_stays_off_loop_and_owns_connection(self):
        game = FakeGame(ranked=False)
        logs = []
        database = FakeDatabase(game, logs)
        models = fake_models(game, database, logs)
        snapshot_started = threading.Event()
        snapshot_release = threading.Event()
        publication = object()

        def block_snapshot(*_args, **_kwargs):
            snapshot_started.set()
            snapshot_release.wait(timeout=2)
            return publication

        with mock.patch.object(self.workers, 'models', models), mock.patch.object(
            self.workers.game_result_publication_workers,
            'build_unwin_publication_snapshot',
            side_effect=block_snapshot,
        ):
            task = asyncio.create_task(self.coordinator.run(
                operation='unwin',
                game_id=42,
                requester_id=200,
                requester_name='Tester',
                worker=self.workers.unwin_game,
                worker_args=(42, 100, 200, '**Tester** (`200`)', True),
            ))
            await self._wait_for_thread_event(snapshot_started)
            self.assertEqual(database.connection_opened, 1)
            self.assertEqual(database.connection_closed, 0)

            heartbeat = asyncio.Event()
            asyncio.get_running_loop().call_later(0.01, heartbeat.set)
            await asyncio.wait_for(heartbeat.wait(), timeout=0.2)
            self.assertTrue(self.coordinator.is_active)

            snapshot_release.set()
            await asyncio.sleep(0.05)
            result = await task

        self.assertIs(result.publication, publication)
        self.assertEqual(database.connection_closed, 1)
        self.assertFalse(self.coordinator.is_active)

    async def test_cancellation_drains_snapshot_and_connection_before_release(self):
        game = FakeGame(ranked=False)
        logs = []
        database = FakeDatabase(game, logs)
        models = fake_models(game, database, logs)
        snapshot_started = threading.Event()
        snapshot_release = threading.Event()

        def block_snapshot(*_args, **_kwargs):
            snapshot_started.set()
            snapshot_release.wait(timeout=2)
            return object()

        with mock.patch.object(self.workers, 'models', models), mock.patch.object(
            self.workers.game_result_publication_workers,
            'build_unwin_publication_snapshot',
            side_effect=block_snapshot,
        ):
            task = asyncio.create_task(self.coordinator.run(
                operation='unwin',
                game_id=42,
                requester_id=200,
                requester_name='Tester',
                worker=self.workers.unwin_game,
                worker_args=(42, 100, 200, '**Tester** (`200`)', True),
            ))
            await self._wait_for_thread_event(snapshot_started)
            task.cancel()
            await asyncio.sleep(0)
            self.assertTrue(self.coordinator.is_active)
            self.assertEqual(database.connection_closed, 0)

            snapshot_release.set()
            await asyncio.sleep(0.05)
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(database.connection_closed, 1)
        self.assertFalse(self.coordinator.is_active)


class HybridUnwinCommandTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.games = import_offline_runtime('modules.games')
        cls.administration = import_offline_runtime(
            'modules.administration'
        )

    def game_app_command(self, name):
        game_group = next(
            command
            for command in self.games.polygames.__cog_app_commands__
            if command.name == 'game'
        )
        return game_group.get_command(name)

    def game_nested_app_command(self, group_name, command_name):
        return self.game_app_command(group_name).get_command(command_name)

    def elo_app_command(self, name):
        elo_group = next(
            command
            for command
            in self.administration.administration.__cog_app_commands__
            if command.name == 'elo'
        )
        return elo_group.get_command(name)

    def test_unwin_is_registered_for_prefix_and_slash(self):
        matches = [
            command for command in self.games.polygames.__cog_commands__
            if command.name == 'unwin'
        ]
        self.assertEqual(len(matches), 1)
        command = matches[0]
        self.assertIsInstance(command, commands.Command)
        self.assertNotIsInstance(command, commands.HybridCommand)
        self.assertEqual(command.name, 'unwin')
        app_command = self.game_nested_app_command('result', 'undo')
        self.assertIsNotNone(app_command)
        self.assertEqual(app_command.name, 'undo')
        self.assertEqual(
            app_command.parameters[0].type,
            discord.AppCommandOptionType.integer,
        )

    def test_delete_is_registered_for_prefix_aliases_and_slash(self):
        command = next(
            command for command in self.games.polygames.__cog_commands__
            if command.name == 'delete'
        )
        self.assertIsInstance(command, commands.Command)
        self.assertNotIsInstance(command, commands.HybridCommand)
        self.assertEqual(
            {alias for alias in command.aliases},
            {'delete_game', 'delgame', 'delmatch', 'deletegame'},
        )
        app_command = self.game_nested_app_command('manage', 'delete')
        self.assertIsNotNone(app_command)
        self.assertEqual(
            app_command.parameters[0].type,
            discord.AppCommandOptionType.integer,
        )

    def test_win_is_registered_for_prefix_alias_and_slash(self):
        command = next(
            command for command in self.games.polygames.__cog_commands__
            if command.name == 'win'
        )
        self.assertIsInstance(command, commands.Command)
        self.assertNotIsInstance(command, commands.HybridCommand)
        self.assertEqual(command.aliases, ['lose'])
        app_command = self.game_app_command('win')
        self.assertIsNotNone(app_command)
        self.assertEqual(
            [
                (parameter.name, parameter.type)
                for parameter in app_command.parameters
            ],
            [
                ('game_id', discord.AppCommandOptionType.integer),
                ('winner', discord.AppCommandOptionType.string),
            ],
        )

    def test_confirm_and_search_unconfirmed_slash_commands_are_registered(self):
        confirm = self.game_nested_app_command('result', 'confirm')
        search = self.game_app_command('search')
        self.assertEqual(
            [
                (parameter.name, parameter.type)
                for parameter in confirm.parameters
            ],
            [('game_id', discord.AppCommandOptionType.integer)],
        )
        self.assertIsNone(self.game_app_command('unconfirmed'))
        self.assertEqual(
            [parameter.name for parameter in search.parameters],
            ['query', 'view'],
        )
        self.assertIn(
            'unconfirmed',
            [choice.value for choice in search.parameters[1].choices],
        )

    def test_recalculation_prefix_and_maintenance_slash_commands_registered(
        self,
    ):
        prefix_names = {
            command.name
            for command
            in self.administration.administration.__cog_commands__
        }
        self.assertNotIn('reverse_duplicated_elo', prefix_names)
        prefix_command = next(
            command
            for command
            in self.administration.administration.__cog_commands__
            if command.name == 'recalc_games_from'
        )
        self.assertIsInstance(prefix_command, commands.Command)
        self.assertTrue(prefix_command.hidden)
        self.assertTrue(
            any(
                check.__qualname__ == 'is_owner.<locals>.predicate'
                for check in prefix_command.checks
            )
        )

        recalculate = self.elo_app_command('recalculate')
        status = self.elo_app_command('status')
        self.assertEqual(
            [
                (parameter.name, parameter.type, parameter.required)
                for parameter
                in recalculate.parameters
            ],
            [
                ('game_id', discord.AppCommandOptionType.integer, True),
                ('confirm', discord.AppCommandOptionType.boolean, True),
            ],
        )
        self.assertEqual(status.parameters, [])

    async def test_recalculation_slash_rejects_non_owner_before_defer(self):
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=400),
            response=SimpleNamespace(
                send_message=mock.AsyncMock(),
                defer=mock.AsyncMock(),
            ),
        )
        command = self.elo_app_command('recalculate')
        cog = SimpleNamespace(
            _run_recalculation_job=mock.AsyncMock(),
        )

        with mock.patch.object(
            self.administration.settings,
            'owner_id',
            999,
        ):
            await command.callback(cog, interaction, 42, True)

        interaction.response.send_message.assert_awaited_once_with(
            'Only the bot owner can use this command.',
            ephemeral=True,
        )
        interaction.response.defer.assert_not_awaited()
        cog._run_recalculation_job.assert_not_awaited()

    async def test_recalculation_slash_requires_confirmation_before_defer(self):
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=999),
            response=SimpleNamespace(
                send_message=mock.AsyncMock(),
                defer=mock.AsyncMock(),
            ),
        )
        command = self.elo_app_command('recalculate')
        cog = SimpleNamespace(
            _run_recalculation_job=mock.AsyncMock(),
        )

        with mock.patch.object(
            self.administration.settings,
            'owner_id',
            999,
        ):
            await command.callback(cog, interaction, 42, False)

        interaction.response.send_message.assert_awaited_once()
        interaction.response.defer.assert_not_awaited()
        cog._run_recalculation_job.assert_not_awaited()

    async def test_confirmed_recalculation_defers_before_job_submission(self):
        events = []
        timestamp = datetime.datetime(
            2026,
            7,
            29,
            12,
            0,
            tzinfo=datetime.timezone.utc,
        )

        async def run_job(**kwargs):
            events.append('run')
            self.assertEqual(events[0], 'defer')
            self.assertEqual(kwargs['game_id'], 42)
            return timestamp

        interaction = SimpleNamespace(
            user=SimpleNamespace(
                id=999,
                display_name='Owner Tester',
            ),
            response=SimpleNamespace(
                send_message=mock.AsyncMock(),
                defer=mock.AsyncMock(
                    side_effect=lambda **kwargs: events.append('defer')
                ),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        command = self.elo_app_command('recalculate')
        cog = SimpleNamespace(_run_recalculation_job=run_job)

        with mock.patch.object(
            self.administration.settings,
            'owner_id',
            999,
        ):
            await command.callback(cog, interaction, 42, True)

        self.assertEqual(events, ['defer', 'run'])
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.followup.send.assert_awaited_once_with(
            f'DB has been refreshed from {timestamp} onward.',
            ephemeral=True,
        )

    async def test_recalculation_slash_reports_conflict_and_validation(self):
        active_job = EloJob(
            operation='unwin',
            game_id=81,
            requester_id=12,
            requester_name='Staff Tester',
            started_at=datetime.datetime.now(datetime.timezone.utc),
        )
        command = self.elo_app_command('recalculate')

        for exception, expected in (
            (EloJobConflict(active_job), 'already running'),
            (
                self.administration.elo_workers.RecalculationValidationError(
                    'not completed'
                ),
                'not completed',
            ),
        ):
            with self.subTest(exception=exception):
                interaction = SimpleNamespace(
                    user=SimpleNamespace(
                        id=999,
                        display_name='Owner Tester',
                    ),
                    response=SimpleNamespace(
                        send_message=mock.AsyncMock(),
                        defer=mock.AsyncMock(),
                    ),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                )
                cog = SimpleNamespace(
                    _run_recalculation_job=mock.AsyncMock(
                        side_effect=exception
                    ),
                )
                with mock.patch.object(
                    self.administration.settings,
                    'owner_id',
                    999,
                ):
                    await command.callback(cog, interaction, 42, True)

                interaction.response.defer.assert_awaited_once_with(
                    ephemeral=True
                )
                self.assertIn(
                    expected,
                    interaction.followup.send.await_args.args[0],
                )

    def test_elo_job_status_formats_idle_and_active_state(self):
        self.assertEqual(
            self.administration.format_elo_job_status(None),
            'No ELO mutation job is currently running.',
        )
        started_at = datetime.datetime(
            2026,
            7,
            29,
            12,
            0,
            tzinfo=datetime.timezone.utc,
        )
        active_job = EloJob(
            operation='recalc_games_from',
            game_id=42,
            requester_id=999,
            requester_name='Owner Tester',
            started_at=started_at,
        )
        message = self.administration.format_elo_job_status(
            active_job,
            now=started_at + datetime.timedelta(seconds=3723),
        )

        self.assertIn('`recalc_games_from`', message)
        self.assertIn('Game: `42`', message)
        self.assertIn('Owner Tester (`999`)', message)
        self.assertIn(f'<t:{int(started_at.timestamp())}:F>', message)
        self.assertIn('Elapsed: 1h 2m 3s', message)

    async def test_elo_job_status_is_staff_only_and_ephemeral(self):
        command = self.elo_app_command('status')
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=400),
            response=SimpleNamespace(send_message=mock.AsyncMock()),
        )

        with mock.patch.object(
            self.administration.settings,
            'is_staff',
            return_value=False,
        ):
            await command.callback(SimpleNamespace(), interaction)
        interaction.response.send_message.assert_awaited_once_with(
            'You do not have permission to use this command.',
            ephemeral=True,
        )

        interaction.response.send_message.reset_mock()
        with mock.patch.object(
            self.administration.settings,
            'is_staff',
            return_value=True,
        ), mock.patch.object(
            self.administration.settings,
            'elo_job_coordinator',
            SimpleNamespace(active_job=None),
        ):
            await command.callback(SimpleNamespace(), interaction)
        interaction.response.send_message.assert_awaited_once_with(
            'No ELO mutation job is currently running.',
            ephemeral=True,
        )

    async def test_slash_context_is_deferred_before_worker_submission(self):
        events = []
        result = self.games.elo_workers.UnwinResult(
            game_id=42,
            message='complete',
            post_unwin_messaging=False,
            previously_confirmed=False,
        )

        class Coordinator:
            active_job = None

            async def run(self, **kwargs):
                events.append('run')
                self_test.assertEqual(events[0], 'defer')
                return result

        class Typing:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_value, traceback):
                return False

        class Context:
            interaction = object()
            author = SimpleNamespace(
                id=200,
                display_name='Tester',
                mention='<@200>',
            )
            guild = SimpleNamespace(id=100)
            prefix = '$'
            channel = SimpleNamespace()

            async def defer(self):
                events.append('defer')

            def typing(self):
                return Typing()

            async def send(self, message):
                events.append(('send', message))

        self_test = self
        context = Context()
        command = next(
            command for command in self.games.polygames.__cog_commands__
            if command.name == 'unwin'
        )
        with mock.patch.object(
            self.games.settings, 'elo_job_coordinator', Coordinator()
        ), mock.patch.object(
            self.games.settings, 'is_staff', return_value=True
        ), mock.patch.object(
            self.games.models.GameLog,
            'member_string',
            return_value='**Tester** (`200`)',
        ):
            await command.callback(SimpleNamespace(), context, 42)

        self.assertEqual(events[0], 'defer')
        self.assertIn(('send', 'complete'), events)

    async def test_database_failure_does_not_run_post_commit_discord_effects(self):
        class Coordinator:
            active_job = None

            async def run(self, **kwargs):
                raise peewee.OperationalError('simulated failure')

        class Typing:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_value, traceback):
                return False

        messages = []
        context = SimpleNamespace(
            interaction=None,
            author=SimpleNamespace(
                id=200,
                display_name='Tester',
                mention='<@200>',
            ),
            guild=SimpleNamespace(id=100),
            prefix='$',
            channel=SimpleNamespace(),
            typing=lambda: Typing(),
            send=mock.AsyncMock(side_effect=lambda message: messages.append(message)),
        )
        command = next(
            command for command in self.games.polygames.__cog_commands__
            if command.name == 'unwin'
        )
        with mock.patch.object(
            self.games.settings, 'elo_job_coordinator', Coordinator()
        ), mock.patch.object(
            self.games.settings, 'is_staff', return_value=True
        ), mock.patch.object(
            self.games.models.GameLog,
            'member_string',
            return_value='**Tester** (`200`)',
        ), mock.patch.object(
            self.games, 'post_unwin_messaging', new=mock.AsyncMock()
        ) as post_effects, mock.patch.object(
            self.games.logger, 'exception'
        ):
            await command.callback(SimpleNamespace(), context, 42)

        post_effects.assert_not_awaited()
        self.assertTrue(
            any('No Discord channel updates were made' in message
                for message in messages)
        )

    async def test_win_database_failure_has_no_discord_channel_effects(self):
        events = []

        class Coordinator:
            is_active = False

            async def run(self, **kwargs):
                events.append('run')
                self_test.assertEqual(events[0], 'defer')
                raise peewee.OperationalError('simulated win failure')

        class Typing:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_value, traceback):
                return False

        winning_side = SimpleNamespace(id=9)
        winning_game = SimpleNamespace(
            id=88,
            guild_id=100,
            is_pending=False,
            gameside_by_name=lambda name: (
                SimpleNamespace(name='Alpha'),
                winning_side,
            ),
            update_squad_channels=mock.AsyncMock(),
        )
        messages = []
        self_test = self
        context = SimpleNamespace(
            interaction=object(),
            author=SimpleNamespace(
                id=200,
                display_name='Tester',
                mention='<@200>',
            ),
            guild=SimpleNamespace(id=100),
            prefix='$',
            channel=SimpleNamespace(),
            invoked_with='win',
            defer=mock.AsyncMock(
                side_effect=lambda: events.append('defer')
            ),
            typing=lambda: Typing(),
            send=mock.AsyncMock(
                side_effect=lambda message: messages.append(message)
            ),
        )
        command = next(
            command for command in self.games.polygames.__cog_commands__
            if command.name == 'win'
        )

        with mock.patch.object(
            self.games.game_win.game_win_workers,
            'run_prepare_win',
            new=mock.AsyncMock(
                return_value=SimpleNamespace(winning_side_id=9),
            ),
        ), mock.patch.object(
            self.games.settings, 'elo_job_coordinator', Coordinator()
        ), mock.patch.object(
            self.games.settings, 'is_staff', return_value=False
        ), mock.patch.object(
            self.games.models.GameLog,
            'member_string',
            return_value='**Tester** (`200`)',
        ), mock.patch.object(
            self.games.utilities, 'lock_game'
        ), mock.patch.object(
            self.games.utilities, 'unlock_game'
        ), mock.patch.object(
            self.games, 'post_win_messaging', new=mock.AsyncMock()
        ) as post_effects, mock.patch.object(
            self.games.logger, 'exception'
        ):
            await command.callback(
                SimpleNamespace(),
                context,
                88,
                winner='Alpha',
            )

        winning_game.update_squad_channels.assert_not_awaited()
        post_effects.assert_not_awaited()
        self.assertEqual(events[:2], ['defer', 'run'])
        self.assertTrue(
            any('No Discord channel updates were made' in message
                for message in messages)
        )

    async def test_manual_confirm_failure_has_no_post_commit_effects(self):
        class Typing:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_value, traceback):
                return False

        winning_side = SimpleNamespace(name=lambda: 'Alpha')
        winning_game = SimpleNamespace(
            id=99,
            is_completed=True,
            is_confirmed=False,
            winner=winning_side,
        )
        messages = []
        context = SimpleNamespace(
            author=SimpleNamespace(
                id=300,
                display_name='Staff Tester',
                mention='<@300>',
            ),
            guild=SimpleNamespace(id=100),
            prefix='$',
            channel=SimpleNamespace(),
            typing=lambda: Typing(),
            send=mock.AsyncMock(
                side_effect=lambda message: messages.append(message)
            ),
        )
        command = next(
            command
            for command in self.administration.administration.__cog_commands__
            if command.name == 'confirm'
        )
        cog = SimpleNamespace(
            _confirm_game_and_post=mock.AsyncMock(
                side_effect=peewee.OperationalError(
                    'simulated confirmation failure'
                )
            )
        )

        with mock.patch.object(
            self.administration.PolyGame,
            'convert',
            new=mock.AsyncMock(return_value=winning_game),
        ), mock.patch.object(
            self.administration.confirmation_publication,
            'publish_confirmed_game',
            new=mock.AsyncMock(),
        ) as post_effects, mock.patch.object(
            self.administration.logger, 'exception'
        ):
            await command.callback(cog, context, arg='99')

        post_effects.assert_not_awaited()
        self.assertTrue(
            any('No Discord channel updates were made' in message
                for message in messages)
        )

    async def test_confirm_publication_skips_duplicate_post_commit_audit(self):
        result = self.administration.elo_workers.ConfirmedWinResult(
            game_id=99,
            winner_name='Alpha',
            publication=object(),
        )
        requester = SimpleNamespace(
            id=300,
            display_name='Staff Tester',
        )
        guild = SimpleNamespace(id=100)
        channel = SimpleNamespace()
        cog = object.__new__(self.administration.administration)
        cog._run_confirm_game_job = mock.AsyncMock(return_value=result)

        with mock.patch.object(
            self.administration.models.GameLog,
            'member_string',
            return_value='**Staff Tester** (`300`)',
        ), mock.patch.object(
            self.administration.confirmation_publication,
            'publish_confirmed_game',
            new=mock.AsyncMock(),
        ) as post_effects:
            returned = await (
                self.administration.administration
                ._confirm_game_and_post(
                    cog,
                    game_id=99,
                    guild=guild,
                    prefix='$',
                    channel=channel,
                    requester=requester,
                )
            )

        self.assertEqual(returned, result)
        cog._run_confirm_game_job.assert_awaited_once_with(
            game_id=99,
            guild_id=100,
            requester_id=300,
            requester_name='Staff Tester',
            requester_description='**Staff Tester** (`300`)',
        )
        post_effects.assert_awaited_once_with(
            guild=guild,
            prefix='$',
            current_channel=channel,
            snapshot=result.publication,
            bot=self.administration.settings.bot,
        )

    async def test_confirm_publication_failure_after_first_effect_is_reconcile(
        self,
    ):
        result = self.administration.elo_workers.ConfirmedWinResult(
            game_id=99,
            winner_name='Alpha',
            publication=object(),
        )
        effects = []

        async def partially_publish(*_args, **kwargs):
            self.assertIs(kwargs['snapshot'], result.publication)
            effects.append('first Discord effect')
            raise RuntimeError('later Discord effect failed')

        cog = object.__new__(self.administration.administration)
        with mock.patch.object(
            self.administration.confirmation_publication,
            'publish_confirmed_game',
            new=partially_publish,
        ), mock.patch.object(
            self.administration.logger,
            'exception',
        ):
            with self.assertRaises(
                self.administration.ConfirmedWinPublicationError
            ) as raised:
                await (
                    self.administration.administration
                    ._publish_confirmed_game(
                        cog,
                        result=result,
                        guild=SimpleNamespace(id=100),
                        prefix='$',
                        channel=SimpleNamespace(),
                    )
                )

        self.assertEqual(effects, ['first Discord effect'])
        self.assertEqual(raised.exception.result, result)

    async def test_confirm_snapshot_failure_is_reconciliation_without_effects(
        self,
    ):
        result = self.administration.elo_workers.ConfirmedWinResult(99, 'Alpha')
        cog = object.__new__(self.administration.administration)
        cog._run_confirm_game_job = mock.AsyncMock(
            side_effect=self.administration.elo_workers.ConfirmedWinSnapshotError(
                result
            )
        )
        cog._publish_confirmed_game = mock.AsyncMock()

        with self.assertRaises(
            self.administration.ConfirmedWinPublicationError
        ) as raised:
            await cog._confirm_game_and_post(
                game_id=99,
                guild=SimpleNamespace(id=100),
                prefix='$',
                channel=SimpleNamespace(),
                requester=SimpleNamespace(id=300, display_name='Staff'),
            )

        self.assertIs(raised.exception.result, result)
        cog._publish_confirmed_game.assert_not_awaited()

    async def test_manual_confirm_post_commit_failure_reports_reconciliation(
        self,
    ):
        class Typing:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_value, traceback):
                return False

        result = self.administration.elo_workers.ConfirmedWinResult(
            game_id=99,
            winner_name='Alpha',
            publication=object(),
        )
        winning_game = SimpleNamespace(
            id=99,
            is_completed=True,
            is_confirmed=False,
            winner=SimpleNamespace(name=lambda: 'Alpha'),
        )
        messages = []
        context = SimpleNamespace(
            author=SimpleNamespace(
                id=300,
                display_name='Staff Tester',
                mention='<@300>',
            ),
            guild=SimpleNamespace(id=100),
            prefix='$',
            channel=SimpleNamespace(),
            typing=lambda: Typing(),
            send=mock.AsyncMock(
                side_effect=lambda message: messages.append(message)
            ),
        )
        command = next(
            command
            for command in self.administration.administration.__cog_commands__
            if command.name == 'confirm'
        )
        cog = SimpleNamespace(
            _confirm_game_and_post=mock.AsyncMock(
                side_effect=self.administration.ConfirmedWinPublicationError(
                    result
                )
            )
        )

        with mock.patch.object(
            self.administration.PolyGame,
            'convert',
            new=mock.AsyncMock(return_value=winning_game),
        ):
            await command.callback(cog, context, arg='99')

        self.assertTrue(any('ELO changes committed' in m for m in messages))
        self.assertTrue(any('Do not confirm it again' in m for m in messages))
        self.assertFalse(any('rolled back' in m for m in messages))

    async def test_auto_confirm_continues_after_publication_failure(self):
        now = datetime.datetime.now()
        auto_workers = self.administration.auto_confirmation_workers
        policy = auto_workers.AutoConfirmationPolicy(as_of=now)
        evidence = auto_workers.AutoConfirmationEvidence(
            reason='Ranked win claimed more than 24 hours ago.',
            confirmed_count=1,
            side_count=2,
        )
        batch = auto_workers.AutoConfirmationBatch(
            guild_id=100,
            policy=policy,
            unconfirmed_count=2,
            candidates=(
                auto_workers.AutoConfirmationCandidate(99, evidence),
                auto_workers.AutoConfirmationCandidate(100, evidence),
            ),
            truncated=False,
        )
        results = [
            self.administration.elo_workers.ConfirmedWinResult(
                99, 'Alpha', object(), evidence
            ),
            self.administration.elo_workers.ConfirmedWinResult(
                100, 'Beta', object(), evidence
            ),
        ]
        publication_error = (
            self.administration.ConfirmedWinPublicationError(results[0])
        )
        channel = SimpleNamespace(send=mock.AsyncMock())
        cog = SimpleNamespace(
            _run_confirm_game_job=mock.AsyncMock(side_effect=results),
            _publish_confirmed_game=mock.AsyncMock(
                side_effect=[publication_error, None]
            ),
        )

        with mock.patch.object(
            self.administration.settings,
            'elo_job_coordinator',
            SimpleNamespace(is_active=False),
        ), mock.patch.object(
            auto_workers,
            'run_discover_auto_confirmations',
            new=mock.AsyncMock(return_value=batch),
        ):
            counts = await self.administration.administration.confirm_auto(
                cog,
                SimpleNamespace(id=100),
                '$',
                channel,
            )

        self.assertEqual(counts, (2, 2))
        self.assertEqual(cog._run_confirm_game_job.await_count, 2)
        self.assertEqual(
            cog._run_confirm_game_job.await_args_list[0].kwargs[
                'auto_policy'
            ],
            policy,
        )
        self.assertEqual(cog._publish_confirmed_game.await_count, 2)
        sent_messages = [call.args[0] for call in channel.send.await_args_list]
        self.assertTrue(
            any('Do not confirm it again' in m for m in sent_messages)
        )
        self.assertTrue(
            any('Game 100 auto-confirmed' in m for m in sent_messages)
        )

    async def test_auto_confirm_skips_stale_candidate_and_continues(self):
        auto_workers = self.administration.auto_confirmation_workers
        policy = auto_workers.AutoConfirmationPolicy(
            as_of=datetime.datetime.now()
        )
        evidence = auto_workers.AutoConfirmationEvidence(
            reason='Due to partial confirmations.',
            confirmed_count=2,
            side_count=2,
        )
        batch = auto_workers.AutoConfirmationBatch(
            guild_id=100,
            policy=policy,
            unconfirmed_count=2,
            candidates=(
                auto_workers.AutoConfirmationCandidate(99, evidence),
                auto_workers.AutoConfirmationCandidate(100, evidence),
            ),
            truncated=False,
        )
        result = self.administration.elo_workers.ConfirmedWinResult(
            100,
            'Beta',
            object(),
            evidence,
        )
        channel = SimpleNamespace(send=mock.AsyncMock())
        cog = SimpleNamespace(
            _run_confirm_game_job=mock.AsyncMock(side_effect=[
                self.administration.elo_workers.AutoConfirmationIneligible(),
                result,
            ]),
            _publish_confirmed_game=mock.AsyncMock(),
        )

        with mock.patch.object(
            self.administration.settings,
            'elo_job_coordinator',
            SimpleNamespace(is_active=False),
        ), mock.patch.object(
            auto_workers,
            'run_discover_auto_confirmations',
            new=mock.AsyncMock(return_value=batch),
        ):
            counts = await self.administration.administration.confirm_auto(
                cog,
                SimpleNamespace(id=100),
                '$',
                channel,
            )

        self.assertEqual(counts, (2, 1))
        self.assertEqual(cog._run_confirm_game_job.await_count, 2)
        cog._publish_confirmed_game.assert_awaited_once_with(
            result=result,
            guild=mock.ANY,
            prefix='$',
            channel=channel,
        )
        self.assertTrue(any(
            'Game 100 auto-confirmed' in call.args[0]
            for call in channel.send.await_args_list
        ))

    async def test_auto_confirm_continues_after_committed_snapshot_failure(self):
        auto_workers = self.administration.auto_confirmation_workers
        policy = auto_workers.AutoConfirmationPolicy(
            as_of=datetime.datetime.now()
        )
        evidence = auto_workers.AutoConfirmationEvidence(
            reason='Due to partial confirmations.',
            confirmed_count=2,
            side_count=2,
        )
        batch = auto_workers.AutoConfirmationBatch(
            guild_id=100,
            policy=policy,
            unconfirmed_count=2,
            candidates=(
                auto_workers.AutoConfirmationCandidate(99, evidence),
                auto_workers.AutoConfirmationCandidate(100, evidence),
            ),
            truncated=False,
        )
        committed = self.administration.elo_workers.ConfirmedWinResult(
            99,
            'Alpha',
            auto_confirmation=evidence,
        )
        second = self.administration.elo_workers.ConfirmedWinResult(
            100,
            'Beta',
            object(),
            evidence,
        )
        channel = SimpleNamespace(send=mock.AsyncMock())
        cog = SimpleNamespace(
            _run_confirm_game_job=mock.AsyncMock(side_effect=[
                self.administration.elo_workers.ConfirmedWinSnapshotError(
                    committed
                ),
                second,
            ]),
            _publish_confirmed_game=mock.AsyncMock(),
        )

        with mock.patch.object(
            self.administration.settings,
            'elo_job_coordinator',
            SimpleNamespace(is_active=False),
        ), mock.patch.object(
            auto_workers,
            'run_discover_auto_confirmations',
            new=mock.AsyncMock(return_value=batch),
        ):
            counts = await self.administration.administration.confirm_auto(
                cog,
                SimpleNamespace(id=100),
                '$',
                channel,
            )

        self.assertEqual(counts, (2, 2))
        cog._publish_confirmed_game.assert_awaited_once()
        self.assertTrue(any(
            'Do not confirm it again' in call.args[0]
            for call in channel.send.await_args_list
        ))

    async def test_auto_confirm_cycle_contains_one_guild_failure(self):
        first_channel = SimpleNamespace(send=mock.AsyncMock())
        second_channel = SimpleNamespace(send=mock.AsyncMock())
        first_guild = SimpleNamespace(
            id=100,
            get_channel=lambda _channel_id: first_channel,
        )
        second_guild = SimpleNamespace(
            id=200,
            get_channel=lambda _channel_id: second_channel,
        )
        cog = SimpleNamespace(
            bot=SimpleNamespace(guilds=(first_guild, second_guild)),
            confirm_auto=mock.AsyncMock(side_effect=[
                peewee.OperationalError('first guild failed'),
                (3, 1),
            ]),
        )

        def guild_setting(_guild_id, setting):
            return 999 if setting == 'log_channel' else '$'

        with mock.patch.object(
            self.administration.settings,
            'elo_job_coordinator',
            SimpleNamespace(is_active=False),
        ), mock.patch.object(
            self.administration.settings,
            'guild_setting',
            side_effect=guild_setting,
        ), mock.patch.object(self.administration.logger, 'exception'):
            await (
                self.administration.administration.run_confirm_auto_cycle(cog)
            )

        self.assertEqual(cog.confirm_auto.await_count, 2)
        second_channel.send.assert_awaited_once_with(
            'Autoconfirm process complete. 1 games auto-confirmed. '
            '2 games left unconfirmed.'
        )

    async def test_auto_confirm_later_cycle_runs_after_prior_failure(self):
        channel = SimpleNamespace(send=mock.AsyncMock())
        guild = SimpleNamespace(
            id=100,
            get_channel=lambda _channel_id: channel,
        )
        cog = SimpleNamespace(
            bot=SimpleNamespace(guilds=(guild,)),
            confirm_auto=mock.AsyncMock(side_effect=[
                peewee.OperationalError('first cycle failed'),
                (1, 0),
            ]),
        )

        def guild_setting(_guild_id, setting):
            return 999 if setting == 'log_channel' else '$'

        with mock.patch.object(
            self.administration.settings,
            'elo_job_coordinator',
            SimpleNamespace(is_active=False),
        ), mock.patch.object(
            self.administration.settings,
            'guild_setting',
            side_effect=guild_setting,
        ), mock.patch.object(self.administration.logger, 'exception'):
            await (
                self.administration.administration.run_confirm_auto_cycle(cog)
            )
            await (
                self.administration.administration.run_confirm_auto_cycle(cog)
            )

        self.assertEqual(cog.confirm_auto.await_count, 2)

    async def test_prefix_auto_confirm_retains_public_summary(self):
        command = next(
            command
            for command in self.administration.administration.__cog_commands__
            if command.name == 'confirm'
        )
        messages = []
        context = SimpleNamespace(
            guild=SimpleNamespace(id=100),
            prefix='$',
            channel=SimpleNamespace(),
            author=SimpleNamespace(mention='<@300>'),
            send=mock.AsyncMock(
                side_effect=lambda message: messages.append(message)
            ),
        )
        cog = SimpleNamespace(
            confirm_auto=mock.AsyncMock(return_value=(5, 2))
        )

        with mock.patch.object(
            self.administration.settings,
            'elo_job_coordinator',
            SimpleNamespace(is_active=False),
        ):
            await command.callback(cog, context, arg='auto')

        cog.confirm_auto.assert_awaited_once_with(
            context.guild,
            '$',
            context.channel,
        )
        self.assertEqual(
            messages,
            ['Autoconfirm process complete. 2 games auto-confirmed. '
             '3 games left unconfirmed.'],
        )

    async def test_confirm_slash_defers_before_shared_confirmation(self):
        events = []

        async def confirm_and_post(**kwargs):
            events.append('confirm')
            self.assertEqual(events[0], 'defer')
            raise self.administration.elo_workers.WinValidationError(
                'not eligible'
            )

        interaction = SimpleNamespace(
            user=SimpleNamespace(
                id=300,
                display_name='Staff Tester',
            ),
            guild=SimpleNamespace(id=100),
            channel=SimpleNamespace(),
            response=SimpleNamespace(
                defer=mock.AsyncMock(
                    side_effect=lambda: events.append('defer')
                ),
                send_message=mock.AsyncMock(),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        cog = SimpleNamespace(
            _confirm_game_and_post=confirm_and_post
        )

        with mock.patch.object(
            self.administration.settings,
            'is_staff',
            return_value=True,
        ), mock.patch.object(
            self.administration.settings,
            'guild_setting',
            return_value='$',
        ):
            await self.administration.administration.confirm_slash(
                cog,
                interaction,
                99,
            )

        self.assertEqual(events[:2], ['defer', 'confirm'])
        interaction.followup.send.assert_awaited_once_with('not eligible')

    async def test_delete_database_failure_preserves_discord_resources(self):
        class Coordinator:
            is_active = False
            active_job = None

            async def run(self, **kwargs):
                raise peewee.OperationalError('simulated delete failure')

        class Typing:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_value, traceback):
                return False

        guild = SimpleNamespace(
            id=100,
            get_channel=lambda channel_id: None,
        )
        game = SimpleNamespace(
            id=126,
            guild_id=100,
            is_pending=False,
            winner=object(),
            is_confirmed=True,
            is_ranked=True,
            notes=None,
            completed_ts=None,
            gamesides=[],
            game_chan=555,
            announcement_message=None,
            announcement_channel=None,
            mentions=lambda: ['<@200>'],
            is_season_game=lambda: False,
        )
        messages = []
        context = SimpleNamespace(
            interaction=None,
            author=SimpleNamespace(
                id=300,
                display_name='Moderator',
                mention='<@300>',
            ),
            guild=guild,
            prefix='$',
            channel=SimpleNamespace(),
            typing=lambda: Typing(),
            send=mock.AsyncMock(
                side_effect=lambda message: messages.append(message)
            ),
        )
        command = next(
            command for command in self.games.polygames.__cog_commands__
            if command.name == 'delete'
        )

        with mock.patch.object(
            self.games.Game, 'get_by_id', return_value=game
        ), mock.patch.object(
            self.games.settings, 'elo_job_coordinator', Coordinator()
        ), mock.patch.object(
            self.games.settings, 'is_mod', return_value=True
        ), mock.patch.object(
            self.games.models.GameLog,
            'member_string',
            return_value='**Moderator** (`300`)',
        ), mock.patch.object(
            self.games.game_deletion,
            'authorize_delete',
            new=mock.AsyncMock(
                return_value=self.games.game_deletion.game_deletion_workers.DeletionClassification(
                    game_id=126,
                    guild_id=100,
                    state=self.games.game_deletion.game_deletion_workers.IN_PROGRESS,
                    host_id=None,
                    host_name=None,
                    registered=True,
                )
            ),
        ), mock.patch.object(
            self.games.utilities, 'lock_game'
        ), mock.patch.object(
            self.games.utilities, 'unlock_game'
        ), mock.patch.object(
            self.games.channels,
            'delete_game_channel',
            new=mock.AsyncMock(),
        ) as delete_channel, mock.patch.object(
            self.games.image_storage,
            'edit_game_embed',
            new=mock.AsyncMock(),
        ) as edit_announcement, mock.patch.object(
            self.games.logger, 'exception'
        ):
            await command.callback(
                SimpleNamespace(bot=SimpleNamespace(guilds=[guild])),
                context,
                126,
            )

        delete_channel.assert_not_awaited()
        edit_announcement.assert_not_awaited()
        self.assertTrue(
            any('No Discord channel updates were made' in message
                for message in messages)
        )
