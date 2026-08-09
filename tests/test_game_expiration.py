"""Focused offline tests for the expired pending-game purge boundary."""

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


workers = import_offline_runtime('modules.game_expiration_workers')
service = import_offline_runtime('modules.game_expiration')
matchmaking = import_offline_runtime('modules.matchmaking')


NOW = datetime.datetime(2026, 8, 9, 12, 0, 0)


def purge_request(game_id=77):
    return workers.ExpiredGamePurgeRequest(
        game_id=game_id,
        guild_id=10,
        as_of=NOW,
        announcement_channel_id=500,
    )


def effect_plan(game_id=77, *, message='Purged game'):
    return workers.ExpiredGameEffectPlan(
        game_id=game_id,
        guild_id=10,
        announcement_channel_id=500,
        public_message=message,
        broadcast_targets=(
            workers.game_deletion_workers.DeletionBroadcastTarget(600, 700),
        ),
    )


def purge_result(game_id=77, *, message='Purged game'):
    return workers.ExpiredGamePurgeResult(
        game_id=game_id,
        status=workers.PURGED,
        effect_plan=effect_plan(game_id, message=message),
    )


class Database:
    def __init__(self, logs):
        self.logs = logs
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
                self.log_count = len(database.logs)
                return self

            def __exit__(self, exc_type, *_args):
                if exc_type is None:
                    database.commits += 1
                    return False
                database.rollbacks += 1
                del database.logs[self.log_count:]
                return False

        return Atomic()


