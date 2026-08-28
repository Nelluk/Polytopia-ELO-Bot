"""Focused Tier-3 coverage for P10.6b3 immutable rollback."""

from __future__ import annotations

import asyncio
import datetime
from dataclasses import replace
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from modules import administration
from modules import guild_configuration_draft_storage as drafts
from modules import operator_guild_configuration_drafts as service
from modules import operator_guild_configuration_draft_workers as workers
from modules import operator_guild_configuration_rollback_views as views
from modules.guild_configuration_schema import document_digest, document_to_mapping
from tests import test_guild_configuration_runtime as runtime_fixtures
from tests import test_guild_configuration_storage as fixtures


GUILD_ID = fixtures.GUILD_ID
OWNER_ID = int(workers.settings.owner_id)
NOW = datetime.datetime(2026, 8, 11, 12, 0, tzinfo=datetime.UTC)


def source_document():
    return fixtures.bundle().imports[0].document


def active_document():
    return service.replace_field(
        source_document(),
        service.FIELD_BY_KEY['display_name'],
        'Current active name',
    )


def profile():
    return SimpleNamespace(
        environment='development', database_name='polytopia_dev',
        database_user='polybot_dev', database_password='secret',
        database_host='localhost', database_port=5432,
        expected_bot_id=drafts.storage.DEVELOPMENT_BETA_APPLICATION_ID,
        background_tasks_enabled=False, api_enabled=False, bullet_enabled=False,
        allowed_guild_ids=(GUILD_ID,), guild_configuration_source='database',
    )


def runtime_record():
    original = runtime_fixtures.snapshot().guilds[GUILD_ID]
    current = active_document()
    return replace(
        original,
        revision=2,
        generation=2,
        document=current,
        document_digest=document_digest(current),
    )


def request(operation=workers.ROLLBACK_PREVIEW, **kwargs):
    source = source_document()
    values = dict(target_revision=1, discord_snapshot=fixtures.snapshot())
    if operation == workers.ROLLBACK_COMMIT:
        digest = document_digest(source)
        values.update(
            expected_target_digest=digest,
            expected_active_revision=2,
            expected_active_generation=2,
            expected_active_digest=document_digest(active_document()),
            confirmation_text=f'ROLLBACK 1 {digest}',
        )
    values.update(kwargs)
    return workers.request_from_profile(
        profile=profile(), requester_id=OWNER_ID, guild_id=GUILD_ID,
        operation=operation, runtime_record=runtime_record(), **values,
    )


def rollback_result():
    source = source_document()
    return drafts.GuildConfigurationRollback(
        guild_id=GUILD_ID,
        previous_revision=2,
        previous_generation=2,
        source_revision=1,
        revision=3,
        generation=3,
        event_number=3,
        document_digest=document_digest(source),
        source_digest='b' * 64,
        actor=f'discord:{OWNER_ID}',
        document=source,
    )


def preview_result():
    source = source_document()
    current = active_document()
    preview = workers.GuildConfigurationRollbackPreview(
        guild_id=GUILD_ID,
        active_revision=2,
        active_generation=2,
        active_document_digest=document_digest(current),
        source_revision=1,
        source_document_digest=document_digest(source),
        changed_paths=('identity.display_name',),
        source_document=source,
    )
    return workers.GuildConfigurationDraftResult(
        operation=workers.ROLLBACK_PREVIEW,
        guild_id=GUILD_ID,
        active_revision=2,
        active_generation=2,
        active_document_digest=document_digest(current),
        draft=None,
        rollback_preview=preview,
    )


class StorageCursor:
    def __init__(self):
        self.statements = []
        self.row = None
        self.rowcount = 1

    def execute(self, statement, parameters=None):
        self.statements.append((statement, parameters))
        if 'MAX(revision_number)' in statement:
            self.row = (2,)
        elif 'MAX(event_number)' in statement:
            self.row = (2,)

    def fetchone(self):
        return self.row


class WorkerCursor:
    def __init__(self):
        self.readonly = True
        self.statements = []
        self.row = None

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

    def fetchone(self):
        return self.row


class Connection:
    def __init__(self):
        self.cursor_value = WorkerCursor()
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


