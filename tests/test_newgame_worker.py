"""Offline tests for the P2.1 newgame transaction boundary."""

import asyncio
from contextlib import AbstractContextManager
from dataclasses import FrozenInstanceError, replace
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
import discord
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
game_record_views = import_offline_runtime('modules.game_record_views')


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
        name='Valid War Game',
        is_ranked=True,
        is_mobile=True,
        mod_override=True,
        requester_is_staff=True,
        requester_id=100,
        requester_name='host',
        requester_nick='Host Nick',
        requester_description='**Host Display** (`100`)',
        invoked_with='newgame',
        escaped_game_name='Valid War Game',
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

    def test_non_staff_name_restrictions_are_enforced_inside_worker(self):
        for name in ('War', 'Unusual Name'):
            with self.subTest(name=name):
                database = FakeDatabase({'games': [], 'hosts': [], 'logs': []})
                request = replace(
                    new_game_request(),
                    name=name,
                    escaped_game_name=name,
                    requester_is_staff=False,
                )
                with (
                    mock.patch.object(game_workers.models, 'db', database),
                    mock.patch.object(
                        game_workers.utilities,
                        'is_valid_poly_gamename',
                        return_value=False,
                    ),
                    mock.patch.object(
                        game_workers.models.Game,
                        'create_game',
                    ) as create_game,
                ):
                    with self.assertRaises(ValueError):
                        game_workers.create_new_game(request)

                create_game.assert_not_called()
                self.assertEqual(database.commits, 0)
                self.assertEqual(database.rollbacks, 1)

    def test_staff_name_override_warns_after_commit_and_preserves_input(self):
        state = {'games': [], 'hosts': [], 'logs': []}
        database = FakeDatabase(state)
        host = SimpleNamespace(id=10)
        captured = {}

        class FakeGame:
            id = 42
            host = None

            def save(self):
                state['hosts'].append(self.host.id)

        def create_game(**kwargs):
            captured.update(kwargs)
            return FakeGame(), []

        request = replace(
            new_game_request(),
            name='unusual Name',
            escaped_game_name='unusual Name',
            mod_override=False,
            requester_is_staff=True,
        )
        with (
            mock.patch.object(game_workers.models, 'db', database),
            mock.patch.object(
                game_workers.utilities,
                'is_valid_poly_gamename',
                return_value=False,
            ),
            mock.patch.object(
                game_workers.models.Game,
                'create_game',
                side_effect=create_game,
            ),
            mock.patch.object(
                game_workers.models.Player,
                'get_by_discord_id',
                return_value=(host, False),
            ),
            mock.patch.object(game_workers.models.GameLog, 'write'),
        ):
            result = game_workers.create_new_game(request)

        self.assertEqual(captured['name'], 'unusual Name')
        self.assertEqual(len(result.warnings), 1)
        self.assertIn('staff permission', result.warnings[0])
        self.assertEqual(database.commits, 1)
        self.assertEqual(database.rollbacks, 0)

    def test_non_mod_staff_keeps_normal_total_and_side_limits(self):
        member = lambda identifier: SimpleNamespace(
            id=identifier,
            name=f'player-{identifier}',
            nick=None,
            display_name=f'Player {identifier}',
            roles=(),
        )
        groups = [[member(1), member(2), member(3)], [member(4)]]

        with (
            mock.patch.object(game_workers.models.settings, 'max_game_size', 3),
            mock.patch.object(
                game_workers.models.settings,
                'guild_setting',
                side_effect=lambda _guild_id, name: {
                    'allow_uneven_teams': True,
                    'max_team_size': 16,
                }.get(name),
            ),
        ):
            with self.assertRaisesRegex(ValueError, 'Maximum players'):
                game_workers.models.Game.create_game(
                    groups,
                    guild_id=300,
                    name='War Game',
                    mod_override=False,
                )

        groups = [[member(1), member(2), member(3)], [member(4), member(5)]]
        with (
            mock.patch.object(game_workers.models.settings, 'max_game_size', 16),
            mock.patch.object(
                game_workers.models.settings,
                'guild_setting',
                side_effect=lambda _guild_id, name: {
                    'allow_uneven_teams': True,
                    'max_team_size': 2,
                }.get(name),
            ),
        ):
            with self.assertRaisesRegex(ValueError, 'over 2 members'):
                game_workers.models.Game.create_game(
                    groups,
                    guild_id=300,
                    name='War Game',
                    mod_override=False,
                )

    def test_mod_override_allows_total_and_side_limits_with_warnings(self):
        member = lambda identifier: SimpleNamespace(
            id=identifier,
            name=f'player-{identifier}',
            nick=None,
            display_name=f'Player {identifier}',
            roles=(),
        )
        groups = [[member(1), member(2), member(3)], [member(4), member(5)]]
        teams = (SimpleNamespace(name='Team One'), SimpleNamespace(name='Team Two'))
        atomic = mock.MagicMock()
        with (
            mock.patch.object(game_workers.models.settings, 'max_game_size', 3),
            mock.patch.object(
                game_workers.models.settings,
                'discord_id_ban_list',
                (),
            ),
            mock.patch.object(
                game_workers.models.settings,
                'guild_setting',
                side_effect=lambda _guild_id, name: {
                    'allow_uneven_teams': True,
                    'max_team_size': 2,
                    'require_teams': False,
                }.get(name),
            ),
            mock.patch.object(
                game_workers.models.Game,
                'pregame_check',
                return_value=([
                    [None, None, None],
                    [None, None],
                ], teams),
            ),
            mock.patch.object(
                game_workers.models.Game,
                'create',
                return_value=SimpleNamespace(id=99),
            ),
            mock.patch.object(
                game_workers.models.Player,
                'upsert',
                side_effect=lambda **kwargs: (
                    SimpleNamespace(
                        id=kwargs['discord_id'],
                        name=f"Player {kwargs['discord_id']}",
                    ),
                    False,
                ),
            ),
            mock.patch.object(
                game_workers.models.Squad,
                'upsert',
                return_value=SimpleNamespace(id=501),
            ),
            mock.patch.object(
                game_workers.models.GameSide,
                'create',
                side_effect=lambda **kwargs: SimpleNamespace(**kwargs),
            ),
            mock.patch.object(game_workers.models.Lineup, 'create'),
            mock.patch.object(
                game_workers.models.db,
                'atomic',
                return_value=atomic,
            ),
        ):
            game, warnings_value = game_workers.models.Game.create_game(
                groups,
                guild_id=300,
                name='War Game',
                mod_override=True,
            )

        self.assertEqual(game.id, 99)
        self.assertTrue(any('maximum players per game' in item for item in warnings_value))
        self.assertTrue(any('maximum team size' in item for item in warnings_value))

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


