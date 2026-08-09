"""Behavior-first offline coverage for the P5.2 join/leave lifecycle."""

import asyncio
from contextlib import AbstractContextManager, ExitStack
from dataclasses import FrozenInstanceError
import inspect
import threading
import unittest
from unittest import mock
from types import SimpleNamespace

import peewee
import discord

from tests.test_newgame_worker import import_offline_runtime


game_join_workers = import_offline_runtime('modules.game_join_workers')
game_join_leave = import_offline_runtime('modules.game_join_leave')
games = import_offline_runtime('modules.games')
matchmaking = import_offline_runtime('modules.matchmaking')


def post_commit_game_card(*, content='card', files=()):
    return SimpleNamespace(
        snapshot=SimpleNamespace(game_id=322),
        rendered=SimpleNamespace(
            embed=discord.Embed(title='Game 322'),
            content=content,
            new_file=mock.Mock(side_effect=tuple(files) or (None,) * 20),
        ),
    )


class FakeDatabase:
    def __init__(self, harness):
        self.harness = harness
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
                return False

        return ConnectionContext()

    def atomic(self):
        database = self

        class AtomicContext(AbstractContextManager):
            def __enter__(self):
                self.lineups = list(database.harness.state['lineups'])
                self.logs = list(database.harness.state['logs'])
                self.player_team = database.harness.player.team

            def __exit__(self, exc_type, exc_value, traceback):
                if exc_type is None:
                    database.commits += 1
                    return False
                database.rollbacks += 1
                database.harness.state['lineups'] = self.lineups
                database.harness.state['logs'] = self.logs
                database.harness.player.team = self.player_team
                return False

        return AtomicContext()


class JoinHarness:
    def __init__(self, *, capacity=2):
        self.state = {
            'lineups': [],
            'logs': [],
            'waitlist': [],
            'player_saves': 0,
            'remove_lineup_failure': False,
        }
        self.database = FakeDatabase(self)
        self.side_one = SimpleNamespace(
            position=1,
            sidename='Alpha',
            required_role_id=None,
            size=capacity,
            lineup=[],
        )
        self.side_two = SimpleNamespace(
            position=2,
            sidename='Bravo',
            required_role_id=None,
            size=capacity,
            lineup=[],
        )
        self.sides = [self.side_one, self.side_two]

        host_member = SimpleNamespace(
            discord_id=100,
            name='host',
            display_name='Host',
            polytopia_name='Host Poly',
            name_steam=None,
            is_banned=False,
            elo_moonrise=1000,
        )
        self.host = SimpleNamespace(
            name='Host',
            discord_member=host_member,
            is_banned=False,
            elo_moonrise=1000,
            team=None,
        )
        self.player_member = SimpleNamespace(
            discord_id=200,
            name='joiner',
            display_name='Joiner',
            polytopia_name='Joiner Poly',
            name_steam=None,
            is_banned=False,
            elo_moonrise=1000,
        )
        self.player = SimpleNamespace(
            name='Joiner',
            discord_member=self.player_member,
            is_banned=False,
            elo_moonrise=1000,
            team=None,
        )
        self.player.save = self._save_player
        self.elo_requirements = (0, 9999, 0, 9999)
        self.game = self._make_game(capacity)

        harness = self

        class GameModel:
            @staticmethod
            def get_by_id(game_id):
                if int(game_id) != harness.game.id:
                    raise peewee.DoesNotExist()
                return harness.game

            @staticmethod
            def search_pending(**kwargs):
                return list(harness.state['waitlist'])

            @staticmethod
            def waiting_for_creator(**kwargs):
                return []

        class PlayerModel:
            @staticmethod
            def get_by_discord_id(**kwargs):
                if kwargs.get('discord_id') == harness.player_member.discord_id:
                    return harness.player, False
                return None, False

            @staticmethod
            def is_in_team(**kwargs):
                return harness.on_team, harness.detected_team

        class LineupModel:
            @staticmethod
            def create(**kwargs):
                if harness.lineup_failure:
                    raise RuntimeError('lineup failure')
                lineup = SimpleNamespace(
                    player=kwargs['player'],
                    gameside=kwargs['gameside'],
                )
                harness.state['lineups'].append(lineup)
                lineup.delete_instance = lambda: harness.state['lineups'].remove(lineup)
                return lineup

        class GameLogModel:
            @staticmethod
            def member_string(member):
                return f'**{getattr(member, "name", "Member")}**'

            @staticmethod
            def write(**kwargs):
                if harness.log_failure:
                    raise RuntimeError('log failure')
                harness.state['logs'].append(kwargs)

        self.game_model = GameModel
        self.player_model = PlayerModel
        self.lineup_model = LineupModel
        self.log_model = GameLogModel
        self.on_team = False
        self.detected_team = None
        self.lineup_failure = False
        self.player_save_failure = False
        self.log_failure = False
        self.require_teams = False
        self.join_allowed = (True, None)
        self.settings_values = {
            'command_prefix': '$',
            'inactive_role': 'Inactive',
            'require_teams': False,
        }

    def _save_player(self):
        if self.player_save_failure:
            raise RuntimeError('player refresh failure')
        self.state['player_saves'] += 1

    def _make_game(self, capacity):
        harness = self

        class FakeGame:
            id = 322
            guild_id = 300
            is_pending = True
            is_ranked = True
            is_mobile = False
            notes = None
            host = harness.host

            def capacity(self):
                return len(harness.state['lineups']), capacity * len(harness.sides)

            def has_player(self, player):
                for lineup in harness.state['lineups']:
                    if lineup.player is player:
                        return True, lineup.gameside
                return False, None

            def player(self, *, discord_id=None, **kwargs):
                for lineup in harness.state['lineups']:
                    if lineup.player.discord_member.discord_id == int(discord_id):
                        return lineup
                return None

            def first_open_side(self, roles):
                role_locked = any(
                    side.required_role_id is not None for side in harness.sides
                )
                for side in harness.sides:
                    if (
                        side.required_role_id in roles
                        and len([l for l in harness.state['lineups'] if l.gameside is side])
                        < side.size
                    ):
                        return side, True
                for side in harness.sides:
                    if (
                        side.required_role_id is None
                        and len([l for l in harness.state['lineups'] if l.gameside is side])
                        < side.size
                    ):
                        return side, role_locked
                return None, role_locked

            def get_side(self, lookup):
                try:
                    position = int(lookup)
                except (TypeError, ValueError):
                    position = None
                for side in harness.sides:
                    if position and side.position == position:
                        open_ = len([
                            l for l in harness.state['lineups']
                            if l.gameside is side
                        ]) < side.size
                        return side, open_
                    if (
                        position is None
                        and side.sidename
                        and len(str(lookup)) > 2
                        and str(lookup).upper() in side.sidename.upper()
                    ):
                        open_ = len([
                            l for l in harness.state['lineups']
                            if l.gameside is side
                        ]) < side.size
                        return side, open_
                return None, False

            def is_hosted_by(self, discord_id):
                return int(discord_id) == 100, harness.host

            def elo_requirements(self):
                return harness.elo_requirements

            def creating_player(self):
                return harness.host

        return FakeGame()

    def patch(self):
        harness = self

        def guild_setting(guild_id, name):
            if name == 'require_teams':
                return harness.require_teams
            return harness.settings_values.get(name)

        stack = ExitStack()
        stack.enter_context(mock.patch.object(game_join_workers.models, 'db', self.database))
        stack.enter_context(mock.patch.object(game_join_workers.models, 'Game', self.game_model))
        stack.enter_context(mock.patch.object(game_join_workers.models, 'Player', self.player_model))
        stack.enter_context(mock.patch.object(game_join_workers.models, 'Lineup', self.lineup_model))
        stack.enter_context(mock.patch.object(game_join_workers.models, 'GameLog', self.log_model))
        stack.enter_context(mock.patch.object(game_join_workers.settings, 'guild_setting', side_effect=guild_setting))
        stack.enter_context(mock.patch.object(game_join_workers.settings, 'can_user_join_game', side_effect=lambda **kwargs: harness.join_allowed))
        return stack


