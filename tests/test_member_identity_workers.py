"""Focused username, nickname, and ELO-ban listener coverage."""

import asyncio
from contextlib import nullcontext
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

import peewee

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.member_identity_workers')
games = import_offline_runtime('modules.games')


def username_request():
    return workers.UsernameUpdateRequest(
        discord_id=20,
        before_name='Old',
        after_name='New',
        stored_name='New',
        member_description='**New** (`20`)',
    )


def nickname_request():
    return workers.NicknameUpdateRequest(
        guild_id=300,
        member_id=20,
        before_nick='Old Nick',
        after_name='New',
        after_nick='New Nick',
        member_description='**New Nick** (`20`)',
    )


def ban_request(*, is_banned=True):
    return workers.EloBanUpdateRequest(
        guild_id=300,
        member_id=20,
        is_banned=is_banned,
        member_description='**New Nick** (`20`)',
    )


class WorkerTests(unittest.TestCase):
    def db_contexts(self):
        return (
            mock.patch.object(
                workers.models.db,
                'connection_context',
                return_value=nullcontext(),
            ),
            mock.patch.object(
                workers.models.db,
                'atomic',
                return_value=nullcontext(),
            ),
        )

    def test_requests_and_results_are_frozen_primitives(self):
        item = username_request()
        with self.assertRaises(FrozenInstanceError):
            item.discord_id = 1
        result = workers.UsernameUpdateResult(
            discord_id=20,
            registered=True,
            discord_member_id=7,
            updated_player_ids=(8, 9),
        )
        with self.assertRaises(FrozenInstanceError):
            result.registered = False

    def test_username_update_owns_one_atomic_account_graph(self):
        member = SimpleNamespace(id=7, update_name=mock.Mock())
        connection = mock.MagicMock()
        connection.__enter__.return_value = None
        connection.__exit__.return_value = False
        atomic = mock.MagicMock()
        atomic.__enter__.return_value = None
        atomic.__exit__.return_value = False
        with mock.patch.object(
            workers.models.db,
            'connection_context',
            return_value=connection,
        ), mock.patch.object(
            workers.models.db,
            'atomic',
            return_value=atomic,
        ), mock.patch.object(
            workers.models.DiscordMember,
            'get',
            return_value=member,
        ), mock.patch.object(
            workers,
            '_player_ids_for_discord_member',
            return_value=(8, 9),
        ), mock.patch.object(workers.models.GameLog, 'write') as write:
            result = workers.update_username(username_request())
        self.assertEqual(result.updated_player_ids, (8, 9))
        member.update_name.assert_called_once_with(new_name='New')
        self.assertEqual(write.call_args.kwargs['guild_id'], 0)
        self.assertIn('Old"" to "New', write.call_args.kwargs['message'])
        connection.__enter__.assert_called_once()
        atomic.__enter__.assert_called_once()

    def test_unregistered_username_is_transactional_noop(self):
        connection, atomic = self.db_contexts()
        with connection, atomic, mock.patch.object(
            workers.models.DiscordMember,
            'get',
            side_effect=peewee.DoesNotExist,
        ), mock.patch.object(workers.models.GameLog, 'write') as write:
            result = workers.update_username(username_request())
        self.assertFalse(result.registered)
        write.assert_not_called()

    def test_nickname_update_regenerates_display_and_audits_same_graph(self):
        player = SimpleNamespace(
            id=7,
            generate_display_name=mock.Mock(return_value='New (New Nick)'),
        )
        connection, atomic = self.db_contexts()
        with connection, atomic, mock.patch.object(
            workers,
            '_player_for_member',
            return_value=player,
        ), mock.patch.object(workers.models.GameLog, 'write') as write:
            result = workers.update_nickname(nickname_request())
        self.assertEqual(result.display_name, 'New (New Nick)')
        player.generate_display_name.assert_called_once_with(
            player_name='New',
            player_nick='New Nick',
        )
        self.assertEqual(write.call_args.kwargs['guild_id'], 300)

    def test_elo_ban_update_saves_and_audits_same_graph(self):
        player = SimpleNamespace(id=7, is_banned=False, save=mock.Mock())
        connection, atomic = self.db_contexts()
        with connection, atomic, mock.patch.object(
            workers,
            '_player_for_member',
            return_value=player,
        ), mock.patch.object(workers.models.GameLog, 'write') as write:
            result = workers.update_elo_ban(ban_request())
        self.assertTrue(result.is_banned)
        self.assertTrue(player.is_banned)
        player.save.assert_called_once()
        self.assertIn('role applied', write.call_args.kwargs['message'])

    def test_audit_failure_propagates_from_transaction_graph(self):
        player = SimpleNamespace(id=7, is_banned=False, save=mock.Mock())
        connection, atomic = self.db_contexts()
        with connection, atomic, mock.patch.object(
            workers,
            '_player_for_member',
            return_value=player,
        ), mock.patch.object(
            workers.models.GameLog,
            'write',
            side_effect=peewee.OperationalError('audit failed'),
        ):
            with self.assertRaises(peewee.OperationalError):
                workers.update_elo_ban(ban_request())

    def test_slow_worker_keeps_event_loop_responsive(self):
        async def scenario():
            started = threading.Event()
            release = threading.Event()

            def slow(_request):
                started.set()
                release.wait(timeout=2)
                return workers.EloBanUpdateResult(300, 20, True, 7, True)

            with mock.patch.object(workers, 'update_elo_ban', side_effect=slow):
                task = asyncio.create_task(
                    workers.run_elo_ban_update(ban_request())
                )
                while not started.is_set():
                    await asyncio.sleep(0.001)
                responsive = not task.done()
                release.set()
                result = await task
            return responsive, result

        responsive, result = asyncio.run(scenario())
        self.assertTrue(responsive)
        self.assertTrue(result.is_banned)

    def test_cancelled_worker_drains_before_propagating(self):
        async def scenario():
            started = threading.Event()
            release = threading.Event()

            def slow(_request):
                started.set()
                release.wait(timeout=2)
                return workers.EloBanUpdateResult(300, 20, True, 7, True)

            with mock.patch.object(workers, 'update_elo_ban', side_effect=slow):
                task = asyncio.create_task(
                    workers.run_elo_ban_update(ban_request())
                )
                while not started.is_set():
                    await asyncio.sleep(0.001)
                task.cancel()
                await asyncio.sleep(0.005)
                still_draining = not task.done()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            return still_draining

        self.assertTrue(asyncio.run(scenario()))


