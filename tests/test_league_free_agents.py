"""Focused coverage for P8.13 Free Agent announcement posting."""

import asyncio
from contextlib import AbstractContextManager
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

import discord

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.league_free_agents_workers')
service = import_offline_runtime('modules.league_free_agents')
views = import_offline_runtime('modules.league_free_agents_views')
league = import_offline_runtime('modules.league')


class FakeDatabase:
    def __init__(self):
        self.connections = 0
        self.atomics = 0
        self.atomic_errors = 0

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
                    database.atomic_errors += 1
                return False

        return Context()


class FakeConfigQuery:
    def __init__(self, record):
        self.record = record

    def where(self, *args):
        return self

    def for_update(self):
        return self

    def get(self):
        return self.record


def role(role_id, name):
    return SimpleNamespace(id=role_id, name=name, mention=f'<@&{role_id}>')


def actor(member_id=10):
    return SimpleNamespace(
        id=member_id,
        name='Actor',
        display_name='Actor Display',
        mention=f'<@{member_id}>',
        roles=(),
    )


def role_snapshot():
    return service.FreeAgentRoleSnapshot(
        grad_role_id=1,
        grad_mention='<@&1>',
        novas_role_id=2,
        novas_mention='<@&2>',
        free_agent_role_id=3,
        free_agent_mention='<@&3>',
    )


def state(**overrides):
    values = dict(
        guild_id=300,
        announcement_message_id=None,
        announcement_channel_id=None,
        draft_open=False,
        added_message='',
    )
    values.update(overrides)
    return workers.DraftState(**values)


class FakeMessage:
    def __init__(self, message_id=700, channel=None, content='announcement'):
        self.id = message_id
        self.channel = channel
        self.content = content
        self.add_reaction = mock.AsyncMock()
        self.delete = mock.AsyncMock()
        self.edit = mock.AsyncMock()

    @property
    def jump_url(self):
        return f'https://discord.com/channels/300/400/{self.id}'


class FakeChannel:
    def __init__(self, guild, channel_id=400):
        self.guild = guild
        self.id = channel_id
        self.mention = f'<#{channel_id}>'
        self.message = FakeMessage(channel=self)
        self.send = mock.AsyncMock(return_value=self.message)
        self.fetch_message = mock.AsyncMock()


class FakeGuild:
    def __init__(self, guild_id=300):
        self.id = guild_id
        self.roles = [
            role(1, service.GRAD_ROLE_NAME),
            role(2, service.NOVAS_ROLE_NAME),
            role(3, service.FREE_AGENT_ROLE_NAME),
        ]
        self.channels = {}

    def get_channel(self, channel_id):
        return self.channels.get(int(channel_id))


