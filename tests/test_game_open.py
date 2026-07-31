"""Focused offline coverage for P5.1 atomic open-game creation."""

import asyncio
from dataclasses import FrozenInstanceError, replace
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import discord
import peewee
from discord.ext import commands

from tests.test_newgame_worker import import_offline_runtime


game_open = import_offline_runtime('modules.game_open')
game_open_views = import_offline_runtime('modules.game_open_views')
game_open_workers = import_offline_runtime('modules.game_open_workers')
games = import_offline_runtime('modules.games')
matchmaking = import_offline_runtime('modules.matchmaking')


def open_request(
    *,
    level=3,
    size=(1, 1),
    is_mobile=True,
    platform_validation_mode=(
        game_open_workers.LEGACY_PLATFORM_VALIDATION_MODE
    ),
):
    return game_open_workers.OpenGameRequest(
        guild_id=300,
        requester_id=100,
        requester_name='host',
        requester_nick='Host Nick',
        prefix='$',
        requester_role_ids=(),
        requester_role_names=(),
        requester_level=level,
        requester_is_mod=False,
        requester_is_staff=False,
        sides=tuple(
            game_open_workers.OpenGameSide(side_size)
            for side_size in size
        ),
        expiration_hours=24,
        is_ranked=True,
        is_mobile=is_mobile,
        notes='A note',
        notes_display='A note',
        requester_description='**Host** (`100`)',
        invoked_with='opengame',
        platform_validation_mode=platform_validation_mode,
    )


class Field:
    def __eq__(self, other):
        return self

    def __and__(self, other):
        return self

    def __rand__(self, other):
        return self

    def in_(self, values):
        return self


class FakeDatabase:
    def __init__(self, state):
        self.state = state
        self.connection_opened = 0
        self.connection_closed = 0
        self.commits = 0
        self.rollbacks = 0

    def connection_context(self):
        database = self

        class ConnectionContext:
            def __enter__(self):
                database.connection_opened += 1
                return database

            def __exit__(self, exc_type, exc_value, traceback):
                database.connection_closed += 1

        return ConnectionContext()

    def atomic(self):
        database = self

        class AtomicContext:
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


def harness_context(harness, *, failure=None):
    settings_values = {
        'require_teams': False,
        'allow_uneven_teams': True,
        'max_team_size': 4,
    }

    def guild_setting(guild_id, name):
        return settings_values.get(name)

    patches = [
        mock.patch.object(game_open_workers.models, 'db', harness.database),
        mock.patch.object(game_open_workers.models, 'Game', harness.game),
        mock.patch.object(game_open_workers.models, 'Player', harness.player),
        mock.patch.object(game_open_workers.models, 'Team', harness.team),
        mock.patch.object(game_open_workers.models, 'GameSide', harness.side),
        mock.patch.object(game_open_workers.models, 'Lineup', harness.lineup),
        mock.patch.object(game_open_workers.models, 'GameLog', harness.log),
        mock.patch.object(
            game_open_workers.settings,
            'guild_setting',
            side_effect=guild_setting,
        ),
        mock.patch.object(
            game_open_workers.settings,
            'can_user_join_game',
            return_value=(True, None),
        ),
    ]
    if failure == 'side':
        patches.append(mock.patch.object(
            harness.side,
            'create',
            side_effect=RuntimeError('side failure'),
        ))
    elif failure == 'lineup':
        patches.append(mock.patch.object(
            harness.lineup,
            'create',
            side_effect=RuntimeError('lineup failure'),
        ))
    elif failure == 'log':
        patches.append(mock.patch.object(
            harness.log,
            'write',
            side_effect=RuntimeError('log failure'),
        ))
    return mock.patch.multiple(
        game_open_workers.models,
        db=harness.database,
        Game=harness.game,
        Player=harness.player,
        Team=harness.team,
        GameSide=harness.side,
        Lineup=harness.lineup,
        GameLog=harness.log,
    ), patches


