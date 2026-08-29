"""Focused offline coverage for P10.6b1 owner draft editing."""

from __future__ import annotations

import asyncio
import datetime
from dataclasses import FrozenInstanceError, replace
import inspect
from types import SimpleNamespace
import threading
import time
import unittest
from unittest import mock

import discord

from modules import administration
from modules import guild_configuration_draft_storage as drafts
from modules import guild_configuration_delegation_storage as delegation
from modules import operator_guild_configuration_drafts as service
from modules import operator_guild_configuration_draft_views as views
from modules import operator_guild_configuration_draft_workers as workers
from modules.guild_configuration_schema import document_digest, document_to_mapping
from tests import test_guild_configuration_runtime as runtime_fixtures
from tests import test_guild_configuration_storage as fixtures


GUILD_ID = fixtures.GUILD_ID
OWNER_ID = int(workers.settings.owner_id)
NOW = datetime.datetime(2026, 8, 11, 12, 0, tzinfo=datetime.UTC)


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
    return runtime_fixtures.snapshot().guilds[GUILD_ID]


def request(operation=workers.SHOW, **kwargs):
    return workers.request_from_profile(
        profile=profile(), requester_id=OWNER_ID, guild_id=GUILD_ID,
        operation=operation, runtime_record=runtime_record(), **kwargs,
    )


def manager_request(operation=workers.SHOW, **kwargs):
    requester_role_ids = kwargs.pop('requester_role_ids', (900,))
    requester_is_guild_owner = kwargs.pop('requester_is_guild_owner', False)
    return workers.request_from_profile(
        profile=profile(), requester_id=OWNER_ID + 1, guild_id=GUILD_ID,
        invoking_guild_id=GUILD_ID,
        requester_role_ids=requester_role_ids,
        requester_is_guild_owner=requester_is_guild_owner,
        operation=operation, runtime_record=runtime_record(), **kwargs,
    )


def manager_policy(*, activation=False):
    return delegation.GuildConfigurationDelegation(
        guild_id=GUILD_ID, policy_version=1, manager_role_ids=(900,),
        allow_activation=activation, actor=f'discord:{OWNER_ID}',
        created_at=NOW.isoformat(), updated_at=NOW.isoformat(),
    )


def stored(document=None, *, version=1, base_revision=1, base_generation=1):
    document = document or fixtures.bundle().imports[0].document
    return drafts.StoredGuildConfigurationDraft(
        guild_id=GUILD_ID, draft_version=version,
        base_revision=base_revision, base_generation=base_generation,
        document_digest=document_digest(document), actor=f'discord:{OWNER_ID}',
        created_at=NOW.isoformat(), updated_at=NOW.isoformat(),
        expires_at=(NOW + datetime.timedelta(hours=24)).isoformat(),
        document=document,
    )


def activation(document=None):
    document = document or fixtures.bundle().imports[0].document
    return drafts.GuildConfigurationActivation(
        guild_id=GUILD_ID, previous_revision=1, previous_generation=1,
        revision=2, generation=2, event_number=2,
        document_digest=document_digest(document), source_digest='a' * 64,
        actor=f'discord:{OWNER_ID}', document=document,
    )


class Cursor:
    def __init__(self, *, readonly=True):
        self.readonly = readonly
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


def run_with(connection, value, *, selected=None):
    active = fixtures.bundle().imports[0].document
    selected = stored() if selected is None else selected
    with mock.patch.object(workers.settings, 'owner_id', OWNER_ID), \
            mock.patch.object(workers, '_connect', return_value=connection), \
            mock.patch.object(workers, '_validate_schema'), \
            mock.patch.object(
                workers, '_active',
                return_value=(1, 1, active, document_digest(active)),
            ), mock.patch.object(
                workers.drafts, 'select_draft', return_value=selected,
            ):
        return workers.execute_draft_operation(value)


