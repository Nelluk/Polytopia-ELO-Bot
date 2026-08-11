"""Focused offline coverage for P10.6a owner configuration reads."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import FrozenInstanceError, replace
import datetime
import inspect
from types import SimpleNamespace
import threading
import time
import unittest
from unittest import mock

import discord

from modules import administration
from modules import guild_configuration_storage as storage
from modules import operator_guild_configuration as service
from modules import operator_guild_configuration_workers as workers
from modules.guild_configuration_schema import document_to_mapping
from tests import test_guild_configuration_runtime as runtime_fixtures
from tests import test_guild_configuration_storage as fixtures


GUILD_ID = fixtures.GUILD_ID
OWNER_ID = int(workers.settings.owner_id)
NOW = datetime.datetime(2026, 8, 11, 12, 0, tzinfo=datetime.UTC)


def runtime_record():
    return runtime_fixtures.snapshot().guilds[GUILD_ID]


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
        guild_configuration_source='database',
    )


def request(operation=workers.SETTINGS, *, snapshot=None):
    return workers.request_from_profile(
        profile=profile(),
        requester_id=OWNER_ID,
        guild_id=GUILD_ID,
        operation=operation,
        runtime_record=runtime_record(),
        discord_snapshot=snapshot,
    )


def registry_row(*, generation=1, digest=None, document=None):
    imported = fixtures.bundle().imports[0]
    document = imported.document if document is None else document
    return (
        GUILD_ID,
        storage.STORAGE_SCHEMA_VERSION,
        'active',
        1,
        generation,
        NOW,
        1,
        document.schema_version,
        document_to_mapping(document),
        imported.document_digest if digest is None else digest,
        imported.source_digest,
    )


def revision_row():
    imported = fixtures.bundle().imports[0]
    return (
        1,
        imported.document.schema_version,
        document_to_mapping(imported.document),
        imported.document_digest,
        imported.source_digest,
        None,
        storage.IMPORT_SOURCE_KIND,
        storage.IMPORT_ACTOR,
        NOW,
    )


def audit_row():
    imported = fixtures.bundle().imports[0]
    return (
        1,
        storage.IMPORT_EVENT_TYPE,
        1,
        1,
        imported.document_digest,
        storage.IMPORT_ACTOR,
        {'source_digest': imported.source_digest},
        NOW,
    )


class FakeCursor:
    def __init__(self, *, registry=None, revisions=None, audits=None):
        self.registry = [registry_row()] if registry is None else list(registry)
        self.revisions = [revision_row()] if revisions is None else list(revisions)
        self.audits = [audit_row()] if audits is None else list(audits)
        self.one = None
        self.rows = []
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, parameters=None):
        self.statements.append((statement, parameters))
        if statement == 'SHOW transaction_read_only':
            self.one = ('on',)
            self.rows = []
        elif statement == 'SELECT current_database(), current_user':
            self.one = ('polytopia_dev', 'polybot_dev')
            self.rows = []
        elif 'FROM "guild_configuration_registry" AS registry' in statement:
            self.rows = self.registry
        elif 'FROM "guild_configuration_revision" WHERE' in statement:
            self.rows = self.revisions
        elif 'FROM "guild_configuration_audit" WHERE' in statement:
            self.rows = self.audits
        else:
            self.rows = []

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, **cursor_values):
        self.cursor_value = FakeCursor(**cursor_values)
        self.sessions = []
        self.rollbacks = 0
        self.closed = False

    def set_session(self, **kwargs):
        self.sessions.append(kwargs)

    def cursor(self):
        return self.cursor_value

    def close(self):
        self.closed = True

    def rollback(self):
        self.rollbacks += 1


def inspect_with(connection, value):
    with mock.patch.object(workers.settings, 'owner_id', OWNER_ID), \
            mock.patch.object(workers, '_connect', return_value=connection), \
            mock.patch.object(
                workers.storage,
                'inspect_schema_inventory',
                return_value=mock.sentinel.inventory,
            ), mock.patch.object(
                workers.storage,
                'validate_schema_inventory',
                return_value=True,
            ):
        return workers.inspect_guild_configuration(value)


class WorkerContractTests(unittest.TestCase):
    def test_request_is_frozen_and_requires_development_database_authority(self):
        value = request()
        with self.assertRaises(FrozenInstanceError):
            value.operation = workers.LIST
        for environment, source in (
            ('production', 'static'),
            ('development', 'static'),
        ):
            selected = profile()
            selected.environment = environment
            selected.guild_configuration_source = source
            with self.assertRaisesRegex(
                workers.OperatorGuildConfigurationValidationError,
                'development database authority',
            ):
                workers.request_from_profile(
                    profile=selected,
                    requester_id=OWNER_ID,
                    guild_id=GUILD_ID,
                    operation=workers.LIST,
                    runtime_record=runtime_record(),
                )

    def test_non_owner_is_rejected_before_connection(self):
        value = request()
        value = workers.GuildConfigurationReadRequest(
            **{**value.__dict__, 'requester_id': OWNER_ID + 1}
        )
        with mock.patch.object(workers.settings, 'owner_id', OWNER_ID), \
                mock.patch.object(workers, '_connect') as connect:
            with self.assertRaisesRegex(
                workers.OperatorGuildConfigurationPermissionError,
                'configured bot owner',
            ):
                workers.inspect_guild_configuration(value)
        connect.assert_not_called()

    def test_list_uses_and_closes_one_read_only_owned_connection(self):
        connection = FakeConnection()
        result = inspect_with(connection, request(workers.LIST))
        self.assertEqual(result.operation, workers.LIST)
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0].display_name, 'Development Test Guild')
        self.assertEqual(connection.sessions, [{
            'readonly': True,
            'autocommit': False,
            'isolation_level': 'REPEATABLE READ',
        }])
        self.assertEqual(connection.rollbacks, 1)
        self.assertTrue(connection.closed)

    def test_settings_returns_validated_active_document(self):
        connection = FakeConnection()
        result = inspect_with(connection, request())
        self.assertEqual(result.selected.active_revision, 1)
        self.assertEqual(result.selected.generation, 1)
        self.assertEqual(
            result.selected.document,
            fixtures.bundle().imports[0].document,
        )

    def test_running_snapshot_mismatch_fails_closed(self):
        connection = FakeConnection(registry=[registry_row(generation=2)])
        with self.assertRaisesRegex(
            workers.OperatorGuildConfigurationValidationError,
            'running immutable snapshot',
        ):
            inspect_with(connection, request())
        self.assertEqual(connection.rollbacks, 1)
        self.assertTrue(connection.closed)

    def test_invalid_document_digest_fails_closed(self):
        connection = FakeConnection(registry=[registry_row(digest='0' * 64)])
        with self.assertRaisesRegex(
            workers.OperatorGuildConfigurationValidationError,
            'metadata is invalid',
        ):
            inspect_with(connection, request())
        self.assertTrue(connection.closed)

    def test_connection_unavailable_is_safe_and_opens_no_fallback(self):
        with mock.patch.object(workers.settings, 'owner_id', OWNER_ID), \
                mock.patch.object(
                    workers,
                    '_connect',
                    side_effect=workers.psycopg2.OperationalError('down'),
                ):
            with self.assertRaisesRegex(
                workers.OperatorGuildConfigurationUnavailable,
                'database is unavailable',
            ):
                workers.inspect_guild_configuration(request())

    def test_absent_storage_schema_fails_and_closes_connection(self):
        connection = FakeConnection()
        with mock.patch.object(workers.settings, 'owner_id', OWNER_ID), \
                mock.patch.object(workers, '_connect', return_value=connection), \
                mock.patch.object(
                    workers.storage,
                    'inspect_schema_inventory',
                    return_value=mock.sentinel.inventory,
                ), mock.patch.object(
                    workers.storage,
                    'validate_schema_inventory',
                    return_value=False,
                ):
            with self.assertRaisesRegex(
                workers.OperatorGuildConfigurationValidationError,
                'identity or schema is invalid',
            ):
                workers.inspect_guild_configuration(request())
        self.assertEqual(connection.rollbacks, 1)
        self.assertTrue(connection.closed)

    def test_validate_checks_live_references_and_running_snapshot(self):
        connection = FakeConnection()
        result = inspect_with(
            connection,
            request(workers.VALIDATE, snapshot=fixtures.snapshot()),
        )
        self.assertTrue(result.validation.storage_schema_valid)
        self.assertTrue(result.validation.database_identity_valid)
        self.assertTrue(result.validation.active_document_valid)
        self.assertTrue(result.validation.live_references_valid)
        self.assertTrue(result.validation.running_snapshot_current)

    def test_validate_rejects_a_deleted_live_role(self):
        snapshot = copy.deepcopy(fixtures.snapshot())
        snapshot['guilds'][0]['roles'] = [
            value for value in snapshot['guilds'][0]['roles']
            if value['id'] != 201
        ]
        with self.assertRaisesRegex(
            workers.OperatorGuildConfigurationValidationError,
            'current Discord roles and channels',
        ):
            inspect_with(
                FakeConnection(),
                request(workers.VALIDATE, snapshot=snapshot),
            )

    def test_history_validates_revision_and_audit_rows(self):
        result = inspect_with(FakeConnection(), request(workers.HISTORY))
        self.assertEqual(result.revisions[0].revision_number, 1)
        self.assertEqual(result.revisions[0].source_kind, 'legacy_static_import')
        self.assertEqual(result.audits[0].event_type, 'initial_import')
        self.assertEqual(result.audits[0].generation, 1)

    def test_source_has_no_configuration_write_statement(self):
        source = inspect.getsource(workers)
        for statement in ('INSERT INTO', 'UPDATE "guild_configuration', 'DELETE FROM'):
            self.assertNotIn(statement, source)
        self.assertNotIn('modules.models', source)
        self.assertNotIn('modules.models', inspect.getsource(service))


class WorkerAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_read_drains_worker_ownership(self):
        started = threading.Event()
        released = threading.Event()
        completed = threading.Event()

        def slow_read(_request):
            started.set()
            released.wait(timeout=2)
            completed.set()
            return mock.sentinel.result

        with mock.patch.object(workers.settings, 'owner_id', OWNER_ID), \
                mock.patch.object(workers, 'inspect_guild_configuration', slow_read):
            task = asyncio.create_task(workers.run_read(request()))
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.001)
            self.assertTrue(started.is_set())
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            released.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(completed.is_set())

    async def test_read_keeps_event_loop_responsive(self):
        def slow_read(_request):
            time.sleep(0.05)
            return mock.sentinel.result

        ticked = False

        async def ticker():
            nonlocal ticked
            await asyncio.sleep(0.005)
            ticked = True

        with mock.patch.object(workers.settings, 'owner_id', OWNER_ID), \
                mock.patch.object(workers, 'inspect_guild_configuration', slow_read):
            result, _ = await asyncio.gather(workers.run_read(request()), ticker())
        self.assertIs(result, mock.sentinel.result)
        self.assertTrue(ticked)


class ServiceTests(unittest.TestCase):
    def test_settings_sections_are_compact_and_private_safe(self):
        result = inspect_with(FakeConnection(), request())
        for section in service.SETTINGS_SECTIONS:
            with self.subTest(section=section):
                embed = service.result_embed(result, section=section)
                value = embed.to_dict()
                self.assertLessEqual(len(value.get('description', '')), 4096)
                self.assertTrue(all(
                    len(field['value']) <= 1024
                    for field in value.get('fields', [])
                ))
                self.assertIn('Read-only', value['footer']['text'])

    def test_validate_and_history_render_actionable_bounded_summaries(self):
        validated = inspect_with(
            FakeConnection(),
            request(workers.VALIDATE, snapshot=fixtures.snapshot()),
        )
        validation_embed = service.result_embed(validated).to_dict()
        self.assertIn('validation passed', validation_embed['title'])
        self.assertIn('Current Discord role/channel references', validation_embed['description'])

        history = inspect_with(FakeConnection(), request(workers.HISTORY))
        history_embed = service.result_embed(history).to_dict()
        self.assertIn('configuration history', history_embed['title'])
        self.assertEqual(len(history_embed['fields']), 2)

    def test_failed_validation_cannot_render_as_passed(self):
        validated = inspect_with(
            FakeConnection(),
            request(workers.VALIDATE, snapshot=fixtures.snapshot()),
        )
        changed = replace(
            validated,
            validation=replace(
                validated.validation,
                running_snapshot_current=False,
            ),
        )
        with self.assertRaisesRegex(ValueError, 'cannot render as passed'):
            service.result_embed(changed)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = administration.administration.__new__(administration.administration)
        self.cog.bot = SimpleNamespace(guilds=())
        self.operator_group = next(
            command
            for command in administration.administration.__cog_app_commands__
            if command.name == 'operator'
        )
        self.guild_group = self.operator_group.get_command('guild')

    def test_exact_registration_shape(self):
        self.assertEqual(
            {command.name for command in self.guild_group.commands},
            {'list', 'settings', 'validate', 'history', 'edit', 'rollback'},
        )
        settings_command = self.guild_group.get_command('settings')
        self.assertEqual(
            [(parameter.name, parameter.required) for parameter in settings_command.parameters],
            [('section', False)],
        )
        self.assertEqual(
            {choice.value for choice in settings_command.parameters[0].choices},
            service.SETTINGS_SECTIONS,
        )
        prefix_names = {
            command.name
            for command in administration.administration.__cog_commands__
        }
        self.assertNotIn('guild', prefix_names)

    async def test_non_owner_denial_is_private_and_does_not_defer(self):
        command = self.guild_group.get_command('list')
        response = SimpleNamespace(
            send_message=mock.AsyncMock(),
            defer=mock.AsyncMock(),
        )
        interaction = SimpleNamespace(
            guild_id=GUILD_ID,
            user=SimpleNamespace(id=OWNER_ID + 1),
            response=response,
        )
        with mock.patch.object(service.settings, 'owner_id', OWNER_ID), \
                mock.patch.object(workers, 'run_read', new=mock.AsyncMock()) as run:
            await command.callback(self.cog, interaction)
        response.send_message.assert_awaited_once_with(
            'Only the configured bot owner can inspect guild configuration.',
            ephemeral=True,
        )
        response.defer.assert_not_awaited()
        run.assert_not_awaited()

    async def test_owner_defers_then_runs_worker_and_private_publisher(self):
        command = self.guild_group.get_command('validate')
        response = SimpleNamespace(defer=mock.AsyncMock())
        interaction = SimpleNamespace(
            guild_id=GUILD_ID,
            user=SimpleNamespace(id=OWNER_ID),
            response=response,
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        request_value = mock.sentinel.request
        result = mock.sentinel.result
        with mock.patch.object(service, 'access_error', return_value=None), \
                mock.patch.object(service, 'build_request', return_value=request_value), \
                mock.patch.object(workers, 'run_read', new=mock.AsyncMock(return_value=result)) as run, \
                mock.patch.object(service, 'publish_private', new=mock.AsyncMock()) as publish:
            returned = await command.callback(self.cog, interaction)
        response.defer.assert_awaited_once_with(ephemeral=True)
        run.assert_awaited_once_with(request_value)
        publish.assert_awaited_once_with(
            interaction,
            result,
            section=service.OVERVIEW,
        )
        self.assertIs(returned, result)


if __name__ == '__main__':
    unittest.main()
