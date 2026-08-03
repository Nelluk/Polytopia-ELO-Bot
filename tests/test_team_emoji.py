"""Focused offline coverage for the P8.1 team-emoji workflow."""

import asyncio
from contextlib import AbstractContextManager, ExitStack
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import threading
import time
import unittest
from unittest import mock

import discord
import peewee
from tests.test_newgame_worker import import_offline_runtime


team_emoji_workers = import_offline_runtime('modules.team_emoji_workers')
team_emoji = import_offline_runtime('modules.team_emoji')
administration = import_offline_runtime('modules.administration')


class TeamDatabase:
    def __init__(self, team):
        self.team = team
        self.state = {'logs': []}
        self.events = []
        self.connection_opened = 0
        self.connection_closed = 0
        self.commits = 0
        self.rollbacks = 0
        self.fail_save = False
        self.fail_audit = False

    def connection_context(self):
        database = self

        class ConnectionContext(AbstractContextManager):
            def __enter__(self):
                database.connection_opened += 1
                database.events.append('connection-open')
                return database

            def __exit__(self, exc_type, exc_value, traceback):
                database.connection_closed += 1
                database.events.append('connection-close')
                return False

        return ConnectionContext()

    def atomic(self):
        database = self

        class AtomicContext(AbstractContextManager):
            def __enter__(self):
                self.old_emoji = database.team.emoji
                self.old_logs = list(database.state['logs'])
                database.events.append('atomic-open')

            def __exit__(self, exc_type, exc_value, traceback):
                if exc_type is None:
                    database.commits += 1
                    database.events.append('commit')
                    return False
                database.rollbacks += 1
                database.team.emoji = self.old_emoji
                database.state['logs'] = self.old_logs
                database.events.append('rollback')
                return False

        return AtomicContext()


class TeamRecord:
    def __init__(self, database, *, team_id=42, name='Ronin', emoji='😀'):
        self.database = database
        self.id = team_id
        self.guild_id = 300
        self.name = name
        self.emoji = emoji

    def save(self):
        self.database.events.append('save')
        if self.database.fail_save:
            raise peewee.OperationalError('save failed')


class FakeTeamModel:
    record = None
    responses = {}
    calls = []

    @classmethod
    def get_by_name(cls, team_name, guild_id, **kwargs):
        cls.calls.append((team_name, guild_id, kwargs))
        return cls.responses.get(team_name, (cls.record,))


class FakeGameLog:
    database = None

    @staticmethod
    def member_string(member):
        return f'**{member.display_name}** (`{member.id}`)'

    @classmethod
    def write(cls, **kwargs):
        cls.database.events.append('audit')
        if cls.database.fail_audit:
            raise peewee.OperationalError('audit failed')
        cls.database.state['logs'].append(kwargs)


def mutation_request(**overrides):
    values = dict(
        guild_id=300,
        requester_id=100,
        requester_is_mod=True,
        team_enabled=True,
        team_lookup='Ronin',
        emoji='❤️',
        clear=False,
        requester_description='**Mod** (`100`)',
        expected_emoji=None,
        native=True,
        invoked_with='/team emoji',
    )
    values.update(overrides)
    return team_emoji_workers.TeamEmojiMutationRequest(**values)


def read_request(**overrides):
    values = dict(
        guild_id=300,
        requester_id=100,
        requester_is_mod=True,
        team_enabled=True,
        team_lookup='Ronin',
        requester_description='**Mod** (`100`)',
        invoked_with='/team emoji',
    )
    values.update(overrides)
    return team_emoji_workers.TeamEmojiReadRequest(**values)


