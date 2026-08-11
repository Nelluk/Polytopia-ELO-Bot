"""Focused offline coverage for retained name/game-ID boundaries."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

from discord.ext import commands

from tests.test_newgame_worker import FakeDatabase, import_offline_runtime


workers = import_offline_runtime('modules.legacy_name_workers')
games = import_offline_runtime('modules.games')
administration = import_offline_runtime('modules.administration')


class LegacyNameWorkerTests(unittest.TestCase):
    def test_account_read_owns_connection_and_freezes_primitives(self):
        database = FakeDatabase({})
        stored = SimpleNamespace(
            name='AccountUser',
            polytopia_name='Canonical Name',
            name_steam='Legacy Name',
        )
        with mock.patch.object(
            workers.models, 'db', database
        ), mock.patch.object(
            workers.models.DiscordMember,
            'get_or_none',
            return_value=stored,
        ):
            snapshot = workers.load_account_name(100)

        self.assertEqual((database.connection_opened, database.connection_closed), (1, 1))
        self.assertEqual(snapshot.account_name, 'Canonical Name')
        stored.polytopia_name = 'Changed after return'
        self.assertEqual(snapshot.account_name, 'Canonical Name')
        with self.assertRaises(FrozenInstanceError):
            snapshot.display_name = 'changed'

    def test_registered_match_never_returns_a_model(self):
        database = FakeDatabase({})
        member = SimpleNamespace(
            polytopia_name=None,
            name_steam='Fallback Name',
        )
        player = SimpleNamespace(name='Player Display', discord_member=member)
        with mock.patch.object(
            workers.models, 'db', database
        ), mock.patch.object(
            workers.models.Player,
            'string_matches',
            return_value=[player],
        ):
            result = workers.load_registered_name_match('Player', 300)

        self.assertEqual(result.match_count, 1)
        self.assertEqual(result.player.account_name, 'Fallback Name')
        self.assertIsInstance(result.player.display_name, str)
        self.assertFalse(hasattr(result.player, 'discord_member'))

    def test_game_names_freeze_draft_order_before_connection_closes(self):
        database = FakeDatabase({})
        member = SimpleNamespace(
            polytopia_name='Alpha Code',
            name_steam=None,
            timezone_offset_minutes=-300,
            timezone_offset_cleared=False,
            timezone_offset=None,
        )
        player = SimpleNamespace(name='Alpha', discord_member=member)
        game = SimpleNamespace(
            id=42,
            draft_order=lambda: [{'player': player}],
        )
        with mock.patch.object(
            workers.models, 'db', database
        ), mock.patch.object(
            workers.models.Game,
            'get_by_id',
            return_value=game,
        ):
            snapshot = workers.load_game_names(game_id=42, channel_id=900)

        self.assertEqual((database.connection_opened, database.connection_closed), (1, 1))
        self.assertEqual(snapshot.game_id, 42)
        self.assertEqual(snapshot.rows[0].timezone, 'UTC-05:00')
        self.assertFalse(hasattr(snapshot.rows[0], 'discord_member'))
        with self.assertRaises(FrozenInstanceError):
            snapshot.rows[0].player_name = 'changed'


class LegacyNameAsyncBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_read_drains_owned_worker_and_keeps_loop_responsive(self):
        started = threading.Event()
        release = threading.Event()
        executor = ThreadPoolExecutor(max_workers=1)

        def slow_read(_discord_id):
            started.set()
            release.wait(timeout=2)
            return workers.AccountNameSnapshot('Alpha', 'Alpha Code')

        with mock.patch.object(
            workers, '_legacy_name_executor', executor
        ), mock.patch.object(
            workers, 'load_account_name', side_effect=slow_read
        ):
            task = asyncio.create_task(workers.run_account_name(100))
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.001)
            self.assertTrue(started.is_set())
            task.cancel()
            await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.05)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        executor.shutdown(wait=True)


class RetainedPrefixBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_polygame_converter_is_syntax_only(self):
        ctx = SimpleNamespace(send=mock.AsyncMock())
        with mock.patch.object(
            games.utilities, 'connect', side_effect=AssertionError
        ), mock.patch.object(
            games.Game, 'get', side_effect=AssertionError
        ):
            self.assertEqual(await games.PolyGame().convert(ctx, '42'), 42)
        ctx.send.assert_not_awaited()

        with self.assertRaises(commands.UserInputError):
            await games.PolyGame().convert(ctx, 'not-a-game')
        ctx.send.assert_awaited_once_with('Invalid game ID "not-a-game".')

        ctx.send.reset_mock()
        with self.assertRaises(commands.UserInputError):
            await games.PolyGame().convert(ctx, str(2 ** 40))
        ctx.send.assert_awaited_once_with(f'Invalid game ID "{2 ** 40}".')

    async def test_rank_prefixes_forward_primitive_id_to_authoritative_worker(self):
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=300),
            author=SimpleNamespace(id=100),
            send=mock.AsyncMock(),
        )
        for command_name, expected_ranked in (
            ('rankset', True),
            ('rankunset', False),
        ):
            command = next(
                command
                for command in administration.administration.__cog_commands__
                if command.name == command_name
            )
            run = mock.AsyncMock(return_value='updated')
            cog = SimpleNamespace(_set_ranked_state_and_post=run)
            await command.callback(cog, ctx, 42)
            run.assert_awaited_once_with(
                game_id=42,
                guild=ctx.guild,
                is_ranked=expected_ranked,
                requester=ctx.author,
            )

    async def test_extend_prefix_forwards_zero_for_authoritative_not_found(self):
        command = next(
            command
            for command in administration.administration.__cog_commands__
            if command.name == 'extend'
        )
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=300),
            author=SimpleNamespace(id=100),
            send=mock.AsyncMock(),
        )
        run = mock.AsyncMock(
            side_effect=administration.game_workers.GameExtensionValidationError(
                'Game with ID 0 cannot be found.'
            )
        )
        cog = SimpleNamespace(_extend_pending_game=run)
        await command.callback(cog, ctx, 0)
        run.assert_awaited_once_with(
            game_id=0,
            guild_id=300,
            requester=ctx.author,
        )
        ctx.send.assert_awaited_once_with('Game with ID 0 cannot be found.')

    async def test_getname_publishes_only_worker_snapshot(self):
        command = next(
            command for command in games.polygames.__cog_commands__
            if command.name == 'getname'
        )
        target = SimpleNamespace(id=100, name='Discord Alpha')
        ctx = SimpleNamespace(
            author=target,
            guild=SimpleNamespace(id=300),
            invoked_with='getname',
            prefix='$',
            send=mock.AsyncMock(),
        )
        snapshot = workers.AccountNameSnapshot('Stored Alpha', 'Alpha Code')
        with mock.patch.object(
            games.utilities,
            'get_guild_member',
            new=mock.AsyncMock(return_value=[target]),
        ), mock.patch.object(
            games.legacy_name_workers,
            'run_account_name',
            new=mock.AsyncMock(return_value=snapshot),
        ) as run, mock.patch.object(
            games.DiscordMember,
            'get_or_none',
            side_effect=AssertionError('ORM reached the event loop'),
        ):
            await command.callback(games.polygames.__new__(games.polygames), ctx)

        run.assert_awaited_once_with(100)
        output = '\n'.join(call.args[0] for call in ctx.send.await_args_list)
        self.assertIn('Stored Alpha', output)
        self.assertIn('Alpha Code', output)

    async def test_getnames_preserves_ordered_output_from_snapshot(self):
        command = next(
            command for command in games.polygames.__cog_commands__
            if command.name == 'getnames'
        )

        class Typing:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        ctx = SimpleNamespace(
            message=SimpleNamespace(channel=SimpleNamespace(id=900)),
            prefix='$',
            invoked_with='getnames',
            typing=lambda: Typing(),
            send=mock.AsyncMock(),
        )
        snapshot = workers.GameNamesSnapshot(
            game_id=42,
            rows=(
                workers.GameNameRow('Alpha', 'Alpha Code', 'UTC-05:00'),
                workers.GameNameRow('Beta', None, 'UTC+01:00'),
            ),
        )
        with mock.patch.object(
            games.legacy_name_workers,
            'run_game_names',
            new=mock.AsyncMock(return_value=snapshot),
        ) as run, mock.patch.object(
            games.models.Game,
            'by_channel_id',
            side_effect=AssertionError('ORM reached the event loop'),
        ):
            await command.callback(
                games.polygames.__new__(games.polygames),
                ctx,
                arg='42',
            )

        run.assert_awaited_once_with(game_id=42, channel_id=900)
        output = '\n'.join(call.args[0] for call in ctx.send.await_args_list)
        self.assertLess(output.index('Alpha'), output.index('Beta'))
        self.assertIn('UTC+01:00', output)
        self.assertIn('No account-wide name set', output)


if __name__ == '__main__':
    unittest.main()
