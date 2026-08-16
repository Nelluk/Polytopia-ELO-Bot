"""Focused coverage for P8.12 league season records."""

import asyncio
from contextlib import AbstractContextManager, asynccontextmanager
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

import discord

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.league_season_workers')
service = import_offline_runtime('modules.league_season')
views = import_offline_runtime('modules.league_season_views')
league = import_offline_runtime('modules.league')


class FakeDatabase:
    def __init__(self):
        self.opened = 0
        self.closed = 0

    def connection_context(self):
        database = self

        class Context(AbstractContextManager):
            def __enter__(self):
                database.opened += 1
                return database

            def __exit__(self, exc_type, exc_value, traceback):
                database.closed += 1
                return False

        return Context()


def request(**overrides):
    values = dict(
        guild_id=300,
        requester_id=10,
        season=17,
        league_scope=True,
        channel_allowed=True,
        tier_labels=((1, 'Platinum'), (2, 'Gold'), (3, 'Silver')),
    )
    values.update(overrides)
    return workers.LeagueSeasonRequest(**values)


def row(team_id, tier, name, *, regular=0, postseason=0):
    return SimpleNamespace(
        id=team_id,
        name=name,
        emoji='⚔️',
        game_league_tier=tier,
        regular_wins=regular,
        regular_losses=1,
        regular_incomplete=2,
        postseason_wins=postseason,
        postseason_losses=3,
        postseason_incomplete=4,
    )


def result(*, season=17, team_count=2):
    teams = tuple(
        workers.LeagueSeasonTeamRow(
            team_id=index,
            team_name=f'Team {index}',
            team_emoji='⚔️',
            regular_wins=index,
            regular_losses=1,
            regular_incomplete=0,
            postseason_wins=0,
            postseason_losses=0,
            postseason_incomplete=0,
        )
        for index in range(1, team_count + 1)
    )
    return workers.LeagueSeasonResult(
        guild_id=300,
        requester_id=10,
        season=season,
        title=f'Season {season} Records',
        tiers=(workers.LeagueSeasonTier(2, 'Gold', teams),),
        historical_note=None,
        rows_truncated=False,
    )


class RegistrationTests(unittest.TestCase):
    def test_native_shape_and_prefix_aliases(self):
        root = next(
            command for command in league.league.__cog_app_commands__
            if command.name == 'league'
        )
        command = root.get_command('season')
        self.assertIsNotNone(command)
        self.assertEqual(
            [
                (parameter.name, parameter.required, parameter.type)
                for parameter in command.parameters
            ],
            [('season', False, discord.AppCommandOptionType.integer)],
        )
        prefix = {command.name: command for command in league.league.__cog_commands__}
        self.assertEqual(
            prefix['season'].aliases,
            ['jrseason', 'ps', 'js', 'seasonjr'],
        )

    def test_request_and_results_are_frozen_primitives(self):
        with self.assertRaises(FrozenInstanceError):
            request().guild_id = 1
        loaded = result()
        self.assertIsInstance(loaded.tiers, tuple)
        self.assertIsInstance(loaded.tiers[0].teams, tuple)

    def test_configured_tier_ids_are_not_list_indexes(self):
        with mock.patch.object(
            service.settings,
            'league_tiers',
            [(4, 'Bronze'), (7, 'Paper')],
        ):
            self.assertEqual(service._tier_labels(), ((4, 'Bronze'), (7, 'Paper')))


