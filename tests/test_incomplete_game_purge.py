"""Focused tests for P5.14 old incomplete-game cleanup."""

import asyncio
from contextlib import AbstractContextManager
from dataclasses import FrozenInstanceError
import datetime
import inspect
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

import peewee

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.incomplete_game_purge_workers')
service = import_offline_runtime('modules.incomplete_game_purge')
administration = import_offline_runtime('modules.administration')

TODAY = datetime.date(2026, 8, 9)


def request(game_id=77):
    return workers.IncompleteGamePurgeRequest(game_id, 10, TODAY)


def game(*, age=61, count=2, ranked=False, pending=False, season=False):
    return SimpleNamespace(
        id=77,
        guild_id=10,
        date=TODAY - datetime.timedelta(days=age),
        is_pending=pending,
        is_completed=False,
        is_confirmed=False,
        is_ranked=ranked,
        league_season=3 if season else None,
        is_season_game=lambda: (3, 1, False) if season else (),
        lineup=tuple(range(count)),
    )


def plan(game_id=77):
    return workers.game_deletion_workers.DeletionEffectPlan(
        game_id=game_id,
        guild_id=10,
        state=workers.game_deletion_workers.IN_PROGRESS,
        mentions=('<@1>', '<@2>'),
        public_message='deleted',
        channel_targets=(
            workers.game_deletion_workers.DeletionChannelTarget(10, 900),
        ),
    )


def result(game_id=77):
    return workers.IncompleteGamePurgeResult(
        game_id=game_id,
        status=workers.PURGED,
        summary=f'Game {game_id} purged',
        effect_plan=plan(game_id),
    )


class Database:
    def __init__(self, logs):
        self.logs = logs
        self.opens = 0
        self.closes = 0
        self.commits = 0
        self.rollbacks = 0

    def connection_context(self):
        database = self

        class Connection(AbstractContextManager):
            def __enter__(self):
                database.opens += 1
                return self

            def __exit__(self, *_args):
                database.closes += 1

        return Connection()

    def atomic(self):
        database = self

        class Atomic(AbstractContextManager):
            def __enter__(self):
                self.count = len(database.logs)
                return self

            def __exit__(self, exc_type, *_args):
                if exc_type is None:
                    database.commits += 1
                else:
                    database.rollbacks += 1
                    del database.logs[self.count:]
                return False

        return Atomic()


class IncompletePurgePolicyTests(unittest.TestCase):
    def test_threshold_matrix_is_exact(self):
        cases = {
            (2, False): 60,
            (2, True): 60,
            (3, False): 90,
            (3, True): 90,
            (4, False): 90,
            (4, True): 120,
            (5, False): 120,
            (5, True): 150,
            (6, False): 120,
            (6, True): 150,
            (7, False): 120,
            (7, True): None,
        }
        for inputs, expected in cases.items():
            with self.subTest(inputs=inputs):
                self.assertEqual(
                    workers.purge_threshold_days(*inputs),
                    expected,
                )

    def test_started_scope_excludes_pending_completed_and_season_games(self):
        self.assertIsNone(workers.classify_game(
            game(pending=True), as_of=TODAY, player_count=2,
        ))
        completed = game()
        completed.is_completed = True
        self.assertIsNone(workers.classify_game(
            completed, as_of=TODAY, player_count=2,
        ))
        self.assertIsNone(workers.classify_game(
            game(season=True), as_of=TODAY, player_count=2,
        ))

    def test_strict_cutoff_and_two_player_warning_parity(self):
        self.assertEqual(
            workers.classify_game(
                game(age=57), as_of=TODAY, player_count=2,
            ),
            'warning',
        )
        self.assertEqual(
            workers.classify_game(
                game(age=60), as_of=TODAY, player_count=2,
            ),
            'warning',
        )
        self.assertEqual(
            workers.classify_game(
                game(age=61), as_of=TODAY, player_count=2,
            ),
            workers.PURGED,
        )

    def test_requests_are_frozen_primitives(self):
        value = request()
        self.assertTrue(all(isinstance(item, (int, datetime.date))
                            for item in value.__dict__.values()))
        with self.assertRaises(FrozenInstanceError):
            value.game_id = 88


