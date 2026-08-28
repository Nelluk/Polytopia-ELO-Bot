"""Focused Tier-3 coverage for P10.6c command-capability activation."""

from __future__ import annotations

import asyncio
from dataclasses import fields, replace
from types import SimpleNamespace
import unittest
from unittest import mock

import bot as bot_module
from modules import administration
from modules import operator_guild_command_capabilities as service
from modules import operator_guild_command_capability_views as views
from modules import operator_guild_configuration_draft_workers as workers
from modules import operator_guild_configuration_drafts as draft_service
from modules import operator_guild_configuration_draft_views as draft_views
from modules.application_command_policy import build_capability_policy
from modules.guild_configuration_schema import document_digest
import settings
from tests import test_guild_configuration_runtime as runtime_fixtures
from tests import test_guild_configuration_storage as storage_fixtures
from tests import test_operator_guild_configuration_drafts as draft_fixtures


GUILD_ID = storage_fixtures.GUILD_ID
OWNER_ID = int(settings.owner_id)


class FakeCommand:
    def __init__(self, name: str, version: str = 'v1'):
        self.name = name
        self.version = version

    def to_dict(self, _tree=None):
        return {
            'name': self.name,
            'description': self.version,
            'options': [],
        }


class FakeTree:
    def __init__(self, *, source, current=(), global_commands=()):
        self.source = tuple(source)
        self.current = tuple(current)
        self.global_commands = tuple(global_commands)
        self.pending = []
        self.fetch_scopes = []
        self.sync_scopes = []

    def get_commands(self, *, guild=None):
        if guild is not None:
            return tuple(self.pending)
        return self.source

    async def fetch_commands(self, *, guild=None):
        self.fetch_scopes.append(None if guild is None else guild.id)
        return self.global_commands if guild is None else self.current

    def clear_commands(self, *, guild):
        self.pending = []
        self.clear_scope = guild.id

    def add_command(self, command, *, guild):
        self.pending.append(command)
        self.add_scope = guild.id

    async def sync(self, *, guild):
        self.sync_scopes.append(guild.id)
        self.current = tuple(self.pending)
        return self.current


def command_bot(*, current=(), global_commands=()):
    source = tuple(FakeCommand(name) for name in (
        'game', 'guild', 'leaderboard', 'operator', 'player', 'staffhelp',
    ))
    return SimpleNamespace(tree=FakeTree(
        source=source,
        current=current,
        global_commands=global_commands,
    ))


def policy(capabilities=('core_user',)):
    return build_capability_policy({GUILD_ID: capabilities}, (GUILD_ID,))


async def activation_plan(bot):
    return await service.inspect_command_plan(
        bot=bot,
        policy=policy(),
        guild_id=GUILD_ID,
        active_revision=1,
        active_generation=1,
        active_document_digest='a' * 64,
        current_capabilities=('core_user',),
        desired_capabilities=('core_user', 'operator'),
        mode=service.ACTIVATE,
        draft_version=3,
        draft_document_digest='b' * 64,
    )


class PurePlanAndApplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_activation_plan_is_digest_bound_model_free_and_deterministic(self):
        current = tuple(FakeCommand(name) for name in (
            'game', 'leaderboard', 'player',
        ))
        first_bot = command_bot(current=current)
        second_bot = command_bot(current=current)
        first = await activation_plan(first_bot)
        second = await activation_plan(second_bot)

        self.assertEqual(first, second)
        self.assertEqual(first.creates, ('operator',))
        self.assertEqual(first.removals, ())
        self.assertEqual(
            first.confirmation,
            f'ACTIVATE COMMANDS {"b" * 64} {first.plan_digest}',
        )
        for field in fields(first):
            self.assertNotIsInstance(getattr(first, field.name), FakeCommand)
        self.assertEqual(first_bot.tree.fetch_scopes, [None, GUILD_ID])

    async def test_nonempty_global_tree_refuses_before_any_guild_apply(self):
        bot = command_bot(global_commands=(FakeCommand('stale-global'),))
        with self.assertRaisesRegex(
            service.OperatorGuildCommandCapabilityError,
            'global command tree is nonempty',
        ):
            await activation_plan(bot)
        self.assertEqual(bot.tree.fetch_scopes, [None])
        self.assertEqual(bot.tree.sync_scopes, [])

    async def test_existing_active_root_source_drift_requires_external_tool(self):
        current = (
            FakeCommand('game', 'old'),
            FakeCommand('leaderboard'),
            FakeCommand('player'),
        )
        bot = command_bot(current=current)
        with self.assertRaisesRegex(
            service.OperatorGuildCommandCapabilityError,
            'older registered version.*No configuration or Discord commands were changed',
        ):
            await activation_plan(bot)
        self.assertEqual(bot.tree.sync_scopes, [])

    async def test_apply_rechecks_exact_evidence_and_syncs_only_target_guild(self):
        current = tuple(FakeCommand(name) for name in (
            'game', 'leaderboard', 'player',
        ))
        bot = command_bot(current=current)
        plan = await activation_plan(bot)
        result = await service.apply_command_plan(
            bot=bot,
            policy=policy(('core_user', 'operator')),
            plan=plan,
        )

        self.assertEqual(result.guild_id, GUILD_ID)
        self.assertEqual(
            result.roots,
            ('game', 'leaderboard', 'operator', 'player'),
        )
        self.assertEqual(bot.tree.sync_scopes, [GUILD_ID])
        self.assertEqual(bot.tree.clear_scope, GUILD_ID)
        self.assertEqual(bot.tree.add_scope, GUILD_ID)
        self.assertEqual(bot.tree.fetch_scopes.count(None), 3)

    async def test_remote_drift_blocks_apply_without_sync(self):
        current = tuple(FakeCommand(name) for name in (
            'game', 'leaderboard', 'player',
        ))
        bot = command_bot(current=current)
        plan = await activation_plan(bot)
        bot.tree.current = (*current, FakeCommand('operator'))
        with self.assertRaisesRegex(
            service.OperatorGuildCommandCapabilityDrift,
            'changed after preview',
        ):
            await service.apply_command_plan(
                bot=bot,
                policy=policy(('core_user', 'operator')),
                plan=plan,
            )
        self.assertEqual(bot.tree.sync_scopes, [])

    async def test_tampered_plan_digest_blocks_apply_before_remote_io(self):
        current = tuple(FakeCommand(name) for name in (
            'game', 'leaderboard', 'player',
        ))
        bot = command_bot(current=current)
        plan = await activation_plan(bot)
        bot.tree.fetch_scopes.clear()
        with self.assertRaisesRegex(
            service.OperatorGuildCommandCapabilityDrift,
            'plan digest is invalid',
        ):
            await service.apply_command_plan(
                bot=bot,
                policy=policy(('core_user', 'operator')),
                plan=replace(plan, removals=('player',)),
            )
        self.assertEqual(bot.tree.fetch_scopes, [])
        self.assertEqual(bot.tree.sync_scopes, [])

    async def test_reconcile_mode_does_not_accept_draft_evidence(self):
        bot = command_bot()
        with self.assertRaisesRegex(
            service.OperatorGuildCommandCapabilityError,
            'does not accept draft evidence',
        ):
            await service.inspect_command_plan(
                bot=bot,
                policy=policy(),
                guild_id=GUILD_ID,
                active_revision=1,
                active_generation=1,
                active_document_digest='a' * 64,
                current_capabilities=('core_user',),
                desired_capabilities=('core_user',),
                mode=service.RECONCILE,
                draft_version=1,
                draft_document_digest='b' * 64,
            )

    async def test_reconcile_cannot_expand_beyond_active_policy(self):
        bot = command_bot()
        with self.assertRaisesRegex(
            service.OperatorGuildCommandCapabilityError,
            'already-active capability policy',
        ):
            await service.inspect_command_plan(
                bot=bot,
                policy=policy(),
                guild_id=GUILD_ID,
                active_revision=1,
                active_generation=1,
                active_document_digest='a' * 64,
                current_capabilities=('core_user',),
                desired_capabilities=('core_user', 'operator'),
                mode=service.RECONCILE,
            )
        self.assertEqual(bot.tree.fetch_scopes, [])


