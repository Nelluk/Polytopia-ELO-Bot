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

from modules import (
    beta_feedback,
    beta_lab_personas,
    beta_lab_workers,
    beta_operations,
)
from scripts import (
    audit_development_beta_processes,
    manage_beta_release,
    run_development_beta,
)


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


def manifest(*, ping=False, checkpoint=CHECKPOINT, release_id='wb1-2-test', notify_users=None):
    value = {
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
    if notify_users is not None:
        value['notify_user_ids'] = notify_users
    return value


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
        self.assertIn(
            'ExecStartPre=/home/nelluk/PolyBot39-dev/.venv/bin/python '
            '/home/nelluk/PolyBot39-dev/scripts/audit_development_beta_processes.py '
            '--require-clear',
            template,
        )
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

    def test_unguarded_legacy_beta_is_seen_without_a_wrapper_lock(self):
        proc_root = self.root / 'proc'

        def fake_process(pid, command, cwd, *, ppid=1, uid=1000):
            process = proc_root / str(pid)
            process.mkdir(parents=True)
            (process / 'cmdline').write_bytes(b'\0'.join(
                item.encode() for item in command
            ) + b'\0')
            (process / 'cwd').symlink_to(cwd)
            (process / 'stat').write_text(
                f'{pid} (python) S {ppid} 0 0 0',
                encoding='utf-8',
            )
            (process / 'status').write_text(
                f'Name:\tpython\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n',
                encoding='utf-8',
            )

        fake_process(
            101,
            ['/home/nelluk/.venv/bin/python', '/tmp/task/PolyBot39-dev/bot.py', '--skip_tasks'],
            '/tmp/task/PolyBot39-dev',
            ppid=55,
        )
        fake_process(
            202,
            ['/home/nelluk/.venv/bin/python', '/home/nelluk/PolyBot39/bot.py', '--skip_tasks'],
            '/home/nelluk/PolyBot39',
            ppid=66,
        )
        audit = audit_development_beta_processes.audit_processes(proc_root)
        self.assertEqual([candidate.pid for candidate in audit.candidates], [101, 202])
        self.assertEqual(audit.candidates[0].classification, 'development')
        self.assertEqual(audit.candidates[1].classification, 'production')
        self.assertEqual(audit.unreadable, ())

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
                ), mock.patch.object(
                    run_development_beta.Path,
                    'is_file',
                    return_value=True,
                ), mock.patch.object(
                    run_development_beta.os.path,
                    'samefile',
                    return_value=True,
                ):
            with mock.patch.object(
                    run_development_beta,
                    'operation_paths',
                    return_value=beta_operations.operation_paths(selected_profile, create=True),
            ):
                self.assertEqual(run_development_beta.main(['--skip_tasks']), 0)
        self.assertEqual(len(executed), 1)
        self.assertEqual(
            executed[0][0],
            str(run_development_beta.SHARED_DEVELOPMENT_PYTHON),
        )
        self.assertEqual(executed[0][1][0], executed[0][0])
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

    def test_first_activation_gate_documents_unguarded_lock_limitation(self):
        root = Path(__file__).resolve().parents[1]
        service = (root / 'deploy/systemd/polybot-development-beta@.service').read_text()
        runbook = (root / 'docs/DEVELOPMENT_BETA_OPERATIONS.md').read_text()
        self.assertIn('--require-clear', service)
        self.assertIn('cannot see or stop an already-running unguarded', runbook)
        self.assertIn('kill -INT PID', runbook)
        self.assertIn('Do not use `pkill`, `killall`', runbook)
        self.assertNotIn('pkill', service)
        self.assertNotIn('killall', service)

    def test_release_process_makes_testability_and_terminal_action_durable(self):
        root = Path(__file__).resolve().parents[1]
        runbook = (root / 'docs/DEVELOPMENT_BETA_OPERATIONS.md').read_text()
        workflow = (
            root / 'docs/MODERNIZATION_COLLABORATION_WORKFLOW.md'
        ).read_text()
        checklist = (root / 'docs/BETA_WHAT_TO_TEST.md').read_text()

        self.assertIn('must be usable against the current development state', runbook)
        self.assertIn('terminal deployment\n   action', runbook)
        self.assertIn('does not justify restarting an unchanged bot', runbook)
        self.assertIn('Chat promises are not process authority', workflow)
        self.assertIn('Successful announcement delivery is', workflow)
        self.assertIn('**No eligible squads** promptly', checklist)
        self.assertIn('stale desktop/mobile command caches', checklist)
        self.assertIn('one to three registered members', checklist)


class ReleaseManifestTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.manifest_root = self.root / beta_operations.BETA_MANIFEST_DIRECTORY
        self.manifest_root.mkdir()
        self.profile = profile(self.root)
        self.template_path = self.manifest_root / beta_operations.BETA_TEMPLATE_FILENAME
        self.template_path.write_text(
            json.dumps(manifest(
                checkpoint=beta_operations.DRAFT_CHECKPOINT,
                release_id='replace-me',
            )),
            encoding='utf-8',
        )

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

    def test_prepare_sequence_from_tracked_template_is_constructible(self):
        draft = beta_operations.init_release_draft(self.profile, 'constructible-release')
        draft_value = json.loads(draft.read_text(encoding='utf-8'))
        draft_value['title'] = 'Prepared reviewed release'
        draft.write_text(json.dumps(draft_value), encoding='utf-8')
        prepared = beta_operations.prepare_release_manifest(
            self.profile,
            draft,
            current_checkpoint=CHECKPOINT,
        )
        self.assertEqual(prepared.status, 'prepared')
        self.assertEqual(prepared.manifest.expected_checkpoint, CHECKPOINT)
        self.assertEqual(stat.S_IMODE(prepared.draft_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(prepared.prepared_path.stat().st_mode), 0o600)
        loaded = beta_operations.load_prepared_release_manifest(
            self.profile,
            prepared.prepared_path,
            current_checkpoint=CHECKPOINT,
        )
        self.assertEqual(loaded.as_dict(), prepared.manifest.as_dict())
        state = beta_operations._read_release_state(
            beta_operations.operation_paths(self.profile, create=False),
        )
        self.assertEqual(
            state['prepared']['constructible-release']['fingerprint'],
            prepared.fingerprint,
        )
        again = beta_operations.prepare_release_manifest(
            self.profile,
            draft,
            current_checkpoint=CHECKPOINT,
        )
        self.assertEqual(again.status, 'already-prepared')

    def test_prepare_rejects_a_self_pinned_or_unprepared_draft(self):
        draft = beta_operations.init_release_draft(self.profile, 'checkpoint-gate')
        draft_value = json.loads(draft.read_text(encoding='utf-8'))
        draft_value['expected_checkpoint'] = CHECKPOINT
        draft.write_text(json.dumps(draft_value), encoding='utf-8')
        with self.assertRaises(beta_operations.ReleaseManifestError):
            beta_operations.prepare_release_manifest(
                self.profile,
                draft,
                current_checkpoint=CHECKPOINT,
            )

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
        self.assertIn('## 🧪 WHAT TO TEST', announcement)
        self.assertNotIn('Smoke test:', announcement)
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
        self._archive_for_delivery(manifest())

    async def asyncTearDown(self):
        self.tempdir.cleanup()

    def _archive_for_delivery(self, value):
        parsed = beta_operations.validate_release_manifest(
            value,
            current_checkpoint=CHECKPOINT,
        )
        state = self.service.status()
        state['prepared'][parsed.release_id] = {
            'fingerprint': beta_operations.manifest_fingerprint(parsed),
            'manifest': parsed.as_dict(),
        }
        beta_operations._write_release_state(self.service.paths, state)

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

    async def test_delivery_requires_the_archived_prepared_manifest(self):
        state = self.service.status()
        state['prepared'].pop('wb1-2-test', None)
        beta_operations._write_release_state(self.service.paths, state)
        with self.assertRaises(beta_operations.ReleaseDeliveryError):
            await self.service.deliver(manifest())
        self.assertEqual(self.guild.channel.send_calls, [])

    async def test_ping_requires_persisted_unique_role_and_allows_only_that_role(self):
        self._archive_for_delivery(manifest(ping=True))
        with self.assertRaises(beta_operations.ReleaseRoleError):
            await self.service.deliver(manifest(ping=True))
        self.guild.roles = [SimpleNamespace(id=901, name=beta_operations.BETA_TESTER_ROLE_NAME)]
        binding = await self.service.resolve_tester_role()
        self.assertEqual(binding.role_id, 901)
        self._archive_for_delivery(manifest(ping=True, release_id='pinged-release'))
        result = await self.service.deliver(manifest(ping=True, release_id='pinged-release'))
        self.assertEqual(result.status, 'posted')
        content, kwargs = self.guild.channel.send_calls[0]
        self.assertIn('<@&901>', content)
        allowed_roles = kwargs['allowed_mentions'].roles
        self.assertEqual([int(role.id) for role in allowed_roles], [901])
        self.assertFalse(kwargs['allowed_mentions'].users)
        self.assertFalse(kwargs['allowed_mentions'].everyone)

    async def test_explicit_user_notification_allows_only_reviewed_users(self):
        user_id = 339773948537470976
        value = manifest(notify_users=[user_id])
        self._archive_for_delivery(value)

        result = await self.service.deliver(value)

        self.assertEqual(result.status, 'posted')
        content, kwargs = self.guild.channel.send_calls[0]
        self.assertIn(f'<@{user_id}>', content)
        self.assertEqual(
            [int(user.id) for user in kwargs['allowed_mentions'].users],
            [user_id],
        )
        self.assertFalse(kwargs['allowed_mentions'].roles)
        self.assertFalse(kwargs['allowed_mentions'].everyone)

    def test_explicit_user_notifications_are_bounded_and_validated(self):
        parsed = beta_operations.validate_release_manifest(
            manifest(notify_users=[339773948537470976]),
            current_checkpoint=CHECKPOINT,
        )
        self.assertEqual(parsed.notify_user_ids, (339773948537470976,))
        for invalid in (
            ['339773948537470976'],
            [True],
            [123],
            [339773948537470976, 339773948537470976],
            [339773948537470976] * (beta_operations.MAX_NOTIFY_USERS + 1),
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(beta_operations.ReleaseManifestError):
                    beta_operations.validate_release_manifest(
                        manifest(notify_users=invalid),
                        current_checkpoint=CHECKPOINT,
                    )

    async def test_missing_or_ambiguous_or_changed_role_fails_before_post(self):
        self._archive_for_delivery(manifest(ping=True))
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
        wrong_state = wrong_service.status()
        parsed = beta_operations.validate_release_manifest(
            manifest(),
            current_checkpoint=CHECKPOINT,
        )
        wrong_state['prepared'][parsed.release_id] = {
            'fingerprint': beta_operations.manifest_fingerprint(parsed),
            'manifest': parsed.as_dict(),
        }
        beta_operations._write_release_state(wrong_service.paths, wrong_state)
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
    async def test_control_dispatches_bounded_beta_lab_operations(self):
        control = beta_operations.BetaReleaseControl.__new__(
            beta_operations.BetaReleaseControl
        )
        control.service = SimpleNamespace(
            _assert_authenticated_identity=mock.Mock(),
            _guild=mock.Mock(),
        )
        status = beta_lab_workers.BetaLabStatus(
            guild_id=beta_operations.BETA_GUILD_ID,
            overall='ready',
            packs=(),
            result_snapshot=None,
        )
        with mock.patch.object(
            beta_lab_workers,
            'run_status',
            new=mock.AsyncMock(return_value=status),
        ):
            result = await control._dispatch({'operation': 'beta-lab-plan'})
        self.assertEqual(result['overall'], 'ready')
        self.assertEqual(result['live_apply_supported'], ['game-results'])

        refresh_result = beta_lab_workers.BetaLabRefreshResult(
            pack='game-results',
            committed=True,
            old_game_ids=(1, 2, 3),
            new_game_ids=(4, 5, 6),
            status=status,
        )
        with mock.patch.object(
            beta_lab_workers,
            'refresh_results',
            new=mock.AsyncMock(return_value=refresh_result),
        ) as refresh:
            result = await control._dispatch({
                'operation': 'beta-lab-refresh',
                'pack': 'game-results',
                'confirm': beta_lab_workers.REFRESH_CONFIRMATION,
            })
        self.assertTrue(result['committed'])
        refresh.assert_awaited_once_with(
            guild_id=beta_operations.BETA_GUILD_ID,
            actor='Local Beta Lab operator',
        )

        with self.assertRaises(beta_operations.BetaOperationsError):
            await control._dispatch({
                'operation': 'beta-lab-refresh',
                'pack': 'game-results',
                'confirm': 'wrong',
            })

    async def test_persona_role_setup_requires_exact_control_confirmation(self):
        control = beta_operations.BetaReleaseControl.__new__(
            beta_operations.BetaReleaseControl
        )
        guild = object()
        control.profile = object()
        control.service = SimpleNamespace(
            _assert_authenticated_identity=mock.Mock(),
            _guild=mock.Mock(return_value=guild),
        )
        with mock.patch.object(
            beta_lab_personas,
            'setup_roles',
            new=mock.AsyncMock(return_value=beta_lab_personas.PersonaRoleBinding(
                700, 701,
            )),
        ) as setup:
            with self.assertRaises(beta_operations.BetaOperationsError):
                await control._dispatch({
                    'operation': 'beta-lab-persona-setup',
                    'confirm': 'wrong',
                })
            setup.assert_not_awaited()
            result = await control._dispatch({
                'operation': 'beta-lab-persona-setup',
                'confirm': 'PREPARE-BETA-LAB-PERSONAS',
            })
        self.assertTrue(result['ready'])
        self.assertEqual(result['team_role_id'], 700)
        self.assertEqual(result['staff_role_id'], 701)
        setup.assert_awaited_once_with(control.profile, guild)

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
            (manifest_root / beta_operations.BETA_TEMPLATE_FILENAME).write_text(
                json.dumps(manifest(
                    checkpoint=beta_operations.DRAFT_CHECKPOINT,
                    release_id='replace-me',
                )),
                encoding='utf-8',
            )
            selected_profile = profile(root)
            stdout = []
            with mock.patch.object(manage_beta_release, '_selected_profile', return_value=selected_profile), \
                    mock.patch.object(manage_beta_release, 'assert_clean_checkout'), \
                    mock.patch.object(manage_beta_release, 'current_checkpoint', return_value=CHECKPOINT), \
                    mock.patch('sys.stdout', new_callable=lambda: _ListWriter(stdout)):
                result = manage_beta_release.main([
                    '--json', 'init', '--release-id', 'manager-release'
                ])
                self.assertEqual(result, 0)
                draft_path = (
                    selected_profile.log_root
                    / beta_operations.BETA_STATE_DIRECTORY
                    / beta_operations.BETA_DRAFT_DIRECTORY
                    / 'manager-release.json'
                )
                result = manage_beta_release.main([
                    '--json', 'prepare', '--manifest', str(
                        draft_path.relative_to(selected_profile.project_root)
                    )
                ])
                self.assertEqual(result, 0)
                prepared_path = (
                    selected_profile.log_root
                    / beta_operations.BETA_STATE_DIRECTORY
                    / beta_operations.BETA_PREPARED_DIRECTORY
                    / 'manager-release.json'
                )
                result = manage_beta_release.main([
                    '--json', 'validate', '--manifest', str(
                        prepared_path.relative_to(selected_profile.project_root)
                    )
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
