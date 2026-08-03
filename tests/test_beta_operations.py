"""Offline safety and idempotency coverage for WB1.2 beta operations."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import os
import stat
import tempfile
import unittest
from unittest import mock

import discord

from modules import beta_feedback, beta_operations
from scripts import manage_beta_release, run_development_beta


CHECKPOINT = 'a' * 40


def profile(root: Path, **overrides):
    values = {
        'environment': 'development',
        'project_root': root,
        'log_root': root / 'logs' / 'development',
        'expected_bot_id': beta_operations.BETA_APPLICATION_ID,
        'allowed_guild_ids': (beta_operations.BETA_GUILD_ID,),
        'database_name': beta_operations.BETA_DATABASE_NAME,
        'database_user': beta_operations.BETA_DATABASE_ROLE,
        'background_tasks_enabled': False,
        'api_enabled': False,
        'bullet_enabled': False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def manifest(*, ping=False, checkpoint=CHECKPOINT, release_id='wb1-2-test'):
    return {
        'schema_version': 1,
        'release_id': release_id,
        'expected_checkpoint': checkpoint,
        'title': 'Reviewed beta rollout',
        'bounded_summary': 'A safe, reviewed development release.',
        'changed_commands': ['/game show', '$help'],
        'known_limitations': ['Controls expire after the documented timeout.'],
        'smoke_test_checklist': ['Run the command and verify the public result.'],
        'ping_testers': ping,
    }


class FakeMessage:
    def __init__(self, message_id: int, content: str):
        self.id = message_id
        self.content = content


class FakeChannel:
    def __init__(self, guild, *, name=beta_operations.BETA_PUBLIC_RELEASE_CHANNEL_NAME):
        self.id = beta_operations.BETA_PUBLIC_RELEASE_CHANNEL_ID
        self.name = name
        self.guild = guild
        self.messages = []
        self.send_calls = []
        self.next_message_id = 500
        self.failure = None

    def history(self, *, limit):
        async def iterator():
            for message in list(reversed(self.messages))[:limit]:
                yield message
        return iterator()

    async def send(self, content, **kwargs):
        self.send_calls.append((content, kwargs))
        if self.failure is not None:
            failure = self.failure
            self.failure = None
            raise failure
        message = FakeMessage(self.next_message_id, content)
        self.next_message_id += 1
        self.messages.append(message)
        return message


class FakeGuild:
    def __init__(self, *, channel_name=beta_operations.BETA_PUBLIC_RELEASE_CHANNEL_NAME):
        self.id = beta_operations.BETA_GUILD_ID
        self.roles = []
        self.channel = FakeChannel(self, name=channel_name)

    def get_channel(self, channel_id):
        return self.channel if channel_id == self.channel.id else None


class FakeBot:
    def __init__(self, guild):
        self.user = SimpleNamespace(id=beta_operations.BETA_APPLICATION_ID)
        self.guild = guild

    def is_ready(self):
        return True

    def get_guild(self, guild_id):
        return self.guild if guild_id == beta_operations.BETA_GUILD_ID else None


class BetaRuntimeGuardTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_service_template_is_development_only_and_explicit(self):
        root = Path(__file__).resolve().parents[1]
        template = (root / 'deploy/systemd/polybot-development-beta@.service').read_text()
        self.assertIn('Environment=POLYBOT_ENV=development', template)
        self.assertIn('Environment=POLYBOT_BETA_STARTUP_SYNC=disabled', template)
        self.assertIn('Environment=POLYBOT_BETA_DATABASE=polytopia_dev', template)
        self.assertIn('Environment=POLYBOT_BETA_DATABASE_ROLE=polybot_dev', template)
        self.assertIn('--skip_tasks', template)
        self.assertIn('Restart=on-failure', template)
        self.assertIn('RestartSec=5s', template)
        self.assertIn('KillSignal=SIGINT', template)
        self.assertIn('WorkingDirectory=/home/nelluk/PolyBot39-dev', template)
        self.assertNotRegex(template, r'(?<!-)\/home/nelluk/PolyBot39(?:/|$)')
        self.assertNotIn('polytopia2', template)

    def test_profile_invariants_fail_closed(self):
        good = profile(self.root)
        beta_operations.assert_beta_profile(good)
        for field, value in (
                ('environment', 'production'),
                ('expected_bot_id', 123),
                ('allowed_guild_ids', (1,)),
                ('database_name', 'polytopia2'),
                ('database_user', 'postgres'),
                ('background_tasks_enabled', True),
                ('api_enabled', True),
                ('bullet_enabled', True)):
            with self.subTest(field=field):
                with self.assertRaises(beta_operations.BetaRuntimeInvariantError):
                    beta_operations.assert_beta_profile(
                        profile(self.root, **{field: value})
                    )

    def test_service_environment_and_startup_flag_are_exact(self):
        good = {
            'POLYBOT_ENV': 'development',
            'POLYBOT_BETA_CONTROL': 'enabled',
            'POLYBOT_BETA_STARTUP_SYNC': 'disabled',
            'POLYBOT_BETA_APPLICATION_ID': str(beta_operations.BETA_APPLICATION_ID),
            'POLYBOT_BETA_GUILD_ID': str(beta_operations.BETA_GUILD_ID),
            'POLYBOT_BETA_DATABASE': 'polytopia_dev',
            'POLYBOT_BETA_DATABASE_ROLE': 'polybot_dev',
        }
        beta_operations.assert_beta_profile(
            profile(self.root), environ=good, require_service_environment=True
        )
        for key in ('POLYBOT_ENV', 'POLYBOT_BETA_STARTUP_SYNC', 'POLYBOT_BETA_DATABASE_ROLE'):
            with self.subTest(key=key):
                bad = dict(good)
                bad[key] = 'wrong'
                with self.assertRaises(beta_operations.BetaRuntimeInvariantError):
                    beta_operations.assert_beta_profile(
                        profile(self.root), environ=bad, require_service_environment=True
                    )

    def test_single_writer_lock_blocks_a_second_beta(self):
        paths = beta_operations.operation_paths(profile(self.root), create=True)
        first = beta_operations.BetaWriterLock(paths.writer_lock)
        second = beta_operations.BetaWriterLock(paths.writer_lock)
        first.acquire()
        try:
            with self.assertRaises(beta_operations.BetaRuntimeInvariantError):
                second.acquire()
        finally:
            first.release()
        second.acquire()
        second.release()

    def test_state_root_and_writer_lock_are_restricted(self):
        paths = beta_operations.operation_paths(profile(self.root), create=True)
        self.assertEqual(stat.S_IMODE(paths.state_root.stat().st_mode), 0o700)
        lock = beta_operations.BetaWriterLock(paths.writer_lock)
        lock.acquire()
        lock.release()
        self.assertEqual(stat.S_IMODE(paths.writer_lock.stat().st_mode), 0o600)

    def test_launcher_executes_only_the_guarded_skip_tasks_command(self):
        selected_profile = profile(self.root)
        environment = {
            'POLYBOT_ENV': 'development',
            'POLYBOT_BETA_CONTROL': 'enabled',
            'POLYBOT_BETA_STARTUP_SYNC': 'disabled',
            'POLYBOT_BETA_APPLICATION_ID': str(beta_operations.BETA_APPLICATION_ID),
            'POLYBOT_BETA_GUILD_ID': str(beta_operations.BETA_GUILD_ID),
            'POLYBOT_BETA_DATABASE': beta_operations.BETA_DATABASE_NAME,
            'POLYBOT_BETA_DATABASE_ROLE': beta_operations.BETA_DATABASE_ROLE,
        }
        executed = []
        with mock.patch.dict(os.environ, environment, clear=False), \
                mock.patch.object(run_development_beta, 'load_runtime_profile', return_value=selected_profile), \
                mock.patch.object(run_development_beta, 'validate_beta_launch', return_value=CHECKPOINT), \
                mock.patch.object(run_development_beta, 'assert_beta_profile'), \
                mock.patch.object(
                    run_development_beta.os,
                    'execv',
                    side_effect=lambda python, argv: executed.append((python, argv)),
                ):
            with mock.patch.object(
                    run_development_beta,
                    'operation_paths',
                    return_value=beta_operations.operation_paths(selected_profile, create=True),
            ):
                self.assertEqual(run_development_beta.main(['--skip_tasks']), 0)
        self.assertEqual(len(executed), 1)
        self.assertEqual(executed[0][1][-1], '--skip_tasks')

    def test_launcher_rejects_any_other_runtime_argument(self):
        selected_profile = profile(self.root)
        with mock.patch.object(
                run_development_beta,
                'load_runtime_profile',
                return_value=selected_profile):
            self.assertEqual(run_development_beta.main([]), 2)

    def test_startup_source_has_no_automatic_sync_or_release_on_ready(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / 'bot.py').read_text()
        self.assertNotIn('tree.sync', source)
        on_ready_source = source[source.index('async def on_ready'):]
        self.assertNotIn('deliver(', on_ready_source)
        self.assertNotIn('resolve-tester-role', on_ready_source)
        self.assertIn('Automatic application-command synchronization is disabled', on_ready_source)


class ReleaseManifestTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.manifest_root = self.root / beta_operations.BETA_MANIFEST_DIRECTORY
        self.manifest_root.mkdir()
        self.profile = profile(self.root)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_manifest_schema_bounds_and_checkpoint(self):
        parsed = beta_operations.validate_release_manifest(
            manifest(), current_checkpoint=CHECKPOINT
        )
        self.assertEqual(parsed.release_id, 'wb1-2-test')
        self.assertEqual(parsed.changed_commands, ('/game show', '$help'))
        with self.assertRaises(beta_operations.ReleaseManifestError):
            beta_operations.validate_release_manifest(
                manifest(checkpoint='b' * 40), current_checkpoint=CHECKPOINT
            )
        with self.assertRaises(beta_operations.ReleaseManifestError):
            beta_operations.validate_release_manifest({**manifest(), 'smoke_test_checklist': []})
        with self.assertRaises(beta_operations.ReleaseManifestError):
            beta_operations.validate_release_manifest({**manifest(), 'ping_testers': 1})
        with self.assertRaises(beta_operations.ReleaseManifestError):
            beta_operations.validate_release_manifest({**manifest(), 'changed_commands': ['not a command']})
        with self.assertRaises(beta_operations.ReleaseManifestError):
            beta_operations.validate_release_manifest({**manifest(), 'title': 'x\nunsafe'})

    def test_manifest_file_path_is_repository_backed_and_traversal_safe(self):
        path = self.manifest_root / 'release.json'
        path.write_text(json.dumps(manifest()), encoding='utf-8')
        parsed = beta_operations.load_release_manifest(
            self.profile,
            Path('release-manifests/release.json'),
            current_checkpoint=CHECKPOINT,
        )
        self.assertEqual(parsed.release_id, 'wb1-2-test')
        with self.assertRaises(beta_operations.BetaPathError):
            beta_operations.load_release_manifest(
                self.profile,
                Path('../release.json'),
                current_checkpoint=CHECKPOINT,
            )
        link = self.manifest_root / 'link.json'
        link.symlink_to(path)
        with self.assertRaises(beta_operations.BetaPathError):
            beta_operations.load_release_manifest(
                self.profile,
                Path('release-manifests/link.json'),
                current_checkpoint=CHECKPOINT,
            )

    def test_rendered_manifest_never_contains_private_feedback_body(self):
        parsed = beta_operations.validate_release_manifest(
            manifest(), current_checkpoint=CHECKPOINT
        )
        announcement = beta_operations.build_release_announcement(parsed)
        self.assertIn('Reviewed beta rollout', announcement)
        self.assertNotIn('SECRET REPORT DETAILS', announcement)
        self.assertLessEqual(len(announcement), beta_operations.MAX_ANNOUNCEMENT_LENGTH)


class ReleaseDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.profile = profile(self.root)
        self.guild = FakeGuild()
        self.bot = FakeBot(self.guild)
        self.service = beta_operations.BetaReleaseService(
            self.bot, self.profile, CHECKPOINT
        )

    async def asyncTearDown(self):
        self.tempdir.cleanup()

    async def test_minor_release_posts_once_with_no_role_mention(self):
        first = await self.service.deliver(manifest())
        second = await self.service.deliver(manifest())
        self.assertEqual(first.status, 'posted')
        self.assertEqual(second.status, 'already-posted')
        self.assertEqual(len(self.guild.channel.send_calls), 1)
        content, kwargs = self.guild.channel.send_calls[0]
        self.assertNotIn('<@&', content)
        self.assertFalse(kwargs['allowed_mentions'].roles)
        self.assertFalse(kwargs['allowed_mentions'].everyone)
        self.assertEqual(first.message_id, second.message_id)
        self.assertEqual(
            stat.S_IMODE(self.service.paths.release_state.stat().st_mode),
            0o600,
        )
        os.chmod(self.service.paths.release_state, 0o644)
        with self.assertRaises(beta_operations.BetaPathError):
            self.service.status()

    async def test_ping_requires_persisted_unique_role_and_allows_only_that_role(self):
        with self.assertRaises(beta_operations.ReleaseRoleError):
            await self.service.deliver(manifest(ping=True))
        self.guild.roles = [SimpleNamespace(id=901, name=beta_operations.BETA_TESTER_ROLE_NAME)]
        binding = await self.service.resolve_tester_role()
        self.assertEqual(binding.role_id, 901)
        result = await self.service.deliver(manifest(ping=True, release_id='pinged-release'))
        self.assertEqual(result.status, 'posted')
        content, kwargs = self.guild.channel.send_calls[0]
        self.assertIn('<@&901>', content)
        allowed_roles = kwargs['allowed_mentions'].roles
        self.assertEqual([int(role.id) for role in allowed_roles], [901])
        self.assertFalse(kwargs['allowed_mentions'].users)
        self.assertFalse(kwargs['allowed_mentions'].everyone)

    async def test_missing_or_ambiguous_or_changed_role_fails_before_post(self):
        self.guild.roles = [SimpleNamespace(id=901, name=beta_operations.BETA_TESTER_ROLE_NAME)]
        await self.service.resolve_tester_role()
        self.guild.roles.append(SimpleNamespace(id=902, name=beta_operations.BETA_TESTER_ROLE_NAME))
        with self.assertRaises(beta_operations.ReleaseRoleError):
            await self.service.deliver(manifest(ping=True))
        self.assertEqual(self.guild.channel.send_calls, [])

    async def test_wrong_channel_or_checkpoint_fails_before_post(self):
        wrong_guild = FakeGuild(channel_name='not-todo-and-changelog')
        wrong_bot = FakeBot(wrong_guild)
        wrong_service = beta_operations.BetaReleaseService(
            wrong_bot, self.profile, CHECKPOINT
        )
        with self.assertRaises(beta_operations.ReleaseDeliveryError):
            await wrong_service.deliver(manifest())
        with self.assertRaises(beta_operations.ReleaseManifestError):
            await self.service.deliver(manifest(checkpoint='b' * 40))
        self.assertEqual(self.guild.channel.send_calls, [])

    async def test_certain_failed_post_can_retry_without_duplicate_success(self):
        self.guild.channel.failure = RuntimeError('rejected before send')
        with mock.patch.object(
                beta_operations,
                '_discord_send_is_certainly_rejected',
                return_value=True):
            with self.assertRaises(beta_operations.ReleasePostFailure) as raised:
                await self.service.deliver(manifest())
        self.assertTrue(raised.exception.retryable)
        retried = await self.service.deliver(manifest())
        self.assertEqual(retried.status, 'posted')
        self.assertEqual(len(self.guild.channel.messages), 1)
        self.assertEqual(len(self.guild.channel.send_calls), 2)
        again = await self.service.deliver(manifest())
        self.assertEqual(again.status, 'already-posted')
        self.assertEqual(len(self.guild.channel.send_calls), 2)

    async def test_uncertain_failed_post_blocks_retry_and_crash_recovery_scans_marker(self):
        self.guild.channel.failure = RuntimeError('connection lost after request')
        with mock.patch.object(
                beta_operations,
                '_discord_send_is_certainly_rejected',
                return_value=False):
            with self.assertRaises(beta_operations.ReleasePostFailure) as raised:
                await self.service.deliver(manifest())
        self.assertFalse(raised.exception.retryable)
        with self.assertRaises(beta_operations.ReleaseDeliveryError):
            await self.service.deliver(manifest())
        # Simulate the remote post having succeeded just before the process
        # crashed.  The bounded marker scan marks it posted without reposting.
        self.guild.channel.messages.append(FakeMessage(
            777,
            beta_operations.build_release_announcement(
                beta_operations.validate_release_manifest(manifest())
            ),
        ))
        state = self.service.status()
        state['releases']['wb1-2-test']['status'] = 'posting'
        beta_operations._write_release_state(self.service.paths, state)
        recovered = await self.service.deliver(manifest())
        self.assertEqual(recovered.status, 'already-posted')
        # An uncertain state is never sent again.  The marker lets the
        # authenticated process reconcile a post that committed before a
        # crash, without creating a duplicate success.
        self.assertEqual(len(self.guild.channel.send_calls), 1)

    async def test_history_check_failure_prevents_first_post(self):
        def broken_history(*, limit):
            async def iterator():
                raise RuntimeError('history unavailable')
                yield None
            return iterator()

        self.guild.channel.history = broken_history
        with self.assertRaises(beta_operations.ReleaseDeliveryError):
            await self.service.deliver(manifest())
        self.assertEqual(self.guild.channel.send_calls, [])


class ReleaseControlAndSeparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_unix_control_socket_is_local_and_status_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected_profile = profile(root)
            environment = {
                'POLYBOT_BETA_CONTROL': 'enabled',
                'POLYBOT_BETA_STARTUP_SYNC': 'disabled',
                'POLYBOT_ENV': 'development',
                'POLYBOT_BETA_APPLICATION_ID': str(beta_operations.BETA_APPLICATION_ID),
                'POLYBOT_BETA_GUILD_ID': str(beta_operations.BETA_GUILD_ID),
                'POLYBOT_BETA_DATABASE': beta_operations.BETA_DATABASE_NAME,
                'POLYBOT_BETA_DATABASE_ROLE': beta_operations.BETA_DATABASE_ROLE,
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                control = beta_operations.BetaReleaseControl(
                    FakeBot(FakeGuild()), selected_profile, CHECKPOINT
                )
                fake_server = SimpleNamespace(
                    close=mock.Mock(),
                    wait_closed=mock.AsyncMock(),
                )
                async def fake_start(handler, *, path, limit):
                    self.assertEqual(path, str(control.paths.socket_path))
                    self.assertEqual(limit, beta_operations.MAX_SOCKET_REQUEST_BYTES)
                    return fake_server
                with mock.patch.object(
                        asyncio, 'start_unix_server', side_effect=fake_start), \
                        mock.patch.object(beta_operations.os, 'chmod') as chmod:
                    await control.start()
                    result = await control._dispatch({'operation': 'status'})
                    self.assertEqual(result['schema_version'], 1)
                    chmod.assert_called_once_with(control.paths.socket_path, 0o600)
                    await control.stop()
                fake_server.close.assert_called_once_with()
                fake_server.wait_closed.assert_awaited_once_with()

    def test_staffhelp_mirror_and_release_target_are_separate_fixed_channels(self):
        self.assertEqual(
            beta_operations.BETA_STAFFHELP_MIRROR_CHANNEL_ID,
            480078679930830849,
        )
        self.assertEqual(
            beta_operations.BETA_PUBLIC_RELEASE_CHANNEL_ID,
            481779940124000256,
        )
        self.assertNotEqual(
            beta_operations.BETA_STAFFHELP_MIRROR_CHANNEL_ID,
            beta_operations.BETA_PUBLIC_RELEASE_CHANNEL_ID,
        )
        settings_stub = SimpleNamespace(
            runtime_profile=SimpleNamespace(environment='development'),
        )
        channel = SimpleNamespace(id=beta_operations.BETA_STAFFHELP_MIRROR_CHANNEL_ID)
        guild = SimpleNamespace(
            id=beta_operations.BETA_GUILD_ID,
            get_channel=lambda channel_id: channel if channel_id == channel.id else None,
        )
        bot = SimpleNamespace(get_guild=lambda guild_id: guild)
        with mock.patch.dict('sys.modules', {'settings': settings_stub}):
            resolved = beta_feedback.staff_help_channel(bot, beta_operations.BETA_GUILD_ID)
        self.assertIs(resolved, channel)

    def test_manager_validate_is_offline_and_checkpoint_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_root = root / beta_operations.BETA_MANIFEST_DIRECTORY
            manifest_root.mkdir()
            path = manifest_root / 'release.json'
            path.write_text(json.dumps(manifest()), encoding='utf-8')
            selected_profile = profile(root)
            stdout = []
            with mock.patch.object(manage_beta_release, '_selected_profile', return_value=selected_profile), \
                    mock.patch.object(manage_beta_release, 'assert_clean_checkout'), \
                    mock.patch.object(manage_beta_release, 'current_checkpoint', return_value=CHECKPOINT), \
                    mock.patch('sys.stdout', new_callable=lambda: _ListWriter(stdout)):
                result = manage_beta_release.main([
                    '--json', 'validate', '--manifest', 'release-manifests/release.json'
                ])
            self.assertEqual(result, 0)
            self.assertIn('"status": "valid"', ''.join(stdout))


class _ListWriter:
    def __init__(self, target):
        self.target = target

    def write(self, value):
        self.target.append(value)
        return len(value)

    def flush(self):
        pass


if __name__ == '__main__':
    unittest.main()