class CoordinatedDatabaseWorkerTests(unittest.TestCase):
    def capability_draft(self):
        active = storage_fixtures.bundle().imports[0].document
        edited = draft_service.replace_field(
            active,
            draft_service.FIELD_BY_KEY['command_capabilities'],
            active.command_capabilities[:-1],
        )
        return draft_fixtures.stored(edited), edited

    def request(self, draft, *, confirmation=None, plan_digest='c' * 64):
        expected = (
            f'ACTIVATE COMMANDS {draft.document_digest} {plan_digest}'
            if confirmation is None else confirmation
        )
        return draft_fixtures.request(
            workers.ACTIVATE_COMMANDS,
            expected_draft_version=draft.draft_version,
            expected_draft_digest=draft.document_digest,
            discord_snapshot=storage_fixtures.snapshot(),
            command_plan_digest=plan_digest,
            confirmation_text=expected,
        )

    def test_confirmation_and_plan_digest_fail_before_connection(self):
        draft, _edited = self.capability_draft()
        with mock.patch.object(workers, '_connect') as connect, self.assertRaisesRegex(
            workers.OperatorGuildConfigurationDraftValidationError,
            'exact confirmation',
        ):
            workers.execute_draft_operation(
                self.request(draft, confirmation='wrong')
            )
        connect.assert_not_called()

    def test_capability_activation_commits_audit_bound_plan_and_reloads(self):
        draft, edited = self.capability_draft()
        value = self.request(draft)
        connection = draft_fixtures.Connection()
        committed = draft_fixtures.activation(edited)
        base = runtime_fixtures.snapshot()
        advanced = replace(
            base.guilds[GUILD_ID],
            revision=2,
            generation=2,
            document=edited,
            document_digest=document_digest(edited),
        )
        reloaded = replace(
            base,
            guilds={GUILD_ID: advanced},
            command_policy=policy(edited.command_capabilities),
        )
        with mock.patch.object(
            workers.drafts, 'activate_draft', return_value=committed,
        ) as write, mock.patch.object(
            workers, '_post_commit_runtime_snapshot', return_value=reloaded,
        ):
            result = draft_fixtures.run_with(
                connection,
                value,
                selected=draft,
            )
        self.assertEqual(connection.commits, 1)
        self.assertIs(result.activation, committed)
        self.assertEqual(write.call_args.kwargs['command_plan_digest'], 'c' * 64)

    def test_precommit_audit_failure_rolls_back_without_publication(self):
        draft, _edited = self.capability_draft()
        connection = draft_fixtures.Connection()
        with mock.patch.object(
            workers.drafts,
            'activate_draft',
            side_effect=workers.drafts.GuildConfigurationDraftStorageError('audit failed'),
        ), mock.patch.object(workers, '_post_commit_runtime_snapshot') as reload:
            with self.assertRaisesRegex(
                workers.OperatorGuildConfigurationDraftConflict,
                'audit failed',
            ):
                draft_fixtures.run_with(
                    connection,
                    self.request(draft),
                    selected=draft,
                )
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)
        reload.assert_not_called()

    def test_postcommit_reload_failure_reports_committed_truth(self):
        draft, edited = self.capability_draft()
        connection = draft_fixtures.Connection()
        committed = draft_fixtures.activation(edited)
        with mock.patch.object(
            workers.drafts, 'activate_draft', return_value=committed,
        ), mock.patch.object(
            workers, '_post_commit_runtime_snapshot', side_effect=RuntimeError('down'),
        ):
            with self.assertRaises(
                workers.OperatorGuildConfigurationActivationCommitted,
            ) as raised:
                draft_fixtures.run_with(
                    connection,
                    self.request(draft),
                    selected=draft,
                )
        self.assertIs(raised.exception.activation, committed)
        self.assertEqual(connection.commits, 1)


class RuntimeAndAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_registered_root_is_denied_by_runtime_policy(self):
        response = SimpleNamespace(
            is_done=lambda: False,
            send_message=mock.AsyncMock(),
        )
        interaction = SimpleNamespace(
            guild_id=GUILD_ID,
            data={'name': 'operator'},
            response=response,
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        with mock.patch.object(settings, 'guild_configuration_ready', return_value=True), \
                mock.patch.object(settings, 'config', {GUILD_ID: {}}), \
                mock.patch.object(settings, 'application_command_policy', policy()):
            allowed = await bot_module.PolyBotCommandTree.interaction_check(
                SimpleNamespace(), interaction
            )
        self.assertFalse(allowed)
        response.send_message.assert_awaited_once()

    async def test_stale_root_autocomplete_is_denied_with_empty_choices(self):
        response = SimpleNamespace(
            is_done=lambda: False,
            autocomplete=mock.AsyncMock(),
            send_message=mock.AsyncMock(),
        )
        interaction = SimpleNamespace(
            type=bot_module.discord.InteractionType.autocomplete,
            guild_id=GUILD_ID,
            data={'name': 'operator'},
            response=response,
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        with mock.patch.object(settings, 'guild_configuration_ready', return_value=True), \
                mock.patch.object(settings, 'config', {GUILD_ID: {}}), \
                mock.patch.object(settings, 'application_command_policy', policy()):
            allowed = await bot_module.PolyBotCommandTree.interaction_check(
                SimpleNamespace(), interaction
            )
        self.assertFalse(allowed)
        response.autocomplete.assert_awaited_once_with([])
        response.send_message.assert_not_awaited()

    async def test_postcommit_cancellation_drains_discord_apply_and_reports_truth(self):
        cog = administration.administration.__new__(administration.administration)
        cog.bot = mock.sentinel.bot
        started = asyncio.Event()
        release = asyncio.Event()
        completed = False

        async def slow_apply(**_kwargs):
            nonlocal completed
            started.set()
            await release.wait()
            completed = True
            return mock.sentinel.applied

        with mock.patch.object(
            service, 'apply_command_plan', side_effect=slow_apply,
        ):
            task = asyncio.create_task(cog._operator_apply_commands_after_commit(
                plan=mock.sentinel.plan,
                revision=2,
                generation=2,
            ))
            await started.wait()
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(
                service.OperatorGuildCommandCapabilityCommitted,
            ) as raised:
                await task
        self.assertTrue(completed)
        self.assertEqual(raised.exception.revision, 2)

    async def test_remote_planning_remains_event_loop_responsive(self):
        bot = command_bot()
        original = bot.tree.fetch_commands

        async def slow_fetch(*, guild=None):
            await asyncio.sleep(0.02)
            return await original(guild=guild)

        bot.tree.fetch_commands = slow_fetch
        ticked = False

        async def ticker():
            nonlocal ticked
            await asyncio.sleep(0.005)
            ticked = True

        await asyncio.gather(
            service.inspect_command_plan(
                bot=bot,
                policy=policy(),
                guild_id=GUILD_ID,
                active_revision=1,
                active_generation=1,
                active_document_digest='a' * 64,
                current_capabilities=('core_user',),
                desired_capabilities=('core_user',),
                mode=service.RECONCILE,
            ),
            ticker(),
        )
        self.assertTrue(ticked)

    async def test_workspace_binds_requester_and_full_confirmation(self):
        bot = command_bot(current=tuple(FakeCommand(name) for name in (
            'game', 'leaderboard', 'player',
        )))
        plan = await activation_plan(bot)

        async def runner(*_args):
            raise AssertionError('not called')

        workspace = views.GuildCommandCapabilityWorkspace(
            requester_id=OWNER_ID,
            guild_name='Target',
            plan=plan,
            runner=runner,
        )
        modal = views.GuildCommandCapabilityConfirmationModal(workspace)
        self.assertEqual(modal.expected, plan.confirmation)
        self.assertEqual(modal.confirmation.min_length, len(plan.confirmation))
        denied = SimpleNamespace(
            user=SimpleNamespace(id=OWNER_ID + 1),
            response=SimpleNamespace(send_message=mock.AsyncMock()),
        )
        self.assertFalse(await workspace.interaction_check(denied))

    async def test_successful_commit_uses_private_fallback_if_terminal_edit_fails(self):
        bot = command_bot(current=tuple(FakeCommand(name) for name in (
            'game', 'leaderboard', 'player',
        )))
        plan = await activation_plan(bot)
        completion = service.GuildCommandCapabilityCompletion(
            plan=plan,
            apply=service.GuildCommandCapabilityApplyResult(
                guild_id=GUILD_ID,
                roots=('game', 'leaderboard', 'operator', 'player'),
                synced_count=4,
            ),
            committed_revision=2,
            committed_generation=2,
        )

        async def runner(*_args):
            return completion

        workspace = views.GuildCommandCapabilityWorkspace(
            requester_id=OWNER_ID,
            guild_name='Target',
            plan=plan,
            runner=runner,
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=OWNER_ID),
            response=SimpleNamespace(defer=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(
                side_effect=[None, RuntimeError('panel deleted')]
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        await workspace.commit(interaction, plan.confirmation)
        self.assertTrue(workspace.terminal)
        interaction.followup.send.assert_awaited_once()
        self.assertIn(
            'Activated revision 2',
            interaction.followup.send.await_args.args[0],
        )

    async def test_cross_guild_plan_uses_exact_active_target(self):
        target = GUILD_ID + 99
        active = storage_fixtures.bundle().imports[0].document
        target_document = replace(active, guild_id=target)
        target_record = replace(
            runtime_fixtures.snapshot().guilds[GUILD_ID],
            guild_id=target,
            document=target_document,
            document_digest=document_digest(target_document),
        )
        shown = workers.GuildConfigurationDraftResult(
            operation=workers.SHOW,
            guild_id=target,
            active_revision=target_record.revision,
            active_generation=target_record.generation,
            active_document_digest=target_record.document_digest,
            draft=None,
        )
        cog = administration.administration.__new__(administration.administration)
        cog.bot = SimpleNamespace(get_guild=lambda guild_id: SimpleNamespace(
            id=guild_id,
            name='Enrolled target',
        ))
        cog._operator_guild_draft_operation = mock.AsyncMock(return_value=shown)
        planned = mock.sentinel.planned
        with mock.patch.object(
            settings,
            'database_guild_configuration',
            side_effect=lambda guild_id: target_record if guild_id == target else None,
        ), mock.patch.object(
            service,
            'inspect_command_plan',
            new=mock.AsyncMock(return_value=planned),
        ) as inspect_plan:
            result = await cog._operator_guild_command_plan(
                mock.sentinel.interaction,
                target,
            )
        self.assertIs(result, planned)
        self.assertEqual(
            cog._operator_guild_draft_operation.await_args.kwargs['target_guild_id'],
            target,
        )
        self.assertEqual(inspect_plan.await_args.kwargs['guild_id'], target)

    async def test_cross_guild_editor_exposes_only_capabilities(self):
        active = storage_fixtures.bundle().imports[0].document
        result = workers.GuildConfigurationDraftResult(
            operation=workers.SHOW,
            guild_id=GUILD_ID,
            active_revision=1,
            active_generation=1,
            active_document_digest=document_digest(active),
            draft=draft_fixtures.stored(),
        )

        async def runner(*_args, **_kwargs):
            return result

        workspace = draft_views.GuildConfigurationDraftWorkspace(
            requester_id=OWNER_ID,
            active_document=active,
            result=result,
            runner=runner,
            role_names={},
            channel_names={},
            target_guild_id=GUILD_ID,
            capabilities_only=True,
        )
        self.assertEqual(workspace.sections, (draft_service.CAPABILITIES,))
        self.assertEqual(workspace.field.key, 'command_capabilities')

    async def test_operator_commands_registered_without_duplicate_editor(self):
        operator = next(
            command for command in administration.administration.__cog_app_commands__
            if command.name == 'operator'
        )
        guild = operator.get_command('guild')
        commands = guild.get_command('commands')
        capabilities = guild.get_command('capabilities')
        edit = guild.get_command('edit')
        self.assertIsNotNone(commands)
        self.assertIsNotNone(capabilities)
        self.assertEqual([value.name for value in commands.parameters], ['target_guild_id'])
        self.assertEqual(
            [value.name for value in capabilities.parameters],
            ['target_guild_id'],
        )
        self.assertIsNone(edit)


class RuntimePublicationTests(unittest.TestCase):
    def test_exact_target_policy_change_can_publish_but_mismatch_fails(self):
        current = runtime_fixtures.snapshot()
        record = current.guilds[GUILD_ID]
        edited = draft_service.replace_field(
            record.document,
            draft_service.FIELD_BY_KEY['command_capabilities'],
            record.document.command_capabilities[:-1],
        )
        advanced = replace(
            record,
            revision=record.revision + 1,
            generation=record.generation + 1,
            document=edited,
            document_digest=document_digest(edited),
        )
        candidate = replace(
            current,
            guilds={GUILD_ID: advanced},
            command_policy=policy(edited.command_capabilities),
        )
        expected_current = {
            GUILD_ID: (
                record.revision,
                record.generation,
                record.document_digest,
            )
        }
        with mock.patch.object(settings, 'guild_configuration_source', 'database'), \
                mock.patch.object(settings, '_database_guild_configuration', current), \
                mock.patch.object(settings, 'config', current.legacy_config), \
                mock.patch.object(settings, 'application_command_policy', current.command_policy):
            settings.reconcile_database_guild_configuration(
                candidate,
                expected_current=expected_current,
                activated_guild_id=GUILD_ID,
                expected_activation=(
                    advanced.revision,
                    advanced.generation,
                    advanced.document_digest,
                ),
                expected_command_capabilities=edited.command_capabilities,
            )
            self.assertIs(settings._database_guild_configuration, candidate)

        with mock.patch.object(settings, 'guild_configuration_source', 'database'), \
                mock.patch.object(settings, '_database_guild_configuration', current):
            with self.assertRaisesRegex(RuntimeError, 'differ'):
                settings.reconcile_database_guild_configuration(
                    candidate,
                    expected_current=expected_current,
                    activated_guild_id=GUILD_ID,
                    expected_activation=(
                        advanced.revision,
                        advanced.generation,
                        advanced.document_digest,
                    ),
                    expected_command_capabilities=('operator',),
                )
