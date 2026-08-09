"""Focused offline coverage for P5.15 external-broadcast reconciliation."""

from contextlib import AbstractContextManager
from dataclasses import FrozenInstanceError
import asyncio
import inspect
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import discord

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.game_broadcast_workers')
service = import_offline_runtime('modules.game_broadcasts')
start_workers = import_offline_runtime('modules.game_start_workers')
matchmaking = import_offline_runtime('modules.matchmaking')
models = import_offline_runtime('modules.models')


def target(*, row_id=1, game_id=2, guild_id=3, channel_id=4, message_id=5):
    return workers.ExternalBroadcastTarget(
        row_id=row_id,
        game_id=game_id,
        guild_id=guild_id,
        channel_id=channel_id,
        message_id=message_id,
    )


def discord_error(error_type, *, status, code):
    response = SimpleNamespace(status=status, reason='test')
    return error_type(response, {'message': 'test', 'code': code})


class FakeDatabase:
    def __init__(self):
        self.connections = 0
        self.closed = 0
        self.commits = 0
        self.rollbacks = 0

    def connection_context(self):
        database = self

        class Connection(AbstractContextManager):
            def __enter__(self):
                database.connections += 1

            def __exit__(self, *_args):
                database.closed += 1

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
                return False

        return Atomic()


class BroadcastWorkerTests(unittest.IsolatedAsyncioTestCase):
    def test_targets_are_frozen_primitives_and_sorted(self):
        game = SimpleNamespace(id=2, guild_id=3)
        rows = (
            SimpleNamespace(id=8, game=game, channel_id=80, message_id=800),
            SimpleNamespace(id=7, game=game, channel_id=70, message_id=700),
        )
        game.broadcasts = rows
        frozen = workers.freeze_game_broadcast_targets(game)
        self.assertEqual(tuple(item.row_id for item in frozen), (7, 8))
        with self.assertRaises(FrozenInstanceError):
            frozen[0].row_id = 9
        self.assertTrue(all(
            isinstance(value, int)
            for item in frozen
            for value in item.__dict__.values()
        ))

    def test_start_snapshot_is_bounded_and_defers_remaining_rows(self):
        game = SimpleNamespace(id=2, guild_id=3)
        game.broadcasts = tuple(
            SimpleNamespace(
                id=row_id,
                game=game,
                channel_id=1_000 + row_id,
                message_id=2_000 + row_id,
            )
            for row_id in range(
                workers.MAX_STARTED_BROADCASTS_PER_GAME + 1,
                0,
                -1,
            )
        )
        with self.assertLogs(workers.logger, level='WARNING'):
            frozen = workers.freeze_game_broadcast_targets(game)
        self.assertEqual(
            len(frozen),
            workers.MAX_STARTED_BROADCASTS_PER_GAME,
        )
        self.assertEqual(frozen[0].row_id, 1)

    def test_prepare_and_finalize_own_connection_and_exact_row(self):
        database = FakeDatabase()
        game = SimpleNamespace(id=2, guild_id=3, is_pending=False)
        row = SimpleNamespace(
            id=1,
            game=game,
            channel_id=4,
            message_id=5,
            delete_instance=mock.Mock(),
        )
        model = SimpleNamespace(get_or_none=mock.Mock(return_value=row))
        with mock.patch.object(workers.models, 'db', database), \
             mock.patch.object(
                 workers.models, 'TeamServerBroadcastMessage', model
             ):
            prepared = workers.prepare_started_broadcast(target())
            finalized = workers.finalize_started_broadcast(target())

        self.assertEqual(prepared.status, workers.READY)
        self.assertEqual(finalized.status, workers.FINALIZED)
        row.delete_instance.assert_called_once_with()
        self.assertEqual(database.connections, 2)
        self.assertEqual(database.closed, 2)
        self.assertEqual(database.commits, 1)

    def test_finalization_failure_rolls_back_and_escapes(self):
        database = FakeDatabase()
        game = SimpleNamespace(id=2, guild_id=3, is_pending=False)
        row = SimpleNamespace(
            id=1,
            game=game,
            channel_id=4,
            message_id=5,
            delete_instance=mock.Mock(side_effect=RuntimeError('delete failed')),
        )
        model = SimpleNamespace(get_or_none=mock.Mock(return_value=row))
        with mock.patch.object(workers.models, 'db', database), \
             mock.patch.object(
                 workers.models, 'TeamServerBroadcastMessage', model
             ), self.assertRaisesRegex(RuntimeError, 'delete failed'):
            workers.finalize_started_broadcast(target())
        self.assertEqual(database.rollbacks, 1)

    async def test_cancelled_wait_drains_running_worker(self):
        started = threading.Event()
        release = threading.Event()

        def slow(_target):
            started.set()
            release.wait(timeout=2)
            return workers.BroadcastTargetState(workers.GONE, None)

        with mock.patch.object(
            workers, 'prepare_started_broadcast', side_effect=slow
        ):
            task = asyncio.create_task(
                workers.run_prepare_started_broadcast(target())
            )
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.002)
            task.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task


class BroadcastServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.target = target()
        self.member = object()
        self.message = SimpleNamespace(
            content='Open game invitation',
            guild=SimpleNamespace(me=self.member),
            edit=mock.AsyncMock(),
            remove_reaction=mock.AsyncMock(),
        )
        self.channel = SimpleNamespace(
            fetch_message=mock.AsyncMock(return_value=self.message)
        )
        self.bot = SimpleNamespace(
            user=self.member,
            get_channel=mock.Mock(return_value=self.channel),
            fetch_channel=mock.AsyncMock(return_value=self.channel),
        )
        self.lock = mock.patch.object(service.utilities, 'lock_game')
        self.unlock = mock.patch.object(service.utilities, 'unlock_game')
        self.lock.start()
        self.unlock.start()
        self.addCleanup(self.lock.stop)
        self.addCleanup(self.unlock.stop)

    def prepared(self):
        return workers.BroadcastTargetState(workers.READY, self.target)

    async def reconcile(self, *, finalize_status=workers.FINALIZED):
        with mock.patch.object(
            service.game_broadcast_workers,
            'run_prepare_started_broadcast',
            new=mock.AsyncMock(return_value=self.prepared()),
        ), mock.patch.object(
            service.game_broadcast_workers,
            'run_finalize_started_broadcast',
            new=mock.AsyncMock(return_value=workers.BroadcastFinalizationResult(
                finalize_status,
                self.target,
            )),
        ) as finalize:
            outcome = await service.reconcile_started_broadcast(
                bot=self.bot,
                target=self.target,
            )
        return outcome, finalize

    async def test_success_updates_then_finalizes(self):
        outcome, finalize = await self.reconcile()
        self.assertEqual(outcome.status, service.RECONCILED)
        self.message.edit.assert_awaited_once_with(
            content=(
                '~~Open game invitation~~\n'
                f'{service.STARTED_MARKER}'
            )
        )
        self.message.remove_reaction.assert_awaited_once_with(
            service.settings.emoji_join_game,
            self.member,
        )
        finalize.assert_awaited_once_with(self.target)

    async def test_retry_is_idempotent(self):
        self.message.content = (
            f'~~Open game invitation~~\n{service.STARTED_MARKER}'
        )
        outcome, _finalize = await self.reconcile()
        self.assertEqual(outcome.status, service.RECONCILED)
        self.message.edit.assert_not_awaited()
        self.message.remove_reaction.assert_awaited_once()

    async def test_deleted_marker_is_terminal_without_overwrite(self):
        self.message.content = (
            f'~~Open game invitation~~\n{service.DELETED_MARKER}'
        )
        outcome, finalize = await self.reconcile()
        self.assertEqual(outcome.status, service.RECONCILED)
        self.message.edit.assert_not_awaited()
        self.message.remove_reaction.assert_not_awaited()
        finalize.assert_awaited_once()

    async def test_confirmed_missing_message_finalizes(self):
        self.channel.fetch_message.side_effect = discord_error(
            discord.NotFound,
            status=404,
            code=10008,
        )
        outcome, finalize = await self.reconcile()
        self.assertEqual(outcome.status, service.RECONCILED)
        self.assertIn('no longer exists', outcome.detail)
        finalize.assert_awaited_once()

    async def test_forbidden_retains_row(self):
        self.channel.fetch_message.side_effect = discord_error(
            discord.Forbidden,
            status=403,
            code=50013,
        )
        outcome, finalize = await self.reconcile()
        self.assertEqual(outcome.status, service.RETAINED)
        finalize.assert_not_awaited()

    async def test_database_finalization_failure_retains_row(self):
        with mock.patch.object(
            service.game_broadcast_workers,
            'run_prepare_started_broadcast',
            new=mock.AsyncMock(return_value=self.prepared()),
        ), mock.patch.object(
            service.game_broadcast_workers,
            'run_finalize_started_broadcast',
            new=mock.AsyncMock(side_effect=RuntimeError('database down')),
        ):
            outcome = await service.reconcile_started_broadcast(
                bot=self.bot,
                target=self.target,
            )
        self.assertEqual(outcome.status, service.RETAINED)

    async def test_locked_game_defers_without_discord_or_database(self):
        with mock.patch.object(
            service.utilities,
            'lock_game',
            side_effect=service.exceptions.RecordLocked('busy'),
        ), mock.patch.object(
            service.game_broadcast_workers,
            'run_prepare_started_broadcast',
            new=mock.AsyncMock(),
        ) as prepare:
            outcome = await service.reconcile_started_broadcast(
                bot=self.bot,
                target=self.target,
            )
        self.assertEqual(outcome.status, service.DEFERRED)
        prepare.assert_not_awaited()
        self.channel.fetch_message.assert_not_awaited()

    async def test_one_target_failure_does_not_suppress_later_target(self):
        later = target(row_id=9, message_id=10)
        first = service.BroadcastReconciliationOutcome(
            self.target, service.RETAINED, 'failed'
        )
        second = service.BroadcastReconciliationOutcome(
            later, service.RECONCILED, 'done'
        )
        with mock.patch.object(
            service,
            'reconcile_started_broadcast',
            new=mock.AsyncMock(side_effect=(first, second)),
        ) as reconcile:
            outcomes = await service.reconcile_started_broadcasts(
                bot=self.bot,
                targets=(self.target, later),
            )
        self.assertEqual(outcomes, (first, second))
        self.assertEqual(reconcile.await_count, 2)

    async def test_hourly_cycle_sends_one_bounded_staff_summary(self):
        retained = service.BroadcastReconciliationOutcome(
            self.target, service.RETAINED, 'forbidden'
        )
        log_channel = SimpleNamespace(send=mock.AsyncMock())
        guild = SimpleNamespace(
            id=3,
            get_channel=mock.Mock(return_value=log_channel),
        )
        discovery = workers.BroadcastDiscoveryResult(
            guild_id=3,
            targets=(self.target,),
            truncated=True,
        )
        with mock.patch.object(
            service.game_broadcast_workers,
            'run_discover_started_broadcasts',
            new=mock.AsyncMock(return_value=discovery),
        ), mock.patch.object(
            service,
            'reconcile_started_broadcasts',
            new=mock.AsyncMock(return_value=(retained,)),
        ), mock.patch.object(
            service.settings,
            'guild_setting',
            return_value=55,
        ):
            cycle = await service.reconcile_started_broadcasts_for_guild(
                bot=self.bot,
                guild=guild,
            )
        self.assertTrue(cycle.truncated)
        log_channel.send.assert_awaited_once()
        self.assertIn('4/5', log_channel.send.await_args.args[0])


class BroadcastIntegrationBoundaryTests(unittest.TestCase):
    def test_start_result_has_primitive_broadcast_plan(self):
        field = start_workers.StartResult.__dataclass_fields__[
            'broadcast_targets'
        ]
        self.assertEqual(field.default, ())

    def test_legacy_async_model_helpers_are_retired(self):
        self.assertFalse(hasattr(models.Game, 'update_external_broadcasts'))
        self.assertFalse(
            hasattr(models.TeamServerBroadcastMessage, 'fetch_message')
        )

    def test_hourly_callback_delegates_without_direct_database_use(self):
        source = inspect.getsource(
            matchmaking.matchmaking.task_reconcile_started_broadcasts.coro
        )
        self.assertIn(
            'reconcile_started_broadcasts_for_guild',
            source,
        )
        self.assertNotIn('models.', source)
        self.assertNotIn('db.', source)


if __name__ == '__main__':
    unittest.main()
