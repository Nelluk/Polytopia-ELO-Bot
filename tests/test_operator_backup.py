"""Focused offline coverage for P9.6 production backup orchestration."""

import asyncio
from dataclasses import FrozenInstanceError
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import discord

from tests.test_newgame_worker import import_offline_runtime


backup = import_offline_runtime('modules.operator_backup')
views = import_offline_runtime('modules.operator_backup_views')
administration = import_offline_runtime('modules.administration')
release_tool = import_offline_runtime(
    'scripts.manage_production_backup_release'
)


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
        reporting_exporter=Path('/missing/exporter'),
        reporting_python=Path('/missing/python'),
        current_executable=Path('/missing/python'),
        release_manifest=Path('/missing/manifest'),
    )
    values.update(overrides)
    return backup.BackupRuntime(**values)


def artifact(label='Full database'):
    return backup.BackupArtifact(label, 1234, 1_700_000_000)


def make_release_runtime(directory: str):
    git = shutil.which('git')
    if git is None:
        raise unittest.SkipTest('Git CLI is not installed in this runtime image.')
    root = Path(directory)
    (root / 'scripts').mkdir()
    (root / '.venv/bin').mkdir(parents=True)
    source = root / 'scripts/backup_db.sh'
    deployed = root / 'deployed.sh'
    exporter = root / 'scripts/export_reporting_duckdb.py'
    python = root / '.venv/bin/python'
    source.write_bytes(b'#!/bin/sh\nexit 0\n')
    deployed.write_bytes(source.read_bytes())
    exporter.write_bytes(b'print("export")\n')
    python.write_bytes(b'python-runtime')
    source.chmod(0o700)
    deployed.chmod(0o700)
    exporter.chmod(0o600)
    python.chmod(0o700)
    subprocess.run([git, 'init', '-q'], cwd=root, check=True)
    subprocess.run([git, 'add', 'scripts'], cwd=root, check=True)
    subprocess.run(
        [
            git, '-c', 'user.name=PolyBot Test',
            '-c', 'user.email=polybot@example.invalid',
            'commit', '-qm', 'reviewed release',
        ],
        cwd=root,
        check=True,
    )
    checkpoint = subprocess.run(
        [git, 'rev-parse', 'HEAD'],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    value = runtime(
        project_root=root,
        source_script=source,
        deployed_script=deployed,
        reporting_exporter=exporter,
        reporting_python=python,
        current_executable=python,
        release_manifest=root / backup.RELEASE_MANIFEST_NAME,
    )
    return value, checkpoint


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

    def test_clean_pinned_release_and_runtime_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            value, checkpoint = make_release_runtime(directory)
            with mock.patch.object(backup, 'PRODUCTION_ROOT', value.project_root):
                manifest = backup.build_release_manifest_sync(
                    value,
                    expected_checkpoint=checkpoint,
                )
                backup.write_release_manifest_sync(value, manifest)
                result = backup.validate_runtime_sync(10, value)
        self.assertEqual(len(result.source_digest), 64)
        self.assertEqual(result.release_checkpoint, checkpoint)

    def test_matching_scripts_without_pinned_manifest_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            value, _checkpoint = make_release_runtime(directory)
            with mock.patch.object(backup, 'PRODUCTION_ROOT', value.project_root), \
                    self.assertRaises(backup.BackupSourceError):
                backup.validate_runtime_sync(10, value)

    def test_source_and_deployed_symlinks_fail_closed(self):
        for selected in ('source', 'deployed'):
            with self.subTest(selected=selected), \
                    tempfile.TemporaryDirectory() as directory:
                value, checkpoint = make_release_runtime(directory)
                with mock.patch.object(
                    backup,
                    'PRODUCTION_ROOT',
                    value.project_root,
                ):
                    manifest = backup.build_release_manifest_sync(
                        value,
                        expected_checkpoint=checkpoint,
                    )
                    backup.write_release_manifest_sync(value, manifest)
                    target = (
                        value.source_script
                        if selected == 'source'
                        else value.deployed_script
                    )
                    original = target.with_name(target.name + '.real')
                    target.rename(original)
                    target.symlink_to(original)
                    with mock.patch.object(
                        backup,
                        '_checkout_checkpoint',
                        return_value=checkpoint,
                    ), self.assertRaises(backup.BackupSourceError):
                        backup.validate_runtime_sync(10, value)

    def test_dirty_exporter_fails_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            value, checkpoint = make_release_runtime(directory)
            with mock.patch.object(backup, 'PRODUCTION_ROOT', value.project_root):
                manifest = backup.build_release_manifest_sync(
                    value,
                    expected_checkpoint=checkpoint,
                )
                backup.write_release_manifest_sync(value, manifest)
                value.reporting_exporter.write_text('dirty\n', encoding='utf-8')
                with self.assertRaises(backup.BackupSourceError):
                    backup.validate_runtime_sync(10, value)

    def test_wrong_checkout_checkpoint_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            value, checkpoint = make_release_runtime(directory)
            with mock.patch.object(backup, 'PRODUCTION_ROOT', value.project_root):
                manifest = backup.build_release_manifest_sync(
                    value,
                    expected_checkpoint=checkpoint,
                )
                wrong = backup.BackupReleaseManifest(
                    **{
                        **manifest.as_dict(),
                        'release_checkpoint': 'f' * 40,
                    }
                )
                backup.write_release_manifest_sync(value, wrong)
                with self.assertRaises(backup.BackupSourceError):
                    backup.validate_runtime_sync(10, value)

    def test_wrong_interpreter_identity_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            value, checkpoint = make_release_runtime(directory)
            other = value.project_root / '.venv/bin/other-python'
            other.write_bytes(b'other-runtime')
            other.chmod(0o700)
            with mock.patch.object(backup, 'PRODUCTION_ROOT', value.project_root):
                manifest = backup.build_release_manifest_sync(
                    value,
                    expected_checkpoint=checkpoint,
                )
                backup.write_release_manifest_sync(value, manifest)
                with self.assertRaises(backup.BackupSourceError):
                    backup.validate_runtime_sync(
                        10,
                        runtime(**{
                            **value.__dict__,
                            'current_executable': other,
                        }),
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


class BackupReleaseToolTests(unittest.TestCase):
    manifest = backup.BackupReleaseManifest(
        schema_version=1,
        release_checkpoint='a' * 40,
        backup_script_sha256='b' * 64,
        reporting_exporter_sha256='c' * 64,
        python_resolved_path='/reviewed/python',
        python_sha256='d' * 64,
    )

    def test_nonproduction_refuses_before_inspection(self):
        with mock.patch.dict(os.environ, {'POLYBOT_ENV': 'development'}), \
                mock.patch.object(
                    backup,
                    'build_release_manifest_sync',
                ) as build:
            result = release_tool.main(
                ['--checkpoint', 'a' * 40],
                runtime=runtime(),
            )
        self.assertEqual(result, 2)
        build.assert_not_called()

    def test_plan_is_read_only_and_apply_requires_exact_confirmation(self):
        value = runtime()
        with mock.patch.dict(os.environ, {'POLYBOT_ENV': 'production'}), \
                mock.patch.object(backup, 'PRODUCTION_ROOT', value.project_root), \
                mock.patch.object(
                    backup,
                    'build_release_manifest_sync',
                    return_value=self.manifest,
                ), mock.patch.object(
                    backup,
                    'write_release_manifest_sync',
                ) as write:
            plan_result = release_tool.main(
                ['--checkpoint', 'a' * 40],
                runtime=value,
            )
            refused_result = release_tool.main(
                ['--checkpoint', 'a' * 40, '--apply'],
                runtime=value,
            )
        self.assertEqual((plan_result, refused_result), (0, 2))
        write.assert_not_called()

    def test_apply_installs_only_after_exact_confirmation(self):
        value = runtime()
        with mock.patch.dict(os.environ, {'POLYBOT_ENV': 'production'}), \
                mock.patch.object(backup, 'PRODUCTION_ROOT', value.project_root), \
                mock.patch.object(
                    backup,
                    'build_release_manifest_sync',
                    return_value=self.manifest,
                ), mock.patch.object(
                    backup,
                    'write_release_manifest_sync',
                ) as write:
            result = release_tool.main(
                [
                    '--checkpoint',
                    'a' * 40,
                    '--apply',
                    '--confirm',
                    backup.RELEASE_MANIFEST_CONFIRMATION,
                ],
                runtime=value,
            )
        self.assertEqual(result, 0)
        write.assert_called_once_with(value, self.manifest)

    def test_validate_requires_manifest_preflight_checkpoint_match(self):
        value = runtime()
        preflight = backup.BackupPreflight('b' * 64, 'a' * 40)
        with mock.patch.dict(os.environ, {'POLYBOT_ENV': 'production'}), \
                mock.patch.object(backup, 'PRODUCTION_ROOT', value.project_root), \
                mock.patch.object(
                    backup,
                    'validate_runtime_sync',
                    return_value=preflight,
                ) as validate, mock.patch.object(
                    backup,
                    'build_release_manifest_sync',
                ) as build:
            result = release_tool.main(
                ['--checkpoint', 'a' * 40, '--validate'],
                runtime=value,
            )
        self.assertEqual(result, 0)
        validate.assert_called_once_with(value.owner_id, value)
        build.assert_not_called()


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