def make_harness():
    harness = SimpleNamespace()
    state = {
        'games': [],
        'sides': [],
        'lineups': [],
        'logs': [],
        'open_count': 0,
        'host_saves': 0,
        'host_team': 'existing-team',
    }
    harness.state = state
    harness.database = FakeDatabase(state)
    harness.host = SimpleNamespace(
        id=7,
        name='Host',
        team='existing-team',
        discord_member=SimpleNamespace(
            polytopia_name='Host Poly',
            name_steam='Host Steam',
        ),
    )

    def save_host():
        state['host_saves'] += 1
        state['host_team'] = harness.host.team

    harness.host.save = save_host

    class Query:
        def where(self, *args):
            return self

        def count(self):
            return state['open_count']

    class FakeGame:
        host = Field()
        is_pending = Field()

        def __init__(self, game_id):
            self.id = game_id
            self.guild_id = 300

        def first_open_side(self, roles):
            return SimpleNamespace(position=1), False

    class GameModel:
        host = Field()
        is_pending = Field()

        @staticmethod
        def select(*args):
            return Query()

        @staticmethod
        def create(**kwargs):
            game = FakeGame(len(state['games']) + 1)
            state['games'].append((game, kwargs))
            state['open_count'] += 1
            return game

    class PlayerModel:
        @staticmethod
        def get_by_discord_id(**kwargs):
            return harness.host, False

    class GameSideModel:
        @staticmethod
        def create(**kwargs):
            state['sides'].append(kwargs)
            return SimpleNamespace(**kwargs)

    class LineupModel:
        @staticmethod
        def create(**kwargs):
            state['lineups'].append(kwargs)
            return SimpleNamespace(**kwargs)

    class GameLogModel:
        @staticmethod
        def write(**kwargs):
            state['logs'].append(kwargs)

    harness.game = GameModel
    harness.player = PlayerModel
    harness.team = SimpleNamespace()
    harness.side = GameSideModel
    harness.lineup = LineupModel
    harness.log = GameLogModel
    return harness


