"""Focused P8.23 league team-channel cache coverage."""

import asyncio
from contextlib import nullcontext
from dataclasses import FrozenInstanceError
import inspect
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.league_channel_workers')
league = import_offline_runtime('modules.league')
games = import_offline_runtime('modules.games')
game_start = import_offline_runtime('modules.game_start')


def request():
    return workers.LeagueChannelCacheRequest(guild_id=300)


def result(*, channels=(800, 801)):
    return workers.LeagueChannelCacheResult(
        guild_id=300,
        channel_ids=tuple(channels),
    )


class WorkerTests(unittest.TestCase):
    def test_request_and_result_are_frozen_primitives(self):
        item = request()
        with self.assertRaises(FrozenInstanceError):
            item.guild_id = 1
        loaded = result()
        self.assertEqual(loaded.channel_count, 2)
        with self.assertRaises(FrozenInstanceError):
            loaded.channel_ids = ()

    def test_load_owns_connection_and_preserves_complete_snapshot(self):
        connection = mock.MagicMock()
        connection.__enter__.return_value = None
        connection.__exit__.return_value = False
        with mock.patch.object(
            workers.models.db,
            'connection_context',
            return_value=connection,
        ), mock.patch.object(
            workers,
            '_league_team_ids',
            return_value=(11, 12),
        ) as teams, mock.patch.object(
            workers,
            '_league_channel_ids',
            return_value=(800, 801),
        ) as channels:
            loaded = workers.load_league_team_channels(request())
        self.assertEqual(loaded, result())
        teams.assert_called_once_with(300)
        channels.assert_called_once_with(guild_id=300, team_ids=(11, 12))
        connection.__enter__.assert_called_once()

    def test_no_teams_skips_channel_query(self):
        with mock.patch.object(
            workers.models.db,
            'connection_context',
            return_value=nullcontext(),
        ), mock.patch.object(
            workers,
            '_league_team_ids',
            return_value=(),
        ), mock.patch.object(
            workers,
            '_league_channel_ids',
            wraps=workers._league_channel_ids,
        ) as channels:
            loaded = workers.load_league_team_channels(request())
        self.assertEqual(loaded.channel_ids, ())
        channels.assert_called_once_with(guild_id=300, team_ids=())

    def test_row_limit_fails_closed_without_truncation(self):
        rows = ((index,) for index in range(
            workers.MAX_LEAGUE_TEAM_CHANNELS + 1
        ))
        with self.assertRaises(workers.LeagueChannelCacheError):
            workers._bounded_channel_ids(rows)

    def test_slow_load_keeps_event_loop_responsive(self):
        async def scenario():
            started = threading.Event()
            release = threading.Event()

            def slow(_request):
                started.set()
                release.wait(timeout=2)
                return result()

            with mock.patch.object(
                workers,
                'load_league_team_channels',
                side_effect=slow,
            ):
                task = asyncio.create_task(
                    workers.run_load_league_team_channels(request())
                )
                while not started.is_set():
                    await asyncio.sleep(0.001)
                responsive = not task.done()
                release.set()
                loaded = await task
            return responsive, loaded

        responsive, loaded = asyncio.run(scenario())
        self.assertTrue(responsive)
        self.assertEqual(loaded.channel_ids, (800, 801))

    def test_cancelled_load_drains_before_propagating(self):
        async def scenario():
            started = threading.Event()
            release = threading.Event()

            def slow(_request):
                started.set()
                release.wait(timeout=2)
                return result()

            with mock.patch.object(
                workers,
                'load_league_team_channels',
                side_effect=slow,
            ):
                task = asyncio.create_task(
                    workers.run_load_league_team_channels(request())
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


class CacheAndCallerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_cache = league.league_team_channels

    async def asyncTearDown(self):
        league.league_team_channels = self.original_cache

    async def test_refresh_replaces_cache_only_after_success(self):
        league.league_team_channels = [700]
        with mock.patch.object(
            league.league_channel_workers,
            'run_load_league_team_channels',
            new=mock.AsyncMock(return_value=result()),
        ) as load:
            count = await league.refresh_league_team_channels(300)
        self.assertEqual(count, 2)
        self.assertEqual(league.league_team_channels, [800, 801])
        self.assertEqual(load.await_args.args[0], request())

        league.league_team_channels = [900]
        with mock.patch.object(
            league.league_channel_workers,
            'run_load_league_team_channels',
            new=mock.AsyncMock(side_effect=RuntimeError('down')),
        ):
            with self.assertRaises(RuntimeError):
                await league.refresh_league_team_channels(300)
        self.assertEqual(league.league_team_channels, [900])

    async def test_on_ready_loads_draft_then_channel_cache(self):
        events = []
        bot = SimpleNamespace(user=SimpleNamespace(id=479029527553638401))
        cog = league.league(bot)

        async def draft(guild_id):
            events.append(('draft', guild_id))
            return SimpleNamespace(announcement_message_id=123)

        async def refresh(guild_id):
            events.append(('channels', guild_id))
            return 2

        with mock.patch.object(league.utilities, 'connect'), mock.patch.object(
            league.settings,
            'server_ids',
            {'polychampions': 300, 'test': 301},
        ), mock.patch.object(
            league.settings,
            'guild_configuration_ready',
            return_value=True,
        ), mock.patch.object(
            league.league_free_agents_workers,
            'run_load_draft_state',
            side_effect=draft,
        ), mock.patch.object(
            league,
            'refresh_league_team_channels',
            side_effect=refresh,
        ):
            await cog.on_ready()
        self.assertEqual(events, [('draft', 301), ('channels', 300)])
        self.assertEqual(cog.announcement_message, 123)

    def test_every_cache_caller_awaits_new_helper(self):
        league_source = inspect.getsource(league.league.on_ready)
        start_source = inspect.getsource(game_start.publish_start_result)
        legacy_source = inspect.getsource(games.post_newgame_messaging)
        for source in (league_source, start_source, legacy_source):
            self.assertIn('await ', source)
            self.assertIn('refresh_league_team_channels', source)
            self.assertNotIn('populate_league_team_channels', source)


if __name__ == '__main__':
    unittest.main()
