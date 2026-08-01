"""Offline coverage for the P5.3 atomic pending-game kick workflow."""

from contextlib import AbstractContextManager, ExitStack
import asyncio
import datetime
from dataclasses import FrozenInstanceError
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import discord
import peewee

from tests.test_newgame_worker import import_offline_runtime


game_join_workers = import_offline_runtime('modules.game_join_workers')
game_kick_workers = import_offline_runtime('modules.game_kick_workers')
game_join_leave = import_offline_runtime('modules.game_join_leave')
games = import_offline_runtime('modules.games')
matchmaking = import_offline_runtime('modules.matchmaking')


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
                self.expiration = database.harness.game.expiration

            def __exit__(self, exc_type, exc_value, traceback):
                if exc_type is None:
                    database.commits += 1
                    return False
                database.rollbacks += 1
                database.harness.state['lineups'] = self.lineups
                database.harness.state['logs'] = self.logs
                database.harness.game.expiration = self.expiration
                return False

        return AtomicContext()


class KickHarness:
    def __init__(self):
        self.state = {'lineups': [], 'logs': []}
        self.database = FakeDatabase(self)
        self.author_member = SimpleNamespace(
            discord_id=100,
            name='Host',
            display_name='Host',
        )
        self.target_member = SimpleNamespace(
            discord_id=200,
            name='Target',
            display_name='Target',
        )
        self.host = SimpleNamespace(
            name='Host',
            discord_member=self.author_member,
        )
        self.target = SimpleNamespace(
            name='Target',
            discord_member=self.target_member,
        )
        self.game = self._make_game()
        self.registered_ids = {100}
        self.lineup_failure = False
        self.log_failure = False
        self.save_failure = False
        self.settings_values = {'helper_roles': ['Helper']}

    def _make_game(self):
        harness = self

        class FakeGame:
            id = 322
            guild_id = 300
            is_pending = True
            host = harness.host
            expiration = datetime.datetime.now() + datetime.timedelta(hours=1)

            @property
            def lineup(self):
                return list(harness.state['lineups'])

            def is_hosted_by(self, discord_id):
                return (
                    discord_id == self.host.discord_member.discord_id,
                    self.host,
                )

            def save(self):
                if harness.save_failure:
                    raise RuntimeError('game save failure')

        return FakeGame()

    def make_lineup(self, player=None):
        harness = self
        player = player or self.target

        class Lineup:
            def __init__(self):
                self.player = player

            def delete_instance(self):
                if harness.lineup_failure:
                    raise RuntimeError('lineup deletion failure')
                harness.state['lineups'].remove(self)

        return Lineup()

    def snapshot(self, *, discord_id=100, name='Host', staff=False):
        return game_join_workers.MemberSnapshot(
            guild_id=300,
            discord_id=discord_id,
            discord_name=name,
            discord_nick=None,
            display_name=name,
            role_ids=(),
            role_names=(),
            level=5 if staff else 3,
            is_mod=False,
            is_staff=staff,
            description=f'**{name}** (`{discord_id}`)',
        )

    def request(self, *, author=None, target=None, query='Target'):
        return game_kick_workers.KickRequest(
            game_id=322,
            guild_id=300,
            prefix='$',
            author=author or self.snapshot(),
            target=target,
            target_query=query,
        )

    def patch(self):
        harness = self

        class DiscordMemberModel:
            @staticmethod
            def get_or_none(**kwargs):
                if kwargs.get('discord_id') in harness.registered_ids:
                    return harness.author_member
                return None

        class GameModel:
            @staticmethod
            def get_by_id(game_id):
                if int(game_id) != harness.game.id:
                    raise peewee.DoesNotExist()
                return harness.game

        class GameLogModel:
            @staticmethod
            def member_string(member):
                return f'**{member.display_name}** (`{member.discord_id}`)'

            @staticmethod
            def write(**kwargs):
                if harness.log_failure:
                    raise RuntimeError('audit log failure')
                harness.state['logs'].append(kwargs)

        def guild_setting(_guild_id, key):
            return harness.settings_values[key]

        stack = ExitStack()
        stack.enter_context(mock.patch.object(
            game_kick_workers.models, 'db', harness.database,
        ))
        stack.enter_context(mock.patch.object(
            game_kick_workers.models, 'DiscordMember', DiscordMemberModel,
        ))
        stack.enter_context(mock.patch.object(
            game_kick_workers.models, 'Game', GameModel,
        ))
        stack.enter_context(mock.patch.object(
            game_kick_workers.models, 'GameLog', GameLogModel,
        ))
        stack.enter_context(mock.patch.object(
            game_kick_workers.settings, 'guild_setting', guild_setting,
        ))
        return stack


