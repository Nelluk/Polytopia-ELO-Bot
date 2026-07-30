"""Offline tests for P4.1b pending-game extension."""

import asyncio
import datetime
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

import discord
import peewee
from discord.ext import commands

from tests.test_newgame_worker import FakeDatabase, import_offline_runtime


game_workers = import_offline_runtime('modules.game_workers')
administration = import_offline_runtime('modules.administration')


class GameExtensionWorkerTests(unittest.TestCase):
    def run_worker(self, expiration, now, log_effect=None):
        state = {'expiration': expiration, 'logs': []}
        database = FakeDatabase(state)
        game = SimpleNamespace(
            id=42,
            guild_id=300,
            is_pending=True,
            expiration=expiration,
        )

        def save():
            state['expiration'] = game.expiration

        def write_log(**kwargs):
            state['logs'].append(kwargs)
            if log_effect:
                raise log_effect

        game.save = save
        patches = (
            mock.patch.object(game_workers.models, 'db', database),
            mock.patch.object(
                game_workers.models.Game, 'get_by_id', return_value=game
            ),
            mock.patch.object(
                game_workers.models.GameLog,
                'write',
                side_effect=write_log,
            ),
        )
        with patches[0], patches[1], patches[2]:
            if log_effect:
                with self.assertRaises(type(log_effect)):
                    game_workers.extend_pending_game(
                        42, 300, 'Staff', now=now
                    )
                result = None
            else:
                result = game_workers.extend_pending_game(
                    42, 300, 'Staff', now=now
                )
        return state, database, result

    def test_future_deadline_extends_from_existing_expiration(self):
        now = datetime.datetime(2026, 7, 29, 12)
        old = now + datetime.timedelta(hours=6)
        state, database, result = self.run_worker(old, now)

        self.assertEqual(
            result.new_expiration,
            old + datetime.timedelta(hours=24),
        )
        self.assertEqual(state['expiration'], result.new_expiration)
        self.assertEqual(len(state['logs']), 1)
        self.assertEqual(database.commits, 1)
        self.assertEqual(database.connection_closed, 1)

    def test_expired_deadline_extends_from_current_time(self):
        now = datetime.datetime(2026, 7, 29, 12)
        old = now - datetime.timedelta(hours=6)
        _, _, result = self.run_worker(old, now)
        self.assertEqual(
            result.new_expiration,
            now + datetime.timedelta(hours=24),
        )

    def test_log_failure_rolls_back_expiration_and_closes_connection(self):
        now = datetime.datetime(2026, 7, 29, 12)
        old = now + datetime.timedelta(hours=6)
        state, database, _ = self.run_worker(
            old,
            now,
            peewee.OperationalError('log failure'),
        )
        self.assertEqual(state['expiration'], old)
        self.assertEqual(database.rollbacks, 1)
        self.assertEqual(database.connection_closed, 1)

    def test_worker_revalidates_pending_and_same_guild_state(self):
        now = datetime.datetime(2026, 7, 29, 12)
        for guild_id, is_pending, expected in (
            (301, True, 'different Discord server'),
            (300, False, 'no longer an open game'),
        ):
            with self.subTest(guild_id=guild_id, is_pending=is_pending):
                database = FakeDatabase({})
                game = SimpleNamespace(
                    id=42,
                    guild_id=guild_id,
                    is_pending=is_pending,
                    expiration=now,
                    save=mock.Mock(),
                )
                with mock.patch.object(
                    game_workers.models, 'db', database
                ), mock.patch.object(
                    game_workers.models.Game,
                    'get_by_id',
                    return_value=game,
                ), mock.patch.object(
                    game_workers.models.GameLog,
                    'write',
                ) as write_log:
                    with self.assertRaisesRegex(
                        game_workers.GameExtensionValidationError,
                        expected,
                    ):
                        game_workers.extend_pending_game(
                            42, 300, 'Staff', now=now
                        )

                game.save.assert_not_called()
                write_log.assert_not_called()
                self.assertEqual(database.connection_closed, 1)


class GameExtensionCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_prefix_and_typed_slash_registration(self):
        prefix = {
            command.name: command
            for command in administration.administration.__cog_commands__
        }
        self.assertIsInstance(prefix['extend'], commands.Command)
        match_group = {
            command.name: command
            for command in administration.administration.__cog_app_commands__
        }['match']
        slash = match_group.get_command('extend')
        self.assertIsNotNone(slash)
        self.assertEqual(
            [(parameter.name, parameter.type) for parameter in slash.parameters],
            [('game_id', discord.AppCommandOptionType.integer)],
        )
        self.assertNotIn(
            'extend',
            {
                command.name
                for command
                in administration.administration.__cog_app_commands__
            },
        )

    async def test_slash_rejects_non_staff_before_defer(self):
        cog = administration.administration.__new__(
            administration.administration
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=1),
            response=SimpleNamespace(
                send_message=mock.AsyncMock(),
                defer=mock.AsyncMock(),
            ),
        )
        with mock.patch.object(
            administration.settings, 'is_staff', return_value=False
        ), mock.patch.object(
            cog, '_extend_pending_game', new=mock.AsyncMock()
        ) as run_extension:
            await administration.administration.extend_slash.callback(
                cog, interaction, 42
            )

        interaction.response.send_message.assert_awaited_once()
        interaction.response.defer.assert_not_awaited()
        run_extension.assert_not_awaited()

    async def test_slash_defers_publicly_before_worker(self):
        events = []
        cog = administration.administration.__new__(
            administration.administration
        )

        async def defer(**kwargs):
            events.append(('defer', kwargs))

        async def extend(**kwargs):
            events.append(('worker', kwargs))
            return game_workers.GameExtensionResult(
                game_id=42,
                old_expiration=datetime.datetime(2026, 7, 29),
                new_expiration=datetime.datetime(2026, 7, 30),
            )

        interaction = SimpleNamespace(
            user=SimpleNamespace(id=1, display_name='Staff'),
            guild=SimpleNamespace(id=300),
            response=SimpleNamespace(
                send_message=mock.AsyncMock(),
                defer=mock.AsyncMock(side_effect=defer),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        with mock.patch.object(
            administration.settings, 'is_staff', return_value=True
        ), mock.patch.object(
            cog,
            '_extend_pending_game',
            new=mock.AsyncMock(side_effect=extend),
        ):
            await administration.administration.extend_slash.callback(
                cog, interaction, 42
            )

        self.assertEqual([event[0] for event in events], ['defer', 'worker'])
        self.assertEqual(events[0][1], {})
        self.assertNotIn(
            'ephemeral',
            interaction.followup.send.await_args.kwargs,
        )

    async def test_slow_extension_does_not_block_event_loop(self):
        started = threading.Event()
        release = threading.Event()

        def slow(*args):
            started.set()
            release.wait(timeout=2)
            now = datetime.datetime(2026, 7, 29)
            return game_workers.GameExtensionResult(
                42,
                now,
                now + datetime.timedelta(hours=24),
            )

        with mock.patch.object(
            game_workers, 'extend_pending_game', side_effect=slow
        ):
            task = asyncio.create_task(
                game_workers.run_pending_game_extension(42, 300, 'Staff')
            )
            while not started.is_set():
                await asyncio.sleep(0.005)
            await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.2)
            release.set()
            await asyncio.sleep(0.05)
            self.assertEqual((await task).game_id, 42)
