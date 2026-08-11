"""Focused H8 startup identity and ban-reconciliation coverage."""

import asyncio
from contextlib import AbstractContextManager
from dataclasses import FrozenInstanceError
import inspect
from types import SimpleNamespace
import sys
import threading
import unittest
from unittest import mock

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.startup_ban_workers')
bot_module = import_offline_runtime('bot')


class Database:
    def __init__(self):
        self.opens = 0
        self.closes = 0
        self.commits = 0
        self.rollbacks = 0

    def connection_context(self):
        database = self

        class Connection(AbstractContextManager):
            def __enter__(self):
                database.opens += 1

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


class Field:
    def __init__(self, name):
        self.name = name

    def in_(self, values):
        return self.name, tuple(values)


class Query:
    def __init__(self, events, values, result, failure=None):
        self.events = events
        self.values = values
        self.result = result
        self.failure = failure
        self.predicate = None

    def where(self, predicate):
        self.predicate = predicate
        return self

    def execute(self):
        self.events.append((self.values, self.predicate))
        if self.failure:
            raise self.failure
        return self.result


def request(**overrides):
    values = dict(
        discord_ids=(111, 222),
        polytopia_ids=('alpha', 'beta'),
    )
    values.update(overrides)
    return workers.StartupBanReconciliationRequest(**values)


class StartupBanWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_is_frozen_and_bounded(self):
        value = request()
        with self.assertRaises(FrozenInstanceError):
            value.discord_ids = ()
        with self.assertRaises(workers.StartupBanReconciliationError):
            workers.reconcile_startup_bans(request(
                discord_ids=tuple(
                    range(1, workers.MAX_DISCORD_BANS + 2)
                ),
            ))
        with self.assertRaises(workers.StartupBanReconciliationError):
            workers.reconcile_startup_bans(request(
                polytopia_ids=tuple(
                    str(value)
                    for value in range(workers.MAX_POLYTOPIA_BANS + 1)
                ),
            ))
        with self.assertRaises(workers.StartupBanReconciliationError):
            workers.reconcile_startup_bans(request(discord_ids=(1, 1)))
        with self.assertRaises(workers.StartupBanReconciliationError):
            workers.reconcile_startup_bans(request(polytopia_ids=('',)))
        with self.assertRaises(workers.StartupBanReconciliationError):
            workers.reconcile_startup_bans(request(
                polytopia_ids=('x' * (workers.MAX_POLYTOPIA_ID_LENGTH + 1),),
            ))

    async def test_worker_owns_one_atomic_connection_and_exact_filters(self):
        database = Database()
        events = []
        results = iter((5, 2, 1))

        def update(**values):
            return Query(events, values, next(results))

        member = SimpleNamespace(
            discord_id=Field('discord_id'),
            polytopia_id=Field('polytopia_id'),
            update=update,
        )
        with mock.patch.object(
            workers,
            '_load_models',
            return_value=SimpleNamespace(db=database, DiscordMember=member),
        ):
            result = await workers.run_startup_ban_reconciliation(request())

        self.assertEqual(
            result,
            workers.StartupBanReconciliationResult(5, 2, 1),
        )
        self.assertEqual(
            events,
            [
                ({'is_banned': False}, None),
                ({'is_banned': True}, ('discord_id', (111, 222))),
                ({'is_banned': True}, ('polytopia_id', ('alpha', 'beta'))),
            ],
        )
        self.assertEqual((database.opens, database.closes), (1, 1))
        self.assertEqual((database.commits, database.rollbacks), (1, 0))

    async def test_write_failure_rolls_back_and_closes_connection(self):
        database = Database()
        events = []
        calls = 0

        def update(**values):
            nonlocal calls
            calls += 1
            return Query(
                events,
                values,
                1,
                RuntimeError('write failed') if calls == 3 else None,
            )

        member = SimpleNamespace(
            discord_id=Field('discord_id'),
            polytopia_id=Field('polytopia_id'),
            update=update,
        )
        with mock.patch.object(
            workers,
            '_load_models',
            return_value=SimpleNamespace(db=database, DiscordMember=member),
        ):
            with self.assertRaisesRegex(RuntimeError, 'write failed'):
                await workers.run_startup_ban_reconciliation(request())
        self.assertEqual((database.opens, database.closes), (1, 1))
        self.assertEqual((database.commits, database.rollbacks), (0, 1))

    async def test_slow_worker_keeps_loop_responsive_and_cancellation_drains(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def slow(_request):
            started.set()
            release.wait(timeout=2)
            finished.set()
            return workers.StartupBanReconciliationResult(1, 1, 1)

        with mock.patch.object(
            workers, 'reconcile_startup_bans', side_effect=slow,
        ):
            task = asyncio.create_task(
                workers.run_startup_ban_reconciliation(request())
            )
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.002)
            heartbeat = asyncio.Event()
            asyncio.get_running_loop().call_later(0.01, heartbeat.set)
            await asyncio.wait_for(heartbeat.wait(), 0.2)
            task.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(finished.is_set())


class StartupIdentityOrderingTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self):
        instance = bot_module.MyBot()
        instance._connection.user = SimpleNamespace(id=123)
        return instance

    async def test_wrong_authenticated_id_has_zero_startup_effects(self):
        instance = self.make_bot()
        reconcile = mock.AsyncMock()
        connect = mock.Mock()
        ensure_images = mock.Mock()
        load_extension = mock.AsyncMock()
        profile = SimpleNamespace(
            bullet_enabled=False,
            validate_logged_in_bot=mock.Mock(
                side_effect=RuntimeError('wrong bot identity')
            ),
        )
        utilities = SimpleNamespace(connect=connect)
        try:
            with mock.patch.object(
                bot_module.settings, 'runtime_profile', profile,
            ), mock.patch.object(
                instance, '_reconcile_startup_bans', reconcile,
            ), mock.patch.object(
                bot_module.image_storage,
                'ensure_image_directories',
                ensure_images,
            ), mock.patch.object(
                instance, 'load_extension', load_extension,
            ), mock.patch.dict(
                sys.modules, {'modules.utilities': utilities},
            ):
                with self.assertRaisesRegex(RuntimeError, 'wrong bot identity'):
                    await instance.setup_hook()
        finally:
            await instance.close()

        reconcile.assert_not_awaited()
        connect.assert_not_called()
        ensure_images.assert_not_called()
        load_extension.assert_not_awaited()
        self.assertFalse(instance._startup_identity_validated)
        self.assertFalse(instance._startup_bans_reconciled)

    async def test_expected_identity_reconciles_once_before_other_effects(self):
        instance = self.make_bot()
        events = []
        profile = SimpleNamespace(
            bullet_enabled=False,
            database_name='polytopia_dev',
            database_user='polybot_dev',
            database_password='secret',
            database_host='localhost',
            database_port=5432,
            validate_logged_in_bot=lambda _bot_id: events.append('identity'),
        )
        schema_result = SimpleNamespace(
            database_name='polytopia_dev',
            database_user='polybot_dev',
            verified_tables=('game',),
            winner_foreign_key_verified=True,
        )
        schema = SimpleNamespace(
            StartupSchemaPreflightRequest=lambda **kwargs: SimpleNamespace(
                **kwargs
            ),
            run_startup_schema_preflight=mock.AsyncMock(
                side_effect=lambda _request: (
                    events.append('schema') or schema_result
                )
            ),
        )
        result = SimpleNamespace(
            reset_rows=3,
            discord_rows=2,
            polytopia_rows=1,
        )
        worker = SimpleNamespace(
            StartupBanReconciliationRequest=lambda **kwargs: SimpleNamespace(
                **kwargs
            ),
            run_startup_ban_reconciliation=mock.AsyncMock(
                side_effect=lambda _request: (
                    events.append('bans') or result
                )
            ),
        )
        utilities = SimpleNamespace(
            connect=lambda: events.append('connect')
        )

        async def load_extension(_name):
            events.append('extension')

        try:
            with mock.patch.object(
                bot_module.settings, 'runtime_profile', profile,
            ), mock.patch.object(
                bot_module.settings, 'discord_id_ban_list', [1, 1, 2],
            ), mock.patch.object(
                bot_module.settings, 'poly_id_ban_list', ['a', 'a', 'b'],
            ), mock.patch.object(
                bot_module.image_storage,
                'ensure_image_directories',
                side_effect=lambda: events.append('images'),
            ), mock.patch.object(
                bot_module.beta_operations,
                'beta_control_enabled',
                return_value=False,
            ), mock.patch.object(
                instance, 'load_extension', side_effect=load_extension,
            ), mock.patch.dict(
                sys.modules,
                {
                    'modules.startup_schema_preflight': schema,
                    'modules.startup_ban_workers': worker,
                    'modules.utilities': utilities,
                },
            ):
                await instance.setup_hook()
                await instance._reconcile_startup_bans()
        finally:
            await instance.close()

        self.assertEqual(
            events[:5],
            ['identity', 'schema', 'bans', 'connect', 'images'],
        )
        self.assertEqual(events.count('schema'), 1)
        self.assertEqual(events.count('bans'), 1)
        self.assertTrue(instance._startup_schema_preflight_complete)
        self.assertEqual(worker.run_startup_ban_reconciliation.await_count, 1)
        sent_request = worker.run_startup_ban_reconciliation.await_args.args[0]
        self.assertEqual(sent_request.discord_ids, (1, 2))
        self.assertEqual(sent_request.polytopia_ids, ('a', 'b'))
        self.assertTrue(instance._startup_identity_validated)
        self.assertTrue(instance._startup_bans_reconciled)

    async def test_incomplete_schema_aborts_before_ban_reconciliation(self):
        instance = self.make_bot()
        profile = SimpleNamespace(
            bullet_enabled=False,
            database_name='polytopia_dev',
            database_user='polybot_dev',
            database_password='secret',
            database_host='localhost',
            database_port=5432,
            validate_logged_in_bot=lambda _bot_id: None,
        )
        schema = SimpleNamespace(
            StartupSchemaPreflightRequest=lambda **kwargs: SimpleNamespace(
                **kwargs
            ),
            run_startup_schema_preflight=mock.AsyncMock(
                side_effect=RuntimeError('missing required tables')
            ),
        )
        reconcile = mock.AsyncMock()
        try:
            with mock.patch.object(
                bot_module.settings, 'runtime_profile', profile,
            ), mock.patch.object(
                instance, '_reconcile_startup_bans', reconcile,
            ), mock.patch.dict(
                sys.modules, {'modules.startup_schema_preflight': schema},
            ):
                with self.assertRaisesRegex(
                    RuntimeError, 'missing required tables'
                ):
                    await instance.setup_hook()
        finally:
            await instance.close()

        reconcile.assert_not_awaited()
        self.assertTrue(instance._startup_identity_validated)
        self.assertFalse(instance._startup_schema_preflight_complete)
        self.assertFalse(instance._startup_bans_reconciled)

    async def test_development_persona_reconciliation_runs_on_every_ready_cycle(self):
        instance = self.make_bot()
        guild = SimpleNamespace(id=300)
        instance.get_guild = mock.Mock(return_value=guild)
        persona_module = SimpleNamespace(
            manifest=lambda: SimpleNamespace(guild_id=300),
            revoke_members_on_startup=mock.AsyncMock(side_effect=(2, 0)),
        )
        try:
            with mock.patch.object(
                bot_module.settings,
                'runtime_profile',
                SimpleNamespace(environment='development'),
            ), mock.patch.dict(
                sys.modules,
                {'modules.beta_lab_personas': persona_module},
            ):
                self.assertEqual(await instance._revoke_beta_lab_personas(), 2)
                self.assertEqual(await instance._revoke_beta_lab_personas(), 0)
        finally:
            await instance.close()
        self.assertEqual(persona_module.revoke_members_on_startup.await_count, 2)

    async def test_production_never_enters_beta_persona_reconciliation(self):
        instance = self.make_bot()
        try:
            with mock.patch.object(
                bot_module.settings,
                'runtime_profile',
                SimpleNamespace(environment='production'),
            ), mock.patch.object(
                instance, 'get_guild',
            ) as get_guild:
                self.assertEqual(await instance._revoke_beta_lab_personas(), 0)
        finally:
            await instance.close()
        get_guild.assert_not_called()

    def test_ordinary_init_has_no_pre_identity_database_import_or_connect(self):
        source = inspect.getsource(bot_module)
        init_source = inspect.getsource(bot_module.init_bot)
        imports_before_main = source[:source.index('def main')]
        self.assertNotIn('modules.models', imports_before_main)
        self.assertNotIn('modules.utilities', imports_before_main)
        self.assertNotIn('modules.startup_schema_preflight', imports_before_main)
        self.assertNotIn('\n    utilities.connect()', init_source)


if __name__ == '__main__':
    unittest.main()