class ListenerTests(unittest.IsolatedAsyncioTestCase):
    def role(self, role_id, name):
        return SimpleNamespace(id=role_id, name=name)

    def member(self, *, name='Name', nick='Nick', roles=()):
        guild = SimpleNamespace(id=300, roles=list(roles))
        return SimpleNamespace(
            id=20,
            name=name,
            nick=nick,
            display_name=nick or name,
            roles=list(roles),
            guild=guild,
        )

    async def test_user_listener_submits_only_primitive_snapshot(self):
        before = SimpleNamespace(id=20, name='Old', display_name='Old')
        after = SimpleNamespace(id=20, name='New', display_name='New')
        result = workers.UsernameUpdateResult(20, True, 7, (8, 9))
        cog = games.polygames.__new__(games.polygames)
        with mock.patch.object(
            games.member_identity_workers,
            'run_username_update',
            new=mock.AsyncMock(return_value=result),
        ) as run:
            await cog.on_user_update(before, after)
        submitted = run.await_args.args[0]
        self.assertEqual(submitted.discord_id, 20)
        self.assertEqual(submitted.before_name, 'Old')
        self.assertEqual(submitted.after_name, 'New')
        self.assertIsInstance(submitted.member_description, str)

    async def test_user_listener_contains_database_failure(self):
        before = SimpleNamespace(id=20, name='Old', display_name='Old')
        after = SimpleNamespace(id=20, name='New', display_name='New')
        cog = games.polygames.__new__(games.polygames)
        with mock.patch.object(
            games.member_identity_workers,
            'run_username_update',
            new=mock.AsyncMock(side_effect=peewee.OperationalError('down')),
        ):
            await cog.on_user_update(before, after)

    async def test_member_listener_preserves_ban_inactive_nickname_order(self):
        banned = self.role(1, 'ELO Banned')
        inactive = self.role(2, 'Inactive')
        before = self.member(name='Name', nick='Old', roles=())
        after = self.member(name='Name', nick='New', roles=(banned, inactive))
        before.guild.roles = [banned, inactive]
        after.guild.roles = [banned, inactive]
        events = []

        async def ban(_request):
            events.append('ban')
            return workers.EloBanUpdateResult(300, 20, True, 7, True)

        async def inactive_change(_request):
            events.append('inactive')
            return SimpleNamespace(player_id=7)

        async def nickname(_request):
            events.append('nickname')
            return workers.NicknameUpdateResult(
                300, 20, True, 7, 'Name (New)'
            )

        cog = games.polygames.__new__(games.polygames)
        with mock.patch.object(
            games.settings,
            'guild_setting',
            return_value='Inactive',
        ), mock.patch.object(
            games.member_identity_workers,
            'run_elo_ban_update',
            side_effect=ban,
        ), mock.patch.object(
            games.league_inactivity_workers,
            'record_inactive_role_change',
            side_effect=inactive_change,
        ), mock.patch.object(
            games.member_identity_workers,
            'run_nickname_update',
            side_effect=nickname,
        ):
            await cog.on_member_update(before, after)
        self.assertEqual(events, ['ban', 'inactive', 'nickname'])

    async def test_unregistered_ban_preserves_early_return(self):
        banned = self.role(1, 'ELO Banned')
        before = self.member(name='Name', nick='Old', roles=())
        after = self.member(name='Name', nick='New', roles=(banned,))
        before.guild.roles = [banned]
        after.guild.roles = [banned]
        cog = games.polygames.__new__(games.polygames)
        with mock.patch.object(
            games.member_identity_workers,
            'run_elo_ban_update',
            new=mock.AsyncMock(return_value=workers.EloBanUpdateResult(
                300, 20, False, None, True
            )),
        ), mock.patch.object(
            games.member_identity_workers,
            'run_nickname_update',
            new=mock.AsyncMock(),
        ) as nickname:
            await cog.on_member_update(before, after)
        nickname.assert_not_awaited()

    async def test_member_listener_contains_database_failure(self):
        banned = self.role(1, 'ELO Banned')
        before = self.member(roles=())
        after = self.member(roles=(banned,))
        before.guild.roles = [banned]
        after.guild.roles = [banned]
        cog = games.polygames.__new__(games.polygames)
        with mock.patch.object(
            games.member_identity_workers,
            'run_elo_ban_update',
            new=mock.AsyncMock(side_effect=peewee.OperationalError('down')),
        ):
            await cog.on_member_update(before, after)


if __name__ == '__main__':
    unittest.main()