def advanced_snapshot():
    snapshot = runtime_fixtures.snapshot()
    source = source_document()
    record = replace(
        snapshot.guilds[GUILD_ID],
        revision=3,
        generation=3,
        document=source,
        document_digest=document_digest(source),
    )
    return replace(snapshot, guilds={GUILD_ID: record})


def run_worker(connection, value):
    current = active_document()
    with mock.patch.object(workers.settings, 'owner_id', OWNER_ID), \
            mock.patch.object(workers, '_connect', return_value=connection), \
            mock.patch.object(workers, '_validate_schema'), \
            mock.patch.object(
                workers, '_active',
                return_value=(2, 2, current, document_digest(current)),
            ), mock.patch.object(
                workers.drafts, 'select_revision',
                return_value=(source_document(), document_digest(source_document())),
            ):
        return workers.execute_draft_operation(value)


class RollbackStorageTests(unittest.TestCase):
    def test_rollback_clones_source_into_new_monotonic_revision_and_audit(self):
        cursor = StorageCursor()
        source = source_document()
        current = active_document()
        result = drafts.rollback_to_revision(
            cursor,
            guild_id=GUILD_ID,
            active_revision=2,
            active_generation=2,
            active_document_digest=document_digest(current),
            source_revision=1,
            source_document=source,
            source_document_digest=document_digest(source),
            actor=f'discord:{OWNER_ID}',
            changed_paths=('identity.display_name',),
        )
        self.assertEqual((result.source_revision, result.revision, result.generation), (1, 3, 3))
        statements = tuple(value[0] for value in cursor.statements)
        revision_insert = next(value for value in cursor.statements if value[0].startswith(
            f'INSERT INTO "{drafts.storage.REVISION_TABLE}"'
        ))
        self.assertEqual(revision_insert[1][6], 2)
        self.assertEqual(revision_insert[1][7], drafts.ROLLBACK_SOURCE_KIND)
        audit_insert = next(value for value in cursor.statements if value[0].startswith(
            f'INSERT INTO "{drafts.storage.AUDIT_TABLE}"'
        ))
        self.assertEqual(audit_insert[1][2], drafts.ROLLBACK_EVENT_TYPE)
        self.assertNotIn('DELETE FROM', '\n'.join(statements))

    def test_registry_cas_failure_stops_before_audit(self):
        cursor = StorageCursor()
        original = cursor.execute

        def fail_registry(statement, parameters=None):
            original(statement, parameters)
            if statement.startswith(f'UPDATE "{drafts.storage.REGISTRY_TABLE}"'):
                cursor.rowcount = 0

        cursor.execute = fail_registry
        with self.assertRaisesRegex(
            drafts.GuildConfigurationDraftStorageError,
            'changed during rollback',
        ):
            drafts.rollback_to_revision(
                cursor,
                guild_id=GUILD_ID,
                active_revision=2,
                active_generation=2,
                active_document_digest=document_digest(active_document()),
                source_revision=1,
                source_document=source_document(),
                source_document_digest=document_digest(source_document()),
                actor=f'discord:{OWNER_ID}',
                changed_paths=('identity.display_name',),
            )
        self.assertFalse(any(
            statement.startswith(f'INSERT INTO "{drafts.storage.AUDIT_TABLE}"')
            for statement, _ in cursor.statements
        ))


