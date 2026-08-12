"""Focused offline coverage for P10.4 development shadow reads."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import FrozenInstanceError
import importlib
import inspect
import os
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

from peewee import SchemaManager
from playhouse.postgres_ext import PostgresqlExtDatabase

from modules import guild_configuration_shadow as shadow
from modules import guild_configuration_storage as storage
from modules.guild_configuration_schema import (
    document_digest,
    document_to_mapping,
    validate_document,
)
from tests import test_guild_configuration_storage as fixtures


def import_offline_runtime(module_name):
    with mock.patch.dict(
        os.environ, {'POLYBOT_ENV': 'development'}, clear=False,
    ), mock.patch.object(
        PostgresqlExtDatabase, 'connect', return_value=True,
    ), mock.patch.object(
        PostgresqlExtDatabase, 'close', return_value=True,
    ), mock.patch.object(
        PostgresqlExtDatabase, 'create_tables',
    ), mock.patch.object(
        SchemaManager, 'create_foreign_key',
    ):
        return importlib.import_module(module_name)


bot_module = import_offline_runtime('bot')
GUILD_ID = fixtures.GUILD_ID


def profile():
    return SimpleNamespace(
        environment='development',
        database_name='polytopia_dev',
        database_user='polybot_dev',
        database_password='secret',
        database_host='localhost',
        database_port=5432,
        expected_bot_id=storage.DEVELOPMENT_BETA_APPLICATION_ID,
        background_tasks_enabled=False,
        api_enabled=False,
        bullet_enabled=False,
        allowed_guild_ids=(GUILD_ID,),
        server_settings=fixtures.server_settings(),
        guild_configuration_source='static',
    )


def request():
    return shadow.request_from_profile(
        profile=profile(),
        expected_bundle=fixtures.bundle(),
    )


def stored_row(*, document=None, state='active', source_digest=None):
    imported = fixtures.bundle().imports[0]
    document = imported.document if document is None else document
    return (
        GUILD_ID,
        storage.STORAGE_SCHEMA_VERSION,
        state,
        1,
        1,
        1,
        document.schema_version,
        document_to_mapping(document),
        document_digest(document),
        imported.source_digest if source_digest is None else source_digest,
    )


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.one = None
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, parameters=None):
        self.statements.append((statement, parameters))
        if statement == 'SHOW transaction_read_only':
            self.one = ('on',)
        elif statement == 'SELECT current_database(), current_user':
            self.one = ('polytopia_dev', 'polybot_dev')

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows):
        self.cursor_value = FakeCursor(rows)
        self.sessions = []
        self.closed = False
        self.worker_threads = []

    def set_session(self, **values):
        self.worker_threads.append(threading.get_ident())
        self.sessions.append(values)

    def cursor(self):
        return self.cursor_value

    def close(self):
        self.worker_threads.append(threading.get_ident())
        self.closed = True


def exact_inventory():
    return storage.SchemaInventory(
        tuple(sorted(storage.STORAGE_TABLES)),
        storage.EXPECTED_COLUMNS,
        storage.EXPECTED_CONSTRAINTS,
    )


class ShadowWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def run_rows(self, rows):
        connection = FakeConnection(rows)
        with mock.patch.object(
            shadow, '_connect', return_value=connection,
        ), mock.patch.object(
            storage, 'inspect_schema_inventory', return_value=exact_inventory(),
        ):
            result = await shadow.run_shadow_comparison(request())
        return result, connection

    async def test_exact_effective_document_matches_on_read_only_owned_connection(self):
        event_thread = threading.get_ident()
        result, connection = await self.run_rows((stored_row(),))

        self.assertEqual(result.status, shadow.STATUS_MATCHED)
        self.assertTrue(result.promotion_ready)
        self.assertEqual(result.matched_guild_ids, (GUILD_ID,))
        self.assertEqual(result.mismatches, ())
        self.assertEqual(
            connection.sessions,
            [{'readonly': True, 'autocommit': True}],
        )
        self.assertTrue(connection.closed)
        self.assertTrue(all(value != event_thread for value in connection.worker_threads))
        with self.assertRaises(FrozenInstanceError):
            result.status = 'changed'

    async def test_source_provenance_drift_does_not_override_effective_semantics(self):
        result, _connection = await self.run_rows((
            stored_row(source_digest='a' * 64),
        ))
        self.assertEqual(result.status, shadow.STATUS_MATCHED)

    async def test_direct_active_loader_requires_exact_inventory_without_static_compare(self):
        selected = profile()
        selected.guild_configuration_source = 'database'
        connection = FakeConnection((stored_row(),))
        with mock.patch.object(
            shadow, '_connect', return_value=connection,
        ), mock.patch.object(
            storage, 'inspect_schema_inventory', return_value=exact_inventory(),
        ):
            result = await shadow.run_active_configuration(
                shadow.active_request_from_profile(selected)
            )
        self.assertEqual(tuple(value.guild_id for value in result), (GUILD_ID,))
        self.assertEqual(result[0].document, fixtures.bundle().imports[0].document)
        self.assertEqual(
            connection.sessions,
            [{'readonly': True, 'autocommit': True}],
        )
        self.assertTrue(connection.closed)

    async def test_direct_active_loader_can_discover_additional_active_guilds(self):
        selected = profile()
        selected.guild_configuration_source = 'database'
        mapping = copy.deepcopy(
            document_to_mapping(fixtures.bundle().imports[0].document)
        )
        mapping['guild_id'] = 987654321012345678
        mapping['identity']['display_name'] = 'New active guild'
        additional_document = validate_document(mapping)
        additional = list(stored_row(document=additional_document))
        additional[0] = additional_document.guild_id
        connection = FakeConnection((stored_row(), tuple(additional)))
        request_value = shadow.active_request_from_profile(selected)
        self.assertTrue(request_value.include_all_active)
        with mock.patch.object(
            shadow, '_connect', return_value=connection,
        ), mock.patch.object(
            storage, 'inspect_schema_inventory', return_value=exact_inventory(),
        ):
            result = await shadow.run_active_configuration(request_value)
        self.assertEqual(
            tuple(value.guild_id for value in result),
            (GUILD_ID, additional_document.guild_id),
        )

    async def test_document_state_and_inventory_mismatches_block_promotion(self):
        mapping = copy.deepcopy(
            document_to_mapping(fixtures.bundle().imports[0].document)
        )
        mapping['identity']['display_name'] = 'Changed static display'
        changed = validate_document(mapping)
        result, _connection = await self.run_rows((stored_row(document=changed),))
        self.assertEqual(result.status, shadow.STATUS_MISMATCH)
        self.assertFalse(result.promotion_ready)
        self.assertEqual(
            result.mismatches[0].paths,
            ('identity.display_name',),
        )

        result, _connection = await self.run_rows((stored_row(state='suspended'),))
        self.assertEqual(
            result.mismatches[0].paths,
            ('registry.enrollment_state',),
        )

        result, _connection = await self.run_rows(())
        self.assertEqual(
            result.mismatches[0].paths,
            ('guild.missing_from_storage',),
        )

    async def test_malformed_document_digest_and_identity_fail_closed_and_close(self):
        malformed = list(stored_row())
        malformed[8] = '0' * 64
        connection = FakeConnection((tuple(malformed),))
        with mock.patch.object(
            shadow, '_connect', return_value=connection,
        ), mock.patch.object(
            storage, 'inspect_schema_inventory', return_value=exact_inventory(),
        ):
            with self.assertRaisesRegex(
                shadow.GuildConfigurationShadowMalformed,
                'metadata',
            ):
                await shadow.run_shadow_comparison(request())
        self.assertTrue(connection.closed)

        malformed = list(stored_row())
        malformed[9] = 'not-a-source-digest'
        connection = FakeConnection((tuple(malformed),))
        with mock.patch.object(
            shadow, '_connect', return_value=connection,
        ), mock.patch.object(
            storage, 'inspect_schema_inventory', return_value=exact_inventory(),
        ):
            with self.assertRaisesRegex(
                shadow.GuildConfigurationShadowMalformed,
                'metadata',
            ):
                await shadow.run_shadow_comparison(request())
        self.assertTrue(connection.closed)

        connection = FakeConnection((stored_row(),))
        connection.cursor_value.one = None

        def wrong_identity(statement, parameters=None):
            connection.cursor_value.statements.append((statement, parameters))
            if statement == 'SHOW transaction_read_only':
                connection.cursor_value.one = ('on',)
            elif statement == 'SELECT current_database(), current_user':
                connection.cursor_value.one = ('polytopia2', 'wrong')

        connection.cursor_value.execute = wrong_identity
        with mock.patch.object(
            shadow, '_connect', return_value=connection,
        ), mock.patch.object(
            storage, 'inspect_schema_inventory', return_value=exact_inventory(),
        ):
            with self.assertRaisesRegex(
                shadow.GuildConfigurationShadowMalformed,
                'storage_or_identity',
            ):
                await shadow.run_shadow_comparison(request())
        self.assertTrue(connection.closed)

    async def test_unavailable_connection_has_bounded_classification(self):
        with mock.patch.object(
            shadow,
            '_connect',
            side_effect=shadow.psycopg2.OperationalError('secret host detail'),
        ):
            with self.assertRaisesRegex(
                shadow.GuildConfigurationShadowUnavailable,
                'database_connection_unavailable',
            ) as caught:
                await shadow.run_shadow_comparison(request())
        self.assertNotIn('secret host detail', str(caught.exception))

    async def test_slow_worker_keeps_loop_responsive_and_cancellation_drains(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def slow(_request):
            started.set()
            release.wait(timeout=2)
            finished.set()
            return shadow.GuildConfigurationShadowResult(
                status=shadow.STATUS_MATCHED,
            )

        with mock.patch.object(
            shadow, 'inspect_shadow_configuration', side_effect=slow,
        ):
            task = asyncio.create_task(shadow.run_shadow_comparison(request()))
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


class SnapshotAndRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def discord_guild(self):
        raw = fixtures.snapshot()['guilds'][0]

        class Role(SimpleNamespace):
            def is_default(self):
                return self.default

        roles = tuple(Role(
            id=value['id'],
            name=value['name'],
            managed=value['managed'],
            default=value['is_default'],
        ) for value in raw['roles'])
        channels = tuple(SimpleNamespace(
            id=value['id'],
            name=value['name'],
            type=SimpleNamespace(name=value['type']),
            category_id=value['category_id'],
        ) for value in raw['channels'])
        return SimpleNamespace(
            id=raw['guild_id'],
            name=raw['guild_name'],
            roles=roles,
            channels=channels,
        )

    def test_ready_cache_snapshot_recreates_exact_static_bundle_without_members(self):
        guild = self.discord_guild()
        snapshot = shadow.capture_discord_snapshot(
            profile=profile(),
            guilds=(guild,),
        )
        self.assertNotIn('members', snapshot['guilds'][0])
        bundle = shadow.expected_bundle_from_runtime(
            profile=profile(),
            guilds=(guild,),
        )
        self.assertEqual(bundle, fixtures.bundle())

    def test_missing_guild_or_production_target_is_rejected_before_database(self):
        with self.assertRaisesRegex(
            shadow.GuildConfigurationShadowMalformed,
            'incomplete',
        ):
            shadow.capture_discord_snapshot(profile=profile(), guilds=())
        unsafe = profile()
        unsafe.environment = 'production'
        with self.assertRaisesRegex(
            shadow.GuildConfigurationShadowMalformed,
            'runtime_target_invalid',
        ):
            shadow.target_from_profile(unsafe)

    def test_ready_cache_and_database_inventories_are_bounded(self):
        guild = self.discord_guild()
        with self.assertRaisesRegex(
            shadow.GuildConfigurationShadowMalformed,
            'unbounded',
        ):
            shadow.capture_discord_snapshot(
                profile=profile(),
                guilds=(guild,) * (shadow.MAX_SHADOW_GUILDS + 1),
            )
        cursor = FakeCursor(())
        shadow._load_rows(cursor)
        self.assertEqual(
            cursor.statements[-1][1],
            (shadow.MAX_SHADOW_GUILDS + 1,),
        )

    def make_bot(self):
        instance = bot_module.MyBot()
        instance._startup_identity_validated = True
        instance._startup_schema_preflight_complete = True
        return instance

    async def test_bot_publishes_one_visible_match_without_changing_settings(self):
        instance = self.make_bot()
        result = shadow.GuildConfigurationShadowResult(
            status=shadow.STATUS_MATCHED,
            expected_guild_ids=(GUILD_ID,),
            stored_guild_ids=(GUILD_ID,),
            matched_guild_ids=(GUILD_ID,),
        )
        run = mock.AsyncMock(return_value=result)
        try:
            with mock.patch.object(
                bot_module.settings, 'runtime_profile', profile(),
            ), mock.patch.object(
                shadow, 'capture_discord_snapshot', return_value=fixtures.snapshot(),
            ), mock.patch.object(
                shadow, 'expected_bundle_from_snapshot', return_value=fixtures.bundle(),
            ), mock.patch.object(
                shadow, 'request_from_profile', return_value=request(),
            ), mock.patch.object(
                shadow, 'run_shadow_comparison', run,
            ):
                self.assertIs(
                    await instance._run_development_guild_configuration_shadow(),
                    result,
                )
                self.assertIs(
                    await instance._run_development_guild_configuration_shadow(),
                    result,
                )
        finally:
            await instance.close()
        run.assert_awaited_once()
        self.assertTrue(instance._guild_configuration_shadow_complete)
        self.assertIs(instance.guild_configuration_shadow_result, result)

    async def test_bot_records_unavailable_and_continues_static(self):
        instance = self.make_bot()
        run = mock.AsyncMock(side_effect=(
            shadow.GuildConfigurationShadowUnavailable(
                'database_read_unavailable'
            )
        ))
        try:
            with mock.patch.object(
                bot_module.settings, 'runtime_profile', profile(),
            ), mock.patch.object(
                shadow, 'capture_discord_snapshot', return_value=fixtures.snapshot(),
            ), mock.patch.object(
                shadow, 'expected_bundle_from_snapshot', return_value=fixtures.bundle(),
            ), mock.patch.object(
                shadow, 'request_from_profile', return_value=request(),
            ), mock.patch.object(
                shadow, 'run_shadow_comparison', run,
            ):
                result = await instance._run_development_guild_configuration_shadow()
        finally:
            await instance.close()
        self.assertEqual(result.status, shadow.STATUS_UNAVAILABLE)
        self.assertFalse(result.promotion_ready)
        self.assertTrue(instance._guild_configuration_shadow_complete)

    async def test_database_source_publishes_only_a_current_exact_match(self):
        instance = self.make_bot()
        selected = profile()
        selected.guild_configuration_source = 'database'
        stored = shadow._stored_values((stored_row(),))
        run = mock.AsyncMock(return_value=stored)
        activate = mock.Mock()
        try:
            with mock.patch.object(
                bot_module.settings, 'runtime_profile', selected,
            ), mock.patch.object(
                bot_module.settings,
                'activate_database_guild_configuration',
                activate,
            ), mock.patch.object(
                shadow, 'capture_discord_snapshot', return_value=fixtures.snapshot(),
            ), mock.patch.object(
                shadow, 'active_request_from_profile', return_value=mock.sentinel.request,
            ), mock.patch.object(
                shadow, 'run_active_configuration', run,
            ):
                result = await instance._run_development_guild_configuration_shadow()
        finally:
            await instance.close()
        activate.assert_called_once()
        self.assertEqual(result.stored_configurations, stored)

    async def test_database_source_never_falls_back_after_unavailable_read(self):
        instance = self.make_bot()
        selected = profile()
        selected.guild_configuration_source = 'database'
        run = mock.AsyncMock(side_effect=(
            shadow.GuildConfigurationShadowUnavailable(
                'database_read_unavailable'
            )
        ))
        try:
            with mock.patch.object(
                bot_module.settings, 'runtime_profile', selected,
            ), mock.patch.object(
                shadow, 'capture_discord_snapshot', return_value=fixtures.snapshot(),
            ), mock.patch.object(
                shadow, 'run_active_configuration', run,
            ), self.assertRaisesRegex(
                RuntimeError,
                'database guild configuration is unavailable',
            ):
                await instance._run_development_guild_configuration_shadow()
        finally:
            await instance.close()
        self.assertFalse(instance._guild_configuration_shadow_complete)

    async def test_production_skips_shadow_and_ready_source_invokes_it(self):
        instance = self.make_bot()
        production = SimpleNamespace(environment='production')
        try:
            with mock.patch.object(
                bot_module.settings, 'runtime_profile', production,
            ), mock.patch(
                'bot.importlib.import_module'
            ) as import_module:
                self.assertIsNone(
                    await instance._run_development_guild_configuration_shadow()
                )
        finally:
            await instance.close()
        import_module.assert_not_called()
        source = inspect.getsource(bot_module.init_bot)
        self.assertIn(
            'await bot._run_development_guild_configuration_shadow()',
            source,
        )

    async def test_unpublished_database_source_blocks_prefix_and_slash_dispatch(self):
        interaction = SimpleNamespace(
            response=SimpleNamespace(
                is_done=lambda: False,
                send_message=mock.AsyncMock(),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        message = SimpleNamespace(guild=SimpleNamespace(id=GUILD_ID))
        with mock.patch.object(
            bot_module.settings,
            'guild_configuration_ready',
            return_value=False,
        ), mock.patch.object(
            bot_module.settings,
            'guild_setting',
        ) as guild_setting:
            self.assertEqual(bot_module.get_prefix(None, message), 'fakeprefix')
            self.assertFalse(await bot_module.PolyBotCommandTree.interaction_check(
                None,
                interaction,
            ))
        guild_setting.assert_not_called()
        interaction.response.send_message.assert_awaited_once_with(
            'The bot is still validating its server configuration. '
            'Try the command again in a moment.',
            ephemeral=True,
        )

    async def test_unknown_guild_is_quarantined_for_prefix_and_slash_without_leave(self):
        interaction = SimpleNamespace(
            guild_id=987654321012345678,
            response=SimpleNamespace(
                is_done=lambda: False,
                send_message=mock.AsyncMock(),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        message = SimpleNamespace(
            guild=SimpleNamespace(id=interaction.guild_id),
            author=SimpleNamespace(name='tester'),
        )
        with mock.patch.object(
            bot_module.settings, 'guild_configuration_ready', return_value=True,
        ), mock.patch.object(
            bot_module.settings, 'config', {GUILD_ID: {}},
        ), mock.patch.object(
            bot_module.settings, 'maintenance_mode', False,
        ):
            self.assertEqual(bot_module.get_prefix(None, message), 'fakeprefix')
            self.assertFalse(await bot_module.PolyBotCommandTree.interaction_check(
                None, interaction,
            ))
        interaction.response.send_message.assert_awaited_once_with(
            'This server is quarantined and has not been enrolled by the bot '
            'owner. No command was run.',
            ephemeral=True,
        )
        source = inspect.getsource(bot_module.init_bot)
        self.assertIn('is quarantined', source)
        self.assertNotIn('await guild.leave()', source)

    async def test_unknown_guild_listener_events_are_dropped_at_dispatch_boundary(self):
        instance = self.make_bot()
        unknown = SimpleNamespace(
            guild=SimpleNamespace(id=987654321012345678)
        )
        try:
            with mock.patch.object(
                bot_module.settings, 'guild_configuration_ready', return_value=True,
            ), mock.patch.object(
                bot_module.settings, 'config', {GUILD_ID: {}},
            ), mock.patch.object(
                bot_module.commands.Bot, 'dispatch', autospec=True,
            ) as parent_dispatch:
                instance.dispatch('member_join', unknown)
                parent_dispatch.assert_not_called()
                instance.dispatch('interaction', unknown)
                parent_dispatch.assert_called_once_with(
                    instance, 'interaction', unknown,
                )
                parent_dispatch.reset_mock()
                instance.dispatch(
                    'member_join',
                    SimpleNamespace(guild=SimpleNamespace(id=GUILD_ID)),
                )
                parent_dispatch.assert_called_once()
        finally:
            await instance.close()


if __name__ == '__main__':
    unittest.main()
