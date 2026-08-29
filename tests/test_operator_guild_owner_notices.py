"""Focused coverage for the one-time guild-owner update workflow."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest import mock

import discord

from modules import guild_configuration_schema as schema
from modules import guild_configuration_storage as storage
from modules import administration
from modules import operator_guild_console_views
from modules import operator_guild_owner_notice_views as views
from modules import operator_guild_owner_notices as notices
from tests import test_guild_configuration_storage as fixtures


OWNER_ID = 900000000000000123


def profile():
    return SimpleNamespace(
        environment=storage.DEVELOPMENT_ENVIRONMENT,
        database_name=storage.DEVELOPMENT_DATABASE,
        database_user=storage.DEVELOPMENT_ROLE,
        expected_bot_id=storage.DEVELOPMENT_BETA_APPLICATION_ID,
        background_tasks_enabled=False,
        api_enabled=False,
        bullet_enabled=False,
    )


def record(document=None, *, revision=3, generation=4):
    document = document or fixtures.bundle().imports[0].document
    return SimpleNamespace(
        guild_id=document.guild_id,
        revision=revision,
        generation=generation,
        document_digest=schema.document_digest(document),
        document=document,
    )


def owner(
    guild_id=fixtures.GUILD_ID,
    *,
    owner_id=OWNER_ID,
    name='Owner',
    guild_name='Owner Test Guild',
):
    return notices.GuildOwnerIdentity(
        guild_id=guild_id,
        guild_name=guild_name,
        owner_id=owner_id,
        owner_name=name,
    )


def plan(*, snapshot=None):
    return notices.build_plan(
        profile=profile(),
        runtime_records=(record(),),
        discord_snapshot=snapshot or fixtures.snapshot(),
        owners=(owner(),),
    )


class PlanningTests(unittest.TestCase):
    def test_clean_plan_renders_bounded_plain_language_notice(self):
        value = plan()
        self.assertEqual(value.guild_count, 1)
        self.assertEqual(value.recipient_count, 1)
        self.assertEqual(value.issue_guild_count, 0)
        self.assertEqual(len(value.plan_digest), 64)
        message = value.notices[0].messages[0]
        self.assertIn('Slash commands are now the preferred', message)
        self.assertIn(
            "Ping Nelluk if you notice a slash command that doesn't seem right.",
            message,
        )
        self.assertIn('/guild settings', message)
        self.assertIn('exact role name or role ID', message)
        self.assertIn('No configuration problems were detected', message)
        self.assertIn('does not process replies to DMs', message)
        self.assertLessEqual(len(message), notices.MAX_MESSAGE_CHARACTERS)

    def test_drift_is_split_between_owner_and_nelluk_actions(self):
        snapshot = fixtures.snapshot()
        guild = snapshot['guilds'][0]
        guild['roles'] = [
            value for value in guild['roles'] if value['id'] != 201
        ]
        guild['channels'] = [
            value for value in guild['channels'] if value['id'] != 300
        ]
        value = plan(snapshot=snapshot)
        issues = value.notices[0].guilds[0].issues
        by_field = {issue.field_key: issue for issue in issues}
        self.assertFalse(by_field['helper_roles'].owner_editable)
        self.assertTrue(by_field['bot_channels'].owner_editable)
        message = '\n'.join(value.notices[0].messages)
        self.assertIn('review these items in `/guild settings`', message)
        self.assertIn('ask Nelluk to correct these protected settings', message)
        self.assertIn('Helper roles', message)
        self.assertIn('Bot channels', message)

    def test_one_owner_receives_one_grouped_sequence_for_multiple_guilds(self):
        second_id = fixtures.GUILD_ID + 1
        mapping = schema.document_to_mapping(fixtures.bundle().imports[0].document)
        mapping['guild_id'] = second_id
        mapping['identity']['display_name'] = 'Second Guild'
        for key in (
            'user_role_ids_level_1', 'user_role_ids_level_2',
            'user_role_ids_level_3', 'user_role_ids_level_4',
        ):
            mapping['permissions'][key] = [
                second_id if value == fixtures.GUILD_ID else value
                for value in mapping['permissions'][key]
            ]
        second_document = schema.validate_document(mapping)
        snapshot = fixtures.snapshot()
        second_snapshot = copy.deepcopy(snapshot['guilds'][0])
        second_snapshot['guild_id'] = second_id
        second_snapshot['guild_name'] = 'Second Guild'
        second_snapshot['roles'][0]['id'] = second_id
        snapshot['guilds'].append(second_snapshot)
        value = notices.build_plan(
            profile=profile(),
            runtime_records=(record(), record(second_document)),
            discord_snapshot=snapshot,
            owners=(owner(), owner(second_id, guild_name='Second Guild')),
        )
        self.assertEqual(value.guild_count, 2)
        self.assertEqual(value.recipient_count, 1)
        rendered = '\n'.join(value.notices[0].messages)
        self.assertIn('Owner Test Guild', rendered)
        self.assertIn('Second Guild', rendered)

    def test_owner_or_runtime_change_invalidates_exact_plan(self):
        original = plan()
        changed = notices.build_plan(
            profile=profile(),
            runtime_records=(record(generation=5),),
            discord_snapshot=fixtures.snapshot(),
            owners=(owner(),),
        )
        changed_owner = notices.build_plan(
            profile=profile(),
            runtime_records=(record(),),
            discord_snapshot=fixtures.snapshot(),
            owners=(owner(owner_id=OWNER_ID + 1),),
        )
        self.assertNotEqual(original.plan_digest, changed.plan_digest)
        self.assertNotEqual(original.plan_digest, changed_owner.plan_digest)


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_possible_send_with_receipt_failure_warns_against_retry(self):
        value = plan()
        user = SimpleNamespace(send=mock.AsyncMock(
            return_value=SimpleNamespace(id=700),
        ))

        async def resolve(_owner_id):
            return user

        with mock.patch.object(
                notices, 'run_receipt_io', new=mock.AsyncMock(side_effect=[
                    {
                        'schema_version': 1,
                        'campaign_id': notices.CAMPAIGN_ID,
                        'deliveries': {},
                    },
                    OSError('disk unavailable'),
                ]),
        ):
            result = await notices.deliver_plan(
                value,
                resolve_user=resolve,
                receipts_path=Path('/tmp/not-used-owner-receipts.json'),
            )
        self.assertEqual(result.failed_count, 1)
        self.assertIn('may have been sent', result.statuses[0].detail)
        self.assertIn('Do not retry', result.statuses[0].detail)

    async def test_cancelled_receipt_io_drains_owned_worker(self):
        started = threading.Event()
        release = threading.Event()

        def slow():
            started.set()
            release.wait(timeout=2)
            return 'done'

        task = asyncio.create_task(notices.run_receipt_io(slow))
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.001)
        self.assertTrue(started.is_set())
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_delivery_is_single_flight_across_operator_workspaces(self):
        value = plan()
        started = asyncio.Event()
        release = asyncio.Event()

        class User:
            async def send(self, *_args, **_kwargs):
                started.set()
                await release.wait()
                return SimpleNamespace(id=700)

        async def resolve(_owner_id):
            return User()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'receipts.json'
            first = asyncio.create_task(notices.deliver_plan(
                value, resolve_user=resolve, receipts_path=path,
            ))
            await started.wait()
            with self.assertRaisesRegex(
                    notices.GuildOwnerNoticeError, 'already running'):
                await notices.deliver_plan(
                    value, resolve_user=resolve, receipts_path=path,
                )
            release.set()
            result = await first
        self.assertEqual(result.sent_count, 1)

    async def test_delivery_records_success_and_refuses_duplicate_campaign_send(self):
        value = plan()

        class User:
            def __init__(self):
                self.send = mock.AsyncMock(
                    side_effect=[SimpleNamespace(id=700 + index)
                                 for index in range(value.message_count)]
                )

        user = User()

        async def resolve(owner_id):
            self.assertEqual(owner_id, OWNER_ID)
            return user

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'receipts.json'
            first = await notices.deliver_plan(
                value, resolve_user=resolve, receipts_path=path,
            )
            second = await notices.deliver_plan(
                value, resolve_user=resolve, receipts_path=path,
            )
            receipt_text = path.read_text(encoding='utf-8')
        self.assertEqual(first.sent_count, 1)
        self.assertEqual(second.skipped_count, 1)
        self.assertEqual(user.send.await_count, value.message_count)
        self.assertNotIn(value.notices[0].messages[0], receipt_text)
        self.assertIn(str(OWNER_ID), receipt_text)

    async def test_failed_dm_has_no_public_fallback_or_success_receipt(self):
        value = plan()
        user = SimpleNamespace(send=mock.AsyncMock(
            side_effect=discord.Forbidden(
                SimpleNamespace(status=403, reason='Forbidden'),
                {'message': 'no'},
            )
        ))

        async def resolve(_owner_id):
            return user

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'receipts.json'
            result = await notices.deliver_plan(
                value, resolve_user=resolve, receipts_path=path,
            )
            self.assertFalse(path.exists())
        self.assertEqual(result.failed_count, 1)

    def test_corrupt_receipt_is_rejected_before_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'receipts.json'
            path.write_text('{"schema_version": 1}', encoding='utf-8')
            with self.assertRaisesRegex(
                    notices.GuildOwnerNoticeError, 'invalid'):
                notices.load_receipts(path)


class WorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_requires_two_click_send_and_test_targets_operator_only(self):
        value = plan()
        test_runner = mock.AsyncMock()
        delivery_result = notices.OwnerNoticeDeliveryResult((
            notices.OwnerDeliveryStatus(OWNER_ID, 'sent', 'ok'),
        ))
        delivery_runner = mock.AsyncMock(return_value=delivery_result)
        back_runner = mock.AsyncMock()
        workspace = views.GuildOwnerNoticeWorkspace(
            requester_id=OWNER_ID,
            plan=value,
            completed_owner_ids=(),
            test_runner=test_runner,
            delivery_runner=delivery_runner,
            back_runner=back_runner,
        )
        buttons = {
            item.label: item for item in workspace.walk_children()
            if isinstance(item, discord.ui.Button) and item.label
        }
        self.assertIn('DM this preview to me', buttons)
        self.assertIn('Review sending', buttons)
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=OWNER_ID),
            guild_id=fixtures.GUILD_ID,
            response=SimpleNamespace(
                send_message=mock.AsyncMock(),
                edit_message=mock.AsyncMock(),
                defer=mock.AsyncMock(),
            ),
            edit_original_response=mock.AsyncMock(),
        )
        await workspace._send_all(interaction)
        delivery_runner.assert_not_awaited()
        self.assertTrue(workspace.armed)
        await workspace._send_all(interaction)
        delivery_runner.assert_awaited_once_with(interaction, value)
        self.assertEqual(workspace.pending_owner_count, 0)

        workspace.completed_owner_ids.clear()
        workspace.rebuild()
        await workspace._send_test(interaction)
        test_runner.assert_awaited_once_with(
            interaction, value.notices[0].messages[0],
        )


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = administration.administration.__new__(
            administration.administration
        )
        self.cog.bot = SimpleNamespace(guilds=())

    async def test_plan_blocks_a_database_runtime_mismatch(self):
        runtime = record()
        stored = administration.operator_guild_configuration_workers.GuildConfigurationRecord(
            guild_id=runtime.guild_id,
            storage_schema_version=storage.STORAGE_SCHEMA_VERSION,
            enrollment_state='active',
            active_revision=runtime.revision,
            generation=runtime.generation + 1,
            updated_at='2026-08-29T12:00:00+00:00',
            document_digest=runtime.document_digest,
            source_digest='a' * 64,
            document=runtime.document,
        )
        registry = administration.operator_guild_configuration_workers.GuildConfigurationReadResult(
            operation=administration.operator_guild_configuration_workers.LIST,
            guild_id=runtime.guild_id,
            records=(stored,),
        )
        interaction = SimpleNamespace(
            guild_id=runtime.guild_id,
            user=SimpleNamespace(id=OWNER_ID),
        )
        with mock.patch.object(
                administration.settings, 'database_guild_ids',
                return_value=(runtime.guild_id,),
        ), mock.patch.object(
                administration.settings, 'database_guild_configuration',
                return_value=runtime,
        ), mock.patch.object(
                administration.operator_guild_configuration_service,
                'build_request', return_value=mock.sentinel.request,
        ), mock.patch.object(
                administration.operator_guild_configuration_workers,
                'run_read', new=mock.AsyncMock(return_value=registry),
        ):
            with self.assertRaisesRegex(
                    notices.GuildOwnerNoticeError, 'immutable'):
                await self.cog._operator_guild_owner_notice_plan(interaction)

    async def test_open_is_preview_only_and_loads_receipts_off_loop(self):
        value = plan()
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=OWNER_ID, send=mock.AsyncMock()),
            response=SimpleNamespace(
                defer=mock.AsyncMock(), send_message=mock.AsyncMock(),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        self.cog._operator_guild_owner_notice_plan = mock.AsyncMock(
            return_value=value,
        )
        with mock.patch.object(
                administration.settings, 'owner_id', OWNER_ID,
        ), mock.patch.object(
                notices, 'run_receipt_io',
                new=mock.AsyncMock(return_value={
                    'schema_version': 1,
                    'campaign_id': notices.CAMPAIGN_ID,
                    'deliveries': {},
                }),
        ) as receipt_io, mock.patch.object(
                operator_guild_console_views, 'replace_private',
                new=mock.AsyncMock(),
        ) as replace_private:
            await self.cog._operator_guild_owner_notices_open(interaction)
        interaction.response.defer.assert_awaited_once_with(
            ephemeral=True, thinking=True,
        )
        receipt_io.assert_awaited_once()
        workspace = replace_private.await_args.args[1]
        self.assertIsInstance(workspace, views.GuildOwnerNoticeWorkspace)
        interaction.user.send.assert_not_awaited()

    async def test_delivery_rejects_a_changed_live_plan_before_any_dm(self):
        expected = plan()
        current = replace(expected, plan_digest='f' * 64)
        self.cog._operator_guild_owner_notice_plan = mock.AsyncMock(
            return_value=current,
        )
        self.cog.bot = SimpleNamespace(
            get_user=mock.Mock(), fetch_user=mock.AsyncMock(),
        )
        interaction = SimpleNamespace(user=SimpleNamespace(id=OWNER_ID))
        with mock.patch.object(
                administration.settings, 'owner_id', OWNER_ID,
        ), mock.patch.object(
                notices, 'deliver_plan', new=mock.AsyncMock(),
        ) as deliver:
            with self.assertRaisesRegex(
                    notices.GuildOwnerNoticeError, 'changed'):
                await self.cog._operator_guild_owner_notice_deliver(
                    interaction, expected,
                )
        deliver.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
