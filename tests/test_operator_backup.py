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
        project_root=Path('/srv/polyelo/PolyBot39'),
        owner_id=10,
        current_username='polyelo',
        deployed_script=Path('/missing/deployed'),
    )
    values.update(overrides)
    return backup.BackupRuntime(**values)


def artifact(label='Full database'):
    return backup.BackupArtifact(label, 1234, 1_700_000_000)


def production_patches(value, *, deployed_uid=None):
    return mock.patch.multiple(
        backup,
        PRODUCTION_ROOT=value.project_root,
        DEPLOYED_SCRIPT=value.deployed_script,
        DEPLOYED_SCRIPT_UID=(
            os.geteuid() if deployed_uid is None else deployed_uid
        ),
    )


class FakeProcess:
    def __init__(self, returncode=0, *, blocked=False):
        self.pid = 12345
        self.returncode = None if blocked else returncode
        self.release = asyncio.Event()
        self.wait_completed = asyncio.Event()
        if not blocked:
            self.release.set()
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(b'ordinary output')
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()

    async def wait(self):
        await self.release.wait()
        self.wait_completed.set()
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
        with mock.patch.object(Path, 'lstat') as lstat_path:
            with self.assertRaises(backup.BackupEnvironmentError):
                backup.validate_runtime_sync(10, value)
        lstat_path.assert_not_called()

    def test_non_owner_refuses_before_reading_scripts(self):
        with mock.patch.object(Path, 'lstat') as lstat_path:
            with self.assertRaises(backup.BackupPermissionError):
                backup.validate_runtime_sync(99, runtime())
        lstat_path.assert_not_called()

    def test_fixed_root_controlled_wrapper_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            deployed = Path(directory) / 'polyelo-backup'
            deployed.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
            deployed.chmod(0o755)
            value = runtime(
                project_root=Path(directory),
                deployed_script=deployed,
            )
            with production_patches(value):
                result = backup.validate_runtime_sync(10, value)
        self.assertIsNone(result)

    def test_missing_wrapper_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            deployed = Path(directory) / 'missing-wrapper'
            value = runtime(
                project_root=Path(directory),
                deployed_script=deployed,
            )
            with production_patches(value), \
                    self.assertRaises(backup.BackupSourceError):
                backup.validate_runtime_sync(10, value)

    def test_wrapper_symlink_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / 'real-wrapper'
            real.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
            real.chmod(0o755)
            deployed = root / 'polyelo-backup'
            deployed.symlink_to(real)
            value = runtime(project_root=root, deployed_script=deployed)
            with production_patches(value), self.assertRaisesRegex(
                backup.BackupSourceError,
                'regular non-symlink',
            ):
                backup.validate_runtime_sync(10, value)

    def test_wrapper_must_be_root_controlled_and_not_group_writable(self):
        with tempfile.TemporaryDirectory() as directory:
            deployed = Path(directory) / 'polyelo-backup'
            deployed.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
            deployed.chmod(0o775)
            value = runtime(
                project_root=Path(directory),
                deployed_script=deployed,
            )
            with production_patches(value), self.assertRaisesRegex(
                backup.BackupSourceError,
                'writable outside root',
            ):
                backup.validate_runtime_sync(10, value)
            deployed.chmod(0o755)
            with production_patches(
                value,
                deployed_uid=os.geteuid() + 1,
            ), self.assertRaisesRegex(
                backup.BackupSourceError,
                'not root-controlled',
            ):
                backup.validate_runtime_sync(10, value)

    def test_wrapper_path_cannot_be_substituted(self):
        value = runtime()
        with mock.patch.object(backup, 'PRODUCTION_ROOT', value.project_root), \
                self.assertRaisesRegex(
                    backup.BackupSourceError,
                    'fixed host path',
                ):
            backup.validate_runtime_sync(10, value)

    def test_canonical_production_topology_has_no_legacy_home_paths(self):
        self.assertEqual(backup.PRODUCTION_ROOT, Path('/srv/polyelo/PolyBot39'))
        self.assertEqual(backup.PRODUCTION_USER, 'polyelo')
        self.assertEqual(
            backup.DEPLOYED_SCRIPT,
            Path('/srv/polyelo/bin/polyelo-backup'),
        )
        paths = backup._artifact_paths(1_700_000_000)
        self.assertTrue(all(str(path).startswith('/srv/polyelo/backups/')
                            for _label, path in paths))

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

    async def test_spawn_passes_only_the_fixed_wrapper_path(self):
        process = FakeProcess(0)
        create = mock.AsyncMock(return_value=process)
        wrapper = Path('/srv/polyelo/bin/polyelo-backup')
        with mock.patch.object(
            backup.asyncio,
            'create_subprocess_exec',
            new=create,
        ):
            result = await backup._spawn_process(wrapper)

        self.assertIs(result, process)
        create.assert_awaited_once_with(
            str(wrapper),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

    async def test_execution_revalidates_before_process_spawn(self):
        validate = mock.AsyncMock(side_effect=backup.BackupSourceError(
            'wrapper refused'
        ))
        spawn = mock.AsyncMock()
        with mock.patch.object(
            backup,
            'validate_runtime',
            new=validate,
        ), mock.patch.object(
            backup,
            '_spawn_process',
            new=spawn,
        ):
            with self.assertRaisesRegex(backup.BackupSourceError, 'refused'):
                await backup.execute_backup(request(), runtime=runtime())

        validate.assert_awaited_once()
        spawn.assert_not_awaited()

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

    async def test_repeated_cancellation_keeps_claim_through_reap_and_drain(self):
        process = FakeProcess(blocked=True)
        coordinator = backup.BackupCoordinator()
        termination_started = asyncio.Event()
        release_termination = asyncio.Event()
        release_streams = asyncio.Event()
        process_spawned = asyncio.Event()
        stream_drains = 0

        async def spawn(_script):
            process_spawned.set()
            return process

        async def blocked_termination(_process, wait_task):
            termination_started.set()
            await release_termination.wait()
            process.returncode = -15
            process.release.set()
            await asyncio.shield(wait_task)

        async def blocked_stream_drain(_stream):
            nonlocal stream_drains
            await release_streams.wait()
            stream_drains += 1
            return b'', False

        with mock.patch.object(backup, 'validate_runtime', new=mock.AsyncMock()), \
                mock.patch.object(
                    backup,
                    '_spawn_process',
                    new=mock.AsyncMock(side_effect=spawn),
                ), mock.patch.object(
                    backup,
                    '_terminate_process',
                    new=mock.AsyncMock(side_effect=blocked_termination),
                ) as terminate, mock.patch.object(
                    backup,
                    '_read_bounded',
                    side_effect=blocked_stream_drain,
                ):
            task = asyncio.create_task(
                coordinator.run(request(), runtime=runtime())
            )
            await asyncio.wait_for(process_spawned.wait(), 0.2)
            self.assertIsNotNone(coordinator.active)

            task.cancel()
            await asyncio.wait_for(termination_started.wait(), 0.2)
            task.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            self.assertIsNotNone(coordinator.active)
            with self.assertRaises(backup.BackupConflictError):
                await coordinator.run(
                    request(requester_id=11), runtime=runtime()
                )

            release_termination.set()
            await asyncio.wait_for(process.release.wait(), 0.2)
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            self.assertIsNotNone(coordinator.active)
            self.assertEqual(stream_drains, 0)

            release_streams.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        terminate.assert_awaited_once()
        self.assertTrue(process.wait_completed.is_set())
        self.assertEqual(stream_drains, 2)
        self.assertIsNone(coordinator.active)


class BackupViewLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def interaction(self):
        return SimpleNamespace(
            user=SimpleNamespace(id=10),
            response=SimpleNamespace(
                defer=mock.AsyncMock(),
                send_message=mock.AsyncMock(),
                edit_message=mock.AsyncMock(),
            ),
            edit_original_response=mock.AsyncMock(),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )

    async def test_five_minute_preview_timeout_cannot_expire_busy_panel(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def runner(_interaction):
            started.set()
            await release.wait()
            return backup.BackupResult('success', 0, 301.0, (artifact(),))

        view = views.BackupConfirmationView(
            requester_id=10,
            runner=runner,
        )
        interaction = self.interaction()
        task = asyncio.create_task(view._run(interaction))
        await asyncio.wait_for(started.wait(), 0.2)
        self.assertTrue(view.busy)
        self.assertTrue(view.is_finished())

        await view.on_timeout()
        self.assertIn('Backup running', view.status)
        self.assertNotIn('expired', view.status)

        release.set()
        await task
        self.assertTrue(view.finished)
        self.assertEqual(interaction.edit_original_response.await_count, 2)
        interaction.followup.send.assert_not_awaited()
        self.assertIn('completed', view.status)

    def test_preview_describes_fixed_wrapper_without_release_manifest(self):
        view = views.BackupConfirmationView(
            requester_id=10,
            runner=mock.AsyncMock(),
        )
        rendered = str(view.to_components())
        self.assertIn('fixed host backup wrapper', rendered)
        self.assertNotIn('release manifest', rendered)

    async def test_twelve_minute_bound_leaves_interaction_expiry_margin(self):
        self.assertEqual(backup.MAX_PROCESS_SECONDS, 12 * 60)
        self.assertLessEqual(backup.MAX_PROCESS_SECONDS + 60, 15 * 60)
        interaction = self.interaction()
        result = backup.BackupResult('timeout', None, 12 * 60)
        view = views.BackupConfirmationView(
            requester_id=10,
            runner=mock.AsyncMock(return_value=result),
        )

        await view._run(interaction)

        self.assertEqual(interaction.edit_original_response.await_count, 2)
        interaction.followup.send.assert_not_awaited()
        self.assertIn('12-minute limit', view.status)
        self.assertNotIn('expired', view.status)

    async def test_runner_failure_has_one_terminal_panel_and_no_followup(self):
        interaction = self.interaction()
        view = views.BackupConfirmationView(
            requester_id=10,
            runner=mock.AsyncMock(side_effect=backup.BackupExecutionError(
                'bounded failure'
            )),
        )

        await view._run(interaction)

        self.assertEqual(interaction.edit_original_response.await_count, 2)
        interaction.followup.send.assert_not_awaited()
        self.assertEqual(view.status, 'bounded failure')
        self.assertTrue(view.finished)

    async def test_running_panel_edit_failure_does_not_cancel_accepted_backup(self):
        class FakeHTTPException(Exception):
            pass

        interaction = self.interaction()
        interaction.edit_original_response.side_effect = [
            FakeHTTPException(),
            None,
        ]
        runner = mock.AsyncMock(return_value=backup.BackupResult(
            'success', 0, 1.0, (artifact(),)
        ))
        view = views.BackupConfirmationView(
            requester_id=10,
            runner=runner,
        )

        with mock.patch.object(views.discord, 'HTTPException', FakeHTTPException):
            await view._run(interaction)

        runner.assert_awaited_once_with(interaction)
        self.assertEqual(interaction.edit_original_response.await_count, 2)
        self.assertTrue(view.finished)
        self.assertIn('completed', view.status)

    async def test_cancellation_publishes_terminal_panel_then_propagates(self):
        interaction = self.interaction()
        view = views.BackupConfirmationView(
            requester_id=10,
            runner=mock.AsyncMock(side_effect=asyncio.CancelledError),
        )

        with self.assertRaises(asyncio.CancelledError):
            await view._run(interaction)

        self.assertEqual(interaction.edit_original_response.await_count, 2)
        interaction.followup.send.assert_not_awaited()
        self.assertIn('interrupted', view.status)
        self.assertFalse(view.busy)


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
