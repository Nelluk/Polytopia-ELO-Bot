"""Focused coverage for the shared asynchronous prefix registration check."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

from tests.test_newgame_worker import import_offline_runtime


models = import_offline_runtime('modules.models')
registration_checks = import_offline_runtime('modules.registration_checks')


class FakeConnectionContext(AbstractContextManager):
    def __init__(self, events):
        self.events = events

    def __enter__(self):
        self.events.append('open')
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.events.append('close')
        return False


class FakeDatabase:
    def __init__(self, events):
        self.events = events

    def connection_context(self):
        return FakeConnectionContext(self.events)


class FakeQuery:
    def __init__(self, events, registered):
        self.events = events
        self.registered = registered

    def where(self, expression):
        self.events.append('where')
        return self

    def exists(self):
        self.events.append('exists')
        return self.registered


class RegistrationWorkerTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    async def wait_for_thread_event(event: threading.Event):
        for _ in range(1000):
            if event.is_set():
                return
            await asyncio.sleep(0.001)
        raise AssertionError('worker thread did not start')

    def test_worker_owns_connection_and_returns_frozen_result(self):
        events = []
        query = FakeQuery(events, True)
        table = SimpleNamespace(
            discord_id=mock.MagicMock(),
            select=mock.Mock(return_value=query),
        )
        with mock.patch.object(
            registration_checks.models,
            'db',
            FakeDatabase(events),
        ), mock.patch.object(
            registration_checks.models,
            'DiscordMember',
            table,
        ):
            result = registration_checks.load_registration_check(
                registration_checks.RegistrationCheckRequest(discord_id=100)
            )

        self.assertEqual(events, ['open', 'where', 'exists', 'close'])
        self.assertEqual(result.discord_id, 100)
        self.assertTrue(result.registered)
        with self.assertRaises(FrozenInstanceError):
            result.registered = False

    async def test_slow_read_keeps_event_loop_responsive(self):
        started = threading.Event()
        release = threading.Event()
        executor = ThreadPoolExecutor(max_workers=1)

        def slow_load(request):
            started.set()
            release.wait(timeout=2)
            return registration_checks.RegistrationCheckResult(
                discord_id=request.discord_id,
                registered=True,
            )

        try:
            with mock.patch.object(
                registration_checks,
                'load_registration_check',
                side_effect=slow_load,
            ):
                task = asyncio.create_task(
                    registration_checks.run_registration_check(
                        registration_checks.RegistrationCheckRequest(100),
                        executor=executor,
                    )
                )
                await self.wait_for_thread_event(started)
                ticked = False

                async def tick():
                    nonlocal ticked
                    await asyncio.sleep(0)
                    ticked = True

                await tick()
                self.assertTrue(ticked)
                self.assertFalse(task.done())
                release.set()
                result = await task
            self.assertTrue(result.registered)
        finally:
            release.set()
            executor.shutdown(wait=True)

    async def test_cancellation_drains_submitted_read(self):
        started = threading.Event()
        release = threading.Event()
        executor = ThreadPoolExecutor(max_workers=1)

        def slow_load(request):
            started.set()
            release.wait(timeout=2)
            return registration_checks.RegistrationCheckResult(
                discord_id=request.discord_id,
                registered=False,
            )

        try:
            with mock.patch.object(
                registration_checks,
                'load_registration_check',
                side_effect=slow_load,
            ):
                task = asyncio.create_task(
                    registration_checks.run_registration_check(
                        registration_checks.RegistrationCheckRequest(100),
                        executor=executor,
                    )
                )
                await self.wait_for_thread_event(started)
                task.cancel()
                task.cancel()
                await asyncio.sleep(0.01)
                self.assertFalse(task.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        finally:
            release.set()
            executor.shutdown(wait=True)


class RegistrationDecoratorTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def predicate():
        async def callback(ctx):
            return ctx

        decorated = models.is_registered_member()(callback)
        return decorated.__commands_checks__[0]

    @staticmethod
    def context(*, invoked_with='ping', command_name='ping'):
        return SimpleNamespace(
            author=SimpleNamespace(id=100),
            invoked_with=invoked_with,
            command=SimpleNamespace(name=command_name),
            prefix='$',
            send=mock.AsyncMock(),
        )

    async def test_registered_member_passes_without_message(self):
        ctx = self.context()
        with mock.patch.object(
            registration_checks,
            'run_registration_check',
            new=mock.AsyncMock(return_value=
                registration_checks.RegistrationCheckResult(100, True)),
        ) as reader:
            allowed = await self.predicate()(ctx)
        self.assertTrue(allowed)
        reader.assert_awaited_once_with(
            registration_checks.RegistrationCheckRequest(discord_id=100)
        )
        ctx.send.assert_not_awaited()

    async def test_unregistered_member_gets_existing_public_guidance(self):
        ctx = self.context()
        with mock.patch.object(
            registration_checks,
            'run_registration_check',
            new=mock.AsyncMock(return_value=
                registration_checks.RegistrationCheckResult(100, False)),
        ):
            allowed = await self.predicate()(ctx)
        self.assertFalse(allowed)
        ctx.send.assert_awaited_once_with(
            'This command requires bot registration first. Type '
            '__`$setname YOUR POLYTOPIA NAME`__ or use '
            '`/player register` to set your account-wide canonical name.'
        )

    async def test_help_for_another_command_suppresses_guidance(self):
        ctx = self.context(invoked_with='help', command_name='ping')
        with mock.patch.object(
            registration_checks,
            'run_registration_check',
            new=mock.AsyncMock(return_value=
                registration_checks.RegistrationCheckResult(100, False)),
        ):
            allowed = await self.predicate()(ctx)
        self.assertFalse(allowed)
        ctx.send.assert_not_awaited()

    async def test_database_failure_propagates_without_false_denial(self):
        ctx = self.context()
        with mock.patch.object(
            registration_checks,
            'run_registration_check',
            new=mock.AsyncMock(side_effect=RuntimeError('database down')),
        ):
            with self.assertRaisesRegex(RuntimeError, 'database down'):
                await self.predicate()(ctx)
        ctx.send.assert_not_awaited()

    def test_all_existing_call_sites_retain_shared_decorator(self):
        root = Path(__file__).resolve().parents[1]
        expected = {
            'modules/games.py': 10,
            'modules/matchmaking.py': 4,
            'modules/misc.py': 2,
            'modules/bullet.py': 2,
        }
        actual = {
            relative: (root / relative).read_text(encoding='utf-8').count(
                '@models.is_registered_member()'
            )
            for relative in expected
        }
        self.assertEqual(actual, expected)
        self.assertEqual(sum(actual.values()), 18)


if __name__ == '__main__':
    unittest.main()