class RegistrationAndValueTests(unittest.TestCase):
    @staticmethod
    def root():
        return next(
            command for command in league.league.__cog_app_commands__
            if command.name == 'league'
        )

    def test_nested_native_shape_and_prefix_are_retained(self):
        group = self.root().get_command('free-agents')
        self.assertIsInstance(group, discord.app_commands.Group)
        self.assertEqual({command.name for command in group.commands}, {'post'})
        command = group.get_command('post')
        self.assertEqual(
            [
                (parameter.name, parameter.required, parameter.type)
                for parameter in command.parameters
            ],
            [('channel', False, discord.AppCommandOptionType.channel)],
        )
        prefix = {command.name: command for command in league.league.__cog_commands__}
        self.assertIn('newfreeagent', prefix)

    def test_frozen_primitive_worker_values_and_coordinator_conflict(self):
        loaded = state()
        with self.assertRaises(FrozenInstanceError):
            loaded.guild_id = 1
        coordinator = workers.FreeAgentPostCoordinator()
        coordinator.claim()
        with self.assertRaises(workers.FreeAgentPostBusyError):
            coordinator.claim()
        coordinator.release()
        self.assertFalse(coordinator.active)

    def test_roles_and_content_are_exact_bounded_and_attributed(self):
        guild = FakeGuild()
        captured = service.capture_roles(guild)
        self.assertEqual(captured, role_snapshot())
        content = service.announcement_content(
            roles=captured,
            added_message='Draft Sunday.',
            actor_mention='<@10>',
        )
        self.assertIn('<@&1>', content)
        self.assertIn('Draft Sunday.', content)
        self.assertIn('Signup opened by <@10>', content)
        with self.assertRaises(workers.FreeAgentPostError):
            service.normalize_added_message('x' * (workers.MAX_ADDED_MESSAGE_LENGTH + 1))

    def test_missing_role_fails_closed(self):
        guild = FakeGuild()
        guild.roles.pop()
        with self.assertRaises(service.FreeAgentPostDiscordError):
            service.capture_roles(guild)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        workers.free_agent_post_coordinator.release()

    def test_read_does_not_create_configuration_and_owns_connection(self):
        database = FakeDatabase()
        with mock.patch.object(workers.models, 'db', database), mock.patch.object(
            workers.models.Configuration, 'get_or_none', return_value=None
        ) as lookup:
            loaded = workers.load_draft_state(300)
        self.assertEqual(loaded, state())
        self.assertEqual(database.connections, 1)
        lookup.assert_called_once()

    def test_persist_locks_compares_and_atomically_writes_state_and_audit(self):
        database = FakeDatabase()
        record = SimpleNamespace(
            polychamps_draft={
                'announcement_message': None,
                'announcement_channel': None,
                'draft_open': False,
            },
            save=mock.Mock(),
        )
        request = workers.DraftPersistRequest(
            guild_id=300,
            requester_id=10,
            requester_name='Actor',
            expected_message_id=None,
            expected_channel_id=None,
            announcement_message_id=700,
            announcement_channel_id=400,
            added_message='Sunday',
            opened_at='2026-08-08T12:00:00+00:00',
        )
        with mock.patch.object(workers.models, 'db', database), mock.patch.object(
            workers.models.Configuration,
            'get_or_create',
            return_value=(record, False),
        ), mock.patch.object(
            workers.models.Configuration,
            'select',
            return_value=FakeConfigQuery(record),
        ), mock.patch.object(
            workers.models.GameLog, 'write', return_value=SimpleNamespace(id=1)
        ) as audit:
            result = workers.persist_draft_state(request)
        self.assertEqual(database.connections, 1)
        self.assertEqual(database.atomics, 1)
        self.assertEqual(record.polychamps_draft['announcement_message'], 700)
        self.assertEqual(record.polychamps_draft['draft_message'], 'Sunday')
        self.assertEqual(record.polychamps_draft['added_message'], 'Sunday')
        record.save.assert_called_once()
        self.assertIn('Actor', audit.call_args.kwargs['message'])
        self.assertEqual(result.announcement_message_id, 700)

    def test_conflict_and_audit_failure_leave_atomic_scope_by_exception(self):
        database = FakeDatabase()
        record = SimpleNamespace(
            polychamps_draft={
                'announcement_message': 999,
                'announcement_channel': 400,
            },
            save=mock.Mock(),
        )
        request = workers.DraftPersistRequest(
            300, 10, 'Actor', None, None, 700, 400, '', 'now'
        )
        with mock.patch.object(workers.models, 'db', database), mock.patch.object(
            workers.models.Configuration, 'get_or_create', return_value=(record, False)
        ), mock.patch.object(
            workers.models.Configuration, 'select', return_value=FakeConfigQuery(record)
        ):
            with self.assertRaises(workers.FreeAgentPostConflictError):
                workers.persist_draft_state(request)
        self.assertEqual(database.atomic_errors, 1)
        record.save.assert_not_called()

        record.polychamps_draft = {
            'announcement_message': None,
            'announcement_channel': None,
        }
        with mock.patch.object(workers.models, 'db', database), mock.patch.object(
            workers.models.Configuration, 'get_or_create', return_value=(record, False)
        ), mock.patch.object(
            workers.models.Configuration, 'select', return_value=FakeConfigQuery(record)
        ), mock.patch.object(
            workers.models.GameLog, 'write', side_effect=RuntimeError('audit failed')
        ):
            with self.assertRaises(RuntimeError):
                workers.persist_draft_state(request)
        self.assertEqual(database.atomic_errors, 2)

    def test_slow_load_keeps_event_loop_responsive(self):
        async def run_case():
            started = threading.Event()
            release = threading.Event()

            def slow(_guild_id):
                started.set()
                release.wait(timeout=2)
                return state()

            with mock.patch.object(workers, 'load_draft_state', side_effect=slow):
                task = asyncio.create_task(workers.run_load_draft_state(300))
                deadline = asyncio.get_running_loop().time() + 1
                while not started.is_set():
                    if asyncio.get_running_loop().time() >= deadline:
                        self.fail('Free Agent worker did not start')
                    await asyncio.sleep(0.001)
                heartbeat = 0
                for _ in range(3):
                    await asyncio.sleep(0.01)
                    heartbeat += 1
                release.set()
                await asyncio.sleep(0.05)
                loaded = await task
            return loaded, heartbeat

        loaded, heartbeat = asyncio.run(run_case())
        self.assertEqual(loaded.guild_id, 300)
        self.assertEqual(heartbeat, 3)

    def test_cancelled_persist_returns_known_committed_result_after_drain(self):
        async def run_case():
            started = threading.Event()
            release = threading.Event()
            expected = workers.DraftPersistResult(300, 10, 700, 400, '')

            def slow(_request):
                started.set()
                release.wait(timeout=2)
                return expected

            persist_request = workers.DraftPersistRequest(
                300, 10, 'Actor', None, None, 700, 400, '', 'now'
            )
            with mock.patch.object(workers, 'persist_draft_state', side_effect=slow):
                task = asyncio.create_task(
                    workers.run_persist_draft_state(persist_request)
                )
                deadline = asyncio.get_running_loop().time() + 1
                while not started.is_set():
                    if asyncio.get_running_loop().time() >= deadline:
                        self.fail('Free Agent persistence worker did not start')
                    await asyncio.sleep(0.001)
                task.cancel()
                release.set()
                await asyncio.sleep(0.05)
                return await task

        loaded = asyncio.run(run_case())
        self.assertEqual(loaded.announcement_message_id, 700)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        workers.free_agent_post_coordinator.release()
        self.guild = FakeGuild()
        self.channel = FakeChannel(self.guild)
        self.guild.channels[self.channel.id] = self.channel
        self.actor = actor()
        self.cog = SimpleNamespace(announcement_message=None)

    def patches(self, **overrides):
        values = dict(
            load=mock.AsyncMock(return_value=state()),
            persist=mock.AsyncMock(return_value=workers.DraftPersistResult(300, 10, 700, 400, '')),
            mod=mock.Mock(return_value=True),
            log=mock.AsyncMock(),
        )
        values.update(overrides)
        return values

    async def test_success_orders_post_reactions_persistence_and_log(self):
        calls = []
        patches = self.patches()

        async def send(*args, **kwargs):
            calls.append('send')
            return self.channel.message

        async def reaction(_emoji):
            calls.append('reaction')

        async def persist(_request):
            calls.append('persist')
            return workers.DraftPersistResult(300, 10, 700, 400, '')

        async def log(*args, **kwargs):
            calls.append('log')

        self.channel.send.side_effect = send
        self.channel.message.add_reaction.side_effect = reaction
        with mock.patch.object(service, 'league_scope', return_value=True), mock.patch.object(
            service, 'is_mod', patches['mod']
        ), mock.patch.object(
            workers, 'run_load_draft_state', patches['load']
        ), mock.patch.object(
            workers, 'run_persist_draft_state', new=mock.AsyncMock(side_effect=persist)
        ), mock.patch.object(
            service.utilities, 'send_to_log_channel', new=mock.AsyncMock(side_effect=log)
        ):
            result = await service.post_announcement(
                cog=self.cog,
                guild=self.guild,
                actor=self.actor,
                channel=self.channel,
                added_message='Sunday',
            )
        self.assertEqual(calls, ['send', 'reaction', 'reaction', 'reaction', 'persist', 'log'])
        self.assertEqual(self.cog.announcement_message, 700)
        self.assertEqual(result.message_id, 700)
        sent_content = self.channel.send.await_args.args[0]
        self.assertIn('Signup opened by <@10>', sent_content)
        self.assertFalse(self.channel.send.await_args.kwargs['allowed_mentions'].everyone)

    async def test_live_duplicate_refuses_before_post(self):
        existing = FakeMessage(999, self.channel)
        self.channel.fetch_message.return_value = existing
        loaded = state(announcement_message_id=999, announcement_channel_id=400, draft_open=True)
        with mock.patch.object(service, 'league_scope', return_value=True), mock.patch.object(
            service, 'is_mod', return_value=True
        ), mock.patch.object(
            workers, 'run_load_draft_state', new=mock.AsyncMock(return_value=loaded)
        ):
            with self.assertRaises(service.FreeAgentPostDuplicateError) as raised:
                await service.post_announcement(
                    cog=self.cog,
                    guild=self.guild,
                    actor=self.actor,
                    channel=self.channel,
                    added_message='',
                )
        self.assertIn('/300/400/999', str(raised.exception))
        self.channel.send.assert_not_awaited()

    async def test_reaction_failure_removes_message_and_never_persists(self):
        self.channel.message.add_reaction.side_effect = RuntimeError('reaction failed')
        persist = mock.AsyncMock()
        with mock.patch.object(service, 'league_scope', return_value=True), mock.patch.object(
            service, 'is_mod', return_value=True
        ), mock.patch.object(
            workers, 'run_load_draft_state', new=mock.AsyncMock(return_value=state())
        ), mock.patch.object(workers, 'run_persist_draft_state', persist):
            with self.assertRaises(service.FreeAgentPostDiscordError):
                await service.post_announcement(
                    cog=self.cog,
                    guild=self.guild,
                    actor=self.actor,
                    channel=self.channel,
                    added_message='',
                )
        self.channel.message.delete.assert_awaited_once()
        persist.assert_not_awaited()

    async def test_persist_failure_removes_message_and_is_safe_to_retry(self):
        with mock.patch.object(service, 'league_scope', return_value=True), mock.patch.object(
            service, 'is_mod', return_value=True
        ), mock.patch.object(
            workers, 'run_load_draft_state', new=mock.AsyncMock(return_value=state())
        ), mock.patch.object(
            workers,
            'run_persist_draft_state',
            new=mock.AsyncMock(side_effect=RuntimeError('db failed')),
        ):
            with self.assertRaises(service.FreeAgentPostDiscordError) as raised:
                await service.post_announcement(
                    cog=self.cog,
                    guild=self.guild,
                    actor=self.actor,
                    channel=self.channel,
                    added_message='',
                )
        self.assertIn('safe to retry', str(raised.exception))
        self.channel.message.delete.assert_awaited_once()
        self.assertIsNone(self.cog.announcement_message)

    async def test_failed_cleanup_is_terminal_visible_reconciliation(self):
        self.channel.message.delete.side_effect = RuntimeError('delete failed')
        with mock.patch.object(service, 'league_scope', return_value=True), mock.patch.object(
            service, 'is_mod', return_value=True
        ), mock.patch.object(
            workers, 'run_load_draft_state', new=mock.AsyncMock(return_value=state())
        ), mock.patch.object(
            workers,
            'run_persist_draft_state',
            new=mock.AsyncMock(side_effect=RuntimeError('db failed')),
        ):
            with self.assertRaises(service.FreeAgentPostReconciliationError):
                await service.post_announcement(
                    cog=self.cog,
                    guild=self.guild,
                    actor=self.actor,
                    channel=self.channel,
                    added_message='',
                )
        self.channel.message.edit.assert_awaited()
        self.assertIn('not activated', self.channel.message.edit.await_args.kwargs['content'])

    async def test_permission_is_rechecked_immediately_before_effects(self):
        with mock.patch.object(service, 'league_scope', return_value=True), mock.patch.object(
            service, 'is_mod', return_value=False
        ):
            with self.assertRaises(workers.FreeAgentPostError):
                await service.post_announcement(
                    cog=self.cog,
                    guild=self.guild,
                    actor=self.actor,
                    channel=self.channel,
                    added_message='',
                )
        self.channel.send.assert_not_awaited()


class ViewAndAdapterTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def root():
        return next(
            command for command in league.league.__cog_app_commands__
            if command.name == 'league'
        )

    def interaction(self, *, actor_value=None, guild=None):
        actor_value = actor_value or actor()
        guild = guild or FakeGuild()
        return SimpleNamespace(
            guild=guild,
            user=actor_value,
            channel_id=400,
            response=SimpleNamespace(
                send_message=mock.AsyncMock(),
                send_modal=mock.AsyncMock(),
                is_done=mock.Mock(return_value=False),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )

    async def test_slash_denial_is_private_and_mod_opens_modal(self):
        cog = league.league.__new__(league.league)
        command = self.root().get_command('free-agents').get_command('post')
        denied = self.interaction()
        with mock.patch.object(service, 'access_error', return_value='mods only'):
            await command.callback(cog, denied, None)
        denied.response.send_message.assert_awaited_once_with(
            'mods only', ephemeral=True
        )

        allowed = self.interaction()
        channel = FakeChannel(allowed.guild)
        allowed.guild.channels[channel.id] = channel
        with mock.patch.object(service, 'access_error', return_value=None), mock.patch.object(
            service, 'default_channel', return_value=channel
        ), mock.patch.object(
            views, 'open_initial_modal', new=mock.AsyncMock()
        ) as opener:
            await command.callback(cog, allowed, None)
        opener.assert_awaited_once()
        draft_view = opener.await_args.args[1]
        self.assertEqual(draft_view.channel, channel)

    async def test_preview_is_private_requester_bound_and_serializable(self):
        guild = FakeGuild()
        channel = FakeChannel(guild)
        view = views.FreeAgentPostView(
            requester_id=10,
            actor_mention='<@10>',
            channel=channel,
            roles=role_snapshot(),
            confirmer=mock.AsyncMock(),
        )
        payload = view.to_components()
        self.assertEqual(payload[0]['type'], 17)
        stranger = SimpleNamespace(
            user=actor(11),
            response=SimpleNamespace(
                is_done=mock.Mock(return_value=False),
                send_message=mock.AsyncMock(),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        self.assertFalse(await view.authorize(stranger))
        self.assertTrue(stranger.response.send_message.await_args.kwargs['ephemeral'])

    async def test_prefix_delegates_to_shared_service(self):
        cog = league.league.__new__(league.league)
        cog.announcement_message = None
        guild = FakeGuild()
        channel = FakeChannel(guild)
        invoked = SimpleNamespace(id=401)
        ctx = SimpleNamespace(
            guild=guild,
            author=actor(),
            message=SimpleNamespace(channel=invoked),
            send=mock.AsyncMock(),
        )
        command = next(
            command for command in league.league.__cog_commands__
            if command.name == 'newfreeagent'
        )
        posted = service.FreeAgentPostResult(300, 10, 400, 700, 'https://example.test/post')
        with mock.patch.object(
            service, 'post_announcement', new=mock.AsyncMock(return_value=posted)
        ) as shared:
            await command.callback(cog, ctx, channel_override=channel, added_message='Sunday')
        shared.assert_awaited_once()
        self.assertIn('posted and activated', ctx.send.await_args.args[0])


if __name__ == '__main__':
    unittest.main()
