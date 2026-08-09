"""Focused P8.19 prerequisite member-removal worker coverage."""

import asyncio
from contextlib import nullcontext
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

import peewee

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.member_removal_workers')
games = import_offline_runtime('modules.games')


def request():
    return workers.MemberRemovalRequest(
        guild_id=300,
        member_id=20,
        member_description='**Target** (`20`)',
    )


def result(*, registered=True, deleted=1, incomplete=(202,)):
    return workers.MemberRemovalResult(
        guild_id=300,
        member_id=20,
        registered=registered,
        player_id=7 if registered else None,
        pending_game_ids=(101,) if deleted else (),
        deleted_pending_count=deleted,
        incomplete_game_ids=tuple(incomplete),
    )


class WorkerTests(unittest.TestCase):
    def test_request_and_result_are_frozen_primitives(self):
        item = request()
        with self.assertRaises(FrozenInstanceError):
            item.guild_id = 1
        loaded = result()
        self.assertEqual(loaded.incomplete_count, 1)
        with self.assertRaises(FrozenInstanceError):
            loaded.member_id = 2

    def test_cleanup_owns_connection_and_one_atomic_graph(self):
        connection = mock.MagicMock()
        connection.__enter__.return_value = None
        connection.__exit__.return_value = False
        atomic = mock.MagicMock()
        atomic.__enter__.return_value = None
        atomic.__exit__.return_value = False
        delete_query = mock.MagicMock()
        delete_query.where.return_value = delete_query
        delete_query.execute.return_value = 2
        with mock.patch.object(
            workers.models.db,
            'connection_context',
            return_value=connection,
        ), mock.patch.object(
            workers.models.db,
            'atomic',
            return_value=atomic,
        ), mock.patch.object(
            workers,
            '_player_for_member',
            return_value=SimpleNamespace(id=7),
        ), mock.patch.object(
            workers,
            '_lineup_rows',
            side_effect=(
                ((11, 101), (12, 102)),
                ((13, 201),),
            ),
        ), mock.patch.object(
            workers.models.Lineup,
            'delete',
            return_value=delete_query,
        ), mock.patch.object(workers.models.GameLog, 'write') as write:
            loaded = workers._cleanup_member_removal(request())
        self.assertEqual(loaded.pending_game_ids, (101, 102))
        self.assertEqual(loaded.incomplete_game_ids, (201,))
        self.assertEqual(loaded.deleted_pending_count, 2)
        self.assertEqual(write.call_count, 2)
        self.assertEqual(
            [call.kwargs['game_id'] for call in write.call_args_list],
            [101, 102],
        )
        connection.__enter__.assert_called_once()
        atomic.__enter__.assert_called_once()

    def test_unregistered_member_is_a_transactional_noop(self):
        with mock.patch.object(
            workers.models.db,
            'connection_context',
            return_value=nullcontext(),
        ), mock.patch.object(
            workers.models.db,
            'atomic',
            return_value=nullcontext(),
        ), mock.patch.object(
            workers,
            '_player_for_member',
            side_effect=peewee.DoesNotExist,
        ), mock.patch.object(workers.models.GameLog, 'write') as write:
            loaded = workers._cleanup_member_removal(request())
        self.assertFalse(loaded.registered)
        self.assertEqual(loaded.deleted_pending_count, 0)
        write.assert_not_called()

    def test_audit_failure_prevents_delete_submission(self):
        with mock.patch.object(
            workers.models.db,
            'connection_context',
            return_value=nullcontext(),
        ), mock.patch.object(
            workers.models.db,
            'atomic',
            return_value=nullcontext(),
        ), mock.patch.object(
            workers,
            '_player_for_member',
            return_value=SimpleNamespace(id=7),
        ), mock.patch.object(
            workers,
            '_lineup_rows',
            side_effect=(((11, 101),), ((12, 201),)),
        ), mock.patch.object(
            workers.models.GameLog,
            'write',
            side_effect=peewee.OperationalError('audit failed'),
        ), mock.patch.object(workers.models.Lineup, 'delete') as delete:
            with self.assertRaises(peewee.OperationalError):
                workers._cleanup_member_removal(request())
        delete.assert_not_called()

    def test_delete_conflict_rejects_the_transaction(self):
        delete_query = mock.MagicMock()
        delete_query.where.return_value = delete_query
        delete_query.execute.return_value = 0
        with mock.patch.object(
            workers.models.db,
            'connection_context',
            return_value=nullcontext(),
        ), mock.patch.object(
            workers.models.db,
            'atomic',
            return_value=nullcontext(),
        ), mock.patch.object(
            workers,
            '_player_for_member',
            return_value=SimpleNamespace(id=7),
        ), mock.patch.object(
            workers,
            '_lineup_rows',
            side_effect=(((11, 101),), ()),
        ), mock.patch.object(
            workers.models.Lineup,
            'delete',
            return_value=delete_query,
        ), mock.patch.object(workers.models.GameLog, 'write'):
            with self.assertRaises(workers.MemberRemovalConflictError):
                workers._cleanup_member_removal(request())

    def test_slow_cleanup_keeps_event_loop_responsive(self):
        async def scenario():
            started = threading.Event()
            release = threading.Event()

            def slow(_request):
                started.set()
                release.wait(timeout=2)
                return result()

            with mock.patch.object(
                workers,
                '_cleanup_member_removal',
                side_effect=slow,
            ):
                task = asyncio.create_task(
                    workers.run_member_removal(request())
                )
                while not started.is_set():
                    await asyncio.sleep(0.001)
                responsive = not task.done()
                release.set()
                loaded = await task
            return responsive, loaded

        responsive, loaded = asyncio.run(scenario())
        self.assertTrue(responsive)
        self.assertEqual(loaded.player_id, 7)


