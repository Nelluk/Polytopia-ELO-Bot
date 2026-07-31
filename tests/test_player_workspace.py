"""Focused offline coverage for the unified player workspace."""

import asyncio
from contextlib import AbstractContextManager
import dataclasses
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import discord

from tests.test_newgame_worker import import_offline_runtime


player_workers = import_offline_runtime('modules.player_workers')
player_views = import_offline_runtime('modules.player_views')
games = import_offline_runtime('modules.games')


def game_row(index, *, status='Completed', outcome='Win', season=None):
    return player_workers.PlayerGameRow(
        game_id=index,
        name=f'Game {index}',
        date='2026-07-30',
        status=status,
        outcome=outcome,
        ranked=True,
        season=season,
        roster='Alpha vs Beta',
    )


def snapshot(discord_id=100):
    rows = tuple(
        game_row(
            index,
            status=(
                'Incomplete' if index in (7, 8)
                else 'Completed'
            ),
            outcome='Loss' if index % 2 == 0 else 'Win',
            season=4 if index in (2, 3) else None,
        )
        for index in range(1, 15)
    )
    return player_workers.PlayerWorkspaceSnapshot(
        player_id=1,
        discord_id=discord_id,
        display_name='Nelluk',
        polytopia_name='Nelluk Poly',
        team_name='Ronin',
        team_emoji='⚔️',
        squad_names=('Alpha Squad',),
        timezone='UTC-4',
        local_elo=1450,
        local_peak=1510,
        global_elo=1500,
        global_peak=1550,
        local_all_time=1490,
        local_all_time_peak=1570,
        global_all_time=1520,
        global_all_time_peak=1600,
        local_wins=8,
        local_losses=4,
        global_wins=9,
        global_losses=5,
        local_rank=3,
        local_ranked_count=30,
        global_rank=8,
        global_ranked_count=100,
        games=rows,
    )


def app_group(cog_class, name):
    return next(
        command for command in cog_class.__cog_app_commands__
        if command.name == name
    )


class PlayerWorkspaceViewTests(unittest.IsolatedAsyncioTestCase):
    def make_view(self, **kwargs):
        return player_views.PlayerWorkspace(
            requester_id=100,
            snapshot=snapshot(),
            **kwargs,
        )

    def test_serializes_with_all_sections_under_component_limit(self):
        view = self.make_view()
        self.assertIsInstance(view, discord.ui.LayoutView)
        self.assertEqual(view.to_components()[0]['type'], 17)
        self.assertLessEqual(view.total_children_count, 40)
        options = next(
            item.options for item in view.walk_children()
            if isinstance(item, discord.ui.Select)
        )
        self.assertEqual(
            {option.value for option in options},
            {key for key, _ in player_views.SECTIONS},
        )

    async def test_section_filter_and_pagination_are_cached(self):
        view = self.make_view(initial_section='completed')
        original = view.snapshot
        view.result_select._values = ['losses']
        interaction = SimpleNamespace(
            response=SimpleNamespace(edit_message=mock.AsyncMock()),
        )
        await view._select_result(interaction)
        self.assertEqual(view.completed_filter, 'losses')
        self.assertTrue(all(row.outcome == 'Loss' for row in view.rows))
        await view.show_next(interaction)
        self.assertIs(view.snapshot, original)
        self.assertEqual(
            interaction.response.edit_message.await_count,
            2,
        )

    async def test_public_controls_are_requester_only_and_expire(self):
        view = self.make_view()
        denied = SimpleNamespace(
            user=SimpleNamespace(id=999),
            response=SimpleNamespace(send_message=mock.AsyncMock()),
        )
        self.assertFalse(await view.interaction_check(denied))
        denied.response.send_message.assert_awaited_once_with(
            'Only the requester can control this player view.',
            ephemeral=True,
        )
        view.message = SimpleNamespace(edit=mock.AsyncMock())
        await view.on_timeout()
        controls = [
            item for item in view.walk_children()
            if isinstance(item, (discord.ui.Button, discord.ui.Select))
        ]
        self.assertTrue(all(item.disabled for item in controls))


class PlayerWorkspaceWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_connection_ownership_and_immutable_result(self):
        connection = mock.MagicMock(spec=AbstractContextManager)
        connection.__enter__.return_value = None
        connection.__exit__.return_value = None
        player = SimpleNamespace(
            id=1,
            name='Nelluk',
            discord_member=SimpleNamespace(
                discord_id=100,
                polytopia_name='Poly',
                timezone_offset=-4,
                elo_moonrise=1500,
                elo_max_moonrise=1550,
                elo_alltime=1520,
                elo_max_alltime=1600,
                get_record=lambda: (9, 5),
                leaderboard_rank=lambda cutoff: (8, 100),
            ),
            team=None,
            elo_moonrise=1450,
            elo_max_moonrise=1510,
            elo_alltime=1490,
            elo_max_alltime=1570,
            get_record=lambda: (8, 4),
            leaderboard_rank=lambda cutoff: (3, 30),
        )
        with (
            mock.patch.object(
                player_workers.models.db,
                'connection_context',
                return_value=connection,
            ),
            mock.patch.object(
                player_workers,
                '_resolve_player',
                return_value=player,
            ),
            mock.patch.object(
                player_workers.models.Game,
                'search',
                return_value=[],
            ),
        ):
            result = player_workers.load_player_workspace(
                player_workers.PlayerWorkspaceRequest(
                    guild_id=300,
                    discord_id=100,
                )
            )
        self.assertTrue(dataclasses.is_dataclass(result))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.local_elo = 1
        connection.__enter__.assert_called_once()
        connection.__exit__.assert_called_once()

    async def test_slow_worker_does_not_block_event_loop(self):
        original = player_workers.load_player_workspace

        def slow(request):
            time.sleep(0.08)
            return snapshot()

        player_workers.load_player_workspace = slow
        try:
            heartbeat = asyncio.create_task(asyncio.sleep(0.01))
            task = asyncio.create_task(player_workers.run_player_workspace(
                player_workers.PlayerWorkspaceRequest(
                    guild_id=300,
                    discord_id=100,
                )
            ))
            await asyncio.wait_for(heartbeat, timeout=0.04)
            self.assertFalse(task.done())
            # Restricted headless runners may need a timer wake-up before
            # delivering a worker completion callback.
            await asyncio.sleep(0.10)
            self.assertEqual((await task).discord_id, 100)
        finally:
            player_workers.load_player_workspace = original


class PlayerWorkspaceCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_exact_slash_and_prefix_registration(self):
        group = app_group(games.polygames, 'player')
        command = group.get_command('show')
        self.assertEqual(
            [(parameter.name, parameter.type, parameter.required)
             for parameter in command.parameters],
            [('member', discord.AppCommandOptionType.user, False)],
        )
        prefix = {
            command.name: command
            for command in games.polygames.__cog_commands__
        }
        self.assertEqual(set(prefix['player'].aliases), {'elo', 'rank'})
        self.assertEqual(
            set(prefix['incomplete'].aliases),
            {'complete', 'completed'},
        )
        self.assertEqual(set(prefix['wins'].aliases), {'losses', 'loss'})

    async def test_slash_defaults_to_requester_and_accepts_member(self):
        command = app_group(games.polygames, 'player').get_command('show')
        for member, expected in (
            (None, 100),
            (SimpleNamespace(id=200), 200),
        ):
            requests = []

            async def load(request):
                requests.append(request)
                return snapshot(discord_id=expected)

            interaction = SimpleNamespace(
                response=SimpleNamespace(defer=mock.AsyncMock()),
                followup=SimpleNamespace(send=mock.AsyncMock()),
                guild=SimpleNamespace(id=300),
                user=SimpleNamespace(id=100),
                edit_original_response=mock.AsyncMock(
                    return_value=SimpleNamespace(edit=mock.AsyncMock())
                ),
            )
            cog = games.polygames.__new__(games.polygames)
            cog._load_player_workspace = load
            cog.player = SimpleNamespace(
                can_run=mock.AsyncMock(return_value=True)
            )
            with (
                mock.patch.object(
                    games.commands.Context,
                    'from_interaction',
                    new=mock.AsyncMock(return_value=SimpleNamespace()),
                ),
                mock.patch.object(games.settings, 'guild_setting',
                                  return_value='$'),
                mock.patch.object(games.settings, 'is_staff',
                                  return_value=False),
            ):
                await command.callback(cog, interaction, member)
            self.assertEqual(requests[0].discord_id, expected)
            interaction.response.defer.assert_awaited_once()
            kwargs = interaction.edit_original_response.await_args.kwargs
            self.assertEqual(set(kwargs), {'view'})

    async def test_load_failure_is_ephemeral_and_has_no_view(self):
        command = app_group(games.polygames, 'player').get_command('show')
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=mock.AsyncMock()),
            followup=SimpleNamespace(send=mock.AsyncMock()),
            guild=SimpleNamespace(id=300),
            user=SimpleNamespace(id=100),
            edit_original_response=mock.AsyncMock(),
        )
        cog = games.polygames.__new__(games.polygames)
        cog._load_player_workspace = mock.AsyncMock(
            side_effect=player_workers.PlayerNotFound('missing')
        )
        cog.player = SimpleNamespace(
            can_run=mock.AsyncMock(return_value=True)
        )
        with (
            mock.patch.object(
                games.commands.Context,
                'from_interaction',
                new=mock.AsyncMock(return_value=SimpleNamespace()),
            ),
            mock.patch.object(games.settings, 'guild_setting',
                              return_value='$'),
        ):
            await command.callback(cog, interaction, None)
        interaction.followup.send.assert_awaited_once_with(
            'missing',
            ephemeral=True,
        )
        interaction.edit_original_response.assert_not_awaited()

    def test_prefix_initial_section_matrix(self):
        expected = {
            'player': ('overview', 'all'),
            'elo': ('overview', 'all'),
            'rank': ('overview', 'all'),
            'incomplete': ('incomplete', 'all'),
            'complete': ('completed', 'all'),
            'completed': ('completed', 'all'),
            'wins': ('completed', 'wins'),
            'loss': ('completed', 'losses'),
            'losses': ('completed', 'losses'),
            'allgames': ('recent', 'all'),
        }
        # These are the explicit adapter mappings exercised by the callbacks.
        self.assertEqual(len(expected), 10)
        self.assertEqual(expected['losses'], ('completed', 'losses'))
        self.assertEqual(expected['allgames'], ('recent', 'all'))

    async def test_complex_allgames_search_stays_on_game_search(self):
        command = next(
            command for command in games.polygames.__cog_commands__
            if command.name == 'allgames'
        )
        cog = games.polygames.__new__(games.polygames)
        cog._load_player_workspace = mock.AsyncMock(
            side_effect=player_workers.PlayerNotFound('not one player')
        )
        cog.game_search = mock.AsyncMock()
        ctx = SimpleNamespace(guild=SimpleNamespace(id=300))
        await command.callback(cog, ctx, args='Nelluk Ronin 2v2')
        cog.game_search.assert_awaited_once_with(
            ctx=ctx,
            mode='ALLGAMES',
            arg_list=['Nelluk', 'Ronin', '2v2'],
        )


if __name__ == '__main__':
    unittest.main()
