"""Focused offline coverage for the P4.2e game-side attribute."""

import asyncio
from contextlib import AbstractContextManager, ExitStack
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

import discord
import peewee

from modules import exceptions
from tests.test_game_join_leave import (
    JoinHarness,
    join_request,
    snapshot,
    game_join_workers,
)
from tests.test_newgame_worker import import_offline_runtime


game_workers = import_offline_runtime('modules.game_workers')
game_side = import_offline_runtime('modules.game_side')
games = import_offline_runtime('modules.games')


class SideDatabase:
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
                self.side_name = database.harness.side_one.sidename
                self.role_id = database.harness.side_one.required_role_id
                self.logs = list(database.harness.state['logs'])

            def __exit__(self, exc_type, exc_value, traceback):
                if exc_type is None:
                    database.commits += 1
                    return False
                database.rollbacks += 1
                database.harness.side_one.sidename = self.side_name
                database.harness.side_one.required_role_id = self.role_id
                database.harness.state['side_name'] = self.side_name
                database.harness.state['role_id'] = self.role_id
                database.harness.state['logs'] = self.logs
                return False

        return AtomicContext()


class SideHarness:
    def __init__(
        self,
        *,
        side_name='Alpha',
        role_id=None,
        pending=True,
        host_id=100,
        guild_id=300,
    ):
        self.state = {
            'side_name': side_name,
            'role_id': role_id,
            'logs': [],
        }
        self.database = SideDatabase(self)
        self.side_one = SimpleNamespace(
            id=401,
            position=1,
            sidename=side_name,
            required_role_id=role_id,
            size=2,
            lineup=[],
        )
        self.side_two = SimpleNamespace(
            id=402,
            position=2,
            sidename='Bravo',
            required_role_id=None,
            size=2,
            lineup=[],
        )
        self.sides = (self.side_one, self.side_two)
        self.guild_id = guild_id
        self.host_id = host_id
        self.game = self._make_game(pending=pending)

        def save_side():
            self.state['side_name'] = self.side_one.sidename
            self.state['role_id'] = self.side_one.required_role_id

        self.side_one.save = save_side
        self.side_two.save = lambda: None

        harness = self

        class GameModel:
            @staticmethod
            def get_by_id(game_id):
                if int(game_id) != harness.game.id:
                    raise peewee.DoesNotExist()
                return harness.game

        self.game_model = GameModel

    def _make_game(self, *, pending):
        harness = self

        class FakeGame:
            id = 42
            guild_id = harness.guild_id
            is_pending = pending
            announcement_channel = 900
            announcement_message = 901
            host = SimpleNamespace(
                discord_member=SimpleNamespace(discord_id=harness.host_id),
            )

            @property
            def gamesides(self):
                return harness.sides

            def get_side(self, lookup):
                try:
                    side_num = int(lookup)
                    side_name = None
                except (TypeError, ValueError):
                    side_num = None
                    side_name = str(lookup)
                for side in harness.sides:
                    if side_num and side.position == side_num:
                        return side, True
                    if (
                        side_name
                        and side.sidename
                        and len(side_name) > 2
                        and side_name.upper() in side.sidename.upper()
                    ):
                        return side, True
                return None, False

            def is_hosted_by(self, discord_id):
                return int(discord_id) == harness.host_id, self.host

        return FakeGame()

    def write_log(self, **kwargs):
        if getattr(self, 'log_failure', None):
            raise self.log_failure
        self.state['logs'].append(kwargs)

    def patch(self):
        stack = ExitStack()
        stack.enter_context(
            mock.patch.object(game_workers.models, 'db', self.database)
        )
        stack.enter_context(
            mock.patch.object(
                game_workers.models.Game,
                'get_by_id',
                side_effect=self.game_model.get_by_id,
            )
        )
        stack.enter_context(
            mock.patch.object(
                game_workers.models.GameLog,
                'write',
                side_effect=self.write_log,
            )
        )
        return stack


def side_request(
    *,
    game_id=42,
    guild_id=300,
    requester_id=100,
    requester_is_staff=False,
    side_lookup='1',
    side_name=None,
    role_id=None,
    role_name=None,
    role_guild_id=None,
    clear=False,
    native=True,
    invoked_with='gameside',
):
    return game_workers.GameSideMutationRequest(
        game_id=game_id,
        guild_id=guild_id,
        channel_id=900,
        requester_id=requester_id,
        requester_is_staff=requester_is_staff,
        requester_description='**Host** (`100`)',
        side_lookup=side_lookup,
        side_name=side_name,
        role_id=role_id,
        role_name=role_name,
        role_guild_id=role_guild_id,
        clear=clear,
        native=native,
        invoked_with=invoked_with,
    )


