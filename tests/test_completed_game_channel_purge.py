"""Offline coverage for completed-game channel cleanup."""

import asyncio
from contextlib import AbstractContextManager
from dataclasses import FrozenInstanceError
import datetime
import inspect
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

import peewee

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.completed_game_channel_purge_workers')
service = import_offline_runtime('modules.completed_game_channel_purge')
games = import_offline_runtime('modules.games')
NOW = datetime.datetime(2026, 8, 10, 12, 0, 0)


class Predicate:
    def __and__(self, _other):
        return self

    def __or__(self, _other):
        return self


class Field:
    def __init__(self, name):
        self.name = name

    def __eq__(self, _other):
        return Predicate()

    def __lt__(self, _other):
        return Predicate()

    def __gt__(self, _other):
        return Predicate()

    def is_null(self, _value):
        return Predicate()

    def in_(self, _values):
        return Predicate()


class Query:
    def __init__(self, *, rows=(), row=None):
        self.rows = rows
        self.row = row

    def select(self, *_args):
        return self

    def join(self, *_args):
        return self

    def where(self, *_args):
        return self

    def order_by(self, *_args):
        return self

    def limit(self, *_args):
        return self

    def dicts(self):
        return self.rows

    def for_update(self):
        return self

    def first(self):
        return self.row


class Database:
    def __init__(self):
        self.opens = 0
        self.closes = 0
        self.commits = 0
        self.rollbacks = 0
        self.thread_ids = []

    def connection_context(self):
        database = self

        class Connection(AbstractContextManager):
            def __enter__(self):
                database.opens += 1
                database.thread_ids.append(threading.get_ident())

            def __exit__(self, *_args):
                database.closes += 1

        return Connection()

    def atomic(self):
        database = self

        class Atomic(AbstractContextManager):
            def __enter__(self):
                return self

            def __exit__(self, exc_type, *_args):
                if exc_type is None:
                    database.commits += 1
                else:
                    database.rollbacks += 1

        return Atomic()


def target(channel_id=900, *, kind=workers.GAME_TARGET, record_id=77):
    return workers.CompletedChannelTarget(kind, record_id, 10, channel_id)


def plan(*targets):
    return workers.CompletedGameChannelPlan(
        game_id=77,
        guild_id=10,
        completed_ts=NOW - datetime.timedelta(days=2),
        targets=tuple(targets or (target(),)),
    )


class CompletedPurgeWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_returns_frozen_primitives_on_worker_connection(self):
        database = Database()
        game_rows = (
            {
                'id': 77,
                'guild_id': 10,
                'completed_ts': NOW - datetime.timedelta(days=2),
                'notes': None,
                'game_chan': 900,
            },
        )
        side_rows = (
            {
                'id': 88,
                'game': 77,
                'team_chan': 901,
                'team_chan_external_server': 11,
            },
        )

        class Game:
            id = Field('id')
            guild_id = Field('guild_id')
            completed_ts = Field('completed_ts')
            notes = Field('notes')
            game_chan = Field('game_chan')
            is_confirmed = Field('is_confirmed')
            league_season = Field('league_season')

            @staticmethod
            def select(*_args):
                return Query(rows=game_rows)

        side_queries = iter((Query(), Query(rows=side_rows)))

        class GameSide:
            id = Field('id')
            game = Field('game')
            team_chan = Field('team_chan')
            team_chan_external_server = Field('team_chan_external_server')

            @staticmethod
            def select(*_args):
                return next(side_queries)

        main_thread = threading.get_ident()
        request = workers.CompletedPurgeDiscoveryRequest((10,), NOW)
        with mock.patch.object(
            workers,
            'models',
            SimpleNamespace(db=database, Game=Game, GameSide=GameSide),
        ):
            result = await workers.run_discover_completed_game_channels(request)

        self.assertEqual(result.plans[0].game_id, 77)
        self.assertEqual(
            [(item.kind, item.guild_id, item.channel_id)
             for item in result.plans[0].targets],
            [('side', 11, 901), ('game', 10, 900)],
        )
        self.assertEqual((database.opens, database.closes), (1, 1))
        self.assertNotEqual(database.thread_ids, [main_thread])
        with self.assertRaises(FrozenInstanceError):
            result.plans[0].game_id = 99

    def test_recent_nova_exemption_preserves_strict_four_day_window(self):
        recent = {
            'notes': 'Nova Red versus NOVA BLUE',
            'completed_ts': NOW - datetime.timedelta(days=3),
        }
        old = dict(recent, completed_ts=NOW - datetime.timedelta(days=4))
        self.assertTrue(workers._is_recent_nova_game(recent, as_of=NOW))
        self.assertFalse(workers._is_recent_nova_game(old, as_of=NOW))

    def test_discovery_request_guild_and_game_bounds_are_enforced(self):
        with self.assertRaises(workers.CompletedChannelPurgeWorkerError):
            workers._normalise_discovery_request(
                workers.CompletedPurgeDiscoveryRequest(
                    tuple(range(1, 102)), NOW,
                )
            )
        with self.assertRaises(workers.CompletedChannelPurgeWorkerError):
            workers._normalise_discovery_request(
                workers.CompletedPurgeDiscoveryRequest(
                    (10,), NOW, workers.MAX_COMPLETED_PURGE_GAMES + 1,
                )
            )

    async def test_reconcile_clears_only_exact_channel_in_transaction(self):
        database = Database()
        row = SimpleNamespace(team_chan=901, save=mock.Mock())
        game_side = SimpleNamespace(
            id=Field('id'),
            game=Field('game'),
            team_chan=Field('team_chan'),
            select=lambda *_args: Query(row=row),
        )
        game = SimpleNamespace(guild_id=Field('guild_id'))
        request = workers.CompletedChannelReconcileRequest(
            77, 10, target(901, kind=workers.SIDE_TARGET, record_id=88),
        )
        with mock.patch.object(
            workers,
            'models',
            SimpleNamespace(db=database, Game=game, GameSide=game_side),
        ):
            result = await workers.run_reconcile_deleted_channel(request)

        self.assertEqual(result.status, workers.RECONCILED)
        self.assertIsNone(row.team_chan)
        row.save.assert_called_once_with(only=(game_side.team_chan,))
        self.assertEqual(database.commits, 1)
        self.assertEqual(database.rollbacks, 0)
        self.assertEqual((database.opens, database.closes), (1, 1))

    async def test_changed_target_is_not_cleared(self):
        database = Database()
        row = SimpleNamespace(game_chan=999, save=mock.Mock())
        game = SimpleNamespace(
            id=Field('id'),
            guild_id=Field('guild_id'),
            game_chan=Field('game_chan'),
            select=lambda *_args: Query(row=row),
        )
        with mock.patch.object(
            workers,
            'models',
            SimpleNamespace(db=database, Game=game, GameSide=object()),
        ):
            result = await workers.run_reconcile_deleted_channel(
                workers.CompletedChannelReconcileRequest(77, 10, target(900))
            )
        self.assertEqual(result.status, workers.TARGET_CHANGED)
        self.assertEqual(row.game_chan, 999)
        row.save.assert_not_called()

    async def test_reconcile_failure_rolls_back_and_closes_connection(self):
        database = Database()
        row = SimpleNamespace(
            game_chan=900,
            save=mock.Mock(side_effect=peewee.OperationalError('save failed')),
        )
        game = SimpleNamespace(
            id=Field('id'),
            guild_id=Field('guild_id'),
            game_chan=Field('game_chan'),
            select=lambda *_args: Query(row=row),
        )
        with mock.patch.object(
            workers,
            'models',
            SimpleNamespace(db=database, Game=game, GameSide=object()),
        ):
            with self.assertRaisesRegex(peewee.OperationalError, 'save failed'):
                await workers.run_reconcile_deleted_channel(
                    workers.CompletedChannelReconcileRequest(
                        77, 10, target(900)
                    )
                )
        self.assertEqual(database.commits, 0)
        self.assertEqual(database.rollbacks, 1)
        self.assertEqual((database.opens, database.closes), (1, 1))

    async def test_slow_discovery_is_responsive_and_cancellation_drains(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def slow(_request):
            started.set()
            release.wait(timeout=2)
            finished.set()
            return workers.CompletedPurgeDiscoveryResult((), False)

        request = workers.CompletedPurgeDiscoveryRequest((10,), NOW)
        try:
            with mock.patch.object(
                workers, 'discover_completed_game_channels', side_effect=slow,
            ):
                task = asyncio.create_task(
                    workers.run_discover_completed_game_channels(request)
                )
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.002)
                heartbeat = asyncio.Event()
                asyncio.get_running_loop().call_later(0.01, heartbeat.set)
                await asyncio.wait_for(heartbeat.wait(), timeout=0.2)
                task.cancel()
                await asyncio.sleep(0.01)
                self.assertFalse(task.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        finally:
            release.set()
        self.assertTrue(finished.is_set())


class CompletedPurgeServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_precedes_exact_reconciliation(self):
        events = []
        guild = SimpleNamespace(id=10)
        bot = SimpleNamespace(guilds=(guild,), get_guild=lambda _id: guild)
        discovery = workers.CompletedPurgeDiscoveryResult((plan(target()),), False)

        async def delete(*_args, **_kwargs):
            events.append('delete')
            return True

        async def reconcile(_request):
            events.append('reconcile')
            return workers.CompletedChannelReconcileResult(
                77, 900, workers.RECONCILED,
            )

        with mock.patch.object(
            service.workers,
            'run_discover_completed_game_channels',
            new=mock.AsyncMock(return_value=discovery),
        ), mock.patch.object(
            service.channels, 'delete_game_channel', side_effect=delete,
        ), mock.patch.object(
            service.workers,
            'run_reconcile_deleted_channel',
            side_effect=reconcile,
        ):
            outcome = await service.purge_completed_game_channels(
                bot=bot, as_of=NOW,
            )

        self.assertEqual(events, ['delete', 'reconcile'])
        self.assertEqual(outcome.deleted_targets, 1)
        self.assertEqual(outcome.reconciled_targets, 1)
        self.assertEqual(outcome.failed_targets, 0)

    async def test_failed_delete_retains_reference_and_later_target_runs(self):
        guild = SimpleNamespace(id=10)
        bot = SimpleNamespace(guilds=(guild,), get_guild=lambda _id: guild)
        discovery = workers.CompletedPurgeDiscoveryResult(
            (plan(target(900), target(901)),), False,
        )
        reconcile = mock.AsyncMock(return_value=(
            workers.CompletedChannelReconcileResult(
                77, 901, workers.RECONCILED,
            )
        ))
        with mock.patch.object(
            service.workers,
            'run_discover_completed_game_channels',
            new=mock.AsyncMock(return_value=discovery),
        ), mock.patch.object(
            service.channels,
            'delete_game_channel',
            new=mock.AsyncMock(side_effect=[False, True]),
        ), mock.patch.object(
            service.workers, 'run_reconcile_deleted_channel', new=reconcile,
        ), mock.patch.object(service.logger, 'warning'):
            outcome = await service.purge_completed_game_channels(
                bot=bot, as_of=NOW,
            )

        self.assertEqual(reconcile.await_count, 1)
        self.assertEqual(reconcile.await_args.args[0].target.channel_id, 901)
        self.assertEqual(outcome.failed_targets, 1)
        self.assertEqual(outcome.reconciled_targets, 1)

    async def test_deleted_channel_reconcile_failure_continues(self):
        guild = SimpleNamespace(id=10)
        bot = SimpleNamespace(guilds=(guild,), get_guild=lambda _id: guild)
        discovery = workers.CompletedPurgeDiscoveryResult(
            (plan(target(900), target(901)),), False,
        )
        reconcile = mock.AsyncMock(side_effect=[
            peewee.OperationalError('database unavailable'),
            workers.CompletedChannelReconcileResult(
                77, 901, workers.RECONCILED,
            ),
        ])
        with mock.patch.object(
            service.workers,
            'run_discover_completed_game_channels',
            new=mock.AsyncMock(return_value=discovery),
        ), mock.patch.object(
            service.channels,
            'delete_game_channel',
            new=mock.AsyncMock(return_value=True),
        ), mock.patch.object(
            service.workers, 'run_reconcile_deleted_channel', new=reconcile,
        ), mock.patch.object(service.logger, 'exception'):
            outcome = await service.purge_completed_game_channels(
                bot=bot, as_of=NOW,
            )

        self.assertEqual(reconcile.await_count, 2)
        self.assertEqual(outcome.deleted_targets, 2)
        self.assertEqual(outcome.reconciliation_targets, 1)
        self.assertEqual(outcome.reconciled_targets, 1)

    async def test_changed_target_is_explicit_reconciliation(self):
        guild = SimpleNamespace(id=10)
        bot = SimpleNamespace(guilds=(guild,), get_guild=lambda _id: guild)
        discovery = workers.CompletedPurgeDiscoveryResult((plan(target()),), False)
        changed = workers.CompletedChannelReconcileResult(
            77, 900, workers.TARGET_CHANGED,
        )
        with mock.patch.object(
            service.workers,
            'run_discover_completed_game_channels',
            new=mock.AsyncMock(return_value=discovery),
        ), mock.patch.object(
            service.channels,
            'delete_game_channel',
            new=mock.AsyncMock(return_value=True),
        ), mock.patch.object(
            service.workers,
            'run_reconcile_deleted_channel',
            new=mock.AsyncMock(return_value=changed),
        ), mock.patch.object(service.logger, 'warning'):
            outcome = await service.purge_completed_game_channels(
                bot=bot, as_of=NOW,
            )
        self.assertEqual(outcome.reconciliation_targets, 1)
        self.assertEqual(outcome.reconciled_targets, 0)

    async def test_cancellation_drains_delete_and_reconciliation_pair(self):
        started = asyncio.Event()
        release = asyncio.Event()
        events = []
        guild = SimpleNamespace(id=10)
        bot = SimpleNamespace(guilds=(guild,), get_guild=lambda _id: guild)
        discovery = workers.CompletedPurgeDiscoveryResult((plan(target()),), False)

        async def delete(*_args, **_kwargs):
            started.set()
            await release.wait()
            events.append('delete')
            return True

        async def reconcile(_request):
            events.append('reconcile')
            return workers.CompletedChannelReconcileResult(
                77, 900, workers.RECONCILED,
            )

        with mock.patch.object(
            service.workers,
            'run_discover_completed_game_channels',
            new=mock.AsyncMock(return_value=discovery),
        ), mock.patch.object(
            service.channels, 'delete_game_channel', side_effect=delete,
        ), mock.patch.object(
            service.workers,
            'run_reconcile_deleted_channel',
            side_effect=reconcile,
        ):
            task = asyncio.create_task(service.purge_completed_game_channels(
                bot=bot, as_of=NOW,
            ))
            await started.wait()
            task.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(events, ['delete', 'reconcile'])

    async def test_later_scheduled_cycle_runs_after_failure(self):
        cog = SimpleNamespace(bot=object())
        runner = mock.AsyncMock(side_effect=[
            RuntimeError('first failed'),
            'second cycle',
        ])
        with mock.patch.object(
            games.completed_game_channel_purge,
            'purge_completed_game_channels',
            new=runner,
        ), mock.patch.object(games.logger, 'exception'):
            first = await games.polygames.run_completed_channel_purge_cycle(cog)
            second = await games.polygames.run_completed_channel_purge_cycle(cog)
        self.assertIsNone(first)
        self.assertEqual(second, 'second cycle')
        self.assertEqual(runner.await_count, 2)

    def test_task_and_publisher_have_no_orm_or_live_game_graph(self):
        task_source = inspect.getsource(games.polygames.task_purge_game_channels)
        service_source = inspect.getsource(service.purge_completed_game_channels)
        self.assertNotIn('Game.select', task_source)
        self.assertNotIn('utilities.connect', task_source)
        self.assertNotIn('delete_game_channels', task_source)
        self.assertNotIn('models.', service_source)


if __name__ == '__main__':
    unittest.main()
