"""Offline coverage for the bounded player leaderboard unit."""

import asyncio
from contextlib import AbstractContextManager
import datetime
from itertools import product
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import discord
from discord.ext import commands

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


def result_with_rows(count=23):
    rows = tuple(
        leaderboard_workers.PlayerLeaderboardRow(
            rank=index,
            name=f'Player {index}',
            elo=1500 - index,
            wins=index,
            losses=index // 2,
            team_emoji='🏹',
        )
        for index in range(1, count + 1)
    )
    return leaderboard_workers.PlayerLeaderboardResult(
        title='Individual Leaderboard',
        total_ranked=count,
        rows=rows,
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

    def count(self):
        return len(self.rows)

    def __getitem__(self, item):
        return self.rows[item]


class PlayerLeaderboardWorkerTests(unittest.TestCase):
    def test_worker_owns_connection_and_returns_primitive_rows(self):
        database = FakeDatabase()
        received = {}
        players = [
            SimpleNamespace(
                name='Alpha',
                elo_field=1450,
                team=SimpleNamespace(emoji='🏹'),
                get_record=lambda version=None: (7, 3),
            ),
            SimpleNamespace(
                name='Beta',
                elo_field=1400,
                team=None,
                get_record=lambda version=None: (5, 4),
            ),
        ]

        def leaderboard(**kwargs):
            received.update(kwargs)
            return FakeQuery(players)

        request = leaderboard_workers.PlayerLeaderboardRequest(
            guild_id=300,
            active_cutoff=datetime.datetime(2025, 1, 1),
        )
        with mock.patch.object(
            leaderboard_workers.models,
            'db',
            database,
        ), mock.patch.object(
            leaderboard_workers.models.Player,
            'leaderboard',
            side_effect=leaderboard,
        ):
            result = leaderboard_workers.load_player_leaderboard(request)

        self.assertEqual(database.opened, 1)
        self.assertEqual(database.closed, 1)
        self.assertEqual(received['guild_id'], 300)
        self.assertFalse(received['max_flag'])
        self.assertIsNone(received['version'])
        self.assertEqual(result.total_ranked, 2)
        self.assertIsInstance(result.rows, tuple)
        self.assertEqual(result.rows[0].rank, 1)
        self.assertEqual(result.rows[0].team_emoji, '🏹')
        self.assertEqual(result.rows[1].wins, 5)

    def test_global_peak_all_time_all_players_mapping(self):
        database = FakeDatabase()
        received = {}
        member = SimpleNamespace(
            name='Global Alpha',
            elo_field=1700,
            get_record=lambda version=None: (
                9 if version == 'ALLTIME' else 0,
                2,
            ),
        )

        def leaderboard(**kwargs):
            received.update(kwargs)
            return FakeQuery([member])

        request = leaderboard_workers.PlayerLeaderboardRequest(
            guild_id=300,
            scope='global',
            rating='peak',
            era='all-time',
            population='all',
            active_cutoff=datetime.datetime(2025, 1, 1),
        )
        with mock.patch.object(
            leaderboard_workers.models,
            'db',
            database,
        ), mock.patch.object(
            leaderboard_workers.models.DiscordMember,
            'leaderboard',
            side_effect=leaderboard,
        ):
            result = leaderboard_workers.load_player_leaderboard(request)

        self.assertEqual(received['date_cutoff'], datetime.date.min)
        self.assertTrue(received['max_flag'])
        self.assertEqual(received['version'], 'ALLTIME')
        self.assertEqual(result.rows[0].team_emoji, '')
        self.assertEqual(result.rows[0].wins, 9)
        self.assertIn('Global Leaderboard', result.title)
        self.assertIn('Including Inactive Players', result.title)
        self.assertIn('Maximum ELO Achieved', result.title)
        self.assertIn('Alltime', result.title)

    def test_page_boundaries_are_deterministic(self):
        result = result_with_rows()

        first = leaderboard_workers.player_leaderboard_page(result, 0)
        middle = leaderboard_workers.player_leaderboard_page(result, 1)
        last = leaderboard_workers.player_leaderboard_page(result, 2)

        self.assertEqual(first.page_count, 3)
        self.assertEqual((first.start_rank, first.end_rank), (1, 10))
        self.assertEqual((middle.start_rank, middle.end_rank), (11, 20))
        self.assertEqual((last.start_rank, last.end_rank), (21, 23))
        self.assertEqual([row.rank for row in last.rows], [21, 22, 23])
        with self.assertRaises(IndexError):
            leaderboard_workers.player_leaderboard_page(result, 3)


class PlayerLeaderboardExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_reads_are_bounded_and_do_not_block_event_loop(self):
        active = 0
        max_active = 0
        lock = threading.Lock()
        release = threading.Event()

        def slow_read(request):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            release.wait(timeout=2)
            with lock:
                active -= 1
            return result_with_rows(1)

        request = leaderboard_workers.PlayerLeaderboardRequest(
            guild_id=300,
            active_cutoff=datetime.datetime(2025, 1, 1),
        )
        heartbeat = False

        async def tick():
            nonlocal heartbeat
            await asyncio.sleep(0.01)
            heartbeat = True

        with mock.patch.object(
            leaderboard_workers,
            'load_player_leaderboard',
            side_effect=slow_read,
        ):
            reads = [
                asyncio.create_task(
                    leaderboard_workers.run_player_leaderboard(request)
                )
                for _ in range(3)
            ]
            for _ in range(100):
                with lock:
                    if active == 2:
                        break
                await asyncio.sleep(0.005)
            self.assertEqual(active, 2)
            await tick()
            self.assertTrue(heartbeat)
            release.set()
            # Give restricted headless runners a timer wake-up so executor
            # completion callbacks can be delivered.
            await asyncio.sleep(0.05)
            await asyncio.gather(*reads)

        self.assertEqual(max_active, 2)


class PlayerLeaderboardCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_all_sixteen_prefix_filter_combinations_map_to_options(self):
        for scope, rating, era, population in product(
            ('local', 'global'),
            ('current', 'peak'),
            ('current', 'all-time'),
            ('active', 'all'),
        ):
            filters = ' '.join(
                value
                for value in (
                    'global' if scope == 'global' else '',
                    'max' if rating == 'peak' else '',
                    'alltime' if era == 'all-time' else '',
                    'allplayers' if population == 'all' else '',
                )
                if value
            )
            request = games.polygames._player_leaderboard_request(
                guild_id=300,
                invoked_with='lb',
                filters=filters,
            )
            self.assertEqual(
                (
                    request.scope,
                    request.rating,
                    request.era,
                    request.population,
                ),
                (scope, rating, era, population),
            )

        for alias in ('lbglobal', 'lbg'):
            request = games.polygames._player_leaderboard_request(
                guild_id=300,
                invoked_with=alias,
            )
            self.assertEqual(request.scope, 'global')

    def test_prefix_aliases_and_typed_slash_registration(self):
        prefix = next(
            command
            for command in games.polygames.__cog_commands__
            if command.name == 'lb'
        )
        self.assertEqual(
            set(prefix.aliases),
            {'leaderboard', 'leaderboards', 'lbglobal', 'lbg'},
        )
        command = app_group(
            games.polygames,
            'leaderboard',
        ).get_command('players')
        self.assertEqual(
            [
                (parameter.name, parameter.type)
                for parameter in command.parameters
            ],
            [
                ('scope', discord.AppCommandOptionType.string),
                ('rating', discord.AppCommandOptionType.string),
                ('era', discord.AppCommandOptionType.string),
                ('population', discord.AppCommandOptionType.string),
            ],
        )
        self.assertEqual(
            [choice.value for choice in command.parameters[0].choices],
            ['local', 'global'],
        )

    async def test_slash_defers_checks_then_edits_public_result(self):
        events = []

        async def defer():
            events.append('defer')

        async def can_run(ctx):
            events.append('checks')
            return True

        async def load(request):
            events.append(
                (
                    'load',
                    request.scope,
                    request.rating,
                    request.era,
                    request.population,
                )
            )
            return result_with_rows(11)

        async def edit_original_response(**kwargs):
            events.append('edit')
            self.assertIsInstance(kwargs['embed'], discord.Embed)
            self.assertIsInstance(
                kwargs['view'],
                leaderboard_views.PlayerLeaderboardView,
            )
            return SimpleNamespace(edit=mock.AsyncMock())

        context = SimpleNamespace()
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=defer),
            guild=SimpleNamespace(id=300),
            user=SimpleNamespace(id=100),
            channel_id=400,
            edit_original_response=edit_original_response,
        )
        cog = games.polygames.__new__(games.polygames)
        cog.bot = SimpleNamespace()
        prefix_command = SimpleNamespace(can_run=can_run)
        cog.lb = prefix_command
        cog._load_player_leaderboard = load
        command = app_group(
            games.polygames,
            'leaderboard',
        ).get_command('players')

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
                'global',
                'peak',
                'all-time',
                'all',
            )

        self.assertEqual(
            events[:3],
            [
                'defer',
                'checks',
                ('load', 'global', 'peak', 'all-time', 'all'),
            ],
        )
        self.assertEqual(events[-1], 'edit')

    async def test_component_pagination_is_requester_controlled(self):
        result = result_with_rows(23)
        view = leaderboard_views.PlayerLeaderboardView(
            result,
            requester_id=100,
        )
        self.assertEqual(view.page_count, 3)
        self.assertTrue(view.first_page.disabled)
        self.assertFalse(view.next_page.disabled)

        response = SimpleNamespace(send_message=mock.AsyncMock())
        denied = SimpleNamespace(
            user=SimpleNamespace(id=200),
            response=response,
        )
        allowed = SimpleNamespace(
            user=SimpleNamespace(id=100),
            response=response,
        )
        self.assertFalse(await view.interaction_check(denied))
        response.send_message.assert_awaited_once_with(
            'Only the requester can change this leaderboard page.',
            ephemeral=True,
        )
        self.assertTrue(await view.interaction_check(allowed))

        embed = leaderboard_views.player_leaderboard_embed(result, 2)
        self.assertEqual(len(embed.fields), 3)
        self.assertIn('Page 3 of 3', embed.footer.text)
