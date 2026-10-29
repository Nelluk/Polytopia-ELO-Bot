"""Security coverage for retained-prefix unexpected failures."""

from types import SimpleNamespace
import unittest
from unittest import mock

from discord.ext import commands

from tests.test_newgame_worker import import_offline_runtime


bot = import_offline_runtime('bot')


class PrefixErrorBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_unexpected_failure_logs_detail_but_sends_only_reference(self):
        secret = 'database-password-shaped-secret'
        error = RuntimeError(secret)
        wrapped = commands.CommandInvokeError(error)
        ctx = SimpleNamespace(
            command=SimpleNamespace(name='legacy'),
            invoked_with='legacy',
            prefix='$',
            send=mock.AsyncMock(),
        )
        with mock.patch.object(
            bot, 'prefix_error_reference', return_value='A1B2C3D4'
        ), mock.patch.object(bot.logger, 'critical') as log:
            await bot.handle_prefix_command_error(ctx, wrapped)

        public_message = ctx.send.await_args.args[0]
        self.assertIn('A1B2C3D4', public_message)
        self.assertNotIn(secret, public_message)
        self.assertNotIn('<@', public_message)
        log.assert_called_once()
        self.assertIs(log.call_args.kwargs['exc_info'][1], error)
        self.assertEqual(log.call_args.args[1], 'A1B2C3D4')

    async def test_converter_error_already_explained_preserves_single_reply(self):
        ctx = SimpleNamespace(
            command=SimpleNamespace(name='legacy'),
            invoked_with='legacy',
            prefix='$',
            send=mock.AsyncMock(),
        )
        with mock.patch.object(bot.logger, 'warning') as log:
            await bot.handle_prefix_command_error(
                ctx,
                commands.UserInputError('bad input'),
            )
        ctx.send.assert_not_awaited()
        log.assert_called_once()

    async def test_parser_error_does_not_publish_exception_details(self):
        command = SimpleNamespace(
            name='example', qualified_name='example', signature='count',
        )
        ctx = SimpleNamespace(
            command=command, invoked_with='example', prefix='$',
            current_parameter=SimpleNamespace(name='count', annotation=int),
            send=mock.AsyncMock(),
        )
        await bot.handle_prefix_command_error(
            ctx, commands.BadArgument('private exception details'),
        )
        message = ctx.send.await_args.args[0]
        self.assertIn('Use a whole number.', message)
        self.assertIn('$example count', message)
        self.assertNotIn('private exception details', message)
        self.assertFalse(ctx.send.await_args.kwargs['allowed_mentions'].everyone)

    async def test_unknown_commands_and_checks_do_not_receive_usage_fallback(self):
        ctx = SimpleNamespace(
            command=None, invoked_with='unknown', prefix='$',
            send=mock.AsyncMock(),
        )
        with mock.patch.object(bot.logger, 'warning'):
            for error in (commands.CommandNotFound(), commands.CheckFailure()):
                await bot.handle_prefix_command_error(ctx, error)
        ctx.send.assert_not_awaited()

    async def test_retired_commands_explain_replacement_using_the_guild_prefix(self):
        for name, replacement in (
            ('squad', '/squad show'),
            ('pingsteam', '!pingall'),
            ('dbb', 'host'),
        ):
            with self.subTest(name=name):
                ctx = SimpleNamespace(
                    command=None, invoked_with=name, prefix='!',
                    send=mock.AsyncMock(),
                )
                await bot.handle_prefix_command_error(ctx, commands.CommandNotFound())
                ctx.send.assert_awaited_once()
                self.assertIn(replacement, ctx.send.await_args.args[0])


if __name__ == '__main__':
    unittest.main()
