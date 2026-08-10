"""Offline coverage for the shared win application boundary."""

from contextlib import AbstractContextManager
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import unittest
from unittest import mock

import peewee

from tests.test_newgame_worker import import_offline_runtime


game_win = import_offline_runtime('modules.game_win')
workers = import_offline_runtime('modules.game_win_workers')
elo_workers = import_offline_runtime('modules.elo_workers')
elo_jobs = import_offline_runtime('modules.elo_jobs')


class WinServiceBoundaryTests(unittest.TestCase):
    def test_request_is_frozen_and_contains_only_primitive_values(self):
        request = game_win.WinRequest(
            game_id=77,
            guild_id=10,
            requester_id=900,
            requester_name='Tester',
            requester_mention='<@900>',
            requester_description='**Tester** (`900`)',
            requester_is_staff=False,
            prefix='!',
            winner_text='Side 1 — Alpha',
            winning_side_id=101,
        )
        with self.assertRaises(FrozenInstanceError):
            request.game_id = 78
        self.assertTrue(all(
            isinstance(value, (int, str, bool, type(None)))
            for value in (
                request.game_id,
                request.guild_id,
                request.requester_id,
                request.requester_name,
                request.requester_mention,
                request.requester_description,
                request.requester_is_staff,
                request.prefix,
                request.winner_text,
                request.winning_side_id,
            )
        ))

    def test_preflight_reloads_and_maps_stable_side_id_on_worker_connection(self):
        events = []

        class Connection(AbstractContextManager):
            def __enter__(self):
                events.append('open')
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                events.append('close')

        side_one = SimpleNamespace(
            id=101,
            name=lambda: 'Alpha',
        )
        side_two = SimpleNamespace(
            id=202,
            name=lambda: 'Blue Team',
        )
        game = SimpleNamespace(
            id=77,
            guild_id=10,
            is_pending=False,
            gamesides=(side_one, side_two),
        )
        game.gameside_by_name = lambda name: (side_two, side_two)

        with (
            mock.patch.object(
                workers.models,
                'db',
                SimpleNamespace(connection_context=lambda: Connection()),
            ),
            mock.patch.object(
                workers.models,
                'DiscordMember',
                SimpleNamespace(get_or_none=mock.Mock(return_value=object())),
            ),
            mock.patch.object(
                workers.models.Game,
                'load_full_game',
                return_value=game,
            ) as load_full,
        ):
            result = workers.prepare_win(workers.WinPreflightRequest(
                game_id=77,
                guild_id=10,
                requester_id=900,
                requester_is_staff=False,
                prefix='!',
                winning_side_id=202,
                winner_text='Blue Team',
            ))

        self.assertEqual(events, ['open', 'close'])
        load_full.assert_called_once_with(game_id=77)
        self.assertEqual(result.winning_side_id, 202)
        self.assertEqual(result.winner_name, 'Blue Team')


class WinServiceExecutionTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def publication(*, confirmed=False):
        return SimpleNamespace(
            game=SimpleNamespace(name='Test Game'),
            roster_mentions=('<@900>', '<@901>'),
            side_channel_targets=(),
            game_channel_id=None,
            confirmed_publication=(object() if confirmed else None),
        )

    def request(self, *, staff=False):
        return game_win.WinRequest(
            game_id=77,
            guild_id=10,
            requester_id=900,
            requester_name='Tester',
            requester_mention='<@900>',
            requester_description='**Tester** (`900`)',
            requester_is_staff=staff,
            prefix='!',
            winner_text='Side 2 — Blue',
            winning_side_id=202,
        )

    async def test_worker_failure_has_no_post_commit_load_or_discord_effects(self):
        events = []

        class Coordinator:
            is_active = False

            async def run(self, **kwargs):
                events.append('run')
                raise peewee.OperationalError('simulated failure')

        async def defer():
            events.append('defer')

        async def send_public(_content):
            events.append('public')

        async def send_error(content):
            events.append(('error', content))

        with (
            mock.patch.object(
                game_win.settings,
                'elo_job_coordinator',
                Coordinator(),
            ),
            mock.patch.object(
                game_win.game_win_workers,
                'run_prepare_win',
                new=mock.AsyncMock(
                    return_value=SimpleNamespace(winning_side_id=202),
                ),
            ),
            mock.patch.object(
                game_win.models.Game,
                'load_full_game',
            ) as load_full,
        ):
            result = await game_win.run_win(
                self.request(),
                guild=SimpleNamespace(id=10),
                current_channel=SimpleNamespace(),
                send_public=send_public,
                send_error=send_error,
                post_win_publisher=mock.AsyncMock(),
                defer=defer,
            )

        self.assertIsNone(result)
        self.assertEqual(events[0], 'defer')
        self.assertEqual(events[1], 'run')
        self.assertFalse(any(value == 'public' for value in events))
        self.assertIn('No Discord channel updates were made', events[-1][1])
        load_full.assert_not_called()

    async def test_first_claim_returns_after_preserving_public_result_output(self):
        events = []
        result = elo_workers.WinResult(
            game_id=77,
            confirmed=False,
            all_sides_confirmed=False,
            winner_name='Blue Team',
            confirmed_count=1,
            side_count=2,
            new_confirmation=True,
            first_claim=True,
            previous_winner_name=None,
            previous_confirmed_count=0,
            previous_side_count=0,
            publication=self.publication(),
        )

        class Coordinator:
            is_active = False

            async def run(self, **_kwargs):
                return result

        public_messages = []

        async def send_public(content):
            public_messages.append(content)

        with (
            mock.patch.object(
                game_win.settings,
                'elo_job_coordinator',
                Coordinator(),
            ),
            mock.patch.object(
                game_win.settings,
                'bot',
                SimpleNamespace(guilds=[]),
            ),
            mock.patch.object(
                game_win.game_win_workers,
                'run_prepare_win',
                new=mock.AsyncMock(
                    return_value=SimpleNamespace(winning_side_id=202),
                ),
            ),
            mock.patch.object(
                game_win.game_result_publication.confirmation_publication,
                'publish_game_channels',
                new=mock.AsyncMock(
                    side_effect=lambda *_args, **_kwargs: events.append(
                        'squad-channels'
                    ),
                ),
            ),
        ):
            returned = await game_win.run_win(
                self.request(),
                guild=SimpleNamespace(id=10),
                current_channel=SimpleNamespace(),
                send_public=send_public,
                send_error=mock.AsyncMock(),
                post_win_publisher=mock.AsyncMock(),
                acknowledged=True,
            )

        self.assertIs(returned.result, result)
        self.assertTrue(returned.public_effects_published)
        self.assertEqual(events, ['squad-channels'])
        self.assertEqual(len(public_messages), 1)
        self.assertIn('pending confirmation of winner **Blue Team**', public_messages[0])
        self.assertIn('`!win 77 Blue Team`', public_messages[0])

    async def test_committed_but_publish_failed_is_distinct_from_worker_failure(self):
        result = elo_workers.WinResult(
            game_id=77,
            confirmed=True,
            all_sides_confirmed=True,
            winner_name='Blue Team',
            confirmed_count=2,
            side_count=2,
            new_confirmation=True,
            first_claim=False,
            previous_winner_name=None,
            previous_confirmed_count=0,
            previous_side_count=0,
            publication=self.publication(confirmed=True),
        )

        class Coordinator:
            is_active = False

            async def run(self, **_kwargs):
                return result

        errors = []

        async def send_error(content):
            errors.append(content)

        with (
            mock.patch.object(
                game_win.settings,
                'elo_job_coordinator',
                Coordinator(),
            ),
            mock.patch.object(
                game_win.settings,
                'bot',
                SimpleNamespace(guilds=[]),
            ),
            mock.patch.object(
                game_win.game_win_workers,
                'run_prepare_win',
                new=mock.AsyncMock(
                    return_value=SimpleNamespace(winning_side_id=202),
                ),
            ),
            mock.patch.object(
                game_win.game_result_publication.confirmation_publication,
                'publish_game_channels',
                new=mock.AsyncMock(),
            ),
        ):
            returned = await game_win.run_win(
                self.request(),
                guild=SimpleNamespace(id=10),
                current_channel=SimpleNamespace(),
                send_public=mock.AsyncMock(),
                send_error=send_error,
                post_win_publisher=mock.AsyncMock(
                    side_effect=RuntimeError('publisher unavailable'),
                ),
                acknowledged=True,
            )

        self.assertIs(returned.result, result)
        self.assertFalse(returned.public_effects_published)
        self.assertEqual(len(errors), 1)
        self.assertIn('was updated', errors[0])
        self.assertNotIn('No public game change', errors[0])

    async def test_snapshot_failure_reports_committed_reconciliation(self):
        committed = elo_workers.WinResult(
            game_id=77,
            confirmed=False,
            all_sides_confirmed=False,
            winner_name='Blue Team',
            confirmed_count=1,
            side_count=2,
            new_confirmation=True,
            first_claim=True,
            previous_winner_name=None,
            previous_confirmed_count=0,
            previous_side_count=0,
        )

        class Coordinator:
            is_active = False

            async def run(self, **_kwargs):
                raise elo_workers.WinSnapshotError(committed)

        errors = []
        publisher = mock.AsyncMock()
        with (
            mock.patch.object(
                game_win.settings,
                'elo_job_coordinator',
                Coordinator(),
            ),
            mock.patch.object(
                game_win.game_win_workers,
                'run_prepare_win',
                new=mock.AsyncMock(
                    return_value=SimpleNamespace(winning_side_id=202),
                ),
            ),
            mock.patch.object(game_win.logger, 'exception'),
        ):
            outcome = await game_win.run_win(
                self.request(),
                guild=SimpleNamespace(id=10),
                current_channel=SimpleNamespace(),
                send_public=mock.AsyncMock(),
                send_error=lambda content: self._append(errors, content),
                post_win_publisher=publisher,
                acknowledged=True,
            )

        self.assertFalse(outcome.public_effects_published)
        publisher.assert_not_awaited()
        self.assertIn('was updated', errors[0])
        self.assertIn('snapshot could not be loaded', errors[0])

    @staticmethod
    async def _append(values, content):
        values.append(content)

    async def test_side_parser_check_failure_keeps_prefix_error_shape(self):
        errors = []

        async def send_error(content):
            errors.append(content)

        with mock.patch.object(
            game_win.game_win_workers,
            'run_prepare_win',
            new=mock.AsyncMock(
                side_effect=game_win.exceptions.CheckFailedError(
                    'Name given is not enough characters.'
                ),
            ),
        ):
            returned = await game_win.run_win(
                self.request(),
                guild=SimpleNamespace(id=10),
                current_channel=SimpleNamespace(),
                send_public=mock.AsyncMock(),
                send_error=send_error,
                post_win_publisher=mock.AsyncMock(),
                acknowledged=True,
            )

        self.assertIsNone(returned)
        self.assertEqual(
            errors,
            ['*Error*: Name given is not enough characters.'],
        )

    async def test_success_preserves_coordinator_claim_cleanup_and_public_order(self):
        events = []

        result = elo_workers.WinResult(
            game_id=77,
            confirmed=True,
            all_sides_confirmed=True,
            winner_name='Blue Team',
            confirmed_count=2,
            side_count=2,
            new_confirmation=True,
            first_claim=False,
            previous_winner_name=None,
            previous_confirmed_count=0,
            previous_side_count=0,
            publication=self.publication(confirmed=True),
        )

        class Coordinator:
            is_active = False

            async def run(self, **kwargs):
                events.append(('coordinator', kwargs['worker_args']))
                kwargs['before_submit']()
                try:
                    return result
                finally:
                    kwargs['after_complete']()

        async def send_public(content):
            events.append(('public', content))

        with (
            mock.patch.object(
                game_win.settings,
                'elo_job_coordinator',
                Coordinator(),
            ),
            mock.patch.object(
                game_win.settings,
                'bot',
                SimpleNamespace(guilds=[]),
            ),
            mock.patch.object(
                game_win.game_win_workers,
                'run_prepare_win',
                new=mock.AsyncMock(
                    return_value=SimpleNamespace(winning_side_id=202),
                ),
            ),
            mock.patch.object(
                game_win.game_result_publication.confirmation_publication,
                'publish_game_channels',
                new=mock.AsyncMock(
                    side_effect=lambda *_args, **_kwargs: events.append(
                        'squad-channels'
                    ),
                ),
            ),
            mock.patch.object(
                game_win.utilities,
                'lock_game',
                side_effect=lambda game_id: events.append(('lock', game_id)),
            ),
            mock.patch.object(
                game_win.utilities,
                'unlock_game',
                side_effect=lambda game_id: events.append(('unlock', game_id)),
            ),
        ):
            published = mock.AsyncMock(
                side_effect=lambda *args: events.append('post-win'),
            )
            returned = await game_win.run_win(
                self.request(staff=True),
                guild=SimpleNamespace(id=10),
                current_channel=SimpleNamespace(),
                send_public=send_public,
                send_error=mock.AsyncMock(),
                post_win_publisher=published,
                acknowledged=True,
            )

        self.assertIs(returned.result, result)
        self.assertTrue(returned.public_effects_published)
        self.assertEqual(events[0][0], 'coordinator')
        self.assertEqual(events[1], ('lock', 77))
        self.assertEqual(events[2], ('unlock', 77))
        self.assertEqual(events[3], 'squad-channels')
        self.assertEqual(events[4], (
            'public',
            'All sides have confirmed this victory. Good game!',
        ))
        self.assertEqual(events[5], 'post-win')
        worker_args = events[0][1]
        self.assertEqual(worker_args[:5], (
            77,
            10,
            202,
            900,
            '**Tester** (`900`)',
        ))
        self.assertTrue(worker_args[5])
        published.assert_awaited_once()

    async def test_coordinator_conflict_is_ephemeral_error_without_mutation(self):
        active = SimpleNamespace(
            operation='record_win',
            game_id=77,
        )

        class Coordinator:
            is_active = False

            async def run(self, **_kwargs):
                raise elo_jobs.EloJobConflict(active)

        errors = []

        async def send_error(content):
            errors.append(content)

        with (
            mock.patch.object(
                game_win.settings,
                'elo_job_coordinator',
                Coordinator(),
            ),
            mock.patch.object(
                game_win.game_win_workers,
                'run_prepare_win',
                new=mock.AsyncMock(
                    return_value=SimpleNamespace(winning_side_id=202),
                ),
            ),
        ):
            result = await game_win.run_win(
                self.request(),
                guild=SimpleNamespace(id=10),
                current_channel=SimpleNamespace(),
                send_public=mock.AsyncMock(),
                send_error=send_error,
                post_win_publisher=mock.AsyncMock(),
                acknowledged=True,
            )

        self.assertIsNone(result)
        self.assertIn('already running', errors[0])


if __name__ == '__main__':
    unittest.main()
