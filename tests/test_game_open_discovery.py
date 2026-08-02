"""Focused P5.8 coverage for shared open-game discovery and prefix parity."""

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
import time
import unittest
from unittest import mock

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.game_search_workers')
matchmaking = import_offline_runtime('modules.matchmaking')


class OpenGame:
    def __init__(
        self,
        game_id=1,
        *,
        players=1,
        capacity=2,
        notes='',
        ranked=True,
        member=False,
        host=False,
        open_side=True,
        expiration=None,
    ):
        self.id = game_id
        self.guild_id = 300
        self.name = f'Open {game_id}'
        self.date = '2026-08-02'
        self.is_pending = True
        self.is_completed = False
        self.is_confirmed = False
        self.is_ranked = ranked
        self.notes = notes
        self.expiration = expiration or datetime.now() + timedelta(hours=4)
        self.size = [capacity]
        self.lineup = [SimpleNamespace(player_id=index + 1)
                       for index in range(players)]
        self.gamesides = [SimpleNamespace(size=capacity, team_id=None)]
        self._member = member
        self._host = host
        self._open_side = open_side

    def capacity(self):
        return len(self.lineup), self.size[0]

    def has_player(self, *, discord_id):
        return (self._member and discord_id == 77), None

    def is_hosted_by(self, discord_id):
        return (self._host and discord_id == 77), None

    def first_open_side(self, *, roles):
        return (SimpleNamespace(position=1), False) if self._open_side else (None, True)

    def elo_requirements(self):
        return 0, 3000, 0, 3000

    def creating_player(self):
        return SimpleNamespace(name=f'Host {self.id}')

    def size_string(self):
        return '1v1'

    def get_gamesides_string(self):
        return 'Host vs Open'

    def platform_emoji(self):
        return ''


def open_request(mode='joinable', **kwargs):
    return workers.GameSearchRequest(
        guild_id=300,
        requester_discord_id=77,
        key=workers.GameSearchKey(status=mode),
        requester_level=kwargs.pop('requester_level', 3),
        requester_role_ids=kwargs.pop('requester_role_ids', ()),
        requester_name='requester',
        requester_nick=None,
        **kwargs,
    )


class OpenEligibilityTests(unittest.TestCase):
    def setUp(self):
        self.game = OpenGame()
        self.request = open_request()

    def allowed(self, *, player=None):
        with mock.patch.object(
            workers.settings,
            'can_user_join_game',
            return_value=(True, None),
        ), mock.patch.object(
            workers,
            '_lookup_registered_requester',
            return_value=player,
        ):
            return workers._requester_can_join_open_game(
                self.game,
                self.request,
                capacity=2,
            )

    def test_user_level_restriction_is_shared_by_joinable_reads(self):
        with mock.patch.object(
            workers.settings,
            'can_user_join_game',
            return_value=(False, 'restricted'),
        ) as can_join, mock.patch.object(
            workers,
            '_lookup_registered_requester',
        ) as lookup:
            self.assertFalse(
                workers._requester_can_join_open_game(
                    self.game,
                    self.request,
                    capacity=2,
                )
            )
        can_join.assert_called_once_with(
            user_level=3,
            game_size=2,
            is_ranked=True,
            is_host=False,
        )
        lookup.assert_not_called()

    def test_invitation_mentions_are_authoritative(self):
        self.game.notes = '<@999>'
        self.assertFalse(self.allowed())
        self.game.notes = '<@999> <@77>'
        self.assertTrue(self.allowed())

    def test_role_lock_is_authoritative(self):
        self.game._open_side = False
        self.assertFalse(self.allowed())
        self.game._open_side = True
        self.assertTrue(self.allowed())

    def test_capacity_full_games_are_not_published_by_open_views(self):
        self.game.lineup = [SimpleNamespace(player_id=1, extra=True)] * 2
        events = []

        @contextmanager
        def connection_context():
            events.append('open')
            yield
            events.append('close')

        with mock.patch.object(
            workers.models.db,
            'connection_context',
            side_effect=connection_context,
        ), mock.patch.object(
            workers,
            '_parse_targets',
            return_value=([], [], [], ()),
        ), mock.patch.object(
            workers,
            '_open_base_games',
            return_value=[self.game],
        ), mock.patch.object(
            workers,
            '_open_waitlist_ids',
            return_value=(),
        ), mock.patch.object(
            workers,
            '_lookup_registered_requester',
            return_value=None,
        ):
            result = workers.load_game_search(self.request)

        self.assertEqual(result.rows, ())
        self.assertEqual(events, ['open', 'close'])

    def test_existing_participant_and_host_are_included_without_rechecks(self):
        for attribute in ('_member', '_host'):
            with self.subTest(attribute=attribute):
                game = OpenGame(**{attribute[1:]: True})
                with mock.patch.object(
                    workers.settings,
                    'can_user_join_game',
                    return_value=(False, 'restricted'),
                ) as can_join:
                    self.assertTrue(
                        workers._requester_can_join_open_game(
                            game,
                            self.request,
                            capacity=2,
                        )
                    )
                can_join.assert_not_called()

    def test_registered_player_elo_and_account_name_are_checked(self):
        member = SimpleNamespace(
            elo_moonrise=1000,
            polytopia_name=None,
            name_steam=None,
        )
        player = SimpleNamespace(elo_moonrise=1000, discord_member=member)
        self.assertFalse(self.allowed(player=player))
        member.polytopia_name = 'Canonical'
        self.assertTrue(self.allowed(player=player))


