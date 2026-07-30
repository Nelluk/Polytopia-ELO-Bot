"""Offline coverage for the experimental Components v2 leaderboard."""

import datetime
from types import SimpleNamespace
import unittest
from unittest import mock

import discord

from tests.test_newgame_worker import import_offline_runtime


leaderboard_workers = import_offline_runtime(
    'modules.leaderboard_workers'
)
leaderboard_v2 = import_offline_runtime('modules.leaderboard_v2')
games = import_offline_runtime('modules.games')


def result_with_rows(count=20, requester_id=777):
    return leaderboard_workers.PlayerLeaderboardResult(
        title='Individual Leaderboard',
        total_ranked=count,
        rows=tuple(
            leaderboard_workers.PlayerLeaderboardRow(
                rank=index,
                name=f'Showcase {index:02d}',
                elo=1620 - (index * 20),
                wins=max(0, 5 - index // 5),
                losses=index // 6,
                team_emoji='',
                discord_id=requester_id if index == 12 else 10_000 + index,
            )
            for index in range(1, count + 1)
        ),
    )


class LeaderboardV2LayoutTests(unittest.IsolatedAsyncioTestCase):
    def make_view(self, result=None, loader=None):
        return leaderboard_v2.ExperimentalLeaderboardView(
            guild_id=300,
            requester_id=777,
            result=result or result_with_rows(),
            loader=loader or mock.AsyncMock(return_value=result_with_rows()),
            active_cutoff=datetime.datetime(2025, 1, 1),
        )

    def test_uses_components_v2_container_and_no_embed(self):
        view = self.make_view()
        self.assertIsInstance(view, discord.ui.LayoutView)
        self.assertEqual(len(view.children), 1)
        self.assertIsInstance(view.children[0], discord.ui.Container)
        children = list(view.walk_children())
        self.assertTrue(any(
            isinstance(item, discord.ui.TextDisplay)
            for item in children
        ))
        selects = [
            item for item in children
            if isinstance(item, discord.ui.Select)
        ]
        self.assertEqual(len(selects), 1)
        self.assertEqual(
            [option.value for option in selects[0].options],
            [preset.key for preset in leaderboard_v2.PRESETS],
        )
        self.assertIn('Showcase 01', view.children[0].children[2].content)
        self.assertLessEqual(view.total_children_count, 40)
        payload = view.to_components()
        self.assertEqual(payload[0]['type'], 17)
        self.assertEqual(payload[0]['components'][0]['type'], 10)

    async def test_controls_are_requester_only(self):
        view = self.make_view()
        denied_response = SimpleNamespace(send_message=mock.AsyncMock())
        denied = SimpleNamespace(
            user=SimpleNamespace(id=888),
            response=denied_response,
        )
        allowed = SimpleNamespace(
            user=SimpleNamespace(id=777),
            response=denied_response,
        )
        self.assertFalse(await view.interaction_check(denied))
        denied_response.send_message.assert_awaited_once_with(
            'Only the requester can control this leaderboard.',
            ephemeral=True,
        )
        self.assertTrue(await view.interaction_check(allowed))

    async def test_select_loads_and_caches_a_new_preset(self):
        global_result = result_with_rows(12)
        loader = mock.AsyncMock(return_value=global_result)
        view = self.make_view(loader=loader)
        view.preset_select._values = ['global-current']
        response = SimpleNamespace(
            defer=mock.AsyncMock(),
            is_done=lambda: True,
        )
        interaction = SimpleNamespace(
            response=response,
            followup=SimpleNamespace(send=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(),
        )

        await view._select_preset(interaction)

        loader.assert_awaited_once()
        request = loader.await_args.args[0]
        self.assertEqual(request.scope, 'global')
        self.assertEqual(view.result, global_result)
        interaction.edit_original_response.assert_awaited_once_with(
            view=view,
        )

    async def test_page_and_my_rank_navigation_need_no_database_read(self):
        loader = mock.AsyncMock()
        view = self.make_view(loader=loader)
        response = SimpleNamespace(edit_message=mock.AsyncMock())
        interaction = SimpleNamespace(response=response)

        await view._next_page(interaction)
        self.assertEqual(view.page_index, 1)
        await view._show_requester_rank(interaction)
        self.assertEqual(view.page_index, 1)
        loader.assert_not_awaited()
        self.assertEqual(response.edit_message.await_count, 2)


class LeaderboardV2CommandTests(unittest.IsolatedAsyncioTestCase):
    def test_lb2_is_a_no_option_experimental_command(self):
        command = next(
            command
            for command in games.polygames.__cog_app_commands__
            if command.name == 'lb2'
        )
        self.assertEqual(command.parameters, [])
        self.assertIn('experimental', command.description.lower())

    async def test_command_defers_and_sends_only_layout_view(self):
        events = []
        result = result_with_rows()

        async def defer():
            events.append('defer')

        async def can_run(ctx):
            events.append('checks')
            return True

        async def load(request):
            events.append('load')
            return result

        async def edit_original_response(**kwargs):
            events.append('edit')
            self.assertEqual(set(kwargs), {'view'})
            self.assertIsInstance(
                kwargs['view'],
                leaderboard_v2.ExperimentalLeaderboardView,
            )
            return SimpleNamespace(edit=mock.AsyncMock())

        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=defer),
            guild=SimpleNamespace(id=300),
            user=SimpleNamespace(id=777),
            channel_id=400,
            edit_original_response=edit_original_response,
        )
        cog = games.polygames.__new__(games.polygames)
        cog.bot = SimpleNamespace()
        cog.lb = SimpleNamespace(can_run=can_run)
        cog._load_player_leaderboard = load
        command = next(
            command
            for command in games.polygames.__cog_app_commands__
            if command.name == 'lb2'
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
        ):
            await command.callback(cog, interaction)

        self.assertEqual(events, ['defer', 'checks', 'load', 'edit'])


if __name__ == '__main__':
    unittest.main()
