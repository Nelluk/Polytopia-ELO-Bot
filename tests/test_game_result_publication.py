"""Offline coverage for ordinary win/unwin immutable publication."""

from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.test_newgame_worker import import_offline_runtime


publication = import_offline_runtime('modules.game_result_publication')
publication_workers = import_offline_runtime(
    'modules.game_result_publication_workers'
)
game_unwin = import_offline_runtime('modules.game_unwin')
elo_workers = import_offline_runtime('modules.elo_workers')


def snapshot(*, confirmed=False):
    return publication_workers.GameResultPublicationSnapshot(
        game=SimpleNamespace(name='Frozen Game'),
        roster_mentions=('<@200>', '<@201>'),
        side_channel_targets=(),
        game_channel_id=None,
        experience_roles=(object(),),
        champion_roles=object(),
        confirmed_publication=(object() if confirmed else None),
    )


class GameResultPublisherTests(unittest.IsolatedAsyncioTestCase):
    def test_snapshot_is_frozen(self):
        value = snapshot()
        with self.assertRaises(FrozenInstanceError):
            value.game_channel_id = 12

    async def test_unwin_publisher_is_model_free(self):
        value = snapshot()
        channel = SimpleNamespace(send=mock.AsyncMock())
        bot = object()
        self.assertNotIn('models', vars(publication))

        with (
            mock.patch.object(
                publication.confirmation_publication,
                'publish_game_channels',
                new=mock.AsyncMock(),
            ) as publish_channels,
            mock.patch.object(
                publication.confirmation_publication,
                'publish_experience_role',
                new=mock.AsyncMock(),
            ) as publish_experience,
            mock.patch.object(
                publication.confirmation_publication,
                'publish_champion_roles',
                new=mock.AsyncMock(),
            ) as publish_champion,
        ):
            await publication.publish_unwin_result(
                snapshot=value,
                current_channel=channel,
                previously_confirmed=True,
                bot=bot,
            )

        publish_channels.assert_awaited_once_with(
            value,
            bot=bot,
            message='The game has reset to *Incomplete* status.',
        )
        publish_experience.assert_awaited_once_with(
            value.experience_roles[0],
            bot,
        )
        publish_champion.assert_awaited_once_with(value.champion_roles, bot)
        self.assertIn('<@200> <@201>', channel.send.await_args.args[0])

    async def test_confirmed_win_passes_only_snapshot_to_publisher(self):
        value = snapshot(confirmed=True)
        request = SimpleNamespace(
            game_id=77,
            requester_name='Tester',
            prefix='$',
            winning_side_id=1,
            winner_text='Alpha',
        )
        result = SimpleNamespace(
            previous_winner_name=None,
            winner_name='Alpha',
            confirmed=True,
            all_sides_confirmed=True,
        )
        confirmed_publisher = mock.AsyncMock()
        send_public = mock.AsyncMock()

        with mock.patch.object(
            publication.confirmation_publication,
            'publish_game_channels',
            new=mock.AsyncMock(),
        ):
            await publication.publish_win_result(
                request=request,
                result=result,
                snapshot=value,
                guild='guild',
                current_channel='channel',
                send_public=send_public,
                confirmed_publisher=confirmed_publisher,
                bot='bot',
            )

        confirmed_publisher.assert_awaited_once_with(
            'guild',
            '$',
            'channel',
            value.confirmed_publication,
        )