class OpenDiscoveryWorkerTests(unittest.TestCase):
    def setUp(self):
        self.games = [OpenGame(1), OpenGame(2, notes='NOVA template')]

    def load(self, mode, *, ranked_filter=2, platform_filter=2):
        request = open_request(
            mode,
            ranked_filter=ranked_filter,
            platform_filter=platform_filter,
        )
        with mock.patch.object(
            workers.models.db,
            'connection_context',
        ) as connection, mock.patch.object(
            workers,
            '_parse_targets',
            return_value=([], [], [], ()),
        ), mock.patch.object(
            workers,
            '_open_base_games',
            return_value=self.games,
        ), mock.patch.object(
            workers,
            '_open_waitlist_ids',
            return_value=(),
        ), mock.patch.object(
            workers,
            '_lookup_registered_requester',
            return_value=None,
        ):
            connection.return_value.__enter__ = mock.Mock()
            connection.return_value.__exit__ = mock.Mock(return_value=False)
            return workers.load_game_search(request)

    def test_open_rows_are_frozen_and_preserve_expired_pending_games(self):
        self.games[0].expiration = datetime.now() - timedelta(hours=1)
        result = self.load('all-open')
        self.assertEqual(result.rows[0].expiration, 'Exp')
        self.assertTrue(result.rows[0].is_open_listing)
        with self.assertRaises(Exception):
            result.rows[0].game_id = 99

    def test_all_open_does_not_call_requester_eligibility(self):
        with mock.patch.object(
            workers,
            '_requester_can_join_open_game',
            side_effect=AssertionError('all-open must not filter requester'),
        ):
            result = self.load('all-open')
        self.assertEqual(len(result.rows), 2)

    def test_dto_boundary_rejects_cross_guild_rows(self):
        self.games[0].guild_id = 301
        result = self.load('all-open')
        self.assertEqual([row.game_id for row in result.rows], [2])

    def test_nova_modes_filter_notes_after_open_selection(self):
        result = self.load('nova-all')
        self.assertEqual([row.game_id for row in result.rows], [2])
        with mock.patch.object(
            workers,
            '_requester_can_join_open_game',
            return_value=True,
        ):
            result = self.load('nova-joinable')
        self.assertEqual([row.game_id for row in result.rows], [2])

    def test_native_and_prefix_modes_use_guild_ranked_and_platform_filters(self):
        with mock.patch.object(
            workers.models.Game,
            'search_pending',
            return_value=[],
        ) as search_pending, mock.patch.object(
            workers,
            '_open_waitlist_ids',
            return_value=(),
        ), mock.patch.object(
            workers,
            '_parse_targets',
            return_value=([], [], [], ()),
        ), mock.patch.object(
            workers,
            '_lookup_registered_requester',
            return_value=None,
        ), mock.patch.object(
            workers.models.db,
            'connection_context',
        ) as connection:
            connection.return_value.__enter__ = mock.Mock()
            connection.return_value.__exit__ = mock.Mock(return_value=False)
            workers.load_game_search(open_request(
                'joinable',
                ranked_filter=1,
                platform_filter=0,
            ))
        search_pending.assert_called_once_with(
            status_filter=2,
            guild_id=300,
            ranked_filter=1,
            platform_filter=0,
            limit=workers.MAX_GAMES + 1,
        )

    def test_waiting_and_mine_map_to_legacy_queries(self):
        calls = []

        def search_pending(**kwargs):
            calls.append(kwargs)
            return []

        with mock.patch.object(
            workers.models.Game,
            'search_pending',
            side_effect=search_pending,
        ), mock.patch.object(
            workers.models.db,
            'connection_context',
        ) as connection, mock.patch.object(
            workers,
            '_parse_targets',
            return_value=([], [], [], ()),
        ), mock.patch.object(
            workers,
            '_open_waitlist_ids',
            return_value=(),
        ):
            connection.return_value.__enter__ = mock.Mock()
            connection.return_value.__exit__ = mock.Mock(return_value=False)
            workers.load_game_search(open_request('waiting', ranked_filter=0))
            workers.load_game_search(open_request('mine'))

        self.assertEqual(
            calls,
            [
                {
                    'status_filter': 1,
                    'guild_id': 300,
                    'ranked_filter': 0,
                    'limit': workers.MAX_GAMES + 1,
                },
                {
                    'guild_id': 300,
                    'player_discord_id': 77,
                    'limit': workers.MAX_GAMES + 1,
                },
                {
                    'status_filter': 0,
                    'guild_id': 300,
                    'host_discord_id': 77,
                    'limit': workers.MAX_GAMES + 1,
                },
            ],
        )