class KickWorkerTests(unittest.TestCase):
    def test_success_is_atomic_and_resets_near_expiration(self):
        harness = KickHarness()
        harness.state['lineups'].append(harness.make_lineup())
        with harness.patch():
            result = game_kick_workers.kick_game(harness.request())

        self.assertEqual(harness.state['lineups'], [])
        self.assertEqual(len(harness.state['logs']), 1)
        self.assertTrue(result.expiration_reset)
        self.assertEqual(result.removal_message, 'Removing **Target** from the game.')
        self.assertIn('Host', harness.state['logs'][0]['message'])
        self.assertIn('Target', harness.state['logs'][0]['message'])
        self.assertEqual(harness.database.commits, 1)
        self.assertEqual(harness.database.rollbacks, 0)
        self.assertEqual(harness.database.connection_opened, 1)
        self.assertEqual(harness.database.connection_closed, 1)
        with self.assertRaises(FrozenInstanceError):
            result.target_name = 'other'

    def test_expiration_far_away_is_not_changed(self):
        harness = KickHarness()
        original = datetime.datetime.now() + datetime.timedelta(hours=8)
        harness.game.expiration = original
        harness.state['lineups'].append(harness.make_lineup())
        with harness.patch():
            result = game_kick_workers.kick_game(harness.request())
        self.assertFalse(result.expiration_reset)
        self.assertEqual(harness.game.expiration, original)
        self.assertIsNone(result.expiration_message)

    def test_staff_can_kick_and_non_host_is_denied(self):
        harness = KickHarness()
        harness.state['lineups'].append(harness.make_lineup())
        staff = harness.snapshot(discord_id=400, name='Staff', staff=True)
        harness.registered_ids.add(400)
        with harness.patch():
            result = game_kick_workers.kick_game(
                harness.request(author=staff),
            )
        self.assertEqual(result.target_id, 200)

        harness = KickHarness()
        harness.state['lineups'].append(harness.make_lineup())
        non_host = harness.snapshot(discord_id=400, name='Other')
        harness.registered_ids.add(400)
        with harness.patch(), self.assertRaisesRegex(
            game_kick_workers.PendingGameKickValidationError,
            'Only the game host',
        ):
            game_kick_workers.kick_game(harness.request(author=non_host))

    def test_registered_member_is_revalidated_inside_worker(self):
        harness = KickHarness()
        harness.state['lineups'].append(harness.make_lineup())
        harness.registered_ids.clear()
        with harness.patch(), self.assertRaisesRegex(
            game_kick_workers.PendingGameKickValidationError,
            'requires bot registration',
        ):
            game_kick_workers.kick_game(harness.request())
        self.assertEqual(len(harness.state['lineups']), 1)
        self.assertEqual(harness.database.commits, 0)
        self.assertEqual(harness.database.rollbacks, 1)

    def test_self_kick_unknown_and_ambiguous_targets_are_rejected(self):
        harness = KickHarness()
        self_lineup = harness.make_lineup(
            SimpleNamespace(
                name='Host',
                discord_member=harness.author_member,
            )
        )
        harness.state['lineups'].append(self_lineup)
        with harness.patch(), self.assertRaisesRegex(
            game_kick_workers.PendingGameKickValidationError,
            'Stop kicking yourself',
        ):
            game_kick_workers.kick_game(harness.request(query='Host'))

        harness = KickHarness()
        harness.state['lineups'].append(harness.make_lineup())
        with harness.patch(), self.assertRaisesRegex(
            game_kick_workers.PendingGameKickValidationError,
            'Could not find a match',
        ):
            game_kick_workers.kick_game(harness.request(query='missing'))

        harness = KickHarness()
        harness.state['lineups'].extend((
            harness.make_lineup(
                SimpleNamespace(
                    name='Target One',
                    discord_member=SimpleNamespace(
                        discord_id=201,
                        display_name='Target One',
                    ),
                )
            ),
            harness.make_lineup(
                SimpleNamespace(
                    name='Target Two',
                    discord_member=SimpleNamespace(
                        discord_id=202,
                        display_name='Target Two',
                    ),
                )
            ),
        ))
        with harness.patch(), self.assertRaisesRegex(
            game_kick_workers.PendingGameKickValidationError,
            'Could not uniquely match',
        ):
            game_kick_workers.kick_game(harness.request(query='Target'))

    def test_flexible_substring_and_mention_lookup_are_preserved(self):
        harness = KickHarness()
        harness.state['lineups'].append(harness.make_lineup())
        with harness.patch():
            result = game_kick_workers.kick_game(harness.request(query='arge'))
        self.assertEqual(result.target_id, 200)

        harness = KickHarness()
        harness.state['lineups'].append(harness.make_lineup())
        with harness.patch():
            result = game_kick_workers.kick_game(
                harness.request(query='<@!200>'),
            )
        self.assertEqual(result.target_id, 200)

    def test_guild_and_pending_state_are_revalidated_in_worker(self):
        harness = KickHarness()
        harness.state['lineups'].append(harness.make_lineup())
        request = game_kick_workers.KickRequest(
            game_id=322,
            guild_id=999,
            prefix='$',
            author=harness.snapshot(),
            target_query='Target',
        )
        with harness.patch(), self.assertRaisesRegex(
            game_kick_workers.PendingGameKickValidationError,
            'different Discord server',
        ):
            game_kick_workers.kick_game(request)

        harness = KickHarness()
        harness.game.is_pending = False
        harness.state['lineups'].append(harness.make_lineup())
        with harness.patch(), self.assertRaisesRegex(
            game_kick_workers.PendingGameKickValidationError,
            'already started',
        ):
            game_kick_workers.kick_game(harness.request())

    def test_lineup_log_and_game_save_failures_roll_back_everything(self):
        for failure in ('lineup', 'log', 'save'):
            harness = KickHarness()
            harness.state['lineups'].append(harness.make_lineup())
            original_expiration = harness.game.expiration
            if failure == 'lineup':
                harness.lineup_failure = True
            elif failure == 'log':
                harness.log_failure = True
            else:
                harness.save_failure = True
            with self.subTest(failure=failure), harness.patch(), self.assertRaises(
                RuntimeError,
            ):
                game_kick_workers.kick_game(harness.request())
            self.assertEqual(len(harness.state['lineups']), 1)
            self.assertEqual(harness.state['logs'], [])
            self.assertEqual(harness.game.expiration, original_expiration)
            self.assertEqual(harness.database.commits, 0)
            self.assertEqual(harness.database.rollbacks, 1)
            self.assertEqual(harness.database.connection_opened, 1)
            self.assertEqual(harness.database.connection_closed, 1)

    def test_request_and_result_cross_boundary_as_frozen_primitive_snapshots(self):
        harness = KickHarness()
        request = harness.request(target=harness.snapshot(discord_id=200, name='Target'))
        self.assertIsInstance(request.author, game_join_workers.MemberSnapshot)
        self.assertIsInstance(request.target, game_join_workers.MemberSnapshot)
        self.assertNotIsInstance(request.author, discord.Member)
        with self.assertRaises(FrozenInstanceError):
            request.game_id = 7


class KickCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_retains_kick_ownership_until_worker_finishes(self):
        coordinator = game_kick_workers.game_open_workers.PendingGameCoordinator()
        started = threading.Event()
        release = threading.Event()
        request = SimpleNamespace()

        def slow_kick(_request):
            started.set()
            release.wait(timeout=2)
            return 'done'

        try:
            with mock.patch.object(game_kick_workers, 'kick_game', slow_kick), \
                    mock.patch.object(
                        game_kick_workers.game_open_workers,
                        'pending_game_coordinator',
                        coordinator,
                    ):
                task = asyncio.create_task(game_kick_workers.run_kick(request))
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

    async def test_slow_kick_leaves_event_loop_heartbeat_responsive(self):
        coordinator = game_kick_workers.game_open_workers.PendingGameCoordinator()
        started = threading.Event()
        release = threading.Event()

        def slow_kick(_request):
            started.set()
            release.wait(timeout=2)
            return 'ok'

        try:
            with mock.patch.object(game_kick_workers, 'kick_game', slow_kick), \
                    mock.patch.object(
                        game_kick_workers.game_open_workers,
                        'pending_game_coordinator',
                        coordinator,
                    ):
                task = asyncio.create_task(game_kick_workers.run_kick(None))
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.001)
                heartbeat = asyncio.create_task(asyncio.sleep(0.01))
                await asyncio.wait_for(heartbeat, timeout=0.1)
                release.set()
                self.assertEqual(await task, 'ok')
        finally:
            coordinator.executor.shutdown(wait=True)

    async def test_duplicate_kicks_and_kick_leave_share_one_serialized_coordinator(self):
        harness = KickHarness()
        harness.state['lineups'].append(harness.make_lineup())
        coordinator = game_kick_workers.game_open_workers.PendingGameCoordinator()
        request = harness.request()
        try:
            with harness.patch(), mock.patch.object(
                game_kick_workers.game_open_workers,
                'pending_game_coordinator',
                coordinator,
            ):
                first, second = await asyncio.gather(
                    game_kick_workers.run_kick(request),
                    game_kick_workers.run_kick(request),
                    return_exceptions=True,
                )
            self.assertEqual(
                sum(isinstance(value, game_kick_workers.KickResult)
                    for value in (first, second)),
                1,
            )
            failures = [
                value for value in (first, second)
                if isinstance(value, Exception)
            ]
            self.assertIsInstance(
                failures[0],
                game_kick_workers.PendingGameKickValidationError,
            )
            self.assertEqual(harness.state['lineups'], [])
            self.assertEqual(coordinator.active_count, 0)
        finally:
            coordinator.executor.shutdown(wait=True)

        coordinator = game_kick_workers.game_open_workers.PendingGameCoordinator()
        active = 0
        maximum = 0
        events = []
        lock = threading.Lock()

        def operation(label, _request):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                events.append(f'{label}-start')
            time.sleep(0.01)
            with lock:
                events.append(f'{label}-end')
                active -= 1
            return label

        try:
            with mock.patch.object(
                game_kick_workers, 'kick_game',
                side_effect=lambda request: operation('kick', request),
            ), mock.patch.object(
                game_join_workers, 'leave_game',
                side_effect=lambda request: operation('leave', request),
            ), mock.patch.object(
                game_kick_workers.game_open_workers,
                'pending_game_coordinator',
                coordinator,
            ):
                kick_result, leave_result = await asyncio.gather(
                    game_kick_workers.run_kick(request),
                    game_join_workers.run_leave(
                        game_join_workers.LeaveRequest(
                            game_id=322,
                            guild_id=300,
                            prefix='$',
                            member=request.author,
                            author=request.author,
                        )
                    ),
                )
            self.assertEqual((kick_result, leave_result), ('kick', 'leave'))
            self.assertEqual(maximum, 1)
            self.assertEqual(
                {event.split('-')[0] for event in events},
                {'kick', 'leave'},
            )
        finally:
            coordinator.executor.shutdown(wait=True)


