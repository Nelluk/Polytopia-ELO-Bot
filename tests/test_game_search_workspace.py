"""Focused offline coverage for the bounded game-search workspace."""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
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
            [('query', False, discord.AppCommandOptionType.string)],
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
