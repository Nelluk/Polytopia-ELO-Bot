"""Offline coverage for activity and squad leaderboard units."""

import asyncio
from contextlib import AbstractContextManager
import datetime
from types import SimpleNamespace
import unittest
from unittest import mock

import discord

from tests.test_newgame_worker import import_offline_runtime


leaderboard_workers = import_offline_runtime(
    'modules.leaderboard_workers'
)
leaderboard_views = import_offline_runtime('modules.leaderboard_views')
games = import_offline_runtime('modules.games')


def app_group(cog_class, name):
    return next(
        command
        for command in cog_class.__cog_app_commands__
        if command.name == name
    )


class FakeDatabase:
    def __init__(self):
        self.opened = 0
        self.closed = 0

    def connection_context(self):
        database = self

        class ConnectionContext(AbstractContextManager):
            def __enter__(self):
                database.opened += 1
                return database

            def __exit__(self, exc_type, exc_value, traceback):
                database.closed += 1

        return ConnectionContext()


class FakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def join(self, *args, **kwargs):
        return self

    def where(self, *args, **kwargs):
        return self

    def group_by(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def count(self):
        return len(self.rows)

    def __getitem__(self, item):
        return self.rows[item]


def activity_result(count=12, view='server-30-days'):
    return leaderboard_workers.ActivityLeaderboardResult(
        title='Activity',
        total_players=count,
        view=view,
        rows=tuple(
            leaderboard_workers.ActivityLeaderboardRow(
                rank=index,
                name=f'Player {index}',
                elo=1400 - index,
                games=index * 2,
                team_emoji='🏹' if view == 'server-30-days' else '',
            )
            for index in range(1, count + 1)
        ),
    )


def squad_result(count=12):
    return leaderboard_workers.SquadLeaderboardResult(
        title='Squad Leaderboard',
        total_squads=count,
        rows=tuple(
            leaderboard_workers.SquadLeaderboardRow(
                rank=index,
                squad_id=100 + index,
                squad_name=f'Squad {index}',
                member_names=('Alpha', 'Beta'),
                member_emojis=('🏹', '🛡️'),
                elo=1500 - index,
                wins=index,
                losses=index // 2,
            )
            for index in range(1, count + 1)
        ),
    )


class ActivityLeaderboardWorkerTests(unittest.TestCase):
    def test_server_activity_owns_connection_and_returns_primitives(self):
        database = FakeDatabase()
        query = FakeQuery([
            SimpleNamespace(
                name='Alpha',
                elo_moonrise=1450,
                count=12,
                team=SimpleNamespace(emoji='🏹'),
            ),
        ])
        request = leaderboard_workers.ActivityLeaderboardRequest(
            guild_id=300,
            view='server-30-days',
            recent_cutoff=datetime.datetime(2026, 6, 30),
        )

        with mock.patch.object(
            leaderboard_workers.models,
            'db',
            database,
        ), mock.patch.object(
            leaderboard_workers.models.Player,
            'select',
            return_value=query,
        ):
            result = leaderboard_workers.load_activity_leaderboard(
                request,
            )

        self.assertEqual((database.opened, database.closed), (1, 1))
        self.assertEqual(result.total_players, 1)
        self.assertEqual(result.view, 'server-30-days')
        self.assertEqual(result.rows[0].team_emoji, '🏹')
        self.assertEqual(result.rows[0].games, 12)

    def test_global_activity_has_no_team_dependency(self):
        database = FakeDatabase()
        query = FakeQuery([
            SimpleNamespace(
                name='Global Alpha',
                elo_moonrise=1550,
                count=40,
            ),
        ])
        request = leaderboard_workers.ActivityLeaderboardRequest(
            guild_id=300,
            view='global-all-time',
        )

        with mock.patch.object(
            leaderboard_workers.models,
            'db',
            database,
        ), mock.patch.object(
            leaderboard_workers.models.DiscordMember,
            'select',
            return_value=query,
        ):
            result = leaderboard_workers.load_activity_leaderboard(
                request,
            )

        self.assertEqual((database.opened, database.closed), (1, 1))
        self.assertEqual(result.view, 'global-all-time')
        self.assertEqual(result.rows[0].team_emoji, '')
        self.assertEqual(result.rows[0].games, 40)


class SquadLeaderboardWorkerTests(unittest.TestCase):
    def test_squad_worker_returns_immutable_member_snapshot(self):
        database = FakeDatabase()
        received = {}
        members = [
            SimpleNamespace(
                name='Alpha',
                team=SimpleNamespace(emoji='🏹'),
            ),
            SimpleNamespace(name='Beta', team=None),
        ]
        squad = SimpleNamespace(
            id=44,
            name='The Testers',
            elo=1510,
            get_record=lambda: (8, 3),
            get_members=lambda: members,
        )

        def leaderboard(**kwargs):
            received.update(kwargs)
            return FakeQuery([squad])

        request = leaderboard_workers.SquadLeaderboardRequest(
            guild_id=300,
            period='all-time',
            active_cutoff=datetime.datetime(2025, 1, 1),
        )
        with mock.patch.object(
            leaderboard_workers.models,
            'db',
            database,
        ), mock.patch.object(
            leaderboard_workers.models.Squad,
            'leaderboard',
            side_effect=leaderboard,
        ):
            result = leaderboard_workers.load_squad_leaderboard(request)

        self.assertEqual((database.opened, database.closed), (1, 1))
        self.assertEqual(received['date_cutoff'], datetime.date.min)
        self.assertEqual(result.total_squads, 1)
        self.assertEqual(result.rows[0].member_names, ('Alpha', 'Beta'))
        self.assertEqual(result.rows[0].member_emojis, ('🏹',))
        self.assertEqual((result.rows[0].wins, result.rows[0].losses), (8, 3))


class ActivitySquadCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_prefix_aliases_and_exact_view_mapping(self):
        activity = next(
            command
            for command in games.polygames.__cog_commands__
            if command.name == 'lbrecent'
        )
        squads = next(
            command
            for command in games.polygames.__cog_commands__
            if command.name == 'lbsquad'
        )
        self.assertEqual(
            set(activity.aliases),
            {'recent', 'active', 'lbactivealltime'},
        )
        self.assertEqual(set(squads.aliases), {'squadlb'})

        for invoked_with in ('lbrecent', 'recent', 'active'):
            request = games.polygames._activity_leaderboard_request(
                guild_id=300,
                invoked_with=invoked_with,
            )
            self.assertEqual(request.view, 'server-30-days')
        request = games.polygames._activity_leaderboard_request(
            guild_id=300,
            invoked_with='lbactivealltime',
        )
        self.assertEqual(request.view, 'global-all-time')

        self.assertEqual(
            games.polygames._squad_leaderboard_request(300).period,
            'current',
        )
        self.assertEqual(
            games.polygames._squad_leaderboard_request(
                300,
                'ALLTIME',
            ).period,
            'all-time',
        )

    def test_typed_slash_commands_are_registered(self):
        group = app_group(games.polygames, 'leaderboard')
        activity = group.get_command('activity')
        squads = group.get_command('squads')

        self.assertEqual(
            [choice.value for choice in activity.parameters[0].choices],
            ['server-30-days', 'global-all-time'],
        )
        self.assertEqual(
            [choice.name for choice in activity.parameters[0].choices],
            [
                'This server — past 30 days',
                'Global — all time',
            ],
        )
        self.assertEqual(
            [choice.value for choice in squads.parameters[0].choices],
            ['current', 'all-time'],
        )
        self.assertEqual(
            [choice.name for choice in squads.parameters[0].choices],
            ['Current eligibility', 'All time'],
        )

    async def test_activity_slash_defers_checks_loads_and_edits(self):
        events = []

        async def defer():
            events.append('defer')

        async def can_run(ctx):
            events.append(('checks', ctx.invoked_with))
            return True

        async def load(request):
            events.append(('load', request.view))
            return activity_result(view=request.view)

        async def edit_original_response(**kwargs):
            events.append('edit')
            self.assertIsInstance(
                kwargs['view'],
                leaderboard_views.ActivityLeaderboardView,
            )
            return SimpleNamespace(edit=mock.AsyncMock())

        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=defer),
            guild=SimpleNamespace(id=300),
            user=SimpleNamespace(id=100),
            channel_id=400,
            edit_original_response=edit_original_response,
        )
        context = SimpleNamespace()
        cog = games.polygames.__new__(games.polygames)
        cog.bot = SimpleNamespace()
        cog.lbrecent = SimpleNamespace(can_run=can_run)
        cog._load_activity_leaderboard = load
        command = app_group(
            games.polygames,
            'leaderboard',
        ).get_command('activity')

        with mock.patch.object(
            games.commands.Context,
            'from_interaction',
            new=mock.AsyncMock(return_value=context),
        ), mock.patch.object(
            games.settings,
            'guild_setting',
            return_value='$',
        ):
            await command.callback(
                cog,
                interaction,
                'global-all-time',
            )

        self.assertEqual(
            events,
            [
                'defer',
                ('checks', 'lbactivealltime'),
                ('load', 'global-all-time'),
                'edit',
            ],
        )

    async def test_squad_slash_defers_checks_loads_and_edits(self):
        events = []

        async def defer():
            events.append('defer')

        async def can_run(ctx):
            events.append('checks')
            return True

        async def load(request):
            events.append(('load', request.period))
            return squad_result()

        async def edit_original_response(**kwargs):
            events.append('edit')
            self.assertIsInstance(
                kwargs['view'],
                leaderboard_views.SquadLeaderboardView,
            )
            return SimpleNamespace(edit=mock.AsyncMock())

        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=defer),
            guild=SimpleNamespace(id=300),
            user=SimpleNamespace(id=100),
            channel_id=400,
            edit_original_response=edit_original_response,
        )
        context = SimpleNamespace()
        cog = games.polygames.__new__(games.polygames)
        cog.bot = SimpleNamespace()
        cog.lbsquad = SimpleNamespace(can_run=can_run)
        cog._load_squad_leaderboard = load
        command = app_group(
            games.polygames,
            'leaderboard',
        ).get_command('squads')

        with mock.patch.object(
            games.commands.Context,
            'from_interaction',
            new=mock.AsyncMock(return_value=context),
        ), mock.patch.object(
            games.settings,
            'guild_setting',
            return_value='$',
        ):
            await command.callback(cog, interaction, 'all-time')

        self.assertEqual(
            events,
            ['defer', 'checks', ('load', 'all-time'), 'edit'],
        )

    async def test_public_component_pages_are_deterministic(self):
        activity = activity_result()
        activity_view = leaderboard_views.ActivityLeaderboardView(
            activity,
            requester_id=100,
        )
        squad = squad_result()
        squad_view = leaderboard_views.SquadLeaderboardView(
            squad,
            requester_id=100,
        )

        self.assertEqual(activity_view.page_count, 2)
        self.assertEqual(squad_view.page_count, 2)
        self.assertEqual(
            len(
                leaderboard_views.activity_leaderboard_embed(
                    activity,
                    1,
                ).fields
            ),
            2,
        )
        squad_embed = leaderboard_views.squad_leaderboard_embed(
            squad,
            1,
        )
        self.assertEqual(len(squad_embed.fields), 2)
        self.assertIn('#111', squad_embed.fields[0].value)


class SharedLeaderboardExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_activity_and_squad_share_bounded_responsive_executor(self):
        started = 0
        release = asyncio.Event()

        async def fake_run_in_executor(executor, call):
            nonlocal started
            self.assertIs(
                executor,
                leaderboard_workers._leaderboard_read_executor,
            )
            started += 1
            await release.wait()
            return call()

        activity_request = leaderboard_workers.ActivityLeaderboardRequest(
            guild_id=300,
            view='global-all-time',
        )
        squad_request = leaderboard_workers.SquadLeaderboardRequest(
            guild_id=300,
            period='all-time',
        )
        loop = asyncio.get_running_loop()

        with mock.patch.object(
            loop,
            'run_in_executor',
            side_effect=fake_run_in_executor,
        ), mock.patch.object(
            leaderboard_workers,
            'load_activity_leaderboard',
            return_value=activity_result(1),
        ), mock.patch.object(
            leaderboard_workers,
            'load_squad_leaderboard',
            return_value=squad_result(1),
        ):
            tasks = [
                asyncio.create_task(
                    leaderboard_workers.run_activity_leaderboard(
                        activity_request,
                    )
                ),
                asyncio.create_task(
                    leaderboard_workers.run_squad_leaderboard(
                        squad_request,
                    )
                ),
            ]
            await asyncio.sleep(0)
            self.assertEqual(started, 2)
            heartbeat = False

            async def tick():
                nonlocal heartbeat
                await asyncio.sleep(0)
                heartbeat = True

            await tick()
            self.assertTrue(heartbeat)
            release.set()
            await asyncio.gather(*tasks)
