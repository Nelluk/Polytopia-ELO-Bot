"""Focused offline coverage for P10.7 quarantined guild enrollment."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
import copy
from dataclasses import FrozenInstanceError, replace
import inspect
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

from modules import administration
from modules import guild_configuration_runtime as runtime
from modules import guild_configuration_storage as storage
from modules import operator_guild_enrollment as service
from modules import operator_guild_enrollment_views as views
from modules import operator_guild_enrollment_workers as workers
from modules import guild_types
from modules.guild_configuration_schema import document_digest
from tests import test_guild_configuration_runtime as runtime_fixtures
from tests import test_guild_configuration_storage as fixtures


GUILD_ID = fixtures.GUILD_ID
TARGET_ID = 987654321012345678
OWNER_ID = int(workers.settings.owner_id)


def profile():
    return SimpleNamespace(
        environment='development', database_name='polytopia_dev',
        database_user='polybot_dev', database_password='secret',
        database_host='localhost', database_port=5432,
        expected_bot_id=storage.DEVELOPMENT_BETA_APPLICATION_ID,
        background_tasks_enabled=False, api_enabled=False, bullet_enabled=False,
        allowed_guild_ids=(GUILD_ID,), guild_configuration_source='database',
    )


def enrollment_snapshot():
    value = copy.deepcopy(fixtures.snapshot())
    value['guilds'].append({
        'guild_id': TARGET_ID,
        'guild_name': 'Fresh Guild',
        'roles': [{
            'id': TARGET_ID, 'name': '@everyone', 'managed': False,
            'is_default': True,
        }],
        'channels': [],
    })
    return value


def enrollment_request(operation=workers.PREVIEW, **kwargs):
    current = runtime_fixtures.snapshot().guilds[GUILD_ID]
    preview_document = workers.basic_prefix_document(
        guild_id=TARGET_ID, guild_name='Fresh Guild'
    )
    defaults = {}
    if operation == workers.COMMIT:
        digest = document_digest(preview_document)
        defaults = {
            'expected_document_digest': digest,
            'confirmation_text': f'ENROLL {TARGET_ID} {digest}',
        }
    defaults.update(kwargs)
    return workers.request_from_profile(
        profile=profile(), requester_id=OWNER_ID,
        invoking_guild_id=GUILD_ID, target_guild_id=TARGET_ID,
        target_guild_name='Fresh Guild',
        template=workers.BASIC_PREFIX_TEMPLATE,
        guild_type=guild_types.STANDARD,
        include_in_global_leaderboard=None,
        bot_permissions=tuple(sorted(workers.REQUIRED_BOT_PERMISSIONS)),
        current_runtime_records=(current,), forbidden_guild_ids=(),
        discord_snapshot=enrollment_snapshot(), operation=operation,
        **defaults,
    )


class Cursor:
    def __init__(self):
        self.readonly = True
        self.row = None
        self.rows = ()
        self.rowcount = 0
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, parameters=None):
        self.statements.append((statement, parameters))
        if statement == 'SHOW transaction_read_only':
            self.row = ('on' if self.readonly else 'off',)
        elif statement == 'SELECT current_database(), current_user':
            self.row = ('polytopia_dev', 'polybot_dev')
        elif 'registry JOIN' in statement:
            current = runtime_fixtures.snapshot().guilds[GUILD_ID]
            self.rows = ((
                GUILD_ID, current.revision, current.generation,
                current.document_digest,
            ),)
        elif statement.startswith('SELECT enrollment_state'):
            self.row = None
        elif statement.startswith('UPDATE'):
            self.rowcount = 1

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self):
        self.cursor_value = Cursor()
        self.sessions = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def set_session(self, **kwargs):
        self.sessions.append(kwargs)
        self.cursor_value.readonly = kwargs['readonly']

    def cursor(self):
        return self.cursor_value

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def exact_inventory():
    return storage.SchemaInventory(
        tuple(sorted(storage.STORAGE_TABLES)),
        storage.EXPECTED_COLUMNS,
        storage.EXPECTED_CONSTRAINTS,
    )


def published_snapshot(request):
    preview = workers._preview(request)
    record = SimpleNamespace(
        revision=1, generation=1, document_digest=preview.document_digest,
    )
    return SimpleNamespace(guilds={TARGET_ID: record})


class TemplateAndValidationTests(unittest.TestCase):
    def test_basic_template_is_complete_usable_and_least_authority(self):
        document = workers.basic_prefix_document(
            guild_id=TARGET_ID, guild_name='Fresh Guild'
        )
        self.assertEqual(document.identity.command_prefix, '$')
        self.assertEqual(document.permissions.user_role_ids_level_1, ())
        self.assertEqual(document.permissions.user_role_ids_level_2, (TARGET_ID,))
        self.assertEqual(document.permissions.user_role_ids_level_3, ())
        self.assertEqual(document.permissions.helper_role_ids, ())
        self.assertEqual(document.permissions.mod_role_ids, ())
        self.assertFalse(document.teams.allow_teams)
        self.assertFalse(document.teams.require_teams)
        self.assertFalse(document.teams.allow_uneven_teams)
        self.assertEqual(document.teams.max_team_size, 2)
        self.assertFalse(document.visibility.include_in_global_leaderboard)
        self.assertEqual(
            document.command_capabilities,
            ('core_user', 'guild_admin', 'squad'),
        )
        self.assertIsNone(document.channels.bot_channel_ids)
        self.assertIsNone(document.channels.strict_bot_channel_ids)

    def test_request_is_frozen_owner_only_and_digest_bound(self):
        value = enrollment_request()
        with self.assertRaises(FrozenInstanceError):
            value.target_guild_id = 1
        with mock.patch.object(workers.settings, 'owner_id', OWNER_ID), \
                self.assertRaisesRegex(
                    workers.OperatorGuildEnrollmentPermissionError, 'owner'
                ):
            workers.execute_enrollment(replace(value, requester_id=OWNER_ID + 1))
        digest = value.current_runtime[0].document_digest
        with self.assertRaisesRegex(
            workers.OperatorGuildEnrollmentValidationError, 'digest'
        ):
            workers.execute_enrollment(replace(
                value,
                current_runtime=(replace(value.current_runtime[0], document_digest=digest[:-1]),),
            ))

    def test_production_target_and_missing_permissions_fail_before_connection(self):
        value = enrollment_request()
        for changed, pattern in (
            (replace(value, forbidden_guild_ids=(TARGET_ID,)), 'protected'),
            (replace(value, bot_permissions=('view_channel',)), 'permissions'),
        ):
            with self.subTest(pattern=pattern), mock.patch.object(
                workers, '_connect'
            ) as connect, self.assertRaisesRegex(
                workers.OperatorGuildEnrollmentValidationError, pattern
            ):
                workers.execute_enrollment(changed)
            connect.assert_not_called()

    def test_commit_requires_full_preview_digest_and_exact_confirmation(self):
        value = enrollment_request()
        with self.assertRaisesRegex(
            workers.OperatorGuildEnrollmentConflict, 'changed after preview'
        ):
            workers.execute_enrollment(replace(
                value, operation=workers.COMMIT,
                expected_document_digest='0' * 64,
                confirmation_text='wrong',
            ))


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def run_worker(self, value, connection=None):
        connection = connection or Connection()
        patches = (
            mock.patch.object(workers.settings, 'owner_id', OWNER_ID),
            mock.patch.object(workers, '_connect', return_value=connection),
            mock.patch.object(
                storage, 'inspect_schema_inventory', return_value=exact_inventory()
            ),
        )
        for selected in patches:
            selected.start()
            self.addCleanup(selected.stop)
        return workers.execute_enrollment(value), connection

    def test_preview_owns_one_read_only_connection_and_does_not_write(self):
        result, connection = self.run_worker(enrollment_request())
        self.assertEqual(result.operation, workers.PREVIEW)
        self.assertIsNone(result.enrollment)
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)
        self.assertTrue(connection.closed)
        sql = '\n'.join(value for value, _params in connection.cursor_value.statements)
        self.assertNotIn('INSERT INTO', sql)

    def test_commit_inserts_revision_and_audit_then_reloads(self):
        value = enrollment_request(workers.COMMIT)
        connection = Connection()
        with mock.patch.object(
            workers, '_post_commit_snapshot', return_value=published_snapshot(value)
        ):
            result, connection = self.run_worker(value, connection)
        self.assertEqual(result.enrollment.revision, 1)
        self.assertEqual(result.enrollment.event_number, 1)
        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 0)
        sql = '\n'.join(statement for statement, _ in connection.cursor_value.statements)
        self.assertIn(f'INSERT INTO "{storage.REGISTRY_TABLE}"', sql)
        self.assertIn(f'INSERT INTO "{storage.REVISION_TABLE}"', sql)
        self.assertIn(f'INSERT INTO "{storage.AUDIT_TABLE}"', sql)
        self.assertNotIn('application command', sql.casefold())

    def test_precommit_failure_rolls_back_without_committed_result(self):
        value = enrollment_request(workers.COMMIT)
        connection = Connection()
        with mock.patch.object(
            workers, '_insert_enrollment', side_effect=RuntimeError('write failed')
        ):
            with self.assertRaisesRegex(RuntimeError, 'write failed'):
                self.run_worker(value, connection)
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)

    def test_postcommit_reload_failure_reports_truthfully_committed(self):
        value = enrollment_request(workers.COMMIT)
        connection = Connection()
        with mock.patch.object(
            workers, '_post_commit_snapshot', side_effect=RuntimeError('reload failed')
        ), self.assertRaisesRegex(
            workers.OperatorGuildEnrollmentCommitted, '(?s)committed.*Restart'
        ):
            self.run_worker(value, connection)
        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 0)

    async def test_worker_is_event_loop_responsive_and_cancellation_drains(self):
        pending = Future()
        result = workers.GuildEnrollmentResult(
            operation=workers.PREVIEW,
            preview=workers._preview(enrollment_request()),
        )
        with mock.patch.object(workers._executor, 'submit', return_value=pending):
            task = asyncio.create_task(workers.run_enrollment(enrollment_request()))
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            task.cancel()
            pending.set_result(result)
            with self.assertRaises(asyncio.CancelledError):
                await task


class AdapterAndViewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = administration.administration.__new__(administration.administration)
        operator = next(
            value for value in administration.administration.__cog_app_commands__
            if value.name == 'operator'
        )
        self.command = operator.get_command('guild').get_command('enroll')

    def test_registration_uses_string_snowflake_type_and_optional_global_flag(self):
        self.assertIsNotNone(self.command)
        self.assertEqual(
            [(value.name, value.required) for value in self.command.parameters],
            [
                ('target_guild_id', True),
                ('guild_type', True),
                ('global_leaderboard', False),
            ],
        )
        self.assertEqual(
            [value.value for value in self.command.parameters[1].choices],
            list(guild_types.GUILD_TYPES),
        )

    async def test_non_owner_is_denied_before_defer(self):
        interaction = SimpleNamespace(
            guild_id=GUILD_ID, user=SimpleNamespace(id=OWNER_ID + 1),
            response=SimpleNamespace(
                send_message=mock.AsyncMock(), defer=mock.AsyncMock(),
            ),
        )
        with mock.patch.object(service.settings, 'owner_id', OWNER_ID):
            await self.command.callback(
                self.cog, interaction, str(TARGET_ID),
                SimpleNamespace(value=guild_types.STANDARD),
            )
        interaction.response.send_message.assert_awaited_once()
        interaction.response.defer.assert_not_awaited()

    async def test_workspace_is_requester_bound_and_uses_exact_modal_text(self):
        result = workers.GuildEnrollmentResult(
            operation=workers.PREVIEW,
            preview=workers._preview(enrollment_request()),
        )

        async def runner(*_args, **_kwargs):
            return result

        workspace = views.GuildEnrollmentWorkspace(
            requester_id=OWNER_ID, result=result, runner=runner
        )
        modal = views.GuildEnrollmentModal(workspace)
        self.assertEqual(modal.expected, result.preview.confirmation)
        denied = SimpleNamespace(
            user=SimpleNamespace(id=OWNER_ID + 1),
            response=SimpleNamespace(send_message=mock.AsyncMock()),
        )
        self.assertFalse(await workspace.authorize(denied))
        denied.response.send_message.assert_awaited_once()
        self.assertNotIn('modules.models', inspect.getsource(views))

    async def test_committed_result_survives_failed_panel_and_followup_publication(self):
        initial = workers.GuildEnrollmentResult(
            operation=workers.PREVIEW,
            preview=workers._preview(enrollment_request()),
        )
        enrollment = workers.GuildEnrollment(
            guild_id=TARGET_ID, guild_name='Fresh Guild',
            template=workers.BASIC_PREFIX_TEMPLATE, revision=1, generation=1,
            event_number=1, document_digest=initial.preview.document_digest,
            actor=f'discord:{OWNER_ID}', created=True,
            document=initial.preview.document,
        )
        committed = replace(
            initial, operation=workers.COMMIT, enrollment=enrollment,
        )

        async def runner(*_args, **_kwargs):
            return committed

        workspace = views.GuildEnrollmentWorkspace(
            requester_id=OWNER_ID, result=initial, runner=runner
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(
                side_effect=[None, RuntimeError('panel gone')]
            ),
            followup=SimpleNamespace(
                send=mock.AsyncMock(side_effect=RuntimeError('followup gone'))
            ),
        )
        await workspace.commit(interaction, initial.preview.confirmation)
        self.assertTrue(workspace.terminal)
        self.assertIn('Enrolled and published', workspace.status)


class RuntimePublicationTests(unittest.TestCase):
    def test_reconcile_adds_exactly_one_guild_and_preserves_existing_evidence(self):
        current = runtime_fixtures.snapshot()
        old = current.guilds[GUILD_ID]
        new_record = replace(
            old, guild_id=TARGET_ID, revision=1, generation=1,
            document_digest='f' * 64,
        )
        candidate = runtime.GuildConfigurationRuntimeSnapshot(
            source='database', guilds={GUILD_ID: old, TARGET_ID: new_record},
            legacy_config={GUILD_ID: {}, TARGET_ID: {}},
            command_policy=mock.sentinel.policy,
        )
        with mock.patch.object(settings := workers.settings, 'guild_configuration_source', 'database'), \
                mock.patch.object(settings, '_database_guild_configuration', current), \
                mock.patch.object(settings, 'config', current.legacy_config), \
                mock.patch.object(settings, 'application_command_policy', current.command_policy):
            settings.reconcile_database_guild_enrollment(
                candidate,
                expected_current={GUILD_ID: (
                    old.revision, old.generation, old.document_digest,
                )},
                enrolled_guild_id=TARGET_ID,
                expected_enrollment=(1, 1, 'f' * 64),
            )
            self.assertIs(settings._database_guild_configuration, candidate)
            self.assertIs(settings.config, candidate.legacy_config)


if __name__ == '__main__':
    unittest.main()