class RequestAndWorkerTests(unittest.TestCase):
    def test_request_accepts_production_local_peer_authentication(self):
        selected = profile()
        selected.environment = drafts.storage.PRODUCTION_ENVIRONMENT
        selected.database_name = drafts.storage.PRODUCTION_DATABASE
        selected.database_user = drafts.storage.PRODUCTION_ROLE
        selected.database_password = ''
        selected.database_host = None
        selected.database_port = None
        selected.expected_bot_id = drafts.storage.PRODUCTION_APPLICATION_ID
        selected.background_tasks_enabled = True
        selected.bullet_enabled = True

        value = workers.request_from_profile(
            profile=selected, requester_id=OWNER_ID, guild_id=GUILD_ID,
            operation=workers.SHOW, runtime_record=runtime_record(),
        )

        self.assertEqual(value.database_password, '')
        self.assertIsNone(value.database_host)

    def test_request_is_frozen_and_owner_is_checked_before_connection(self):
        value = request()
        with self.assertRaises(FrozenInstanceError):
            value.operation = workers.RESET
        denied = replace(value, requester_id=OWNER_ID + 1)
        with mock.patch.object(workers.settings, 'owner_id', OWNER_ID), \
                mock.patch.object(workers, '_connect') as connect:
            with self.assertRaisesRegex(
                workers.OperatorGuildConfigurationDraftPermissionError,
                'configured bot owner',
            ):
                workers.execute_draft_operation(denied)
        connect.assert_not_called()

    def test_tampered_allowlist_and_non_database_authority_fail_closed(self):
        value = request()
        for allowed in ((True, GUILD_ID), (0, GUILD_ID), (GUILD_ID, GUILD_ID)):
            with self.subTest(allowed=allowed), self.assertRaisesRegex(
                workers.OperatorGuildConfigurationDraftValidationError,
                'allowlist',
            ):
                workers.execute_draft_operation(replace(value, allowed_guild_ids=allowed))
        selected = profile()
        selected.guild_configuration_source = 'static'
        with self.assertRaisesRegex(
            workers.OperatorGuildConfigurationDraftValidationError,
                'database authority',
        ):
            workers.request_from_profile(
                profile=selected, requester_id=OWNER_ID, guild_id=GUILD_ID,
                operation=workers.SHOW, runtime_record=runtime_record(),
            )

    def test_show_owns_one_read_only_connection(self):
        connection = Connection()
        result = run_with(connection, request())
        self.assertEqual(result.draft, stored())
        self.assertEqual(connection.sessions, [{
            'readonly': True, 'autocommit': False,
            'isolation_level': 'REPEATABLE READ',
        }])
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)
        self.assertTrue(connection.closed)

    def test_reset_uses_active_snapshot_and_commits_only_draft(self):
        connection = Connection()
        fresh = stored(version=2)
        with mock.patch.object(workers.drafts, 'put_draft', return_value=fresh) as put:
            result = run_with(connection, request(workers.RESET))
        self.assertEqual(result.draft, fresh)
        self.assertTrue(result.committed)
        self.assertEqual(connection.commits, 1)
        put.assert_called_once()
        self.assertEqual(put.call_args.kwargs['actor'], f'discord:{OWNER_ID}')

    def test_replace_requires_complete_cas_and_commits(self):
        active = fixtures.bundle().imports[0].document
        replacement_document = service.replace_field(
            active, service.FIELD_BY_KEY['display_name'], 'Edited Guild',
        )
        old = stored()
        updated = stored(replacement_document, version=2)
        value = request(
            workers.REPLACE,
            expected_draft_version=old.draft_version,
            expected_draft_digest=old.document_digest,
            replacement_document=document_to_mapping(replacement_document),
        )
        connection = Connection()
        with mock.patch.object(
            workers.drafts, 'replace_draft', return_value=updated,
        ) as write:
            result = run_with(connection, value, selected=old)
        self.assertEqual(result.draft, updated)
        self.assertEqual(connection.commits, 1)
        self.assertEqual(write.call_args.kwargs['expected_version'], 1)
        self.assertEqual(write.call_args.kwargs['document'], replacement_document)

    def test_discard_expires_inactive_row_and_never_deletes(self):
        old = stored()
        value = request(
            workers.DISCARD, expected_draft_version=1,
            expected_draft_digest=old.document_digest,
        )
        connection = Connection()
        with mock.patch.object(workers.drafts, 'expire_draft') as expire:
            result = run_with(connection, value, selected=old)
        self.assertIsNone(result.draft)
        self.assertTrue(result.committed)
        expire.assert_called_once()
        source = inspect.getsource(workers) + inspect.getsource(drafts.expire_draft)
        self.assertNotIn('DELETE FROM', source)

    def test_stale_active_base_fails_before_write_and_rolls_back(self):
        old = stored(base_generation=2)
        connection = Connection()
        with mock.patch.object(workers.drafts, 'replace_draft') as write:
            with self.assertRaisesRegex(
                workers.OperatorGuildConfigurationDraftConflict, 'older active revision'
            ):
                run_with(connection, request(), selected=old)
        write.assert_not_called()
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)

    def test_failed_cas_rolls_back_without_committed_result(self):
        old = stored()
        active = old.document
        value = request(
            workers.REPLACE,
            expected_draft_version=old.draft_version,
            expected_draft_digest=old.document_digest,
            replacement_document=document_to_mapping(active),
        )
        connection = Connection()
        with mock.patch.object(
            workers.drafts,
            'replace_draft',
            side_effect=drafts.GuildConfigurationDraftStorageError('stale'),
        ):
            with self.assertRaisesRegex(
                workers.OperatorGuildConfigurationDraftConflict, 'stale'
            ):
                run_with(connection, value, selected=old)
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)
        self.assertTrue(connection.closed)

    def test_validation_uses_frozen_live_snapshot_without_write(self):
        connection = Connection()
        value = request(workers.VALIDATE, discord_snapshot=fixtures.snapshot())
        result = run_with(connection, value)
        self.assertTrue(result.validation.live_references_valid)
        self.assertFalse(result.committed)
        self.assertEqual(connection.commits, 0)

    def test_activation_commits_then_reloads_complete_runtime_snapshot(self):
        active = fixtures.bundle().imports[0].document
        edited = service.replace_field(
            active, service.FIELD_BY_KEY['display_name'], 'Activated Guild',
        )
        old = stored(edited)
        value = request(
            workers.ACTIVATE,
            expected_draft_version=old.draft_version,
            expected_draft_digest=old.document_digest,
            discord_snapshot=fixtures.snapshot(),
        )
        connection = Connection()
        committed = activation(edited)
        reloaded = runtime_fixtures.snapshot()
        advanced_record = replace(
            reloaded.guilds[GUILD_ID], revision=2, generation=2,
            document=edited, document_digest=document_digest(edited),
        )
        reloaded = replace(
            reloaded,
            guilds={GUILD_ID: advanced_record},
        )
        with mock.patch.object(
            workers.drafts, 'activate_draft', return_value=committed,
        ) as activate_write, mock.patch.object(
            workers, '_post_commit_runtime_snapshot', return_value=reloaded,
        ) as reload_active:
            result = run_with(connection, value, selected=old)
        self.assertEqual(connection.commits, 1)
        self.assertIs(result.activation, committed)
        self.assertIs(result.runtime_snapshot, reloaded)
        self.assertIsNone(result.draft)
        activate_write.assert_called_once()
        reload_active.assert_called_once_with(value)

    def test_activation_reload_failure_reports_committed_reconciliation(self):
        active = fixtures.bundle().imports[0].document
        edited = service.replace_field(
            active, service.FIELD_BY_KEY['display_name'], 'Activated Guild',
        )
        old = stored(edited)
        value = request(
            workers.ACTIVATE,
            expected_draft_version=1,
            expected_draft_digest=old.document_digest,
            discord_snapshot=fixtures.snapshot(),
        )
        connection = Connection()
        committed = activation(edited)
        with mock.patch.object(
            workers.drafts, 'activate_draft', return_value=committed,
        ), mock.patch.object(
            workers, '_post_commit_runtime_snapshot', side_effect=RuntimeError('down'),
        ):
            with self.assertRaisesRegex(
                workers.OperatorGuildConfigurationActivationCommitted,
                'committed.*restart',
            ) as raised:
                run_with(connection, value, selected=old)
        self.assertIs(raised.exception.activation, committed)
        self.assertEqual(connection.commits, 1)

    def test_activation_can_publish_derived_capabilities_without_discord_sync(self):
        active = fixtures.bundle().imports[0].document
        edited = service.replace_field(
            active,
            service.FIELD_BY_KEY['command_capabilities'],
            active.command_capabilities[:-1],
        )
        old = stored(edited)
        value = request(
            workers.ACTIVATE,
            expected_draft_version=1,
            expected_draft_digest=old.document_digest,
            discord_snapshot=fixtures.snapshot(),
        )
        connection = Connection()
        committed = SimpleNamespace(
            revision=2,
            generation=2,
            document_digest=old.document_digest,
            document=edited,
        )
        published = SimpleNamespace(
            guilds={GUILD_ID: SimpleNamespace(
                revision=2,
                generation=2,
                document_digest=old.document_digest,
            )},
        )
        with mock.patch.object(
            workers.drafts, 'activate_draft', return_value=committed,
        ) as write, mock.patch.object(
            workers, '_post_commit_runtime_snapshot', return_value=published,
        ):
            result = run_with(connection, value, selected=old)
        write.assert_called_once()
        self.assertIs(result.activation, committed)
        self.assertEqual(connection.commits, 1)

    def test_activation_blocks_unchanged_draft_before_write(self):
        old = stored()
        value = request(
            workers.ACTIVATE,
            expected_draft_version=1,
            expected_draft_digest=old.document_digest,
            discord_snapshot=fixtures.snapshot(),
        )
        connection = Connection()
        with mock.patch.object(workers.drafts, 'activate_draft') as write:
            with self.assertRaisesRegex(
                workers.OperatorGuildConfigurationDraftValidationError,
                'nothing to activate',
            ):
                run_with(connection, value, selected=old)
        write.assert_not_called()
        self.assertEqual(connection.commits, 0)

    def test_unavailable_connection_has_no_fallback(self):
        with mock.patch.object(workers.settings, 'owner_id', OWNER_ID), \
                mock.patch.object(
                    workers, '_connect',
                    side_effect=workers.psycopg2.OperationalError('down'),
                ):
            with self.assertRaisesRegex(
                workers.OperatorGuildConfigurationDraftUnavailable, 'unavailable'
            ):
                workers.execute_draft_operation(request())

    def test_delegated_manager_is_rechecked_and_can_edit_only_ordinary_fields(self):
        active = fixtures.bundle().imports[0].document
        edited = service.replace_field(
            active, service.FIELD_BY_KEY['display_name'], 'Manager edit',
        )
        edited = service.replace_field(
            edited, service.FIELD_BY_KEY['allow_uneven_teams'], False,
        )
        edited = service.replace_field(
            edited, service.FIELD_BY_KEY['max_team_size'], 2,
        )
        edited = service.replace_field(
            edited, service.FIELD_BY_KEY['staff_help_channel'], 301,
        )
        old = stored()
        updated = stored(edited, version=2)
        value = manager_request(
            workers.REPLACE,
            expected_draft_version=old.draft_version,
            expected_draft_digest=old.document_digest,
            replacement_document=document_to_mapping(edited),
        )
        connection = Connection()
        with mock.patch.object(
            workers.delegation, 'select_delegation', return_value=manager_policy(),
        ), mock.patch.object(
            workers.drafts, 'replace_draft', return_value=updated,
        ) as write:
            result = run_with(connection, value, selected=old)
        self.assertTrue(result.delegated)
        self.assertFalse(result.activation_allowed)
        write.assert_called_once()

    def test_discord_guild_owner_has_ordinary_edit_and_activation_fallback(self):
        active = fixtures.bundle().imports[0].document
        edited = service.replace_field(
            active, service.FIELD_BY_KEY['display_name'], 'Owner edit',
        )
        old = stored(edited)
        value = manager_request(
            workers.ACTIVATE,
            requester_role_ids=(),
            requester_is_guild_owner=True,
            expected_draft_version=old.draft_version,
            expected_draft_digest=old.document_digest,
            discord_snapshot=fixtures.snapshot(),
        )
        committed = activation(edited)
        reloaded = runtime_fixtures.snapshot()
        reloaded = replace(
            reloaded,
            guilds={GUILD_ID: replace(
                reloaded.guilds[GUILD_ID],
                revision=2,
                generation=2,
                document=edited,
                document_digest=document_digest(edited),
            )},
        )
        connection = Connection()
        with mock.patch.object(
            workers.delegation, 'select_delegation',
        ) as delegation_read, mock.patch.object(
            workers.drafts, 'activate_draft', return_value=committed,
        ), mock.patch.object(
            workers, '_post_commit_runtime_snapshot', return_value=reloaded,
        ):
            result = run_with(connection, value, selected=old)
        self.assertTrue(result.delegated)
        self.assertTrue(result.activation_allowed)
        delegation_read.assert_not_called()

    def test_discord_guild_owner_fallback_still_rejects_protected_settings(self):
        active = fixtures.bundle().imports[0].document
        protected = service.replace_field(
            active, service.FIELD_BY_KEY['allow_teams'], False,
        )
        value = manager_request(
            requester_role_ids=(),
            requester_is_guild_owner=True,
        )
        with self.assertRaisesRegex(
            workers.OperatorGuildConfigurationDraftPermissionError,
            'owner-only configuration',
        ):
            run_with(Connection(), value, selected=stored(protected))

    def test_delegated_manager_cannot_read_or_overwrite_protected_draft(self):
        active = fixtures.bundle().imports[0].document
        protected_documents = {
            'helper_roles': service.replace_field(
                active, service.FIELD_BY_KEY['helper_roles'], (999,),
            ),
            'allow_teams': service.replace_field(
                active, service.FIELD_BY_KEY['allow_teams'], False,
            ),
            'require_teams': service.replace_field(
                active, service.FIELD_BY_KEY['require_teams'], True,
            ),
            'global_leaderboard': service.replace_field(
                active, service.FIELD_BY_KEY['global_leaderboard'], False,
            ),
        }
        for field, protected in protected_documents.items():
            with self.subTest(field=field), mock.patch.object(
                workers.delegation,
                'select_delegation',
                return_value=manager_policy(),
            ):
                with self.assertRaisesRegex(
                    workers.OperatorGuildConfigurationDraftPermissionError,
                    'owner-only configuration',
                ):
                    run_with(
                        Connection(),
                        manager_request(),
                        selected=stored(protected),
                    )

    def test_delegated_activation_requires_separate_owner_opt_in(self):
        active = fixtures.bundle().imports[0].document
        edited = service.replace_field(
            active, service.FIELD_BY_KEY['display_name'], 'Manager edit',
        )
        old = stored(edited)
        value = manager_request(
            workers.ACTIVATE,
            expected_draft_version=1,
            expected_draft_digest=old.document_digest,
            discord_snapshot=fixtures.snapshot(),
        )
        connection = Connection()
        with mock.patch.object(
            workers.delegation, 'select_delegation', return_value=manager_policy(),
        ), mock.patch.object(workers.drafts, 'activate_draft') as write:
            with self.assertRaisesRegex(
                workers.OperatorGuildConfigurationDraftPermissionError,
                'activation owner-only',
            ):
                run_with(connection, value, selected=old)
        write.assert_not_called()

    def test_delegated_activation_opt_in_uses_existing_atomic_publication_path(self):
        active = fixtures.bundle().imports[0].document
        edited = service.replace_field(
            active, service.FIELD_BY_KEY['display_name'], 'Manager activation',
        )
        old = stored(edited)
        value = manager_request(
            workers.ACTIVATE,
            expected_draft_version=1,
            expected_draft_digest=old.document_digest,
            discord_snapshot=fixtures.snapshot(),
        )
        connection = Connection()
        committed = activation(edited)
        reloaded = runtime_fixtures.snapshot()
        reloaded = replace(
            reloaded,
            guilds={GUILD_ID: replace(
                reloaded.guilds[GUILD_ID],
                revision=2,
                generation=2,
                document=edited,
                document_digest=document_digest(edited),
            )},
        )
        with mock.patch.object(
            workers.delegation,
            'select_delegation',
            return_value=manager_policy(activation=True),
        ), mock.patch.object(
            workers.drafts, 'activate_draft', return_value=committed,
        ) as write, mock.patch.object(
            workers, '_post_commit_runtime_snapshot', return_value=reloaded,
        ):
            result = run_with(connection, value, selected=old)
        self.assertTrue(result.delegated)
        self.assertTrue(result.activation_allowed)
        self.assertIs(result.activation, committed)
        self.assertEqual(connection.commits, 1)
        write.assert_called_once()

    def test_delegated_role_and_same_guild_are_required_before_database_use(self):
        value = manager_request()
        with mock.patch.object(workers.settings, 'owner_id', OWNER_ID), \
                mock.patch.object(workers, '_connect') as connect:
            with self.assertRaisesRegex(
                workers.OperatorGuildConfigurationDraftPermissionError,
                'configured bot owner',
            ):
                workers.execute_draft_operation(replace(
                    value, invoking_guild_id=GUILD_ID + 1,
                ))
        connect.assert_not_called()

        connection = Connection()
        with mock.patch.object(
            workers.delegation, 'select_delegation', return_value=manager_policy(),
        ):
            with self.assertRaisesRegex(
                workers.OperatorGuildConfigurationDraftPermissionError,
                'currently hold',
            ):
                run_with(
                    connection,
                    replace(value, requester_role_ids=(901,)),
                )


class AsyncOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_drains_owned_worker(self):
        started = threading.Event()
        released = threading.Event()
        completed = threading.Event()

        def slow(_request):
            started.set()
            released.wait(timeout=2)
            completed.set()
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
        self.assertTrue(completed.is_set())

    async def test_worker_keeps_event_loop_responsive(self):
        def slow(_request):
            time.sleep(0.05)
            return mock.sentinel.result

        ticked = False

        async def ticker():
            nonlocal ticked
            await asyncio.sleep(0.005)
            ticked = True

        with mock.patch.object(workers.settings, 'owner_id', OWNER_ID), \
                mock.patch.object(workers, 'execute_draft_operation', slow):
            result, _ = await asyncio.gather(
                workers.run_draft_operation(request()), ticker()
            )
        self.assertIs(result, mock.sentinel.result)
        self.assertTrue(ticked)


class EditServiceAndViewTests(unittest.IsolatedAsyncioTestCase):
    def test_editor_groups_fields_into_three_clear_sections(self):
        self.assertEqual(service.SECTIONS, (
            service.SERVER_BASICS,
            service.ROLES,
            service.CHANNELS_AND_MESSAGES,
            service.CAPABILITIES,
        ))
        self.assertEqual(
            views.SECTION_LABELS[service.SERVER_BASICS],
            'Server Basics',
        )
        self.assertEqual(views.SECTION_LABELS[service.ROLES], 'Roles')
        self.assertEqual(
            views.SECTION_LABELS[service.CHANNELS_AND_MESSAGES],
            'Channels and Messages',
        )
        basics = {
            field.key for field in service.fields_for_section(
                service.SERVER_BASICS,
            )
        }
        self.assertIn('display_name', basics)
        self.assertIn('allow_teams', basics)
        channels = {
            field.key for field in service.fields_for_section(
                service.CHANNELS_AND_MESSAGES,
            )
        }
        self.assertIn('bot_channels', channels)
        self.assertIn('game_announce_channel', channels)

    async def test_section_landing_has_info_and_no_preselected_field(self):
        active = fixtures.bundle().imports[0].document
        result = workers.GuildConfigurationDraftResult(
            operation=workers.SHOW, guild_id=GUILD_ID,
            active_revision=1, active_generation=1,
            active_document_digest=document_digest(active), draft=stored(),
        )

        async def runner(*_args, **_kwargs):
            return result

        workspace = views.GuildConfigurationDraftWorkspace(
            requester_id=OWNER_ID, active_document=active, result=result,
            runner=runner, role_names={}, channel_names={}, simple_owner=True,
        )
        self.assertIsNone(workspace.field_key)
        self.assertEqual(workspace.sections, (
            service.SERVER_BASICS,
            service.ROLES,
            service.CHANNELS_AND_MESSAGES,
        ))

        def assert_landing(section):
            items = tuple(workspace.walk_children())
            rendered = '\n'.join(
                item.content
                for item in items
                if isinstance(item, discord.ui.TextDisplay)
            )
            self.assertIn(views.SECTION_DESCRIPTIONS[section], rendered)
            self.assertIn('Choose a setting below', rendered)
            field_select = next(
                item
                for item in items
                if isinstance(item, discord.ui.Select)
                and item.placeholder == 'Choose one field to edit'
            )
            self.assertFalse(any(option.default for option in field_select.options))

        assert_landing(service.SERVER_BASICS)
        workspace.field_key = 'display_name'
        workspace.ready = mock.AsyncMock(return_value=True)
        interaction = SimpleNamespace(
            response=SimpleNamespace(edit_message=mock.AsyncMock()),
        )
        await workspace._select_section(
            interaction,
            SimpleNamespace(values=[service.ROLES]),
        )
        self.assertEqual(workspace.section, service.ROLES)
        self.assertIsNone(workspace.field_key)
        assert_landing(service.ROLES)

    def test_every_settings_field_has_bounded_explanatory_text(self):
        self.assertTrue(service.FIELDS)
        for field in service.FIELDS:
            with self.subTest(field=field.key):
                self.assertTrue(field.help_text.strip())
                self.assertLessEqual(
                    len(views._option_description(field.help_text)),
                    100,
                )

    def test_editor_covers_every_mutable_document_leaf_once(self):
        document = document_to_mapping(fixtures.bundle().imports[0].document)

        def leaves(value, prefix=()):
            if isinstance(value, dict):
                result = set()
                for key, child in value.items():
                    result |= leaves(child, (*prefix, key))
                return result
            return {prefix}

        immutable = {('schema_version',), ('guild_id',)}
        self.assertEqual({field.path for field in service.FIELDS}, leaves(document) - immutable)
        self.assertEqual(len(service.FIELD_BY_KEY), len(service.FIELDS))

    def test_typed_edits_preserve_complete_validation_and_diff(self):
        active = fixtures.bundle().imports[0].document
        edited = service.replace_field(
            active, service.FIELD_BY_KEY['display_name'], 'Edited Guild'
        )
        edited = service.add_id(
            edited, service.FIELD_BY_KEY['helper_roles'], 999
        )
        edited = service.replace_field(
            edited, service.FIELD_BY_KEY['bot_channels'], None
        )
        self.assertEqual(edited.identity.display_name, 'Edited Guild')
        self.assertIn(999, edited.permissions.helper_role_ids)
        self.assertIsNone(edited.channels.bot_channel_ids)
        self.assertEqual(service.changed_paths(active, edited), (
            'channels.bot_channel_ids', 'identity.display_name',
            'permissions.helper_role_ids',
        ))
        with self.assertRaisesRegex(service.GuildConfigurationDraftEditError, 'already'):
            service.add_id(edited, service.FIELD_BY_KEY['helper_roles'], 999)

    def test_cross_field_validation_blocks_invalid_intermediate_document(self):
        active = fixtures.bundle().imports[0].document
        require = service.replace_field(
            active, service.FIELD_BY_KEY['require_teams'], True
        )
        with self.assertRaisesRegex(
            service.GuildConfigurationDraftEditError, 'cannot be true'
        ):
            service.replace_field(
                require, service.FIELD_BY_KEY['allow_teams'], False
            )

    def test_requester_role_snapshot_excludes_everyone_and_managed_roles(self):
        roles = (
            SimpleNamespace(
                id=GUILD_ID, managed=False, is_default=lambda: True,
            ),
            SimpleNamespace(id=800, managed=True, is_default=lambda: False),
            SimpleNamespace(id=900, managed=False, is_default=lambda: False),
        )
        interaction = SimpleNamespace(user=SimpleNamespace(roles=roles))
        self.assertEqual(service.requester_role_ids(interaction), (900,))

    def test_workspace_builds_every_field_kind_and_is_private_safe(self):
        active = fixtures.bundle().imports[0].document
        result = workers.GuildConfigurationDraftResult(
            operation=workers.SHOW, guild_id=GUILD_ID,
            active_revision=1, active_generation=1,
            active_document_digest=document_digest(active), draft=stored(),
        )

        async def runner(*_args, **_kwargs):
            return result

        workspace = views.GuildConfigurationDraftWorkspace(
            requester_id=OWNER_ID, active_document=active, result=result,
            runner=runner, role_names={}, channel_names={},
        )
        for field in service.FIELDS:
            with self.subTest(field=field.key):
                workspace.section = field.section
                workspace.field_key = field.key
                workspace.rebuild()
                self.assertEqual(len(workspace.children), 1)
                items = tuple(workspace.walk_children())
                rendered = '\n'.join(
                    item.content
                    for item in items
                    if isinstance(item, discord.ui.TextDisplay)
                )
                self.assertIn(field.help_text, rendered)
                field_select = next(
                    item
                    for item in items
                    if isinstance(item, discord.ui.Select)
                    and item.placeholder == 'Choose one field to edit'
                )
                selected_option = next(
                    option
                    for option in field_select.options
                    if option.value == field.key
                )
                self.assertEqual(
                    selected_option.description,
                    views._option_description(field.help_text),
                )
        self.assertIn('activate', workspace.status.lower())
        self.assertNotIn(
            '_refresh',
            views.GuildConfigurationDraftWorkspace.__dict__,
        )

    def test_identity_maps_show_names_not_only_ids(self):
        guild = SimpleNamespace(
            roles=(SimpleNamespace(id=1, name='Helpers'),),
            channels=(SimpleNamespace(id=2, name='staff-help'),),
        )
        self.assertEqual(views.identity_maps(guild), ({1: 'Helpers'}, {2: 'staff-help'}))

    async def test_activation_button_keeps_digest_internal(self):
        active = fixtures.bundle().imports[0].document
        result = workers.GuildConfigurationDraftResult(
            operation=workers.VALIDATE, guild_id=GUILD_ID,
            active_revision=1, active_generation=1,
            active_document_digest=document_digest(active), draft=stored(),
            validation=workers.GuildConfigurationDraftValidation(
                True, True, True, True,
            ),
        )

        async def runner(*_args, **_kwargs):
            return result

        workspace = views.GuildConfigurationDraftWorkspace(
            requester_id=OWNER_ID, active_document=active, result=result,
            runner=runner, role_names={}, channel_names={},
        )
        workspace.ready = mock.AsyncMock(return_value=True)
        workspace.run_operation = mock.AsyncMock()
        interaction = mock.sentinel.interaction
        await workspace._activate(interaction)
        workspace.run_operation.assert_awaited_once_with(
            interaction, workers.ACTIVATE,
        )
        self.assertNotIn(
            result.draft.document_digest,
            str(workspace.to_components()),
        )

    async def test_simple_owner_workspace_uses_readable_save_and_cancel_flow(self):
        active = fixtures.bundle().imports[0].document
        edited = service.replace_field(
            active,
            service.FIELD_BY_KEY['display_name'],
            'Edited Guild',
        )
        result = workers.GuildConfigurationDraftResult(
            operation=workers.SHOW, guild_id=GUILD_ID,
            active_revision=1, active_generation=1,
            active_document_digest=document_digest(active), draft=stored(edited),
        )

        async def runner(*_args, **_kwargs):
            return result

        workspace = views.GuildConfigurationDraftWorkspace(
            requester_id=OWNER_ID, active_document=active, result=result,
            runner=runner, role_names={}, channel_names={},
            target_guild_name='Development Test Guild', simple_owner=True,
        )
        self.assertNotIn(service.CAPABILITIES, workspace.sections)
        items = tuple(workspace.walk_children())
        rendered = '\n'.join(
            item.content
            for item in items
            if isinstance(item, discord.ui.TextDisplay)
        )
        button_labels = {
            item.label
            for item in items
            if isinstance(item, discord.ui.Button) and item.label is not None
        }
        self.assertIn('Display name', rendered)
        self.assertIn('Development Test Guild', rendered)
        self.assertIn('Edited Guild', rendered)
        self.assertIn('Save changes', button_labels)
        self.assertIn('Cancel', button_labels)
        self.assertNotIn('Digest', rendered)
        self.assertNotIn('Validate', button_labels)
        self.assertNotIn('Activate', button_labels)
        workspace.ready = mock.AsyncMock(return_value=True)
        workspace.save = mock.AsyncMock()
        interaction = mock.sentinel.interaction
        await workspace._save(interaction)
        workspace.save.assert_awaited_once_with(interaction)

    def test_delegated_workspace_exposes_only_ordinary_fields_and_disables_activation(self):
        active = fixtures.bundle().imports[0].document
        result = workers.GuildConfigurationDraftResult(
            operation=workers.SHOW, guild_id=GUILD_ID,
            active_revision=1, active_generation=1,
            active_document_digest=document_digest(active), draft=stored(),
            delegated=True, activation_allowed=False,
        )

        async def runner(*_args, **_kwargs):
            return result

        workspace = views.GuildConfigurationDraftWorkspace(
            requester_id=OWNER_ID + 1, active_document=active, result=result,
            runner=runner, role_names={}, channel_names={}, ordinary_only=True,
        )
        self.assertEqual(workspace.sections, service.ORDINARY_SECTIONS)
        self.assertTrue(all(
            field.key in service.ORDINARY_FIELD_KEYS
            for section in workspace.sections
            for field in service.fields_for_section(section, ordinary_only=True)
        ))
        ordinary_labels = {
            field.label
            for section in workspace.sections
            for field in service.fields_for_section(
                section,
                ordinary_only=True,
            )
        }
        self.assertIn('Allow unequal side sizes', ordinary_labels)
        self.assertIn('Maximum players per side', ordinary_labels)
        self.assertIn('Staff-help channel', ordinary_labels)
        self.assertNotIn('Allow persistent Teams', ordinary_labels)
        self.assertNotIn('Require persistent Teams', ordinary_labels)
        self.assertNotIn('Include in global leaderboard', ordinary_labels)
        self.assertNotIn('Helper roles', ordinary_labels)
        self.assertNotIn('Command capabilities', ordinary_labels)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = administration.administration.__new__(administration.administration)
        self.cog.bot = SimpleNamespace(guilds=())
        self.guild = next(
            command for command in administration.administration.__cog_app_commands__
            if command.name == 'guild'
        )
        self.command = self.guild.get_command('settings')
        self.operator = next(
            command for command in administration.administration.__cog_app_commands__
            if command.name == 'operator'
        )
        self.capabilities_command = self.operator.get_command(
            'guild'
        ).get_command('capabilities')
        self.assertIsNotNone(self.command)
        self.assertIsNone(self.capabilities_command)
        self.assertIsNone(self.guild.get_command('edit'))

    async def test_preflight_denial_is_private_and_does_not_defer(self):
        interaction = SimpleNamespace(
            guild_id=GUILD_ID, user=SimpleNamespace(id=OWNER_ID + 1),
            response=SimpleNamespace(
                send_message=mock.AsyncMock(), defer=mock.AsyncMock()
            ),
        )
        with mock.patch.object(
            service,
            'delegated_access_error',
            return_value='Guild settings are unavailable.',
        ):
            await self.command.callback(self.cog, interaction)
        interaction.response.send_message.assert_awaited_once_with(
            'Guild settings are unavailable.',
            ephemeral=True,
        )
        interaction.response.defer.assert_not_awaited()

    async def test_settings_routes_owner_to_full_current_guild_editor(self):
        interaction = SimpleNamespace(
            guild_id=GUILD_ID,
            user=SimpleNamespace(id=OWNER_ID),
            response=SimpleNamespace(send_message=mock.AsyncMock()),
        )
        with mock.patch.object(
            service,
            'delegated_access_error',
            return_value=None,
        ), mock.patch.object(
            service.settings,
            'owner_id',
            OWNER_ID,
        ), mock.patch.object(
            self.cog,
            '_open_guild_draft_workspace',
            new=mock.AsyncMock(),
        ) as open_workspace:
            await self.command.callback(self.cog, interaction)
        open_workspace.assert_awaited_once_with(
            interaction,
            target=GUILD_ID,
            ordinary_only=False,
        )

    async def test_owner_editor_creates_private_draft_on_open(self):
        active = fixtures.bundle().imports[0].document
        shown = workers.GuildConfigurationDraftResult(
            operation=workers.SHOW, guild_id=GUILD_ID,
            active_revision=1, active_generation=1,
            active_document_digest=document_digest(active), draft=None,
        )
        reset = replace(shown, operation=workers.RESET, draft=stored())
        interaction = SimpleNamespace(
            guild_id=GUILD_ID,
            user=SimpleNamespace(id=OWNER_ID),
            response=SimpleNamespace(defer=mock.AsyncMock()),
        )
        target_guild = SimpleNamespace(
            id=GUILD_ID,
            name='Development Test Guild',
            roles=(),
            channels=(),
        )
        operation = mock.AsyncMock(side_effect=(shown, reset))
        with mock.patch.object(
            self.cog, '_operator_visible_target_guild', return_value=target_guild,
        ), mock.patch.object(
            self.cog, '_operator_guild_draft_operation', new=operation,
        ), mock.patch.object(
            administration.settings, 'owner_id', OWNER_ID,
        ), mock.patch.object(
            administration.settings,
            'database_guild_configuration',
            return_value=runtime_record(),
        ), mock.patch.object(
            views, 'publish_private', new=mock.AsyncMock(),
        ):
            workspace = await self.cog._open_guild_draft_workspace(
                interaction,
                target=GUILD_ID,
                ordinary_only=False,
            )
        self.assertEqual(
            [call.args[1] for call in operation.await_args_list],
            [workers.SHOW, workers.RESET],
        )
        self.assertTrue(workspace.simple_owner)
        self.assertEqual(workspace.target_guild_name, 'Development Test Guild')

    async def test_committed_edit_survives_publication_failure_for_refresh(self):
        active = fixtures.bundle().imports[0].document
        initial = workers.GuildConfigurationDraftResult(
            operation=workers.SHOW, guild_id=GUILD_ID,
            active_revision=1, active_generation=1,
            active_document_digest=document_digest(active), draft=stored(),
        )
        committed = replace(
            initial, operation=workers.REPLACE,
            draft=stored(active, version=2), committed=True,
        )

        async def runner(*_args, **_kwargs):
            return committed

        workspace = views.GuildConfigurationDraftWorkspace(
            requester_id=OWNER_ID, active_document=active, result=initial,
            runner=runner, role_names={}, channel_names={},
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(
                side_effect=[None, RuntimeError('publication failed')]
            ),
        )
        with self.assertRaisesRegex(RuntimeError, 'publication failed'):
            await workspace.run_operation(interaction, workers.REPLACE)
        self.assertEqual(workspace.result, committed)
        self.assertFalse(workspace.busy)

    async def test_refresh_validate_and_reset_do_not_send_edit_only_evidence(self):
        active = fixtures.bundle().imports[0].document
        result = workers.GuildConfigurationDraftResult(
            operation=workers.SHOW, guild_id=GUILD_ID,
            active_revision=1, active_generation=1,
            active_document_digest=document_digest(active), draft=stored(),
        )
        calls = []

        async def runner(*args, **kwargs):
            calls.append((args, kwargs))
            return result

        workspace = views.GuildConfigurationDraftWorkspace(
            requester_id=OWNER_ID, active_document=active, result=result,
            runner=runner, role_names={}, channel_names={},
        )
        for operation in (workers.SHOW, workers.VALIDATE, workers.RESET):
            interaction = SimpleNamespace(
                response=SimpleNamespace(defer=mock.AsyncMock()),
                edit_original_response=mock.AsyncMock(),
            )
            await workspace.run_operation(interaction, operation)
            kwargs = calls[-1][1]
            self.assertIsNone(kwargs['expected_draft_version'])
            self.assertIsNone(kwargs['expected_draft_digest'])

    async def test_simple_save_activates_directly_with_current_draft_evidence(self):
        active = fixtures.bundle().imports[0].document
        edited = service.replace_field(
            active, service.FIELD_BY_KEY['display_name'], 'Edited Guild',
        )
        initial = workers.GuildConfigurationDraftResult(
            operation=workers.SHOW, guild_id=GUILD_ID,
            active_revision=1, active_generation=1,
            active_document_digest=document_digest(active), draft=stored(edited),
        )
        activated = activation(edited)
        completed = workers.GuildConfigurationDraftResult(
            operation=workers.ACTIVATE, guild_id=GUILD_ID,
            active_revision=2, active_generation=2,
            active_document_digest=activated.document_digest, draft=None,
            activation=activated, committed=True, runtime_published=True,
        )
        calls = []

        async def runner(*args, **kwargs):
            calls.append((args, kwargs))
            return completed

        workspace = views.GuildConfigurationDraftWorkspace(
            requester_id=OWNER_ID, active_document=active, result=initial,
            runner=runner, role_names={}, channel_names={},
            target_guild_name='Development Test Guild', simple_owner=True,
        )
        interaction = SimpleNamespace(
            guild_id=None,
            user=SimpleNamespace(id=OWNER_ID),
            response=SimpleNamespace(defer=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(),
        )
        await workspace.save(interaction)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][1], workers.ACTIVATE)
        self.assertEqual(
            calls[0][1]['expected_draft_version'], initial.draft.draft_version,
        )
        self.assertEqual(
            calls[0][1]['expected_draft_digest'], initial.draft.document_digest,
        )
        self.assertTrue(workspace.terminal)
        self.assertIn('Saved and published', workspace.status)

    async def test_simple_cancel_discards_without_confirmation_ceremony(self):
        active = fixtures.bundle().imports[0].document
        initial = workers.GuildConfigurationDraftResult(
            operation=workers.SHOW, guild_id=GUILD_ID,
            active_revision=1, active_generation=1,
            active_document_digest=document_digest(active), draft=stored(),
        )
        discarded = replace(initial, operation=workers.DISCARD, draft=None)
        calls = []

        async def runner(*args, **kwargs):
            calls.append((args, kwargs))
            return discarded

        workspace = views.GuildConfigurationDraftWorkspace(
            requester_id=OWNER_ID, active_document=active, result=initial,
            runner=runner, role_names={}, channel_names={}, simple_owner=True,
        )
        interaction = SimpleNamespace(
            guild_id=None,
            user=SimpleNamespace(id=OWNER_ID),
            response=SimpleNamespace(defer=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(),
        )
        await workspace._cancel(interaction)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][1], workers.DISCARD)
        self.assertTrue(workspace.terminal)
        self.assertIn('unchanged', workspace.status)

    async def test_activation_reconciles_once_on_event_loop_after_worker(self):
        active = fixtures.bundle().imports[0].document
        current = runtime_record()
        activated = activation()
        result = workers.GuildConfigurationDraftResult(
            operation=workers.ACTIVATE, guild_id=GUILD_ID,
            active_revision=2, active_generation=2,
            active_document_digest=activated.document_digest, draft=None,
            activation=activated,
            runtime_snapshot=runtime_fixtures.snapshot(),
            committed=True,
        )
        interaction = SimpleNamespace(
            guild_id=GUILD_ID,
            user=SimpleNamespace(id=OWNER_ID),
        )
        selected_profile = profile()
        with mock.patch.object(
            administration.settings, 'runtime_profile', selected_profile,
        ), mock.patch.object(
            administration.settings, 'database_guild_ids', return_value=(GUILD_ID,),
        ), mock.patch.object(
            administration.settings,
            'database_guild_configuration',
            return_value=current,
        ), mock.patch.object(
            service, 'build_request', return_value=mock.sentinel.request,
        ), mock.patch.object(
            workers, 'run_draft_operation', mock.AsyncMock(return_value=result),
        ), mock.patch.object(
            administration.settings, 'reconcile_database_guild_configuration',
        ) as reconcile:
            returned = await self.cog._operator_guild_draft_operation(
                interaction,
                workers.ACTIVATE,
                expected_draft_version=1,
                expected_draft_digest=document_digest(active),
            )
        self.assertTrue(returned.runtime_published)
        reconcile.assert_called_once()
        self.assertEqual(
            reconcile.call_args.kwargs['expected_current'],
            {GUILD_ID: (1, 1, document_digest(active))},
        )

    async def test_activation_publication_failure_stays_truthfully_committed(self):
        current = runtime_record()
        activated = activation()
        result = workers.GuildConfigurationDraftResult(
            operation=workers.ACTIVATE, guild_id=GUILD_ID,
            active_revision=2, active_generation=2,
            active_document_digest=activated.document_digest, draft=None,
            activation=activated,
            runtime_snapshot=runtime_fixtures.snapshot(), committed=True,
        )
        interaction = SimpleNamespace(
            guild_id=GUILD_ID, user=SimpleNamespace(id=OWNER_ID),
        )
        with mock.patch.object(
            administration.settings, 'runtime_profile', profile(),
        ), mock.patch.object(
            administration.settings, 'database_guild_ids', return_value=(GUILD_ID,),
        ), mock.patch.object(
            administration.settings, 'database_guild_configuration',
            return_value=current,
        ), mock.patch.object(
            service, 'build_request', return_value=mock.sentinel.request,
        ), mock.patch.object(
            workers, 'run_draft_operation', mock.AsyncMock(return_value=result),
        ), mock.patch.object(
            administration.settings,
            'reconcile_database_guild_configuration',
            side_effect=RuntimeError('stale runtime'),
        ), mock.patch.object(
            administration.settings,
            'quarantine_database_guild_configuration',
        ) as quarantine:
            with self.assertRaisesRegex(
                workers.OperatorGuildConfigurationActivationCommitted,
                'committed.*restart',
            ):
                await self.cog._operator_guild_draft_operation(
                    interaction, workers.ACTIVATE,
                )
        quarantine.assert_called_once_with(GUILD_ID)


if __name__ == '__main__':
    unittest.main()
