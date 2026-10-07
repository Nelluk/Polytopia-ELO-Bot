"""Offline production feedback persistence and acknowledgement coverage."""

import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from modules import beta_feedback, staff_help
from tests import test_staff_help


class ProductionFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.profile = test_staff_help._production_profile()
        self.profile.project_root = self.root
        self.profile.log_root = self.root / 'logs'
        self.store = beta_feedback.FeedbackStore(self.profile)
        self.channel = SimpleNamespace(id=700, send=mock.AsyncMock(
            return_value=SimpleNamespace(id=701),
        ))
        guild = SimpleNamespace(
            id=200,
            name='Source Guild',
            get_channel=lambda channel_id: self.channel if channel_id == 700 else None,
        )
        self.bot = SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == 200 else None)
        self.draft = beta_feedback.build_report_draft(
            category='bug',
            summary='A broken command',
            details='Private report details.',
            context='/game show 42500',
            requester_id=100,
            requester_display_name='Tester',
            guild_id=200,
            channel_id=300,
            source='slash',
            game_id=42500,
            command_reference='/game',
            attachments=(beta_feedback.AttachmentInput(
                attachment_id=123,
                filename='evidence.txt',
                content_type='text/plain',
                extension='.txt',
                data=b'Private attachment.',
            ),),
            git_checkpoint='abcdef1',
        )

    def tearDown(self):
        self.store.executor.shutdown(wait=True)
        self.tempdir.cleanup()

    def submit(self, draft=None):
        with mock.patch.object(
                beta_feedback, '_default_feedback_store', return_value=self.store), \
                mock.patch.object(
                    staff_help.staff_help_workers, 'run_find_related_game',
                    new=mock.AsyncMock(return_value=None)):
            return asyncio.run(staff_help.submit(
                self.bot, draft or self.draft, profile=self.profile,
            ))

    def records(self):
        path = self.profile.log_root / 'production-feedback' / 'reports.jsonl'
        return [json.loads(line) for line in path.read_bytes().splitlines()]

    def test_bug_and_feature_commit_attachments_before_the_single_discord_post(self):
        async def send(*_args, **kwargs):
            # Inspect real local persistence at the moment Discord is called.
            record = self.records()[-1]
            paths = beta_feedback.feedback_paths(self.profile)
            metadata = record['attachments'][0]
            attachment_path = (
                paths.attachments_root / record['report_id'] / metadata['storage_name']
            )
            self.assertEqual(attachment_path.read_bytes(), b'Private attachment.')
            self.assertEqual(metadata['sha256'], hashlib.sha256(b'Private attachment.').hexdigest())
            self.assertEqual(stat.S_IMODE(attachment_path.stat().st_mode), 0o600)
            fields = {field.name: field.value for field in kwargs['embed'].fields}
            self.assertEqual(fields['Report ID'], f"`{record['report_id']}`")
            self.assertEqual(record['details'], self.draft.details)
            self.assertEqual(record['game_id'], 42500)
            self.assertEqual(record['command_reference'], '/game')
            self.assertEqual(len(kwargs['files']), 1)
            self.assertFalse(kwargs['allowed_mentions'].everyone)
            return SimpleNamespace(id=701)

        self.channel.send.side_effect = send
        for category in ('bug', 'feature'):
            with self.subTest(category=category):
                result = self.submit(replace(self.draft, category=category))
                self.assertTrue(result.stored)
                self.assertTrue(result.delivered)
                self.assertEqual(result.relay_message_id, 701)
                self.assertEqual(result.report_id, self.records()[-1]['report_id'])
                self.assertEqual(self.records()[-1]['category'], category)
        self.assertEqual(self.channel.send.await_count, 2)
        paths = beta_feedback.feedback_paths(self.profile)
        self.assertEqual(stat.S_IMODE(paths.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(paths.record_file.stat().st_mode), 0o600)
        development = SimpleNamespace(
            environment='development', project_root=self.root,
            log_root=self.root / 'logs' / 'development',
        )
        self.assertFalse(beta_feedback.feedback_paths(development).record_file.exists())

    def test_storage_preserves_existing_shared_log_root_permissions(self):
        self.profile.log_root.mkdir(mode=0o2770)
        self.profile.log_root.chmod(0o2770)
        self.submit()
        self.assertEqual(stat.S_IMODE(self.profile.log_root.stat().st_mode), 0o2770)

    def test_relay_failure_keeps_saved_report_and_logs_id_without_private_text(self):
        self.channel.send.side_effect = RuntimeError('SECRET transport detail')
        with self.assertLogs('polybot.modules.staff_help', level='WARNING') as logs:
            result = self.submit()
        self.assertTrue(result.stored)
        self.assertFalse(result.delivered)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(result.report_id, self.records()[0]['report_id'])
        logged = '\n'.join(logs.output)
        self.assertIn(result.report_id, logged)
        self.assertNotIn('SECRET transport detail', logged)
        self.assertNotIn(self.draft.details, logged)

    def test_missing_maintainer_destination_keeps_saved_report(self):
        self.profile.server_settings.polyelo_feedback_route = {}
        with self.assertLogs('polybot.modules.staff_help', level='WARNING'):
            result = self.submit()
        self.assertTrue(result.stored)
        self.assertFalse(result.delivered)
        self.assertEqual(len(self.records()), 1)
        self.channel.send.assert_not_awaited()

    def test_storage_failure_does_not_attempt_discord(self):
        with mock.patch.object(
                beta_feedback, '_append_record_line',
                side_effect=beta_feedback.FeedbackStorageError('disk full')):
            with self.assertRaises(beta_feedback.FeedbackStorageError):
                self.submit()
        self.channel.send.assert_not_awaited()
        paths = beta_feedback.feedback_paths(self.profile)
        self.assertFalse(paths.record_file.exists())

    def test_beta_mirror_remains_unavailable_in_production(self):
        with mock.patch.object(beta_feedback, 'get_runtime_profile', return_value=self.profile), \
                mock.patch.object(beta_feedback, 'store_report', new=mock.AsyncMock()) as store:
            with self.assertRaises(beta_feedback.FeedbackRuntimeGateError):
                asyncio.run(beta_feedback.submit_native_report(self.bot, self.draft))
        store.assert_not_awaited()
        self.channel.send.assert_not_awaited()

    def test_cancelled_relay_preserves_committed_report_and_logs_its_id(self):
        async def cancel_submission():
            started = asyncio.Event()
            release = asyncio.Event()

            async def slow_send(*_args, **_kwargs):
                started.set()
                await release.wait()
                return SimpleNamespace(id=701)

            self.channel.send.side_effect = slow_send
            with mock.patch.object(
                    beta_feedback, '_default_feedback_store', return_value=self.store), \
                    mock.patch.object(
                        staff_help.staff_help_workers, 'run_find_related_game',
                        new=mock.AsyncMock(return_value=None)):
                task = asyncio.create_task(staff_help.submit(
                    self.bot, self.draft, profile=self.profile,
                ))
                await started.wait()
                task.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        with self.assertLogs('polybot.modules.staff_help', level='WARNING') as logs:
            asyncio.run(cancel_submission())
        self.assertEqual(len(self.records()), 1)
        self.channel.send.assert_awaited_once()
        self.assertIn(self.records()[0]['report_id'], '\n'.join(logs.output))

    def test_uncertain_append_preserves_report_and_attachment_without_discord_post(self):
        original_fsync = beta_feedback._fsync_directory
        feedback_root = self.profile.log_root / 'production-feedback'

        def fail_record_fsync(path):
            if path == feedback_root:
                raise OSError('simulated fsync failure')
            original_fsync(path)

        with mock.patch.object(beta_feedback, '_fsync_directory', side_effect=fail_record_fsync), \
                self.assertLogs('polybot.modules.beta_feedback', level='WARNING'), \
                self.assertRaises(beta_feedback.FeedbackStorageError) as caught:
            self.submit()
        self.assertTrue(caught.exception.may_have_committed)
        record = self.records()[0]
        self.assertTrue((
            feedback_root / 'attachments' / record['report_id'] / 'attachment-01.txt'
        ).exists())
        self.channel.send.assert_not_awaited()


class ProductionFeedbackModalTests(unittest.TestCase):
    Response = test_staff_help.ProductionStaffHelpModalTests.Response
    Followup = test_staff_help.ProductionStaffHelpModalTests.Followup
    _interaction = test_staff_help.ProductionStaffHelpModalTests._interaction
    _modal = test_staff_help.ProductionStaffHelpModalTests._modal
    def test_saved_feedback_acknowledgement_includes_id_for_success_and_relay_failure(self):
        for category in ('bug', 'feature'):
            for delivered in (True, False):
                with self.subTest(category=category, delivered=delivered):
                    interaction = self._interaction()
                    modal = self._modal()
                    modal.category.component._value = category
                    result = staff_help.StaffHelpSubmission(
                        environment='production', delivered=delivered, stored=True,
                        report_id='r' + 'A' * 24, destination=f'polyelo_{category}',
                    )
                    with mock.patch.object(
                            beta_feedback, 'capture_attachments',
                            new=mock.AsyncMock(return_value=())), mock.patch.object(
                                staff_help, 'submit', new=mock.AsyncMock(return_value=result)):
                        asyncio.run(modal.on_submit(interaction))
                    content, kwargs = interaction.followup.sent[0]
                    self.assertIn('recorded as', content)
                    self.assertIn(result.report_id, content)
                    self.assertTrue(kwargs['ephemeral'])
                    if not delivered:
                        self.assertIn('do not need to submit it again', content)

    def test_uncertain_storage_failure_does_not_encourage_resubmission(self):
        interaction = self._interaction()
        modal = self._modal()
        modal.category.component._value = 'bug'
        with mock.patch.object(
                beta_feedback, 'capture_attachments', new=mock.AsyncMock(return_value=())), \
                mock.patch.object(
                    staff_help, 'submit', new=mock.AsyncMock(side_effect=(
                        beta_feedback.FeedbackStorageError('fsync failed', may_have_committed=True)
                    ))):
            asyncio.run(modal.on_submit(interaction))
        content, kwargs = interaction.followup.sent[0]
        self.assertIn('may have been recorded', content)
        self.assertNotIn('try again', content)
        self.assertTrue(kwargs['ephemeral'])

    def test_failed_acknowledgement_logs_saved_id_without_second_submission(self):
        interaction = self._interaction()
        interaction.followup.send = mock.AsyncMock(side_effect=RuntimeError('unavailable'))
        modal = self._modal()
        modal.category.component._value = 'bug'
        result = staff_help.StaffHelpSubmission(
            environment='production', delivered=True, stored=True,
            report_id='r' + 'A' * 24, destination='polyelo_bug',
        )
        with mock.patch.object(
                beta_feedback, 'capture_attachments', new=mock.AsyncMock(return_value=())), \
                mock.patch.object(
                    staff_help, 'submit', new=mock.AsyncMock(return_value=result)) as submit, \
                self.assertLogs('polybot.modules.beta_feedback_views', level='ERROR') as logs:
            asyncio.run(modal.on_submit(interaction))
        submit.assert_awaited_once()
        interaction.followup.send.assert_awaited_once()
        self.assertIn(result.report_id, '\n'.join(logs.output))
        self.assertNotIn('No report ID was issued', '\n'.join(logs.output))


if __name__ == '__main__':
    unittest.main()