class OpenGameWorkerTests(unittest.TestCase):
    def run_worker(self, *, failure=None, request=None, host_update=None):
        harness = make_harness()
        if host_update is not None:
            host_update(harness.host)
        patched, patches = harness_context(harness, failure=failure)
        request = request or open_request()
        with patched:
            with mock.patch.object(
                game_open_workers.settings,
                'guild_setting',
                side_effect=lambda guild_id, name: {
                    'require_teams': False,
                    'allow_uneven_teams': True,
                    'max_team_size': 4,
                }.get(name),
            ), mock.patch.object(
                game_open_workers.settings,
                'can_user_join_game',
                return_value=(True, None),
            ):
                for patcher in patches:
                    patcher.start()
                try:
                    if failure:
                        with self.assertRaises(RuntimeError):
                            game_open_workers.create_open_game(request)
                        result = None
                    else:
                        result = game_open_workers.create_open_game(request)
                finally:
                    for patcher in reversed(patches):
                        patcher.stop()
        return harness, result

    def test_request_and_result_are_immutable_primitive_snapshots(self):
        request = open_request()
        with self.assertRaises(FrozenInstanceError):
            request.notes = 'changed'
        self.assertIsInstance(request.sides, tuple)
        self.assertTrue(all(
            isinstance(side, game_open_workers.OpenGameSide)
            for side in request.sides
        ))

        harness, result = self.run_worker()
        with self.assertRaises(FrozenInstanceError):
            result.game_id = 99
        self.assertIsInstance(result.warnings, tuple)
        self.assertIsInstance(result.role_locks, tuple)
        self.assertEqual(harness.database.connection_opened, 1)
        self.assertEqual(harness.database.connection_closed, 1)

    def test_worker_commits_game_sides_lineup_host_and_log(self):
        harness, result = self.run_worker()
        self.assertEqual(result.game_id, 1)
        self.assertEqual(len(harness.state['games']), 1)
        self.assertEqual(len(harness.state['sides']), 2)
        self.assertEqual(len(harness.state['lineups']), 1)
        self.assertEqual(len(harness.state['logs']), 1)
        self.assertEqual(harness.database.commits, 1)
        self.assertEqual(harness.database.rollbacks, 0)

    def test_native_crossplay_accepts_steam_only_host(self):
        request = open_request(
            platform_validation_mode=(
                game_open_workers.CROSSPLAY_PLATFORM_VALIDATION_MODE
            ),
        )
        harness, result = self.run_worker(
            request=request,
            host_update=lambda host: setattr(
                host.discord_member,
                'polytopia_name',
                None,
            ),
        )
        self.assertEqual(result.game_id, 1)
        self.assertTrue(result.is_mobile)
        self.assertTrue(harness.state['games'][0][1]['is_mobile'])

    def test_native_crossplay_requires_an_account_name(self):
        request = open_request(
            platform_validation_mode=(
                game_open_workers.CROSSPLAY_PLATFORM_VALIDATION_MODE
            ),
        )
        with self.assertRaisesRegex(
            game_open_workers.OpenGameValidationError,
            'canonical account name',
        ):
            self.run_worker(
                request=request,
                host_update=lambda host: (
                    setattr(host.discord_member, 'polytopia_name', None),
                    setattr(host.discord_member, 'name_steam', None),
                ),
            )

    def test_legacy_platform_validation_remains_mobile_and_steam_specific(self):
        for is_mobile, missing_field, expected in (
            (True, 'polytopia_name', 'mobile name on file'),
            (False, 'name_steam', 'Steam username on file'),
        ):
            with self.subTest(is_mobile=is_mobile):
                request = open_request(is_mobile=is_mobile)
                with self.assertRaisesRegex(
                    game_open_workers.OpenGameValidationError,
                    expected,
                ):
                    self.run_worker(
                        request=request,
                        host_update=lambda host, field=missing_field: setattr(
                            host.discord_member,
                            field,
                            None,
                        ),
                    )

    def test_worker_preserves_escaped_audit_note_snapshot(self):
        request = replace(
            open_request(),
            notes_display='A *note*',
            log_notes_display=r'A \*note\*',
        )
        harness, result = self.run_worker(request=request)
        self.assertIsNotNone(result)
        self.assertIn(r'A \*note\*', harness.state['logs'][0]['message'])
        self.assertNotIn('A *note*', harness.state['logs'][0]['message'])

    def test_worker_preserves_prefix_warning_order(self):
        request = replace(
            open_request(size=(5, 1)),
            role_lock_message=(
                '**Side 2** will be locked to players with role *Jets*\n'
            ),
        )
        harness, result = self.run_worker(request=request)
        self.assertIsNotNone(result)
        self.assertEqual(
            result.warnings,
            (
                ':warning: Team sizes are uneven.',
                '**Side 2** will be locked to players with role *Jets*\n',
            ),
        )

    def test_side_lineup_and_log_failures_roll_back_everything(self):
        for failure in ('side', 'lineup', 'log'):
            with self.subTest(failure=failure):
                harness, result = self.run_worker(failure=failure)
                self.assertIsNone(result)
                self.assertEqual(harness.state['games'], [])
                self.assertEqual(harness.state['sides'], [])
                self.assertEqual(harness.state['lineups'], [])
                self.assertEqual(harness.state['logs'], [])
                self.assertEqual(harness.state['open_count'], 0)
                self.assertEqual(harness.state['host_team'], 'existing-team')
                self.assertEqual(harness.state['host_saves'], 0)
                self.assertEqual(harness.database.rollbacks, 1)
                self.assertEqual(harness.database.connection_closed, 1)

    def test_size_parser_preserves_arbitrary_v_vs_and_ffa_shapes(self):
        for token, expected in (
            ('1v3', ((1, 3), '1v3')),
            ('2vs2vs1', ((2, 2, 1), '2v2v1')),
            ('2v2vs1', ((2, 2, 1), '2v2v1')),
            ('6FFA', ((1, 1, 1, 1, 1, 1), '1v1v1v1v1v1')),
        ):
            self.assertEqual(
                game_open_workers.parse_game_size_token(token),
                expected,
            )
        with self.assertRaises(game_open_workers.OpenGameSizeError):
            game_open_workers.parse_game_size_token('1FFA')


class PendingGameCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_worker_keeps_event_loop_responsive(self):
        coordinator = game_open_workers.PendingGameCoordinator()
        started = threading.Event()
        release = threading.Event()
        result = mock.Mock()

        def slow_worker(request):
            started.set()
            release.wait(timeout=2)
            return result

        with mock.patch.object(
            game_open_workers,
            'create_open_game',
            side_effect=slow_worker,
        ):
            task = asyncio.create_task(coordinator.run(open_request()))
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.001)
            self.assertTrue(started.is_set())
            ticks = 0
            for _ in range(5):
                ticks += 1
                await asyncio.sleep(0.005)
            self.assertEqual(ticks, 5)
            release.set()
            self.assertIs(await task, result)
            self.assertEqual(coordinator.active_count, 0)
        coordinator.executor.shutdown(wait=True)

    async def test_cancellation_does_not_release_in_flight_thread_early(self):
        coordinator = game_open_workers.PendingGameCoordinator()
        started = threading.Event()
        release = threading.Event()

        def slow_worker(request):
            started.set()
            release.wait(timeout=2)
            return mock.Mock()

        with mock.patch.object(
            game_open_workers,
            'create_open_game',
            side_effect=slow_worker,
        ):
            task = asyncio.create_task(coordinator.run(open_request()))
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.001)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(coordinator.active_count, 1)
            release.set()
            for _ in range(100):
                if coordinator.active_count == 0:
                    break
                await asyncio.sleep(0.001)
            self.assertEqual(coordinator.active_count, 0)
        coordinator.executor.shutdown(wait=True)

    async def test_concurrent_requests_are_serialized_against_open_limit(self):
        harness = make_harness()
        patched, patches = harness_context(harness)
        coordinator = game_open_workers.PendingGameCoordinator()
        with patched:
            for patcher in patches:
                patcher.start()
            try:
                results = await asyncio.gather(*(
                    coordinator.run(open_request(level=1))
                    for _ in range(4)
                ), return_exceptions=True)
            finally:
                for patcher in reversed(patches):
                    patcher.stop()
        successes = [item for item in results if not isinstance(item, Exception)]
        failures = [item for item in results if isinstance(item, Exception)]
        self.assertEqual(len(successes), 3)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(
            failures[0],
            game_open_workers.OpenGameValidationError,
        )
        coordinator.executor.shutdown(wait=True)

    async def test_worker_exception_releases_slot_after_thread_finishes(self):
        coordinator = game_open_workers.PendingGameCoordinator()
        started = threading.Event()
        release = threading.Event()

        def failing_worker(request):
            started.set()
            release.wait(timeout=2)
            raise RuntimeError('worker failure')

        with mock.patch.object(
            game_open_workers,
            'create_open_game',
            side_effect=failing_worker,
        ):
            task = asyncio.create_task(coordinator.run(open_request()))
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.001)
            self.assertTrue(started.is_set())
            self.assertEqual(coordinator.active_count, 1)
            release.set()
            with self.assertRaises(RuntimeError):
                await task
            self.assertEqual(coordinator.active_count, 0)
        coordinator.executor.shutdown(wait=True)