class ListenerTests(unittest.IsolatedAsyncioTestCase):
    def member(self, *, guild_id=300):
        helper = SimpleNamespace(id=2, name='Helper', mention='<@&2>')
        guild = SimpleNamespace(
            id=guild_id,
            name='Guild',
            roles=[helper],
        )
        return SimpleNamespace(
            id=20,
            display_name='Target',
            mention='<@20>',
            guild=guild,
        )

    async def test_listener_waits_for_commit_then_notifies_staff(self):
        member = self.member()
        events = []

        async def run(_request):
            events.append('worker')
            return result()

        async def send(_guild, _message):
            events.append('discord')

        cog = games.polygames.__new__(games.polygames)
        with mock.patch.object(
            games.settings,
            'server_ids',
            {'polychampions': 300},
        ), mock.patch.object(
            games.settings,
            'guild_setting',
            side_effect=lambda _guild_id, key: (
                ['Helper'] if key == 'helper_roles' else None
            ),
        ), mock.patch.object(
            games.member_removal_workers,
            'run_member_removal',
            side_effect=run,
        ), mock.patch.object(
            games.utilities,
            'send_to_log_channel',
            side_effect=send,
        ) as notify:
            await cog.on_member_remove(member)
        self.assertEqual(events, ['worker', 'discord'])
        self.assertIn('1 incomplete games', notify.call_args.args[1])
        self.assertIn('<@&2>', notify.call_args.args[1])

    async def test_database_failure_has_no_discord_effect(self):
        member = self.member()
        cog = games.polygames.__new__(games.polygames)
        with mock.patch.object(
            games.member_removal_workers,
            'run_member_removal',
            new=mock.AsyncMock(side_effect=peewee.OperationalError('down')),
        ), mock.patch.object(
            games.utilities,
            'send_to_log_channel',
            new=mock.AsyncMock(),
        ) as notify:
            await cog.on_member_remove(member)
        notify.assert_not_awaited()

    async def test_unregistered_and_nonleague_departures_do_not_notify(self):
        cog = games.polygames.__new__(games.polygames)
        notify = mock.AsyncMock()
        with mock.patch.object(
            games.settings,
            'server_ids',
            {'polychampions': 300},
        ), mock.patch.object(
            games.member_removal_workers,
            'run_member_removal',
            new=mock.AsyncMock(return_value=result(registered=False, deleted=0, incomplete=())),
        ), mock.patch.object(
            games.utilities,
            'send_to_log_channel',
            notify,
        ):
            await cog.on_member_remove(self.member())
        notify.assert_not_awaited()

        with mock.patch.object(
            games.settings,
            'server_ids',
            {'polychampions': 300},
        ), mock.patch.object(
            games.member_removal_workers,
            'run_member_removal',
            new=mock.AsyncMock(return_value=result()),
        ), mock.patch.object(
            games.utilities,
            'send_to_log_channel',
            notify,
        ):
            await cog.on_member_remove(self.member(guild_id=301))
        notify.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
