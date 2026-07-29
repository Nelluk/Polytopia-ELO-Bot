"""Offline tests for the P2.1 newgame transaction boundary."""

import asyncio
from contextlib import AbstractContextManager
from dataclasses import FrozenInstanceError
import importlib
from types import SimpleNamespace
import threading
import unittest
from unittest import mock
import warnings

warnings.filterwarnings(
    'ignore',
    message="'audioop' is deprecated and slated for removal in Python 3.13",
    category=DeprecationWarning,
)

import peewee
from discord.ext import commands
from peewee import SchemaManager
from playhouse.postgres_ext import PostgresqlExtDatabase


def import_offline_runtime(module_name):
    """Import a model-dependent module without touching PostgreSQL."""

    with mock.patch.object(
        PostgresqlExtDatabase, 'connect', return_value=True
    ), mock.patch.object(
        PostgresqlExtDatabase, 'close', return_value=True
    ), mock.patch.object(
        PostgresqlExtDatabase, 'create_tables'
    ), mock.patch.object(
        SchemaManager, 'create_foreign_key'
    ):
        return importlib.import_module(module_name)


game_workers = import_offline_runtime('modules.game_workers')


class FakeDatabase:
    def __init__(self, state):
        self.state = state
        self.connection_opened = 0
        self.connection_closed = 0
        self.commits = 0
        self.rollbacks = 0

    def connection_context(self):
        database = self

        class ConnectionContext(AbstractContextManager):
            def __enter__(self):
                database.connection_opened += 1
                return database

            def __exit__(self, exc_type, exc_value, traceback):
                database.connection_closed += 1

        return ConnectionContext()

    def atomic(self):
        database = self

        class AtomicContext(AbstractContextManager):
            def __enter__(self):
                self.snapshot = {
                    key: list(value) if isinstance(value, list) else value
                    for key, value in database.state.items()
                }

            def __exit__(self, exc_type, exc_value, traceback):
                if exc_type is None:
                    database.commits += 1
                    return False
                database.rollbacks += 1
                database.state.clear()
                database.state.update(self.snapshot)
                return False

        return AtomicContext()


def new_game_request():
    participant_one = game_workers.NewGameParticipant(
        discord_id=100,
        discord_name='host',
        discord_nick='Host Nick',
        display_name='Host Display',
        role_names=('The Ronin', 'ELO Banned'),
    )
    participant_two = game_workers.NewGameParticipant(
        discord_id=200,
        discord_name='opponent',
        discord_nick=None,
        display_name='Opponent Display',
        role_names=('The Jets',),
    )
    return game_workers.NewGameRequest(
        guild_id=300,
        name='Valid Game',
        is_ranked=True,
        is_mobile=True,
        mod_override=True,
        requester_id=100,
        requester_name='host',
        requester_nick='Host Nick',
        requester_description='**Host Display** (`100`)',
        invoked_with='newgame',
        escaped_game_name='Valid Game',
        sides=((participant_one,), (participant_two,)),
    )


