"""Focused P8.13b reaction-lifecycle and transaction coverage."""

import asyncio
from contextlib import AbstractContextManager
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

import discord

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.league_free_agents_workers')
reactions = import_offline_runtime('modules.league_free_agent_reactions')
league = import_offline_runtime('modules.league')


class FakeDatabase:
    def __init__(self):
        self.connections = 0
        self.atomics = 0
        self.errors = 0

    def connection_context(self):
        database = self

        class Context(AbstractContextManager):
            def __enter__(self):
                database.connections += 1

            def __exit__(self, exc_type, exc_value, traceback):
                return False

        return Context()

    def atomic(self):
        database = self

        class Context(AbstractContextManager):
            def __enter__(self):
                database.atomics += 1

            def __exit__(self, exc_type, exc_value, traceback):
                if exc_type is not None:
                    database.errors += 1
                return False

        return Context()


class FakeQuery:
    def __init__(self, record):
        self.record = record

    def where(self, *args):
        return self

    def for_update(self):
        return self

    def get(self):
        return self.record


def state(*, open_state=True):
    return workers.DraftState(
        guild_id=300,
        announcement_message_id=700,
        announcement_channel_id=400,
        draft_open=open_state,
        added_message='Sunday deadline',
    )


def transition(operation='toggle'):
    return workers.DraftTransitionRequest(
        guild_id=300,
        requester_id=10,
        requester_name='Moderator',
        expected_message_id=700,
        expected_channel_id=400,
        operation=operation,
    )


class WorkerTransitionTests(unittest.TestCase):
    def setUp(self):
        self.database = FakeDatabase()
        self.record = SimpleNamespace(
            polychamps_draft={
                'announcement_message': 700,
                'announcement_channel': 400,
                'draft_open': True,
                'draft_message': 'Sunday deadline',
            },
            save=mock.Mock(),
        )

    def patches(self):
        return (
            mock.patch.object(workers.models, 'db', self.database),
            mock.patch.object(
                workers.models.Configuration,
                'select',
                return_value=FakeQuery(self.record),
            ),
            mock.patch.object(
                workers.models.GameLog,
                'write',
                return_value=SimpleNamespace(id=1),
            ),
        )

    def test_toggle_locks_and_atomically_saves_state_with_audit(self):
        database_patch, select_patch, audit_patch = self.patches()
        with database_patch, select_patch as selected, audit_patch as audit:
            result = workers.transition_draft_state(transition())
        self.assertTrue(result.previous_open)
        self.assertFalse(result.draft_open)
        self.assertEqual(result.added_message, 'Sunday deadline')
        self.assertFalse(self.record.polychamps_draft['draft_open'])
        self.record.save.assert_called_once()
        self.assertEqual(self.database.connections, 1)
        self.assertEqual(self.database.atomics, 1)
        self.assertEqual(self.database.errors, 0)
        selected.return_value.for_update()
        self.assertIn('closed', audit.call_args.kwargs['message'])
        self.assertIn('Moderator', audit.call_args.kwargs['message'])

    def test_conclude_resets_only_draft_field_and_audits(self):
        database_patch, select_patch, audit_patch = self.patches()
        with database_patch, select_patch, audit_patch as audit:
            result = workers.transition_draft_state(transition('conclude'))
        self.assertEqual(
            self.record.polychamps_draft,
            workers.models.Configuration.draft_config_defaults(),
        )
        self.assertFalse(result.draft_open)
        self.assertIn('concluded', audit.call_args.kwargs['message'])

    def test_audit_failure_leaves_the_atomic_scope_by_exception(self):
        with mock.patch.object(workers.models, 'db', self.database), mock.patch.object(
            workers.models.Configuration,
            'select',
            return_value=FakeQuery(self.record),
        ), mock.patch.object(
            workers.models.GameLog,
            'write',
            side_effect=RuntimeError('audit unavailable'),
        ):
            with self.assertRaises(RuntimeError):
                workers.transition_draft_state(transition())
        self.assertEqual(self.database.errors, 1)

    def test_signup_audit_rechecks_pointer_and_open_state(self):
        request = workers.SignupAuditRequest(
            guild_id=300,
            requester_id=20,
            requester_name='Player',
            expected_message_id=700,
            expected_channel_id=400,
            action='join',
            role_name='Free Agent',
        )
        database_patch, select_patch, audit_patch = self.patches()
        with database_patch, select_patch, audit_patch as audit:
            result = workers.write_signup_audit(request)
        self.assertEqual(result.action, 'join')
        self.assertIn('received', audit.call_args.kwargs['message'])

        self.record.polychamps_draft['draft_open'] = False
        with mock.patch.object(workers.models, 'db', self.database), mock.patch.object(
            workers.models.Configuration,
            'select',
            return_value=FakeQuery(self.record),
        ):
            with self.assertRaises(workers.FreeAgentPostConflictError):
                workers.write_signup_audit(request)

    def test_cancelled_signup_audit_drains_and_returns_known_result(self):
        async def run_case():
            started = threading.Event()
            release = threading.Event()
            expected = workers.SignupAuditResult(300, 20, 'join')

            def slow(_request):
                started.set()
                release.wait(timeout=2)
                return expected

            request = workers.SignupAuditRequest(
                300, 20, 'Player', 700, 400, 'join', 'Free Agent'
            )
            with mock.patch.object(workers, 'write_signup_audit', side_effect=slow):
                task = asyncio.create_task(workers.run_write_signup_audit(request))
                deadline = asyncio.get_running_loop().time() + 1
                while not started.is_set():
                    if asyncio.get_running_loop().time() >= deadline:
                        self.fail('Free Agent audit worker did not start')
                    await asyncio.sleep(0.001)
                task.cancel()
                release.set()
                await asyncio.sleep(0.05)
                return await task

        result = asyncio.run(run_case())
        self.assertEqual(result.action, 'join')