class KickAdapterTests(unittest.IsolatedAsyncioTestCase):
    def _member(self, *, member_id=100, name='Host', guild_id=300):
        guild = SimpleNamespace(id=guild_id, roles=[])
        return SimpleNamespace(
            id=member_id,
            name=name,
            nick=None,
            display_name=name,
            guild=guild,
            roles=(),
        )

    def _result(self, *, reset=True):
        return game_kick_workers.KickResult(
            game_id=322,
            guild_id=300,
            author_id=100,
            target_id=200,
            target_name='Target',
            removal_message='Removing **Target** from the game.',
            expiration_reset=reset,
        )

    async def test_post_commit_card_failure_reconciles_and_later_effects_continue(self):
        game = SimpleNamespace(
            embed=mock.Mock(return_value=(None, None)),
        )
        sender = mock.AsyncMock()
        with mock.patch.object(
            game_join_leave.models.Game,
            'load_full_game',
            return_value=game,
        ), mock.patch.object(
            game_join_leave.image_storage,
            'send_game_embed',
            new=mock.AsyncMock(side_effect=RuntimeError('card failed')),
        ):
            await game_join_leave.publish_kick_result(
                self._result(),
                send=sender,
                card_destination=SimpleNamespace(),
                guild=SimpleNamespace(id=300),
                prefix='$',
            )

        contents = [call.args[0] for call in sender.await_args_list]
        self.assertIn('game card could not be updated', contents[0])
        self.assertIn('Removing **Target**', '\n'.join(contents))
        self.assertIn('expiration has been reset', '\n'.join(contents))
        self.assertEqual(len(contents), 3)

    async def test_prefix_registration_and_failure_do_not_publish_success_effects(self):
        command = next(
            command for command in matchmaking.matchmaking.__cog_commands__
            if command.name == 'kick'
        )
        context = SimpleNamespace(
            prefix='$',
            invoked_with='kick',
            author=self._member(),
            guild=SimpleNamespace(id=300),
            send=mock.AsyncMock(),
        )
        cog = matchmaking.matchmaking.__new__(matchmaking.matchmaking)
        cog.execute_kick = mock.AsyncMock(
            side_effect=peewee.OperationalError('database failed'),
        )
        with mock.patch.object(
            matchmaking.game_join_leave,
            'publish_kick_result',
            new=mock.AsyncMock(),
        ) as publish:
            await command.callback(cog, context, '322', 'Target')
        publish.assert_not_awaited()
        self.assertIn('database operation failed', context.send.await_args.args[0])
        self.assertEqual(command.usage, 'game_id player')
        self.assertEqual(command.aliases, [])
        self.assertEqual(len(command.checks), 2)

    async def test_post_commit_send_failure_reconciles_and_keeps_later_effects(self):
        game = SimpleNamespace(
            embed=mock.Mock(return_value=(None, None)),
        )
        sender = mock.AsyncMock(
            side_effect=[
                RuntimeError('removal send failed'),
                None,
                None,
            ],
        )
        with mock.patch.object(
            game_join_leave.models.Game,
            'load_full_game',
            return_value=game,
        ), mock.patch.object(
            game_join_leave.image_storage,
            'send_game_embed',
            new=mock.AsyncMock(),
        ):
            await game_join_leave.publish_kick_result(
                self._result(),
                send=sender,
                card_destination=SimpleNamespace(),
                guild=SimpleNamespace(id=300),
                prefix='$',
            )

        self.assertIn('Removing **Target**', sender.await_args_list[0].args[0])
        self.assertIn('could not be published', sender.await_args_list[1].args[0])
        self.assertIn(
            'expiration has been reset',
            sender.await_args_list[2].args[0],
        )

    async def test_native_kick_defers_and_keeps_success_public_but_failure_ephemeral(self):
        command = (
            next(command for command in games.polygames.__cog_app_commands__
                 if command.name == 'game')
            .get_command('manage')
            .get_command('kick')
        )
        requester = self._member()
        target = self._member(member_id=200, name='Target')
        interaction = SimpleNamespace(
            guild=requester.guild,
            user=requester,
            channel_id=301,
            response=SimpleNamespace(defer=mock.AsyncMock()),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        result = self._result(reset=False)
        matchmaking_cog = SimpleNamespace(
            execute_kick=mock.AsyncMock(return_value=result),
        )
        cog = games.polygames.__new__(games.polygames)
        cog.bot = SimpleNamespace(
            get_cog=lambda name: matchmaking_cog if name == 'matchmaking' else None,
        )
        cog._native_pending_game_channel_allowed = mock.AsyncMock(
            return_value=True,
        )
        with mock.patch.object(
            games.settings,
            'guild_setting',
            return_value='$',
        ), mock.patch.object(
            games.game_join_leave,
            'publish_kick_result',
            new=mock.AsyncMock(),
        ) as publish:
            await command.callback(cog, interaction, 322, target)
        interaction.response.defer.assert_awaited_once_with()
        matchmaking_cog.execute_kick.assert_awaited_once_with(
            game_id=322,
            author_member=requester,
            target_member=target,
            invoked_with='/game manage kick',
            prefix=mock.ANY,
        )
        publish.assert_awaited_once()

        interaction = SimpleNamespace(
            guild=requester.guild,
            user=requester,
            channel_id=301,
            response=SimpleNamespace(defer=mock.AsyncMock()),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        matchmaking_cog.execute_kick = mock.AsyncMock(
            side_effect=game_kick_workers.PendingGameKickValidationError(
                'Only the game host can do this.'
            ),
        )
        with mock.patch.object(
            games.settings,
            'guild_setting',
            return_value='$',
        ), mock.patch.object(
            games.game_join_leave,
            'publish_kick_result',
            new=mock.AsyncMock(),
        ) as publish:
            await command.callback(cog, interaction, 322, target)
        interaction.response.defer.assert_awaited_once_with()
        interaction.followup.send.assert_awaited_once_with(
            'Only the game host can do this.',
            ephemeral=True,
        )
        publish.assert_not_awaited()

        interaction = SimpleNamespace(
            guild=requester.guild,
            user=requester,
            channel_id=999,
            response=SimpleNamespace(defer=mock.AsyncMock()),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        matchmaking_cog.execute_kick = mock.AsyncMock(return_value=result)
        cog._native_pending_game_channel_allowed = mock.AsyncMock(
            return_value=False,
        )
        with mock.patch.object(
            games.settings,
            'guild_setting',
            return_value='$',
        ), mock.patch.object(
            games.game_join_leave,
            'publish_kick_result',
            new=mock.AsyncMock(),
        ) as publish:
            await command.callback(cog, interaction, 322, target)
        interaction.response.defer.assert_awaited_once_with()
        matchmaking_cog.execute_kick.assert_not_awaited()
        interaction.followup.send.assert_not_awaited()
        publish.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
