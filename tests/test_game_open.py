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
    sides=None,
    requester_role_ids=(),
    requester_role_names=(),
    notes='A note',
    notes_display='A note',
    role_lock_message='',
    size_display=None,
):
    sides = sides or tuple(
        game_open_workers.OpenGameSide(side_size)
        for side_size in size
    )
    return game_open_workers.OpenGameRequest(
        guild_id=300,
        requester_id=100,
        requester_name='host',
        requester_nick='Host Nick',
        prefix='$',
        requester_role_ids=tuple(requester_role_ids),
        requester_role_names=tuple(requester_role_names),
        requester_level=level,
        requester_is_mod=False,
        requester_is_staff=False,
        sides=tuple(sides),
        expiration_hours=24,
        is_ranked=True,
        is_mobile=is_mobile,
        notes=notes,
        notes_display=notes_display,
        requester_description='**Host** (`100`)',
        invoked_with='opengame',
        role_lock_message=role_lock_message,
        size_display=size_display,
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
        'role_team': SimpleNamespace(name='Jets', id=501),
        'first_side_position': 1,
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
            return SimpleNamespace(position=state['first_side_position']), False

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
    class TeamQuery:
        def where(self, *args):
            return self

        def __iter__(self):
            return iter((state['role_team'],))

    class TeamModel:
        guild_id = Field()
        name = Field()

        @staticmethod
        def select(*args):
            return TeamQuery()

        @staticmethod
        def get_or_except(*, team_name, guild_id, require_exact):
            if team_name == state['role_team'].name:
                return state['role_team']
            raise game_open_workers.exceptions.NoSingleMatch()

    harness.team = TeamModel
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

    def test_crossplay_accepts_mobile_only_steam_only_or_both_host_names(self):
        for names in (
            ('Host Poly', None),
            (None, 'Host Steam'),
            ('Host Poly', 'Host Steam'),
        ):
            with self.subTest(names=names):
                request = open_request(is_mobile=False)
                harness, result = self.run_worker(
                    request=request,
                    host_update=lambda host, names=names: (
                        setattr(host.discord_member, 'polytopia_name', names[0]),
                        setattr(host.discord_member, 'name_steam', names[1]),
                    ),
                )
                self.assertEqual(result.game_id, 1)
                self.assertTrue(result.is_mobile)
                self.assertTrue(harness.state['games'][0][1]['is_mobile'])

    def test_crossplay_requires_one_canonical_host_name(self):
        request = open_request()
        with self.assertRaisesRegex(
            game_open_workers.OpenGameValidationError,
            'canonical Polytopia account name',
        ):
            self.run_worker(
                request=request,
                host_update=lambda host: (
                    setattr(host.discord_member, 'polytopia_name', None),
                    setattr(host.discord_member, 'name_steam', None),
                ),
            )

    def test_crossplay_storage_does_not_follow_primitive_compatibility_flag(self):
        request = open_request(is_mobile=False)
        harness, result = self.run_worker(
            request=request,
        )
        self.assertEqual(result.game_id, 1)
        self.assertTrue(result.is_mobile)
        self.assertTrue(harness.state['games'][0][1]['is_mobile'])

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

    def test_role_locks_persist_team_preassignment_and_requester_placement(self):
        sides = (
            game_open_workers.OpenGameSide(1),
            game_open_workers.OpenGameSide(
                1,
                required_role_id=501,
                required_role_name='Jets',
            ),
        )
        request = open_request(
            sides=sides,
            requester_role_ids=(501,),
            requester_role_names=('Jets',),
            role_lock_message=(
                '**Side 2** will be locked to players with role *Jets*\n'
            ),
        )
        harness = make_harness()
        harness.state['first_side_position'] = 2
        patched, patches = harness_context(harness)
        with patched:
            for patcher in patches:
                patcher.start()
            try:
                result = game_open_workers.create_open_game(request)
            finally:
                for patcher in reversed(patches):
                    patcher.stop()

        self.assertEqual(
            [side['required_role_id'] for side in harness.state['sides']],
            [None, 501],
        )
        self.assertEqual(
            [side['sidename'] for side in harness.state['sides']],
            [None, 'Jets'],
        )
        self.assertIs(
            harness.state['sides'][1]['team'],
            harness.state['role_team'],
        )
        self.assertIs(harness.state['host_team'], harness.state['role_team'])
        self.assertEqual(
            harness.state['lineups'][0]['gameside'].position,
            2,
        )
        self.assertIn('not be the game host', '\n'.join(result.warnings))
        self.assertIn('Side 2', '\n'.join(result.warnings))

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

    async def test_shared_join_reaction_helper_uses_configured_emoji(self):
        public_message = SimpleNamespace(add_reaction=mock.AsyncMock())

        await game_open.add_join_reaction(public_message)

        public_message.add_reaction.assert_awaited_once_with(
            game_open.settings.emoji_join_game,
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

    async def test_join_reaction_failure_gets_public_reconciliation_with_game_id(self):
        result = game_open_workers.OpenGameResult(
            game_id=78,
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
        public_message = SimpleNamespace(
            add_reaction=mock.AsyncMock(
                side_effect=RuntimeError('reaction unavailable')
            )
        )
        send = mock.AsyncMock(side_effect=[public_message, None])

        with self.assertLogs(game_open.logger.name, level='ERROR') as logs:
            await game_open.publish_open_game_result(
                result,
                prefix='$',
                send=send,
                add_completion_reaction=public_message.add_reaction,
            )

        self.assertEqual(send.await_count, 2)
        self.assertIn('78', send.await_args_list[1].args[0])
        self.assertIn('78', '\n'.join(logs.output))

    async def test_team_broadcast_only_runs_for_role_locked_prefix_result(self):
        send = mock.AsyncMock(return_value=SimpleNamespace())
        broadcast = mock.AsyncMock()
        role_locked = game_open_workers.OpenGameResult(
            game_id=79,
            guild_id=300,
            requester_id=100,
            host_name='Host',
            size=(1, 1),
            expiration_hours=24,
            is_ranked=True,
            is_mobile=True,
            notes_display='\u200b',
            warnings=(),
            role_locks=(
                game_open_workers.OpenGameSide(1, required_role_id=501),
                game_open_workers.OpenGameSide(1),
            ),
        )
        ordinary = replace(role_locked, game_id=80, role_locks=(
            game_open_workers.OpenGameSide(1),
            game_open_workers.OpenGameSide(1),
        ))

        await game_open.publish_open_game_result(
            role_locked,
            prefix='$',
            send=send,
            broadcast=broadcast,
        )
        broadcast.assert_awaited_once()

        broadcast.reset_mock()
        await game_open.publish_open_game_result(
            ordinary,
            prefix='$',
            send=send,
            broadcast=broadcast,
        )
        broadcast.assert_not_awaited()


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

    async def invoke_prefix_open(
        self,
        alias,
        args,
        *,
        channel_id=900,
        author_roles=(),
        role_by_id=None,
        named_role=None,
    ):
        role_by_id = role_by_id or {}
        author = SimpleNamespace(
            id=100,
            name='host',
            nick=None,
            display_name='Host',
            roles=tuple(author_roles),
        )
        guild = SimpleNamespace(
            id=300,
            get_role=lambda role_id: role_by_id.get(role_id),
        )
        context = SimpleNamespace(
            guild=guild,
            author=author,
            channel=SimpleNamespace(id=channel_id),
            prefix='$',
            invoked_with=alias,
            send=mock.AsyncMock(),
        )
        captured = []

        async def run_creation(request):
            captured.append(request)
            return game_open_workers.OpenGameResult(
                game_id=42,
                guild_id=300,
                requester_id=100,
                host_name='Host',
                size=tuple(side.size for side in request.sides),
                expiration_hours=request.expiration_hours,
                is_ranked=request.is_ranked,
                is_mobile=True,
                notes_display=request.notes_display,
                warnings=(),
                role_locks=request.sides,
                size_display=request.size_display,
            )

        def guild_setting(guild_id, name):
            return {
                'unranked_game_channel': 901,
                'steam_game_channel': 902,
            }.get(name)

        with mock.patch.object(
            matchmaking.settings,
            'guild_setting',
            side_effect=guild_setting,
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
            new=mock.AsyncMock(side_effect=run_creation),
        ), mock.patch.object(
            matchmaking.game_open,
            'publish_open_game_result',
            new=mock.AsyncMock(),
        ), mock.patch.object(
            matchmaking.utilities,
            'guild_role_by_name',
            return_value=named_role,
        ):
            await self.command.callback(
                SimpleNamespace(),
                context,
                args=args,
            )
        return captured, context

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

    async def test_prefix_aliases_share_crossplay_storage_and_parser_options(self):
        for alias in ('opengame', 'openmatch', 'open', 'opensteam'):
            with self.subTest(alias=alias):
                captured, context = await self.invoke_prefix_open(
                    alias,
                    '2vs2 12h unranked for <@!200>',
                    channel_id=902 if alias == 'opensteam' else 900,
                )
                self.assertEqual(context.send.await_count, 0)
                request = captured[0]
                self.assertEqual(
                    tuple(side.size for side in request.sides),
                    (2, 2),
                )
                self.assertEqual(request.size_display, '2vs2')
                self.assertEqual(request.expiration_hours, 12)
                self.assertFalse(request.is_ranked)
                self.assertTrue(request.is_mobile)
                self.assertEqual(request.notes, 'for <@!200>')

        ranked, _ = await self.invoke_prefix_open(
            'openmatch',
            '1v1 6h ranked note',
            channel_id=900,
        )
        unranked, _ = await self.invoke_prefix_open(
            'open',
            '1v1 6h note',
            channel_id=901,
        )
        self.assertTrue(ranked[0].is_ranked)
        self.assertFalse(unranked[0].is_ranked)

    async def test_prefix_role_mentions_and_explicit_role_positions_preserve_locks(self):
        role = SimpleNamespace(id=501, name='The Jets')
        mentioned, _ = await self.invoke_prefix_open(
            'opengame',
            '1v1 <@&501> for <@!200>',
            role_by_id={501: role},
            author_roles=(role,),
        )
        self.assertEqual(
            [
                (side.required_role_id, side.required_role_name)
                for side in mentioned[0].sides
            ],
            [(501, 'The Jets'), (None, None)],
        )
        self.assertEqual(mentioned[0].notes, '**@The Jets** for <@!200>')

        offset, _ = await self.invoke_prefix_open(
            'opengame',
            '2v2 <@&501>',
            role_by_id={501: role},
        )
        self.assertEqual(
            [side.required_role_id for side in offset[0].sides],
            [None, 501],
        )

        explicit, _ = await self.invoke_prefix_open(
            'opengame',
            '1v1 role2="The Jets"',
            named_role=role,
        )
        self.assertEqual(
            (explicit[0].sides[1].required_role_id,
             explicit[0].sides[1].required_role_name),
            (501, 'The Jets'),
        )
        self.assertIn('Side 2', explicit[0].role_lock_message)

    async def test_prefix_rejects_mixed_and_invalid_role_positions(self):
        role = SimpleNamespace(id=501, name='The Jets')
        for args, expected in (
            (
                '1v1 <@&501> role1="The Jets"',
                'both mention and explicit',
            ),
            ('1v1 role0="The Jets"', 'position of 0 is invalid'),
            ('1v1 role3="The Jets"', 'does not have that many sides'),
        ):
            with self.subTest(args=args):
                captured, context = await self.invoke_prefix_open(
                    'opengame',
                    args,
                    role_by_id={501: role},
                    named_role=role,
                )
                self.assertEqual(captured, [])
                self.assertIn(expected, context.send.await_args.args[0])

    def test_join_reaction_parser_accepts_three_digit_game_ids(self):
        cog = matchmaking.matchmaking.__new__(matchmaking.matchmaking)
        game = object()
        message = (
            'Other players can join game 322 by reacting with '
            f'{matchmaking.settings.emoji_join_game}.'
        )

        with mock.patch.object(
            matchmaking.models.Game,
            'get_or_none',
            return_value=game,
        ) as get_game:
            parsed = cog.is_joingame_message(message)

        self.assertEqual(parsed, (322, game))
        get_game.assert_called_once_with(id=322)

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


class CrossPlayJoinTests(unittest.IsolatedAsyncioTestCase):
    async def run_join_case(
        self,
        *,
        game_is_mobile,
        polytopia_name,
        name_steam,
        member_id=200,
        notes=None,
        host_polytopia_name='Host Poly',
        host_name_steam=None,
    ):
        guild = SimpleNamespace(id=300, roles=[])
        member = SimpleNamespace(
            id=member_id,
            name='joiner',
            nick=None,
            display_name='Joiner',
            mention=f'<@{member_id}>',
            guild=guild,
            roles=(),
            remove_roles=mock.AsyncMock(),
        )
        host_member = SimpleNamespace(
            discord_id=100,
            polytopia_name=host_polytopia_name,
            name_steam=host_name_steam,
        )
        host = SimpleNamespace(
            name='Host',
            discord_member=host_member,
        )
        discord_member = SimpleNamespace(
            discord_id=member_id,
            polytopia_name=polytopia_name,
            name_steam=name_steam,
            is_banned=False,
            elo_moonrise=1000,
        )
        player = SimpleNamespace(
            name='Joiner',
            discord_member=discord_member,
            is_banned=False,
            elo_moonrise=1000,
            team=None,
            save=mock.Mock(),
        )
        side = SimpleNamespace(
            position=1,
            required_role_id=None,
            sidename=None,
        )

        class FakeGame:
            id = 322
            guild_id = 300
            is_pending = True
            is_ranked = True
            is_mobile = game_is_mobile

            def capacity(self):
                return 1, 3

            def has_player(self, player):
                return False, None

            def first_open_side(self, roles):
                return side, False

            def is_hosted_by(self, discord_id):
                return False, host

            def elo_requirements(self):
                return 0, 9999, 0, 9999

            def creating_player(self):
                return host

        FakeGame.notes = notes
        fake_game = FakeGame()
        fake_game.host = host
        lineup = SimpleNamespace()

        class PlayerModel:
            @staticmethod
            def get_by_discord_id(**kwargs):
                return player, False

            @staticmethod
            def is_in_team(**kwargs):
                return False, None

        class LineupModel:
            @staticmethod
            def create(**kwargs):
                return lineup

        class GameLogModel:
            @staticmethod
            def member_string(member):
                return 'Joiner'

            @staticmethod
            def write(**kwargs):
                return None

        class Atomic:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

        class Database:
            def connection_context(self):
                return Atomic()

            def atomic(self):
                return Atomic()

        def guild_setting(guild_id, name):
            return {
                'command_prefix': '$',
                'inactive_role': None,
                'require_teams': False,
                'helper_roles': [],
                'mod_roles': [],
            }.get(name)

        with mock.patch.object(
            matchmaking.models,
            'Player',
            PlayerModel,
        ), mock.patch.object(
            matchmaking.models,
            'Lineup',
            LineupModel,
        ), mock.patch.object(
            matchmaking.models,
            'GameLog',
            GameLogModel,
        ), mock.patch.object(
            matchmaking.models,
            'db',
            Database(),
        ), mock.patch.object(
            matchmaking.models.Game,
            'search_pending',
            return_value=[],
        ), mock.patch.object(
            matchmaking.models.Game,
            'waiting_for_creator',
            return_value=[],
        ), mock.patch.object(
            matchmaking.settings,
            'guild_setting',
            side_effect=guild_setting,
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
            matchmaking.settings,
            'can_user_join_game',
            return_value=(True, None),
        ), mock.patch.object(
            matchmaking.game_join_workers.models.Game,
            'get_by_id',
            return_value=fake_game,
        ):
            result = await matchmaking.models.Game.join(
                fake_game,
                member=member,
                author_member=member,
            )
        return result, fake_game, member, player

    async def test_historical_platform_values_do_not_gate_crossplay_joiners(self):
        for game_is_mobile in (True, False):
            for names in (
                ('Mobile Joiner', None),
                (None, 'Steam Joiner'),
                ('Mobile Joiner', 'Steam Joiner'),
            ):
                with self.subTest(game_is_mobile=game_is_mobile, names=names):
                    (lineup, messages), _, _, _ = await self.run_join_case(
                        game_is_mobile=game_is_mobile,
                        polytopia_name=names[0],
                        name_steam=names[1],
                    )
                    self.assertIsNotNone(lineup)
                    self.assertIn('Host Poly', '\n'.join(messages))

    async def test_crossplay_join_rejects_only_when_both_names_are_missing(self):
        for game_is_mobile in (True, False):
            with self.subTest(game_is_mobile=game_is_mobile):
                (lineup, messages), _, _, _ = await self.run_join_case(
                    game_is_mobile=game_is_mobile,
                    polytopia_name=None,
                    name_steam=None,
                )
                self.assertIsNone(lineup)
                self.assertIn('canonical Polytopia account name', messages[0])
                self.assertNotIn('Steam game', messages[0])

    async def test_friend_guidance_uses_steam_name_when_mobile_name_is_missing(self):
        (lineup, messages), _, _, _ = await self.run_join_case(
            game_is_mobile=True,
            polytopia_name='Joiner Poly',
            name_steam=None,
            host_polytopia_name=None,
            host_name_steam='Host Steam',
        )
        self.assertIsNotNone(lineup)
        self.assertIn('Host Steam', '\n'.join(messages))
        self.assertNotIn('None', '\n'.join(messages))

    async def test_mention_restriction_is_shared_by_canonical_join_checks(self):
        allowed, _, _, _ = await self.run_join_case(
            game_is_mobile=False,
            polytopia_name=None,
            name_steam='Allowed Steam',
            member_id=200,
            notes='<@!200> <@201>',
        )
        rejected, _, _, _ = await self.run_join_case(
            game_is_mobile=True,
            polytopia_name='Other Poly',
            name_steam=None,
            member_id=202,
            notes='<@!200> <@201>',
        )
        self.assertIsNotNone(allowed[0])
        self.assertIsNone(rejected[0])
        self.assertIn('limited to specific players', rejected[1][0])


class MatchmakingReactionTests(unittest.IsolatedAsyncioTestCase):
    def make_reaction_case(self, game):
        guild = SimpleNamespace(id=300, name='Guild')
        channel = SimpleNamespace(
            id=20,
            name='bot',
            send=mock.AsyncMock(),
        )
        guild.get_channel = lambda channel_id: channel
        member = SimpleNamespace(
            id=200,
            name='joiner',
            nick=None,
            display_name='Joiner',
            mention='<@200>',
            guild=guild,
            roles=(),
        )
        guild.get_member = lambda member_id: member
        message = SimpleNamespace(
            author=SimpleNamespace(id=123),
            content=(
                'Other players can join game 322 by reacting with '
                f'{matchmaking.settings.emoji_join_game}.'
            ),
            remove_reaction=mock.AsyncMock(),
        )
        channel.fetch_message = mock.AsyncMock(return_value=message)
        payload = SimpleNamespace(
            emoji=SimpleNamespace(name=matchmaking.settings.emoji_join_game),
            user_id=200,
            message_id=10,
            channel_id=20,
            guild_id=300,
            member=member,
        )
        bot = SimpleNamespace(
            user=SimpleNamespace(id=999),
            get_guild=lambda guild_id: guild,
        )
        cog = matchmaking.matchmaking.__new__(matchmaking.matchmaking)
        cog.bot = bot
        cog.ignorable_join_reactions = set()
        return cog, payload, message, channel, member, game

    async def test_three_digit_raw_reaction_add_calls_shared_join_and_cleans_up_failure(self):
        game = SimpleNamespace(
            guild_id=300,
            id=322,
        )
        cog, payload, message, channel, member, _ = self.make_reaction_case(game)
        cog.execute_join = mock.AsyncMock(
            side_effect=matchmaking.game_join_workers.PendingGameJoinValidationError(
                'Game is limited to specific players.'
            )
        )
        with mock.patch.object(
            matchmaking.models.Game,
            'get_or_none',
            return_value=game,
        ), mock.patch.object(
            matchmaking.settings,
            'guild_setting',
            return_value='$',
        ):
            await cog.on_raw_reaction_add(payload)

        cog.execute_join.assert_awaited_once_with(
            game_id=322,
            member=member,
            author_member=member,
            side_arg=None,
            log_note='(via reaction)',
            invoked_with='reaction',
            notification_member_id=member.id,
            prefix='$',
        )
        message.remove_reaction.assert_awaited_once_with(
            payload.emoji.name,
            member,
        )
        self.assertTrue(channel.send.await_count >= 1)
        self.assertIn('no_entry_sign', channel.send.await_args.args[0])

    async def test_raw_reaction_success_reaches_same_join_path_and_clears_marker(self):
        game = SimpleNamespace(
            guild_id=300,
            id=322,
            embed=mock.Mock(return_value=(object(), '')),
        )
        cog, payload, message, channel, member, _ = self.make_reaction_case(game)
        cog.execute_join = mock.AsyncMock(return_value=matchmaking.game_join_workers.JoinResult(
            game_id=322,
            guild_id=300,
            member_id=member.id,
            side_position=1,
            messages=('Joined',),
            players=1,
            capacity=3,
            creator_id=100,
            host_id=100,
            remove_inactive_role=False,
            inactive_role_name=None,
        ))
        with mock.patch.object(
            matchmaking.models.Game,
            'get_or_none',
            return_value=game,
        ), mock.patch.object(
            matchmaking.models.Game,
            'search_pending',
            return_value=[],
        ), mock.patch.object(
            matchmaking.models.Game,
            'waiting_for_creator',
            return_value=[],
        ), mock.patch.object(
            matchmaking.models.Game,
            'load_full_game',
            return_value=game,
        ), mock.patch.object(
            matchmaking.settings,
            'guild_setting',
            return_value='$',
        ), mock.patch.object(
            matchmaking.image_storage,
            'send_game_embed',
            new=mock.AsyncMock(),
        ):
            await cog.on_raw_reaction_add(payload)

        cog.execute_join.assert_awaited_once()
        self.assertNotIn((payload.message_id, payload.user_id), cog.ignorable_join_reactions)
        message.remove_reaction.assert_not_awaited()

    async def test_raw_reaction_remove_preserves_leave_behavior_for_three_digit_game(self):
        game = SimpleNamespace(
            id=322,
            guild_id=300,
            embed=mock.Mock(return_value=(None, None)),
        )
        cog, payload, message, channel, member, _ = self.make_reaction_case(game)
        cog.execute_leave = mock.AsyncMock(return_value=matchmaking.game_join_workers.LeaveResult(
            game_id=322,
            guild_id=300,
            member_id=member.id,
            host_warning=None,
            message='Removing you from the game.',
        ))
        with mock.patch.object(
            matchmaking.models.Game,
            'get_or_none',
            return_value=game,
        ), mock.patch.object(
            matchmaking.settings,
            'guild_setting',
            return_value='$',
        ):
            await cog.on_raw_reaction_remove(payload)

        cog.execute_leave.assert_awaited_once_with(
            game_id=322,
            member=member,
            author_member=member,
            log_note='(via reaction)',
            invoked_with='reaction',
            prefix='$',
        )
        self.assertIn('Removing you from game 322.', channel.send.await_args.args[0])

    async def test_typed_and_raw_handlers_both_delegate_to_game_join(self):
        game = SimpleNamespace(
            id=322,
            guild_id=300,
            embed=mock.Mock(return_value=(None, None)),
        )
        member = SimpleNamespace(
            id=200,
            name='joiner',
            nick=None,
            display_name='Joiner',
            mention='<@200>',
            roles=(),
        )
        guild = SimpleNamespace(id=300, roles=[])
        member.guild = guild
        context = SimpleNamespace(
            author=member,
            guild=guild,
            prefix='$',
            invoked_with='join',
            message=SimpleNamespace(mentions=[], role_mentions=[]),
            send=mock.AsyncMock(),
        )
        cog = matchmaking.matchmaking.__new__(matchmaking.matchmaking)
        result = matchmaking.game_join_workers.JoinResult(
            game_id=322,
            guild_id=300,
            member_id=member.id,
            side_position=1,
            messages=('Joined',),
            players=1,
            capacity=3,
            creator_id=100,
            host_id=100,
            remove_inactive_role=False,
            inactive_role_name=None,
        )
        cog.execute_join = mock.AsyncMock(return_value=result)
        join_command = next(
            command
            for command in matchmaking.matchmaking.__cog_commands__
            if command.name == 'join'
        )
        with mock.patch.object(
            matchmaking.settings,
            'get_user_level',
            return_value=3,
        ), mock.patch.object(
            matchmaking.utilities,
            'get_guild_member',
            new=mock.AsyncMock(return_value=[member]),
        ), mock.patch.object(
            matchmaking.models.Game,
            'load_full_game',
            return_value=SimpleNamespace(
                embed=mock.Mock(return_value=(None, None)),
            ),
        ), mock.patch.object(
            matchmaking.image_storage,
            'send_game_embed',
            new=mock.AsyncMock(),
        ):
            await join_command.callback(cog, context, '322')

        reaction_cog, payload, message, channel, reaction_member, _ = (
            self.make_reaction_case(game)
        )
        reaction_cog.execute_join = mock.AsyncMock(return_value=result)
        with mock.patch.object(
            matchmaking.models.Game,
            'get_or_none',
            return_value=game,
        ), mock.patch.object(
            matchmaking.settings,
            'guild_setting',
            return_value='$',
        ), mock.patch.object(
            matchmaking.models.Game,
            'load_full_game',
            return_value=game,
        ), mock.patch.object(
            matchmaking.image_storage,
            'send_game_embed',
            new=mock.AsyncMock(),
        ):
            await reaction_cog.on_raw_reaction_add(payload)

        self.assertEqual(cog.execute_join.await_count, 1)
        self.assertEqual(reaction_cog.execute_join.await_count, 1)
        self.assertEqual(
            cog.execute_join.await_args.kwargs['author_member'],
            member,
        )
        self.assertEqual(
            reaction_cog.execute_join.await_args.kwargs['log_note'],
            '(via reaction)',
        )