class GameSideWorkerTests(unittest.TestCase):
    def test_requests_are_frozen_and_worker_safe(self):
        request = side_request(
            side_lookup='pha',
            side_name='Blue Team',
            role_id=77,
            role_name='Ronin',
            role_guild_id=300,
        )
        with self.assertRaises(FrozenInstanceError):
            request.game_id = 99
        self.assertIsInstance(request.side_lookup, str)
        self.assertIsInstance(request.requester_description, str)
        self.assertIsInstance(request.role_id, int)
        self.assertNotIn('discord', repr(request).lower())

    def test_read_returns_current_name_role_and_abbreviated_lookup(self):
        harness = SideHarness(side_name='Alpha Team', role_id=77)
        with harness.patch():
            result = game_workers.read_game_side(
                game_workers.GameSideReadRequest(
                    game_id=42,
                    guild_id=300,
                    channel_id=900,
                    requester_id=999,
                    side_lookup='pha',
                )
            )

        self.assertEqual(result.side_id, 401)
        self.assertEqual(result.position, 1)
        self.assertEqual(result.side_name, 'Alpha Team')
        self.assertEqual(result.required_role_id, 77)
        self.assertEqual(result.required_role_name, 'Alpha Team')
        self.assertTrue(result.is_pending)
        self.assertEqual(harness.database.connection_opened, 1)
        self.assertEqual(harness.database.connection_closed, 1)
        self.assertEqual(harness.database.commits, 0)

    def test_edit_role_and_name_commits_side_and_audit_together(self):
        harness = SideHarness(side_name='Old Name')
        with harness.patch():
            result = game_workers.set_game_side(
                side_request(
                    side_name='Blue Team',
                    role_id=77,
                    role_name='Ronin',
                    role_guild_id=300,
                    invoked_with='/game side',
                )
            )

        self.assertEqual(result.old_side_name, 'Old Name')
        self.assertEqual(result.side_name, 'Blue Team')
        self.assertEqual(result.required_role_id, 77)
        self.assertEqual(result.required_role_name, 'Ronin')
        self.assertEqual(harness.state['side_name'], 'Blue Team')
        self.assertEqual(harness.state['role_id'], 77)
        self.assertEqual(len(harness.state['logs']), 1)
        self.assertIn('/game side', harness.state['logs'][0]['message'])
        self.assertEqual(harness.database.commits, 1)
        self.assertEqual(harness.database.rollbacks, 0)
        self.assertEqual(harness.database.connection_closed, 1)

    def test_name_only_edit_removes_an_existing_role_restriction(self):
        harness = SideHarness(side_name='Ronin', role_id=77)
        with harness.patch():
            result = game_workers.set_game_side(
                side_request(side_name='Open Side')
            )

        self.assertEqual(result.side_name, 'Open Side')
        self.assertIsNone(result.required_role_id)
        self.assertEqual(harness.state['side_name'], 'Open Side')
        self.assertIsNone(harness.state['role_id'])

    def test_explicit_clear_removes_both_side_attributes(self):
        harness = SideHarness(side_name='Ronin', role_id=77)
        with harness.patch():
            result = game_workers.set_game_side(
                side_request(clear=True)
            )

        self.assertTrue(result.cleared)
        self.assertIsNone(result.side_name)
        self.assertIsNone(result.required_role_id)
        self.assertIsNone(harness.state['side_name'])
        self.assertIsNone(harness.state['role_id'])
        self.assertIn('cleared side 1', harness.state['logs'][0]['message'])

    def test_edit_permissions_and_state_validation_match_gameside(self):
        denied = SideHarness(host_id=100)
        with denied.patch(), self.assertRaises(
            game_workers.GameSidePermissionError
        ):
            game_workers.set_game_side(
                side_request(requester_id=999)
            )

        staff = SideHarness(host_id=100)
        with staff.patch():
            game_workers.set_game_side(
                side_request(requester_id=999, requester_is_staff=True)
            )
        self.assertEqual(staff.database.commits, 1)

        started = SideHarness(pending=False)
        with started.patch(), self.assertRaisesRegex(
            game_workers.GameSideValidationError,
            'already started',
        ):
            game_workers.set_game_side(side_request())

        other_guild = SideHarness(guild_id=301)
        with other_guild.patch(), self.assertRaisesRegex(
            game_workers.GameSideValidationError,
            'different discord server',
        ):
            game_workers.set_game_side(side_request(guild_id=300))

        wrong_role_guild = SideHarness()
        with wrong_role_guild.patch(), self.assertRaisesRegex(
            game_workers.GameSideValidationError,
            'belong to this Discord server',
        ):
            game_workers.set_game_side(
                side_request(
                    role_id=77,
                    role_name='Ronin',
                    role_guild_id=301,
                )
            )

        missing_side = SideHarness()
        with missing_side.patch(), self.assertRaises(
            game_workers.GameSideLookupError
        ):
            game_workers.set_game_side(side_request(side_lookup='not-real'))

    def test_audit_failure_rolls_back_side_and_closes_connection(self):
        harness = SideHarness(side_name='Before', role_id=77)
        harness.log_failure = peewee.OperationalError('side log failure')
        with harness.patch(), self.assertRaisesRegex(
            peewee.OperationalError,
            'side log failure',
        ):
            game_workers.set_game_side(
                side_request(side_name='After')
            )

        self.assertEqual(harness.state['side_name'], 'Before')
        self.assertEqual(harness.state['role_id'], 77)
        self.assertEqual(harness.state['logs'], [])
        self.assertEqual(harness.database.commits, 0)
        self.assertEqual(harness.database.rollbacks, 1)
        self.assertEqual(harness.database.connection_opened, 1)
        self.assertEqual(harness.database.connection_closed, 1)

    def test_join_worker_honors_the_role_lock_written_by_side_configuration(self):
        side_result = game_workers.GameSideMutationResult(
            game_id=322,
            guild_id=300,
            side_id=1,
            position=1,
            old_side_name=None,
            side_name='Ronin',
            old_required_role_id=None,
            required_role_id=77,
            required_role_name='Ronin',
            cleared=False,
            native=True,
        )
        harness = JoinHarness()
        harness.side_one.required_role_id = side_result.required_role_id
        harness.side_one.sidename = side_result.side_name
        with harness.patch(), self.assertRaises(
            game_join_workers.PendingGameJoinValidationError
        ) as raised:
            game_join_workers.join_game(
                join_request(side_arg=str(side_result.position))
            )
        self.assertIn('@Ronin', str(raised.exception))

        staff = snapshot(level=5)
        with harness.patch():
            result = game_join_workers.join_game(
                join_request(
                    member=staff,
                    author=staff,
                    side_arg=str(side_result.position),
                )
            )
        self.assertIn('Overriding restriction', '\n'.join(result.messages))