def snapshot(
    discord_id=200,
    *,
    guild_id=300,
    level=3,
    role_ids=(),
    role_names=(),
    inactive=False,
    is_mod=False,
    name='joiner',
):
    return game_join_workers.MemberSnapshot(
        guild_id=guild_id,
        discord_id=discord_id,
        discord_name=name,
        discord_nick=None,
        display_name=name.title(),
        role_ids=tuple(role_ids),
        role_names=tuple(role_names),
        level=level,
        is_mod=is_mod,
        is_staff=is_mod,
        description=f'**{name.title()}** (`{discord_id}`)',
        inactive_role_name='Inactive',
        inactive_role_present=inactive,
    )


def join_request(*, member=None, author=None, side_arg=None, **kwargs):
    member = member or snapshot()
    author = author or member
    return game_join_workers.JoinRequest(
        game_id=322,
        guild_id=300,
        prefix='$',
        member=member,
        author=author,
        side_arg=side_arg,
        notification_member_id=member.discord_id,
        **kwargs,
    )


class JoinWorkerTests(unittest.TestCase):
    def test_requests_and_results_are_frozen_primitive_snapshots(self):
        request = join_request(side_arg='Alpha')
        with self.assertRaises(FrozenInstanceError):
            request.game_id = 999
        self.assertIsInstance(request.member.role_ids, tuple)
        self.assertIsInstance(request.side_arg, str)
        self.assertNotIn('discord', repr(request).lower().split('member=')[0])

        side_request = game_join_workers.PrefixSideTokenRequest(322, 300, 'Alpha')
        with self.assertRaises(FrozenInstanceError):
            side_request.token = 'Bravo'

    def test_prefix_side_token_worker_is_guild_scoped_and_connection_owned(self):
        harness = JoinHarness()
        request = game_join_workers.PrefixSideTokenRequest(322, 300, 'Bravo')
        with harness.patch():
            matched = game_join_workers.load_prefix_side_token(request)
        self.assertTrue(matched.matches_side)
        self.assertEqual(matched.token, 'Bravo')
        self.assertEqual(harness.database.connection_opened, 1)
        self.assertEqual(harness.database.connection_closed, 1)
        self.assertEqual(harness.database.commits, 0)

        harness = JoinHarness()
        with harness.patch():
            wrong_guild = game_join_workers.load_prefix_side_token(
                game_join_workers.PrefixSideTokenRequest(322, 301, 'Bravo')
            )
            missing = game_join_workers.load_prefix_side_token(
                game_join_workers.PrefixSideTokenRequest(999, 300, 'Bravo')
            )
        self.assertFalse(wrong_guild.matches_side)
        self.assertFalse(missing.matches_side)

    def test_crossplay_accepts_either_legacy_name_and_ignores_historical_flag(self):
        for game_is_mobile in (True, False):
            for polytopia_name, name_steam in (
                ('Joiner Poly', None),
                (None, 'Joiner Steam'),
                ('Joiner Poly', 'Joiner Steam'),
            ):
                with self.subTest(game_is_mobile=game_is_mobile, names=(polytopia_name, name_steam)):
                    harness = JoinHarness()
                    harness.game.is_mobile = game_is_mobile
                    harness.player_member.polytopia_name = polytopia_name
                    harness.player_member.name_steam = name_steam
                    with harness.patch():
                        result = game_join_workers.join_game(join_request())
                    self.assertEqual(result.side_position, 1)
                    self.assertEqual(len(harness.state['lineups']), 1)

    def test_rejects_only_when_both_legacy_names_are_missing(self):
        harness = JoinHarness()
        harness.player_member.polytopia_name = None
        harness.player_member.name_steam = None
        with harness.patch(), self.assertRaises(
            game_join_workers.PendingGameJoinValidationError
        ) as raised:
            game_join_workers.join_game(join_request())
        self.assertIn('canonical Polytopia account name', str(raised.exception))
        self.assertNotIn('Steam game', str(raised.exception))
        self.assertEqual(harness.state['lineups'], [])
        self.assertEqual(harness.database.rollbacks, 1)

    def test_worker_rejects_third_party_join_below_level_four_before_mutation(self):
        harness = JoinHarness()
        low_level_author = snapshot(discord_id=100, level=3, name='host')
        with harness.patch(), self.assertRaises(
            game_join_workers.PendingGameJoinValidationError
        ) as raised:
            game_join_workers.join_game(
                join_request(author=low_level_author)
            )
        self.assertIn('permissions to add another person', str(raised.exception))
        self.assertEqual(harness.state['lineups'], [])
        self.assertEqual(harness.state['logs'], [])
        self.assertEqual(harness.state['player_saves'], 0)
        self.assertEqual(harness.database.connection_opened, 0)

    def test_worker_allows_third_party_join_at_level_four(self):
        harness = JoinHarness()
        level_four_author = snapshot(discord_id=100, level=4, name='host')
        with harness.patch():
            result = game_join_workers.join_game(
                join_request(author=level_four_author)
            )
        self.assertEqual(result.member_id, 200)
        self.assertEqual(len(harness.state['lineups']), 1)
        self.assertEqual(len(harness.state['logs']), 1)

    def test_named_and_numeric_sides_are_revalidated_in_worker(self):
        harness = JoinHarness()
        with harness.patch():
            named = game_join_workers.join_game(join_request(side_arg='Bravo'))
        self.assertEqual(named.side_position, 2)

        harness = JoinHarness()
        with harness.patch():
            numeric = game_join_workers.join_game(join_request(side_arg='2'))
        self.assertEqual(numeric.side_position, 2)

    def test_role_lock_and_staff_override_are_preserved(self):
        harness = JoinHarness()
        harness.side_one.required_role_id = 77
        harness.side_one.sidename = 'Ronin'
        harness.side_two.required_role_id = 88
        harness.side_two.sidename = 'Jets'
        with harness.patch(), self.assertRaises(
            game_join_workers.PendingGameJoinValidationError
        ) as raised:
            game_join_workers.join_game(join_request())
        self.assertIn('limited to specific roles', str(raised.exception))

        harness = JoinHarness()
        harness.side_one.required_role_id = 77
        harness.side_one.sidename = 'Ronin'
        staff = snapshot(level=5, is_mod=False)
        with harness.patch():
            result = game_join_workers.join_game(
                join_request(member=staff, author=staff, side_arg='1')
            )
        self.assertEqual(result.side_position, 1)
        self.assertIn('Overriding restriction', '\n'.join(result.messages))

    def test_notes_and_team_requirements_are_authoritative(self):
        harness = JoinHarness()
        harness.game.notes = '<@!201> <@202> <@203>'
        with harness.patch(), self.assertRaises(
            game_join_workers.PendingGameJoinValidationError
        ) as raised:
            game_join_workers.join_game(join_request())
        self.assertIn('limited to specific players', str(raised.exception))

        harness = JoinHarness()
        harness.require_teams = True
        with harness.patch(), self.assertRaises(
            game_join_workers.PendingGameJoinValidationError
        ) as raised:
            game_join_workers.join_game(join_request())
        self.assertIn('must join a Team', str(raised.exception))

    def test_ban_and_elo_rules_allow_only_the_documented_overrides(self):
        harness = JoinHarness()
        harness.player.is_banned = True
        with harness.patch(), self.assertRaises(
            game_join_workers.PendingGameJoinValidationError
        ):
            game_join_workers.join_game(join_request())

        harness = JoinHarness()
        harness.player.is_banned = True
        moderator = snapshot(level=6, is_mod=True)
        with harness.patch():
            result = game_join_workers.join_game(
                join_request(author=moderator)
            )
        self.assertIn('moderator over-ride', '\n'.join(result.messages))

        harness = JoinHarness()
        harness.elo_requirements = (1100, 1200, 0, 9999)
        with harness.patch(), self.assertRaises(
            game_join_workers.PendingGameJoinValidationError
        ):
            game_join_workers.join_game(join_request())

        harness = JoinHarness()
        harness.elo_requirements = (1100, 1200, 0, 9999)
        host_author = snapshot(discord_id=100, level=4, name='host')
        with harness.patch():
            result = game_join_workers.join_game(
                join_request(author=host_author)
            )
        self.assertIn('Bypassing because you are game host', '\n'.join(result.messages))

    def test_duplicate_full_started_and_backlog_rules_fail_without_mutation(self):
        harness = JoinHarness(capacity=1)
        existing = SimpleNamespace(player=harness.player, gameside=harness.side_one)
        harness.state['lineups'].append(existing)
        with harness.patch(), self.assertRaises(
            game_join_workers.PendingGameJoinValidationError
        ) as raised:
            game_join_workers.join_game(join_request())
        self.assertIn('already in game', str(raised.exception))

        harness = JoinHarness(capacity=1)
        other = SimpleNamespace(
            player=SimpleNamespace(
                discord_member=SimpleNamespace(discord_id=201),
            ),
            gameside=harness.side_one,
        )
        harness.state['lineups'].append(other)
        second_side = SimpleNamespace(
            player=SimpleNamespace(
                discord_member=SimpleNamespace(discord_id=202),
            ),
            gameside=harness.side_two,
        )
        harness.state['lineups'].append(second_side)
        with harness.patch(), self.assertRaises(
            game_join_workers.PendingGameJoinValidationError
        ) as raised:
            game_join_workers.join_game(join_request())
        self.assertIn('completely full', str(raised.exception))

        harness = JoinHarness()
        harness.game.is_pending = False
        with harness.patch(), self.assertRaises(
            game_join_workers.PendingGameJoinValidationError
        ) as raised:
            game_join_workers.join_game(join_request())
        self.assertIn('already started', str(raised.exception))

        harness = JoinHarness()
        harness.state['waitlist'] = [SimpleNamespace(id=1), SimpleNamespace(id=2), SimpleNamespace(id=3)]
        with harness.patch(), self.assertRaises(
            game_join_workers.PendingGameJoinValidationError
        ) as raised:
            game_join_workers.join_game(join_request(member=snapshot(level=1)))
        self.assertIn('waiting to start', str(raised.exception))

    def test_inactive_role_is_only_a_post_commit_effect(self):
        harness = JoinHarness()
        request = join_request(member=snapshot(inactive=True))
        with harness.patch():
            result = game_join_workers.join_game(request)
        self.assertTrue(result.remove_inactive_role)
        self.assertEqual(harness.database.commits, 1)

    def test_lineup_player_refresh_and_log_failures_roll_back_as_one_unit(self):
        for failure in ('lineup', 'player', 'log'):
            harness = JoinHarness()
            if failure == 'lineup':
                harness.lineup_failure = True
            elif failure == 'player':
                harness.player_save_failure = True
            else:
                harness.log_failure = True
            with self.subTest(failure=failure), harness.patch(), self.assertRaises(RuntimeError):
                game_join_workers.join_game(join_request())
            self.assertEqual(harness.state['lineups'], [])
            self.assertEqual(harness.state['logs'], [])
            self.assertEqual(harness.database.commits, 0)
            self.assertEqual(harness.database.rollbacks, 1)
            self.assertEqual(harness.database.connection_opened, 1)
            self.assertEqual(harness.database.connection_closed, 1)

    def test_leave_is_atomic_and_preserves_host_warning_and_validations(self):
        harness = JoinHarness()
        lineup = SimpleNamespace(player=harness.player, gameside=harness.side_one)
        harness.state['lineups'].append(lineup)
        lineup.delete_instance = lambda: harness.state['lineups'].remove(lineup)
        request = game_join_workers.LeaveRequest(
            game_id=322,
            guild_id=300,
            prefix='$',
            member=snapshot(),
            author=snapshot(),
            log_note='(via reaction)',
        )
        with harness.patch():
            result = game_join_workers.leave_game(request)
        self.assertEqual(harness.state['lineups'], [])
        self.assertEqual(result.message, 'Removing you from the game.')
        self.assertIsNone(result.host_warning)
        self.assertEqual(harness.database.commits, 1)

        harness = JoinHarness()
        host_lineup = SimpleNamespace(player=harness.host, gameside=harness.side_one)
        harness.state['lineups'].append(host_lineup)
        host_lineup.delete_instance = lambda: harness.state['lineups'].remove(host_lineup)
        host_request = game_join_workers.LeaveRequest(
            game_id=322,
            guild_id=300,
            prefix='$',
            member=snapshot(discord_id=100, name='host', level=4),
            author=snapshot(discord_id=100, name='host', level=4),
        )
        with harness.patch():
            result = game_join_workers.leave_game(host_request)
        self.assertIn('You are leaving your own game', result.host_warning)
        self.assertIn('$delete 322', result.host_warning)

        harness = JoinHarness()
        reaction_host_lineup = SimpleNamespace(
            player=harness.host,
            gameside=harness.side_one,
        )
        harness.state['lineups'].append(reaction_host_lineup)
        reaction_host_lineup.delete_instance = lambda: harness.state['lineups'].remove(
            reaction_host_lineup
        )
        reaction_host_request = game_join_workers.LeaveRequest(
            game_id=322,
            guild_id=300,
            prefix='$',
            member=snapshot(discord_id=100, name='host', level=4),
            author=snapshot(discord_id=100, name='host', level=4),
            invoked_with='reaction',
        )
        with harness.patch():
            result = game_join_workers.leave_game(reaction_host_request)
        self.assertIn('`delete` command', result.host_warning)
        self.assertNotIn('$delete 322', result.host_warning)
        self.assertEqual(result.message, 'Removing you from game 322.')

        harness = JoinHarness()
        with harness.patch(), self.assertRaises(
            game_join_workers.PendingGameLeaveValidationError
        ) as raised:
            game_join_workers.leave_game(request)
        self.assertIn('not a member', str(raised.exception))


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_generic_worker_cancellation_retains_ownership_until_finish(self):
        coordinator = game_join_workers.game_open_workers.PendingGameCoordinator()
        started = threading.Event()
        release = threading.Event()

        def slow_worker():
            started.set()
            release.wait(timeout=2)
            return 'done'

        task = asyncio.create_task(coordinator.run_worker(slow_worker))
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.001)
        self.assertTrue(started.is_set())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(coordinator.active_count, 1)
        release.set()
        for _ in range(100):
            if coordinator.active_count == 0:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(coordinator.active_count, 0)
        coordinator.executor.shutdown(wait=True)

    async def test_slow_worker_does_not_block_heartbeat(self):
        coordinator = game_join_workers.game_open_workers.PendingGameCoordinator()
        started = threading.Event()
        release = threading.Event()

        def slow_worker():
            started.set()
            release.wait(timeout=2)
            return 'ok'

        task = asyncio.create_task(coordinator.run_worker(slow_worker))
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.001)
        self.assertTrue(started.is_set())
        heartbeat = asyncio.create_task(asyncio.sleep(0.01))
        await asyncio.wait_for(heartbeat, timeout=0.1)
        release.set()
        self.assertEqual(await task, 'ok')
        coordinator.executor.shutdown(wait=True)

    async def test_repeated_cancellation_never_leaks_coordinator_ownership(self):
        coordinator = game_join_workers.game_open_workers.PendingGameCoordinator()
        try:
            for _ in range(3):
                started = threading.Event()
                release = threading.Event()

                def slow_worker():
                    started.set()
                    release.wait(timeout=2)
                    return 'cancelled-after-finish'

                task = asyncio.create_task(coordinator.run_worker(slow_worker))
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.001)
                self.assertTrue(started.is_set())
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
        finally:
            coordinator.executor.shutdown(wait=True)

    async def test_serialized_application_service_prevents_duplicate_or_over_capacity_join(self):
        harness = JoinHarness(capacity=1)
        coordinator = game_join_workers.game_open_workers.PendingGameCoordinator()
        request_one = join_request(member=snapshot(discord_id=200))
        harness.player_member.discord_id = 200
        request_two = join_request(member=snapshot(discord_id=200))
        with harness.patch(), mock.patch.object(
            game_join_workers.game_open_workers,
            'pending_game_coordinator',
            coordinator,
        ):
            first, second = await asyncio.gather(
                game_join_workers.run_join(request_one),
                game_join_workers.run_join(request_two),
                return_exceptions=True,
            )
        self.assertEqual(len(harness.state['lineups']), 1)
        self.assertTrue(
            isinstance(first, game_join_workers.JoinResult)
            or isinstance(second, game_join_workers.JoinResult)
        )
        failure = second if isinstance(first, game_join_workers.JoinResult) else first
        self.assertIsInstance(
            failure,
            game_join_workers.PendingGameJoinValidationError,
        )
        coordinator.executor.shutdown(wait=True)

    async def test_join_and_leave_are_deterministically_serialized(self):
        harness = JoinHarness(capacity=1)
        coordinator = game_join_workers.game_open_workers.PendingGameCoordinator()
        joiner = snapshot()
        leave_request = game_join_workers.LeaveRequest(
            game_id=322,
            guild_id=300,
            prefix='$',
            member=joiner,
            author=joiner,
        )
        with harness.patch(), mock.patch.object(
            game_join_workers.game_open_workers,
            'pending_game_coordinator',
            coordinator,
        ):
            joined, left = await asyncio.gather(
                game_join_workers.run_join(join_request(member=joiner)),
                game_join_workers.run_leave(leave_request),
                return_exceptions=True,
            )
        self.assertIsInstance(joined, game_join_workers.JoinResult)
        self.assertIsInstance(left, game_join_workers.LeaveResult)
        self.assertEqual(harness.state['lineups'], [])
        self.assertEqual(harness.database.commits, 2)
        coordinator.executor.shutdown(wait=True)


