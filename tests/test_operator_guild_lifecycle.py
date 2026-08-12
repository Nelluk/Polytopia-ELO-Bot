"""Focused Tier-3 coverage for P10.8 guild suspension and resumption."""

from __future__ import annotations

import asyncio
import copy
from concurrent.futures import Future
from dataclasses import fields, FrozenInstanceError, replace
import inspect
import json
from types import SimpleNamespace
import unittest
from unittest import mock

from modules import administration
from modules import guild_configuration_runtime as runtime
from modules import guild_configuration_storage as storage
from modules import operator_guild_command_capabilities as commands
from modules import operator_guild_configuration as configuration_service
from modules import operator_guild_configuration_workers as configuration_workers
from modules import operator_guild_lifecycle as service
from modules import operator_guild_lifecycle_views as views
from modules import operator_guild_lifecycle_workers as workers
from modules.guild_configuration_schema import document_digest, document_to_mapping
import settings
from tests import test_guild_configuration_runtime as runtime_fixtures
from tests import test_guild_configuration_storage as fixtures
from tests.test_operator_guild_command_capabilities import FakeCommand, FakeTree


CONTROL_ID = fixtures.GUILD_ID
TARGET_ID = CONTROL_ID + 99
OWNER_ID = int(settings.owner_id)


def profile():
    return SimpleNamespace(
        environment='development', database_name='polytopia_dev',
        database_user='polybot_dev', database_password='secret',
        database_host='localhost', database_port=5432,
        expected_bot_id=storage.DEVELOPMENT_BETA_APPLICATION_ID,
        background_tasks_enabled=False, api_enabled=False, bullet_enabled=False,
        allowed_guild_ids=(CONTROL_ID,), guild_configuration_source='database',
    )


def target_document():
    value = workers.validate_document({
        'schema_version': 1,
        'guild_id': TARGET_ID,
        'identity': {'display_name': 'Lifecycle Target', 'command_prefix': '$'},
        'permissions': {
            'helper_role_ids': [], 'mod_role_ids': [],
            'user_role_ids_level_1': [TARGET_ID],
            'user_role_ids_level_2': [TARGET_ID],
            'user_role_ids_level_3': [TARGET_ID],
            'user_role_ids_level_4': [], 'inactive_role_id': None,
        },
        'teams': {
            'require_teams': False, 'allow_teams': False,
            'allow_uneven_teams': False, 'max_team_size': 1,
        },
        'visibility': {'include_in_global_leaderboard': False},
        'channels': {
            'bot_channel_ids': None, 'strict_bot_channel_ids': None,
            'private_bot_channel_ids': [], 'newbie_message_channel_ids': [],
            'match_challenge_channel_ids': [], 'ranked_game_channel_id': None,
            'unranked_game_channel_id': None, 'steam_game_channel_id': None,
            'log_channel_id': None, 'game_announce_channel_id': None,
            'staff_help_channel_id': None, 'game_category_ids': [],
        },
        'command_capabilities': ['operator'],
    })
    return value


def discord_snapshot():
    value = copy.deepcopy(fixtures.snapshot())
    value['guilds'].append({
        'guild_id': TARGET_ID,
        'guild_name': 'Lifecycle Target',
        'roles': [{
            'id': TARGET_ID, 'name': '@everyone', 'managed': False,
            'is_default': True,
        }],
        'channels': [],
    })
    return value


def runtime_record(guild_id, *, generation=1, document=None):
    document = target_document() if document is None else document
    return SimpleNamespace(
        guild_id=guild_id,
        revision=1,
        generation=generation,
        document_digest=document_digest(document),
        document=document,
    )


def lifecycle_request(
    *,
    action=workers.SUSPEND,
    operation=workers.PREVIEW,
    state=None,
    generation=None,
    **changes,
):
    state = (
        workers.ACTIVE if action == workers.SUSPEND else workers.SUSPENDED
    ) if state is None else state
    generation = (1 if state == workers.ACTIVE else 2) if generation is None else generation
    control = runtime_fixtures.snapshot().guilds[CONTROL_ID]
    target = runtime_record(TARGET_ID, generation=generation)
    current = (
        (control, target) if state == workers.ACTIVE else (control,)
    )
    document = target_document()
    kwargs = {}
    if operation == workers.COMMIT:
        digest = document_digest(document)
        plan_digest = 'c' * 64
        kwargs = {
            'expected_state': state,
            'expected_revision': 1,
            'expected_generation': generation,
            'expected_document_digest': digest,
            'command_plan_digest': plan_digest,
            'confirmation_text': (
                f'{action.upper()} GUILD {TARGET_ID} {digest} {plan_digest}'
            ),
        }
    kwargs.update(changes)
    request = workers.request_from_profile(
        profile=profile(), requester_id=OWNER_ID,
        invoking_guild_id=CONTROL_ID, target_guild_id=TARGET_ID,
        target_guild_name='Lifecycle Target', current_runtime_records=current,
        discord_snapshot=discord_snapshot(), action=action,
        operation=operation, **kwargs,
    )
    return request, state, generation, document


