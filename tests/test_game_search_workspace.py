"""Focused offline coverage for the bounded game-search workspace."""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
import threading
import time
import unittest
from unittest import mock

import discord

from tests.test_newgame_worker import import_offline_runtime


games = import_offline_runtime('modules.games')
workers = import_offline_runtime('modules.game_search_workers')
views = import_offline_runtime('modules.game_search_views')


def snapshot(
    *,
    key=None,
    count=14,
    query='Nelluk Ronin 2v2',
):
    key = key or workers.GameSearchKey()
    return workers.GameSearchSnapshot(
        query=query,
        key=key,
        description='players: Nelluk · teams: Ronin · size: 2v2',
        rows=tuple(
            workers.GameSearchRow(
                game_id=index + 1,
                name=f'Game {index + 1}',
                date='2026-07-30',
                status='completed',
                outcome='Win',
                ranked=True,
                size='2v2',
                roster='Nelluk vs Ronin',
                notes='',
                channel_mention='',
            )
            for index in range(count)
        ),
        truncated=False,
    )


class GameSearchRegistrationTests(unittest.TestCase):
    def test_exact_slash_shape_and_prefix_surface(self):
        game_group = next(
            command for command in games.polygames.__cog_app_commands__
            if command.name == 'game'
        )
        command = game_group.get_command('search')
        self.assertIsNotNone(command)
        self.assertEqual(
            [(parameter.name, parameter.required, parameter.type)
             for parameter in command.parameters],
            [
                ('query', False, discord.AppCommandOptionType.string),
                ('view', False, discord.AppCommandOptionType.string),
            ],
        )
        self.assertEqual(
            [choice.name for choice in command.parameters[1].choices],
            [
                'All games',
                'Joinable for me',
                'All open',
                'Waiting to start',
                'My open games',
                'Active games',
                'Completed games',
                'Unconfirmed results',
            ],
        )
        self.assertEqual(
            [choice.value for choice in command.parameters[1].choices],
            [
                'all',
                'joinable',
                'all-open',
                'waiting',
                'mine',
                'active',
                'completed',
                'unconfirmed',
            ],
        )
        prefix = {
            command.name: command
            for command in games.polygames.__cog_commands__
        }
        self.assertIn('allgames', prefix)
        self.assertIn('incomplete', prefix)
        self.assertIn('wins', prefix)
        self.assertEqual(
            set(prefix['incomplete'].aliases),
            {'complete', 'completed'},
        )
        self.assertEqual(set(prefix['wins'].aliases), {'loss', 'losses'})


class GameSearchViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_result_has_requester_only_controls(self):
        view = views.GameSearchWorkspace(
            requester_id=10,
            initial_result=snapshot(),
            loader=mock.AsyncMock(),
        )
        unauthorized = SimpleNamespace(
            user=SimpleNamespace(id=11),
            response=SimpleNamespace(send_message=mock.AsyncMock()),
        )
        self.assertFalse(await view.interaction_check(unauthorized))
        unauthorized.response.send_message.assert_awaited_once_with(
            view.unauthorized_message,
            ephemeral=True,
        )
        authorized = SimpleNamespace(user=SimpleNamespace(id=10))
        self.assertTrue(await view.interaction_check(authorized))

    async def test_pagination_uses_immutable_snapshot(self):
        loader = mock.AsyncMock()
        view = views.GameSearchWorkspace(
            requester_id=10,
            initial_result=snapshot(),
            loader=loader,
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(edit_message=mock.AsyncMock()),
        )
        await view.show_next(interaction)
        self.assertEqual(view.page_index, 1)
        loader.assert_not_awaited()

    async def test_filter_navigation_loads_once_then_uses_cache(self):
        initial = snapshot(count=1)
        filtered_key = workers.GameSearchKey(status='active')
        filtered = snapshot(key=filtered_key, count=2)
        loader = mock.AsyncMock(return_value=filtered)
        view = views.GameSearchWorkspace(
            requester_id=10,
            initial_result=initial,
            loader=loader,
        )

        class Response:
            def __init__(self):
                self.defer = mock.AsyncMock()
                self.edit_message = mock.AsyncMock()

            def is_done(self):
                return bool(self.defer.await_count)

        interaction = SimpleNamespace(
            response=Response(),
            edit_original_response=mock.AsyncMock(),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        await view._change_filter(interaction, status='active')
        await view._change_filter(interaction, status='all')
        await view._change_filter(interaction, status='active')
        loader.assert_awaited_once_with(filtered_key)

    async def test_view_navigation_preserves_public_cached_workspace(self):
        initial = snapshot(count=1)
        filtered_key = workers.GameSearchKey(status='joinable')
        filtered = snapshot(key=filtered_key, count=2)
        loader = mock.AsyncMock(return_value=filtered)
        view = views.GameSearchWorkspace(
            requester_id=10,
            initial_result=initial,
            loader=loader,
        )

        class Response:
            def __init__(self):
                self.defer = mock.AsyncMock()
                self.edit_message = mock.AsyncMock()

            def is_done(self):
                return bool(self.defer.await_count)

        interaction = SimpleNamespace(
            response=Response(),
            edit_original_response=mock.AsyncMock(),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        await view._change_filter(interaction, status='joinable')
        await view._change_filter(interaction, status='joinable')
        loader.assert_awaited_once_with(filtered_key)
        self.assertEqual(view.result.key.status, 'joinable')

    async def test_view_switch_keeps_existing_outcome_and_size_refinements(self):
        initial_key = workers.GameSearchKey(
            status='all',
            outcome='win',
            size='2v2',
        )
        initial = snapshot(key=initial_key, count=1)
        loader = mock.AsyncMock(return_value=snapshot(
            key=workers.GameSearchKey(
                status='active',
                outcome='win',
                size='2v2',
            ),
            count=1,
        ))
        view = views.GameSearchWorkspace(
            requester_id=10,
            initial_result=initial,
            loader=loader,
        )
        response = SimpleNamespace(
            defer=mock.AsyncMock(),
            edit_message=mock.AsyncMock(),
            is_done=lambda: True,
        )
        interaction = SimpleNamespace(
            response=response,
            edit_original_response=mock.AsyncMock(),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        await view._change_filter(interaction, status='active')
        loader.assert_awaited_once_with(
            workers.GameSearchKey(
                status='active',
                outcome='win',
                size='2v2',
            )
        )

    async def test_open_view_switch_resets_incompatible_outcome(self):
        initial_key = workers.GameSearchKey(
            status='all',
            outcome='loss',
            size='2v2',
        )
        initial = snapshot(key=initial_key, count=1)
        open_key = workers.GameSearchKey(
            status='joinable',
            outcome='any',
            size='2v2',
        )
        loader = mock.AsyncMock(return_value=snapshot(key=open_key, count=1))
        view = views.GameSearchWorkspace(
            requester_id=10,
            initial_result=initial,
            loader=loader,
        )
        view.view_select._values = ['joinable']
        response = SimpleNamespace(
            defer=mock.AsyncMock(),
            edit_message=mock.AsyncMock(),
            is_done=lambda: True,
        )
        interaction = SimpleNamespace(
            response=response,
            edit_original_response=mock.AsyncMock(),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )

        await view._select_view(interaction)

        loader.assert_awaited_once_with(open_key)
        self.assertEqual(view.result.key, open_key)
        outcome_defaults = {
            option.value: option.default
            for option in view.outcome_select.options
        }
        self.assertTrue(outcome_defaults['any'])
        self.assertFalse(outcome_defaults['loss'])

    async def test_filter_failure_is_ephemeral_and_keeps_snapshot(self):
        initial = snapshot(count=1)
        loader = mock.AsyncMock(side_effect=ValueError('bad filter'))
        view = views.GameSearchWorkspace(
            requester_id=10,
            initial_result=initial,
            loader=loader,
        )
        response = SimpleNamespace(defer=mock.AsyncMock())
        response.is_done = lambda: True
        interaction = SimpleNamespace(
            response=response,
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        await view._change_filter(interaction, status='active')
        self.assertIs(view.result, initial)
        interaction.followup.send.assert_awaited_once_with(
            'Could not load that view: bad filter',
            ephemeral=True,
        )

    def test_component_tree_stays_below_discord_limits(self):
        view = views.GameSearchWorkspace(
            requester_id=10,
            initial_result=snapshot(),
            loader=mock.AsyncMock(),
        )
        children = list(view.walk_children())
        self.assertLessEqual(len(children), 40)
        self.assertEqual(len(view.children), 1)


class GameSearchWorkerTests(unittest.IsolatedAsyncioTestCase):
    def test_request_and_result_are_immutable(self):
        request = workers.GameSearchRequest(
            guild_id=1,
            requester_discord_id=2,
        )
        with self.assertRaises(Exception):
            request.guild_id = 3
        with self.assertRaises(Exception):
            snapshot().rows += ()

    def test_size_parser_preserves_arbitrary_side_shapes(self):
        self.assertEqual(workers._parse_size('1v1v1'), (1, 1, 1))
        self.assertEqual(workers._parse_size('3vs2'), (3, 2))
        with self.assertRaises(workers.GameSearchError):
            workers._parse_size('large')

    def test_legacy_mention_tokens_are_normalized(self):
        with (
            mock.patch.object(
                workers.models.Team,
                'get_by_name',
                return_value=[],
            ),
            mock.patch.object(
                workers.models.Player,
                'string_matches',
                return_value=[],
            ) as player_matches,
        ):
            workers._parse_targets('<@123456789012345678>', 1)
        self.assertEqual(
            player_matches.call_args.kwargs['player_string'],
            '123456789012345678',
        )

    def test_worker_owns_connection_and_returns_frozen_rows(self):
        events = []

        @contextmanager
        def connection_context():
            events.append('open')
            yield
            events.append('close')

        game = SimpleNamespace(
            id=7,
            name='Fixture',
            date='2026-07-30',
            is_pending=False,
            is_completed=False,
            is_confirmed=False,
            is_ranked=True,
            notes='notes',
            winner_id=None,
            gamesides=(),
            size_string=lambda: '1v1',
            get_gamesides_string=lambda: 'A vs B',
        )
        with (
            mock.patch.object(
                workers.models.db,
                'connection_context',
                side_effect=connection_context,
            ),
            mock.patch.object(
                workers,
                '_parse_targets',
                return_value=([], [], [], ()),
            ),
            mock.patch.object(
                workers.models.Game,
                'search',
                return_value=[game],
            ),
        ):
            result = workers.load_game_search(
                workers.GameSearchRequest(
                    guild_id=1,
                    requester_discord_id=2,
                )
            )
        self.assertEqual(events, ['open', 'close'])
        self.assertEqual(result.rows[0].status, 'active')

    async def test_slow_worker_keeps_event_loop_responsive(self):
        original = workers.load_game_search

        def slow(_request):
            time.sleep(0.08)
            return snapshot(count=1)

        workers.load_game_search = slow
        try:
            task = asyncio.create_task(workers.run_game_search(
                workers.GameSearchRequest(
                    guild_id=1,
                    requester_discord_id=2,
                )
            ))
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            ticked = False

            async def tick():
                nonlocal ticked
                await asyncio.sleep(0)
                ticked = True

            await tick()
            self.assertTrue(ticked)
            # Restricted headless runners may need a timer wake-up before
            # delivering a worker completion callback.
            await asyncio.sleep(0.10)
            self.assertEqual((await task).rows[0].game_id, 1)
        finally:
            workers.load_game_search = original

    async def test_cancelled_read_drains_worker_before_propagating(self):
        started = threading.Event()
        release = threading.Event()
        connection_closed = threading.Event()

        def blocked(_request):
            started.set()
            try:
                release.wait(timeout=2)
                return snapshot(count=1)
            finally:
                connection_closed.set()

        with mock.patch.object(
            workers,
            'load_game_search',
            side_effect=blocked,
        ):
            task = asyncio.create_task(workers.run_game_search(
                workers.GameSearchRequest(
                    guild_id=1,
                    requester_discord_id=2,
                )
            ))
            try:
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.001)
                self.assertTrue(started.is_set())
                task.cancel()
                task.cancel()
                await asyncio.sleep(0.01)
                self.assertFalse(task.done())
                self.assertFalse(connection_closed.is_set())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(connection_closed.is_set())
            finally:
                release.set()

    async def test_cancelled_worker_failure_is_logged_not_reinterpreted(self):
        started = threading.Event()
        release = threading.Event()

        def blocked_failure(_request):
            started.set()
            release.wait(timeout=2)
            raise RuntimeError('worker failed after cancellation')

        with mock.patch.object(
            workers,
            'load_game_search',
            side_effect=blocked_failure,
        ), mock.patch.object(workers.logger, 'exception') as logged:
            task = asyncio.create_task(workers.run_game_search(
                workers.GameSearchRequest(
                    guild_id=1,
                    requester_discord_id=2,
                )
            ))
            try:
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.001)
                self.assertTrue(started.is_set())
                task.cancel()
                task.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                logged.assert_called_once_with(
                    'Cancelled game-search worker completed with an error'
                )
            finally:
                release.set()

    def test_unconfirmed_and_outcome_guards(self):
        with self.assertRaisesRegex(workers.GameSearchError, 'Only staff'):
            workers.load_game_search(
                workers.GameSearchRequest(
                    guild_id=1,
                    requester_discord_id=2,
                    key=workers.GameSearchKey(status='unconfirmed'),
                )
            )
        with (
            mock.patch.object(
                workers.models.db,
                'connection_context',
            ),
            mock.patch.object(
                workers,
                '_parse_targets',
                return_value=([], [], [], ()),
            ),
        ):
            with self.assertRaisesRegex(
                workers.GameSearchError,
                'Choose a player or team',
            ):
                workers.load_game_search(
                    workers.GameSearchRequest(
                        guild_id=1,
                        requester_discord_id=2,
                        key=workers.GameSearchKey(outcome='win'),
                    )
                )

    def test_open_view_is_staff_guarded_and_uses_immutable_key(self):
        with self.assertRaisesRegex(workers.GameSearchError, 'Only staff'):
            workers.load_game_search(
                workers.GameSearchRequest(
                    guild_id=1,
                    requester_discord_id=2,
                    key=workers.GameSearchKey(status='unconfirmed'),
                )
            )
        request = workers.GameSearchRequest(
            guild_id=1,
            requester_discord_id=2,
            key=workers.GameSearchKey(status='joinable'),
            requester_role_ids=(9, 10),
        )
        with self.assertRaises(Exception):
            request.requester_role_ids = (11,)


class GameSearchAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_prefix_modes_map_to_workspace_filters(self):
        cog = games.polygames.__new__(games.polygames)
        cog._send_game_search_workspace = mock.AsyncMock()
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            author=SimpleNamespace(id=2),
        )
        cases = {
            'ALLGAMES': ('all', 'any'),
            'COMPLETE': ('completed', 'any'),
            'INCOMPLETE': ('unfinished', 'any'),
            'WINS': ('all', 'win'),
            'LOSSES': ('all', 'loss'),
        }
        for mode, expected in cases.items():
            cog._send_game_search_workspace.reset_mock()
            await cog.game_search(ctx, mode, ['Nelluk', 'Ronin', '2v2'])
            kwargs = cog._send_game_search_workspace.await_args.kwargs
            self.assertEqual(
                (kwargs['key'].status, kwargs['key'].outcome),
                expected,
            )
            self.assertEqual(kwargs['query'], 'Nelluk Ronin 2v2')

    async def test_bare_prefix_defaults_to_requester_but_all_does_not(self):
        cog = games.polygames.__new__(games.polygames)
        cog._send_game_search_workspace = mock.AsyncMock()
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            author=SimpleNamespace(id=222),
        )
        await cog.game_search(ctx, 'ALLGAMES', [])
        self.assertEqual(
            cog._send_game_search_workspace.await_args.kwargs['query'],
            '222',
        )
        await cog.game_search(ctx, 'ALLGAMES', ['all'])
        self.assertEqual(
            cog._send_game_search_workspace.await_args.kwargs['query'],
            '',
        )

    async def test_prefix_workspace_preserves_requester_snapshot_on_view_switch(self):
        cog = games.polygames.__new__(games.polygames)
        requests = []

        async def load(request):
            requests.append(request)
            return snapshot(key=request.key, query=request.query, count=0)

        cog._load_game_search = load
        author = SimpleNamespace(
            id=222,
            name='Nelluk',
            nick='Nell',
            roles=(SimpleNamespace(id=9), SimpleNamespace(id=10)),
        )
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            author=author,
            send=mock.AsyncMock(),
        )
        with (
            mock.patch.object(games.settings, 'get_user_level', return_value=3),
            mock.patch.object(games.settings, 'is_staff', return_value=False),
        ):
            await cog.game_search(ctx, 'ALLGAMES', ['all'])

        view = ctx.send.await_args.kwargs['view']
        view.view_select._values = ['joinable']
        response = SimpleNamespace(
            defer=mock.AsyncMock(),
            edit_message=mock.AsyncMock(),
            is_done=lambda: True,
        )
        interaction = SimpleNamespace(
            response=response,
            edit_original_response=mock.AsyncMock(),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        await view._select_view(interaction)

        request = requests[-1]
        self.assertEqual(request.key.status, 'joinable')
        self.assertEqual(request.requester_level, 3)
        self.assertEqual(request.requester_role_ids, (9, 10))
        self.assertEqual(request.requester_name, 'Nelluk')
        self.assertEqual(request.requester_nick, 'Nell')
        self.assertFalse(request.staff)

    async def test_slash_defers_before_database_load_and_failure_is_private(self):
        command = next(
            command for command in games.polygames.__cog_app_commands__
            if command.name == 'game'
        ).get_command('search')
        cog = games.polygames.__new__(games.polygames)
        events = []

        async def load(_request):
            events.append('load')
            raise workers.GameSearchError('bad query')

        cog._load_game_search = load
        can_run = mock.AsyncMock(return_value=True)
        cog.allgames = SimpleNamespace(can_run=can_run)
        response = SimpleNamespace(defer=mock.AsyncMock())

        async def defer():
            events.append('defer')

        response.defer.side_effect = defer
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            user=SimpleNamespace(id=2),
            response=response,
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        context = SimpleNamespace()
        with (
            mock.patch.object(
                games.commands.Context,
                'from_interaction',
                new=mock.AsyncMock(return_value=context),
            ),
            mock.patch.object(
                games.settings,
                'guild_setting',
                return_value='$',
            ),
            mock.patch.object(games.settings, 'is_staff', return_value=False),
        ):
            await command.callback(cog, interaction, 'query')
        self.assertEqual(events, ['defer', 'load'])
        interaction.followup.send.assert_awaited_once_with(
            'bad query',
            ephemeral=True,
        )

    async def test_native_initial_view_mapping_defaults_to_all_games(self):
        command = next(
            command for command in games.polygames.__cog_app_commands__
            if command.name == 'game'
        ).get_command('search')
        cog = games.polygames.__new__(games.polygames)
        cog.allgames = SimpleNamespace(can_run=mock.AsyncMock(return_value=True))
        supplied_views = (
            (None, 'all'),
            ('all', 'all'),
            ('joinable', 'joinable'),
            ('all-open', 'all-open'),
            ('waiting', 'waiting'),
            ('mine', 'mine'),
            ('active', 'active'),
            ('completed', 'completed'),
            ('unconfirmed', 'unconfirmed'),
        )
        for supplied, expected in supplied_views:
            with self.subTest(supplied=supplied):
                loaded = snapshot(
                    key=workers.GameSearchKey(status=expected),
                    count=0,
                )
                cog._load_game_search = mock.AsyncMock(return_value=loaded)
                interaction = SimpleNamespace(
                    guild=SimpleNamespace(id=1),
                    user=SimpleNamespace(id=2, roles=()),
                    response=SimpleNamespace(defer=mock.AsyncMock()),
                    edit_original_response=mock.AsyncMock(),
                )
                with (
                    mock.patch.object(
                        games.commands.Context,
                        'from_interaction',
                        new=mock.AsyncMock(return_value=SimpleNamespace()),
                    ),
                    mock.patch.object(
                        games.settings,
                        'guild_setting',
                        return_value='$',
                    ),
                    mock.patch.object(
                        games.settings,
                        'is_staff',
                        return_value=(expected == 'unconfirmed'),
                    ),
                    mock.patch.object(
                        games.settings,
                        'get_user_level',
                        return_value=5,
                    ),
                ):
                    await command.callback(
                        cog,
                        interaction,
                        'term',
                        supplied,
                    )
                request = cog._load_game_search.await_args.args[0]
                self.assertEqual(request.key.status, expected)
                self.assertEqual(request.query, 'term')

    def test_unconfirmed_view_is_only_available_to_staff_workspace(self):
        for staff, expected in ((False, False), (True, True)):
            with self.subTest(staff=staff):
                view = views.GameSearchWorkspace(
                    requester_id=10,
                    initial_result=snapshot(count=0),
                    loader=mock.AsyncMock(),
                    can_view_unconfirmed=staff,
                )
                labels = [option.label for option in view.view_select.options]
                self.assertEqual('Unconfirmed results' in labels, expected)

    async def test_timeout_is_user_facing(self):
        cog = games.polygames.__new__(games.polygames)
        cog._load_game_search = mock.AsyncMock(
            side_effect=asyncio.TimeoutError,
        )
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            author=SimpleNamespace(id=2),
            send=mock.AsyncMock(),
        )
        with mock.patch.object(games.settings, 'is_staff', return_value=False):
            result = await cog._send_game_search_workspace(
                ctx,
                query='',
                key=workers.GameSearchKey(),
            )
        self.assertFalse(result)
        ctx.send.assert_awaited_once_with('Game search timed out.')


if __name__ == '__main__':
    unittest.main()
