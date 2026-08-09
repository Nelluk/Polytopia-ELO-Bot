"""Focused offline coverage for automatic open-game list broadcasts."""

import asyncio
from contextlib import AbstractContextManager
from dataclasses import FrozenInstanceError
import datetime
import inspect
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

import discord

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.game_list_broadcast_workers')
service = import_offline_runtime('modules.game_list_broadcasts')
matchmaking = import_offline_runtime('modules.matchmaking')

NOW = datetime.datetime(2026, 8, 9, 12, 0, 0)


def request(*, ranked_filter=2, limit=12):
    return workers.GameListBroadcastRequest(
        guild_id=10,
        ranked_filter=ranked_filter,
        as_of=NOW,
        limit=limit,
    )


def row(game_id=77, *, ranked=True, notes='Open to all'):
    return workers.GameListBroadcastRow(
        game_id=game_id,
        host_name='Host',
        size='2v2',
        players=1,
        capacity=4,
        expiration='23H',
        ranked=ranked,
        notes=notes,
    )


def snapshot(*rows, ranked_filter=2):
    return workers.GameListBroadcastSnapshot(
        guild_id=10,
        ranked_filter=ranked_filter,
        rows=tuple(rows),
        skipped_game_ids=(),
    )


class Database:
    def __init__(self):
        self.opened = 0
        self.closed = 0

    def connection_context(self):
        database = self

        class Connection(AbstractContextManager):
            def __enter__(self):
                database.opened += 1
                return self

            def __exit__(self, *_args):
                database.closed += 1

        return Connection()