class WorkerAndRenderingTests(unittest.TestCase):
    def test_scope_failures_happen_before_connection(self):
        database = FakeDatabase()
        for blocked in (
            request(league_scope=False),
            request(channel_allowed=False),
        ):
            with mock.patch.object(workers.models, 'db', database):
                with self.assertRaises(workers.LeagueSeasonPermissionError):
                    workers.load_league_season(blocked)
        self.assertEqual(database.opened, 0)

    def test_historical_seasons_do_not_connect(self):
        database = FakeDatabase()
        with mock.patch.object(workers.models, 'db', database):
            loaded = workers.load_league_season(request(season=1))
        self.assertIn('dark ages', loaded.historical_note)
        self.assertEqual(loaded.tiers, ())
        self.assertEqual(database.opened, 0)

    def test_worker_owns_connection_groups_tiers_and_preserves_query_order(self):
        database = FakeDatabase()
        rows = (
            row(2, 2, 'Second', regular=3),
            row(1, 2, 'First', regular=2),
            row(3, 3, 'Junior', postseason=1),
        )
        with mock.patch.object(workers.models, 'db', database), mock.patch.object(
            workers, '_season_query', return_value=rows
        ):
            loaded = workers.load_league_season(request())
        self.assertEqual(database.opened, 1)
        self.assertEqual(database.closed, 1)
        self.assertEqual([tier.tier_name for tier in loaded.tiers], ['Gold', 'Silver'])
        self.assertEqual(
            [team.team_name for team in loaded.tiers[0].teams],
            ['Second', 'First'],
        )

    def test_game_tier_does_not_collide_with_nullable_team_tier(self):
        database = FakeDatabase()
        collision_row = row(1, 2, 'Historical Team')
        collision_row.league_tier = None
        with mock.patch.object(workers.models, 'db', database), mock.patch.object(
            workers, '_season_query', return_value=(collision_row,)
        ):
            loaded = workers.load_league_season(request())
        self.assertEqual(loaded.tiers[0].tier_number, 2)
        self.assertEqual(loaded.tiers[0].teams[0].team_name, 'Historical Team')

    def test_legacy_early_season_names_and_dense_counts(self):
        early = result(season=16, team_count=1)
        early = workers.LeagueSeasonResult(
            **{**early.__dict__, 'tiers': (
                workers.LeagueSeasonTier(2, 'Pro', early.tiers[0].teams),
                workers.LeagueSeasonTier(3, 'Jr', early.tiers[0].teams),
            )}
        )
        output = service.legacy_text(early)
        self.assertIn('**Pro Tier**', output)
        self.assertIn('**Jr Tier**', output)
        self.assertIn('1W', output)

    def test_native_workspace_paginates_without_more_reads(self):
        loaded = result(team_count=13)
        workspace = views.LeagueSeasonWorkspace(result=loaded, requester_id=10)
        self.assertEqual(workspace.page_count, 2)
        self.assertIn('Team 1', workspace.pages[0])
        self.assertIn('Team 13', workspace.pages[1])
        payload = workspace.to_components()
        self.assertEqual(payload[0]['type'], 17)

    def test_slow_worker_keeps_event_loop_responsive(self):
        async def run_case():
            started = threading.Event()
            release = threading.Event()

            def slow(_request):
                started.set()
                release.wait(timeout=2)
                return result()

            with mock.patch.object(workers, 'load_league_season', side_effect=slow):
                task = asyncio.create_task(workers.run_league_season(request()))
                deadline = asyncio.get_running_loop().time() + 1
                while not started.is_set():
                    if asyncio.get_running_loop().time() >= deadline:
                        self.fail('league season worker did not start')
                    await asyncio.sleep(0.001)
                heartbeat = 0
                for _ in range(3):
                    await asyncio.sleep(0.01)
                    heartbeat += 1
                release.set()
                await asyncio.sleep(0.05)
                loaded = await task
            return loaded, heartbeat

        loaded, heartbeat = asyncio.run(run_case())
        self.assertEqual(loaded.season, 17)
        self.assertEqual(heartbeat, 3)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def root():
        return next(
            command for command in league.league.__cog_app_commands__
            if command.name == 'league'
        )

    @staticmethod
    def interaction():
        actor = SimpleNamespace(id=10, roles=())
        return SimpleNamespace(
            guild=SimpleNamespace(id=300),
            user=actor,
            channel_id=400,
            channel=SimpleNamespace(send=mock.AsyncMock()),
            response=SimpleNamespace(
                defer=mock.AsyncMock(),
                send_message=mock.AsyncMock(),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
            delete_original_response=mock.AsyncMock(),
        )

    async def test_native_defers_before_worker_and_publishes_publicly(self):
        cog = league.league.__new__(league.league)
        interaction = self.interaction()
        events = []

        async def defer(**kwargs):
            events.append(('defer', kwargs))

        async def load(_request):
            events.append(('worker', {}))
            return result()

        async def publish(_interaction, _view):
            events.append(('public', {}))

        interaction.response.defer.side_effect = defer
        command = self.root().get_command('season')
        with mock.patch.object(service, 'native_access_error', return_value=None), mock.patch.object(
            workers, 'run_league_season', new=mock.AsyncMock(side_effect=load)
        ), mock.patch.object(views, 'publish', new=mock.AsyncMock(side_effect=publish)):
            await command.callback(cog, interaction, 17)
        self.assertEqual([event[0] for event in events], ['defer', 'worker', 'public'])
        self.assertEqual(events[0][1], {'ephemeral': True})

    async def test_native_failure_remains_private(self):
        cog = league.league.__new__(league.league)
        interaction = self.interaction()
        command = self.root().get_command('season')
        with mock.patch.object(service, 'native_access_error', return_value=None), mock.patch.object(
            workers,
            'run_league_season',
            new=mock.AsyncMock(side_effect=workers.LeagueSeasonError('no records')),
        ):
            await command.callback(cog, interaction, None)
        interaction.followup.send.assert_awaited_once_with(
            'no records', ephemeral=True
        )

    async def test_prefix_invalid_input_and_shared_worker_output(self):
        cog = league.league.__new__(league.league)

        @asynccontextmanager
        async def typing():
            yield

        ctx = SimpleNamespace(
            author=SimpleNamespace(id=10, roles=()),
            guild=SimpleNamespace(id=300),
            channel=SimpleNamespace(id=400),
            prefix='!',
            invoked_with='season',
            send=mock.AsyncMock(),
            typing=typing,
        )
        prefix = next(
            command for command in league.league.__cog_commands__
            if command.name == 'season'
        )
        await prefix.callback(cog, ctx, season='later')
        self.assertIn('!season 13', ctx.send.await_args.args[0])

        ctx.send.reset_mock()
        with mock.patch.object(
            workers, 'run_league_season', new=mock.AsyncMock(return_value=result())
        ), mock.patch.object(
            league.utilities, 'buffered_send', new=mock.AsyncMock()
        ) as buffered:
            await prefix.callback(cog, ctx, season='17')
        buffered.assert_awaited_once()
        self.assertIn('Season 17 Records', buffered.await_args.kwargs['content'])


if __name__ == '__main__':
    unittest.main()