class Cursor:
    def __init__(self, request, state, generation, document):
        self.request = request
        self.state = state
        self.generation = generation
        self.document = document
        self.row = None
        self.rows = ()
        self.rowcount = 0
        self.readonly = True
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
        elif 'WHERE registry.enrollment_state' in statement:
            self.rows = tuple(
                (value.guild_id, value.revision, value.generation, value.document_digest)
                for value in self.request.current_runtime
            )
        elif 'WHERE registry.guild_id' in statement:
            self.row = (
                self.state, 1, self.generation, self.document.schema_version,
                document_to_mapping(self.document), document_digest(self.document),
            )
        elif statement.startswith('UPDATE'):
            self.rowcount = 1
        elif statement.startswith('SELECT COALESCE'):
            self.row = (2,)

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, request, state, generation, document):
        self.cursor_value = Cursor(request, state, generation, document)
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def set_session(self, **kwargs):
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


class WorkerContractTests(unittest.TestCase):
    def run_worker(self, request, state, generation, document, *, post=None):
        connection = Connection(request, state, generation, document)
        patches = (
            mock.patch.object(workers.settings, 'owner_id', OWNER_ID),
            mock.patch.object(workers, '_connect', return_value=connection),
            mock.patch.object(
                storage, 'inspect_schema_inventory', return_value=exact_inventory()
            ),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        post_patch = mock.patch.object(
            workers,
            '_post_commit_snapshot',
            return_value=mock.sentinel.snapshot if post is None else post,
        )
        post_patch.start()
        self.addCleanup(post_patch.stop)
        return workers.execute_lifecycle(request), connection

    def test_request_is_frozen_owner_only_and_exact_confirmation_precedes_io(self):
        request, _state, _generation, _document = lifecycle_request()
        with self.assertRaises(FrozenInstanceError):
            request.action = workers.RESUME
        with mock.patch.object(workers, '_connect') as connect, self.assertRaisesRegex(
            workers.OperatorGuildLifecyclePermissionError,
            'owner',
        ):
            workers.execute_lifecycle(replace(request, requester_id=OWNER_ID + 1))
        connect.assert_not_called()

        commit, *_ = lifecycle_request(operation=workers.COMMIT)
        with mock.patch.object(workers, '_connect') as connect, self.assertRaisesRegex(
            workers.OperatorGuildLifecycleValidationError,
            'exact confirmation',
        ):
            workers.execute_lifecycle(replace(commit, confirmation_text='wrong'))
        connect.assert_not_called()

    def test_preview_is_read_only_and_preserves_revision_and_drafts(self):
        request, state, generation, document = lifecycle_request()
        result, connection = self.run_worker(request, state, generation, document)
        self.assertTrue(result.preview.write_required)
        self.assertEqual(result.preview.revision, 1)
        self.assertEqual(result.preview.desired_generation, 2)
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)
        self.assertTrue(connection.closed)
        sql = '\n'.join(value for value, _ in connection.cursor_value.statements)
        self.assertNotIn('DELETE', sql)
        self.assertNotIn('guild_configuration_draft', sql)

    def test_suspend_commit_updates_registry_and_audit_without_revision_write(self):
        request, state, generation, document = lifecycle_request(
            operation=workers.COMMIT
        )
        result, connection = self.run_worker(request, state, generation, document)
        self.assertEqual(result.transition.enrollment_state, workers.SUSPENDED)
        self.assertEqual(result.transition.generation, 2)
        self.assertEqual(result.transition.command_plan_digest, 'c' * 64)
        self.assertEqual(connection.commits, 1)
        sql = '\n'.join(value for value, _ in connection.cursor_value.statements)
        self.assertIn('UPDATE "guild_configuration_registry"', sql)
        self.assertIn('INSERT INTO "guild_configuration_audit"', sql)
        self.assertNotIn('INSERT INTO "guild_configuration_revision"', sql)

    def test_precommit_audit_failure_rolls_back_without_reload(self):
        request, state, generation, document = lifecycle_request(
            operation=workers.COMMIT
        )
        connection = Connection(request, state, generation, document)
        with mock.patch.object(workers.settings, 'owner_id', OWNER_ID), \
                mock.patch.object(workers, '_connect', return_value=connection), \
                mock.patch.object(storage, 'inspect_schema_inventory', return_value=exact_inventory()), \
                mock.patch.object(
                    workers,
                    '_transition',
                    side_effect=workers.psycopg2.DataError('audit insert failed'),
                ), \
                mock.patch.object(workers, '_post_commit_snapshot') as reload, \
                self.assertRaisesRegex(
                    workers.OperatorGuildLifecycleValidationError,
                    'transaction was invalid',
                ):
            workers.execute_lifecycle(request)
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)
        self.assertTrue(connection.closed)
        reload.assert_not_called()

    def test_suspend_without_a_different_active_control_guild_is_refused(self):
        request, state, generation, document = lifecycle_request()
        request = replace(
            request,
            current_runtime=(request.current_runtime[-1],),
            invoking_guild_id=request.target_guild_id + 1,
        )
        connection = Connection(request, state, generation, document)
        with mock.patch.object(workers.settings, 'owner_id', OWNER_ID), \
                mock.patch.object(workers, '_connect', return_value=connection), \
                mock.patch.object(storage, 'inspect_schema_inventory', return_value=exact_inventory()), \
                self.assertRaisesRegex(
                    workers.OperatorGuildLifecycleValidationError,
                    'active-guild inventory',
                ):
            workers.execute_lifecycle(request)
        self.assertEqual(connection.commits, 0)

    def test_postcommit_reload_failure_reports_committed_truth(self):
        request, state, generation, document = lifecycle_request(
            operation=workers.COMMIT
        )
        connection = Connection(request, state, generation, document)
        with mock.patch.object(workers.settings, 'owner_id', OWNER_ID), \
                mock.patch.object(workers, '_connect', return_value=connection), \
                mock.patch.object(storage, 'inspect_schema_inventory', return_value=exact_inventory()), \
                mock.patch.object(workers, '_post_commit_snapshot', side_effect=RuntimeError('down')), \
                self.assertRaises(workers.OperatorGuildLifecycleCommitted):
            workers.execute_lifecycle(request)
        self.assertEqual(connection.commits, 1)

    def test_resume_revalidates_live_references_and_advances_generation(self):
        request, state, generation, document = lifecycle_request(
            action=workers.RESUME,
            operation=workers.COMMIT,
        )
        result, _connection = self.run_worker(request, state, generation, document)
        self.assertEqual(result.transition.enrollment_state, workers.ACTIVE)
        self.assertEqual(result.transition.generation, 3)

    def test_resume_with_deleted_saved_role_fails_before_commit(self):
        request, state, generation, document = lifecycle_request(
            action=workers.RESUME,
            operation=workers.COMMIT,
        )
        snapshot = json.loads(request.discord_snapshot_json)
        target = next(
            guild for guild in snapshot['guilds']
            if guild['guild_id'] == TARGET_ID
        )
        target['roles'] = []
        request = replace(
            request,
            discord_snapshot_json=json.dumps(
                snapshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(',', ':'),
            ),
        )
        connection = Connection(request, state, generation, document)
        with mock.patch.object(workers.settings, 'owner_id', OWNER_ID), \
                mock.patch.object(workers, '_connect', return_value=connection), \
                mock.patch.object(storage, 'inspect_schema_inventory', return_value=exact_inventory()), \
                self.assertRaisesRegex(
                    workers.OperatorGuildLifecycleValidationError,
                    'references are invalid',
                ):
            workers.execute_lifecycle(request)
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_worker_keeps_event_loop_responsive(self):
        request, state, generation, document = lifecycle_request()
        preview = workers.GuildLifecyclePreview(
            action=workers.SUSPEND, guild_id=TARGET_ID,
            guild_name='Lifecycle Target', current_state=state,
            desired_state=workers.SUSPENDED, revision=1, generation=generation,
            desired_generation=generation + 1,
            document_digest=document_digest(document),
            command_capabilities=('operator',), write_required=True,
            document=document,
        )
        pending = Future()
        with mock.patch.object(workers._executor, 'submit', return_value=pending):
            task = asyncio.create_task(workers.run_lifecycle(request))
            ticked = False

            async def ticker():
                nonlocal ticked
                await asyncio.sleep(0)
                ticked = True

            await ticker()
            self.assertTrue(ticked)
            self.assertFalse(task.done())
            pending.set_result(workers.GuildLifecycleResult(
                operation=workers.PREVIEW,
                preview=preview,
            ))
            self.assertEqual(await task, pending.result())

    async def test_database_worker_cancellation_drains_committed_result(self):
        request, state, generation, document = lifecycle_request(
            operation=workers.COMMIT
        )
        transition = workers.GuildLifecycleTransition(
            action=workers.SUSPEND, guild_id=TARGET_ID,
            guild_name='Lifecycle Target', previous_state=workers.ACTIVE,
            enrollment_state=workers.SUSPENDED, revision=1, generation=2,
            event_number=2, document_digest=document_digest(document),
            command_plan_digest='c' * 64, actor=f'discord:{OWNER_ID}',
        )
        preview = workers.GuildLifecyclePreview(
            action=workers.SUSPEND, guild_id=TARGET_ID,
            guild_name='Lifecycle Target', current_state=state,
            desired_state=workers.SUSPENDED, revision=1, generation=1,
            desired_generation=2, document_digest=document_digest(document),
            command_capabilities=('operator',), write_required=True,
            document=document,
        )
        future = Future()
        with mock.patch.object(workers._executor, 'submit', return_value=future):
            task = asyncio.create_task(workers.run_lifecycle(request))
            await asyncio.sleep(0)
            task.cancel()
            future.set_result(workers.GuildLifecycleResult(
                operation=workers.COMMIT,
                preview=preview,
                transition=transition,
            ))
            with self.assertRaises(workers.OperatorGuildLifecycleCommitted):
                await task


class CommandPlanAndRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifecycle_plan_removes_target_roots_and_syncs_exact_guild(self):
        source = (FakeCommand('guild'), FakeCommand('operator'))
        current = (FakeCommand('guild'), FakeCommand('operator'))
        bot = SimpleNamespace(tree=FakeTree(source=source, current=current))
        policy = commands.build_capability_policy(
            {CONTROL_ID: (), TARGET_ID: ('operator',)},
            (CONTROL_ID, TARGET_ID),
        )
        plan = await commands.inspect_command_plan(
            bot=bot, policy=policy, guild_id=TARGET_ID,
            active_revision=1, active_generation=1,
            active_document_digest='a' * 64,
            current_capabilities=('operator',), desired_capabilities=(),
            mode=commands.LIFECYCLE,
        )
        self.assertEqual(plan.removals, ('guild', 'operator'))
        result = await commands.apply_command_plan(bot=bot, policy=policy, plan=plan)
        self.assertEqual(result.guild_id, TARGET_ID)
        self.assertEqual(bot.tree.sync_scopes, [TARGET_ID])
        self.assertEqual(bot.tree.fetch_scopes.count(None), 3)

    async def test_resume_plan_restores_saved_roots_to_exact_guild(self):
        source = (FakeCommand('guild'), FakeCommand('operator'))
        bot = SimpleNamespace(tree=FakeTree(source=source, current=()))
        policy = commands.build_capability_policy(
            {CONTROL_ID: (), TARGET_ID: ()},
            (CONTROL_ID, TARGET_ID),
        )
        plan = await commands.inspect_command_plan(
            bot=bot, policy=policy, guild_id=TARGET_ID,
            active_revision=1, active_generation=2,
            active_document_digest='a' * 64,
            current_capabilities=(), desired_capabilities=('operator',),
            mode=commands.LIFECYCLE,
        )
        self.assertEqual(plan.creates, ('guild', 'operator'))
        result = await commands.apply_command_plan(bot=bot, policy=policy, plan=plan)
        self.assertEqual(result.roots, ('guild', 'operator'))
        self.assertEqual(bot.tree.sync_scopes, [TARGET_ID])

    def test_runtime_publication_removes_and_restores_exact_target(self):
        control = runtime_fixtures.snapshot().guilds[CONTROL_ID]
        target = replace(
            control,
            guild_id=TARGET_ID,
            generation=1,
            document_digest='a' * 64,
        )
        current = runtime.GuildConfigurationRuntimeSnapshot(
            source='database', guilds={CONTROL_ID: control, TARGET_ID: target},
            legacy_config={CONTROL_ID: {}, TARGET_ID: {}},
            command_policy=mock.sentinel.before,
        )
        suspended = runtime.GuildConfigurationRuntimeSnapshot(
            source='database', guilds={CONTROL_ID: control},
            legacy_config={CONTROL_ID: {}}, command_policy=mock.sentinel.suspended,
        )
        expected = {
            CONTROL_ID: (control.revision, control.generation, control.document_digest),
            TARGET_ID: (target.revision, target.generation, target.document_digest),
        }
        with mock.patch.object(settings, 'guild_configuration_source', 'database'), \
                mock.patch.object(settings, '_database_guild_configuration', current), \
                mock.patch.object(settings, 'config', current.legacy_config), \
                mock.patch.object(settings, 'application_command_policy', current.command_policy):
            settings.reconcile_database_guild_lifecycle(
                suspended, action=workers.SUSPEND, expected_current=expected,
                target_guild_id=TARGET_ID,
                expected_previous=(1, 1, 'a' * 64),
                expected_transition=(1, 2, 'a' * 64),
            )
            self.assertIsNone(settings.database_guild_configuration(TARGET_ID))

        resumed_target = replace(target, generation=3)
        resumed = runtime.GuildConfigurationRuntimeSnapshot(
            source='database', guilds={CONTROL_ID: control, TARGET_ID: resumed_target},
            legacy_config={CONTROL_ID: {}, TARGET_ID: {}},
            command_policy=mock.sentinel.resumed,
        )
        with mock.patch.object(settings, 'guild_configuration_source', 'database'), \
                mock.patch.object(settings, '_database_guild_configuration', suspended), \
                mock.patch.object(settings, 'config', suspended.legacy_config), \
                mock.patch.object(settings, 'application_command_policy', suspended.command_policy):
            settings.reconcile_database_guild_lifecycle(
                resumed, action=workers.RESUME,
                expected_current={CONTROL_ID: expected[CONTROL_ID]},
                target_guild_id=TARGET_ID,
                expected_previous=(1, 2, 'a' * 64),
                expected_transition=(1, 3, 'a' * 64),
            )
            self.assertIs(settings.database_guild_configuration(TARGET_ID), resumed_target)


class AdapterAndViewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        operator = next(
            value for value in administration.administration.__cog_app_commands__
            if value.name == 'operator'
        )
        self.guild = operator.get_command('guild')

    def test_commands_are_owner_surface_with_exact_required_target(self):
        for name in ('suspend', 'resume'):
            command = self.guild.get_command(name)
            self.assertIsNotNone(command)
            self.assertEqual(
                [(value.name, value.required) for value in command.parameters],
                [('target_guild_id', True)],
            )

    def test_lifecycle_publication_values_are_frozen_and_model_free(self):
        request, state, generation, document = lifecycle_request()
        preview = workers.GuildLifecyclePreview(
            action=workers.SUSPEND, guild_id=TARGET_ID,
            guild_name='Lifecycle Target', current_state=state,
            desired_state=workers.SUSPENDED, revision=1, generation=generation,
            desired_generation=generation + 1,
            document_digest=document_digest(document),
            command_capabilities=('operator',), write_required=True,
            document=document,
        )
        for field in fields(preview):
            self.assertFalse(hasattr(getattr(preview, field.name), 'save'))
        self.assertNotIn('modules.models', inspect.getsource(views))

    async def test_workspace_is_requester_bound_and_confirmation_is_complete(self):
        request, state, generation, document = lifecycle_request()
        preview = workers.GuildLifecyclePreview(
            action=workers.SUSPEND, guild_id=TARGET_ID,
            guild_name='Lifecycle Target', current_state=state,
            desired_state=workers.SUSPENDED, revision=1, generation=generation,
            desired_generation=generation + 1,
            document_digest=document_digest(document),
            command_capabilities=('operator',), write_required=True,
            document=document,
        )
        plan = SimpleNamespace(
            plan_digest='d' * 64,
            creates=(), updates=(), removals=('operator',), unchanged=(),
        )

        async def runner(*_args):
            raise AssertionError('not called')

        workspace = views.GuildLifecycleWorkspace(
            requester_id=OWNER_ID, preview=preview,
            command_plan=plan, runner=runner,
        )
        modal = views.GuildLifecycleConfirmationModal(workspace)
        self.assertEqual(modal.expected, preview.confirmation('d' * 64))
        denied = SimpleNamespace(
            user=SimpleNamespace(id=OWNER_ID + 1),
            response=SimpleNamespace(send_message=mock.AsyncMock()),
        )
        self.assertFalse(await workspace.authorize(denied))

    async def test_confirmation_mismatch_stops_before_replan_or_database(self):
        cog = administration.administration.__new__(administration.administration)
        preview = mock.Mock()
        preview.confirmation.return_value = 'expected'
        plan = SimpleNamespace(plan_digest='d' * 64)
        cog._operator_guild_lifecycle_plan = mock.AsyncMock()
        cog._operator_guild_lifecycle_operation = mock.AsyncMock()
        with self.assertRaisesRegex(
            workers.OperatorGuildLifecycleConflict,
            'did not match',
        ):
            await cog._operator_guild_lifecycle_commit(
                mock.sentinel.interaction,
                preview,
                plan,
                'wrong',
            )
        cog._operator_guild_lifecycle_plan.assert_not_awaited()
        cog._operator_guild_lifecycle_operation.assert_not_awaited()

    async def test_postcommit_command_cancellation_drains_and_reports_truth(self):
        cog = administration.administration.__new__(administration.administration)
        cog.bot = mock.sentinel.bot
        request, _state, _generation, document = lifecycle_request()
        preview = workers.GuildLifecyclePreview(
            action=workers.SUSPEND, guild_id=TARGET_ID,
            guild_name='Lifecycle Target', current_state=workers.ACTIVE,
            desired_state=workers.SUSPENDED, revision=1, generation=1,
            desired_generation=2, document_digest=document_digest(document),
            command_capabilities=('operator',), write_required=True,
            document=document,
        )
        transition = workers.GuildLifecycleTransition(
            action=workers.SUSPEND, guild_id=TARGET_ID,
            guild_name='Lifecycle Target', previous_state=workers.ACTIVE,
            enrollment_state=workers.SUSPENDED, revision=1, generation=2,
            event_number=2, document_digest=document_digest(document),
            command_plan_digest='c' * 64, actor=f'discord:{OWNER_ID}',
        )
        started = asyncio.Event()
        release = asyncio.Event()
        completed = False

        async def slow_apply(**_kwargs):
            nonlocal completed
            started.set()
            await release.wait()
            completed = True
            return mock.sentinel.applied

        with mock.patch.object(service, 'planning_policy', return_value=mock.sentinel.policy), \
                mock.patch.object(commands, 'apply_command_plan', side_effect=slow_apply):
            task = asyncio.create_task(cog._operator_apply_lifecycle_commands(
                preview=preview,
                plan=mock.sentinel.plan,
                transition=transition,
            ))
            await started.wait()
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(
                workers.OperatorGuildLifecycleCommandUnverified,
            ):
                await task
        self.assertTrue(completed)

    def test_registry_list_prominently_shows_suspended_state_and_actor(self):
        document = target_document()
        record = configuration_workers.GuildConfigurationRecord(
            guild_id=TARGET_ID,
            storage_schema_version=storage.STORAGE_SCHEMA_VERSION,
            enrollment_state=workers.SUSPENDED,
            active_revision=1,
            generation=2,
            updated_at='2026-08-11T21:00:00+00:00',
            document_digest=document_digest(document),
            source_digest='a' * 64,
            document=document,
            last_lifecycle_event='suspension',
            last_lifecycle_actor=f'discord:{OWNER_ID}',
            last_lifecycle_at='2026-08-11T21:00:00+00:00',
        )
        embed = configuration_service.result_embed(
            configuration_workers.GuildConfigurationReadResult(
                operation=configuration_workers.LIST,
                guild_id=CONTROL_ID,
                records=(record,),
            )
        )
        self.assertIn('⏸️ suspended', embed.description)
        self.assertIn('Last lifecycle', embed.description)
        self.assertIn(f'discord:{OWNER_ID}', embed.description)


if __name__ == '__main__':
    unittest.main()