class BroadcastWorkerTests(unittest.IsolatedAsyncioTestCase):
    def test_request_snapshot_and_rows_are_frozen_primitives(self):
        value = snapshot(row())
        with self.assertRaises(FrozenInstanceError):
            value.guild_id = 11
        with self.assertRaises(FrozenInstanceError):
            value.rows[0].host_name = 'Changed'
        self.assertEqual(value.rows[0].game_id, 77)

    def test_worker_owns_connection_forwards_filters_and_freezes_rows(self):
        database = Database()
        query = mock.Mock(return_value=(
            SimpleNamespace(
                id=90,
                capacity=lambda: (2, 4),
                creating_player=lambda: SimpleNamespace(name='Newest Host'),
                size_string=lambda: '2v2',
                expiration=NOW + datetime.timedelta(hours=24),
                is_ranked=True,
                notes='First',
            ),
            SimpleNamespace(
                id=89,
                capacity=lambda: (0, 3),
                creating_player=lambda: None,
                size_string=lambda: 'FFA',
                expiration=NOW - datetime.timedelta(hours=1),
                is_ranked=False,
                notes='',
            ),
        ))
        models = SimpleNamespace(
            db=database,
            Game=SimpleNamespace(search_pending=query),
        )
        with mock.patch.object(workers, 'models', models):
            result = workers.load_game_list_broadcast(
                request(ranked_filter=1)
            )

        query.assert_called_once_with(
            status_filter=2,
            ranked_filter=1,
            guild_id=10,
            limit=12,
        )
        self.assertEqual([item.game_id for item in result.rows], [90, 89])
        self.assertEqual(result.rows[0].expiration, '24H')
        self.assertEqual(result.rows[1].expiration, 'Exp')
        self.assertEqual(result.rows[1].host_name, '<Vacant>')
        self.assertEqual(database.opened, 1)
        self.assertEqual(database.closed, 1)

    def test_worker_skips_one_malformed_row_and_validates_bounds(self):
        database = Database()
        bad = SimpleNamespace(id=70, capacity=mock.Mock(side_effect=ValueError()))
        good = SimpleNamespace(
            id=69,
            capacity=lambda: (1, 2),
            creating_player=lambda: SimpleNamespace(name='Good'),
            size_string=lambda: '1v1',
            expiration=NOW + datetime.timedelta(hours=1),
            is_ranked=True,
            notes='',
        )
        models = SimpleNamespace(
            db=database,
            Game=SimpleNamespace(search_pending=lambda **_kwargs: (bad, good)),
        )
        with mock.patch.object(workers, 'models', models):
            result = workers.load_game_list_broadcast(request())
            with self.assertRaises(ValueError):
                workers.load_game_list_broadcast(request(limit=13))
        self.assertEqual(result.skipped_game_ids, (70,))
        self.assertEqual([item.game_id for item in result.rows], [69])

    async def test_executor_keeps_loop_responsive_and_drains_cancellation(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def slow(_request):
            started.set()
            release.wait(timeout=2)
            finished.set()
            return snapshot(row())

        try:
            with mock.patch.object(
                workers,
                'load_game_list_broadcast',
                side_effect=slow,
            ):
                task = asyncio.create_task(
                    workers.run_load_game_list_broadcast(request())
                )
                for _ in range(500):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.001)
                self.assertTrue(started.is_set())
                await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.2)
                task.cancel()
                await asyncio.sleep(0.01)
                self.assertFalse(task.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        finally:
            release.set()
        self.assertTrue(finished.is_set())


class BroadcastServiceTests(unittest.IsolatedAsyncioTestCase):
    def test_renderer_uses_native_crossplay_copy_and_dense_rows(self):
        embed = service.render_game_list(
            snapshot(row(), row(76, ranked=False, notes='')),
            title='Current open games',
        )
        self.assertIn('/game join', embed.title)
        self.assertIn('/game show', embed.title)
        self.assertNotIn('$join', embed.title)
        self.assertNotIn('$game', embed.title)
        rendered = '\n'.join(field.value for field in embed.fields)
        self.assertIn('Open to all', rendered)
        self.assertIn('*Unranked*', rendered)
        self.assertNotIn('Mobile', rendered)
        self.assertNotIn('Steam', rendered)

    async def test_channel_routing_publication_and_purge_tracking(self):
        ranked = SimpleNamespace(
            id=100,
            send=mock.AsyncMock(return_value=SimpleNamespace(id=1000)),
        )
        unranked = SimpleNamespace(
            id=101,
            send=mock.AsyncMock(return_value=SimpleNamespace(id=1001)),
        )
        other = SimpleNamespace(
            id=102,
            send=mock.AsyncMock(return_value=SimpleNamespace(id=1002)),
        )
        channels = {channel.id: channel for channel in (ranked, unranked, other)}
        guild = SimpleNamespace(
            id=10,
            get_channel=lambda channel_id: channels.get(channel_id),
        )
        bot = SimpleNamespace(guilds=[guild], purgable_messages=[(1, 2, 3)])

        def guild_setting(_guild_id, key):
            return {
                'match_challenge_channels': (100, 101, 102),
                'ranked_game_channel': 100,
                'unranked_game_channel': 101,
            }[key]

        loader = mock.AsyncMock(side_effect=(
            snapshot(row(1), ranked_filter=1),
            snapshot(row(2, ranked=False), ranked_filter=0),
            snapshot(row(3), ranked_filter=2),
        ))
        with mock.patch.object(
            service.settings,
            'guild_setting',
            side_effect=guild_setting,
        ), mock.patch.object(
            service.game_list_broadcast_workers,
            'run_load_game_list_broadcast',
            loader,
        ):
            result = await service.broadcast_open_game_lists(
                bot=bot,
                as_of=NOW,
                delete_after=3600,
            )

        filters = [call.args[0].ranked_filter for call in loader.await_args_list]
        self.assertEqual(filters, [1, 0, 2])
        self.assertEqual(
            result.sent_targets,
            ((10, 100, 1000), (10, 101, 1001), (10, 102, 1002)),
        )
        self.assertEqual(bot.purgable_messages[-3:], list(result.sent_targets))
        for channel in channels.values():
            channel.send.assert_awaited_once()
            self.assertEqual(channel.send.await_args.kwargs['delete_after'], 3600)

    async def test_one_channel_failure_does_not_block_later_channel(self):
        failed = SimpleNamespace(
            id=100,
            send=mock.AsyncMock(
                side_effect=discord.HTTPException(
                    SimpleNamespace(status=500, reason='failed'),
                    'failed',
                )
            ),
        )
        good = SimpleNamespace(
            id=101,
            send=mock.AsyncMock(return_value=SimpleNamespace(id=1001)),
        )
        guild = SimpleNamespace(
            id=10,
            get_channel=lambda channel_id: {100: failed, 101: good}[channel_id],
        )
        bot = SimpleNamespace(guilds=[guild], purgable_messages=[])

        def guild_setting(_guild_id, key):
            return {
                'match_challenge_channels': (100, 101),
                'ranked_game_channel': None,
                'unranked_game_channel': None,
            }[key]

        with mock.patch.object(
            service.settings,
            'guild_setting',
            side_effect=guild_setting,
        ), mock.patch.object(
            service.game_list_broadcast_workers,
            'run_load_game_list_broadcast',
            mock.AsyncMock(return_value=snapshot(row())),
        ):
            result = await service.broadcast_open_game_lists(
                bot=bot,
                as_of=NOW,
            )
        self.assertEqual(result.skipped_channel_ids, (100,))
        self.assertEqual(result.sent_targets, ((10, 101, 1001),))
        good.send.assert_awaited_once()

    def test_task_delegates_without_direct_models_or_discord_send(self):
        source = inspect.getsource(matchmaking.matchmaking.task_print_matchlist)
        self.assertIn('broadcast_open_game_lists', source)
        self.assertNotIn('models.', source)
        self.assertNotIn('utilities.connect', source)
        self.assertNotIn('.send(', source)


if __name__ == '__main__':
    unittest.main()