class RollbackWorkerTests(unittest.TestCase):
    def test_preview_is_read_only_live_validated_and_model_free(self):
        connection = Connection()
        result = run_worker(connection, request())
        self.assertEqual(result.rollback_preview.source_revision, 1)
        self.assertEqual(result.rollback_preview.changed_paths, ('identity.display_name',))
        self.assertEqual(connection.commits, 0)
        self.assertTrue(connection.closed)
        self.assertTrue(connection.sessions[0]['readonly'])

    def test_commit_revalidates_digest_commits_then_reloads_snapshot(self):
        connection = Connection()
        committed = rollback_result()
        with mock.patch.object(
            workers.drafts, 'rollback_to_revision', return_value=committed,
        ) as write, mock.patch.object(
            workers, '_post_commit_runtime_snapshot', return_value=advanced_snapshot(),
        ):
            result = run_worker(connection, request(workers.ROLLBACK_COMMIT))
        self.assertIs(result.rollback, committed)
        self.assertEqual(connection.commits, 1)
        self.assertEqual(result.runtime_snapshot.guilds[GUILD_ID].revision, 3)
        write.assert_called_once()

    def test_wrong_confirmation_and_stale_digest_fail_before_connection_or_write(self):
        value = request(workers.ROLLBACK_COMMIT)
        with mock.patch.object(workers.settings, 'owner_id', OWNER_ID), \
                mock.patch.object(workers, '_connect') as connect:
            with self.assertRaisesRegex(
                workers.OperatorGuildConfigurationDraftValidationError,
                'exact confirmation',
            ):
                workers.execute_draft_operation(replace(value, confirmation_text='wrong'))
        connect.assert_not_called()

        connection = Connection()
        with mock.patch.object(workers.drafts, 'rollback_to_revision') as write:
            with self.assertRaisesRegex(
                workers.OperatorGuildConfigurationDraftConflict,
                'digest changed',
            ):
                run_worker(
                    connection,
                    replace(value, expected_target_digest='0' * 64,
                            confirmation_text=f'ROLLBACK 1 {"0" * 64}'),
                )
        write.assert_not_called()
        self.assertEqual(connection.commits, 0)

    def test_intervening_active_revision_rejects_stale_preview_before_connection(self):
        value = request(workers.ROLLBACK_COMMIT)
        with mock.patch.object(workers.settings, 'owner_id', OWNER_ID), \
                mock.patch.object(workers, '_connect') as connect:
            with self.assertRaisesRegex(
                workers.OperatorGuildConfigurationDraftConflict,
                'changed after rollback preview',
            ):
                workers.execute_draft_operation(replace(
                    value,
                    runtime_revision=3,
                    runtime_generation=3,
                    runtime_document_digest='c' * 64,
                ))
        connect.assert_not_called()

    def test_command_capability_drift_and_noop_are_blocked(self):
        value = request()
        current = active_document()
        capability_source = service.replace_field(
            source_document(),
            service.FIELD_BY_KEY['command_capabilities'],
            source_document().command_capabilities[:-1],
        )
        connection = Connection()
        with mock.patch.object(workers.settings, 'owner_id', OWNER_ID), \
                mock.patch.object(workers, '_connect', return_value=connection), \
                mock.patch.object(workers, '_validate_schema'), \
                mock.patch.object(
                    workers, '_active',
                    return_value=(2, 2, current, document_digest(current)),
                ), mock.patch.object(
                    workers.drafts, 'select_revision',
                    return_value=(capability_source, document_digest(capability_source)),
                ):
            with self.assertRaisesRegex(
                workers.OperatorGuildConfigurationDraftValidationError,
                'different command capabilities',
            ):
                workers.execute_draft_operation(value)

        same = active_document()
        connection = Connection()
        with mock.patch.object(workers.settings, 'owner_id', OWNER_ID), \
                mock.patch.object(workers, '_connect', return_value=connection), \
                mock.patch.object(workers, '_validate_schema'), \
                mock.patch.object(
                    workers, '_active',
                    return_value=(2, 2, same, document_digest(same)),
                ), mock.patch.object(
                    workers.drafts, 'select_revision',
                    return_value=(same, document_digest(same)),
                ):
            with self.assertRaisesRegex(
                workers.OperatorGuildConfigurationDraftValidationError,
                'nothing to roll back',
            ):
                workers.execute_draft_operation(value)

    def test_postcommit_reload_failure_is_truthfully_committed(self):
        connection = Connection()
        committed = rollback_result()
        with mock.patch.object(
            workers.drafts, 'rollback_to_revision', return_value=committed,
        ), mock.patch.object(
            workers, '_post_commit_runtime_snapshot', side_effect=RuntimeError('down'),
        ):
            with self.assertRaisesRegex(
                workers.OperatorGuildConfigurationRollbackCommitted,
                'committed.*restart',
            ) as raised:
                run_worker(connection, request(workers.ROLLBACK_COMMIT))
        self.assertIs(raised.exception.rollback, committed)
        self.assertEqual(connection.commits, 1)

    def test_transaction_or_audit_failure_rolls_back_without_publication(self):
        connection = Connection()
        with mock.patch.object(
            workers.drafts,
            'rollback_to_revision',
            side_effect=drafts.GuildConfigurationDraftStorageError(
                'forced audit failure'
            ),
        ), mock.patch.object(
            workers, '_post_commit_runtime_snapshot',
        ) as reload_active:
            with self.assertRaisesRegex(
                workers.OperatorGuildConfigurationDraftConflict,
                'forced audit failure',
            ):
                run_worker(connection, request(workers.ROLLBACK_COMMIT))
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)
        reload_active.assert_not_called()


class RollbackAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_drains_worker_and_event_loop_remains_responsive(self):
        started = threading.Event()
        released = threading.Event()

        def slow(_request):
            started.set()
            released.wait(timeout=2)
            return mock.sentinel.result

        with mock.patch.object(workers.settings, 'owner_id', OWNER_ID), \
                mock.patch.object(workers, 'execute_draft_operation', slow):
            task = asyncio.create_task(workers.run_draft_operation(request()))
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.001)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            released.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        ticked = False

        def blocking(_request):
            time.sleep(0.05)
            return mock.sentinel.result

        async def ticker():
            nonlocal ticked
            await asyncio.sleep(0.005)
            ticked = True

        with mock.patch.object(workers.settings, 'owner_id', OWNER_ID), \
                mock.patch.object(workers, 'execute_draft_operation', blocking):
            await asyncio.gather(workers.run_draft_operation(request()), ticker())
        self.assertTrue(ticked)

    async def test_cancellation_after_commit_becomes_truthful_restart_result(self):
        started = threading.Event()
        released = threading.Event()
        committed = rollback_result()
        result = workers.GuildConfigurationDraftResult(
            operation=workers.ROLLBACK_COMMIT,
            guild_id=GUILD_ID,
            active_revision=3,
            active_generation=3,
            active_document_digest=committed.document_digest,
            draft=None,
            rollback=committed,
            runtime_snapshot=advanced_snapshot(),
            committed=True,
        )

        def slow(_request):
            started.set()
            released.wait(timeout=2)
            return result

        with mock.patch.object(workers.settings, 'owner_id', OWNER_ID), \
                mock.patch.object(workers, 'execute_draft_operation', slow):
            task = asyncio.create_task(workers.run_draft_operation(
                request(workers.ROLLBACK_COMMIT)
            ))
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.001)
            task.cancel()
            released.set()
            with self.assertRaises(
                workers.OperatorGuildConfigurationRollbackCommitted
            ) as raised:
                await task
        self.assertIs(raised.exception.rollback, committed)


class RollbackViewAndAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = administration.administration.__new__(administration.administration)
        self.cog.bot = SimpleNamespace(guilds=())
        operator = next(
            command for command in administration.administration.__cog_app_commands__
            if command.name == 'operator'
        )
        self.assertIsNone(operator.get_command('guild').get_command('rollback'))

    async def test_full_digest_modal_and_requester_bound_workspace(self):
        result = preview_result()

        async def runner(*_args, **_kwargs):
            return result

        workspace = views.GuildConfigurationRollbackWorkspace(
            requester_id=OWNER_ID,
            result=result,
            runner=runner,
        )
        modal = views.GuildConfigurationRollbackModal(workspace)
        self.assertEqual(modal.expected, result.rollback_preview.confirmation)
        self.assertEqual(modal.confirmation.max_length, len(modal.expected))
        self.assertIn(result.rollback_preview.source_document_digest, str(workspace.to_components()))

    async def test_committed_panel_edit_failure_uses_truthful_fallback(self):
        preview = preview_result()
        committed = rollback_result()
        result = workers.GuildConfigurationDraftResult(
            operation=workers.ROLLBACK_COMMIT,
            guild_id=GUILD_ID,
            active_revision=3,
            active_generation=3,
            active_document_digest=committed.document_digest,
            draft=None,
            rollback=committed,
            runtime_snapshot=advanced_snapshot(),
            runtime_published=True,
            committed=True,
        )

        async def runner(*_args, **_kwargs):
            return result

        workspace = views.GuildConfigurationRollbackWorkspace(
            requester_id=OWNER_ID,
            result=preview,
            runner=runner,
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(
                side_effect=[None, RuntimeError('panel unavailable')]
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        await workspace.commit(interaction, preview.rollback_preview.confirmation)
        self.assertTrue(workspace.terminal)
        self.assertIn('committed as revision 3', workspace.status)
        interaction.followup.send.assert_awaited_once()
        self.assertIn(
            'panel could not be updated',
            interaction.followup.send.call_args.args[0],
        )

    async def test_committed_reconciliation_warning_survives_panel_edit_failure(self):
        preview = preview_result()
        committed = rollback_result()

        async def runner(*_args, **_kwargs):
            raise workers.OperatorGuildConfigurationRollbackCommitted(committed)

        workspace = views.GuildConfigurationRollbackWorkspace(
            requester_id=OWNER_ID,
            result=preview,
            runner=runner,
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(
                side_effect=[None, RuntimeError('panel unavailable')]
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        await workspace.commit(interaction, preview.rollback_preview.confirmation)
        self.assertTrue(workspace.terminal)
        interaction.followup.send.assert_awaited_once()
        self.assertIn(
            'committed, but runtime publication could not be verified',
            interaction.followup.send.call_args.args[0],
        )

    async def test_commit_reconciles_once_and_publication_failure_is_truthful(self):
        current = runtime_record()
        committed = rollback_result()
        result = workers.GuildConfigurationDraftResult(
            operation=workers.ROLLBACK_COMMIT,
            guild_id=GUILD_ID,
            active_revision=3,
            active_generation=3,
            active_document_digest=committed.document_digest,
            draft=None,
            rollback=committed,
            runtime_snapshot=advanced_snapshot(),
            committed=True,
        )
        interaction = SimpleNamespace(
            guild_id=GUILD_ID,
            user=SimpleNamespace(id=OWNER_ID),
        )
        with mock.patch.object(administration.settings, 'runtime_profile', profile()), \
                mock.patch.object(
                    administration.settings,
                    'database_guild_configuration',
                    return_value=current,
                ), mock.patch.object(
                    service, 'build_rollback_request',
                    return_value=mock.sentinel.request,
                ), mock.patch.object(
                    workers, 'run_draft_operation',
                    new=mock.AsyncMock(return_value=result),
                ), mock.patch.object(
                    administration.settings,
                    'reconcile_database_guild_configuration',
                ) as reconcile:
            returned = await self.cog._operator_guild_draft_operation(
                interaction,
                workers.ROLLBACK_COMMIT,
                target_revision=1,
                expected_target_digest=committed.document_digest,
                expected_active_revision=2,
                expected_active_generation=2,
                expected_active_digest=current.document_digest,
                confirmation_text=f'ROLLBACK 1 {committed.document_digest}',
            )
        self.assertTrue(returned.runtime_published)
        reconcile.assert_called_once()

        with mock.patch.object(administration.settings, 'runtime_profile', profile()), \
                mock.patch.object(
                    administration.settings,
                    'database_guild_configuration',
                    return_value=current,
                ), mock.patch.object(
                    service, 'build_rollback_request',
                    return_value=mock.sentinel.request,
                ), mock.patch.object(
                    workers, 'run_draft_operation',
                    new=mock.AsyncMock(return_value=result),
                ), mock.patch.object(
                    administration.settings,
                    'reconcile_database_guild_configuration',
                    side_effect=RuntimeError('stale'),
                ), mock.patch.object(
                    administration.settings,
                    'quarantine_database_guild_configuration',
                ) as quarantine:
            with self.assertRaisesRegex(
                workers.OperatorGuildConfigurationRollbackCommitted,
                'committed.*restart',
            ):
                await self.cog._operator_guild_draft_operation(
                    interaction,
                    workers.ROLLBACK_COMMIT,
                    target_revision=1,
                    expected_target_digest=committed.document_digest,
                    expected_active_revision=2,
                    expected_active_generation=2,
                    expected_active_digest=current.document_digest,
                    confirmation_text=f'ROLLBACK 1 {committed.document_digest}',
                )
        quarantine.assert_called_once_with(GUILD_ID)


if __name__ == '__main__':
    unittest.main()
