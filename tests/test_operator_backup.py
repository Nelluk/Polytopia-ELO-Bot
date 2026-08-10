"""Focused offline coverage for P9.6 production backup orchestration."""

import asyncio
from dataclasses import FrozenInstanceError
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import discord

from tests.test_newgame_worker import import_offline_runtime


backup = import_offline_runtime('modules.operator_backup')
views = import_offline_runtime('modules.operator_backup_views')
administration = import_offline_runtime('modules.administration')


def request(**overrides):
    values = dict(
        guild_id=300,
        channel_id=400,
        requester_id=10,
        requester_description='Operator (`10`)',
    )
    values.update(overrides)
    return backup.BackupRequest(**values)


def runtime(**overrides):
    values = dict(
        environment='production',
        database_name='polytopia2',
        project_root=Path('/home/nelluk/PolyBot39'),
        owner_id=10,
        current_uid=os.geteuid(),
        current_username='nelluk',
        source_script=Path('/missing/source'),
        deployed_script=Path('/missing/deployed'),
    )
    values.update(overrides)
    return backup.BackupRuntime(**values)


def artifact(label='Full database'):
    return backup.BackupArtifact(label, 1234, 1_700_000_000)


class FakeProcess:
    def __init__(self, returncode=0, *, blocked=False):
        self.pid = 12345
        self.returncode = None if blocked else returncode
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(b'ordinary output')
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()

    async def wait(self):
        await self.release.wait()
        return self.returncode


class BackupBoundaryTests(unittest.TestCase):
    def test_request_is_a_frozen_primitive_snapshot(self):
        value = request()
        with self.assertRaises(FrozenInstanceError):
            value.requester_id = 20
        self.assertNotIn('Interaction', repr(value))

    def test_development_refuses_before_reading_production_paths(self):
        value = runtime(
            environment='development',
            database_name='polytopia_dev',
            project_root=Path('/home/nelluk/PolyBot39-dev'),
        )
        with mock.patch.object(Path, 'stat') as stat_path, \
                mock.patch.object(backup, '_digest') as digest:
            with self.assertRaises(backup.BackupEnvironmentError):
                backup.validate_runtime_sync(10, value)
        stat_path.assert_not_called()
        digest.assert_not_called()

    def test_non_owner_refuses_before_reading_scripts(self):
        with mock.patch.object(Path, 'stat') as stat_path:
            with self.assertRaises(backup.BackupPermissionError):
                backup.validate_runtime_sync(99, runtime())
        stat_path.assert_not_called()

    def test_matching_private_owner_executable_scripts_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.sh'
            deployed = root / 'deployed.sh'
            source.write_bytes(b'#!/bin/sh\nexit 0\n')
            deployed.write_bytes(source.read_bytes())
            source.chmod(0o700)
            deployed.chmod(0o700)
            value = runtime(source_script=source, deployed_script=deployed)
            result = backup.validate_runtime_sync(10, value)
        self.assertEqual(len(result.source_digest), 64)

    def test_source_drift_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.sh'
            deployed = root / 'deployed.sh'
            source.write_bytes(b'source')
            deployed.write_bytes(b'drift')
            source.chmod(0o700)
            deployed.chmod(0o700)
            with self.assertRaises(backup.BackupSourceError):
                backup.validate_runtime_sync(
                    10,
                    runtime(source_script=source, deployed_script=deployed),
                )

    def test_result_never_exposes_process_diagnostics(self):
        rendered = backup.format_result(backup.BackupResult(
            category='core_failed',
            returncode=1,
            duration_seconds=1.2,
        ))
        self.assertIn('core backup process failed', rendered)
        self.assertNotIn('stdout', rendered)
        self.assertNotIn('stderr', rendered)


class BackupExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def _execute(self, returncode, artifacts=()):
        process = FakeProcess(returncode)
        with mock.patch.object(backup, 'validate_runtime', new=mock.AsyncMock()), \
                mock.patch.object(
                    backup,
                    '_spawn_process',
                    new=mock.AsyncMock(return_value=process),
                ), mock.patch.object(
                    backup,
                    '_inspect_artifacts',
                    new=mock.AsyncMock(return_value=artifacts),
                ):
            return await backup.execute_backup(request(), runtime=runtime())

    async def test_exit_statuses_have_distinct_machine_categories(self):
        success = await self._execute(0, (artifact(),))
        partial = await self._execute(20, (artifact(),))
        busy = await self._execute(75)
        failed = await self._execute(4)
        self.assertEqual(success.category, 'success')
        self.assertEqual(partial.category, 'reporting_failed')
        self.assertEqual(busy.category, 'busy')
        self.assertEqual(failed.category, 'core_failed')

    async def test_output_capture_is_bounded_while_stream_is_drained(self):
        stream = asyncio.StreamReader()
        stream.feed_data(b'x' * (backup.MAX_CAPTURE_BYTES + 5000))
        stream.feed_eof()
        captured, truncated = await backup._read_bounded(stream)
        self.assertEqual(len(captured), backup.MAX_CAPTURE_BYTES)
        self.assertTrue(truncated)

    async def test_timeout_stops_and_drains_the_process_group(self):
        process = FakeProcess(blocked=True)

        def stop_group(_process, _signal):
            process.returncode = -15
            process.release.set()

        with mock.patch.object(backup, 'validate_runtime', new=mock.AsyncMock()), \
                mock.patch.object(
                    backup,
                    '_spawn_process',
                    new=mock.AsyncMock(return_value=process),
                ), mock.patch.object(
                    backup,
                    '_signal_process_group',
                    side_effect=stop_group,
                ), mock.patch.object(backup, 'MAX_PROCESS_SECONDS', 0.001):
            result = await backup.execute_backup(request(), runtime=runtime())
        self.assertEqual(result.category, 'timeout')
        self.assertIsNone(result.returncode)
        self.assertTrue(process.release.is_set())

    async def test_conflict_rejects_promptly_and_cancellation_drains_child(self):
        process = FakeProcess(blocked=True)
        coordinator = backup.BackupCoordinator()

        def stop_group(_process, _signal):
            process.returncode = -15
            process.release.set()

        with mock.patch.object(backup, 'validate_runtime', new=mock.AsyncMock()), \
                mock.patch.object(
                    backup,
                    '_spawn_process',
                    new=mock.AsyncMock(return_value=process),
                ), mock.patch.object(
                    backup,
                    '_signal_process_group',
                    side_effect=stop_group,
                ):
            task = asyncio.create_task(
                coordinator.run(request(), runtime=runtime())
            )
            for _ in range(20):
                if coordinator.active is not None:
                    break
                await asyncio.sleep(0)
            with self.assertRaises(backup.BackupConflictError):
                await coordinator.run(request(requester_id=11), runtime=runtime())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNone(coordinator.active)
        self.assertTrue(process.release.is_set())


class BackupAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = administration.administration.__new__(
            administration.administration
        )
        self.operator = next(
            command
            for command in administration.administration.__cog_app_commands__
            if command.name == 'operator'
        )
        self.command = self.operator.get_command('database').get_command(
            'backup'
        )

    def test_exact_nested_shape_and_prefix_retirement(self):
        self.assertEqual(list(self.command.parameters), [])
        prefix_names = {
            name
            for command in administration.administration.__cog_commands__
            for name in (command.name, *command.aliases)
        }
        self.assertNotIn('backup_db', prefix_names)
        self.assertNotIn('dbb', prefix_names)

    async def test_non_owner_refuses_privately_before_defer(self):
        interaction = SimpleNamespace(
            guild_id=300,
            user=SimpleNamespace(id=99),
            response=SimpleNamespace(
                send_message=mock.AsyncMock(),
                defer=mock.AsyncMock(),
            ),
        )
        with mock.patch.object(administration.settings, 'owner_id', 10):
            await self.command.callback(self.cog, interaction)
        interaction.response.send_message.assert_awaited_once()
        self.assertTrue(
            interaction.response.send_message.await_args.kwargs['ephemeral']
        )
        interaction.response.defer.assert_not_awaited()

    async def test_development_refusal_is_private_and_creates_no_view(self):
        interaction = SimpleNamespace(
            guild_id=300,
            user=SimpleNamespace(id=10),
            response=SimpleNamespace(
                send_message=mock.AsyncMock(),
                defer=mock.AsyncMock(),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(),
        )
        with mock.patch.object(administration.settings, 'owner_id', 10), \
                mock.patch.object(
                    administration.operator_backup,
                    'validate_runtime',
                    new=mock.AsyncMock(side_effect=backup.BackupEnvironmentError(
                        'production only'
                    )),
                ):
            await self.command.callback(self.cog, interaction)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.followup.send.assert_awaited_once_with(
            'production only', ephemeral=True
        )
        interaction.edit_original_response.assert_not_awaited()

    async def test_successful_preflight_builds_private_confirmation(self):
        message = SimpleNamespace()
        interaction = SimpleNamespace(
            guild_id=300,
            channel_id=400,
            user=SimpleNamespace(id=10, display_name='Operator'),
            response=SimpleNamespace(
                send_message=mock.AsyncMock(),
                defer=mock.AsyncMock(),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(),
            original_response=mock.AsyncMock(return_value=message),
        )
        with mock.patch.object(administration.settings, 'owner_id', 10), \
                mock.patch.object(
                    administration.operator_backup,
                    'validate_runtime',
                    new=mock.AsyncMock(),
                ):
            await self.command.callback(self.cog, interaction)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.edit_original_response.assert_awaited_once()
        created_view = interaction.edit_original_response.await_args.kwargs[
            'view'
        ]
        self.assertIsInstance(created_view, views.BackupConfirmationView)
        self.assertIs(created_view.message, message)
        self.assertIn('Run backup', str(created_view.to_components()))


if __name__ == '__main__':
    unittest.main()