class PrefixDiscoveryParityTests(unittest.IsolatedAsyncioTestCase):
    class Typing:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    def context(self, invoked_with, channel_id=999):
        author = SimpleNamespace(
            id=77,
            name='Requester',
            nick=None,
            mention='<@77>',
            roles=(),
        )
        return SimpleNamespace(
            guild=SimpleNamespace(id=300),
            author=author,
            channel=SimpleNamespace(id=channel_id),
            prefix='$',
            invoked_with=invoked_with,
            typing=lambda: self.Typing(),
            send=mock.AsyncMock(),
        )

    async def invoke(self, invoked_with, args=(), channel_id=999):
        ctx = self.context(invoked_with, channel_id=channel_id)
        cog = matchmaking.matchmaking.__new__(matchmaking.matchmaking)
        cog.bot = SimpleNamespace()
        result = workers.GameSearchSnapshot(
            query='',
            key=workers.GameSearchKey(status='all-open'),
            description='view: All open',
            rows=(),
            truncated=False,
        )
        with mock.patch.object(
            matchmaking.game_search_workers,
            'run_game_search',
            new=mock.AsyncMock(return_value=result),
        ) as run_search, mock.patch.object(
            matchmaking.utilities,
            'paginate',
            new=mock.AsyncMock(),
        ), mock.patch.object(
            matchmaking.settings,
            'guild_setting',
            side_effect=lambda _guild, name: {
                'ranked_game_channel': 100,
                'unranked_game_channel': 101,
                'steam_game_channel': 102,
            }[name],
        ), mock.patch.object(
            matchmaking.settings,
            'get_user_level',
            return_value=3,
        ), mock.patch.object(
            matchmaking.settings,
            'is_staff',
            return_value=False,
        ):
            await matchmaking.matchmaking.games.callback(
                cog,
                ctx,
                *args,
            )
            await asyncio.sleep(0)
        return run_search.await_args.args[0]

    async def test_all_legacy_alias_and_argument_modes_map_to_shared_views(self):
        cases = (
            ('games', (), 'joinable', 2, 2),
            ('opengames', (), 'joinable', 2, 2),
            ('novagames', (), 'nova-joinable', 2, 2),
            ('nova', ('games',), 'nova-joinable', 2, 2),
            ('opengames', ('all',), 'all-open', 2, 2),
            ('opengames', ('waiting',), 'waiting', 2, 2),
            ('opengames', ('me',), 'mine', 2, 2),
            ('novagames', ('all',), 'nova-all', 2, 2),
            ('opengames', ('ranked',), 'joinable', 1, 2),
            ('opengames', ('unranked',), 'joinable', 0, 2),
            ('opengames', ('steam',), 'joinable', 2, 0),
        )
        for invoked_with, args, expected_mode, expected_ranked, expected_platform in cases:
            with self.subTest(invoked_with=invoked_with, args=args):
                request = await self.invoke(invoked_with, args)
                self.assertEqual(request.key.status, expected_mode)
                self.assertEqual(request.ranked_filter, expected_ranked)
                self.assertEqual(request.platform_filter, expected_platform)

    async def test_channel_inference_and_no_direct_database_read_remain_legacy_compatible(self):
        with mock.patch.object(
            matchmaking.models.Game,
            'search_pending',
            side_effect=AssertionError('prefix adapter must use worker'),
        ):
            request = await self.invoke('opengames', channel_id=100)
        self.assertEqual(request.ranked_filter, 1)


class OpenDiscoveryExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_open_read_keeps_event_loop_responsive(self):
        original = workers.load_game_search

        def slow(_request):
            time.sleep(0.08)
            return workers.GameSearchSnapshot(
                query='',
                key=workers.GameSearchKey(status='joinable'),
                description='view: Joinable for me',
                rows=(),
                truncated=False,
            )

        workers.load_game_search = slow
        try:
            task = asyncio.create_task(
                workers.run_game_search(open_request('joinable'))
            )
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            heartbeat = asyncio.Event()

            async def pulse():
                await asyncio.sleep(0)
                heartbeat.set()

            await pulse()
            self.assertTrue(heartbeat.is_set())
            await asyncio.sleep(0.10)
            self.assertEqual((await task).key.status, 'joinable')
        finally:
            workers.load_game_search = original

    def test_search_executor_is_bounded(self):
        self.assertEqual(workers._game_search_executor._max_workers, 2)


if __name__ == '__main__':
    unittest.main()
