"""Focused P5.9 reaction lookup and listener-adapter coverage."""

import asyncio
from contextlib import nullcontext
from dataclasses import FrozenInstanceError
import inspect
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.game_reaction_workers')
matchmaking = import_offline_runtime('modules.matchmaking')


def request(game_id=322):
    return workers.ReactionGameRequest(game_id=game_id)


def snapshot(**overrides):
    values = dict(
        game_id=322,
        exists=True,
        guild_id=300,
        is_pending=True,
        external_server_ids=(400, 401),
    )
    values.update(overrides)
    return workers.ReactionGameSnapshot(**values)


class WorkerTests(unittest.TestCase):
    def test_dtos_are_frozen_primitives(self):
        row = snapshot()
        with self.assertRaises(FrozenInstanceError):
            row.guild_id = 1
        self.assertEqual(row.external_server_ids, (400, 401))

    def test_existing_game_uses_connection_and_returns_bounded_snapshot(self):
        connection = mock.MagicMock(return_value=nullcontext())
        with mock.patch.object(
            workers.models.db, 'connection_context', connection
        ), mock.patch.object(
            workers, '_game_row', return_value=(322, 300, True)
        ) as load_game, mock.patch.object(
            workers, '_external_server_ids', return_value=(400, 401)
        ) as external:
            actual = workers.load_reaction_game(request())
        connection.assert_called_once_with()
        load_game.assert_called_once_with(322)
        external.assert_called_once_with(300)
        self.assertEqual(actual, snapshot())

    def test_missing_game_does_not_query_external_servers(self):
        with mock.patch.object(
            workers.models.db, 'connection_context', return_value=nullcontext()
        ), mock.patch.object(
            workers, '_game_row', return_value=None
        ), mock.patch.object(workers, '_external_server_ids') as external:
            actual = workers.load_reaction_game(request())
        external.assert_not_called()
        self.assertEqual(
            actual,
            snapshot(
                exists=False,
                guild_id=None,
                is_pending=False,
                external_server_ids=(),
            ),
        )

    def test_external_server_bound_fails_closed(self):
        with mock.patch.object(
            workers.models.db, 'connection_context', return_value=nullcontext()
        ), mock.patch.object(
            workers, '_game_row', return_value=(322, 300, True)
        ), mock.patch.object(
            workers,
            '_external_server_ids',
            return_value=tuple(range(workers.MAX_EXTERNAL_SERVERS + 1)),
        ):
            with self.assertRaisesRegex(
                workers.ReactionGameLookupError,
                'too many related external servers',
            ):
                workers.load_reaction_game(request())

    def test_executor_keeps_loop_responsive_and_drains_cancellation(self):
        async def scenario():
            started = threading.Event()
            release = threading.Event()

            def slow(_request):
                started.set()
                release.wait(timeout=2)
                return snapshot()

            with mock.patch.object(
                workers, 'load_reaction_game', side_effect=slow
            ):
                task = asyncio.create_task(
                    workers.run_load_reaction_game(request())
                )
                while not started.is_set():
                    await asyncio.sleep(0.001)
                heartbeat = False

                async def mark():
                    nonlocal heartbeat
                    heartbeat = True

                await mark()
                task.cancel()
                await asyncio.sleep(0.005)
                still_draining = not task.done()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            return heartbeat, still_draining

        self.assertEqual(asyncio.run(scenario()), (True, True))


class ListenerLookupTests(unittest.IsolatedAsyncioTestCase):
    def make_cog(self):
        cog = matchmaking.matchmaking.__new__(matchmaking.matchmaking)
        cog.bot = SimpleNamespace(user=SimpleNamespace(id=999))
        cog.ignorable_join_reactions = set()
        return cog

    def test_parser_is_pure_and_accepts_three_digit_ids(self):
        message = (
            'Other players can join game 322 by reacting with '
            f'{matchmaking.settings.emoji_join_game}.'
        )
        with mock.patch.object(
            matchmaking.models.Game, 'get_or_none'
        ) as database:
            actual = matchmaking.matchmaking.parse_joingame_message(message)
        database.assert_not_called()
        self.assertEqual(actual, 322)

    def test_listener_sources_have_no_direct_routing_database_lookup(self):
        for callback in (
            matchmaking.matchmaking.on_message,
            matchmaking.matchmaking.on_raw_reaction_add,
            matchmaking.matchmaking.on_raw_reaction_remove,
        ):
            source = inspect.getsource(callback)
            self.assertNotIn('models.Game.get_or_none', source)
            self.assertNotIn('models.Team.related_external_severs', source)
            self.assertIn('load_reaction_game', source)

    async def test_message_seed_uses_snapshot_for_local_and_external_guilds(self):
        for guild_id, should_react in ((300, True), (400, True), (500, False)):
            with self.subTest(guild_id=guild_id):
                cog = self.make_cog()
                cog.load_reaction_game = mock.AsyncMock(return_value=snapshot())
                message = SimpleNamespace(
                    content='Join game 322 by reacting with ⚔️',
                    guild=SimpleNamespace(id=guild_id),
                    add_reaction=mock.AsyncMock(),
                )
                await cog.on_message(message)
                cog.load_reaction_game.assert_awaited_once_with(322)
                if should_react:
                    message.add_reaction.assert_awaited_once_with(
                        matchmaking.settings.emoji_join_game
                    )
                else:
                    message.add_reaction.assert_not_awaited()

    async def test_message_seed_skips_missing_nonpending_and_failed_reads(self):
        for loaded in (
            snapshot(exists=False, guild_id=None, is_pending=False),
            snapshot(is_pending=False),
            RuntimeError('database down'),
        ):
            with self.subTest(loaded=loaded):
                cog = self.make_cog()
                if isinstance(loaded, Exception):
                    cog.load_reaction_game = mock.AsyncMock(side_effect=loaded)
                else:
                    cog.load_reaction_game = mock.AsyncMock(return_value=loaded)
                message = SimpleNamespace(
                    content='Join game 322 by reacting with ⚔️',
                    guild=SimpleNamespace(id=300),
                    add_reaction=mock.AsyncMock(),
                )
                await cog.on_message(message)
                message.add_reaction.assert_not_awaited()

    async def test_reaction_lookup_failure_is_visible_and_clears_join_marker(self):
        guild = SimpleNamespace(id=300, name='Guild')
        channel = SimpleNamespace(
            id=20,
            name='bot',
            send=mock.AsyncMock(),
            fetch_message=mock.AsyncMock(),
        )
        guild.get_channel = lambda _channel_id: channel
        member = SimpleNamespace(
            id=200,
            display_name='Member',
            mention='<@200>',
            guild=guild,
        )
        message = SimpleNamespace(
            author=SimpleNamespace(id=123),
            content='Join game 322 by reacting with ⚔️',
            remove_reaction=mock.AsyncMock(),
        )
        channel.fetch_message.return_value = message
        payload = SimpleNamespace(
            emoji=SimpleNamespace(name=matchmaking.settings.emoji_join_game),
            user_id=member.id,
            message_id=10,
            channel_id=channel.id,
            guild_id=guild.id,
            member=member,
        )
        cog = self.make_cog()
        cog.load_reaction_game = mock.AsyncMock(
            side_effect=RuntimeError('database down')
        )
        await cog.on_raw_reaction_add(payload)
        self.assertNotIn((10, 200), cog.ignorable_join_reactions)
        self.assertIn('could not be loaded', channel.send.await_args.args[0])
        message.remove_reaction.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
