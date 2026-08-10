"""Offline tests for P4.1a ranked-state correction."""

import asyncio
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
games = import_offline_runtime('modules.games')


class RankedStateWorkerTests(unittest.TestCase):
    def test_worker_commits_state_and_log_on_local_connection(self):
        state = {'ranked': False, 'logs': []}
        database = FakeDatabase(state)
        game = SimpleNamespace(
            id=42,
            guild_id=300,
            is_completed=False,
            is_confirmed=False,
            is_ranked=False,
        )

        def save():
            state['ranked'] = game.is_ranked

        game.save = save
        publication = SimpleNamespace(
            game=SimpleNamespace(
                game_id=42,
                is_completed=False,
                is_confirmed=False,
                is_ranked=True,
            ),
            roster_mentions=('<@1>', '<@2>'),
        )

        def freeze(*_args):
            self.assertEqual(database.commits, 1)
            self.assertEqual(database.connection_closed, 0)
            return publication

        with mock.patch.object(
            game_workers.models, 'db', database
        ), mock.patch.object(
            game_workers.models.Game, 'get_by_id', return_value=game
        ), mock.patch.object(
            game_workers.models.Game, 'load_full_game', return_value=game
        ), mock.patch.object(
            game_workers.game_result_publication_workers,
            'freeze_loaded_game',
            side_effect=freeze,
        ), mock.patch.object(
            game_workers.models.GameLog,
            'write',
            side_effect=lambda **kwargs: state['logs'].append(kwargs),
        ):
            result = game_workers.set_game_ranked_state(
                42, 300, True, 'Staff'
            )

        self.assertTrue(result.is_ranked)
        self.assertIs(result.publication, publication)
        self.assertTrue(state['ranked'])
        self.assertEqual(len(state['logs']), 1)
        self.assertEqual(database.commits, 1)
        self.assertEqual(database.connection_closed, 1)

    def test_snapshot_failure_reports_committed_ranked_state(self):
        state = {'ranked': False, 'logs': []}
        database = FakeDatabase(state)
        game = SimpleNamespace(
            id=42,
            guild_id=300,
            is_completed=False,
            is_confirmed=False,
            is_ranked=False,
            save=lambda: state.update(ranked=game.is_ranked),
        )
        with mock.patch.object(
            game_workers.models, 'db', database
        ), mock.patch.object(
            game_workers.models.Game, 'get_by_id', return_value=game
        ), mock.patch.object(
            game_workers.models.Game, 'load_full_game', return_value=game
        ), mock.patch.object(
            game_workers.game_result_publication_workers,
            'freeze_loaded_game',
            side_effect=peewee.OperationalError('snapshot failure'),
        ), mock.patch.object(
            game_workers.models.GameLog,
            'write',
            side_effect=lambda **kwargs: state['logs'].append(kwargs),
        ):
            with self.assertRaises(
                game_workers.RankedStateSnapshotError
            ) as raised:
                game_workers.set_game_ranked_state(42, 300, True, 'Staff')

        self.assertTrue(state['ranked'])
        self.assertTrue(raised.exception.result.is_ranked)
        self.assertIsNone(raised.exception.result.publication)
        self.assertEqual(database.commits, 1)
        self.assertEqual(database.rollbacks, 0)
        self.assertEqual(database.connection_closed, 1)

    def test_log_failure_rolls_back_ranked_state(self):
        state = {'ranked': False}
        database = FakeDatabase(state)
        game = SimpleNamespace(
            id=42,
            guild_id=300,
            is_completed=False,
            is_confirmed=False,
            is_ranked=False,
        )
        game.save = lambda: state.update(ranked=game.is_ranked)
        with mock.patch.object(
            game_workers.models, 'db', database
        ), mock.patch.object(
            game_workers.models.Game, 'get_by_id', return_value=game
        ), mock.patch.object(
            game_workers.models.GameLog,
            'write',
            side_effect=peewee.OperationalError('log failure'),
        ):
            with self.assertRaises(peewee.OperationalError):
                game_workers.set_game_ranked_state(42, 300, True, 'Staff')

        self.assertFalse(state['ranked'])
        self.assertEqual(database.rollbacks, 1)
        self.assertEqual(database.connection_closed, 1)


class RankedStateCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_prefix_and_typed_slash_registration(self):
        prefix = {
            command.name: command
            for command in administration.administration.__cog_commands__
        }
        self.assertIn('rankset', prefix)
        self.assertIn('rankunset', prefix)
        self.assertTrue(all(
            isinstance(prefix[name], commands.Command)
            for name in ('rankset', 'rankunset')
        ))
        game_group = {
            command.name: command
            for command in games.polygames.__cog_app_commands__
        }['game']
        slash = game_group.get_command('ranked')
        self.assertEqual(
            [(p.name, p.type) for p in slash.parameters],
            [
                ('game_id', discord.AppCommandOptionType.integer),
                ('ranked', discord.AppCommandOptionType.boolean),
            ],
        )

    async def test_database_failure_prevents_discord_update(self):
        cog = administration.administration.__new__(
            administration.administration
        )
        guild = SimpleNamespace(id=300)
        requester = SimpleNamespace(id=1, display_name='Staff')
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
            'run_ranked_state_correction',
            side_effect=peewee.OperationalError('failure'),
        ), mock.patch.object(
            administration.models.Game,
            'load_full_game',
        ) as load_game:
            with self.assertRaises(peewee.OperationalError):
                await cog._set_ranked_state_and_post(
                    game_id=42,
                    guild=guild,
                    is_ranked=True,
                    requester=requester,
                )
        load_game.assert_not_called()

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
            cog, '_set_ranked_state_and_post', new=mock.AsyncMock()
        ) as run_correction:
            await administration.administration.set_ranked_slash(
                cog,
                interaction,
                42,
                True,
            )

        interaction.response.send_message.assert_awaited_once()
        interaction.response.defer.assert_not_awaited()
        run_correction.assert_not_awaited()

    async def test_slash_defers_before_worker_pipeline(self):
        events = []
        cog = administration.administration.__new__(
            administration.administration
        )

        async def defer(**kwargs):
            events.append(('defer', kwargs))

        async def run_correction(**kwargs):
            events.append(('worker', kwargs))
            return 'Game 42 is now marked as ranked.'

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
            '_set_ranked_state_and_post',
            new=mock.AsyncMock(side_effect=run_correction),
        ):
            await administration.administration.set_ranked_slash(
                cog,
                interaction,
                42,
                True,
            )

        self.assertEqual([event[0] for event in events], ['defer', 'worker'])
        self.assertEqual(events[0][1], {})
        interaction.followup.send.assert_awaited_once_with(
            'Game 42 is now marked as ranked.',
        )

    async def test_slow_correction_does_not_block_event_loop(self):
        started = threading.Event()
        release = threading.Event()

        def slow(*args):
            started.set()
            release.wait(timeout=2)
            return game_workers.RankedStateResult(42, True)

        with mock.patch.object(
            game_workers, 'set_game_ranked_state', side_effect=slow
        ):
            task = asyncio.create_task(
                game_workers.run_ranked_state_correction(
                    42, 300, True, 'Staff'
                )
            )
            while not started.is_set():
                await asyncio.sleep(0.005)
            await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.2)
            release.set()
            await asyncio.sleep(0.05)
            self.assertTrue((await task).is_ranked)

    async def test_cancellation_waits_for_rank_worker_to_finish(self):
        started = threading.Event()
        release = threading.Event()

        def slow(*args):
            started.set()
            release.wait(timeout=2)
            return game_workers.RankedStateResult(42, True)

        with mock.patch.object(
            game_workers, 'set_game_ranked_state', side_effect=slow
        ):
            task = asyncio.create_task(
                game_workers.run_ranked_state_correction(
                    42, 300, True, 'Staff'
                )
            )
            while not started.is_set():
                await asyncio.sleep(0.005)
            task.cancel()
            await asyncio.sleep(0.02)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
