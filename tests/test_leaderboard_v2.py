"""Offline coverage for the promoted Components v2 leaderboard."""

import asyncio
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
components_v2 = import_offline_runtime('modules.components_v2')
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
        self.assertEqual(selects[0].placeholder, 'Common filters')
        self.assertEqual(
            [option.value for option in selects[0].options],
            [preset.key for preset in leaderboard_v2.PRESETS],
        )
        self.assertEqual(
            [option.value for option in selects[0].options if option.default],
            ['local-current'],
        )
        header = view.children[0].children[0].content
        self.assertIn('**Scope:** This server', header)
        self.assertIn('**Rating:** Current', header)
        self.assertIn('**Era:** Current era', header)
        self.assertIn('**Population:** Active', header)
        self.assertIn('do not redefine W–L', view.children[0].children[4].content)
        advanced_buttons = [
            item for item in children
            if isinstance(item, discord.ui.Button)
            and item.label == 'Advanced filters...'
        ]
        self.assertEqual(len(advanced_buttons), 1)
        self.assertFalse(hasattr(view, 'advanced_select'))
        page_buttons = [
            item for item in children
            if isinstance(item, discord.ui.Button)
            and item.label.startswith('Jump to page')
        ]
        self.assertEqual(len(page_buttons), 1)
        self.assertEqual(
            len([
                item for item in children
                if isinstance(item, discord.ui.Select)
            ]),
            1,
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
            'Only the requester can control this result.',
            ephemeral=True,
        )
        self.assertTrue(await view.interaction_check(allowed))

    async def test_advanced_button_opens_requester_bound_modal(self):
        view = self.make_view()
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=777),
            response=SimpleNamespace(send_modal=mock.AsyncMock()),
        )

        await view._open_advanced_filters(interaction)

        interaction.response.send_modal.assert_awaited_once()
        modal = interaction.response.send_modal.await_args.args[0]
        self.assertIsInstance(
            modal,
            leaderboard_v2.PlayerLeaderboardAdvancedFiltersModal,
        )
        self.assertIs(modal.workspace, view)
        self.assertEqual(modal.title, 'Advanced leaderboard filters')

    def test_advanced_modal_fields_options_and_defaults(self):
        view = self.make_view()
        view.preset_key = 'global:peak:all-time'
        view.population = 'all'
        modal = leaderboard_v2.PlayerLeaderboardAdvancedFiltersModal(view)

        labels = {item.text: item for item in modal.children}
        self.assertEqual(
            set(labels),
            {'Scope', 'Rating', 'Era', 'Population'},
        )
        expected = {
            'Scope': (['This server', 'Global'], ['global']),
            'Rating': (['Current', 'Peak'], ['peak']),
            'Era': (['Current era', 'All time'], ['all-time']),
            'Population': (['Active', 'All registered'], ['all']),
        }
        for label, (option_labels, defaults) in expected.items():
            options = labels[label].component.options
            self.assertEqual([option.label for option in options], option_labels)
            self.assertEqual(
                [option.value for option in options if option.default],
                defaults,
            )

        serialized = modal.to_components()
        self.assertEqual(len(serialized), 4)
        self.assertTrue(all(component['type'] == 18 for component in serialized))

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

        view.preset_select._values = ['global-current']
        cached_interaction = SimpleNamespace(
            response=SimpleNamespace(
                is_done=lambda: False,
                edit_message=mock.AsyncMock(),
            ),
        )
        await view._select_preset(cached_interaction)
        loader.assert_awaited_once()
        cached_interaction.response.edit_message.assert_awaited_once_with(
            view=view,
        )

    def test_all_sixteen_modal_combinations_map_to_existing_loader_keys(self):
        requests = []

        async def load(request):
            requests.append(request)
            return result_with_rows()

        view = self.make_view(loader=load)
        cache_keys = {
            leaderboard_v2._cache_key_for_filters(*key.split(':'))
            for key in leaderboard_v2.FILTER_KEYS
        }
        self.assertEqual(len(cache_keys), 16)

        async def exercise():
            for cache_key in cache_keys:
                await view._load_request_key(cache_key)

        asyncio.run(exercise())
        self.assertEqual(
            {
                (
                    request.scope,
                    request.rating,
                    request.era,
                    request.population,
                )
                for request in requests
            },
            {
                tuple(key.split(':'))
                for key in leaderboard_v2.FILTER_KEYS
            },
        )

    async def test_modal_submission_resets_page_and_uses_uncached_loader(self):
        loaded = result_with_rows(12)
        loader = mock.AsyncMock(return_value=loaded)
        view = self.make_view(loader=loader)
        view.page_index = 2
        modal = leaderboard_v2.PlayerLeaderboardAdvancedFiltersModal(view)
        modal.scope.component._value = 'global'
        modal.rating.component._value = 'peak'
        modal.era.component._value = 'all-time'
        modal.population.component._value = 'all'
        response = SimpleNamespace(
            defer=mock.AsyncMock(),
            is_done=lambda: True,
            send_message=mock.AsyncMock(),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=777),
            response=response,
            edit_original_response=mock.AsyncMock(),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )

        await modal.on_submit(interaction)

        loader.assert_awaited_once()
        request = loader.await_args.args[0]
        self.assertEqual(
            (
                request.scope,
                request.rating,
                request.era,
                request.population,
            ),
            ('global', 'peak', 'all-time', 'all'),
        )
        self.assertEqual(view.page_index, 0)
        self.assertEqual(view.preset_key, 'global:peak:all-time')
        self.assertEqual(view.population, 'all')
        header = view.children[0].children[0].content
        self.assertIn('**Scope:** Global', header)
        self.assertIn('**Rating:** Peak', header)
        self.assertIn('**Era:** All time', header)
        self.assertIn('**Population:** All registered', header)
        interaction.edit_original_response.assert_awaited_once_with(
            view=view,
        )

    async def test_modal_default_selection_uses_initial_cache(self):
        loader = mock.AsyncMock(return_value=result_with_rows())
        view = self.make_view(loader=loader)
        modal = leaderboard_v2.PlayerLeaderboardAdvancedFiltersModal(view)
        modal.scope.component._value = 'local'
        modal.rating.component._value = 'current'
        modal.era.component._value = 'current'
        modal.population.component._value = 'active'
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=777),
            response=SimpleNamespace(
                is_done=lambda: False,
                edit_message=mock.AsyncMock(),
                send_message=mock.AsyncMock(),
            ),
        )

        await modal.on_submit(interaction)

        loader.assert_not_awaited()
        interaction.response.edit_message.assert_awaited_once_with(view=view)

    async def test_modal_load_failure_rolls_back_page_and_filter_state_privately(self):
        old_result = result_with_rows(20)
        loader = mock.AsyncMock(side_effect=ValueError('unavailable'))
        view = self.make_view(result=old_result, loader=loader)
        view.page_index = 2
        modal = leaderboard_v2.PlayerLeaderboardAdvancedFiltersModal(view)
        modal.scope.component._value = 'global'
        modal.rating.component._value = 'peak'
        modal.era.component._value = 'current'
        modal.population.component._value = 'all'
        response = SimpleNamespace(
            defer=mock.AsyncMock(),
            is_done=lambda: True,
            send_message=mock.AsyncMock(),
        )
        followup = SimpleNamespace(send=mock.AsyncMock())
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=777),
            response=response,
            followup=followup,
            edit_original_response=mock.AsyncMock(),
        )

        await modal.on_submit(interaction)

        self.assertEqual(view.result, old_result)
        self.assertEqual(view.preset_key, 'local-current')
        self.assertEqual(view.population, 'active')
        self.assertEqual(view.page_index, 2)
        interaction.edit_original_response.assert_not_awaited()
        followup.send.assert_awaited_once_with(
            'Could not load that view: unavailable',
            ephemeral=True,
        )

    async def test_modal_rechecks_requester_and_live_workspace(self):
        view = self.make_view(loader=mock.AsyncMock())
        modal = leaderboard_v2.PlayerLeaderboardAdvancedFiltersModal(view)
        denied_response = SimpleNamespace(
            is_done=lambda: False,
            send_message=mock.AsyncMock(),
        )
        denied = SimpleNamespace(
            user=SimpleNamespace(id=888),
            response=denied_response,
        )

        await modal.on_submit(denied)
        denied_response.send_message.assert_awaited_once_with(
            'Only the requester can change this leaderboard filter.',
            ephemeral=True,
        )

        view.stop()
        expired_modal = leaderboard_v2.PlayerLeaderboardAdvancedFiltersModal(view)
        expired_response = SimpleNamespace(
            is_done=lambda: False,
            send_message=mock.AsyncMock(),
        )
        expired = SimpleNamespace(
            user=SimpleNamespace(id=777),
            response=expired_response,
        )
        await expired_modal.on_submit(expired)
        expired_response.send_message.assert_awaited_once_with(
            view.expired_message,
            ephemeral=True,
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

    async def test_timeout_recursively_disables_serializable_controls(self):
        view = self.make_view()
        view.message = SimpleNamespace(edit=mock.AsyncMock())
        await view.on_timeout()
        controls = [
            item for item in view.walk_children()
            if isinstance(item, (discord.ui.Button, discord.ui.Select))
        ]
        self.assertTrue(controls)
        self.assertTrue(all(item.disabled for item in controls))
        self.assertLessEqual(view.total_children_count, 40)
        self.assertEqual(view.to_components()[0]['type'], 17)
        view.message.edit.assert_awaited_once_with(view=view)

    async def test_expired_page_modal_has_rerun_guidance(self):
        view = self.make_view()
        view.stop()
        modal = components_v2.PageJumpModal(view)
        response = SimpleNamespace(send_message=mock.AsyncMock())
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=777),
            response=response,
        )
        await modal.on_submit(interaction)
        response.send_message.assert_awaited_once()
        self.assertIn(
            'Run the command again',
            response.send_message.await_args.args[0],
        )


class LeaderboardV2CommandTests(unittest.IsolatedAsyncioTestCase):
    def test_players_is_no_option_and_lb2_is_removed(self):
        group = next(
            command for command in games.polygames.__cog_app_commands__
            if command.name == 'leaderboard'
        )
        command = next(command for command in group.commands
                       if command.name == 'players')
        self.assertEqual(command.parameters, [])
        self.assertNotIn(
            'lb2',
            {command.name for command in games.polygames.__cog_app_commands__},
        )

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
        group = next(
            command for command in games.polygames.__cog_app_commands__
            if command.name == 'leaderboard'
        )
        command = next(command for command in group.commands
                       if command.name == 'players')
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
