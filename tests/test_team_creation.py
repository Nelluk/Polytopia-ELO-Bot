"""Focused offline coverage for the P8.5 native team-creation workflow."""

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


team_creation_workers = import_offline_runtime('modules.team_creation_workers')
team_creation = import_offline_runtime('modules.team_creation')
administration = import_offline_runtime('modules.administration')


class TeamDatabase:
    def __init__(self):
        self.state = {'teams': [], 'logs': []}
        self.events = []
        self.connection_opened = 0
        self.connection_closed = 0
        self.commits = 0
        self.rollbacks = 0

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
                self.teams = list(database.state['teams'])
                self.logs = list(database.state['logs'])
                database.events.append('atomic-open')

            def __exit__(self, exc_type, exc_value, traceback):
                if exc_type is None:
                    database.commits += 1
                    database.events.append('commit')
                    return False
                database.rollbacks += 1
                database.state['teams'] = self.teams
                database.state['logs'] = self.logs
                database.events.append('rollback')
                return False

        return AtomicContext()


class TeamRecord:
    def __init__(self, database, *, team_id, name, guild_id):
        self.database = database
        self.id = team_id
        self.name = name
        self.guild_id = guild_id


class FakeTeamModel:
    database = None
    next_id = 100
    create_calls = []
    fail_create = None

    @classmethod
    def create(cls, **kwargs):
        cls.create_calls.append(kwargs)
        if cls.fail_create is not None:
            raise cls.fail_create
        team = TeamRecord(
            cls.database,
            team_id=cls.next_id,
            name=kwargs['name'],
            guild_id=kwargs['guild_id'],
        )
        cls.next_id += 1
        cls.database.state['teams'].append(team)
        return team


class FakeGameLog:
    database = None
    fail = None

    @classmethod
    def write(cls, **kwargs):
        if cls.fail is not None:
            raise cls.fail
        cls.database.state['logs'].append(kwargs)
        cls.database.events.append('audit')


def request(**overrides):
    values = dict(
        guild_id=300,
        requester_id=100,
        requester_is_mod=True,
        team_enabled=True,
        name='  The Ronin  ',
        requester_description='**Mod** (`100`)',
        native=True,
        invoked_with='/team create',
    )
    values.update(overrides)
    return team_creation_workers.TeamCreationRequest(**values)


