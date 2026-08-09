"""Focused deleted-channel reference worker and listener coverage."""

import asyncio
from contextlib import nullcontext
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

import peewee

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.channel_reference_workers')
games = import_offline_runtime('modules.games')


def request():
    return workers.ChannelDeleteRequest(
        channel_id=800,
        guild_id=300,
        channel_name='game-channel',
    )


def result(*, sides=(11,), side_games=(101,), games=(102,)):
    return workers.ChannelDeleteResult(
        channel_id=800,
        guild_id=300,
        gameside_ids=tuple(sides),
        side_game_ids=tuple(side_games),
        game_ids=tuple(games),
    )


class WorkerTests(unittest.TestCase):
    def test_request_and_result_are_frozen_primitives(self):
        item = request()
        with self.assertRaises(FrozenInstanceError):
            item.channel_id = 1
        loaded = result()
        self.assertEqual(loaded.cleared_side_count, 1)
        self.assertEqual(loaded.cleared_game_count, 1)
        with self.assertRaises(FrozenInstanceError):
            loaded.guild_id = 1

    def test_cleanup_owns_connection_and_one_atomic_graph(self):
        connection = mock.MagicMock()
        connection.__enter__.return_value = None
        connection.__exit__.return_value = False
        atomic = mock.MagicMock()
        atomic.__enter__.return_value = None
        atomic.__exit__.return_value = False
        with mock.patch.object(
            workers.models.db,
            'connection_context',
            return_value=connection,
        ), mock.patch.object(
            workers.models.db,
            'atomic',
            return_value=atomic,
        ), mock.patch.object(
            workers,
            '_side_reference_rows',
            return_value=((11, 101), (12, 102)),
        ), mock.patch.object(
            workers,
            '_game_reference_ids',
            return_value=(103,),
        ), mock.patch.object(
            workers,
            '_clear_side_references',
            return_value=2,
        ) as clear_sides, mock.patch.object(
            workers,
            '_clear_game_references',
            return_value=1,
        ) as clear_games:
            loaded = workers.clear_deleted_channel_references(request())
        self.assertEqual(loaded.gameside_ids, (11, 12))
        self.assertEqual(loaded.side_game_ids, (101, 102))
        self.assertEqual(loaded.game_ids, (103,))
        clear_sides.assert_called_once_with(800, (11, 12))
        clear_games.assert_called_once_with(800, (103,))
        connection.__enter__.assert_called_once()
        atomic.__enter__.assert_called_once()

    def test_unreferenced_channel_is_transactional_noop(self):
        with mock.patch.object(
            workers.models.db,
            'connection_context',
            return_value=nullcontext(),
        ), mock.patch.object(
            workers.models.db,
            'atomic',
            return_value=nullcontext(),
        ), mock.patch.object(
            workers,
            '_side_reference_rows',
            return_value=(),
        ), mock.patch.object(
            workers,
            '_game_reference_ids',
            return_value=(),
        ), mock.patch.object(
            workers,
            '_clear_side_references',
        ) as clear_sides, mock.patch.object(
            workers,
            '_clear_game_references',
        ) as clear_games:
            loaded = workers.clear_deleted_channel_references(request())
        self.assertEqual(loaded.cleared_side_count, 0)
        self.assertEqual(loaded.cleared_game_count, 0)
        clear_sides.assert_not_called()
        clear_games.assert_not_called()

    def test_side_conflict_stops_full_game_update(self):
        with mock.patch.object(
            workers.models.db,
            'connection_context',
            return_value=nullcontext(),
        ), mock.patch.object(
            workers.models.db,
            'atomic',
            return_value=nullcontext(),
        ), mock.patch.object(
            workers,
            '_side_reference_rows',
            return_value=((11, 101),),
        ), mock.patch.object(
            workers,
            '_game_reference_ids',
            return_value=(102,),
        ), mock.patch.object(
            workers,
            '_clear_side_references',
            return_value=0,
        ), mock.patch.object(
            workers,
            '_clear_game_references',
        ) as clear_games:
            with self.assertRaises(workers.ChannelReferenceConflictError):
                workers.clear_deleted_channel_references(request())
        clear_games.assert_not_called()

    def test_full_game_conflict_rejects_transaction(self):
        with mock.patch.object(
            workers.models.db,
            'connection_context',
            return_value=nullcontext(),
        ), mock.patch.object(
            workers.models.db,
            'atomic',
            return_value=nullcontext(),
        ), mock.patch.object(
            workers,
            '_side_reference_rows',
            return_value=((11, 101),),
        ), mock.patch.object(
            workers,
            '_game_reference_ids',
            return_value=(102,),
        ), mock.patch.object(
            workers,
            '_clear_side_references',
            return_value=1,
        ), mock.patch.object(
            workers,
            '_clear_game_references',
            return_value=0,
        ):
            with self.assertRaises(workers.ChannelReferenceConflictError):
                workers.clear_deleted_channel_references(request())

    def test_slow_cleanup_keeps_event_loop_responsive(self):
        async def scenario():
            started = threading.Event()
            release = threading.Event()

            def slow(_request):
                started.set()
                release.wait(timeout=2)
                return result()

            with mock.patch.object(
                workers,
                'clear_deleted_channel_references',
                side_effect=slow,
            ):
                task = asyncio.create_task(
                    workers.run_channel_reference_cleanup(request())
                )
                while not started.is_set():
                    await asyncio.sleep(0.001)
                responsive = not task.done()
                release.set()
                loaded = await task
            return responsive, loaded

        responsive, loaded = asyncio.run(scenario())
        self.assertTrue(responsive)
        self.assertEqual(loaded.channel_id, 800)

    def test_cancelled_cleanup_drains_before_propagating(self):
        async def scenario():
            started = threading.Event()
            release = threading.Event()

            def slow(_request):
                started.set()
                release.wait(timeout=2)
                return result()

            with mock.patch.object(
                workers,
                'clear_deleted_channel_references',
                side_effect=slow,
            ):
                task = asyncio.create_task(
                    workers.run_channel_reference_cleanup(request())
                )
                while not started.is_set():
                    await asyncio.sleep(0.001)
                task.cancel()
                await asyncio.sleep(0.005)
                still_draining = not task.done()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            return still_draining

        self.assertTrue(asyncio.run(scenario()))


class ListenerTests(unittest.IsolatedAsyncioTestCase):
    def channel(self):
        return SimpleNamespace(
            id=800,
            name='game-channel',
            guild=SimpleNamespace(id=300),
        )

    async def test_listener_submits_only_primitive_snapshot(self):
        channel = self.channel()
        cog = games.polygames.__new__(games.polygames)
        with mock.patch.object(
            games.channel_reference_workers,
            'run_channel_reference_cleanup',
            new=mock.AsyncMock(return_value=result()),
        ) as run:
            await cog.on_guild_channel_delete(channel)
        submitted = run.await_args.args[0]
        self.assertEqual(submitted, request())
        self.assertFalse(hasattr(submitted, '_state'))

    async def test_database_failure_is_contained(self):
        channel = self.channel()
        cog = games.polygames.__new__(games.polygames)
        with mock.patch.object(
            games.channel_reference_workers,
            'run_channel_reference_cleanup',
            new=mock.AsyncMock(side_effect=peewee.OperationalError('down')),
        ):
            await cog.on_guild_channel_delete(channel)


if __name__ == '__main__':
    unittest.main()