class OpenGameViewTests(unittest.IsolatedAsyncioTestCase):
    def make_view(self, confirmer=None):
        return game_open_views.OpenGameView(
            requester_id=100,
            draft=game_open_views.OpenGameDraft(size=(1, 1)),
            confirmer=confirmer or mock.AsyncMock(),
        )

    async def test_controls_are_requester_only(self):
        view = self.make_view()
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=200),
            response=SimpleNamespace(send_message=mock.AsyncMock()),
        )
        self.assertFalse(await view.interaction_check(interaction))
        interaction.response.send_message.assert_awaited_once_with(
            'Only the requester can control this open-game draft.',
            ephemeral=True,
        )

    async def test_notes_modal_is_requester_only(self):
        view = self.make_view()
        modal = game_open_views.OpenGameNotesModal(view)
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=200),
            response=SimpleNamespace(send_message=mock.AsyncMock()),
        )
        await modal.on_submit(interaction)
        interaction.response.send_message.assert_awaited_once_with(
            'Only the requester can control this open-game draft.',
            ephemeral=True,
        )

    async def test_cancel_and_timeout_do_not_call_creation(self):
        confirmer = mock.AsyncMock()
        view = self.make_view(confirmer)
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=100),
            response=SimpleNamespace(edit_message=mock.AsyncMock()),
        )
        await view._cancel(interaction)
        confirmer.assert_not_awaited()
        self.assertTrue(view.finished)
        self.assertIn('No database', view.status)

        second = self.make_view(confirmer)
        message = SimpleNamespace(edit=mock.AsyncMock())
        second.message = message
        await second.on_timeout()
        confirmer.assert_not_awaited()
        self.assertTrue(second.finished)
        self.assertIn('expired', second.status)
        message.edit.assert_awaited_once_with(view=second)

    async def test_confirm_sends_warnings_and_completion_publicly(self):
        result = game_open_workers.OpenGameResult(
            game_id=42,
            guild_id=300,
            requester_id=100,
            host_name='Host',
            size=(1, 1),
            expiration_hours=24,
            is_ranked=True,
            is_mobile=True,
            notes_display='A note',
            warnings=('A public warning',),
            role_locks=(
                game_open_workers.OpenGameSide(1),
                game_open_workers.OpenGameSide(1),
            ),
        )
        confirmation = SimpleNamespace(
            user=SimpleNamespace(id=100),
            response=SimpleNamespace(defer=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        with mock.patch.object(
            game_open_workers,
            'run_open_game_creation',
            new=mock.AsyncMock(return_value=result),
        ) as run_creation:
            view = self.make_view()
            async def confirm(interaction, draft):
                created = await game_open_workers.run_open_game_creation(
                    open_request()
                )
                await game_open.publish_open_game_result(
                    created,
                    prefix='$',
                    send=lambda message: confirmation.followup.send(
                        message,
                        ephemeral=False,
                    ),
                )

            view.confirmer = confirm
            await view._confirm(confirmation)

        run_creation.assert_awaited_once()
        confirmation.response.defer.assert_awaited_once_with(ephemeral=True)
        self.assertEqual(
            [call.kwargs['ephemeral'] for call in confirmation.followup.send.call_args_list],
            [False, False],
        )

    async def test_discord_failure_is_logged_with_committed_game_id(self):
        result = game_open_workers.OpenGameResult(
            game_id=77,
            guild_id=300,
            requester_id=100,
            host_name='Host',
            size=(1, 1),
            expiration_hours=24,
            is_ranked=True,
            is_mobile=True,
            notes_display='\u200b',
            warnings=(),
            role_locks=(),
        )

        async def failing_send(message):
            raise RuntimeError('Discord unavailable')

        with self.assertLogs(game_open.logger.name, level='ERROR') as logs:
            await game_open.publish_open_game_result(
                result,
                prefix='$',
                send=failing_send,
            )
        self.assertIn('77', '\n'.join(logs.output))


class OpenGameCommandTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.command = next(
            command
            for command in matchmaking.matchmaking.__cog_commands__
            if command.name == 'opengame'
        )
        cls.slash = next(
            command
            for command in games.polygames.__cog_app_commands__
            if command.name == 'game'
        ).get_command('open')

    def test_prefix_aliases_and_native_shape(self):
        self.assertIsInstance(self.command, commands.Command)
        self.assertEqual(
            set(self.command.aliases),
            {'openmatch', 'open', 'opensteam'},
        )
        self.assertEqual(
            [(parameter.name, parameter.type) for parameter in self.slash.parameters],
            [('size', discord.AppCommandOptionType.string)],
        )

    async def test_native_open_acknowledges_before_showing_requester_draft(self):
        context = SimpleNamespace(invoked_with='opengame')
        user = SimpleNamespace(
            id=100,
            name='host',
            nick=None,
            display_name='Host',
            roles=(),
        )
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=300),
            user=user,
            channel_id=301,
            response=SimpleNamespace(defer=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(
                return_value=SimpleNamespace()
            ),
        )
        open_command = SimpleNamespace(can_run=mock.AsyncMock(return_value=True))
        cog = games.polygames.__new__(games.polygames)
        cog.bot = SimpleNamespace(
            get_cog=lambda name: (
                SimpleNamespace(opengame=open_command)
                if name == 'matchmaking'
                else None
            )
        )
        with mock.patch.object(
            games.commands.Context,
            'from_interaction',
            new=mock.AsyncMock(return_value=context),
        ), mock.patch.object(
            games.settings,
            'guild_setting',
            return_value='$',
        ), mock.patch.object(
            games.settings,
            'get_user_level',
            return_value=3,
        ), mock.patch.object(
            games.settings,
            'is_mod',
            return_value=False,
        ), mock.patch.object(
            games.settings,
            'is_staff',
            return_value=False,
        ), mock.patch.object(
            games.models.GameLog,
            'member_string',
            return_value='**Host** (`100`)',
        ), mock.patch.object(
            games.game_open_workers,
            'run_open_game_creation',
            new=mock.AsyncMock(),
        ) as run_creation:
            await self.slash.callback(cog, interaction, '2vs2vs1')

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        open_command.can_run.assert_awaited_once_with(context)
        run_creation.assert_not_awaited()
        view = interaction.edit_original_response.await_args.kwargs['view']
        self.assertIsInstance(view, game_open_views.OpenGameView)
        self.assertEqual(view.draft.size, (2, 2, 1))
        self.assertTrue(view.draft.ranked)

        public_message = SimpleNamespace(add_reaction=mock.AsyncMock())
        confirmation = SimpleNamespace(
            followup=SimpleNamespace(
                send=mock.AsyncMock(return_value=public_message),
            ),
        )
        result = game_open_workers.OpenGameResult(
            game_id=42,
            guild_id=300,
            requester_id=100,
            host_name='Host',
            size=(2, 2, 1),
            expiration_hours=24,
            is_ranked=True,
            is_mobile=True,
            notes_display='\u200b',
            warnings=(),
            role_locks=(),
        )
        with mock.patch.object(
            games.game_open_workers,
            'run_open_game_creation',
            new=mock.AsyncMock(return_value=result),
        ):
            await view.confirmer(confirmation, view.draft)

        confirmation.followup.send.assert_awaited_once_with(
            mock.ANY,
            ephemeral=False,
            wait=True,
        )
        public_message.add_reaction.assert_awaited_once_with(
            games.settings.emoji_join_game,
        )

    async def test_native_open_defaults_unranked_in_configured_channel(self):
        context = SimpleNamespace(invoked_with='opengame')
        user = SimpleNamespace(
            id=100,
            name='host',
            nick=None,
            display_name='Host',
            roles=(),
        )
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=300),
            user=user,
            channel_id=301,
            response=SimpleNamespace(defer=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(
                return_value=SimpleNamespace()
            ),
        )
        open_command = SimpleNamespace(can_run=mock.AsyncMock(return_value=True))
        cog = games.polygames.__new__(games.polygames)
        cog.bot = SimpleNamespace(
            get_cog=lambda name: (
                SimpleNamespace(opengame=open_command)
                if name == 'matchmaking'
                else None
            )
        )

        def guild_setting(guild_id, name):
            return {
                'command_prefix': '$',
                'unranked_game_channel': 301,
            }.get(name)

        with mock.patch.object(
            games.commands.Context,
            'from_interaction',
            new=mock.AsyncMock(return_value=context),
        ), mock.patch.object(
            games.settings,
            'guild_setting',
            side_effect=guild_setting,
        ), mock.patch.object(
            games.settings,
            'get_user_level',
            return_value=3,
        ), mock.patch.object(
            games.settings,
            'is_mod',
            return_value=False,
        ), mock.patch.object(
            games.settings,
            'is_staff',
            return_value=False,
        ), mock.patch.object(
            games.models.GameLog,
            'member_string',
            return_value='**Host** (`100`)',
        ), mock.patch.object(
            games.game_open_workers,
            'run_open_game_creation',
            new=mock.AsyncMock(),
        ):
            await self.slash.callback(cog, interaction, '1v1')

        view = interaction.edit_original_response.await_args.kwargs['view']
        self.assertFalse(view.draft.ranked)

    async def test_prefix_database_failure_has_no_post_commit_discord_effects(self):
        author = SimpleNamespace(
            id=100,
            name='host',
            nick=None,
            display_name='Host',
            roles=(),
        )
        context = SimpleNamespace(
            guild=SimpleNamespace(id=300),
            author=author,
            channel=SimpleNamespace(id=301),
            prefix='$',
            invoked_with='opengame',
            send=mock.AsyncMock(),
        )
        with mock.patch.object(
            matchmaking.settings,
            'guild_setting',
            return_value=None,
        ), mock.patch.object(
            matchmaking.settings,
            'get_user_level',
            return_value=3,
        ), mock.patch.object(
            matchmaking.settings,
            'is_mod',
            return_value=False,
        ), mock.patch.object(
            matchmaking.settings,
            'is_staff',
            return_value=False,
        ), mock.patch.object(
            matchmaking.models.GameLog,
            'member_string',
            return_value='**Host** (`100`)',
        ), mock.patch.object(
            matchmaking.game_open_workers,
            'run_open_game_creation',
            new=mock.AsyncMock(
                side_effect=peewee.OperationalError('database failure')
            ),
        ), mock.patch.object(
            matchmaking.models.Game,
            'load_full_game',
        ) as load_game, mock.patch.object(
            matchmaking,
            'broadcast_team_game_to_server',
            new=mock.AsyncMock(),
        ) as broadcast:
            await self.command.callback(SimpleNamespace(), context, args='1v1')

        load_game.assert_not_called()
        broadcast.assert_not_awaited()
        self.assertTrue(context.send.await_count >= 1)
