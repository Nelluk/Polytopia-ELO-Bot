"""Offline coverage for immutable rank/unstart correction publication."""

import datetime
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.test_newgame_worker import import_offline_runtime


publication = import_offline_runtime('modules.game_correction_publication')
publication_workers = import_offline_runtime(
    'modules.game_result_publication_workers'
)
game_workers = import_offline_runtime('modules.game_workers')
administration = import_offline_runtime('modules.administration')
game_detail_workers = import_offline_runtime('modules.game_detail_workers')


def snapshot(*, ranked=True, pending=False):
    return publication_workers.GameResultPublicationSnapshot(
        game=game_detail_workers.GameDetailSnapshot(
            game_id=42,
            guild_id=300,
            name='Frozen Game',
            date='2026-08-10',
            completed_ts='',
            win_claimed_ts='',
            expiration='2026-08-11 12:00:00',
            is_ranked=ranked,
            is_pending=pending,
            is_completed=False,
            is_confirmed=False,
            is_mobile=True,
            map_type='',
            notes='',
            league_season=None,
            league_tier=None,
            league_playoff=False,
            size=(1, 1),
            game_channel_id=None,
            host_discord_id=None,
            host_name='',
            winner_side_id=None,
            status_label='Open' if pending else 'Incomplete',
            result_label='',
            inferred_from_channel=False,
            cross_guild=False,
            sides=(),
        ),
        roster_mentions=('<@1>', '<@2>'),
        side_channel_targets=(),
        game_channel_id=None,
    )


class CorrectionPublisherTests(unittest.IsolatedAsyncioTestCase):
    async def test_ranked_state_publisher_is_model_free(self):
        value = snapshot()
        publish_channels = mock.AsyncMock()
        self.assertNotIn('models', vars(publication))

        with mock.patch.object(
            publication.confirmation_publication,
            'publish_game_channels',
            new=publish_channels,
        ):
            await publication.publish_ranked_state(
                value,
                requester_display_name='Staff',
                bot='bot',
            )

        publish_channels.assert_awaited_once_with(
            value,
            bot='bot',
            message='Staff member **Staff** has set this game to be *ranked*.',
        )

    async def test_unstart_announcement_renders_only_frozen_snapshot(self):
        value = snapshot(pending=True)
        message = SimpleNamespace(
            attachments=(),
            edit=mock.AsyncMock(),
        )
        channel = SimpleNamespace(
            fetch_message=mock.AsyncMock(return_value=message),
        )
        guild = SimpleNamespace(get_channel=lambda channel_id: channel)
        rendered = object()
        captured = {}

        def resolve_display(game, **kwargs):
            captured['game'] = game
            captured['kwargs'] = kwargs
            return 'display'

        with mock.patch.object(
            publication.game_detail_views,
            'resolve_display',
            side_effect=resolve_display,
        ), mock.patch.object(
            publication.game_detail_views,
            'render_classic_game_detail',
            return_value=rendered,
        ), mock.patch.object(
            publication.game_detail_views,
            'classic_edit_kwargs',
            return_value={'embed': 'embed', 'content': None, 'view': None},
        ):
            await publication.publish_cancelled_unstart_announcement(
                value,
                game_name='Frozen Game',
                announcement_channel_id=700,
                announcement_message_id=701,
                guild=guild,
                prefix='$',
                bot='bot',
            )

        channel.fetch_message.assert_awaited_once_with(701)
        self.assertFalse(captured['game'].is_pending)
        self.assertEqual(
            captured['game'].name,
            '~~Frozen Game~~ GAME CANCELLED',
        )
        self.assertTrue(value.game.is_pending)
        self.assertEqual(captured['kwargs']['bot'], 'bot')
        message.edit.assert_awaited_once_with(embed='embed', content=None)

    async def test_unstart_announcement_failure_is_typed(self):
        guild = SimpleNamespace(get_channel=lambda _channel_id: None)
        with self.assertRaises(
            publication.GameCorrectionPublicationError
        ):
            await publication.publish_cancelled_unstart_announcement(
                snapshot(pending=True),
                game_name='Frozen Game',
                announcement_channel_id=700,
                announcement_message_id=701,
                guild=guild,
                prefix='$',
            )


class CorrectionApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_rank_success_uses_snapshot_without_event_loop_orm(self):
        value = snapshot()
        result = game_workers.RankedStateResult(42, True, value)
        cog = administration.administration.__new__(administration.administration)
        with mock.patch.object(
            administration.utilities, 'lock_game'
        ), mock.patch.object(
            administration.utilities, 'unlock_game'
        ), mock.patch.object(
            administration.models.GameLog,
            'member_string',
            return_value='Staff',
        ), mock.patch.object(
            administration.game_workers,
            'run_ranked_state_correction',
            new=mock.AsyncMock(return_value=result),
        ), mock.patch.object(
            administration.game_correction_publication,
            'publish_ranked_state',
            new=mock.AsyncMock(),
        ) as publisher, mock.patch.object(
            administration.models.Game,
            'load_full_game',
            side_effect=AssertionError('event-loop ORM load'),
        ) as load_game:
            message = await cog._set_ranked_state_and_post(
                game_id=42,
                guild=SimpleNamespace(id=300),
                is_ranked=True,
                requester=SimpleNamespace(display_name='Staff'),
            )

        load_game.assert_not_called()
        publisher.assert_awaited_once()
        self.assertIn('Notifying players: <@1> <@2>', message)

    async def test_rank_snapshot_failure_reports_committed_state(self):
        committed = game_workers.RankedStateResult(42, True)
        cog = administration.administration.__new__(administration.administration)
        with mock.patch.object(
            administration.utilities, 'lock_game'
        ), mock.patch.object(
            administration.utilities, 'unlock_game'
        ), mock.patch.object(
            administration.models.GameLog,
            'member_string',
            return_value='Staff',
        ), mock.patch.object(
            administration.game_workers,
            'run_ranked_state_correction',
            new=mock.AsyncMock(
                side_effect=game_workers.RankedStateSnapshotError(committed)
            ),
        ), mock.patch.object(administration.logger, 'exception'):
            message = await cog._set_ranked_state_and_post(
                game_id=42,
                guild=SimpleNamespace(id=300),
                is_ranked=True,
                requester=SimpleNamespace(display_name='Staff'),
            )

        self.assertIn('is now marked as ranked', message)
        self.assertIn('Do not run the correction again', message)

    async def test_rank_publication_failure_reports_committed_state(self):
        value = snapshot()
        result = game_workers.RankedStateResult(42, True, value)
        cog = administration.administration.__new__(administration.administration)
        with mock.patch.object(
            administration.utilities, 'lock_game'
        ), mock.patch.object(
            administration.utilities, 'unlock_game'
        ), mock.patch.object(
            administration.models.GameLog,
            'member_string',
            return_value='Staff',
        ), mock.patch.object(
            administration.game_workers,
            'run_ranked_state_correction',
            new=mock.AsyncMock(return_value=result),
        ), mock.patch.object(
            administration.game_correction_publication,
            'publish_ranked_state',
            new=mock.AsyncMock(
                side_effect=publication.GameCorrectionPublicationError()
            ),
        ), mock.patch.object(administration.logger, 'exception'):
            message = await cog._set_ranked_state_and_post(
                game_id=42,
                guild=SimpleNamespace(id=300),
                is_ranked=True,
                requester=SimpleNamespace(display_name='Staff'),
            )

        self.assertIn('is now marked as ranked', message)
        self.assertIn('game-channel notice failed', message)
        self.assertIn('Do not run the correction again', message)

    async def test_unstart_snapshot_failure_continues_frozen_cleanup(self):
        target = game_workers.GameChannelTarget(None, 900, 300)
        committed = game_workers.GameUnstartResult(
            42,
            'Frozen Game',
            700,
            701,
            ('<@1>', '<@2>'),
            (target,),
            datetime.datetime(2026, 8, 11),
        )
        guild = SimpleNamespace(id=300)
        cog = administration.administration.__new__(administration.administration)
        cog.bot = SimpleNamespace(guilds=[guild])
        with mock.patch.object(
            administration.utilities, 'lock_game'
        ), mock.patch.object(
            administration.utilities, 'unlock_game'
        ), mock.patch.object(
            administration.models.GameLog,
            'member_string',
            return_value='Staff',
        ), mock.patch.object(
            administration.game_workers,
            'run_game_unstart',
            new=mock.AsyncMock(
                side_effect=game_workers.GameUnstartSnapshotError(committed)
            ),
        ), mock.patch.object(
            administration.channels,
            'delete_game_channel',
            new=mock.AsyncMock(return_value=True),
        ), mock.patch.object(
            administration.game_workers,
            'run_deleted_channel_reconciliation',
            new=mock.AsyncMock(return_value=1),
        ), mock.patch.object(
            administration.game_correction_publication,
            'publish_cancelled_unstart_announcement',
            new=mock.AsyncMock(),
        ) as publisher, mock.patch.object(administration.logger, 'exception'):
            message = await cog._unstart_game_and_post(
                game_id=42,
                guild=guild,
                prefix='$',
                requester=SimpleNamespace(display_name='Staff'),
                invocation_channel_id=999,
            )

        publisher.assert_not_awaited()
        self.assertIn('is now an open game', message)
        self.assertIn('committed announcement snapshot', message)
        self.assertIn('<@1> <@2>', message)

    async def test_unstart_publication_failure_reports_committed_cleanup(self):
        value = snapshot(pending=True)
        committed = game_workers.GameUnstartResult(
            42,
            'Frozen Game',
            700,
            701,
            ('<@1>', '<@2>'),
            (),
            datetime.datetime(2026, 8, 11),
            value,
        )
        guild = SimpleNamespace(id=300)
        cog = administration.administration.__new__(administration.administration)
        cog.bot = SimpleNamespace(guilds=[guild])
        with mock.patch.object(
            administration.utilities, 'lock_game'
        ), mock.patch.object(
            administration.utilities, 'unlock_game'
        ), mock.patch.object(
            administration.models.GameLog,
            'member_string',
            return_value='Staff',
        ), mock.patch.object(
            administration.game_workers,
            'run_game_unstart',
            new=mock.AsyncMock(return_value=committed),
        ), mock.patch.object(
            administration.game_correction_publication,
            'publish_cancelled_unstart_announcement',
            new=mock.AsyncMock(
                side_effect=publication.GameCorrectionPublicationError()
            ),
        ), mock.patch.object(administration.logger, 'exception'):
            message = await cog._unstart_game_and_post(
                game_id=42,
                guild=guild,
                prefix='$',
                requester=SimpleNamespace(display_name='Staff'),
                invocation_channel_id=999,
            )

        self.assertIn('is now an open game', message)
        self.assertIn('game announcement was not updated', message)
