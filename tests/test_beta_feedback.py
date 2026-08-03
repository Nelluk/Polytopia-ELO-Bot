"""Offline coverage for WB1.1 structured beta feedback intake."""

from dataclasses import FrozenInstanceError
import asyncio
from contextlib import redirect_stderr, redirect_stdout
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import stat
import tempfile
import time
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest import mock

import discord
from discord.ext import commands

from modules import beta_feedback
from modules.beta_feedback_views import StaffHelpModal
from scripts import manage_beta_feedback
from tests.test_newgame_worker import import_offline_runtime


misc_module = import_offline_runtime('modules.misc')


class FakeAttachment:
    def __init__(
            self,
            filename='evidence.png',
            content_type='image/png',
            data=b'PNG bytes',
            attachment_id=123,
            url='https://cdn.discord.test/evidence.png'):
        self.filename = filename
        self.content_type = content_type
        self.data = data
        self.size = len(data)
        self.id = attachment_id
        self.url = url

    async def read(self):
        return self.data


class FeedbackStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.profile = SimpleNamespace(
            environment='development',
            project_root=self.root,
            log_root=self.root / 'logs' / 'development',
        )
        self.store = beta_feedback.FeedbackStore(self.profile)

    def tearDown(self):
        self.store.executor.shutdown(wait=True)
        self.tempdir.cleanup()

    def draft(self, **overrides):
        values = dict(
            category='bug',
            summary='A concise issue',
            details='A detailed issue description.',
            context='/game show 42500',
            requester_id=100,
            requester_display_name='Tester @everyone',
            guild_id=200,
            channel_id=300,
            source='slash',
            git_checkpoint='checkpoint-abc',
        )
        values.update(overrides)
        return beta_feedback.build_report_draft(**values)

    def test_schema_checkpoint_safe_identity_and_restrictive_modes(self):
        draft = self.draft()
        with self.assertRaises(FrozenInstanceError):
            draft.summary = 'changed'
        self.assertNotIn('@everyone', draft.requester_display_name)

        report = asyncio.run(self.store.store(draft))
        expected_fields = {
            'schema_version', 'report_id', 'category', 'summary', 'details',
            'context', 'game_id', 'command_reference', 'requester_id',
            'requester_display_name', 'guild_id', 'channel_id', 'source',
            'timestamp_utc', 'git_checkpoint', 'attachments',
        }
        self.assertEqual(set(report.record), expected_fields)
        self.assertEqual(report.record['schema_version'], beta_feedback.SCHEMA_VERSION)
        self.assertEqual(report.record['git_checkpoint'], 'checkpoint-abc')
        self.assertRegex(report.report_id, r'^[A-Za-z0-9_-]{20,}$')
        paths = report.paths
        self.assertEqual(stat.S_IMODE(paths.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(paths.attachments_root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(paths.staging_root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(paths.record_file.stat().st_mode), 0o600)
        self.assertEqual(len(paths.record_file.read_bytes().splitlines()), 1)

    def test_attachment_limits_staging_digest_and_safe_fixed_path(self):
        attachments = asyncio.run(beta_feedback.capture_attachments((
            FakeAttachment(filename='../screenshots/evil.png'),
        )))
        report = asyncio.run(self.store.store(self.draft(attachments=attachments)))
        metadata = report.record['attachments'][0]
        self.assertEqual(metadata['storage_name'], 'attachment-01.png')
        self.assertEqual(metadata['filename'], '__screenshots_evil.png')
        self.assertEqual(metadata['sha256'], hashlib.sha256(b'PNG bytes').hexdigest())
        stored_path = report.paths.attachments_root / report.report_id / 'attachment-01.png'
        self.assertEqual(stored_path.read_bytes(), b'PNG bytes')
        self.assertEqual(stat.S_IMODE(stored_path.stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE((report.paths.attachments_root / report.report_id).stat().st_mode),
            0o700,
        )

        with self.assertRaises(beta_feedback.FeedbackValidationError):
            asyncio.run(beta_feedback.capture_attachments(tuple(
                FakeAttachment(filename=f'{index}.png')
                for index in range(beta_feedback.MAX_ATTACHMENTS + 1)
            )))
        with self.assertRaises(beta_feedback.FeedbackValidationError):
            asyncio.run(beta_feedback.capture_attachments((
                FakeAttachment(filename='payload.exe', content_type='application/x-msdownload'),
            )))
        with mock.patch.object(beta_feedback, 'MAX_ATTACHMENT_BYTES', 3):
            with self.assertRaises(beta_feedback.FeedbackValidationError):
                asyncio.run(beta_feedback.capture_attachments((FakeAttachment(),)))

    def test_attachment_stage_failure_and_record_failure_leave_no_acknowledged_partial(self):
        attachment = beta_feedback.AttachmentInput(
            attachment_id=1,
            filename='evidence.png',
            content_type='image/png',
            extension='.png',
            data=b'data',
        )
        with mock.patch.object(
                beta_feedback,
                '_write_attachment',
                side_effect=beta_feedback.FeedbackStorageError('stage failed')):
            with self.assertRaises(beta_feedback.FeedbackStorageError):
                asyncio.run(self.store.store(self.draft(attachments=(attachment,))))
        paths = beta_feedback.feedback_paths(self.profile, create=False)
        self.assertFalse(paths.record_file.exists())
        self.assertEqual(tuple(paths.staging_root.iterdir()), ())

        with mock.patch.object(
                beta_feedback,
                '_append_record_line',
                side_effect=beta_feedback.FeedbackStorageError(
                    'record failed', may_have_committed=False
                )):
            with self.assertRaises(beta_feedback.FeedbackStorageError):
                asyncio.run(self.store.store(self.draft(attachments=(attachment,))))
        self.assertFalse(paths.record_file.exists())
        self.assertEqual(
            tuple(path for path in paths.attachments_root.iterdir() if path.name != '.staging'),
            (),
        )

    def test_concurrent_appends_are_complete_and_event_loop_stays_responsive(self):
        async def append_many():
            reports = await asyncio.gather(*(
                self.store.store(self.draft(summary=f'Issue {index}'))
                for index in range(20)
            ))
            return reports

        reports = asyncio.run(append_many())
        self.assertEqual(len({report.report_id for report in reports}), 20)
        lines = beta_feedback.feedback_paths(self.profile).record_file.read_bytes().splitlines()
        self.assertEqual(len(lines), 20)
        self.assertTrue(all(json.loads(line)['schema_version'] == 1 for line in lines))

        original_append = self.store._append_sync

        def delayed_append(draft):
            time.sleep(0.05)
            return original_append(draft)

        self.store._append_sync = delayed_append

        async def heartbeat_and_store():
            ticks = 0
            task = asyncio.create_task(self.store.store(self.draft(summary='responsive')))
            while not task.done():
                await asyncio.sleep(0.005)
                ticks += 1
            await task
            return ticks

        self.assertGreaterEqual(asyncio.run(heartbeat_and_store()), 3)

    def test_cancellation_drains_worker_before_returning_cancellation(self):
        original_append = self.store._append_sync

        def delayed_append(draft):
            time.sleep(0.04)
            return original_append(draft)

        self.store._append_sync = delayed_append

        async def cancel_submission():
            task = asyncio.create_task(self.store.store(self.draft()))
            await asyncio.sleep(0.005)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(cancel_submission())
        result = beta_feedback.read_feedback_records(self.profile)
        self.assertTrue(result.present)
        self.assertEqual(len(result.records), 1)

    def test_development_gate_and_symlink_escape_are_closed(self):
        production = SimpleNamespace(
            environment='production',
            project_root=self.root,
            log_root=self.root / 'logs',
        )
        with self.assertRaises(beta_feedback.FeedbackRuntimeGateError):
            beta_feedback.feedback_paths(production, create=True)

        paths = beta_feedback.feedback_paths(self.profile, create=True)
        outside = self.root / 'outside'
        outside.mkdir()
        paths.staging_root.rmdir()
        paths.attachments_root.rmdir()
        paths.attachments_root.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(beta_feedback.FeedbackStorageError):
            beta_feedback.feedback_paths(self.profile, create=True)

    def test_reader_absent_malformed_truncated_and_search(self):
        absent = beta_feedback.read_feedback_records(self.profile)
        self.assertFalse(absent.present)
        self.assertEqual(absent.records, ())

        report = asyncio.run(self.store.store(self.draft(
            details='needle in the detailed description',
            context='related context',
            command_reference='$staffhelp',
        )))
        paths = report.paths
        with paths.record_file.open('ab') as stream:
            stream.write(b'{not-json\n')
            stream.write(json.dumps(dict(report.record)).encode('utf-8'))
        result = beta_feedback.read_feedback_records(self.profile)
        self.assertEqual(len(result.records), 1)
        self.assertEqual([issue.kind for issue in result.issues], ['malformed', 'truncated'])
        search = beta_feedback.read_feedback_records(self.profile, query='needle')
        self.assertEqual(len(search.records), 1)
        command_search = beta_feedback.read_feedback_records(
            self.profile,
            query='$staffhelp',
        )
        self.assertEqual(len(command_search.records), 1)


class FeedbackRelayAndModalTests(unittest.TestCase):
    def setUp(self):
        self.draft = beta_feedback.build_report_draft(
            category='feature',
            summary='A useful feature',
            details='SECRET REPORT DETAILS',
            context=None,
            requester_id=100,
            requester_display_name='Tester',
            guild_id=200,
            channel_id=300,
            source='slash',
            git_checkpoint='checkpoint-test',
        )

    def test_native_store_precedes_relay_and_relay_failure_preserves_record(self):
        report = SimpleNamespace(report_id='A' * 24)
        events = []

        async def store(_draft):
            events.append('store')
            return report

        async def relay(_channel, _report):
            events.append('relay')

        bot = object()
        with mock.patch.object(beta_feedback, 'store_report', side_effect=store), \
                mock.patch.object(beta_feedback, 'staff_help_channel', return_value=object()), \
                mock.patch.object(beta_feedback, 'relay_native', side_effect=relay):
            result = asyncio.run(beta_feedback.submit_native_report(bot, self.draft))
        self.assertTrue(result.relay_ok)
        self.assertEqual(events, ['store', 'relay'])

        async def failing_relay(_channel, _report):
            raise RuntimeError('discord unavailable')

        with mock.patch.object(beta_feedback, 'store_report', side_effect=store), \
                mock.patch.object(beta_feedback, 'staff_help_channel', return_value=object()), \
                mock.patch.object(beta_feedback, 'relay_native', side_effect=failing_relay):
            result = asyncio.run(beta_feedback.submit_native_report(bot, self.draft))
        self.assertFalse(result.relay_ok)
        self.assertIs(result.report, report)

    def test_native_staff_mirror_disables_mentions_and_logs_no_report_body(self):
        attachment = beta_feedback.AttachmentInput(
            attachment_id=1,
            filename='evidence.png',
            content_type='image/png',
            extension='.png',
            data=b'bytes',
        )
        record = MappingProxyType({
            'category': 'bug',
            'source': 'slash',
            'requester_display_name': 'Tester',
            'requester_id': 100,
            'guild_id': 200,
            'channel_id': 300,
            'summary': 'Summary',
            'game_id': None,
            'command_reference': None,
            'context': None,
            'details': 'SECRET REPORT DETAILS',
        })
        report = beta_feedback.StoredReport(
            report_id='C' * 24,
            record=record,
            attachments=(beta_feedback.StoredAttachment(
                attachment_id=1,
                filename='evidence.png',
                content_type='image/png',
                size=5,
                sha256=hashlib.sha256(b'bytes').hexdigest(),
                storage_name='attachment-01.png',
                data=b'bytes',
            ),),
            paths=SimpleNamespace(),
        )
        channel = SimpleNamespace(send=mock.AsyncMock())
        asyncio.run(beta_feedback.relay_native(channel, report))
        kwargs = channel.send.await_args.kwargs
        mentions = kwargs['allowed_mentions']
        self.assertFalse(mentions.everyone)
        self.assertFalse(mentions.users)
        self.assertFalse(mentions.roles)
        self.assertEqual(len(kwargs['files']), 1)
        self.assertIn('SECRET REPORT DETAILS', channel.send.await_args.args[0])

        async def failing_relay(_channel, _report):
            raise RuntimeError('relay unavailable')

        with mock.patch.object(beta_feedback, 'store_report', new=mock.AsyncMock(return_value=report)), \
                mock.patch.object(beta_feedback, 'staff_help_channel', return_value=channel), \
                mock.patch.object(beta_feedback, 'relay_native', side_effect=failing_relay), \
                self.assertLogs('polybot.modules.beta_feedback', level='WARNING') as logs:
            asyncio.run(beta_feedback.submit_native_report(object(), self.draft))
        self.assertNotIn('SECRET REPORT DETAILS', '\n'.join(logs.output))

    def test_modal_shape_and_requester_channel_lifecycle(self):
        modal = StaffHelpModal(object(), requester_id=100, guild_id=200, channel_id=300)
        self.assertEqual(len(modal.children), 5)
        category = modal.category.component.to_component_dict()
        self.assertEqual(
            [(option['label'], option['value']) for option in category['options']],
            [('Help', 'help'), ('Bug', 'bug'), ('Feature', 'feature')],
        )
        self.assertEqual(modal.details.component.max_length, beta_feedback.MAX_DETAILS_LENGTH)
        self.assertEqual(modal.files.component.max_values, beta_feedback.MAX_ATTACHMENTS)
        self.assertFalse(modal.files.component.required)

        class Response:
            def __init__(self):
                self.sent = []

            def is_done(self):
                return bool(self.sent)

            async def send_message(self, content, **kwargs):
                self.sent.append((content, kwargs))

            async def defer(self, **kwargs):
                self.sent.append(('defer', kwargs))

        class Followup:
            def __init__(self):
                self.sent = []

            async def send(self, content, **kwargs):
                self.sent.append((content, kwargs))

        unauthorized = SimpleNamespace(
            user=SimpleNamespace(id=999),
            guild_id=200,
            channel_id=300,
            response=Response(),
            followup=Followup(),
        )
        self.assertFalse(asyncio.run(modal.interaction_check(unauthorized)))
        self.assertIn('Only the member', unauthorized.response.sent[0][0])

        wrong_channel = SimpleNamespace(
            user=SimpleNamespace(id=100),
            guild_id=200,
            channel_id=301,
            response=Response(),
            followup=Followup(),
        )
        self.assertFalse(asyncio.run(modal.interaction_check(wrong_channel)))
        self.assertIn('original server channel', wrong_channel.response.sent[0][0])

    def test_modal_success_is_private_and_storage_failure_never_claims_saved(self):
        class Response:
            def __init__(self):
                self.sent = []

            def is_done(self):
                return bool(self.sent)

            async def defer(self, **kwargs):
                self.sent.append(('defer', kwargs))

        class Followup:
            def __init__(self):
                self.sent = []

            async def send(self, content, **kwargs):
                self.sent.append((content, kwargs))

        interaction = SimpleNamespace(
            user=SimpleNamespace(id=100, display_name='Tester'),
            guild_id=200,
            channel_id=300,
            response=Response(),
            followup=Followup(),
        )
        modal = StaffHelpModal(object(), requester_id=100, guild_id=200, channel_id=300)
        modal.category.component._value = 'bug'
        modal.summary.component._value = 'A bug'
        modal.details.component._value = 'Detailed bug description.'
        modal.context.component._value = ''
        modal.files.component._values = []

        with mock.patch.object(
                beta_feedback,
                'capture_attachments',
                new=mock.AsyncMock(return_value=())), \
                mock.patch.object(
                    beta_feedback,
                    'submit_native_report',
                    new=mock.AsyncMock(
                        side_effect=beta_feedback.FeedbackStorageError('disk full')
                    )):
            asyncio.run(modal.on_submit(interaction))
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertIn('could not be recorded', interaction.followup.sent[0][0])
        self.assertNotIn('was recorded', interaction.followup.sent[0][0])

        interaction = SimpleNamespace(
            user=SimpleNamespace(id=100, display_name='Tester'),
            guild_id=200,
            channel_id=300,
            response=Response(),
            followup=Followup(),
        )
        modal = StaffHelpModal(object(), requester_id=100, guild_id=200, channel_id=300)
        modal.category.component._value = 'feature'
        modal.summary.component._value = 'A feature'
        modal.details.component._value = 'Detailed feature description.'
        modal.context.component._value = ''
        modal.files.component._values = []
        submission = beta_feedback.NativeSubmission(
            report=SimpleNamespace(report_id='B' * 24),
            relay_ok=True,
        )
        with mock.patch.object(
                beta_feedback,
                'capture_attachments',
                new=mock.AsyncMock(return_value=())), \
                mock.patch.object(
                    beta_feedback,
                    'submit_native_report',
                    new=mock.AsyncMock(return_value=submission)):
            asyncio.run(modal.on_submit(interaction))
        self.assertIn('`' + ('B' * 24) + '`', interaction.followup.sent[0][0])
        self.assertTrue(interaction.followup.sent[0][1]['ephemeral'])


class PrefixStaffHelpCompatibilityTests(unittest.TestCase):
    def test_prefix_registration_preserves_alias_grammar_and_cooldown(self):
        command = misc_module.misc.staffhelp
        self.assertEqual(command.name, 'staffhelp')
        self.assertEqual(command.aliases, ['helpstaff'])
        self.assertIn('message', command.clean_params)
        self.assertFalse(command.clean_params['message'].required)
        self.assertEqual(command._buckets.type, commands.BucketType.user)
        self.assertEqual(command._buckets._cooldown.rate, 2)
        self.assertEqual(command._buckets._cooldown.per, 30.0)

    def test_prefix_relay_card_gamelog_and_json_routing_are_preserved(self):
        class FakeRole:
            mention = '<@&999>'

            def __init__(self):
                self.name = 'Helper'

        class FakeGuild:
            id = 200
            name = 'Test Guild'
            roles = [FakeRole()]

            def __init__(self, staff_channel):
                self.staff_channel = staff_channel

            def get_channel(self, channel_id):
                self.requested_channel_id = channel_id
                return self.staff_channel

        class FakeStaffChannel:
            id = 888
            name = 'staff-help'

        class FakeGame:
            id = 42500
            guild_id = 200
            name = 'Game Name'

            def embed(self, *, guild, prefix):
                return 'embed', 'card content'

        class FakeMessage:
            jump_url = 'https://discord.test/jump'
            attachments = [FakeAttachment()]

        class FakeCommand:
            def __init__(self):
                self.reset_count = 0

            def reset_cooldown(self, _ctx):
                self.reset_count += 1

        class FakeContext:
            prefix = '$'
            invoked_with = 'helpstaff'
            guild = FakeGuild(None)
            channel = SimpleNamespace(id=301, name='general')
            author = SimpleNamespace(
                id=100,
                name='Tester',
                display_name='Tester',
                mention='<@100>',
            )

            def __init__(self, guild, command):
                self.guild = guild
                self.channel.guild = guild
                self.message = FakeMessage()
                self.command = command
                self.sent = []

            async def send(self, content):
                self.sent.append(content)

        staff_channel = FakeStaffChannel()
        guild = FakeGuild(staff_channel)
        ctx = FakeContext(guild, FakeCommand())
        bot = SimpleNamespace(get_guild=lambda guild_id: guild)
        cog = misc_module.misc(bot)
        game = FakeGame()
        stored = SimpleNamespace(report_id='A' * 24)
        captured_drafts = []
        relayed = []

        async def fake_store(draft):
            captured_drafts.append(draft)
            return stored

        async def fake_relay(_channel, content):
            relayed.append(content)

        def guild_setting(_guild_id, setting_name):
            return {
                'staff_help_channel': 888,
                'helper_roles': ['Helper'],
            }[setting_name]

        with mock.patch.object(
                misc_module.models.Game,
                'by_channel_or_arg',
                return_value=game), \
                mock.patch.object(
                    misc_module.models.GameLog,
                    'member_string',
                    return_value='Tester (`100`)'), \
                mock.patch.object(
                    misc_module.models.GameLog,
                    'write') as gamelog_write, \
                mock.patch.object(
                    misc_module.settings,
                    'guild_setting',
                    side_effect=guild_setting), \
                mock.patch.object(
                    misc_module.beta_feedback,
                    'store_report',
                    side_effect=fake_store), \
                mock.patch.object(
                    misc_module.beta_feedback,
                    'relay_prefix',
                    side_effect=fake_relay), \
                mock.patch.object(
                    misc_module.image_storage,
                    'send_game_embed',
                    new=mock.AsyncMock()) as card:
            asyncio.run(misc_module.misc.staffhelp.callback(
                cog,
                ctx,
                message='Game 42500 was claimed incorrectly',
            ))

        self.assertEqual(len(captured_drafts), 1)
        self.assertEqual(captured_drafts[0].source, 'prefix')
        self.assertEqual(captured_drafts[0].category, 'help')
        self.assertEqual(captured_drafts[0].game_id, 42500)
        self.assertEqual(captured_drafts[0].command_reference, 'helpstaff')
        self.assertEqual(len(captured_drafts[0].attachments), 1)
        self.assertEqual(len(relayed), 1)
        self.assertIn('Attention <@&999>', relayed[0])
        self.assertIn('https://cdn.discord.test/evidence.png', relayed[0])
        card.assert_awaited_once()
        gamelog_write.assert_called_once_with(
            game_id=42500,
            guild_id=200,
            message=(
                'Tester (`100`) requested staffhelp: '
                '*Game 42500 was claimed incorrectly\n'
                'https://cdn.discord.test/evidence.png*'
            ),
        )
        self.assertTrue(
            ctx.sent[-1].startswith('Your message has been sent to server staff.')
        )


class FeedbackReaderCliTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.profile = SimpleNamespace(
            environment='development',
            project_root=root,
            log_root=root / 'logs' / 'development',
        )
        self.store = beta_feedback.FeedbackStore(self.profile)

    def tearDown(self):
        self.store.executor.shutdown(wait=True)
        self.tempdir.cleanup()

    def run_cli(self, args):
        stdout = StringIO()
        stderr = StringIO()
        with mock.patch.object(
                manage_beta_feedback,
                '_selected_profile',
                return_value=self.profile), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            status = manage_beta_feedback.main(args)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_list_show_search_and_machine_output_are_read_only(self):
        status, output, _ = self.run_cli(['--json', 'list'])
        self.assertEqual(status, 0)
        self.assertIn('"present": false', output)

        report = asyncio.run(self.store.store(
            beta_feedback.build_report_draft(
                category='help',
                summary='CLI summary',
                details='CLI searchable details',
                context='context value',
                requester_id=1,
                requester_display_name='CLI tester',
                guild_id=2,
                channel_id=3,
                source='slash',
                git_checkpoint='cli-checkpoint',
            )
        ))
        record_path = self.profile.log_root / 'beta-feedback' / 'reports.jsonl'
        record_before = record_path.read_bytes()
        status, output, _ = self.run_cli(['list', '--limit', '1'])
        self.assertEqual(status, 0)
        self.assertIn(report.report_id, output)
        status, output, _ = self.run_cli(['show', '--report-id', report.report_id])
        self.assertEqual(status, 0)
        self.assertIn('CLI searchable details', output)
        status, output, _ = self.run_cli(['--json', 'search', 'context value'])
        self.assertEqual(status, 0)
        self.assertIn(report.report_id, output)
        self.assertEqual(record_path.read_bytes(), record_before)