class IncompletePurgeWorkerTests(unittest.IsolatedAsyncioTestCase):
    def test_legacy_game_wide_warning_marker_suppresses_repeat(self):
        current_query = mock.MagicMock()
        current_query.where.return_value.exists.return_value = False
        legacy_query = mock.MagicMock()
        legacy_query.where.return_value.exists.return_value = True
        game_log = mock.MagicMock()
        game_log.select.side_effect = [current_query, legacy_query]
        models = SimpleNamespace(GameLog=game_log)
        with mock.patch.object(workers, 'models', models):
            self.assertTrue(workers._warning_was_recorded(
                game_id=77,
                guild_id=10,
                channel_id=500,
            ))

    def test_warning_targets_preserve_side_and_central_mentions(self):
        side = SimpleNamespace(
            team_chan=500,
            team_chan_external_server=11,
            mentions=lambda: ('<@1>',),
        )
        loaded = game(age=58)
        loaded.gamesides = (side,)
        loaded.game_chan = 600
        loaded.mentions = lambda: ('<@1>', '<@2>')
        targets = workers._warning_targets(loaded)
        self.assertEqual(
            [(target.guild_id, target.channel_id, target.mentions)
             for target in targets],
            [(11, 500, ('<@1>',)), (10, 600, ('<@1>', '<@2>'))],
        )

    def test_purge_commits_protected_audit_and_single_delete(self):
        logs = []
        database = Database(logs)
        loaded = game()
        loaded.game_chan = 900
        loaded.delete_game = mock.Mock()
        models = SimpleNamespace(
            db=database,
            GameLog=SimpleNamespace(
                write=lambda **kwargs: logs.append(kwargs),
            ),
        )
        with mock.patch.object(workers, 'models', models), mock.patch.object(
            workers, '_load_game', return_value=loaded,
        ), mock.patch.object(
            workers.game_deletion_workers,
            'build_effect_plan',
            return_value=plan(),
        ):
            actual = workers.purge_incomplete_game(request())

        self.assertEqual(actual.status, workers.PURGED)
        loaded.delete_game.assert_called_once_with()
        self.assertEqual(len(logs), 1)
        self.assertTrue(logs[0]['is_protected'])
        self.assertIn('10/900', logs[0]['message'])
        self.assertEqual(database.commits, 1)
        self.assertEqual(database.rollbacks, 0)
        self.assertEqual((database.opens, database.closes), (1, 1))

    def test_purge_rolls_back_audit_when_delete_fails(self):
        logs = []
        database = Database(logs)
        loaded = game()
        loaded.delete_game = mock.Mock(
            side_effect=peewee.OperationalError('injected delete failure'),
        )
        models = SimpleNamespace(
            db=database,
            GameLog=SimpleNamespace(
                write=lambda **kwargs: logs.append(kwargs),
            ),
        )
        with mock.patch.object(workers, 'models', models), mock.patch.object(
            workers, '_load_game', return_value=loaded,
        ), mock.patch.object(
            workers.game_deletion_workers,
            'build_effect_plan',
            return_value=plan(),
        ):
            with self.assertRaises(peewee.OperationalError):
                workers.purge_incomplete_game(request())

        self.assertEqual(logs, [])
        self.assertEqual(database.commits, 0)
        self.assertEqual(database.rollbacks, 1)

    def test_state_change_returns_typed_skip_without_write(self):
        logs = []
        database = Database(logs)
        loaded = game(pending=True)
        loaded.delete_game = mock.Mock()
        models = SimpleNamespace(
            db=database,
            GameLog=SimpleNamespace(write=lambda **kwargs: logs.append(kwargs)),
        )
        with mock.patch.object(workers, 'models', models), mock.patch.object(
            workers, '_load_game', return_value=loaded,
        ):
            actual = workers.purge_incomplete_game(request())
        self.assertEqual(actual.status, workers.SKIPPED_STATE_CHANGED)
        self.assertEqual(logs, [])
        loaded.delete_game.assert_not_called()

    async def test_purge_runner_uses_elo_coordinator_and_game_lock(self):
        coordinator = mock.AsyncMock(return_value=result())
        with mock.patch.object(
            workers.settings,
            'elo_job_coordinator',
            SimpleNamespace(run=coordinator),
        ), mock.patch.object(
            workers.utilities, 'lock_game',
        ) as lock, mock.patch.object(
            workers.utilities, 'unlock_game',
        ) as unlock:
            actual = await workers.run_purge_incomplete_game(request())
            kwargs = coordinator.await_args.kwargs
            kwargs['before_submit']()
            kwargs['after_complete']()
        self.assertEqual(actual.status, workers.PURGED)
        self.assertEqual(kwargs['operation'], 'auto_purge_incomplete_game')
        lock.assert_called_once_with(77)
        unlock.assert_called_once_with(77)

    async def test_discovery_executor_is_responsive_and_drains_cancellation(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def slow(_request):
            started.set()
            release.wait(timeout=2)
            finished.set()
            return workers.IncompleteGameDiscoveryResult((), (), False)

        try:
            with mock.patch.object(
                workers, 'discover_incomplete_games', side_effect=slow,
            ):
                task = asyncio.create_task(
                    workers.run_discover_incomplete_games(
                        workers.IncompleteGameDiscoveryRequest(10, TODAY)
                    )
                )
                for _ in range(500):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.001)
                self.assertTrue(started.is_set())
                task.cancel()
                await asyncio.sleep(0.01)
                self.assertFalse(task.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        finally:
            release.set()
        self.assertTrue(finished.is_set())


class IncompletePurgeServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_warning_send_does_not_record_marker(self):
        target = workers.WarningTarget(10, 500, ('<@1>',))
        warning = workers.WarningPlan(77, 60, 'warning', (target,))
        channel = SimpleNamespace(
            send=mock.AsyncMock(side_effect=RuntimeError('send failed')),
        )
        guild = SimpleNamespace(id=10, get_channel=lambda _id: channel)
        bot = SimpleNamespace(get_guild=lambda _id: guild)
        staff = SimpleNamespace(send=mock.AsyncMock())
        recorder = mock.AsyncMock()
        with mock.patch.object(
            service.incomplete_game_purge_workers,
            'run_record_warning_delivery',
            recorder,
        ):
            await service.publish_warning_plan(
                warning,
                bot=bot,
                source_guild_id=10,
                as_of=TODAY,
                staff_channel=staff,
            )
        recorder.assert_not_awaited()
        staff.send.assert_awaited_once()

    async def test_successful_warning_records_exact_channel_after_send(self):
        events = []
        target = workers.WarningTarget(10, 500, ('<@1>',))
        warning = workers.WarningPlan(77, 60, 'warning', (target,))

        async def send(_message):
            events.append('send')

        async def record(_request):
            events.append('record')
            return workers.WarningDeliveryResult(
                77, 500, workers.WARNING_RECORDED,
            )

        channel = SimpleNamespace(send=send)
        guild = SimpleNamespace(id=10, get_channel=lambda _id: channel)
        bot = SimpleNamespace(get_guild=lambda _id: guild)
        with mock.patch.object(
            service.incomplete_game_purge_workers,
            'run_record_warning_delivery',
            side_effect=record,
        ) as recorder:
            await service.publish_warning_plan(
                warning,
                bot=bot,
                source_guild_id=10,
                as_of=TODAY,
                staff_channel=None,
            )
        self.assertEqual(events, ['send', 'record'])
        recorded_request = recorder.await_args.args[0]
        self.assertEqual(recorded_request.channel_id, 500)

    async def test_post_commit_announcement_precedes_channel_delete(self):
        events = []

        async def announcement(*_args, **_kwargs):
            events.append('announcement')

        async def delete(*_args, **_kwargs):
            events.append('channel')
            return True

        guild = SimpleNamespace(id=10)
        bot = SimpleNamespace(get_guild=lambda _id: guild)
        with mock.patch.object(
            service.game_deletion,
            '_publish_announcement',
            side_effect=announcement,
        ), mock.patch.object(
            service.channels,
            'delete_game_channel',
            side_effect=delete,
        ), mock.patch.object(
            service.settings,
            'guild_setting',
            return_value='$',
        ):
            await service.publish_purge_result(
                result(), bot=bot, guild=guild, staff_channel=None,
            )
        self.assertEqual(events, ['announcement', 'channel'])

    async def test_conflicting_elo_job_defers_remaining_candidates(self):
        active = SimpleNamespace(operation='record_win', game_id=99)
        conflict = service.EloJobConflict(active)
        discovered = workers.IncompleteGameDiscoveryResult((), (71, 72), False)
        staff = SimpleNamespace(send=mock.AsyncMock())
        guild = SimpleNamespace(
            id=10,
            get_channel=lambda _id: staff,
        )
        bot = SimpleNamespace()
        runner = mock.AsyncMock(side_effect=conflict)
        with mock.patch.object(
            service.settings, 'guild_setting', return_value=900,
        ), mock.patch.object(
            service.incomplete_game_purge_workers,
            'run_discover_incomplete_games',
            new=mock.AsyncMock(return_value=discovered),
        ), mock.patch.object(
            service.incomplete_game_purge_workers,
            'run_purge_incomplete_game',
            runner,
        ):
            actual = await service.purge_incomplete_games_for_guild(
                bot=bot, guild=guild, as_of=TODAY,
            )
        self.assertEqual(actual, ())
        self.assertEqual(runner.await_count, 1)
        self.assertIn('remaining candidates', staff.send.await_args.args[0])

    async def test_unexpected_post_commit_failure_does_not_stop_next_game(self):
        discovered = workers.IncompleteGameDiscoveryResult((), (71, 72), False)
        staff = SimpleNamespace(send=mock.AsyncMock())
        guild = SimpleNamespace(id=10, get_channel=lambda _id: staff)
        bot = SimpleNamespace()
        runner = mock.AsyncMock(side_effect=[result(71), result(72)])
        publisher = mock.AsyncMock(side_effect=[
            RuntimeError('unexpected publication failure'),
            None,
        ])
        with mock.patch.object(
            service.settings, 'guild_setting', return_value=900,
        ), mock.patch.object(
            service.incomplete_game_purge_workers,
            'run_discover_incomplete_games',
            new=mock.AsyncMock(return_value=discovered),
        ), mock.patch.object(
            service.incomplete_game_purge_workers,
            'run_purge_incomplete_game',
            runner,
        ), mock.patch.object(
            service, 'publish_purge_result', publisher,
        ):
            actual = await service.purge_incomplete_games_for_guild(
                bot=bot, guild=guild, as_of=TODAY,
            )
        self.assertEqual([item.game_id for item in actual], [71, 72])
        self.assertEqual(runner.await_count, 2)
        self.assertEqual(publisher.await_count, 2)
        self.assertTrue(any(
            'reconciliation stopped unexpectedly' in call.args[0]
            for call in staff.send.await_args_list
        ))

    def test_background_task_has_no_direct_database_or_discord_mutation(self):
        source = inspect.getsource(
            administration.administration.task_purge_incomplete,
        )
        self.assertIn('purge_incomplete_games_for_guild', source)
        self.assertNotIn('models.Game.search', source)
        self.assertNotIn('delete_game()', source)
        self.assertNotIn('delete_game_channels', source)


if __name__ == '__main__':
    unittest.main()
