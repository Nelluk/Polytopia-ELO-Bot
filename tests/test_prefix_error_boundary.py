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

    async def test_ignored_input_error_preserves_silent_public_behavior(self):
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


if __name__ == '__main__':
    unittest.main()
