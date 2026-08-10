"""Focused offline coverage for the P8.26 native team archive workflow."""

import asyncio
from contextlib import AbstractContextManager, ExitStack
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import threading
import time
import unittest
from unittest import mock

import peewee

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.team_archive_workers')
service = import_offline_runtime('modules.team_archive')
administration = import_offline_runtime('modules.administration')
attribute_workers = import_offline_runtime('modules.team_attributes_workers')


def request(**overrides):
    values = dict(
        guild_id=300,
        requester_id=100,
        requester_is_mod=True,
        team_enabled=True,
        league_scope=True,
        team_lookup='Ronin',
        expected_team_id=42,
        team_role_id=700,
        team_role_name='Ronin',
        requester_description='**Mod** (`100`)',
        confirmed=True,
    )
    values.update(overrides)
    return workers.TeamArchiveRequest(**values)


class ArchiveDatabase:
    def __init__(self, team):
        self.team = team
        self.logs = []
        self.connections = 0
        self.closed = 0
        self.commits = 0
        self.rollbacks = 0

    def connection_context(self):
        database = self

        class ConnectionContext(AbstractContextManager):
            def __enter__(self):
                database.connections += 1
                return database

            def __exit__(self, exc_type, exc_value, traceback):
                database.closed += 1
                return False

        return ConnectionContext()

    def atomic(self):
        database = self

        class AtomicContext(AbstractContextManager):
            def __enter__(self):
                self.archived = database.team.is_archived
                self.logs = list(database.logs)
                return database

            def __exit__(self, exc_type, exc_value, traceback):
                if exc_type is None:
                    database.commits += 1
                else:
                    database.rollbacks += 1
                    database.team.is_archived = self.archived
                    database.logs = self.logs
                return False

        return AtomicContext()


class FakeGameLog:
    database = None
    failure = None

    @classmethod
    def write(cls, **kwargs):
        if cls.failure is not None:
            raise cls.failure
        cls.database.logs.append(kwargs)