class GameSideServiceTests(unittest.IsolatedAsyncioTestCase):
    def result(self, *, clear=False, native=True):
        return game_workers.GameSideMutationResult(
            game_id=42,
            guild_id=300,
            side_id=401,
            position=1,
            old_side_name='Old',
            side_name=None if clear else 'New',
            old_required_role_id=77,
            required_role_id=None if clear else 88,
            required_role_name=None if clear else 'Jets',
            cleared=clear,
            native=native,
            announcement_channel_id=900,
            announcement_message_id=901,
        )

    async def test_same_game_claim_and_post_commit_order(self):
        events = []

        async def worker(_request):
            events.append('commit')
            return self.result()

        async def after(result):
            async def load_card(**kwargs):
                events.append('load')
                return SimpleNamespace()

            async def refresh(*args, **kwargs):
                events.append('refresh')

            await game_side.publish_mutation_result(
                result,
                send=lambda content: self.record_send(events, content),
                destination=SimpleNamespace(),
                guild=SimpleNamespace(),
                bot=SimpleNamespace(),
                prefix='$',
                requester_id=100,
                channel_id=900,
                load_card=load_card,
                refresh_announcement=refresh,
                send_card=self.send_embed(events),
            )

        with mock.patch.object(
            game_side.utilities,
            'lock_game',
            side_effect=lambda game_id: events.append(('lock', game_id)),
        ), mock.patch.object(
            game_side.utilities,
            'unlock_game',
            side_effect=lambda game_id: events.append(('unlock', game_id)),
        ), mock.patch.object(
            game_side.game_workers,
            'run_game_side_mutation',
            side_effect=worker,
        ):
            await game_side.run_side_mutation(
                side_request(),
                after_commit=after,
            )

        self.assertEqual(events[0], ('lock', 42))
        self.assertEqual(events[1], 'commit')
        self.assertEqual(events[2], ('unlock', 42))
        self.assertEqual(events[3][0], 'send')
        self.assertEqual(events[4], 'load')
        self.assertEqual(events[5], 'refresh')
        self.assertEqual(events[6], 'card')

    @staticmethod
    async def record_send(events, content):
        events.append(('send', content))

    @staticmethod
    def presentation_game(events):
        class Game:
            async def update_announcement(self, **kwargs):
                events.append('refresh')
                return True

            def embed(self, **kwargs):
                return None, None

        return Game()

    @staticmethod
    def send_embed(events):
        async def send(*args, **kwargs):
            events.append('card')

        return send

    async def test_database_failure_has_no_post_commit_callback(self):
        after = mock.AsyncMock()
        with mock.patch.object(
            game_side.utilities,
            'lock_game',
        ), mock.patch.object(
            game_side.utilities,
            'unlock_game',
        ), mock.patch.object(
            game_side.game_workers,
            'run_game_side_mutation',
            new=mock.AsyncMock(
                side_effect=peewee.OperationalError('database down')
            ),
        ):
            with self.assertRaises(peewee.OperationalError):
                await game_side.run_side_mutation(
                    side_request(),
                    after_commit=after,
                )
        after.assert_not_awaited()

    async def test_slow_write_and_read_workers_keep_event_loop_responsive(self):
        write_started = threading.Event()
        write_release = threading.Event()

        def slow_write(_request):
            write_started.set()
            write_release.wait(timeout=2)
            return self.result()

        with mock.patch.object(
            game_workers,
            'set_game_side',
            side_effect=slow_write,
        ):
            write_task = asyncio.create_task(
                game_workers.run_game_side_mutation(side_request())
            )
            for _ in range(100):
                if write_started.is_set():
                    break
                await asyncio.sleep(0.005)
            self.assertTrue(write_started.is_set())
            await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.2)
            write_release.set()
            await asyncio.sleep(0.05)
            await write_task

        read_started = threading.Event()
        read_release = threading.Event()

        def slow_read(_request):
            read_started.set()
            read_release.wait(timeout=2)
            return game_workers.GameSideReadResult(
                42, 300, 401, 1, 'New', None, None, True,
            )

        read_request = game_workers.GameSideReadRequest(
            42, 300, 900, 100, '1'
        )
        with mock.patch.object(
            game_workers,
            'read_game_side',
            side_effect=slow_read,
        ):
            read_task = asyncio.create_task(
                game_workers.run_game_side_read(read_request)
            )
            for _ in range(100):
                if read_started.is_set():
                    break
                await asyncio.sleep(0.005)
            self.assertTrue(read_started.is_set())
            await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.2)
            read_release.set()
            await asyncio.sleep(0.05)
            await read_task


