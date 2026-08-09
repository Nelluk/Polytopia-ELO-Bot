"""Focused offline coverage for ranked full-game reminder snapshots."""

import asyncio
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
import datetime
import inspect
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

from tests.test_newgame_worker import import_offline_runtime
from tests.test_game_detail_workspace import snapshot as detail_snapshot


workers = import_offline_runtime('modules.game_reminder_workers')
service = import_offline_runtime('modules.game_reminders')
matchmaking = import_offline_runtime('modules.matchmaking')


NOW = datetime.datetime(2026, 8, 9, 12, 0, 0)


def fake_game(game_id, *, guild_id=10, creator_id=100):
    member = SimpleNamespace(discord_id=creator_id)
    creator = SimpleNamespace(discord_member=member)
    return SimpleNamespace(
        id=game_id,
        guild_id=guild_id,
        creating_player=lambda: creator,
    )


def item(game_id=77):
    return workers.GameReminderItem(
        game_id=game_id,
        guild_id=10,
        creator_discord_id=100,
        snapshot=detail_snapshot(pending=True, completed=False),
    )


class GameReminderWorkerTests(unittest.IsolatedAsyncioTestCase):
    def test_request_and_nested_snapshot_are_immutable(self):
        request = workers.GameReminderRequest(NOW)
        with self.assertRaises(FrozenInstanceError):
            request.limit = 10
        reminder = item()
        with self.assertRaises(FrozenInstanceError):
            reminder.game_id = 88
        with self.assertRaises(FrozenInstanceError):
            reminder.snapshot.name = 'changed'

    def test_worker_owns_connection_suppresses_recent_and_skips_malformed(self):
        events = []

        @contextmanager
        def connection_context():
            events.append('open')
            yield
            events.append('close')

        games = (fake_game(71), fake_game(72), fake_game(73))
        games[2].creating_player = mock.Mock(side_effect=RuntimeError('bad'))
        with mock.patch.object(
            workers.models,
            'db',
            SimpleNamespace(connection_context=connection_context),
        ), mock.patch.object(
            workers,
            '_candidate_games',
            return_value=games,
        ), mock.patch.object(
            workers,
            '_recent_join',
            side_effect=lambda game, **_kwargs: game.id == 72,
        ), mock.patch.object(
            workers.game_detail_workers,
            '_snapshot_from_game',
            return_value=detail_snapshot(pending=True, completed=False),
        ):
            result = workers.load_game_reminders(
                workers.GameReminderRequest(NOW)
            )

        self.assertEqual(events, ['open', 'close'])
        self.assertEqual([entry.game_id for entry in result.items], [71])
        self.assertEqual(result.suppressed_game_ids, (72,))
        self.assertEqual(result.skipped_game_ids, (73,))
        self.assertFalse(result.truncated)

    def test_candidate_bound_is_reported_and_limit_is_validated(self):
        games = tuple(fake_game(game_id) for game_id in range(1, 4))
        with mock.patch.object(
            workers.models.db,
            'connection_context',
            return_value=mock.MagicMock(),
        ), mock.patch.object(
            workers,
            '_candidate_games',
            return_value=games,
        ), mock.patch.object(
            workers,
            '_recent_join',
            return_value=False,
        ), mock.patch.object(
            workers.game_detail_workers,
            '_snapshot_from_game',
            return_value=detail_snapshot(pending=True, completed=False),
        ):
            result = workers.load_game_reminders(
                workers.GameReminderRequest(NOW, limit=2)
            )
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.items), 2)
        with self.assertRaisesRegex(ValueError, 'between 1'):
            workers.load_game_reminders(
                workers.GameReminderRequest(NOW, limit=0)
            )

    async def test_async_loader_is_responsive_and_drains_cancellation(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def slow_loader(_request):
            started.set()
            release.wait(timeout=2)
            finished.set()
            return workers.GameReminderBatch((), (), (), False)

        try:
            with mock.patch.object(
                workers,
                'load_game_reminders',
                side_effect=slow_loader,
            ):
                task = asyncio.create_task(
                    workers.run_load_game_reminders(
                        workers.GameReminderRequest(NOW)
                    )
                )
                for _ in range(500):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.001)
                self.assertTrue(started.is_set())
                await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.2)
                task.cancel()
                await asyncio.sleep(0.01)
                self.assertFalse(task.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        finally:
            release.set()
        self.assertTrue(finished.is_set())


class GameReminderPresentationTests(unittest.IsolatedAsyncioTestCase):
    def test_copy_uses_native_workflows_without_legacy_prefix_guidance(self):
        message = service.reminder_message(
            guild_name='Development Guild',
            guild_id=10,
            channel_id=20,
            game_id=77,
        )
        self.assertIn('/game show', message)
        self.assertIn('/game start', message)
        self.assertIn('draft-order player names', message)
        self.assertNotIn('$game', message)
        self.assertNotIn('$start', message)
        self.assertNotIn('$names', message)

    async def test_presenter_resolves_discord_after_read_and_sends_dense_card(self):
        creator = SimpleNamespace(send=mock.AsyncMock())
        guild = SimpleNamespace(
            id=10,
            name='Development Guild',
            get_member=lambda member_id: creator if member_id == 100 else None,
        )
        bot = SimpleNamespace(
            guilds=(guild,),
            get_guild=lambda guild_id: guild if guild_id == 10 else None,
        )
        batch = workers.GameReminderBatch((item(),), (), (), False)
        rendered = SimpleNamespace(
            embed=object(),
            new_file=lambda: None,
        )
        with mock.patch.object(
            service.game_reminder_workers,
            'run_load_game_reminders',
            new=mock.AsyncMock(return_value=batch),
        ), mock.patch.object(
            service.settings,
            'guild_setting',
            side_effect=lambda _guild_id, key: {
                'bot_channels_strict': (20,),
                'command_prefix': '$',
            }[key],
        ), mock.patch.object(
            service.game_detail_views,
            'resolve_display',
            return_value=object(),
        ) as resolve, mock.patch.object(
            service.game_detail_views,
            'render_classic_game_detail',
            return_value=rendered,
        ):
            result = await service.send_game_reminders(bot=bot, as_of=NOW)

        self.assertEqual(result, batch)
        resolve.assert_called_once_with(
            batch.items[0].snapshot,
            guild=guild,
            bot=bot,
            prefix='$',
            join_emoji=mock.ANY,
            presentation='slash',
        )
        creator.send.assert_awaited_once()
        sent = creator.send.await_args.kwargs
        self.assertIs(sent['embed'], rendered.embed)
        self.assertIn('/game start', sent['content'])

    async def test_missing_guild_or_creator_does_not_block_later_reminder(self):
        creator = SimpleNamespace(send=mock.AsyncMock())
        guild = SimpleNamespace(
            id=10,
            name='Development Guild',
            get_member=lambda member_id: creator if member_id == 100 else None,
        )
        batch = workers.GameReminderBatch(
            (
                workers.GameReminderItem(
                    70, 99, 100,
                    detail_snapshot(pending=True, completed=False),
                ),
                workers.GameReminderItem(
                    71, 10, 999,
                    detail_snapshot(pending=True, completed=False),
                ),
                item(72),
            ),
            (), (), False,
        )
        bot = SimpleNamespace(
            guilds=(guild,),
            get_guild=lambda guild_id: guild if guild_id == 10 else None,
        )
        rendered = SimpleNamespace(embed=object(), new_file=lambda: None)
        with mock.patch.object(
            service.game_reminder_workers,
            'run_load_game_reminders',
            new=mock.AsyncMock(return_value=batch),
        ), mock.patch.object(
            service.settings,
            'guild_setting',
            side_effect=lambda _guild_id, key: {
                'bot_channels_strict': (20,),
                'command_prefix': '$',
            }[key],
        ), mock.patch.object(
            service.game_detail_views,
            'resolve_display',
            return_value=object(),
        ), mock.patch.object(
            service.game_detail_views,
            'render_classic_game_detail',
            return_value=rendered,
        ):
            await service.send_game_reminders(bot=bot, as_of=NOW)
        creator.send.assert_awaited_once()
        self.assertIn('`72`', creator.send.await_args.kwargs['content'])

    def test_background_task_delegates_without_direct_database_reads(self):
        source = inspect.getsource(
            matchmaking.matchmaking.task_dm_game_creators
        )
        self.assertIn('send_game_reminders', source)
        self.assertNotIn('models.Game', source)
        self.assertNotIn('GameLog', source)
        self.assertNotIn('game.embed', source)


if __name__ == '__main__':
    unittest.main()
