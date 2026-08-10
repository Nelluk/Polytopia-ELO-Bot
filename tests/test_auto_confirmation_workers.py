"""Offline coverage for bounded automatic-confirmation discovery."""

import asyncio
from contextlib import AbstractContextManager
import datetime
import importlib
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

from peewee import SchemaManager
from playhouse.postgres_ext import PostgresqlExtDatabase


def import_offline_runtime(module_name):
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


class FakeField:
    def is_null(self, _value):
        return object()


class FakeQuery:
    def __init__(self, games):
        self.games = list(games)
        self.limit_value = None

    def count(self):
        return len(self.games)

    def where(self, *_args):
        return self

    def order_by(self, *_args):
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def __iter__(self):
        games = sorted(
            (game for game in self.games if game.win_claimed_ts is not None),
            key=lambda game: (game.win_claimed_ts, game.id),
        )
        return iter(games[:self.limit_value])


class FakeDatabase:
    def __init__(self):
        self.opened = 0
        self.closed = 0
        self.thread_ids = []

    def connection_context(self):
        database = self

        class Context(AbstractContextManager):
            def __enter__(self):
                database.opened += 1
                database.thread_ids.append(threading.get_ident())

            def __exit__(self, *_args):
                database.closed += 1

        return Context()


class AutoConfirmationWorkerTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.workers = import_offline_runtime(
            'modules.auto_confirmation_workers'
        )

    def _game(self, game_id, claimed, *, ranked=True, confirmed=1, sides=2):
        return SimpleNamespace(
            id=game_id,
            win_claimed_ts=claimed,
            is_ranked=ranked,
            confirmations_count=lambda: (
                confirmed,
                sides,
                confirmed == sides,
            ),
        )

    def test_eligibility_is_filtered_before_the_query_limit(self):
        request = self.workers.AutoConfirmationDiscoveryRequest(
            guild_id=100,
            policy=self.workers.AutoConfirmationPolicy(
                as_of=datetime.datetime(2026, 8, 10, 12, 0)
            ),
        )
        query = self.workers.models.Game.search(
            status_filter=5,
            guild_id=100,
        )
        sql, parameters = self.workers._eligible_unconfirmed_query(
            query,
            request,
        ).limit(101).sql()

        self.assertIn('HAVING', sql)
        self.assertIn('win_claimed_ts', sql)
        self.assertIn('LIMIT', sql)
        self.assertIn(100, parameters)

    async def test_discovery_is_bounded_frozen_and_worker_owned(self):
        as_of = datetime.datetime(2026, 8, 10, 12, 0)
        games = [
            self._game(
                game_id,
                as_of - datetime.timedelta(hours=25, minutes=game_id),
            )
            for game_id in range(1, 5)
        ]
        games.append(self._game(9, None))
        database = FakeDatabase()

        class GameTable:
            win_claimed_ts = FakeField()
            id = FakeField()

            @staticmethod
            def search(**_kwargs):
                return FakeQuery(games)

        request = self.workers.AutoConfirmationDiscoveryRequest(
            guild_id=100,
            policy=self.workers.AutoConfirmationPolicy(as_of=as_of),
            limit=2,
        )
        main_thread = threading.get_ident()
        with mock.patch.object(
            self.workers,
            'models',
            SimpleNamespace(db=database, Game=GameTable),
        ), mock.patch.object(
            self.workers,
            '_eligible_unconfirmed_query',
            side_effect=lambda query, _request: query,
        ):
            batch = await self.workers.run_discover_auto_confirmations(request)

        self.assertEqual(batch.unconfirmed_count, 5)
        self.assertEqual(len(batch.candidates), 2)
        self.assertTrue(batch.truncated)
        self.assertTrue(all(
            isinstance(candidate, self.workers.AutoConfirmationCandidate)
            for candidate in batch.candidates
        ))
        self.assertEqual(database.opened, 1)
        self.assertEqual(database.closed, 1)
        self.assertNotEqual(database.thread_ids, [main_thread])

    async def test_slow_discovery_does_not_block_event_loop(self):
        started = threading.Event()
        release = threading.Event()
        result = object()

        def slow_discovery(_request):
            started.set()
            release.wait(timeout=2)
            return result

        request = self.workers.AutoConfirmationDiscoveryRequest(
            guild_id=100,
            policy=self.workers.AutoConfirmationPolicy(
                as_of=datetime.datetime.now()
            ),
        )
        with mock.patch.object(
            self.workers,
            'discover_auto_confirmations',
            side_effect=slow_discovery,
        ):
            task = asyncio.create_task(
                self.workers.run_discover_auto_confirmations(request)
            )
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.005)
            heartbeat = asyncio.Event()
            asyncio.get_running_loop().call_later(0.01, heartbeat.set)
            await asyncio.wait_for(heartbeat.wait(), timeout=0.2)
            release.set()
            self.assertIs(await task, result)

    async def test_cancellation_drains_discovery_before_returning(self):
        started = threading.Event()
        release = threading.Event()

        def slow_discovery(_request):
            started.set()
            release.wait(timeout=2)
            return object()

        request = self.workers.AutoConfirmationDiscoveryRequest(
            guild_id=100,
            policy=self.workers.AutoConfirmationPolicy(
                as_of=datetime.datetime.now()
            ),
        )
        with mock.patch.object(
            self.workers,
            'discover_auto_confirmations',
            side_effect=slow_discovery,
        ):
            task = asyncio.create_task(
                self.workers.run_discover_auto_confirmations(request)
            )
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.005)
            task.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

    def test_partial_confirmation_thresholds_are_preserved(self):
        policy = self.workers.AutoConfirmationPolicy(
            as_of=datetime.datetime(2026, 8, 10, 12, 0)
        )
        self.assertIsNotNone(self.workers.eligibility_evidence(
            is_ranked=True,
            win_claimed_ts=policy.as_of,
            confirmed_count=2,
            side_count=4,
            policy=policy,
        ))
        self.assertIsNone(self.workers.eligibility_evidence(
            is_ranked=True,
            win_claimed_ts=policy.as_of,
            confirmed_count=2,
            side_count=5,
            policy=policy,
        ))
        self.assertIsNotNone(self.workers.eligibility_evidence(
            is_ranked=True,
            win_claimed_ts=policy.as_of,
            confirmed_count=3,
            side_count=5,
            policy=policy,
        ))