class TeamCreationWorkerTests(unittest.TestCase):
    def setUp(self):
        self.database = TeamDatabase()
        FakeTeamModel.database = self.database
        FakeTeamModel.next_id = 100
        FakeTeamModel.create_calls = []
        FakeTeamModel.fail_create = None
        FakeGameLog.database = self.database
        FakeGameLog.fail = None
        self.patches = ExitStack()
        self.patches.enter_context(
            mock.patch.object(team_creation_workers.models, 'db', self.database)
        )
        self.patches.enter_context(
            mock.patch.object(team_creation_workers.models, 'Team', FakeTeamModel)
        )
        self.patches.enter_context(
            mock.patch.object(
                team_creation_workers.models,
                'GameLog',
                FakeGameLog,
            )
        )
        self.addCleanup(self.patches.close)

    def test_request_and_result_are_frozen_primitive_snapshots(self):
        value = request()
        with self.assertRaises(FrozenInstanceError):
            value.guild_id = 999
        self.assertIsInstance(value.name, str)
        self.assertIsInstance(value.requester_description, str)
        self.assertNotIn('Member', repr(value))

        result = team_creation_workers.create_team(value)
        with self.assertRaises(FrozenInstanceError):
            result.team_name = 'Changed'
        self.assertNotIn('TeamRecord', repr(result))

    def test_name_validation_trims_and_enforces_role_bounds(self):
        self.assertEqual(
            team_creation_workers.validate_team_name('  A  '),
            'A',
        )
        self.assertEqual(
            len(team_creation_workers.validate_team_name('x' * 100)),
            100,
        )
        for value in (
            None,
            '',
            '   ',
            'x' * 101,
            'line\nbreak',
            'zero\u200bwidth',
            '@everyone',
        ):
            with self.subTest(value=value), self.assertRaises(
                team_creation_workers.TeamCreationValidationError
            ):
                team_creation_workers.validate_team_name(value)

    def test_create_uses_only_team_defaults_and_audits_actual_guild_atomically(self):
        result = team_creation_workers.create_team(request())

        self.assertEqual(result.guild_id, 300)
        self.assertEqual(result.team_id, 100)
        self.assertEqual(result.team_name, 'The Ronin')
        self.assertEqual(
            FakeTeamModel.create_calls,
            [{
                'name': 'The Ronin',
                'guild_id': 300,
                'is_hidden': False,
            }],
        )
        self.assertEqual(len(self.database.state['teams']), 1)
        self.assertEqual(len(self.database.state['logs']), 1)
        self.assertEqual(self.database.state['logs'][0]['guild_id'], 300)
        self.assertIn('**Mod** (`100`)', self.database.state['logs'][0]['message'])
        self.assertIn('The Ronin', self.database.state['logs'][0]['message'])
        self.assertIn('/team create', self.database.state['logs'][0]['message'])
        self.assertEqual(self.database.commits, 1)
        self.assertEqual(self.database.rollbacks, 0)
        self.assertEqual(self.database.connection_opened, 1)
        self.assertEqual(self.database.connection_closed, 1)
        self.assertEqual(
            self.database.events,
            [
                'connection-open',
                'atomic-open',
                'audit',
                'commit',
                'connection-close',
            ],
        )

    def test_worker_authoritatively_rechecks_mod_and_allow_teams_snapshots(self):
        for overrides in (
            {'requester_is_mod': False},
            {'team_enabled': False},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(
                team_creation_workers.TeamCreationPermissionError
            ):
                team_creation_workers.create_team(request(**overrides))
        self.assertEqual(self.database.state['teams'], [])
        self.assertEqual(self.database.state['logs'], [])
        self.assertEqual(self.database.commits, 0)
        self.assertEqual(self.database.rollbacks, 2)

    def test_duplicate_and_racing_insert_are_bounded_conflicts(self):
        team_creation_workers.create_team(request())
        FakeTeamModel.fail_create = peewee.IntegrityError('unique violation')

        with self.assertRaisesRegex(
            team_creation_workers.TeamCreationConflictError,
            'already exists',
        ):
            team_creation_workers.create_team(request())

        self.assertEqual(len(self.database.state['teams']), 1)
        self.assertEqual(len(self.database.state['logs']), 1)
        self.assertEqual(self.database.commits, 1)
        self.assertEqual(self.database.rollbacks, 1)
        self.assertEqual(self.database.connection_opened, 2)
        self.assertEqual(self.database.connection_closed, 2)

    def test_audit_failure_rolls_back_team_and_closes_worker_connection(self):
        FakeGameLog.fail = peewee.OperationalError('audit failed')

        with self.assertRaises(peewee.PeeweeException):
            team_creation_workers.create_team(request())

        self.assertEqual(self.database.state['teams'], [])
        self.assertEqual(self.database.state['logs'], [])
        self.assertEqual(self.database.commits, 0)
        self.assertEqual(self.database.rollbacks, 1)
        self.assertEqual(self.database.connection_opened, 1)
        self.assertEqual(self.database.connection_closed, 1)

    def test_worker_keeps_event_loop_responsive_and_drains_cancellation(self):
        async def check():
            release = threading.Event()
            result = team_creation_workers.TeamCreationResult(
                guild_id=300,
                team_id=100,
                team_name='The Ronin',
                native=True,
            )

            def blocked_worker(_request):
                release.wait(1)
                return result

            executor = ThreadPoolExecutor(max_workers=1)
            task = None
            try:
                with mock.patch.object(
                    team_creation_workers.team_emoji_workers,
                    '_team_emoji_executor',
                    executor,
                ), mock.patch.object(
                    team_creation_workers,
                    'create_team',
                    side_effect=blocked_worker,
                ):
                    task = asyncio.create_task(
                        team_creation_workers.run_team_creation(request())
                    )
                    await asyncio.sleep(0)
                    start = time.monotonic()
                    await asyncio.sleep(0.02)
                    elapsed = time.monotonic() - start
                    self.assertLess(elapsed, 0.2)
                    task.cancel()
                    self.assertFalse(task.done())
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
            finally:
                release.set()
                if task is not None and not task.done():
                    task.cancel()
                executor.shutdown(wait=True)

        asyncio.run(check())


class TeamCreationAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.member = SimpleNamespace(
            id=100,
            display_name='Mod',
            name='Mod',
            mention='<@100>',
        )

    def interaction(self):
        return SimpleNamespace(
            guild=SimpleNamespace(id=300),
            user=self.member,
            response=SimpleNamespace(
                send_message=mock.AsyncMock(),
                defer=mock.AsyncMock(),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
            delete_original_response=mock.AsyncMock(),
            channel=SimpleNamespace(send=mock.AsyncMock()),
        )

    def test_native_success_copy_has_actor_role_convention_and_attribute_guidance(self):
        actor = team_creation.capture_actor(self.member)
        message = team_creation.native_success_message(
            team_creation_workers.TeamCreationResult(
                guild_id=300,
                team_id=100,
                team_name='The Ronin',
                native=True,
            ),
            actor=actor,
        )
        self.assertIn('<@100>', message)
        self.assertIn('The Ronin', message)
        self.assertIn('role exactly matching', message)
        self.assertIn('/team house', message)
        self.assertIn('/team tier', message)

    async def test_native_denial_is_private_before_defer(self):
        interaction = self.interaction()
        with mock.patch.object(
            administration.team_creation_service.settings,
            'guild_setting',
            return_value=True,
        ), mock.patch.object(
            administration.team_creation_service.settings,
            'is_mod',
            return_value=False,
        ):
            command = next(
                command
                for command in administration.administration.__cog_app_commands__
                if command.name == 'team'
            ).get_command('create')
            cog = administration.administration.__new__(administration.administration)
            await command.callback(cog, interaction, 'The Ronin')

        interaction.response.send_message.assert_awaited_once()
        self.assertTrue(
            interaction.response.send_message.await_args.kwargs['ephemeral']
        )
        interaction.response.defer.assert_not_awaited()
        interaction.channel.send.assert_not_awaited()

    async def test_native_failure_is_private_and_has_no_public_discord_effect(self):
        interaction = self.interaction()
        result = team_creation_workers.TeamCreationResult(
            guild_id=300,
            team_id=100,
            team_name='The Ronin',
            native=True,
        )
        with mock.patch.object(
            administration.team_creation_service.settings,
            'guild_setting',
            return_value=True,
        ), mock.patch.object(
            administration.team_creation_service.settings,
            'is_mod',
            return_value=True,
        ), mock.patch.object(
            administration.team_creation_service,
            'run_create',
            new=mock.AsyncMock(
                side_effect=team_creation_workers.TeamCreationConflictError(
                    'A team named "The Ronin" already exists on this server.'
                )
            ),
        ):
            command = next(
                command
                for command in administration.administration.__cog_app_commands__
                if command.name == 'team'
            ).get_command('create')
            cog = administration.administration.__new__(administration.administration)
            await command.callback(cog, interaction, 'The Ronin')

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.followup.send.assert_awaited_once()
        self.assertTrue(interaction.followup.send.await_args.kwargs['ephemeral'])
        interaction.channel.send.assert_not_awaited()
        interaction.delete_original_response.assert_not_awaited()

    async def test_native_success_publishes_only_after_worker_result(self):
        interaction = self.interaction()
        result = team_creation_workers.TeamCreationResult(
            guild_id=300,
            team_id=100,
            team_name='The Ronin',
            native=True,
        )
        with mock.patch.object(
            administration.team_creation_service.settings,
            'guild_setting',
            return_value=True,
        ), mock.patch.object(
            administration.team_creation_service.settings,
            'is_mod',
            return_value=True,
        ), mock.patch.object(
            administration.team_creation_service,
            'run_create',
            new=mock.AsyncMock(return_value=result),
        ):
            command = next(
                command
                for command in administration.administration.__cog_app_commands__
                if command.name == 'team'
            ).get_command('create')
            cog = administration.administration.__new__(administration.administration)
            await command.callback(cog, interaction, 'The Ronin')

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.delete_original_response.assert_awaited_once()
        interaction.channel.send.assert_awaited_once()
        self.assertIn(
            'The Ronin',
            interaction.channel.send.await_args.args[0],
        )
        interaction.followup.send.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
