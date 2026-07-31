"""Offline coverage for shared leaderboard page-jump modals."""

from types import SimpleNamespace
import unittest
from unittest import mock

import discord

from tests.test_newgame_worker import import_offline_runtime


leaderboard_workers = import_offline_runtime(
    'modules.leaderboard_workers'
)
leaderboard_views = import_offline_runtime('modules.leaderboard_views')


def player_result(count=23):
    return leaderboard_workers.PlayerLeaderboardResult(
        title='Individual Leaderboard',
        total_ranked=count,
        rows=tuple(
            leaderboard_workers.PlayerLeaderboardRow(
                rank=index,
                name=f'Player {index}',
                elo=1500 - index,
                wins=index,
                losses=index // 2,
                team_emoji='',
            )
            for index in range(1, count + 1)
        ),
    )


def interaction(user_id=100):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        response=SimpleNamespace(
            send_modal=mock.AsyncMock(),
            send_message=mock.AsyncMock(),
            edit_message=mock.AsyncMock(),
        ),
    )


class LeaderboardJumpModalTests(unittest.IsolatedAsyncioTestCase):
    async def test_page_button_opens_bounded_numeric_modal(self):
        view = leaderboard_views.PlayerLeaderboardView(
            player_result(),
            requester_id=100,
        )
        target = interaction()

        await view.page_indicator.callback(target)

        target.response.send_modal.assert_awaited_once()
        modal = target.response.send_modal.await_args.args[0]
        self.assertIsInstance(
            modal,
            leaderboard_views.JumpToPageModal,
        )
        self.assertEqual(modal.page_label.text, 'Page number (1-3)')
        self.assertEqual(modal.page_number.default, '1')
        self.assertEqual(view.page_indicator.label, 'Page 1/3')
        self.assertFalse(view.page_indicator.disabled)

    async def test_valid_first_middle_and_last_pages_update_public_message(self):
        view = leaderboard_views.PlayerLeaderboardView(
            player_result(),
            requester_id=100,
        )

        for page_number in (1, 2, 3):
            with self.subTest(page_number=page_number):
                modal = leaderboard_views.JumpToPageModal(view)
                modal.page_number._value = str(page_number)
                target = interaction()

                await modal.on_submit(target)

                self.assertEqual(view.page_index, page_number - 1)
                target.response.edit_message.assert_awaited_once()
                kwargs = target.response.edit_message.await_args.kwargs
                self.assertIsInstance(kwargs['embed'], discord.Embed)
                self.assertIs(kwargs['view'], view)
                self.assertEqual(
                    view.page_indicator.label,
                    f'Page {page_number}/3',
                )
                target.response.send_message.assert_not_awaited()

    async def test_invalid_page_values_are_ephemeral(self):
        view = leaderboard_views.PlayerLeaderboardView(
            player_result(),
            requester_id=100,
        )

        for value in ('x', '0', '4'):
            with self.subTest(value=value):
                modal = leaderboard_views.JumpToPageModal(view)
                modal.page_number._value = value
                target = interaction()

                await modal.on_submit(target)

                target.response.send_message.assert_awaited_once_with(
                    'Enter a page number from 1 to 3.',
                    ephemeral=True,
                )
                target.response.edit_message.assert_not_awaited()

    async def test_unauthorized_and_expired_submissions_are_rejected(self):
        view = leaderboard_views.PlayerLeaderboardView(
            player_result(),
            requester_id=100,
        )
        modal = leaderboard_views.JumpToPageModal(view)
        modal.page_number._value = '2'
        denied = interaction(user_id=200)

        await modal.on_submit(denied)

        denied.response.send_message.assert_awaited_once_with(
            'Only the requester can change this leaderboard page.',
            ephemeral=True,
        )
        denied.response.edit_message.assert_not_awaited()

        view.stop()
        expired_modal = leaderboard_views.JumpToPageModal(view)
        expired_modal.page_number._value = '2'
        expired = interaction()

        await expired_modal.on_submit(expired)

        expired.response.send_message.assert_awaited_once_with(
            'This leaderboard paginator has expired. Run the command '
            'again for a fresh result.',
            ephemeral=True,
        )
        expired.response.edit_message.assert_not_awaited()

    async def test_timeout_disables_jump_with_other_controls(self):
        view = leaderboard_views.PlayerLeaderboardView(
            player_result(),
            requester_id=100,
        )

        await view.on_timeout()

        self.assertTrue(view.page_indicator.disabled)
        self.assertTrue(
            all(
                item.disabled
                for item in view.children
                if isinstance(item, discord.ui.Button)
            )
        )