class GameRecordRosterTests(unittest.TestCase):
    def test_parses_unequal_and_multiple_sides(self):
        self.assertEqual(
            game_record_views.parse_roster_string(
                'alpha beta vs gamma vs delta epsilon zeta'
            ),
            (
                ('alpha', 'beta'),
                ('gamma',),
                ('delta', 'epsilon', 'zeta'),
            ),
        )

    def test_preserves_single_opponent_shortcut(self):
        sides = game_record_views.parse_roster_string('opponent')
        self.assertEqual(sides, (('opponent',),))
        self.assertEqual(
            game_record_views.roster_arguments(sides),
            ('opponent',),
        )

    def test_supports_quoted_member_tokens(self):
        self.assertEqual(
            game_record_views.parse_roster_string(
                '"Player One" vs "Player Two"'
            ),
            (('Player One',), ('Player Two',)),
        )

    def test_rejects_ambiguous_or_incomplete_sides(self):
        invalid = ('alpha beta', 'alpha vs', 'vs beta', 'alpha vs vs beta')
        for roster in invalid:
            with self.subTest(roster=roster):
                with self.assertRaises(
                    game_record_views.RosterSyntaxError
                ):
                    game_record_views.parse_roster_string(roster)


class GameRecordViewTests(unittest.IsolatedAsyncioTestCase):
    def make_view(self):
        preview = game_record_views.GameRecordPreview(
            game_name='Valid Game',
            roster='alpha vs beta gamma',
            ranked=True,
            sides=(
                (game_record_views.RosterMember(1, 'Alpha'),),
                (
                    game_record_views.RosterMember(2, 'Beta'),
                    game_record_views.RosterMember(3, 'Gamma'),
                ),
            ),
        )
        return game_record_views.GameRecordView(
            requester_id=100,
            preview=preview,
            confirmer=mock.AsyncMock(),
        )

    def test_preview_uses_components_v2_and_shows_unequal_sides(self):
        view = self.make_view()
        self.assertIsInstance(view, discord.ui.LayoutView)
        text = '\n'.join(
            item.content
            for item in view.walk_children()
            if isinstance(item, discord.ui.TextDisplay)
        )
        self.assertIn('Side 1:** Alpha', text)
        self.assertIn('Side 2:** Beta, Gamma', text)
        self.assertLessEqual(view.total_children_count, 40)

    async def test_controls_are_requester_only(self):
        view = self.make_view()
        denied = SimpleNamespace(
            user=SimpleNamespace(id=999),
            response=SimpleNamespace(send_message=mock.AsyncMock()),
        )
        allowed = SimpleNamespace(
            user=SimpleNamespace(id=100),
            response=denied.response,
        )
        self.assertFalse(await view.interaction_check(denied))
        denied.response.send_message.assert_awaited_once_with(
            'Only the requester can control this game draft.',
            ephemeral=True,
        )
        self.assertTrue(await view.interaction_check(allowed))

    async def test_cancel_never_calls_confirmation(self):
        view = self.make_view()
        interaction = SimpleNamespace(
            response=SimpleNamespace(edit_message=mock.AsyncMock()),
        )
        await view._cancel(interaction)
        view.confirmer.assert_not_awaited()
        self.assertTrue(view.finished)
        self.assertIn('Cancelled', view.status)

    async def test_retryable_failure_preserves_exact_draft_and_restores_controls(self):
        view = self.make_view()
        original_preview = replace(
            view.preview,
            game_name='Exact Title Case Game',
            roster='<@10> vs <@20> <@30>',
            ranked=False,
            sides=(
                (game_record_views.RosterMember(10, 'Edited One'),),
                (
                    game_record_views.RosterMember(20, 'Edited Two'),
                    game_record_views.RosterMember(30, 'Edited Three'),
                ),
            ),
        )
        view.preview = original_preview
        view.confirmer = mock.AsyncMock(
            return_value=game_record_views.GameRecordConfirmationOutcome.retryable(
                'The roster is no longer valid.'
            )
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(edit_message=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(),
        )

        await view._confirm(interaction)

        self.assertFalse(view.finished)
        self.assertFalse(view.submission_in_flight)
        self.assertFalse(view.is_finished())
        self.assertEqual(view.preview, original_preview)
        self.assertIn('retry', view.status.lower())
        confirm = next(
            child for child in view.walk_children()
            if isinstance(child, discord.ui.Button)
            and child.label == 'Confirm record'
        )
        self.assertFalse(confirm.disabled)
        view.confirmer.assert_awaited_once_with(interaction, original_preview)

    async def test_retryable_failure_then_success_can_commit_once(self):
        view = self.make_view()
        view.confirmer = mock.AsyncMock(side_effect=[
            game_record_views.GameRecordConfirmationOutcome.retryable(
                'Database validation failed.'
            ),
            game_record_views.GameRecordConfirmationOutcome.committed(
                'Game ID 42 was recorded.'
            ),
        ])

        first = SimpleNamespace(
            response=SimpleNamespace(edit_message=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(),
        )
        second = SimpleNamespace(
            response=SimpleNamespace(edit_message=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(),
        )
        await view._confirm(first)
        self.assertFalse(view.finished)

        await view._confirm(second)

        self.assertTrue(view.finished)
        self.assertTrue(view.is_finished())
        self.assertEqual(
            view.outcome.state,
            game_record_views.GameRecordConfirmationState.COMMITTED,
        )
        self.assertEqual(view.confirmer.await_count, 2)

    async def test_double_submit_is_rejected_while_confirmation_is_running(self):
        view = self.make_view()
        started = asyncio.Event()
        release = asyncio.Event()

        async def pending(_interaction, _preview):
            started.set()
            await release.wait()
            return game_record_views.GameRecordConfirmationOutcome.committed(
                'Game ID 42 was recorded.'
            )

        view.confirmer = mock.AsyncMock(side_effect=pending)
        first = SimpleNamespace(
            response=SimpleNamespace(edit_message=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(),
        )
        second = SimpleNamespace(
            response=SimpleNamespace(
                edit_message=mock.AsyncMock(),
                send_message=mock.AsyncMock(),
            ),
            edit_original_response=mock.AsyncMock(),
        )

        first_task = asyncio.create_task(view._confirm(first))
        await started.wait()
        await view._confirm(second)

        view.confirmer.assert_awaited_once()
        second.response.send_message.assert_awaited_once()
        self.assertTrue(view.submission_in_flight)
        self.assertFalse(view.finished)
        release.set()
        await first_task
        self.assertTrue(view.finished)

    async def test_reconciliation_state_finishes_without_retry(self):
        view = self.make_view()
        view.confirmer = mock.AsyncMock(
            return_value=game_record_views.GameRecordConfirmationOutcome.reconciliation(
                'Game ID 42 was committed, but public publication failed. '
                'Do not retry.'
            )
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(
                edit_message=mock.AsyncMock(),
                send_message=mock.AsyncMock(),
            ),
            edit_original_response=mock.AsyncMock(),
        )

        await view._confirm(interaction)

        self.assertTrue(view.finished)
        self.assertTrue(view.is_finished())
        self.assertIn('Do not retry', view.status)
        await view._confirm(interaction)
        view.confirmer.assert_awaited_once()
        interaction.response.send_message.assert_awaited_once()

    async def test_edit_sides_uses_native_user_selector(self):
        view = self.make_view()
        interaction = SimpleNamespace(
            response=SimpleNamespace(edit_message=mock.AsyncMock()),
        )
        await view._edit(interaction)
        self.assertTrue(view.editing)
        self.assertIsInstance(view.member_select, discord.ui.UserSelect)
        self.assertEqual(
            [item.id for item in view.member_select.default_values],
            [1],
        )

        view.member_select._values = [
            SimpleNamespace(id=10, display_name='New Alpha'),
            SimpleNamespace(id=11, display_name='New Ally'),
        ]
        await view._replace_side(interaction)
        self.assertEqual(
            view.preview.roster,
            '<@10> <@11> vs <@2> <@3>',
        )
        self.assertEqual(
            [member.display_name for member in view.preview.sides[0]],
            ['New Alpha', 'New Ally'],
        )

    async def test_side_editor_can_add_and_remove_sides(self):
        view = self.make_view()
        interaction = SimpleNamespace(
            response=SimpleNamespace(
                edit_message=mock.AsyncMock(),
                send_message=mock.AsyncMock(),
            ),
        )
        await view._edit(interaction)
        await view._add_side(interaction)
        self.assertEqual(len(view.preview.sides), 3)
        self.assertEqual(view.preview.sides[2], ())
        await view._done_editing(interaction)
        interaction.response.send_message.assert_awaited_once_with(
            'Select at least one player for every side.',
            ephemeral=True,
        )
        await view._remove_side(interaction)
        self.assertEqual(len(view.preview.sides), 2)


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
        game_group = next(
            command
            for command in self.games.polygames.__cog_app_commands__
            if command.name == 'game'
        )
        return game_group.get_command('record')

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

    def test_record_command_has_one_roster_and_no_platform_option(self):
        command = self.newgame_slash_command()
        parameters = {
            parameter.name: parameter for parameter in command.parameters
        }

        self.assertEqual(
            set(parameters),
            {'game_name', 'roster', 'ranked'},
        )
        self.assertTrue(parameters['game_name'].required)
        self.assertTrue(parameters['roster'].required)
        self.assertFalse(parameters['ranked'].required)

    async def test_slash_previews_then_confirmation_reuses_prefix_pipeline(
        self,
    ):
        events = []

        async def defer(**kwargs):
            events.append('defer')
            self.assertEqual(kwargs, {'ephemeral': True})

        async def can_run(ctx):
            events.append('checks')
            return True

        async def prefix_callback(cog, ctx, game_name, *args):
            events.append('prefix')
            self.assertIs(cog, fake_cog)
            self.assertEqual(game_name, 'Valid Game')
            self.assertEqual(
                args,
                ('101', '102', 'vs', '201', '202', '203'),
            )

        prefix_command = SimpleNamespace(
            can_run=can_run,
            callback=prefix_callback,
        )
        fake_cog = SimpleNamespace(newgame=prefix_command)
        context = SimpleNamespace(
            invoked_with='newgame',
            author=SimpleNamespace(id=101),
        )
        preview_message = SimpleNamespace(edit=mock.AsyncMock())
        edited = {}

        async def edit_original_response(**kwargs):
            events.append('preview')
            edited.update(kwargs)
            return preview_message

        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=defer),
            guild=SimpleNamespace(id=300),
            user=SimpleNamespace(id=101),
            edit_original_response=edit_original_response,
        )
        resolved = (
            (
                SimpleNamespace(id=101, display_name='One'),
                SimpleNamespace(id=102, display_name='Two'),
            ),
            (
                SimpleNamespace(id=201, display_name='Three'),
                SimpleNamespace(id=202, display_name='Four'),
                SimpleNamespace(id=203, display_name='Five'),
            ),
        )

        slash_command = self.newgame_slash_command()
        with mock.patch.object(
            self.games.commands.Context,
            'from_interaction',
            new=mock.AsyncMock(return_value=context),
        ), mock.patch.object(
            self.games.settings,
            'guild_setting',
            return_value='$',
        ), mock.patch.object(
            self.games,
            'resolve_newgame_roster',
            new=mock.AsyncMock(return_value=resolved),
        ):
            await slash_command.callback(
                fake_cog,
                interaction,
                'Valid Game',
                '101 102 vs 201 202 203',
                False,
            )

        self.assertEqual(events, ['defer', 'checks', 'preview'])
        self.assertNotIn('prefix', events)
        view = edited['view']
        self.assertIsInstance(view, game_record_views.GameRecordView)
        self.assertEqual(
            tuple(
                tuple(member.display_name for member in side)
                for side in view.preview.sides
            ),
            (('One', 'Two'), ('Three', 'Four', 'Five')),
        )

        confirmation = SimpleNamespace(
            user=SimpleNamespace(id=101),
            guild=SimpleNamespace(id=300),
            response=SimpleNamespace(edit_message=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(),
        )
        await view._confirm(confirmation)

        self.assertEqual(events, ['defer', 'checks', 'preview', 'prefix'])
        self.assertEqual(context.invoked_with, 'newgameunranked')
        self.assertEqual(context.prefix, '$')

    async def test_slash_check_failure_stops_before_prefix_pipeline(self):
        prefix_command = SimpleNamespace(
            can_run=mock.AsyncMock(return_value=False),
            callback=mock.AsyncMock(),
        )
        fake_cog = SimpleNamespace(newgame=prefix_command)
        context = SimpleNamespace(invoked_with='newgame')
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=mock.AsyncMock()),
            guild=SimpleNamespace(id=300),
            user=SimpleNamespace(id=101),
        )

        slash_command = self.newgame_slash_command()
        with mock.patch.object(
            self.games.commands.Context,
            'from_interaction',
            new=mock.AsyncMock(return_value=context),
        ), mock.patch.object(
            self.games.settings,
            'guild_setting',
            return_value='$',
        ):
            await slash_command.callback(
                fake_cog,
                interaction,
                'Valid Game',
                '101 vs 201',
            )

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
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
            invoked_with='newsteamgame',
            prefix='$',
            typing=lambda: Typing(),
            send=mock.AsyncMock(
                side_effect=lambda message: messages.append(str(message))
            ),
        )

        async def get_member(ctx, argument):
            return [author] if argument == 'host' else [opponent]

        worker_requests = []

        async def fail_worker(request):
            worker_requests.append(request)
            raise peewee.OperationalError('simulated database failure')

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
            new=mock.AsyncMock(side_effect=fail_worker),
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
        self.assertTrue(worker_requests[0].is_mobile)
        self.assertTrue(
            any('Error creating new game' in message for message in messages)
        )

    async def test_record_precommit_failure_is_private_and_retryable(self):
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
        context = SimpleNamespace(
            guild=SimpleNamespace(id=300),
            author=author,
            invoked_with='newgame',
            prefix='$',
            interaction=SimpleNamespace(),
            _game_record_confirmation=True,
            typing=lambda: SimpleNamespace(
                __aenter__=mock.AsyncMock(return_value=None),
                __aexit__=mock.AsyncMock(return_value=False),
            ),
            send=mock.AsyncMock(),
        )

        class Typing:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_value, traceback):
                return False

        context.typing = lambda: Typing()

        async def fail_worker(request):
            del request
            raise peewee.OperationalError('database unavailable')

        command = self.newgame_command()
        with (
            mock.patch.object(self.games.settings, 'get_user_level', return_value=3),
            mock.patch.object(self.games.settings, 'can_user_join_game', return_value=(True, None)),
            mock.patch.object(self.games.settings, 'is_staff', return_value=False),
            mock.patch.object(self.games.settings, 'is_mod', return_value=False),
            mock.patch.object(self.games.utilities, 'is_valid_poly_gamename', return_value=True),
            mock.patch.object(self.games, 'resolve_newgame_roster', new=mock.AsyncMock(return_value=((author,), (opponent,)))),
            mock.patch.object(self.games.models.GameLog, 'member_string', return_value='Host'),
            mock.patch.object(
                self.games.game_workers,
                'run_new_game_creation',
                new=mock.AsyncMock(side_effect=fail_worker),
            ),
            mock.patch.object(self.games.Game, 'load_full_game') as load_game,
            mock.patch.object(self.games, 'post_newgame_messaging', new=mock.AsyncMock()) as post,
            mock.patch.object(self.games.logger, 'exception'),
        ):
            outcome = await command.callback(
                SimpleNamespace(),
                context,
                'Exact Game Name',
                'host',
                'vs',
                'opponent',
            )

        self.assertEqual(
            outcome.state,
            game_record_views.GameRecordConfirmationState.RETRYABLE_FAILURE,
        )
        context.send.assert_not_awaited()
        load_game.assert_not_called()
        post.assert_not_awaited()

    async def test_record_commit_with_public_effect_failure_is_reconciliation_only(self):
        author = SimpleNamespace(
            id=100,
            name='host',
            nick='Host Nick',
            display_name='Host Display',
            roles=(SimpleNamespace(name='Helper'),),
        )
        opponent = SimpleNamespace(
            id=200,
            name='opponent',
            nick=None,
            display_name='Opponent Display',
            roles=(),
        )

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
            interaction=SimpleNamespace(),
            _game_record_confirmation=True,
            typing=lambda: Typing(),
            send=mock.AsyncMock(),
        )
        committed = self.games.game_workers.NewGameResult(
            game_id=42,
            warnings=(),
        )
        command = self.newgame_command()
        with (
            mock.patch.object(self.games.settings, 'get_user_level', return_value=3),
            mock.patch.object(self.games.settings, 'can_user_join_game', return_value=(True, None)),
            mock.patch.object(self.games.settings, 'is_staff', return_value=True),
            mock.patch.object(self.games.settings, 'is_mod', return_value=True),
            mock.patch.object(self.games.utilities, 'is_valid_poly_gamename', return_value=True),
            mock.patch.object(self.games, 'resolve_newgame_roster', new=mock.AsyncMock(return_value=((author,), (opponent,)))),
            mock.patch.object(self.games.models.GameLog, 'member_string', return_value='Host'),
            mock.patch.object(self.games.game_workers, 'run_new_game_creation', new=mock.AsyncMock(return_value=committed)),
            mock.patch.object(self.games.Game, 'load_full_game', return_value=SimpleNamespace(id=42)),
            mock.patch.object(self.games, 'post_newgame_messaging', new=mock.AsyncMock(side_effect=RuntimeError('public send failed'))) as post,
            mock.patch.object(self.games.logger, 'exception'),
        ):
            outcome = await command.callback(
                SimpleNamespace(),
                context,
                'Exact Game Name',
                'host',
                'vs',
                'opponent',
            )

        self.assertEqual(
            outcome.state,
            game_record_views.GameRecordConfirmationState.RECONCILIATION,
        )
        self.assertIn('Do not retry', outcome.message)
        post.assert_awaited_once()

    async def test_staff_override_warning_is_public_after_worker_commit(self):
        events = []
        author = SimpleNamespace(
            id=100,
            name='host',
            nick='Host Nick',
            display_name='Host Display',
            roles=(SimpleNamespace(name='Helper'),),
        )
        opponent = SimpleNamespace(
            id=200,
            name='opponent',
            nick=None,
            display_name='Opponent Display',
            roles=(),
        )

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
                side_effect=lambda message: events.append(str(message))
            ),
        )

        async def get_member(ctx, argument):
            return [author] if argument == 'host' else [opponent]

        async def committed_worker(request):
            events.append('committed')
            return self.games.game_workers.NewGameResult(
                game_id=42,
                warnings=(
                    ':warning: staff override warning',
                ),
            )

        async def post_effects(ctx, game):
            events.append('post-commit effects')

        command = self.newgame_command()
        with (
            mock.patch.object(self.games.settings, 'get_user_level', return_value=3),
            mock.patch.object(self.games.settings, 'can_user_join_game', return_value=(True, None)),
            mock.patch.object(self.games.settings, 'is_staff', return_value=True),
            mock.patch.object(self.games.settings, 'is_mod', return_value=True),
            mock.patch.object(self.games.utilities, 'is_valid_poly_gamename', return_value=False),
            mock.patch.object(self.games.utilities, 'get_guild_member', side_effect=get_member),
            mock.patch.object(
                self.games.models.GameLog,
                'member_string',
                return_value='**Host Display** (`100`)',
            ),
            mock.patch.object(self.games, 'resolve_newgame_roster', new=mock.AsyncMock(return_value=((author,), (opponent,)))),
            mock.patch.object(
                self.games.game_workers,
                'run_new_game_creation',
                new=mock.AsyncMock(side_effect=committed_worker),
            ),
            mock.patch.object(
                self.games.Game,
                'load_full_game',
                return_value=SimpleNamespace(id=42),
            ),
            mock.patch.object(
                self.games,
                'post_newgame_messaging',
                new=mock.AsyncMock(side_effect=post_effects),
            ),
        ):
            await command.callback(
                SimpleNamespace(),
                context,
                'Unusual Name',
                'host',
                'vs',
                'opponent',
            )

        self.assertEqual(
            events,
            [
                'committed',
                ':warning: staff override warning',
                'post-commit effects',
            ],
        )
