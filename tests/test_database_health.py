"""Focused offline coverage for the event-loop database health boundary."""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
import unittest
from unittest import mock

import peewee

from tests.test_newgame_worker import import_offline_runtime


health = import_offline_runtime('modules.database_health')
bot_module = import_offline_runtime('bot')


class Cursor:
    def __init__(self, events):
        self.events = events

    def close(self):
        self.events.append('cursor.close')


class FakeDatabase:
    def __init__(self, probes=(), *, closed=False, transaction=False):
        self.closed = closed
        self.transaction = transaction
        self.probes = list(probes)
        self.events = []

    def is_closed(self):
        return self.closed

    def in_transaction(self):
        return self.transaction

    def connect(self, *, reuse_if_open):
        self.events.append(('connect', reuse_if_open))
        self.closed = False

    def close(self):
        self.events.append('close')
        self.closed = True

    def execute_sql(self, sql):
        self.events.append(('execute', sql))
        result = self.probes.pop(0) if self.probes else None
        if isinstance(result, BaseException):
            raise result
        return Cursor(self.events)


class ConnectionHealthTests(unittest.TestCase):
    def test_healthy_open_probe_closes_cursor_and_does_not_reconnect(self):
        database = FakeDatabase()

        self.assertTrue(health.ensure_connection(database))
        self.assertEqual(
            database.events,
            [('execute', 'SELECT 1'), 'cursor.close'],
        )

    def test_closed_connection_connects_then_probes(self):
        database = FakeDatabase(closed=True)

        health.ensure_connection(database)

        self.assertEqual(
            database.events,
            [
                ('connect', True),
                ('execute', 'SELECT 1'),
                'cursor.close',
            ],
        )

    def test_stale_connection_resets_reconnects_and_reprobes(self):
        database = FakeDatabase(
            [peewee.OperationalError('server closed connection'), None]
        )

        health.ensure_connection(database)

        self.assertEqual(
            database.events,
            [
                ('execute', 'SELECT 1'),
                'close',
                ('connect', True),
                ('execute', 'SELECT 1'),
                'cursor.close',
            ],
        )

    def test_failed_second_probe_propagates_without_a_third_sql_attempt(self):
        database = FakeDatabase(
            [
                peewee.InterfaceError('stale connection'),
                peewee.OperationalError('database still down'),
            ]
        )

        with self.assertRaisesRegex(peewee.OperationalError, 'still down'):
            health.ensure_connection(database)

        self.assertEqual(
            [event for event in database.events if isinstance(event, tuple) and event[0] == 'execute'],
            [('execute', 'SELECT 1'), ('execute', 'SELECT 1')],
        )

    def test_active_transaction_refuses_reset_or_reconnect(self):
        database = FakeDatabase(
            [peewee.OperationalError('transaction connection failed')],
            transaction=True,
        )

        with self.assertRaises(peewee.OperationalError):
            health.ensure_connection(database)

        self.assertEqual(database.events, [('execute', 'SELECT 1')])

    def test_probe_never_retries_arbitrary_sql(self):
        database = FakeDatabase(
            [peewee.OperationalError('down'), None]
        )

        health.ensure_connection(database)

        self.assertTrue(all(
            event[1] == 'SELECT 1'
            for event in database.events
            if isinstance(event, tuple) and event[0] == 'execute'
        ))


class WatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_resets_failure_counter(self):
        bot = SimpleNamespace(close=mock.AsyncMock())
        watchdog = health.DatabaseWatchdog(
            bot, database=object(), interval=0.001, failure_threshold=3,
        )
        calls = 0
        recovered = asyncio.Event()

        def probe(_database):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise peewee.OperationalError('temporary')
            watchdog.consecutive_failures = 1
            recovered.set()

        with mock.patch.object(health, 'ensure_connection', side_effect=probe):
            task = asyncio.create_task(watchdog.run())
            await asyncio.wait_for(recovered.wait(), timeout=0.2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(watchdog.consecutive_failures, 0)
        bot.close.assert_not_awaited()

    async def test_threshold_sets_failure_exit_status_and_closes_once(self):
        bot = SimpleNamespace(
            _restart_exit_status=None,
            close=mock.AsyncMock(),
        )
        watchdog = health.DatabaseWatchdog(
            bot, database=object(), interval=0.001, failure_threshold=2,
        )
        with mock.patch.object(
            health,
            'ensure_connection',
            side_effect=peewee.OperationalError('database unavailable'),
        ):
            await asyncio.wait_for(watchdog.run(), timeout=0.2)
        self.assertEqual(
            bot._restart_exit_status,
            health.DATABASE_FAILURE_EXIT_STATUS,
        )
        bot.close.assert_awaited_once()

    async def test_stop_cancels_task_and_self_shutdown_does_not_self_cancel(self):
        bot = SimpleNamespace(_restart_exit_status=None, close=mock.AsyncMock())
        watchdog = health.DatabaseWatchdog(bot, interval=60)
        task = watchdog.start()
        await asyncio.sleep(0)
        await watchdog.stop()
        self.assertTrue(task.cancelled())

        # The bot's close implementation calls stop(); a watchdog-triggered
        # close must therefore treat its own task as already stopping.
        bot = bot_module.MyBot()
        watchdog = health.DatabaseWatchdog(bot, interval=0.001, failure_threshold=1)
        bot._database_watchdog = watchdog
        with mock.patch.object(
            health,
            'ensure_connection',
            side_effect=peewee.OperationalError('database unavailable'),
        ):
            await asyncio.wait_for(watchdog.run(), timeout=0.2)
        self.assertTrue(bot._close_complete)
        self.assertEqual(bot.restart_exit_status, health.DATABASE_FAILURE_EXIT_STATUS)


class PrefixRegistrationTests(unittest.TestCase):
    def test_check_once_registration_precedes_specific_checks_and_before_invoke_has_no_connect(self):
        class Loop:
            def create_task(self, coroutine):
                coroutine.close()
                return mock.Mock()

        instance = bot_module.init_bot(loop=Loop(), args=['--skip_tasks'])
        try:
            self.assertEqual(
                [check.__name__ for check in instance._check_once],
                ['database_health_check'],
            )
            self.assertEqual(
                [check.__name__ for check in instance._checks],
                ['globally_block_dms', 'restrict_banned_users', 'cooldown_check'],
            )
            source = inspect.getsource(bot_module.init_bot)
            self.assertNotIn('utilities.connect()', source)
            self.assertLess(
                source.index('check_once'), source.index('before_invoke'),
            )
        finally:
            asyncio.run(instance.close())

    def test_prefix_health_failure_is_routed_as_command_error_with_original(self):
        class Loop:
            def create_task(self, coroutine):
                coroutine.close()
                return mock.Mock()

        instance = bot_module.init_bot(loop=Loop(), args=['--skip_tasks'])
        try:
            check = instance._check_once[0]
            failure = peewee.OperationalError('database unavailable')
            with mock.patch.object(
                bot_module.importlib,
                'import_module',
                return_value=SimpleNamespace(
                    CONNECTION_ERRORS=(peewee.OperationalError, peewee.InterfaceError),
                    ensure_connection=mock.Mock(side_effect=failure),
                ),
            ):
                with self.assertRaises(bot_module.commands.CommandInvokeError) as raised:
                    asyncio.run(check(SimpleNamespace()))
            self.assertIs(raised.exception.original, failure)
        finally:
            asyncio.run(instance.close())


if __name__ == '__main__':
    unittest.main()
