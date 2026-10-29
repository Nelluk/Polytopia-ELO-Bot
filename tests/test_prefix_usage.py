"""Exercise malformed text through Discord's real prefix argument parser."""

from contextlib import nullcontext
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

import discord
from discord.ext import commands
import peewee

from tests.test_newgame_worker import FakeDatabase, import_offline_runtime


bot_module = import_offline_runtime('bot')
games = import_offline_runtime('modules.games')
workers = import_offline_runtime('modules.game_win_workers')


class PrefixUsageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = commands.Bot(command_prefix='$', intents=discord.Intents.none())
        self.bot._connection.user = SimpleNamespace(id=999)
        cog = games.polygames.__new__(games.polygames)
        for original in games.polygames.__cog_commands__:
            if original.name in ('win', 'unwin', 'delete'):
                command = original.copy()
                command.cog = cog
                # Authorization is outside these parser-only tests.
                command.checks = []
                self.bot.add_command(command)

    async def asyncTearDown(self):
        await self.bot.close()

    async def invoke(self, text):
        message = SimpleNamespace(
            content=text,
            author=SimpleNamespace(id=900, display_name='Tester', bot=False),
            guild=SimpleNamespace(id=10),
            channel=SimpleNamespace(id=100),
            attachments=[],
            _state=self.bot._connection,
        )
        ctx = await self.bot.get_context(message)
        self.assertIsNotNone(ctx.command)
        ctx.send = mock.AsyncMock()
        try:
            await ctx.command.invoke(ctx)
        except commands.CommandError as exc:
            await bot_module.handle_prefix_command_error(ctx, exc)
        return ctx

    async def test_missing_parameters_and_aliases_explain_usage_without_writes(self):
        with mock.patch.object(games.game_win, 'run_win', new=mock.AsyncMock()) as win, \
             mock.patch.object(games.game_unwin, 'run_unwin', new=mock.AsyncMock()) as unwin, \
             mock.patch.object(games.game_deletion, 'delete_game', new=mock.AsyncMock()) as delete:
            for name in ('win', 'lose', 'unwin', 'delete', 'delete_game',
                         'delgame', 'delmatch', 'deletegame'):
                with self.subTest(command=name):
                    ctx = await self.invoke(f'${name}')
                    ctx.send.assert_awaited_once()
                    message = ctx.send.await_args.args[0]
                    self.assertIn('$win' if name == 'lose' else f'${name}', message)
                    self.assertIn('usage', message.lower())
            win.assert_not_awaited()
            unwin.assert_not_awaited()
            delete.assert_not_awaited()

    async def test_bad_game_ids_are_explained_once_without_writes(self):
        with mock.patch.object(games.game_win, 'run_win', new=mock.AsyncMock()) as win, \
             mock.patch.object(games.game_unwin, 'run_unwin', new=mock.AsyncMock()) as unwin, \
             mock.patch.object(games.game_deletion, 'delete_game', new=mock.AsyncMock()) as delete:
            for name in ('win', 'unwin', 'deletegame'):
                for game_id in ('abc', str(2 ** 40)):
                    with self.subTest(command=name, game_id=game_id):
                        ctx = await self.invoke(f'${name} {game_id}')
                        ctx.send.assert_awaited_once()
                        self.assertIn('Invalid game ID', ctx.send.await_args.args[0])
            win.assert_not_awaited()
            unwin.assert_not_awaited()
            delete.assert_not_awaited()

    async def test_missing_winner_lists_sides_using_worker_connection_without_writes(self):
        database = FakeDatabase({})
        loop_thread = threading.get_ident()
        game = SimpleNamespace(
            id=157008, guild_id=10, is_pending=False,
            list_gameside_membership=lambda: ['Side 1: **Alpha**', 'Side 2: **Beta**'],
        )

        def load(**kwargs):
            self.assertNotEqual(threading.get_ident(), loop_thread)
            self.assertEqual(kwargs, {'game_id': 157008})
            return game

        with mock.patch.object(workers.models, 'db', database), \
             mock.patch.object(workers, '_registered', return_value=True), \
             mock.patch.object(workers.models.Game, 'load_full_game', side_effect=load), \
             mock.patch.object(games.settings, 'is_staff', return_value=False), \
             mock.patch.object(games.settings, 'elo_job_coordinator', SimpleNamespace(is_active=False)), \
             mock.patch.object(games.game_win, '_run_worker', new=mock.AsyncMock()) as write:
            # Avoid a Discord typing request while exercising the application service.
            with mock.patch.object(commands.Context, 'typing', return_value=nullcontext()):
                ctx = await self.invoke('$win 157008')
            write.assert_not_awaited()
        ctx.send.assert_awaited_once()
        self.assertIn('$win 157008 WINNER', ctx.send.await_args.args[0])
        self.assertIn('Side 1: **Alpha**', ctx.send.await_args.args[0])
        self.assertIn('Side 2: **Beta**', ctx.send.await_args.args[0])
        self.assertEqual((database.connection_opened, database.connection_closed), (1, 1))
        self.assertEqual(database.commits, 0)

    async def test_complete_win_still_forwards_parsed_id_and_winner(self):
        with mock.patch.object(games.settings, 'is_staff', return_value=False), \
             mock.patch.object(games.game_win, 'run_win', new=mock.AsyncMock()) as run:
            ctx = await self.invoke('$win 157008 Blue Team')
        request = run.await_args.args[0]
        self.assertEqual(request.game_id, 157008)
        self.assertEqual(request.winner_text, 'Blue Team')
        ctx.send.assert_not_awaited()

    async def test_shared_parser_guidance_for_required_typed_and_quoted_arguments(self):
        @commands.command(aliases=['example'], usage='count name')
        async def sample(ctx, count: int, name: str):
            raise AssertionError('Invalid input must not invoke the callback')

        self.bot.add_command(sample)
        for text, explanation in (
            ('$example', 'Missing required argument: count'),
            ('$example abc Blue', 'Use a whole number'),
            ('$example 1', 'Missing required argument: name'),
            ('$example 1 "Blue', 'quotation marks'),
        ):
            with self.subTest(text=text):
                ctx = await self.invoke(text)
                ctx.send.assert_awaited_once()
                response = ctx.send.await_args.args[0]
                self.assertIn(explanation, response)
                self.assertIn('$example count name', response)
                self.assertIn('$help sample', response)

    async def test_group_command_and_extra_arguments_get_usable_syntax(self):
        @commands.group()
        async def tools(ctx):
            pass

        @tools.command(aliases=['set'], ignore_extra=False)
        async def count(ctx, value: int):
            raise AssertionError('Invalid input must not invoke the callback')

        self.bot.add_command(tools)
        for text, explanation in (
            ('$tools set', 'Missing required argument: value'),
            ('$tools set 1 extra', 'Too many arguments'),
        ):
            with self.subTest(text=text):
                ctx = await self.invoke(text)
                ctx.send.assert_awaited_once()
                response = ctx.send.await_args.args[0]
                self.assertIn(explanation, response)
                self.assertIn('$tools set <value>', response)
                self.assertIn('$help tools count', response)


class MissingWinnerValidationTests(unittest.TestCase):
    def test_game_validation_precedes_side_disclosure(self):
        for case in ('missing', 'foreign', 'pending'):
            with self.subTest(case=case):
                game = SimpleNamespace(
                    id=77, guild_id=20 if case == 'foreign' else 10,
                    is_pending=case == 'pending',
                    list_gameside_membership=mock.Mock(),
                )
                load = mock.Mock(
                    side_effect=peewee.DoesNotExist if case == 'missing' else None,
                    return_value=game,
                )
                with mock.patch.object(workers.models, 'db', FakeDatabase({})), \
                     mock.patch.object(workers, '_registered', return_value=True), \
                     mock.patch.object(workers.models.Game, 'load_full_game', load):
                    with self.assertRaises(workers.elo_workers.WinValidationError):
                        workers.prepare_win(workers.WinPreflightRequest(
                            game_id=77, guild_id=10, requester_id=900,
                            requester_is_staff=False,
                        ))
                game.list_gameside_membership.assert_not_called()
