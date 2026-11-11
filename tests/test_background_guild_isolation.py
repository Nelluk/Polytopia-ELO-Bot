"""Offline regression coverage for guild isolation in maintenance tasks."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.test_newgame_worker import import_offline_runtime


matchmaking = import_offline_runtime('modules.matchmaking')
misc = import_offline_runtime('modules.misc')


class BackgroundGuildIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_purge_skips_inert_guild_and_contains_failed_guild(self):
        guilds = [SimpleNamespace(id=i, name=str(i)) for i in (1, 2, 3)]
        cog = SimpleNamespace(bot=SimpleNamespace(guilds=guilds))
        purge = mock.AsyncMock(side_effect=[RuntimeError('guild failure'), ()])
        with mock.patch.object(
            matchmaking.settings, 'wait_until_guild_configuration_ready',
            new=mock.AsyncMock(),
        ), mock.patch.object(
            matchmaking.settings, 'guild_configuration_allows_dispatch',
            side_effect=lambda guild_id: guild_id != 1,
        ), mock.patch.object(
            matchmaking.game_expiration, 'purge_expired_games_for_guild', purge,
        ), mock.patch.object(matchmaking.logger, 'exception') as log:
            # A failed guild must also leave the coroutine usable next cycle.
            await matchmaking.matchmaking.task_purge_expired_games.coro(cog)
            purge.side_effect = None
            await matchmaking.matchmaking.task_purge_expired_games.coro(cog)

        self.assertEqual(
            [call.kwargs['guild'].id for call in purge.await_args_list],
            [2, 3, 2, 3],
        )
        log.assert_called_once()

    async def test_purge_preserves_cancellation(self):
        cog = SimpleNamespace(bot=SimpleNamespace(
            guilds=[SimpleNamespace(id=1, name='guild')],
        ))
        with mock.patch.object(
            matchmaking.settings, 'wait_until_guild_configuration_ready',
            new=mock.AsyncMock(),
        ), mock.patch.object(
            matchmaking.settings, 'guild_configuration_allows_dispatch',
            return_value=True,
        ), mock.patch.object(
            matchmaking.game_expiration, 'purge_expired_games_for_guild',
            new=mock.AsyncMock(side_effect=asyncio.CancelledError),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await matchmaking.matchmaking.task_purge_expired_games.coro(cog)

    async def test_newbie_broadcasts_continue_across_guilds_and_cycles(self):
        for task_name in (
            'task_broadcast_newbie_message',
            'task_broadcast_newbie_steam_message',
        ):
            for failure in ('configuration', 'delivery'):
                with self.subTest(task=task_name, failure=failure):
                    await self.check_broadcast_isolation(task_name, failure)

    async def check_broadcast_isolation(self, task_name, failure):
        channels = {
            guild_id: SimpleNamespace(
                id=guild_id * 10,
                send=mock.AsyncMock(return_value=SimpleNamespace(id=100)),
            )
            for guild_id in (1, 2, 3)
        }
        if failure == 'delivery':
            channels[2].send.side_effect = RuntimeError('send failed')
        guilds = [
            SimpleNamespace(
                id=guild_id,
                get_channel=mock.Mock(return_value=channels[guild_id]),
            )
            for guild_id in (1, 2, 3)
        ]
        bot = SimpleNamespace(
            guilds=guilds,
            is_closed=mock.Mock(side_effect=[False, False, True]),
            purgable_messages=[],
        )

        def guild_setting(guild_id, key):
            self.assertNotEqual(guild_id, 1, 'inert guild read configuration')
            if guild_id == 2 and failure == 'configuration':
                raise misc.exceptions.CheckFailedError('configuration removed')
            return {
                'newbie_message_channels': [guild_id * 10],
                'steam_game_channel': guild_id * 10,
                'command_prefix': '$',
                'bot_channels_strict': [999],
            }[key]

        with mock.patch.object(
            misc.settings, 'wait_until_guild_configuration_ready',
            new=mock.AsyncMock(),
        ), mock.patch.object(
            misc.settings, 'guild_configuration_allows_dispatch',
            side_effect=lambda guild_id: guild_id != 1,
        ), mock.patch.object(
            misc.settings, 'guild_setting', side_effect=guild_setting,
        ), mock.patch.object(
            misc.asyncio, 'sleep', new=mock.AsyncMock(),
        ), mock.patch.object(misc.logger, 'exception') as log:
            await getattr(misc.misc, task_name)(SimpleNamespace(bot=bot))

        channels[1].send.assert_not_awaited()
        self.assertEqual(channels[3].send.await_count, 2)
        self.assertEqual(bot.purgable_messages, [(3, 30, 100), (3, 30, 100)])
        self.assertEqual(log.call_count, 2)

    async def test_newbie_broadcasts_preserve_cancellation(self):
        for task_name in (
            'task_broadcast_newbie_message',
            'task_broadcast_newbie_steam_message',
        ):
            with self.subTest(task=task_name):
                cog = SimpleNamespace(bot=SimpleNamespace(
                    guilds=[SimpleNamespace(id=1, get_channel=mock.Mock())],
                    is_closed=mock.Mock(side_effect=[False, True]),
                ))
                with mock.patch.object(
                    misc.settings, 'wait_until_guild_configuration_ready',
                    new=mock.AsyncMock(),
                ), mock.patch.object(
                    misc.settings, 'guild_configuration_allows_dispatch',
                    return_value=True,
                ), mock.patch.object(
                    misc.settings, 'guild_setting',
                    side_effect=asyncio.CancelledError,
                ), mock.patch.object(
                    misc.asyncio, 'sleep', new=mock.AsyncMock(),
                ):
                    with self.assertRaises(asyncio.CancelledError):
                        await getattr(misc.misc, task_name)(cog)