class GameSideSlashTests(unittest.IsolatedAsyncioTestCase):
    def interaction(self):
        role = SimpleNamespace(
            id=77,
            name='Ronin',
            mention='<@&77>',
        )
        member = SimpleNamespace(
            id=100,
            name='Host',
            display_name='Host',
            mention='<@100>',
        )
        guild = SimpleNamespace(
            id=300,
            get_role=lambda role_id: role if role_id == 77 else None,
        )
        interaction = SimpleNamespace(
            user=member,
            guild=guild,
            channel_id=901,
            response=SimpleNamespace(
                defer=mock.AsyncMock(),
                send_message=mock.AsyncMock(),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
            delete_original_response=mock.AsyncMock(),
            channel=SimpleNamespace(
                id=901,
                send=mock.AsyncMock(),
            ),
        )
        return interaction, role

    @staticmethod
    def command():
        game_group = next(
            command
            for command in games.polygames.__cog_app_commands__
            if command.name == 'game'
        )
        return game_group.get_command('side')

    async def test_read_is_public_and_displays_name_and_role(self):
        interaction, _ = self.interaction()
        result = game_workers.GameSideReadResult(
            42, 300, 401, 1, 'Alpha Team', 77, 'Ronin', True,
        )
        with mock.patch.object(
            games.game_side,
            'run_side_read',
            new=mock.AsyncMock(return_value=result),
        ), mock.patch.object(
            games.settings,
            'guild_setting',
            return_value='$',
        ):
            await self.command().callback(
                SimpleNamespace(),
                interaction,
                42,
                'pha',
                None,
                None,
                False,
            )

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.delete_original_response.assert_awaited_once_with()
        interaction.channel.send.assert_awaited_once()
        message = interaction.channel.send.await_args.args[0]
        self.assertIn('Name: **Alpha Team**', message)
        self.assertIn('Role restriction: <@&77>', message)
        interaction.followup.send.assert_not_awaited()

    async def test_edit_uses_typed_role_and_publishes_after_commit(self):
        interaction, role = self.interaction()
        seen = {}
        result = game_workers.GameSideMutationResult(
            42, 300, 401, 1, 'Alpha', 'Blue Team', None, 77, 'Ronin',
            False, True,
        )

        async def run(request, *, after_commit):
            seen['request'] = request
            seen['events'] = ['committed']
            await after_commit(result)

        async def publish(committed, **kwargs):
            seen['events'].append('published')
            await kwargs['send'](
                game_side.native_mutation_message(
                    committed,
                    actor=kwargs['actor'],
                    guild=kwargs['guild'],
                )
            )

        with mock.patch.object(
            games.game_side,
            'run_side_mutation',
            side_effect=run,
        ), mock.patch.object(
            games.game_side,
            'publish_mutation_result',
            side_effect=publish,
        ), mock.patch.object(
            games.settings,
            'guild_setting',
            return_value='$',
        ):
            await self.command().callback(
                SimpleNamespace(),
                interaction,
                42,
                '1',
                role,
                'Blue Team',
                False,
            )

        request = seen['request']
        self.assertEqual(request.role_id, 77)
        self.assertEqual(request.role_name, 'Ronin')
        self.assertEqual(request.side_name, 'Blue Team')
        self.assertFalse(request.clear)
        self.assertTrue(request.native)
        self.assertEqual(seen['events'], ['committed', 'published'])
        interaction.delete_original_response.assert_awaited_once_with()
        interaction.channel.send.assert_awaited_once()
        self.assertIn('updated side 1', interaction.channel.send.await_args.args[0])

    async def test_clear_is_explicit_and_conflicting_inputs_are_private(self):
        interaction, _ = self.interaction()
        seen = {}

        async def run(request, *, after_commit):
            seen['request'] = request

        with mock.patch.object(
            games.game_side,
            'run_side_mutation',
            side_effect=run,
        ):
            await self.command().callback(
                SimpleNamespace(),
                interaction,
                42,
                '1',
                None,
                None,
                True,
            )

        self.assertTrue(seen['request'].clear)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.channel.send.assert_not_awaited()

        conflict, role = self.interaction()
        with mock.patch.object(
            games.game_side,
            'run_side_mutation',
            new=mock.AsyncMock(),
        ):
            await self.command().callback(
                SimpleNamespace(),
                conflict,
                42,
                '1',
                role,
                None,
                True,
            )
        conflict.response.send_message.assert_awaited_once_with(
            'Choose either a side name or role restriction, not clear.',
            ephemeral=True,
        )
        conflict.response.defer.assert_not_awaited()

    async def test_database_failure_stays_private_without_post_commit_effects(self):
        interaction, _ = self.interaction()
        with mock.patch.object(
            games.game_side,
            'run_side_mutation',
            new=mock.AsyncMock(
                side_effect=peewee.OperationalError('database down')
            ),
        ):
            await self.command().callback(
                SimpleNamespace(),
                interaction,
                42,
                '1',
                None,
                'New',
                False,
            )

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.followup.send.assert_awaited_once()
        self.assertTrue(
            interaction.followup.send.await_args.kwargs['ephemeral']
        )
        interaction.delete_original_response.assert_not_awaited()
        interaction.channel.send.assert_not_awaited()


class GameSidePresentationTests(unittest.IsolatedAsyncioTestCase):
    async def test_post_commit_warning_is_public_and_ordered_after_refresh_attempt(self):
        events = []

        async def send(content):
            events.append(('send', content))

        async def load_card(**kwargs):
            return SimpleNamespace()

        async def refresh(*args, **kwargs):
            events.append('refresh')
            raise RuntimeError('refresh failure')

        async def send_embed(*args, **kwargs):
            events.append('card')

        await game_side.publish_mutation_result(
            GameSideServiceTests().result(),
            send=send,
            destination=SimpleNamespace(),
            guild=SimpleNamespace(),
            bot=SimpleNamespace(),
            prefix='$',
            requester_id=100,
            channel_id=900,
            load_card=load_card,
            refresh_announcement=refresh,
            send_card=send_embed,
        )

        self.assertEqual(events[0][0], 'send')
        self.assertEqual(events[1], 'refresh')
        self.assertEqual(events[2][0], 'send')
        self.assertIn(':warning:', events[2][1])
        self.assertEqual(events[3], 'card')


if __name__ == '__main__':
    unittest.main()