class TeamArchiveWorkerTests(unittest.TestCase):
    def setUp(self):
        self.team = SimpleNamespace(
            id=42,
            name='Ronin',
            is_archived=False,
            house=None,
            save=mock.Mock(),
        )
        self.database = ArchiveDatabase(self.team)
        FakeGameLog.database = self.database
        FakeGameLog.failure = None
        self.patches = ExitStack()
        self.patches.enter_context(mock.patch.object(workers.models, 'db', self.database))
        self.patches.enter_context(mock.patch.object(workers.models, 'GameLog', FakeGameLog))
        self.resolve = self.patches.enter_context(
            mock.patch.object(
                workers.team_emoji_workers,
                '_resolve_team',
                return_value=self.team,
            )
        )
        self.search = self.patches.enter_context(
            mock.patch.object(
                workers.models.Game,
                'search',
                return_value=SimpleNamespace(count=lambda: 0),
            )
        )
        self.addCleanup(self.patches.close)

    def test_request_and_result_are_frozen_primitive_snapshots(self):
        value = request()
        with self.assertRaises(FrozenInstanceError):
            value.guild_id = 999
        self.assertNotIn('Member', repr(value))

        result = workers.archive_team(value)
        with self.assertRaises(FrozenInstanceError):
            result.team_name = 'Changed'
        self.assertIsInstance(result.team_id, int)
        self.assertIsInstance(result.team_name, str)
        self.assertNotIn('SimpleNamespace', repr(result))

    def test_commit_archives_and_audits_actual_guild_atomically(self):
        result = workers.archive_team(request())

        self.assertTrue(self.team.is_archived)
        self.team.save.assert_called_once_with()
        self.assertEqual(result.team_id, 42)
        self.assertEqual(self.database.commits, 1)
        self.assertEqual(self.database.rollbacks, 0)
        self.assertEqual(self.database.connections, 1)
        self.assertEqual(self.database.closed, 1)
        self.assertEqual(self.database.logs[0]['guild_id'], 300)
        self.assertIn('**Mod** (`100`)', self.database.logs[0]['message'])
        self.assertIn('/team archive', self.database.logs[0]['message'])
        self.search.assert_called_once_with(
            team_filter=[self.team],
            status_filter=2,
        )

    def test_worker_rechecks_scope_mod_and_confirmation(self):
        for overrides in (
            {'team_enabled': False},
            {'league_scope': False},
            {'requester_is_mod': False},
            {'confirmed': False},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(
                workers.TeamArchiveError
            ):
                workers.archive_team(request(**overrides))
        self.assertFalse(self.team.is_archived)
        self.assertEqual(self.database.logs, [])
        self.assertEqual(self.database.rollbacks, 4)

    def test_worker_rejects_stale_role_house_existing_archive_and_open_games(self):
        cases = (
            ({'team_role_name': 'Other'}, None, False, 0, 'exact Discord role'),
            ({}, SimpleNamespace(name='Ninjas'), False, 0, 'House affiliation'),
            ({}, None, True, 0, 'already archived'),
            ({}, None, False, 2, 'incomplete game'),
        )
        for overrides, house, archived, count, message in cases:
            with self.subTest(message=message):
                self.team.house = house
                self.team.is_archived = archived
                self.search.return_value = SimpleNamespace(count=lambda: count)
                with self.assertRaisesRegex(workers.TeamArchiveValidationError, message):
                    workers.archive_team(request(**overrides))
                self.team.is_archived = False
                self.team.house = None
        self.assertEqual(self.database.logs, [])

    def test_stale_team_identity_is_a_conflict(self):
        with self.assertRaisesRegex(workers.TeamArchiveConflictError, 'changed'):
            workers.archive_team(request(expected_team_id=99))
        self.assertFalse(self.team.is_archived)

    def test_audit_failure_rolls_back_archive_and_closes_connection(self):
        FakeGameLog.failure = peewee.OperationalError('audit failed')

        with self.assertRaises(peewee.OperationalError):
            workers.archive_team(request())

        self.assertFalse(self.team.is_archived)
        self.assertEqual(self.database.logs, [])
        self.assertEqual(self.database.commits, 0)
        self.assertEqual(self.database.rollbacks, 1)
        self.assertEqual(self.database.connections, 1)
        self.assertEqual(self.database.closed, 1)

    def test_worker_keeps_loop_responsive_and_drains_cancellation(self):
        async def check():
            release = threading.Event()
            result = workers.TeamArchiveResult(300, 42, 'Ronin', 'audit')

            def blocked_worker(_request):
                release.wait(1)
                return result

            executor = ThreadPoolExecutor(max_workers=1)
            task = None
            try:
                with mock.patch.object(
                    workers.team_emoji_workers,
                    '_team_emoji_executor',
                    executor,
                ), mock.patch.object(workers, 'archive_team', side_effect=blocked_worker):
                    task = asyncio.create_task(workers.run_team_archive(request()))
                    await asyncio.sleep(0)
                    started = time.monotonic()
                    await asyncio.sleep(0.02)
                    self.assertLess(time.monotonic() - started, 0.2)
                    task.cancel()
                    self.assertFalse(task.done())
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
            finally:
                release.set()
                if task is not None and not task.done():
                    task.cancel()
                executor.shutdown(wait=True)

        asyncio.run(check())


class TeamArchiveAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.member = SimpleNamespace(
            id=100,
            display_name='Mod',
            name='Mod',
            mention='<@100>',
        )

    def interaction(self):
        return SimpleNamespace(
            guild=SimpleNamespace(id=300, roles=[]),
            user=self.member,
            response=SimpleNamespace(
                send_message=mock.AsyncMock(),
                defer=mock.AsyncMock(),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
            delete_original_response=mock.AsyncMock(),
            channel=SimpleNamespace(send=mock.AsyncMock()),
        )

    @staticmethod
    def command():
        return next(
            command
            for command in administration.administration.__cog_app_commands__
            if command.name == 'team'
        ).get_command('archive')

    async def test_preflight_resolves_off_loop_and_requires_exact_role(self):
        current = attribute_workers.TeamAttributeReadResult(
            guild_id=300,
            team_id=42,
            team_name='Ronin',
            attribute=attribute_workers.TEAM_ATTRIBUTE_TIER,
            value=2,
            external_server=None,
            league_tier=2,
            tier_name='Gold',
            house_name=None,
            is_hidden=False,
            is_archived=False,
            house_role_names=(),
        )
        guild = SimpleNamespace(id=300)
        role = SimpleNamespace(id=700, name='Ronin')
        with mock.patch.object(
            service,
            'team_attributes',
            wraps=service.team_attributes,
        ), mock.patch.object(
            service.team_attributes,
            'run_read',
            new=mock.AsyncMock(return_value=current),
        ), mock.patch.object(
            service.utilities,
            'guild_role_by_name',
            return_value=role,
        ):
            result = await service.run_preflight(
                member=self.member,
                guild=guild,
                team_lookup='Ronin',
            )
        self.assertEqual(result.team_id, 42)
        self.assertEqual(result.team_role_id, 700)

    async def test_denial_and_unconfirmed_requests_are_private_before_defer(self):
        for access_error, confirmed in (
            ('You do not have permission to archive teams.', True),
            (None, False),
        ):
            interaction = self.interaction()
            with mock.patch.object(
                administration.team_archive_service,
                'native_access_error',
                return_value=access_error,
            ):
                await self.command().callback(
                    administration.administration.__new__(administration.administration),
                    interaction,
                    'Ronin',
                    confirmed,
                )
            interaction.response.send_message.assert_awaited_once()
            self.assertTrue(
                interaction.response.send_message.await_args.kwargs['ephemeral']
            )
            interaction.response.defer.assert_not_awaited()
            interaction.channel.send.assert_not_awaited()

    async def test_precommit_failure_is_private_and_does_not_publish(self):
        interaction = self.interaction()
        with mock.patch.object(
            administration.team_archive_service,
            'native_access_error',
            return_value=None,
        ), mock.patch.object(
            administration.team_archive_service,
            'run_preflight',
            new=mock.AsyncMock(
                side_effect=workers.TeamArchiveValidationError('incomplete games')
            ),
        ):
            await self.command().callback(
                administration.administration.__new__(administration.administration),
                interaction,
                'Ronin',
                True,
            )
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.followup.send.assert_awaited_once_with(
            'incomplete games',
            ephemeral=True,
        )
        interaction.channel.send.assert_not_awaited()

    async def test_success_is_public_only_after_worker_result(self):
        interaction = self.interaction()
        preflight = service.TeamArchivePreflight(42, 'Ronin', 700, 'Ronin')
        result = workers.TeamArchiveResult(300, 42, 'Ronin', 'audit')
        with mock.patch.object(
            administration.team_archive_service,
            'native_access_error',
            return_value=None,
        ), mock.patch.object(
            administration.team_archive_service,
            'run_preflight',
            new=mock.AsyncMock(return_value=preflight),
        ), mock.patch.object(
            administration.team_archive_service,
            'run_archive',
            new=mock.AsyncMock(return_value=result),
        ):
            returned = await self.command().callback(
                administration.administration.__new__(administration.administration),
                interaction,
                'Ronin',
                True,
            )
        self.assertIs(returned, result)
        interaction.delete_original_response.assert_awaited_once()
        interaction.channel.send.assert_awaited_once()
        self.assertIn('<@100>', interaction.channel.send.await_args.args[0])
        interaction.followup.send.assert_not_awaited()

    async def test_postcommit_publication_failure_is_terminal_reconciliation(self):
        interaction = self.interaction()
        preflight = service.TeamArchivePreflight(42, 'Ronin', 700, 'Ronin')
        result = workers.TeamArchiveResult(300, 42, 'Ronin', 'audit')
        failed_sender = mock.AsyncMock(side_effect=RuntimeError('send failed'))
        with mock.patch.object(
            administration.team_archive_service,
            'native_access_error',
            return_value=None,
        ), mock.patch.object(
            administration.team_archive_service,
            'run_preflight',
            new=mock.AsyncMock(return_value=preflight),
        ), mock.patch.object(
            administration.team_archive_service,
            'run_archive',
            new=mock.AsyncMock(return_value=result),
        ), mock.patch.object(
            administration.team_emoji_service,
            'public_interaction_sender',
            return_value=failed_sender,
        ):
            await self.command().callback(
                administration.administration.__new__(administration.administration),
                interaction,
                'Ronin',
                True,
            )
        failed_sender.assert_awaited_once()
        warning = interaction.followup.send.await_args.args[0]
        self.assertIn('was archived', warning)
        self.assertIn('Do not retry', warning)
        self.assertTrue(interaction.followup.send.await_args.kwargs['ephemeral'])


if __name__ == '__main__':
    unittest.main()
