"""Offline tests for P4.1c pending-game restoration."""

import asyncio
import datetime
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

import peewee
from discord.ext import commands

from tests.test_newgame_worker import FakeDatabase, import_offline_runtime


game_workers = import_offline_runtime('modules.game_workers')
administration = import_offline_runtime('modules.administration')


class GameUnstartWorkerTests(unittest.TestCase):
    def run_worker(self, *, log_effect=None):
        now = datetime.datetime(2026, 7, 29, 12)
        state = {
            'pending': False,
            'expiration': now,
            'logs': [],
        }
        database = FakeDatabase(state)
        gameside = SimpleNamespace(
            id=7,
            team_chan=800,
            team_chan_external_server=301,
        )
        game = SimpleNamespace(
            id=42,
            guild_id=300,
            name='Test Game',
            is_completed=False,
            is_confirmed=False,
            is_pending=False,
            expiration=now,
            announcement_channel=700,
            announcement_message=701,
            game_chan=900,
            gamesides=(gameside,),
            mentions=lambda: ['<@1>', '<@2>'],
        )

        def save():
            state['pending'] = game.is_pending
            state['expiration'] = game.expiration

        def write_log(**kwargs):
            state['logs'].append(kwargs)
            if log_effect:
                raise log_effect

        game.save = save
        with mock.patch.object(
            game_workers.models, 'db', database
        ), mock.patch.object(
            game_workers.models.Game, 'get_by_id', return_value=game
        ), mock.patch.object(
            game_workers.models.GameLog, 'write', side_effect=write_log
        ):
            if log_effect:
                with self.assertRaises(type(log_effect)):
                    game_workers.unstart_game(
                        42, 300, 'Staff', '$unstart', now=now
                    )
                result = None
            else:
                result = game_workers.unstart_game(
                    42, 300, 'Staff', '$unstart', now=now
                )
        return state, database, result

    def test_worker_commits_pending_state_log_and_effect_snapshot(self):
        state, database, result = self.run_worker()

        self.assertTrue(state['pending'])
        self.assertEqual(
            state['expiration'],
            datetime.datetime(2026, 7, 30, 12),
        )
        self.assertEqual(len(state['logs']), 1)
        self.assertEqual(result.mentions, ('<@1>', '<@2>'))
        self.assertEqual(
            result.channel_targets,
            (
                game_workers.GameChannelTarget(7, 800, 301),
                game_workers.GameChannelTarget(None, 900, 300),
            ),
        )
        self.assertEqual(database.commits, 1)
        self.assertEqual(database.connection_closed, 1)

    def test_log_failure_rolls_back_state_and_closes_connection(self):
        state, database, _ = self.run_worker(
            log_effect=peewee.OperationalError('log failure')
        )

        self.assertFalse(state['pending'])
        self.assertEqual(
            state['expiration'],
            datetime.datetime(2026, 7, 29, 12),
        )
        self.assertEqual(database.rollbacks, 1)
        self.assertEqual(database.connection_closed, 1)

    def test_worker_revalidates_mutable_state_and_guild(self):
        for guild_id, completed, pending, expected in (
            (301, False, False, 'different Discord server'),
            (300, True, False, 'completed already'),
            (300, False, True, 'already a pending'),
        ):
            with self.subTest(expected=expected):
                database = FakeDatabase({})
                game = SimpleNamespace(
                    id=42,
                    guild_id=guild_id,
                    is_completed=completed,
                    is_confirmed=False,
                    is_pending=pending,
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
                        game_workers.GameUnstartValidationError,
                        expected,
                    ):
                        game_workers.unstart_game(
                            42, 300, 'Staff', '$unstart'
                        )
                game.save.assert_not_called()
                write_log.assert_not_called()
                self.assertEqual(database.connection_closed, 1)

    def test_deleted_channel_reconciliation_is_worker_local(self):
        state = {'game_chan': 900, 'team_chan': 800}
        database = FakeDatabase(state)
        game = SimpleNamespace(id=42, guild_id=300, game_chan=900)
        gameside = SimpleNamespace(
            id=7,
            game_id=42,
            team_chan=800,
            team_chan_external_server=301,
        )

        def save_game():
            state['game_chan'] = game.game_chan

        def save_side():
            state['team_chan'] = gameside.team_chan

        game.save = save_game
        gameside.save = save_side
        targets = (
            game_workers.GameChannelTarget(7, 800, 301),
            game_workers.GameChannelTarget(None, 900, 300),
        )
        with mock.patch.object(
            game_workers.models, 'db', database
        ), mock.patch.object(
            game_workers.models.Game, 'get_by_id', return_value=game
        ), mock.patch.object(
            game_workers.models.GameSide,
            'get_by_id',
            return_value=gameside,
        ):
            cleared = game_workers.clear_deleted_game_channels(
                42, 300, targets
            )

        self.assertEqual(cleared, 2)
        self.assertIsNone(state['game_chan'])
        self.assertIsNone(state['team_chan'])
        self.assertEqual(database.commits, 1)
        self.assertEqual(database.connection_closed, 1)


class GameUnstartCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_prefix_is_preserved_and_slash_waits_for_taxonomy(self):
        prefix = {
            command.name: command
            for command in administration.administration.__cog_commands__
        }
        self.assertIsInstance(prefix['unstart'], commands.Command)
        slash_names = {
            command.name
            for command in administration.administration.__cog_app_commands__
        }
        self.assertNotIn('unstart', slash_names)

    async def test_database_failure_prevents_discord_effects(self):
        cog = administration.administration.__new__(
            administration.administration
        )
        cog.bot = SimpleNamespace(guilds=[])
        with mock.patch.object(
            administration.utilities, 'lock_game'
        ), mock.patch.object(
            administration.utilities, 'unlock_game'
        ), mock.patch.object(
            administration.models.GameLog,
            'member_string',
            return_value='Staff',
        ), mock.patch.object(
            administration.game_workers,
            'run_game_unstart',
            side_effect=peewee.OperationalError('failure'),
        ), mock.patch.object(
            administration.models.Game,
            'load_full_game',
        ) as load_game, mock.patch.object(
            administration.channels,
            'delete_game_channel',
            new=mock.AsyncMock(),
        ) as delete_channel:
            with self.assertRaises(peewee.OperationalError):
                await cog._unstart_game_and_post(
                    game_id=42,
                    guild=SimpleNamespace(id=300),
                    prefix='$',
                    requester=SimpleNamespace(id=1, display_name='Staff'),
                )

        load_game.assert_not_called()
        delete_channel.assert_not_awaited()

    async def test_discord_deletion_and_reconciliation_follow_commit(self):
        events = []
        target = game_workers.GameChannelTarget(None, 900, 300)
        result = game_workers.GameUnstartResult(
            game_id=42,
            game_name='Test Game',
            announcement_channel_id=None,
            announcement_message_id=None,
            mentions=('<@1>', '<@2>'),
            channel_targets=(target,),
            new_expiration=datetime.datetime(2026, 7, 30),
        )
        guild = SimpleNamespace(id=300)
        cog = administration.administration.__new__(
            administration.administration
        )
        cog.bot = SimpleNamespace(guilds=[guild])

        async def run_unstart(*args):
            events.append('commit')
            return result

        async def delete_channel(*args):
            events.append('discord')
            return True

        async def reconcile(*args):
            events.append('reconcile')
            return 1

        with mock.patch.object(
            administration.utilities, 'lock_game'
        ), mock.patch.object(
            administration.utilities, 'unlock_game'
        ), mock.patch.object(
            administration.models.GameLog,
            'member_string',
            return_value='Staff',
        ), mock.patch.object(
            administration.game_workers,
            'run_game_unstart',
            side_effect=run_unstart,
        ), mock.patch.object(
            administration.channels,
            'delete_game_channel',
            side_effect=delete_channel,
        ), mock.patch.object(
            administration.game_workers,
            'run_deleted_channel_reconciliation',
            side_effect=reconcile,
        ):
            message = await cog._unstart_game_and_post(
                game_id=42,
                guild=guild,
                prefix='$',
                requester=SimpleNamespace(id=1, display_name='Staff'),
            )

        self.assertEqual(events, ['commit', 'discord', 'reconcile'])
        self.assertIn('Game 42 is now an open game', message)
        self.assertNotIn(':warning:', message)

    async def test_invocation_from_game_channel_stops_before_worker(self):
        cog = administration.administration.__new__(
            administration.administration
        )
        game = SimpleNamespace(
            id=42,
            uses_channel_id=lambda channel_id: channel_id == 900,
        )
        ctx = SimpleNamespace(
            channel=SimpleNamespace(id=900),
            send=mock.AsyncMock(),
        )
        with mock.patch.object(
            cog,
            '_unstart_game_and_post',
            new=mock.AsyncMock(),
        ) as run_unstart:
            await administration.administration.unstart.callback(
                cog, ctx, game
            )

        run_unstart.assert_not_awaited()
        ctx.send.assert_awaited_once()

    async def test_slow_unstart_worker_does_not_block_event_loop(self):
        started = threading.Event()
        release = threading.Event()

        def slow(*args):
            started.set()
            release.wait(timeout=2)
            return game_workers.GameUnstartResult(
                42,
                'Test Game',
                None,
                None,
                ('<@1>', '<@2>'),
                (),
                datetime.datetime(2026, 7, 30),
            )

        with mock.patch.object(
            game_workers, 'unstart_game', side_effect=slow
        ):
            task = asyncio.create_task(
                game_workers.run_game_unstart(
                    42, 300, 'Staff', '$unstart'
                )
            )
            while not started.is_set():
                await asyncio.sleep(0.005)
            await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.2)
            release.set()
            await asyncio.sleep(0.05)
            self.assertEqual((await task).game_id, 42)