class PostCommitGameCardServiceTests(unittest.IsolatedAsyncioTestCase):
    def test_direct_join_presenters_have_no_live_game_reload(self):
        prefix_join = next(
            command
            for command in matchmaking.matchmaking.__cog_commands__
            if command.name == 'join'
        )
        for callback in (
            games.polygames._publish_native_join_result,
            prefix_join.callback,
            matchmaking.matchmaking.on_raw_reaction_add,
        ):
            source = inspect.getsource(callback)
            self.assertNotIn('Game.load_full_game', source)
            self.assertIn('load_post_commit_game_card', source)

    async def test_loader_uses_primitive_source_guild_request_and_renderer(self):
        guild = SimpleNamespace(id=300)
        bot = SimpleNamespace()
        loaded_snapshot = SimpleNamespace(game_id=322)
        display = object()
        rendered = SimpleNamespace(
            embed=discord.Embed(title='Game 322'),
            content='card',
            new_file=mock.Mock(return_value=None),
        )
        with mock.patch.object(
            game_join_leave.game_detail_workers,
            'run_game_detail',
            new=mock.AsyncMock(return_value=loaded_snapshot),
        ) as run_detail, mock.patch.object(
            game_join_leave.game_detail_views,
            'resolve_display',
            return_value=display,
        ) as resolve_display, mock.patch.object(
            game_join_leave.game_detail_views,
            'render_classic_game_detail',
            return_value=rendered,
        ) as render:
            card = await game_join_leave.load_post_commit_game_card(
                game_id=322,
                guild=guild,
                bot=bot,
                prefix='!',
                presentation='prefix',
                requester_id=200,
                channel_id=20,
            )

        request = run_detail.await_args.args[0]
        self.assertEqual(request.game_id, 322)
        self.assertEqual(request.guild_id, 300)
        self.assertEqual(request.channel_id, 20)
        self.assertEqual(request.requester_discord_id, 200)
        resolve_display.assert_called_once_with(
            loaded_snapshot,
            guild=guild,
            bot=bot,
            prefix='!',
            join_emoji=game_join_leave.settings.emoji_join_game,
            presentation='prefix',
        )
        render.assert_called_once_with(display)
        self.assertIs(card.snapshot, loaded_snapshot)
        self.assertIs(card.rendered, rendered)

    async def test_each_destination_gets_a_fresh_attachment(self):
        first_file = object()
        second_file = object()
        card = post_commit_game_card(files=(first_file, second_file))
        first = SimpleNamespace(send=mock.AsyncMock())
        second = SimpleNamespace(send=mock.AsyncMock())

        await game_join_leave.send_post_commit_game_card(
            first,
            card,
            content='first',
        )
        await game_join_leave.send_post_commit_game_card(
            second,
            card,
            content='second',
        )

        self.assertIs(first.send.await_args.kwargs['file'], first_file)
        self.assertIs(second.send.await_args.kwargs['file'], second_file)
        self.assertEqual(card.rendered.new_file.call_count, 2)