class NewGameWorkerTests(unittest.TestCase):
    def test_request_is_immutable_and_contains_only_snapshot_data(self):
        request = new_game_request()

        with self.assertRaises(FrozenInstanceError):
            request.guild_id = 999
        with self.assertRaises(FrozenInstanceError):
            request.sides[0][0].display_name = 'Changed'

        self.assertIsInstance(request.sides, tuple)
        self.assertTrue(all(isinstance(side, tuple) for side in request.sides))
        self.assertEqual(
            request.sides[0][0].role_names,
            ('The Ronin', 'ELO Banned'),
        )

    def test_worker_owns_connection_and_commits_complete_workflow(self):
        state = {'games': [], 'hosts': [], 'logs': []}
        database = FakeDatabase(state)
        host = SimpleNamespace(id=10)

        class FakeGame:
            id = 42
            host = None

            def save(self):
                state['hosts'].append(self.host.id)

        def create_game(**kwargs):
            state['games'].append(42)
            groups = kwargs['discord_groups']
            self.assertEqual(groups[0][0].id, 100)
            self.assertEqual(groups[0][0].name, 'host')
            self.assertEqual(
                tuple(role.name for role in groups[0][0].roles),
                ('The Ronin', 'ELO Banned'),
            )
            return FakeGame(), ['override warning']

        def write_log(**kwargs):
            state['logs'].append((kwargs['game_id'], kwargs['message']))

        with mock.patch.object(
            game_workers.models, 'db', database
        ), mock.patch.object(
            game_workers.models.Game,
            'create_game',
            side_effect=create_game,
        ), mock.patch.object(
            game_workers.models.Player,
            'get_by_discord_id',
            return_value=(host, False),
        ), mock.patch.object(
            game_workers.models.GameLog,
            'write',
            side_effect=write_log,
        ):
            result = game_workers.create_new_game(new_game_request())

        self.assertEqual(result.game_id, 42)
        self.assertEqual(result.warnings, ('override warning',))
        self.assertEqual(state['games'], [42])
        self.assertEqual(state['hosts'], [10])
        self.assertEqual(state['logs'][0][0], 42)
        self.assertIn('`newgame`', state['logs'][0][1])
        self.assertEqual(database.connection_opened, 1)
        self.assertEqual(database.connection_closed, 1)
        self.assertEqual(database.commits, 1)
        self.assertEqual(database.rollbacks, 0)

    def test_audit_log_failure_rolls_back_game_host_and_log(self):
        state = {'games': [], 'hosts': [], 'logs': []}
        database = FakeDatabase(state)
        host = SimpleNamespace(id=10)

        class FakeGame:
            id = 42
            host = None

            def save(self):
                state['hosts'].append(self.host.id)

        def create_game(**kwargs):
            state['games'].append(42)
            return FakeGame(), []

        def fail_log(**kwargs):
            state['logs'].append((kwargs['game_id'], kwargs['message']))
            raise peewee.OperationalError('simulated log failure')

        with mock.patch.object(
            game_workers.models, 'db', database
        ), mock.patch.object(
            game_workers.models.Game,
            'create_game',
            side_effect=create_game,
        ), mock.patch.object(
            game_workers.models.Player,
            'get_by_discord_id',
            return_value=(host, False),
        ), mock.patch.object(
            game_workers.models.GameLog,
            'write',
            side_effect=fail_log,
        ):
            with self.assertRaisesRegex(
                peewee.OperationalError,
                'simulated log failure',
            ):
                game_workers.create_new_game(new_game_request())

        self.assertEqual(
            state,
            {'games': [], 'hosts': [], 'logs': []},
        )
        self.assertEqual(database.connection_opened, 1)
        self.assertEqual(database.connection_closed, 1)
        self.assertEqual(database.commits, 0)
        self.assertEqual(database.rollbacks, 1)


class NewGameExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_creation_does_not_block_event_loop(self):
        worker_started = threading.Event()
        worker_release = threading.Event()

        def slow_worker(request):
            worker_started.set()
            worker_release.wait(timeout=2)
            return game_workers.NewGameResult(
                game_id=42,
                warnings=(),
            )

        with mock.patch.object(
            game_workers,
            'create_new_game',
            side_effect=slow_worker,
        ):
            task = asyncio.create_task(
                game_workers.run_new_game_creation(new_game_request())
            )
            for _ in range(100):
                if worker_started.is_set():
                    break
                await asyncio.sleep(0.005)
            self.assertTrue(worker_started.is_set())

            heartbeat = asyncio.Event()

            async def pulse():
                await asyncio.sleep(0.01)
                heartbeat.set()

            await asyncio.wait_for(pulse(), timeout=0.2)
            self.assertTrue(heartbeat.is_set())
            worker_release.set()
            # Give restricted headless runners a timer wake-up so the
            # executor completion callback can be delivered.
            await asyncio.sleep(0.05)
            result = await task

        self.assertEqual(result.game_id, 42)


class NewGameCommandTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.games = import_offline_runtime('modules.games')

    def newgame_command(self):
        return next(
            command
            for command in self.games.polygames.__cog_commands__
            if command.name == 'newgame'
        )

    def newgame_slash_command(self):
        return next(
            command
            for command in self.games.polygames.__cog_app_commands__
            if command.name == 'newgame'
        )

    def test_prefix_command_and_aliases_are_preserved(self):
        command = self.newgame_command()

        self.assertIsInstance(command, commands.Command)
        self.assertNotIsInstance(command, commands.HybridCommand)
        self.assertEqual(
            set(command.aliases),
            {
                'newgameunranked',
                'newsteamgame',
                'newsteamgameunranked',
            },
        )

    def test_typed_slash_command_is_registered_through_four_players_per_side(
        self,
    ):
        command = self.newgame_slash_command()
        parameters = {
            parameter.name: parameter for parameter in command.parameters
        }

        self.assertEqual(
            set(parameters),
            {
                'game_name',
                'side_one_player_one',
                'side_two_player_one',
                'ranked',
                'platform',
                'side_one_player_two',
                'side_two_player_two',
                'side_one_player_three',
                'side_two_player_three',
                'side_one_player_four',
                'side_two_player_four',
            },
        )
        self.assertTrue(parameters['game_name'].required)
        self.assertTrue(parameters['side_one_player_one'].required)
        self.assertTrue(parameters['side_two_player_one'].required)
        self.assertFalse(parameters['side_one_player_four'].required)
        self.assertEqual(
            [
                (choice.name, choice.value)
                for choice in parameters['platform'].choices
            ],
            [('Mobile', 'Mobile'), ('Steam', 'Steam')],
        )

    async def test_slash_defers_then_reuses_prefix_checks_and_pipeline(self):
        events = []

        async def defer():
            events.append('defer')

        async def can_run(ctx):
            events.append('checks')
            return True

        async def prefix_callback(cog, ctx, game_name, *args):
            events.append('prefix')
            self.assertIs(cog, fake_cog)
            self.assertEqual(game_name, 'Valid Game')
            self.assertEqual(
                args,
                ('101', '102', 'vs', '201', '202'),
            )

        prefix_command = SimpleNamespace(
            can_run=can_run,
            callback=prefix_callback,
        )
        fake_cog = SimpleNamespace(newgame=prefix_command)
        context = SimpleNamespace(invoked_with='newgame')
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=defer),
        )

        slash_command = self.newgame_slash_command()
        with mock.patch.object(
            self.games.commands.Context,
            'from_interaction',
            new=mock.AsyncMock(return_value=context),
        ):
            await slash_command.callback(
                fake_cog,
                interaction,
                'Valid Game',
                SimpleNamespace(id=101),
                SimpleNamespace(id=201),
                False,
                'Steam',
                SimpleNamespace(id=102),
                SimpleNamespace(id=202),
            )

        self.assertEqual(events, ['defer', 'checks', 'prefix'])
        self.assertEqual(context.invoked_with, 'newsteamgameunranked')

    async def test_slash_check_failure_stops_before_prefix_pipeline(self):
        prefix_command = SimpleNamespace(
            can_run=mock.AsyncMock(return_value=False),
            callback=mock.AsyncMock(),
        )
        fake_cog = SimpleNamespace(newgame=prefix_command)
        context = SimpleNamespace(invoked_with='newgame')
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=mock.AsyncMock()),
        )

        slash_command = self.newgame_slash_command()
        with mock.patch.object(
            self.games.commands.Context,
            'from_interaction',
            new=mock.AsyncMock(return_value=context),
        ):
            await slash_command.callback(
                fake_cog,
                interaction,
                'Valid Game',
                SimpleNamespace(id=101),
                SimpleNamespace(id=201),
            )

        interaction.response.defer.assert_awaited_once()
        prefix_command.can_run.assert_awaited_once_with(context)
        prefix_command.callback.assert_not_awaited()

    async def test_database_failure_prevents_post_commit_discord_effects(self):
        author = SimpleNamespace(
            id=100,
            name='host',
            nick='Host Nick',
            display_name='Host Display',
            roles=(SimpleNamespace(name='The Ronin'),),
        )
        opponent = SimpleNamespace(
            id=200,
            name='opponent',
            nick=None,
            display_name='Opponent Display',
            roles=(SimpleNamespace(name='The Jets'),),
        )
        messages = []

        class Typing:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_value, traceback):
                return False

        context = SimpleNamespace(
            guild=SimpleNamespace(id=300),
            author=author,
            invoked_with='newgame',
            prefix='$',
            typing=lambda: Typing(),
            send=mock.AsyncMock(
                side_effect=lambda message: messages.append(str(message))
            ),
        )

        async def get_member(ctx, argument):
            return [author] if argument == 'host' else [opponent]

        command = self.newgame_command()
        with mock.patch.object(
            self.games.settings,
            'get_user_level',
            return_value=3,
        ), mock.patch.object(
            self.games.settings,
            'can_user_join_game',
            return_value=(True, None),
        ), mock.patch.object(
            self.games.settings,
            'is_staff',
            return_value=False,
        ), mock.patch.object(
            self.games.settings,
            'is_mod',
            return_value=False,
        ), mock.patch.object(
            self.games.utilities,
            'is_valid_poly_gamename',
            return_value=True,
        ), mock.patch.object(
            self.games.utilities,
            'get_guild_member',
            side_effect=get_member,
        ), mock.patch.object(
            self.games.models.GameLog,
            'member_string',
            return_value='**Host Display** (`100`)',
        ), mock.patch.object(
            self.games.game_workers,
            'run_new_game_creation',
            new=mock.AsyncMock(
                side_effect=peewee.OperationalError(
                    'simulated database failure'
                )
            ),
        ), mock.patch.object(
            self.games.Game,
            'load_full_game',
        ) as load_game, mock.patch.object(
            self.games,
            'post_newgame_messaging',
            new=mock.AsyncMock(),
        ) as post_effects, mock.patch.object(
            self.games.logger,
            'exception',
        ):
            await command.callback(
                SimpleNamespace(),
                context,
                'Valid Game',
                'host',
                'vs',
                'opponent',
            )

        load_game.assert_not_called()
        post_effects.assert_not_awaited()
        self.assertTrue(
            any('Error creating new game' in message for message in messages)
        )