class TeamEmojiWorkerTests(unittest.TestCase):
    def setUp(self):
        self.database = TeamDatabase(None)
        self.team = TeamRecord(self.database)
        self.database.team = self.team
        FakeTeamModel.record = self.team
        FakeTeamModel.responses = {}
        FakeTeamModel.calls = []
        FakeGameLog.database = self.database
        self.patches = ExitStack()
        self.patches.enter_context(
            mock.patch.object(team_emoji_workers.models, 'db', self.database)
        )
        self.patches.enter_context(
            mock.patch.object(team_emoji_workers.models, 'Team', FakeTeamModel)
        )
        self.patches.enter_context(
            mock.patch.object(
                team_emoji_workers.models,
                'GameLog',
                FakeGameLog,
            )
        )
        self.patches.enter_context(
            mock.patch.object(team_emoji.settings, 'guild_setting', return_value=True)
        )
        self.patches.enter_context(
            mock.patch.object(team_emoji.settings, 'is_mod', return_value=True)
        )
        self.addCleanup(self.patches.close)

    def test_requests_are_immutable_primitive_snapshots(self):
        request = mutation_request()
        with self.assertRaises(FrozenInstanceError):
            request.guild_id = 999
        self.assertIsInstance(request.team_lookup, str)
        self.assertIsInstance(request.requester_description, str)
        self.assertNotIn('Member', repr(request))

    def test_read_edit_and_clear_use_worker_connections_and_atomic_audit(self):
        read_result = team_emoji_workers.read_team_emoji(read_request())
        self.assertEqual(read_result.emoji, '😀')
        self.assertEqual(self.database.commits, 0)
        self.assertEqual(self.database.connection_opened, 1)
        self.assertEqual(self.database.connection_closed, 1)

        result = team_emoji_workers.set_team_emoji(mutation_request())
        self.assertEqual(result.old_emoji, '😀')
        self.assertEqual(result.emoji, '❤️')
        self.assertEqual(self.team.emoji, '❤️')
        self.assertEqual(self.database.commits, 1)
        self.assertEqual(self.database.rollbacks, 0)
        self.assertEqual(self.database.state['logs'][0]['guild_id'], 300)

        cleared = team_emoji_workers.set_team_emoji(
            mutation_request(emoji=None, clear=True)
        )
        self.assertTrue(cleared.cleared)
        self.assertEqual(cleared.emoji, '')
        self.assertEqual(self.team.emoji, '')
        self.assertEqual(self.database.commits, 2)
        self.assertEqual(self.database.connection_opened, 3)
        self.assertEqual(self.database.connection_closed, 3)
        self.assertEqual(len(self.database.state['logs']), 2)

    def test_unicode_and_custom_emoji_syntax_are_accepted_without_cache(self):
        for value in (
            '😀',
            '❤️',
            '👍🏽',
            '👩‍💻',
            '🇺🇸',
            '1️⃣',
            '<:ronin:123456789012345678>',
            '<a:ronin_wave:123456789012345678>',
        ):
            with self.subTest(value=value):
                self.assertTrue(team_emoji_workers.is_valid_emoji(value))

    def test_malformed_and_conflicting_values_are_rejected(self):
        for value in ('plain text', 'ab', '😀 text', '<:x:123456789012345678>'):
            with self.subTest(value=value):
                self.assertFalse(team_emoji_workers.is_valid_emoji(value))

        before = self.team.emoji
        with self.assertRaises(team_emoji_workers.TeamEmojiValidationError):
            team_emoji_workers.set_team_emoji(
                mutation_request(emoji='😀', clear=True)
            )
        self.assertEqual(self.team.emoji, before)
        self.assertEqual(self.database.commits, 0)
        self.assertEqual(self.database.rollbacks, 1)
        self.assertEqual(self.database.state['logs'], [])

    def test_ambiguous_and_inferred_team_resolution_is_private_and_authoritative(self):
        FakeTeamModel.responses['R'] = (
            self.team,
            TeamRecord(self.database, team_id=43, name='Ravens'),
        )
        with self.assertRaises(team_emoji_workers.TeamEmojiLookupError):
            team_emoji_workers.read_team_emoji(read_request(team_lookup='R'))

        with mock.patch.object(
            team_emoji_workers,
            '_inferred_team_matches',
            return_value=(self.team,),
        ):
            inferred = team_emoji_workers.read_team_emoji(
                read_request(team_lookup=None)
            )
        self.assertEqual(inferred.team_name, 'Ronin')

        with mock.patch.object(
            team_emoji_workers,
            '_inferred_team_matches',
            return_value=(self.team, TeamRecord(self.database, team_id=44)),
        ), self.assertRaises(team_emoji_workers.TeamEmojiLookupError):
            team_emoji_workers.read_team_emoji(read_request(team_lookup=None))

    def test_permission_and_team_setting_checks_match_read_and_mutation(self):
        for request in (
            read_request(requester_is_mod=False),
            read_request(team_enabled=False),
        ):
            with self.subTest(request=request), self.assertRaises(
                team_emoji_workers.TeamEmojiPermissionError
            ):
                team_emoji_workers.read_team_emoji(request)

        for request in (
            mutation_request(requester_is_mod=False),
            mutation_request(team_enabled=False),
        ):
            with self.subTest(request=request), self.assertRaises(
                team_emoji_workers.TeamEmojiPermissionError
            ):
                team_emoji_workers.set_team_emoji(request)
        self.assertEqual(self.database.commits, 0)
        self.assertEqual(self.database.state['logs'], [])

    def test_stale_expected_value_is_private_and_does_not_mutate(self):
        with self.assertRaises(team_emoji_workers.TeamEmojiConflictError):
            team_emoji_workers.set_team_emoji(
                mutation_request(expected_emoji='old value')
            )
        self.assertEqual(self.team.emoji, '😀')
        self.assertEqual(self.database.state['logs'], [])
        self.assertEqual(self.database.rollbacks, 1)

    def test_failed_save_or_audit_rolls_back_and_closes_connection(self):
        self.database.fail_save = True
        with self.assertRaises(peewee.PeeweeException):
            team_emoji_workers.set_team_emoji(mutation_request())
        self.assertEqual(self.team.emoji, '😀')
        self.assertEqual(self.database.rollbacks, 1)
        self.assertEqual(self.database.connection_opened, 1)
        self.assertEqual(self.database.connection_closed, 1)

        self.database.fail_save = False
        self.database.fail_audit = True
        with self.assertRaises(peewee.PeeweeException):
            team_emoji_workers.set_team_emoji(mutation_request())
        self.assertEqual(self.team.emoji, '😀')
        self.assertEqual(self.database.rollbacks, 2)
        self.assertEqual(self.database.connection_opened, 2)
        self.assertEqual(self.database.connection_closed, 2)

    def test_worker_keeps_event_loop_responsive(self):
        async def check():
            release = threading.Event()
            result = team_emoji_workers.TeamEmojiReadResult(
                guild_id=300,
                team_id=42,
                team_name='Ronin',
                emoji='😀',
            )

            def blocked_worker(_request):
                release.wait(1)
                return result

            request = read_request()
            executor = ThreadPoolExecutor(max_workers=1)
            try:
                with mock.patch.object(
                    team_emoji_workers,
                    '_team_emoji_executor',
                    executor,
                ), mock.patch.object(
                    team_emoji_workers,
                    'read_team_emoji',
                    side_effect=blocked_worker,
                ):
                    task = asyncio.create_task(
                        team_emoji_workers.run_team_emoji_read(request)
                    )
                    await asyncio.sleep(0)
                    start = time.monotonic()
                    await asyncio.sleep(0.02)
                    elapsed = time.monotonic() - start
                    release.set()
                    self.assertLess(elapsed, 0.2)
                    await asyncio.sleep(0.05)
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            finally:
                executor.shutdown(wait=True)

        asyncio.run(check())


class TeamEmojiServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.member = SimpleNamespace(
            id=100,
            display_name='Mod',
            name='Mod',
            mention='<@100>',
        )

    async def test_actor_attribution_and_prefix_success_wording(self):
        actor = team_emoji.capture_actor(self.member)
        result = team_emoji_workers.TeamEmojiMutationResult(
            guild_id=300,
            team_id=42,
            team_name='Ronin',
            old_emoji='😀',
            emoji='❤️',
            cleared=False,
            native=True,
        )
        self.assertIn('<@100>', team_emoji.native_mutation_message(result, actor=actor))
        self.assertEqual(
            team_emoji.legacy_mutation_message(result),
            'Team **Ronin** updated with new emoji: ❤️',
        )
        self.assertEqual(
            team_emoji.legacy_read_message(
                team_emoji_workers.TeamEmojiReadResult(
                    guild_id=300,
                    team_id=42,
                    team_name='Ronin',
                    emoji='😀',
                )
            ),
            'Emoji for team **Ronin**: 😀',
        )
        clear_result = dataclasses_replace(result, emoji='', cleared=True)
        self.assertIn('cleared', team_emoji.native_mutation_message(clear_result, actor=actor))

    async def test_post_commit_callback_runs_after_commit_and_not_on_failure(self):
        events = []
        request = mutation_request()

        async def worker(_request):
            events.append('commit')
            return team_emoji_workers.TeamEmojiMutationResult(
                guild_id=300,
                team_id=42,
                team_name='Ronin',
                old_emoji='😀',
                emoji='❤️',
                cleared=False,
                native=True,
            )

        async def after_commit(_result):
            events.append('public-output')

        with mock.patch.object(
            team_emoji_workers,
            'run_team_emoji_mutation',
            new=worker,
        ):
            await team_emoji.run_mutation(request, after_commit=after_commit)
        self.assertEqual(events, ['commit', 'public-output'])

        async def failed_worker(_request):
            raise peewee.OperationalError('database failed')

        events.clear()
        with mock.patch.object(
            team_emoji_workers,
            'run_team_emoji_mutation',
            new=failed_worker,
        ):
            with self.assertRaises(peewee.PeeweeException):
                await team_emoji.run_mutation(request, after_commit=after_commit)
        self.assertEqual(events, [])

    async def test_public_interaction_sender_deletes_private_defer_before_output(self):
        events = []

        async def delete_original():
            events.append('delete-deferred')

        async def send(content, **kwargs):
            events.append(('public', content, kwargs))

        interaction = SimpleNamespace(
            delete_original_response=delete_original,
            channel=SimpleNamespace(send=send),
        )
        sender = team_emoji.public_interaction_sender(interaction)
        await sender('visible')
        self.assertEqual(events, ['delete-deferred', ('public', 'visible', {})])


class TeamEmojiAdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_prefix_command_registration_and_read_wording(self):
        command = next(
            command
            for command in administration.administration.__cog_commands__
            if command.name == 'team_emoji'
        )
        self.assertEqual(command.name, 'team_emoji')
        self.assertEqual(command.usage, 'team_name new_emoji')

    async def test_prefix_adapter_routes_read_and_edit_through_shared_service(self):
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=300),
            author=SimpleNamespace(id=100),
            prefix='$',
            invoked_with='team_emoji',
            send=mock.AsyncMock(),
        )
        cog = administration.administration.__new__(administration.administration)
        command = next(
            command
            for command in administration.administration.__cog_commands__
            if command.name == 'team_emoji'
        )
        read_result = team_emoji_workers.TeamEmojiReadResult(
            guild_id=300,
            team_id=42,
            team_name='Ronin',
            emoji='😀',
        )
        with mock.patch.object(
            administration.team_emoji_service,
            'build_read_request',
            return_value=SimpleNamespace(),
        ) as build_read, mock.patch.object(
            administration.team_emoji_service,
            'run_read',
            new=mock.AsyncMock(return_value=read_result),
        ) as run_read:
            await command.callback(cog, ctx, 'Ronin')
        build_read.assert_called_once_with(
            member=ctx.author,
            guild_id=300,
            team_lookup='Ronin',
            invoked_with='team_emoji',
        )
        run_read.assert_awaited_once()
        ctx.send.assert_awaited_once_with('Emoji for team **Ronin**: 😀')

        ctx.send.reset_mock()
        mutation_result = team_emoji_workers.TeamEmojiMutationResult(
            guild_id=300,
            team_id=42,
            team_name='Ronin',
            old_emoji='😀',
            emoji='❤️',
            cleared=False,
            native=False,
        )

        async def run_mutation(_request, *, after_commit):
            await after_commit(mutation_result)

        with mock.patch.object(
            administration.team_emoji_service,
            'build_mutation_request',
            return_value=SimpleNamespace(),
        ) as build_mutation, mock.patch.object(
            administration.team_emoji_service,
            'run_mutation',
            new=run_mutation,
        ):
            await command.callback(cog, ctx, 'Ronin', '❤️')
        build_mutation.assert_called_once_with(
            member=ctx.author,
            guild_id=300,
            team_lookup='Ronin',
            emoji='❤️',
            native=False,
            invoked_with='team_emoji',
        )
        ctx.send.assert_awaited_once_with(
            'Team **Ronin** updated with new emoji: ❤️'
        )

    async def test_native_denials_are_private_before_defer(self):
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=300),
            user=SimpleNamespace(id=100),
            response=SimpleNamespace(send_message=mock.AsyncMock()),
        )
        cog = administration.administration.__new__(administration.administration)
        with mock.patch.object(
            administration.settings,
            'guild_setting',
            return_value=True,
        ), mock.patch.object(
            administration.settings,
            'is_mod',
            return_value=False,
        ):
            command = next(
                command
                for command in administration.administration.__cog_app_commands__
                if command.name == 'team'
            ).get_command('emoji')
            await command.callback(cog, interaction, None, None, False)
        interaction.response.send_message.assert_awaited_once()
        self.assertTrue(
            interaction.response.send_message.await_args.kwargs['ephemeral']
        )

    async def test_native_conflict_is_private_without_worker_submission(self):
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=300),
            user=SimpleNamespace(id=100),
            response=SimpleNamespace(send_message=mock.AsyncMock()),
        )
        cog = administration.administration.__new__(administration.administration)
        command = next(
            command
            for command in administration.administration.__cog_app_commands__
            if command.name == 'team'
        ).get_command('emoji')
        with mock.patch.object(
            administration.settings,
            'guild_setting',
            return_value=True,
        ), mock.patch.object(
            administration.settings,
            'is_mod',
            return_value=True,
        ):
            await command.callback(cog, interaction, 'Ronin', '😀', True)
        interaction.response.send_message.assert_awaited_once()
        self.assertTrue(
            interaction.response.send_message.await_args.kwargs['ephemeral']
        )


def dataclasses_replace(value, **changes):
    """Small local helper keeping this test file import-light."""

    values = {
        field: getattr(value, field)
        for field in value.__dataclass_fields__
    }
    values.update(changes)
    return type(value)(**values)