class PostCommitAndAdapterTests(unittest.IsolatedAsyncioTestCase):
    def discord_member(self, *, discord_id=200, name='joiner', guild=None):
        guild = guild or SimpleNamespace(id=300, roles=[])
        return SimpleNamespace(
            id=discord_id,
            name=name,
            nick=None,
            display_name=name.title(),
            mention=f'<@{discord_id}>',
            guild=guild,
            roles=(),
            remove_roles=mock.AsyncMock(),
        )

    def make_reaction_case(self, game):
        guild = SimpleNamespace(id=300, name='Guild', roles=[])
        channel = SimpleNamespace(
            id=20,
            name='bot',
            fetch_message=mock.AsyncMock(),
            send=mock.AsyncMock(),
        )
        guild.get_channel = lambda channel_id: channel
        member = self.discord_member(guild=guild)
        guild.get_member = lambda member_id: member
        message = SimpleNamespace(
            author=SimpleNamespace(id=123),
            content='Other players can join game 322 by reacting with ⚔️',
            remove_reaction=mock.AsyncMock(),
        )
        channel.fetch_message.return_value = message
        payload = SimpleNamespace(
            emoji=SimpleNamespace(name=matchmaking.settings.emoji_join_game),
            user_id=member.id,
            message_id=10,
            channel_id=channel.id,
            guild_id=guild.id,
            member=member,
        )
        cog = matchmaking.matchmaking.__new__(matchmaking.matchmaking)
        cog.bot = SimpleNamespace(
            user=SimpleNamespace(id=999),
            get_guild=lambda guild_id: guild,
        )
        cog.ignorable_join_reactions = set()
        cog.load_reaction_game = mock.AsyncMock(return_value=
            matchmaking.game_reaction_workers.ReactionGameSnapshot(
                game_id=game.id,
                exists=True,
                guild_id=game.guild_id,
                is_pending=True,
                external_server_ids=(),
            )
        )
        return cog, payload, message, channel, member, game

    async def test_inactive_role_removal_happens_after_commit_and_reconciles(self):
        guild = SimpleNamespace(
            id=300,
            roles=[SimpleNamespace(id=9, name='Inactive')],
        )
        member = self.discord_member(guild=guild)
        result = game_join_workers.JoinResult(
            game_id=322,
            guild_id=300,
            member_id=200,
            side_position=1,
            messages=(),
            players=1,
            capacity=2,
            creator_id=100,
            host_id=100,
            remove_inactive_role=True,
            inactive_role_name='Inactive',
        )
        with mock.patch.object(
            game_join_leave.discord.utils,
            'get',
            wraps=discord.utils.get,
        ):
            warning = await game_join_leave.remove_inactive_role_after_commit(
                result,
                member,
            )
        self.assertIsNone(warning)
        member.remove_roles.assert_awaited_once()

        member.remove_roles.side_effect = RuntimeError('discord failed')
        warning = await game_join_leave.remove_inactive_role_after_commit(
            result,
            member,
        )
        self.assertIn('reconcile', warning)

    async def test_prefix_alias_and_argument_paths_call_shared_service(self):
        guild = SimpleNamespace(id=300, roles=[])
        author = self.discord_member(guild=guild)
        context = SimpleNamespace(
            author=author,
            guild=guild,
            prefix='$',
            invoked_with='join',
            message=SimpleNamespace(mentions=[], role_mentions=[]),
            send=mock.AsyncMock(),
        )
        result = game_join_workers.JoinResult(
            game_id=322,
            guild_id=300,
            member_id=200,
            side_position=2,
            messages=('Joined',),
            players=1,
            capacity=2,
            creator_id=100,
            host_id=100,
            remove_inactive_role=False,
            inactive_role_name=None,
        )
        cog = matchmaking.matchmaking.__new__(matchmaking.matchmaking)
        cog.execute_join = mock.AsyncMock(return_value=result)
        command = next(
            command for command in matchmaking.matchmaking.__cog_commands__
            if command.name == 'join'
        )
        with mock.patch.object(
            matchmaking.settings,
            'get_user_level',
            return_value=4,
        ), mock.patch.object(
            matchmaking.utilities,
            'get_guild_member',
            new=mock.AsyncMock(return_value=[author]),
        ), mock.patch.object(
            matchmaking.game_join_leave,
            'load_post_commit_game_card',
            new=mock.AsyncMock(return_value=post_commit_game_card()),
        ) as load_card, mock.patch.object(
            matchmaking.game_join_leave,
            'send_post_commit_game_card',
            new=mock.AsyncMock(),
        ):
            await command.callback(cog, context, '#322', '2')
        self.assertEqual(load_card.await_args.kwargs['presentation'], 'prefix')
        cog.execute_join.assert_awaited_once()
        self.assertEqual(cog.execute_join.await_args.kwargs['game_id'], 322)
        self.assertEqual(cog.execute_join.await_args.kwargs['side_arg'], '2')
        self.assertEqual(
            next(command for command in matchmaking.matchmaking.__cog_commands__ if command.name == 'join').aliases,
            ['joingame', 'joinmatch'],
        )

        cog.execute_join.reset_mock()
        cog.load_prefix_side_token = mock.AsyncMock(
            return_value=game_join_workers.PrefixSideTokenSnapshot(
                game_id=322,
                guild_id=300,
                token='Bravo',
                matches_side=True,
            )
        )
        member_lookup = mock.AsyncMock(return_value=[author])
        with mock.patch.object(
            matchmaking.settings,
            'get_user_level',
            return_value=3,
        ), mock.patch.object(
            matchmaking.utilities,
            'get_guild_member',
            new=member_lookup,
        ), mock.patch.object(
            matchmaking.game_join_leave,
            'load_post_commit_game_card',
            new=mock.AsyncMock(return_value=post_commit_game_card()),
        ), mock.patch.object(
            matchmaking.game_join_leave,
            'send_post_commit_game_card',
            new=mock.AsyncMock(),
        ):
            await command.callback(cog, context, '322', 'Bravo')
        self.assertEqual(cog.execute_join.await_args.kwargs['side_arg'], 'Bravo')
        cog.load_prefix_side_token.assert_awaited_once_with(
            game_id=322,
            guild_id=300,
            token='Bravo',
        )
        member_lookup.assert_awaited_once_with(context, f'<@{author.id}>')

        leave_result = game_join_workers.LeaveResult(
            game_id=322,
            guild_id=300,
            member_id=author.id,
            host_warning=None,
            message='Removing you from the game.',
        )
        cog.execute_leave = mock.AsyncMock(return_value=leave_result)
        leave_command = next(
            command for command in matchmaking.matchmaking.__cog_commands__
            if command.name == 'leave'
        )
        context.invoked_with = 'leave'
        await leave_command.callback(cog, context, '#322')
        cog.execute_leave.assert_awaited_once_with(
            game_id=322,
            member=author,
            author_member=author,
            invoked_with='leave',
            prefix='$',
        )

    async def test_prefix_named_side_lookup_runs_once_and_staff_member_wins(self):
        guild = SimpleNamespace(id=300, roles=[])
        author = self.discord_member(guild=guild)
        target_member = self.discord_member(guild=guild)
        target_member.id = 201
        context = SimpleNamespace(
            author=author,
            guild=guild,
            channel=SimpleNamespace(id=301),
            prefix='$',
            invoked_with='join',
            message=SimpleNamespace(mentions=[], role_mentions=[]),
            send=mock.AsyncMock(),
        )
        result = game_join_workers.JoinResult(
            game_id=322,
            guild_id=300,
            member_id=201,
            side_position=1,
            messages=('Joined',),
            players=1,
            capacity=2,
            creator_id=100,
            host_id=100,
            remove_inactive_role=False,
            inactive_role_name=None,
        )
        cog = matchmaking.matchmaking.__new__(matchmaking.matchmaking)
        cog.execute_join = mock.AsyncMock(return_value=result)
        cog.load_prefix_side_token = mock.AsyncMock(
            return_value=game_join_workers.PrefixSideTokenSnapshot(
                322, 300, 'Bravo', True
            )
        )
        command = next(
            command for command in matchmaking.matchmaking.__cog_commands__
            if command.name == 'join'
        )
        with mock.patch.object(
            matchmaking.settings,
            'get_user_level',
            return_value=4,
        ), mock.patch.object(
            matchmaking.utilities,
            'get_guild_member',
            new=mock.AsyncMock(return_value=[target_member]),
        ), mock.patch.object(
            matchmaking.game_join_leave,
            'load_post_commit_game_card',
            new=mock.AsyncMock(return_value=post_commit_game_card()),
        ), mock.patch.object(
            matchmaking.game_join_leave,
            'send_post_commit_game_card',
            new=mock.AsyncMock(),
        ):
            await command.callback(cog, context, '322', 'Bravo')

        cog.load_prefix_side_token.assert_awaited_once()
        self.assertIs(cog.execute_join.await_args.kwargs['member'], target_member)
        self.assertIsNone(cog.execute_join.await_args.kwargs['side_arg'])

    def test_hidden_prefix_side_edit_commands_are_retired(self):
        command_names = {
            command.name
            for command in matchmaking.matchmaking.__cog_commands__
        }
        aliases = {
            alias
            for command in matchmaking.matchmaking.__cog_commands__
            for alias in command.aliases
        }
        self.assertNotIn('gameside', command_names)
        self.assertTrue({'matchside', 'sidename'}.isdisjoint(aliases))
        self.assertFalse(hasattr(matchmaking, 'PolyMatch'))

    async def test_native_join_defers_and_keeps_success_public_but_failures_ephemeral(self):
        guild = SimpleNamespace(id=300, roles=[])
        requester = self.discord_member(guild=guild)
        interaction = SimpleNamespace(
            guild=guild,
            user=requester,
            channel_id=301,
            response=SimpleNamespace(defer=mock.AsyncMock()),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        result = game_join_workers.JoinResult(
            game_id=322,
            guild_id=300,
            member_id=200,
            side_position=1,
            messages=('Joining <@200>',),
            players=1,
            capacity=2,
            creator_id=100,
            host_id=100,
            remove_inactive_role=False,
            inactive_role_name=None,
        )
        execute_join = mock.AsyncMock(return_value=result)
        bot = SimpleNamespace(
            get_cog=lambda name: SimpleNamespace(execute_join=execute_join)
        )
        cog = games.polygames.__new__(games.polygames)
        cog.bot = bot
        command = next(
            command for command in games.polygames.__cog_app_commands__
            if command.name == 'game'
        ).get_command('join')
        with mock.patch.object(
            games.settings,
            'guild_setting',
            side_effect=lambda guild_id, name: (
                '$' if name == 'command_prefix' else None
            ),
        ), mock.patch.object(
            games.settings,
            'is_mod',
            return_value=False,
        ), mock.patch.object(
            games.settings,
            'get_user_level',
            return_value=3,
        ), mock.patch.object(
            games.game_join_leave,
            'remove_inactive_role_after_commit',
            new=mock.AsyncMock(return_value=None),
        ), mock.patch.object(
            games.game_join_leave,
            'load_post_commit_game_card',
            new=mock.AsyncMock(return_value=post_commit_game_card()),
        ) as load_card, mock.patch.object(
            games.game_join_leave,
            'send_post_commit_game_card',
            new=mock.AsyncMock(),
        ):
            await command.callback(cog, interaction, 322, None, None)
        interaction.response.defer.assert_awaited_once_with()
        self.assertTrue(
            all(
                kwargs.get('ephemeral') is False
                for _, kwargs in interaction.followup.send.await_args_list
            )
        )
        execute_join.assert_awaited_once()
        self.assertEqual(load_card.await_args.kwargs['presentation'], 'slash')

        interaction = SimpleNamespace(
            guild=guild,
            user=requester,
            channel_id=301,
            response=SimpleNamespace(defer=mock.AsyncMock()),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        execute_join = mock.AsyncMock(
            side_effect=game_join_workers.PendingGameJoinValidationError(
                'already in game'
            )
        )
        cog.bot = SimpleNamespace(
            get_cog=lambda name: SimpleNamespace(execute_join=execute_join)
        )
        with mock.patch.object(
            games.settings,
            'guild_setting',
            side_effect=lambda guild_id, name: (
                '$' if name == 'command_prefix' else None
            ),
        ), mock.patch.object(
            games.settings,
            'is_mod',
            return_value=False,
        ):
            await command.callback(cog, interaction, 322, None, None)
        interaction.response.defer.assert_awaited_once_with()
        self.assertTrue(
            interaction.followup.send.await_args.kwargs['ephemeral']
        )

    async def test_prefix_join_send_failure_warns_and_continues_reconciliation(self):
        guild = SimpleNamespace(id=300, roles=[])
        author = self.discord_member(guild=guild)
        sent = []

        async def send(content):
            sent.append(content)
            if len(sent) == 1:
                raise RuntimeError('full notice failed')

        context = SimpleNamespace(
            author=author,
            guild=guild,
            prefix='$',
            invoked_with='join',
            message=SimpleNamespace(mentions=[], role_mentions=[]),
            send=mock.AsyncMock(side_effect=send),
        )
        result = game_join_workers.JoinResult(
            game_id=322,
            guild_id=300,
            member_id=author.id,
            side_position=1,
            messages=('Joined',),
            players=1,
            capacity=1,
            creator_id=100,
            host_id=101,
            remove_inactive_role=False,
            inactive_role_name=None,
        )
        cog = matchmaking.matchmaking.__new__(matchmaking.matchmaking)
        cog.execute_join = mock.AsyncMock(return_value=result)
        command = next(
            command for command in matchmaking.matchmaking.__cog_commands__
            if command.name == 'join'
        )
        with mock.patch.object(
            matchmaking.settings,
            'get_user_level',
            return_value=4,
        ), mock.patch.object(
            matchmaking.utilities,
            'get_guild_member',
            new=mock.AsyncMock(return_value=[author]),
        ), mock.patch.object(
            matchmaking.game_join_leave,
            'load_post_commit_game_card',
            new=mock.AsyncMock(return_value=post_commit_game_card()),
        ), mock.patch.object(
            matchmaking.game_join_leave,
            'send_post_commit_game_card',
            new=mock.AsyncMock(),
        ):
            await command.callback(cog, context, '322')

        self.assertIn('public Discord state', sent[1])
        self.assertTrue(any('Matchmaking host' in message for message in sent))
        self.assertTrue(any(message == 'Joined' for message in sent))

    async def test_native_join_send_failure_warns_and_continues_reconciliation(self):
        guild = SimpleNamespace(id=300, roles=[])
        member = self.discord_member(guild=guild)
        sent = []

        async def send(content, **kwargs):
            sent.append((content, kwargs))
            if len(sent) == 1:
                raise RuntimeError('full notice failed')

        interaction = SimpleNamespace(
            guild=guild,
            followup=SimpleNamespace(send=mock.AsyncMock(side_effect=send)),
        )
        result = game_join_workers.JoinResult(
            game_id=322,
            guild_id=300,
            member_id=member.id,
            side_position=1,
            messages=('Joined',),
            players=1,
            capacity=1,
            creator_id=100,
            host_id=101,
            remove_inactive_role=False,
            inactive_role_name=None,
        )
        cog = games.polygames.__new__(games.polygames)
        with mock.patch.object(
            games.game_join_leave,
            'remove_inactive_role_after_commit',
            new=mock.AsyncMock(return_value=None),
        ), mock.patch.object(
            games.game_join_leave,
            'load_post_commit_game_card',
            new=mock.AsyncMock(return_value=post_commit_game_card()),
        ), mock.patch.object(
            games.game_join_leave,
            'send_post_commit_game_card',
            new=mock.AsyncMock(),
        ):
            await cog._publish_native_join_result(
                interaction,
                result,
                member=member,
                prefix='$',
            )

        self.assertIn('public Discord state', sent[1][0])
        self.assertTrue(any('Matchmaking host' in message for message, _ in sent))
        self.assertTrue(any(message == 'Joined' for message, _ in sent))
        self.assertTrue(all(kwargs['ephemeral'] is False for _, kwargs in sent))

    async def test_prefix_leave_send_failure_warns_and_still_publishes_output(self):
        guild = SimpleNamespace(id=300, roles=[])
        author = self.discord_member(guild=guild)
        sent = []

        async def send(content):
            sent.append(content)
            if len(sent) == 1:
                raise RuntimeError('host warning failed')

        context = SimpleNamespace(
            author=author,
            guild=guild,
            prefix='$',
            invoked_with='leave',
            send=mock.AsyncMock(side_effect=send),
        )
        result = game_join_workers.LeaveResult(
            game_id=322,
            guild_id=300,
            member_id=author.id,
            host_warning='**Warning** use `$delete 322`',
            message='Removing you from the game.',
        )
        cog = matchmaking.matchmaking.__new__(matchmaking.matchmaking)
        cog.execute_leave = mock.AsyncMock(return_value=result)
        command = next(
            command for command in matchmaking.matchmaking.__cog_commands__
            if command.name == 'leave'
        )
        await command.callback(cog, context, '322')

        self.assertIn('public Discord state', sent[1])
        self.assertIn('Removing you from the game.', sent[2])

    async def test_native_leave_send_failure_warns_and_still_publishes_output(self):
        guild = SimpleNamespace(id=300, roles=[])
        requester = self.discord_member(guild=guild)
        sent = []

        async def send(content, **kwargs):
            sent.append((content, kwargs))
            if len(sent) == 1:
                raise RuntimeError('host warning failed')

        interaction = SimpleNamespace(
            guild=guild,
            user=requester,
            channel_id=301,
            response=SimpleNamespace(defer=mock.AsyncMock()),
            followup=SimpleNamespace(send=mock.AsyncMock(side_effect=send)),
        )
        result = game_join_workers.LeaveResult(
            game_id=322,
            guild_id=300,
            member_id=requester.id,
            host_warning='**Warning** use `$delete 322`',
            message='Removing you from the game.',
        )
        execute_leave = mock.AsyncMock(return_value=result)
        cog = games.polygames.__new__(games.polygames)
        cog.bot = SimpleNamespace(
            get_cog=lambda name: SimpleNamespace(execute_leave=execute_leave)
        )
        command = next(
            command for command in games.polygames.__cog_app_commands__
            if command.name == 'game'
        ).get_command('leave')
        with mock.patch.object(
            games.settings,
            'guild_setting',
            side_effect=lambda guild_id, name: (
                '$' if name == 'command_prefix' else None
            ),
        ), mock.patch.object(
            games.settings,
            'is_mod',
            return_value=False,
        ):
            await command.callback(cog, interaction, 322)

        self.assertIn('public Discord state', sent[1][0])
        self.assertEqual(sent[2][0], 'Removing you from the game.')
        self.assertTrue(all(kwargs['ephemeral'] is False for _, kwargs in sent))

    async def test_native_leave_defers_and_keeps_success_public(self):
        guild = SimpleNamespace(id=300, roles=[])
        requester = self.discord_member(guild=guild)
        interaction = SimpleNamespace(
            guild=guild,
            user=requester,
            channel_id=301,
            response=SimpleNamespace(defer=mock.AsyncMock()),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        result = game_join_workers.LeaveResult(
            game_id=322,
            guild_id=300,
            member_id=requester.id,
            host_warning=None,
            message='Removing you from the game.',
        )
        execute_leave = mock.AsyncMock(return_value=result)
        cog = games.polygames.__new__(games.polygames)
        cog.bot = SimpleNamespace(
            get_cog=lambda name: SimpleNamespace(execute_leave=execute_leave)
        )
        command = next(
            command for command in games.polygames.__cog_app_commands__
            if command.name == 'game'
        ).get_command('leave')
        with mock.patch.object(
            games.settings,
            'guild_setting',
            side_effect=lambda guild_id, name: (
                '$' if name == 'command_prefix' else None
            ),
        ), mock.patch.object(
            games.settings,
            'is_mod',
            return_value=False,
        ):
            await command.callback(cog, interaction, 322)
        interaction.response.defer.assert_awaited_once_with()
        execute_leave.assert_awaited_once_with(
            game_id=322,
            member=requester,
            author_member=requester,
            invoked_with='/game leave',
            prefix='$',
        )
        self.assertFalse(
            interaction.followup.send.await_args.kwargs.get('ephemeral', False)
        )

    async def test_raw_reactions_use_shared_service_and_preserve_related_server_routing(self):
        source_guild = SimpleNamespace(id=400, name='External', roles=[])
        game_guild = SimpleNamespace(id=300, name='Game', roles=[])
        source_member = self.discord_member(guild=source_guild)
        game_member = self.discord_member(guild=game_guild)
        source_guild.get_channel = lambda channel_id: channel
        game_guild.get_member = lambda member_id: game_member
        channel = SimpleNamespace(
            id=20,
            name='bot',
            fetch_message=mock.AsyncMock(),
            send=mock.AsyncMock(),
        )
        message = SimpleNamespace(
            author=SimpleNamespace(id=123),
            content='Other players can join game 322 by reacting with ⚔️',
            remove_reaction=mock.AsyncMock(),
        )
        channel.fetch_message.return_value = message
        game_guild.get_channel = lambda channel_id: channel
        payload = SimpleNamespace(
            emoji=SimpleNamespace(name=matchmaking.settings.emoji_join_game),
            user_id=200,
            message_id=10,
            channel_id=20,
            guild_id=400,
            member=source_member,
        )
        result = game_join_workers.JoinResult(
            game_id=322,
            guild_id=300,
            member_id=200,
            side_position=1,
            messages=('Joined',),
            players=1,
            capacity=1,
            creator_id=100,
            host_id=100,
            remove_inactive_role=False,
            inactive_role_name=None,
        )
        cog = matchmaking.matchmaking.__new__(matchmaking.matchmaking)
        cog.bot = SimpleNamespace(
            user=SimpleNamespace(id=999),
            get_guild=lambda guild_id: game_guild,
        )
        cog.ignorable_join_reactions = set()
        cog.load_reaction_game = mock.AsyncMock(return_value=
            matchmaking.game_reaction_workers.ReactionGameSnapshot(
                game_id=322,
                exists=True,
                guild_id=300,
                is_pending=True,
                external_server_ids=(400,),
            )
        )
        game = SimpleNamespace(guild_id=300, id=322)
        execute_join = mock.AsyncMock(return_value=result)
        cog.execute_join = execute_join
        with mock.patch.object(
            matchmaking.models.Game,
            'get_or_none',
            return_value=game,
        ), mock.patch.object(
            matchmaking.models.Team,
            'related_external_severs',
            return_value=[400],
        ), mock.patch.object(
            matchmaking.settings,
            'guild_setting',
            return_value='$',
        ), mock.patch.object(
            matchmaking.game_join_leave,
            'remove_inactive_role_after_commit',
            new=mock.AsyncMock(return_value=None),
        ), mock.patch.object(
            matchmaking.game_join_leave,
            'load_post_commit_game_card',
            new=mock.AsyncMock(return_value=post_commit_game_card()),
        ) as load_card, mock.patch.object(
            matchmaking.game_join_leave,
            'send_post_commit_game_card',
            new=mock.AsyncMock(),
        ) as send_card:
            await cog.on_raw_reaction_add(payload)
        execute_join.assert_awaited_once()
        self.assertIs(execute_join.await_args.kwargs['member'], game_member)
        load_card.assert_awaited_once()
        self.assertIs(load_card.await_args.kwargs['guild'], game_guild)
        self.assertEqual(load_card.await_args.kwargs['presentation'], 'prefix')
        self.assertEqual(send_card.await_count, 2)
        self.assertIs(
            send_card.await_args_list[0].args[1],
            send_card.await_args_list[1].args[1],
        )
        message.remove_reaction.assert_not_awaited()

    async def test_raw_reaction_beta_messages_are_isolated(self):
        game = SimpleNamespace(guild_id=300, id=322)
        cog, payload, message, channel, member, _ = self.make_reaction_case(game)
        cog.execute_join = mock.AsyncMock()
        message.author.id = 479029527553638401
        with mock.patch.object(
            matchmaking.models.Game,
            'get_or_none',
            return_value=game,
        ):
            await cog.on_raw_reaction_add(payload)
        cog.execute_join.assert_not_awaited()

        cog.execute_leave = mock.AsyncMock()
        with mock.patch.object(
            matchmaking.models.Game,
            'get_or_none',
            return_value=game,
        ), mock.patch.object(
            matchmaking.settings,
            'bot_id_beta',
            479029527553638401,
        ):
            await cog.on_raw_reaction_remove(payload)
        cog.execute_leave.assert_not_awaited()

    async def test_database_failure_has_no_reaction_cleanup_and_postcommit_failure_reconciles(self):
        game = SimpleNamespace(guild_id=300, id=322)
        cog, payload, message, channel, member, _ = self.make_reaction_case(game)
        cog.execute_join = mock.AsyncMock(
            side_effect=peewee.OperationalError('database failure')
        )
        with mock.patch.object(
            matchmaking.models.Game,
            'get_or_none',
            return_value=game,
        ), mock.patch.object(
            matchmaking.settings,
            'guild_setting',
            return_value='$',
        ), mock.patch.object(
            matchmaking.game_join_leave,
            'remove_inactive_role_after_commit',
            new=mock.AsyncMock(),
        ) as remove_role:
            await cog.on_raw_reaction_add(payload)
        message.remove_reaction.assert_not_awaited()
        remove_role.assert_not_awaited()
        self.assertNotIn(
            (payload.message_id, payload.user_id),
            cog.ignorable_join_reactions,
        )

        result = game_join_workers.JoinResult(
            game_id=322,
            guild_id=300,
            member_id=member.id,
            side_position=1,
            messages=('Joined',),
            players=1,
            capacity=2,
            creator_id=100,
            host_id=100,
            remove_inactive_role=False,
            inactive_role_name=None,
        )
        cog.execute_join = mock.AsyncMock(return_value=result)
        with mock.patch.object(
            matchmaking.models.Game,
            'get_or_none',
            return_value=game,
        ), mock.patch.object(
            matchmaking.settings,
            'guild_setting',
            return_value='$',
        ), mock.patch.object(
            matchmaking.game_join_leave,
            'load_post_commit_game_card',
            new=mock.AsyncMock(return_value=post_commit_game_card()),
        ), mock.patch.object(
            matchmaking.game_join_leave,
            'send_post_commit_game_card',
            new=mock.AsyncMock(side_effect=RuntimeError('reaction update failed')),
        ):
            await cog.on_raw_reaction_add(payload)
        self.assertTrue(
            any(
                'reconcile' in call.args[0]
                for call in channel.send.await_args_list
                if call.args
            )
        )