class FakeRole:
    def __init__(self, role_id, name, members=()):
        self.id = role_id
        self.name = name
        self.mention = f'<@&{role_id}>'
        self.members = list(members)


class FakeMember:
    def __init__(self, guild, *, member_id=20, roles=()):
        self.guild = guild
        self.id = member_id
        self.name = 'Player'
        self.display_name = 'Player Display'
        self.mention = f'<@{member_id}>'
        self.roles = list(roles)
        self.add_roles = mock.AsyncMock()
        self.remove_roles = mock.AsyncMock()
        self.send = mock.AsyncMock()


class FakeMessage:
    def __init__(self):
        self.id = 700
        self.content = 'Signup content'
        self.jump_url = 'https://discord.com/channels/300/400/700'
        self.remove_reaction = mock.AsyncMock()
        self.clear_reactions = mock.AsyncMock()
        self.edit = mock.AsyncMock()


class FakeChannel:
    def __init__(self, guild):
        self.id = 400
        self.guild = guild
        self.send = mock.AsyncMock()


class ReactionServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.grad = FakeRole(1, 'Nova Grad')
        self.free_agent = FakeRole(2, 'Free Agent')
        self.novas = FakeRole(3, 'The Novas')
        self.guild = SimpleNamespace(
            id=300,
            roles=[self.grad, self.free_agent, self.novas],
        )
        self.member = FakeMember(self.guild, roles=[self.grad])
        self.channel = FakeChannel(self.guild)
        self.message = FakeMessage()
        self.cog = SimpleNamespace(announcement_message=700)

    async def test_eligible_signup_changes_role_then_audits_and_relays(self):
        calls = []

        async def add_role(*args, **kwargs):
            calls.append('role')

        async def audit(_request):
            calls.append('audit')

        async def relay(*args, **kwargs):
            calls.append('relay')

        self.member.add_roles.side_effect = add_role
        with mock.patch.object(
            workers, 'run_load_draft_state', new=mock.AsyncMock(return_value=state())
        ), mock.patch.object(
            workers, 'run_write_signup_audit', new=mock.AsyncMock(side_effect=audit)
        ), mock.patch.object(
            reactions.utilities, 'send_to_log_channel', new=mock.AsyncMock(side_effect=relay)
        ):
            await reactions.handle_signup_reaction(
                member=self.member,
                channel=self.channel,
                message=self.message,
                reaction_added=True,
                signup_emoji='🔆',
                grad_role_name='Nova Grad',
                free_agent_role_name='Free Agent',
            )
        self.assertEqual(calls, ['role', 'audit', 'relay'])
        self.message.remove_reaction.assert_not_awaited()
        self.member.send.assert_awaited_once()

    async def test_closed_signup_removes_reaction_without_role_or_audit(self):
        audit = mock.AsyncMock()
        with mock.patch.object(
            workers,
            'run_load_draft_state',
            new=mock.AsyncMock(return_value=state(open_state=False)),
        ), mock.patch.object(workers, 'run_write_signup_audit', audit):
            await reactions.handle_signup_reaction(
                member=self.member,
                channel=self.channel,
                message=self.message,
                reaction_added=True,
                signup_emoji='🔆',
                grad_role_name='Nova Grad',
                free_agent_role_name='Free Agent',
            )
        self.message.remove_reaction.assert_awaited_once()
        self.member.add_roles.assert_not_awaited()
        audit.assert_not_awaited()
        self.assertIn('closed', self.member.send.await_args.args[0])

    async def test_reaction_removal_removes_role_and_audits(self):
        self.member.roles.append(self.free_agent)
        audit = mock.AsyncMock()
        with mock.patch.object(
            workers, 'run_load_draft_state', new=mock.AsyncMock(return_value=state())
        ), mock.patch.object(workers, 'run_write_signup_audit', audit), mock.patch.object(
            reactions.utilities, 'send_to_log_channel', new=mock.AsyncMock()
        ):
            await reactions.handle_signup_reaction(
                member=self.member,
                channel=self.channel,
                message=self.message,
                reaction_added=False,
                signup_emoji='🔆',
                grad_role_name='Nova Grad',
                free_agent_role_name='Free Agent',
            )
        self.member.remove_roles.assert_awaited_once()
        self.assertEqual(audit.await_args.args[0].action, 'leave')

    async def test_toggle_commits_before_public_edit_and_warns_on_edit_failure(self):
        calls = []

        async def committed(_request):
            calls.append('commit')
            return workers.DraftTransitionResult(
                300, 20, 700, 400, True, False, 'Sunday', 'toggle'
            )

        async def failed_edit(*args, **kwargs):
            calls.append('edit')
            raise discord.HTTPException(mock.Mock(status=500), 'failed')

        self.message.edit.side_effect = failed_edit
        with mock.patch.object(
            reactions.settings, 'is_mod', return_value=True
        ), mock.patch.object(
            workers, 'run_transition_draft_state', new=mock.AsyncMock(side_effect=committed)
        ), mock.patch.object(
            reactions.utilities, 'send_to_log_channel', new=mock.AsyncMock()
        ):
            await reactions.toggle_signup_state(
                cog=self.cog,
                member=self.member,
                channel=self.channel,
                message=self.message,
                close_emoji='⏯',
                closed_message='Closed',
                open_format='{0} {1} {2} {3}',
                grad_role_name='Nova Grad',
                novas_role_name='The Novas',
                free_agent_role_name='Free Agent',
            )
        self.assertEqual(calls, ['commit', 'edit'])
        self.channel.send.assert_awaited_once()
        self.assertIn('committed', self.channel.send.await_args.args[0])

    async def test_non_mod_toggle_only_removes_control_reaction(self):
        transition_worker = mock.AsyncMock()
        with mock.patch.object(reactions.settings, 'is_mod', return_value=False), mock.patch.object(
            workers, 'run_transition_draft_state', transition_worker
        ):
            await reactions.toggle_signup_state(
                cog=self.cog,
                member=self.member,
                channel=self.channel,
                message=self.message,
                close_emoji='⏯',
                closed_message='Closed',
                open_format='{0} {1} {2} {3}',
                grad_role_name='Nova Grad',
                novas_role_name='The Novas',
                free_agent_role_name='Free Agent',
            )
        self.message.remove_reaction.assert_awaited_once()
        transition_worker.assert_not_awaited()

    async def test_conclude_commits_before_clearing_and_editing(self):
        calls = []

        async def committed(_request):
            calls.append('commit')
            return workers.DraftTransitionResult(
                300, 20, 700, 400, True, False, 'Sunday', 'conclude'
            )

        async def clear():
            calls.append('clear')

        async def edit(*args, **kwargs):
            calls.append('edit')

        self.message.clear_reactions.side_effect = clear
        self.message.edit.side_effect = edit
        with mock.patch.object(
            workers, 'run_transition_draft_state', new=mock.AsyncMock(side_effect=committed)
        ), mock.patch.object(
            reactions.utilities, 'send_to_log_channel', new=mock.AsyncMock()
        ):
            await reactions.conclude_signup(
                cog=self.cog,
                member=self.member,
                channel=self.channel,
                message=self.message,
                free_agent_count=4,
            )
        self.assertEqual(calls, ['commit', 'clear', 'edit'])
        self.assertIsNone(self.cog.announcement_message)


class AdapterBoundaryTests(unittest.TestCase):
    def test_legacy_methods_delegate_without_direct_model_access(self):
        import inspect

        for method_name in (
            'signup_emoji_clicked',
            'close_draft_emoji_added',
            'conclude_draft_emoji_added',
        ):
            source = inspect.getsource(getattr(league.league, method_name))
            self.assertIn('league_free_agent_reactions', source)
            self.assertNotIn('models.', source)

    def test_on_ready_loads_pointer_through_worker(self):
        import inspect

        source = inspect.getsource(league.league.on_ready)
        self.assertIn('run_load_draft_state', source)
        self.assertNotIn('get_draft_config', source)


if __name__ == '__main__':
    unittest.main()