class UnwinApplicationTests(unittest.IsolatedAsyncioTestCase):
    def request(self):
        return game_unwin.UnwinRequest(
            game_id=42,
            guild_id=100,
            requester_id=200,
            requester_name='Tester',
            requester_mention='<@200>',
            requester_description='**Tester** (`200`)',
            requester_is_staff=True,
            prefix='$',
        )

    async def test_success_passes_frozen_snapshot_and_context_to_worker(self):
        value = snapshot()
        result = elo_workers.UnwinResult(
            game_id=42,
            message='complete',
            post_unwin_messaging=True,
            previously_confirmed=False,
            publication=value,
        )
        captured = {}

        class Coordinator:
            active_job = None

            async def run(self, **kwargs):
                captured.update(kwargs)
                kwargs['before_submit']()
                kwargs['after_complete']()
                return result

        context = object()
        publisher = mock.AsyncMock()
        sent = mock.AsyncMock()
        with (
            mock.patch.object(
                game_unwin.settings,
                'elo_job_coordinator',
                Coordinator(),
            ),
            mock.patch.object(
                game_unwin.settings,
                'bot',
                SimpleNamespace(guilds=[]),
            ),
            mock.patch.object(
                game_unwin.game_result_publication,
                'capture_publication_context',
                return_value=context,
            ),
            mock.patch.object(game_unwin.utilities, 'lock_game') as lock_game,
            mock.patch.object(game_unwin.utilities, 'unlock_game') as unlock_game,
        ):
            outcome = await game_unwin.run_unwin(
                self.request(),
                guild='guild',
                current_channel='channel',
                send=sent,
                post_unwin_publisher=publisher,
            )

        self.assertTrue(outcome.public_effects_published)
        self.assertIs(captured['worker_args'][-1], context)
        publisher.assert_awaited_once_with(
            'guild',
            '$',
            'channel',
            value,
            previously_confirmed=False,
        )
        sent.assert_awaited_once_with('complete')
        lock_game.assert_called_once_with(42)
        unlock_game.assert_called_once_with(42)

    async def test_snapshot_failure_reports_committed_reconciliation(self):
        committed = elo_workers.UnwinResult(
            game_id=42,
            message='committed',
            post_unwin_messaging=True,
            previously_confirmed=True,
        )

        class Coordinator:
            active_job = None

            async def run(self, **_kwargs):
                raise elo_workers.UnwinSnapshotError(committed)

        sent = mock.AsyncMock()
        publisher = mock.AsyncMock()
        with mock.patch.object(
            game_unwin.settings,
            'elo_job_coordinator',
            Coordinator(),
        ), mock.patch.object(
            game_unwin.game_result_publication,
            'capture_publication_context',
            return_value=object(),
        ), mock.patch.object(game_unwin.logger, 'exception'):
            outcome = await game_unwin.run_unwin(
                self.request(),
                guild='guild',
                current_channel='channel',
                send=sent,
                post_unwin_publisher=publisher,
            )

        self.assertFalse(outcome.public_effects_published)
        publisher.assert_not_awaited()
        warning = sent.await_args.args[0]
        self.assertIn('was reset', warning)
        self.assertIn('do not run unwin again', warning)
        self.assertNotIn('No Discord channel updates', warning)

    async def test_publication_failure_after_commit_is_reconciliation(self):
        value = snapshot()
        result = elo_workers.UnwinResult(
            game_id=42,
            message='complete',
            post_unwin_messaging=True,
            previously_confirmed=True,
            publication=value,
        )

        class Coordinator:
            active_job = None

            async def run(self, **_kwargs):
                return result

        sent = mock.AsyncMock()
        with mock.patch.object(
            game_unwin.settings,
            'elo_job_coordinator',
            Coordinator(),
        ), mock.patch.object(
            game_unwin.game_result_publication,
            'capture_publication_context',
            return_value=object(),
        ), mock.patch.object(game_unwin.logger, 'exception'):
            outcome = await game_unwin.run_unwin(
                self.request(),
                guild='guild',
                current_channel='channel',
                send=sent,
                post_unwin_publisher=mock.AsyncMock(
                    side_effect=RuntimeError('Discord failed')
                ),
            )

        self.assertFalse(outcome.public_effects_published)
        warning = sent.await_args.args[0]
        self.assertIn('was reset', warning)
        self.assertIn('could not be fully published', warning)
