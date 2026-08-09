"""Focused tests for immutable metadata post-commit presentation helpers."""

from types import SimpleNamespace
import inspect
import unittest
from unittest import mock

from tests.test_newgame_worker import import_offline_runtime


presentation = import_offline_runtime('modules.game_metadata_presentation')
game_join_leave = import_offline_runtime('modules.game_join_leave')
game_side = import_offline_runtime('modules.game_side')
game_notes = import_offline_runtime('modules.game_notes')
game_map = import_offline_runtime('modules.game_map')
game_name = import_offline_runtime('modules.game_name')
game_tribe = import_offline_runtime('modules.game_tribe')


class ImmutableMetadataPresentationTests(unittest.IsolatedAsyncioTestCase):
    async def test_load_card_delegates_with_primitive_identity(self):
        expected = SimpleNamespace()
        guild = SimpleNamespace(id=300)
        bot = SimpleNamespace()
        with mock.patch.object(
            game_join_leave,
            'load_post_commit_game_card',
            new=mock.AsyncMock(return_value=expected),
        ) as loader:
            result = await presentation.load_card(
                game_id='42',
                guild=guild,
                bot=bot,
                prefix='$',
                presentation='slash',
                requester_id='100',
                channel_id='900',
            )

        self.assertIs(result, expected)
        loader.assert_awaited_once_with(
            game_id=42,
            guild=guild,
            bot=bot,
            prefix='$',
            presentation='slash',
            requester_id=100,
            channel_id=900,
        )

    async def test_announcement_refresh_preserves_existing_view(self):
        message = SimpleNamespace(edit=mock.AsyncMock())
        channel = SimpleNamespace(fetch_message=mock.AsyncMock(return_value=message))
        guild = SimpleNamespace(get_channel=lambda channel_id: channel)
        rendered = SimpleNamespace()
        card = SimpleNamespace(rendered=rendered)
        edit_kwargs = {
            'content': 'dense card',
            'embed': object(),
            'attachments': [object()],
            'view': None,
        }
        with mock.patch.object(
            presentation.game_detail_views,
            'classic_edit_kwargs',
            return_value=edit_kwargs,
        ) as edit_builder:
            refreshed = await presentation.refresh_announcement(
                card,
                guild=guild,
                channel_id=901,
                message_id=902,
            )

        self.assertTrue(refreshed)
        channel.fetch_message.assert_awaited_once_with(902)
        edit_builder.assert_called_once_with(message, rendered)
        message.edit.assert_awaited_once()
        self.assertNotIn('view', message.edit.await_args.kwargs)
        self.assertEqual(
            message.edit.await_args.kwargs['attachments'],
            edit_kwargs['attachments'],
        )

    async def test_dense_card_uses_shared_fresh_attachment_sender(self):
        destination = SimpleNamespace()
        rendered = SimpleNamespace(content='dense card')
        card = SimpleNamespace(rendered=rendered)
        with mock.patch.object(
            game_join_leave,
            'send_post_commit_game_card',
            new=mock.AsyncMock(),
        ) as sender:
            await presentation.send_dense_card(destination, card)
        sender.assert_awaited_once_with(
            destination,
            card,
            content='dense card',
        )

    async def test_channel_renames_are_derived_from_frozen_snapshot(self):
        source = SimpleNamespace(id=300)
        external = SimpleNamespace(id=301)
        snapshot = SimpleNamespace(
            game_id=42,
            guild_id=300,
            name='New Name',
            league_season=None,
            league_tier=None,
            league_playoff=False,
            game_channel_id=903,
            sides=(
                SimpleNamespace(
                    side_id=1,
                    channel_id=901,
                    external_guild_id=None,
                    team_name='Alpha',
                ),
                SimpleNamespace(
                    side_id=2,
                    channel_id=902,
                    external_guild_id=301,
                    team_name='Beta',
                ),
            ),
        )
        card = SimpleNamespace(snapshot=snapshot)
        with mock.patch.object(
            presentation.channels,
            'update_game_channel_name',
            new=mock.AsyncMock(),
        ) as rename:
            await presentation.rename_game_channels(
                card,
                guild=source,
                guild_list=(source, external),
            )

        self.assertEqual(rename.await_count, 3)
        calls = rename.await_args_list
        self.assertIs(calls[0].args[0], source)
        self.assertEqual(calls[0].kwargs['team_name'], 'Alpha')
        self.assertIs(calls[1].args[0], external)
        self.assertEqual(calls[1].kwargs['team_name'], 'Beta')
        self.assertIs(calls[2].args[0], source)
        self.assertIsNone(calls[2].kwargs['team_name'])
        for call in calls:
            game = call.kwargs['game']
            self.assertEqual(game.id, 42)
            self.assertEqual(game.name, 'New Name')

    def test_metadata_presenters_do_not_reload_live_game_models(self):
        presenters = (
            game_side.reconcile_game_presentation,
            game_notes.refresh_game_card,
            game_map.publish_mutation_result,
            game_name.publish_mutation_result,
            game_tribe.reconcile_game_presentation,
        )
        for presenter in presenters:
            with self.subTest(presenter=presenter.__qualname__):
                source = inspect.getsource(presenter)
                self.assertNotIn('Game.load_full_game', source)
                self.assertNotIn('.embed(', source)
                self.assertNotIn('send_game_embed', source)


if __name__ == '__main__':
    unittest.main()