class ExpiredGameWorkerTests(unittest.IsolatedAsyncioTestCase):
    def test_requests_and_results_are_frozen_primitives(self):
        request = purge_request()
        self.assertTrue(all(
            isinstance(value, (int, datetime.datetime, type(None)))
            for value in request.__dict__.values()
        ))
        with self.assertRaises(FrozenInstanceError):
            request.game_id = 88

    def test_eligibility_preserves_open_and_full_grace_rules(self):
        def game(*, players, capacity, age_days, pending=True):
            return SimpleNamespace(
                is_pending=pending,
                expiration=NOW - datetime.timedelta(days=age_days),
                capacity=lambda: (players, capacity),
            )

        self.assertTrue(workers._is_eligible(
            game(players=1, capacity=2, age_days=0.1), as_of=NOW,
        ))
        self.assertFalse(workers._is_eligible(
            game(players=2, capacity=2, age_days=2), as_of=NOW,
        ))
        self.assertTrue(workers._is_eligible(
            game(players=2, capacity=2, age_days=4), as_of=NOW,
        ))
        self.assertFalse(workers._is_eligible(
            game(players=1, capacity=2, age_days=4, pending=False), as_of=NOW,
        ))

    def test_missing_creator_uses_safe_fallback_and_freezes_targets(self):
        game = SimpleNamespace(
            id=77,
            is_pending=True,
            is_ranked=True,
            host=None,
            capacity=lambda: (2, 2),
            mentions=lambda: ('<@1>', '<@2>'),
            creating_player=mock.Mock(side_effect=peewee.DoesNotExist()),
        )
        targets = (
            workers.game_deletion_workers.DeletionBroadcastTarget(6, 7),
        )
        with mock.patch.object(
            workers.game_deletion_workers,
            'freeze_broadcast_targets',
            return_value=targets,
        ):
            plan, audit = workers._build_effect_plan(game, purge_request())

        self.assertIn('its creator', plan.public_message)
        self.assertEqual(plan.broadcast_targets, targets)
        self.assertIn('external_broadcasts=6/7', audit)

    def test_one_worker_transaction_commits_audit_and_deletion(self):
        logs = []
        database = Database(logs)
        game = SimpleNamespace(id=77)
        models = SimpleNamespace(
            db=database,
            GameLog=SimpleNamespace(
                write=lambda **kwargs: logs.append(kwargs),
            ),
        )
        deleted = []
        with mock.patch.object(workers, 'models', models), mock.patch.object(
            workers,
            '_load_locked_game',
            return_value=game,
        ), mock.patch.object(
            workers,
            '_is_eligible',
            return_value=True,
        ), mock.patch.object(
            workers,
            '_build_effect_plan',
            return_value=(effect_plan(), 'audit targets'),
        ), mock.patch.object(
            workers.game_deletion_workers,
            'delete_pending_records',
            side_effect=lambda loaded: deleted.append(loaded.id),
        ):
            result = workers.purge_expired_game(purge_request())

        self.assertEqual(result.status, workers.PURGED)
        self.assertEqual(deleted, [77])
        self.assertEqual(len(logs), 1)
        self.assertTrue(logs[0]['is_protected'])
        self.assertEqual(database.commits, 1)
        self.assertEqual(database.rollbacks, 0)
        self.assertEqual(database.connection_opened, 1)
        self.assertEqual(database.connection_closed, 1)

    def test_worker_rolls_back_audit_when_graph_delete_fails(self):
        logs = []
        database = Database(logs)
        models = SimpleNamespace(
            db=database,
            GameLog=SimpleNamespace(
                write=lambda **kwargs: logs.append(kwargs),
            ),
        )
        with mock.patch.object(workers, 'models', models), mock.patch.object(
            workers,
            '_load_locked_game',
            return_value=SimpleNamespace(id=77),
        ), mock.patch.object(
            workers,
            '_is_eligible',
            return_value=True,
        ), mock.patch.object(
            workers,
            '_build_effect_plan',
            return_value=(effect_plan(), 'audit targets'),
        ), mock.patch.object(
            workers.game_deletion_workers,
            'delete_pending_records',
            side_effect=peewee.OperationalError('injected purge failure'),
        ):
            with self.assertRaisesRegex(peewee.OperationalError, 'injected'):
                workers.purge_expired_game(purge_request())

        self.assertEqual(logs, [])
        self.assertEqual(database.commits, 0)
        self.assertEqual(database.rollbacks, 1)

    def test_state_change_is_typed_skip_without_audit_or_delete(self):
        logs = []
        database = Database(logs)
        models = SimpleNamespace(
            db=database,
            GameLog=SimpleNamespace(write=lambda **kwargs: logs.append(kwargs)),
        )
        with mock.patch.object(workers, 'models', models), mock.patch.object(
            workers,
            '_load_locked_game',
            return_value=SimpleNamespace(id=77),
        ), mock.patch.object(
            workers,
            '_is_eligible',
            return_value=False,
        ), mock.patch.object(
            workers.game_deletion_workers,
            'delete_pending_records',
        ) as delete:
            result = workers.purge_expired_game(purge_request())

        self.assertEqual(result.status, workers.SKIPPED_STATE_CHANGED)
        self.assertIsNone(result.effect_plan)
        self.assertEqual(logs, [])
        delete.assert_not_called()

    async def test_runner_uses_pending_game_coordinator(self):
        coordinator = mock.AsyncMock(return_value=purge_result())
        with mock.patch.object(
            workers.game_open_workers.pending_game_coordinator,
            'run_worker',
            coordinator,
        ):
            result = await workers.run_purge_expired_game(purge_request())
        self.assertEqual(result.status, workers.PURGED)
        coordinator.assert_awaited_once_with(
            workers.purge_expired_game,
            mock.ANY,
        )

    async def test_discovery_executor_keeps_event_loop_responsive(self):
        started = threading.Event()
        release = threading.Event()

        def slow_discovery(_request):
            started.set()
            release.wait(timeout=2)
            return workers.ExpiredGameDiscoveryResult((77,), False)

        try:
            with mock.patch.object(
                workers,
                'discover_expired_game_ids',
                side_effect=slow_discovery,
            ):
                task = asyncio.create_task(
                    workers.run_discover_expired_game_ids(
                        workers.ExpiredGameDiscoveryRequest(10, NOW)
                    )
                )
                for _ in range(500):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.001)
                self.assertTrue(started.is_set())
                await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.2)
                release.set()
                result = await task
        finally:
            release.set()
        self.assertEqual(result.game_ids, (77,))

    async def test_discovery_cancellation_waits_for_connection_work(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def slow_discovery(_request):
            started.set()
            release.wait(timeout=2)
            finished.set()
            return workers.ExpiredGameDiscoveryResult((77,), False)

        try:
            with mock.patch.object(
                workers,
                'discover_expired_game_ids',
                side_effect=slow_discovery,
            ):
                task = asyncio.create_task(
                    workers.run_discover_expired_game_ids(
                        workers.ExpiredGameDiscoveryRequest(10, NOW)
                    )
                )
                for _ in range(500):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.001)
                task.cancel()
                await asyncio.sleep(0.01)
                self.assertFalse(task.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        finally:
            release.set()
        self.assertTrue(finished.is_set())


class ExpiredGameServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_publication_failure_warns_staff_and_continues(self):
        broadcast_message = SimpleNamespace(
            content='Join game 77',
            edit=mock.AsyncMock(side_effect=RuntimeError('edit failed')),
            clear_reactions=mock.AsyncMock(),
        )
        broadcast_channel = SimpleNamespace(
            fetch_message=mock.AsyncMock(return_value=broadcast_message),
        )
        announce_channel = SimpleNamespace(send=mock.AsyncMock())
        staff_channel = SimpleNamespace(send=mock.AsyncMock())
        bot = SimpleNamespace(
            get_channel=lambda channel_id: {
                500: announce_channel,
                600: broadcast_channel,
            }.get(channel_id),
        )
        await service.publish_purge_result(
            purge_result(),
            bot=bot,
            guild=SimpleNamespace(id=10),
            staff_channel=staff_channel,
        )

        announce_channel.send.assert_awaited_once_with('Purged game')
        staff_channel.send.assert_awaited_once()
        self.assertIn('600/700', staff_channel.send.await_args.args[0])

    async def test_cycle_contains_one_transaction_failure_and_continues(self):
        staff_channel = SimpleNamespace(send=mock.AsyncMock())
        guild = SimpleNamespace(
            id=10,
            get_channel=lambda _channel_id: staff_channel,
        )
        bot = SimpleNamespace(get_channel=lambda _channel_id: None)
        discovered = workers.ExpiredGameDiscoveryResult((71, 72), False)
        runner = mock.AsyncMock(side_effect=[
            peewee.OperationalError('one bad candidate'),
            purge_result(72, message=''),
        ])
        with mock.patch.object(
            service.settings,
            'guild_setting',
            side_effect=lambda _guild_id, key: {
                'game_announce_channel': 500,
                'log_channel': 900,
            }[key],
        ), mock.patch.object(
            service.game_expiration_workers,
            'run_discover_expired_game_ids',
            new=mock.AsyncMock(return_value=discovered),
        ), mock.patch.object(
            service.game_expiration_workers,
            'run_purge_expired_game',
            runner,
        ):
            results = await service.purge_expired_games_for_guild(
                bot=bot,
                guild=guild,
                as_of=NOW,
            )

        self.assertEqual([result.game_id for result in results], [72])
        self.assertEqual(runner.await_count, 2)
        self.assertTrue(staff_channel.send.await_count)

    def test_background_task_delegates_without_direct_model_queries(self):
        source = inspect.getsource(
            matchmaking.matchmaking.task_purge_expired_games.coro
        )
        self.assertIn('purge_expired_games_for_guild', source)
        self.assertNotIn('models.Game.select', source)
        self.assertNotIn('delete_game()', source)


if __name__ == '__main__':
    unittest.main()
