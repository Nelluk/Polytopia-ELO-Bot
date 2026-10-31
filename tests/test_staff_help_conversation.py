"""Public staff-help conversations, private feedback, and partial delivery."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest import mock

from modules import beta_feedback, staff_help
from tests import test_staff_help
from tests.test_staff_help import _draft, _route_fakes, _setting, PRODUCTION
from tests.test_game_detail_workspace import snapshot


class StaffHelpConversationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot, self.guild, self.staff, self.role = _route_fakes()
        self.guild.name = 'Source'
        self.guild.get_member = lambda _id: None
        self.guild.get_role = lambda _id: None
        self.url = 'https://discord.com/channels/200/300/601'
        self.public = SimpleNamespace(send=mock.AsyncMock(
            return_value=SimpleNamespace(id=601, jump_url=self.url),
        ))
        self.guild.get_channel_or_thread = lambda channel_id: self.public if channel_id == 300 else None
        self.settings = self.enterContext(mock.patch('settings.guild_setting', side_effect=_setting))
        self.lookup = self.enterContext(mock.patch.object(
            staff_help.staff_help_workers, 'run_find_related_game', new=mock.AsyncMock(return_value=None),
        ))
        self.audit = self.enterContext(mock.patch.object(
            staff_help.staff_help_workers, 'run_record_request', new=mock.AsyncMock(),
        ))

    async def submit(self, draft=None):
        return await staff_help.submit(self.bot, draft or _draft(), profile=PRODUCTION)

    async def test_posts_request_and_attachments_then_links_staff_to_that_exact_post(self):
        order = []
        self.audit.side_effect = lambda **kwargs: order.append('audit')

        async def public_send(**kwargs):
            order.append('public')
            return SimpleNamespace(id=601, jump_url=self.url)

        async def staff_send(*args, **kwargs):
            order.append('staff')
            return SimpleNamespace(id=600)

        self.public.send.side_effect = public_send
        self.staff.send.side_effect = staff_send
        attachment = beta_feedback.AttachmentInput(
            attachment_id=1, filename='evidence.txt', content_type='text/plain',
            extension='.txt', data=b'Useful evidence',
        )
        draft = _draft(attachments=(attachment,))
        result = await self.submit(draft)
        self.assertEqual(order, ['audit', 'public', 'staff'])
        self.assertEqual(result.source_message_url, self.url)
        self.assertTrue(result.delivered)
        self.assertIsNone(result.warning)
        public = self.public.send.await_args.kwargs
        self.assertEqual(public['embed'].description, draft.details)
        self.assertIn('<@100>', public['content'])
        self.assertEqual(public['embed'].fields[0].value, draft.context)
        self.assertFalse(public['allowed_mentions'].everyone)
        self.assertFalse(public['allowed_mentions'].roles)
        self.assertFalse(public['allowed_mentions'].users)
        staff = self.staff.send.await_args.kwargs
        self.assertIn(self.url, next(f.value for f in staff['embed'].fields if f.name == 'Request'))
        self.assertEqual(staff['allowed_mentions'].roles, (self.role,))
        self.assertEqual(public['files'][0].filename, 'evidence.txt')
        self.assertEqual(staff['files'][0].filename, 'evidence.txt')
        self.assertIsNot(public['files'][0], staff['files'][0])
        self.audit.assert_awaited_once_with(
            guild_id=200, game_id=None, requester_id=100,
            requester_name=draft.requester_display_name, summary=draft.summary,
            details=draft.details, context=draft.context,
        )

    async def test_game_guild_gets_notification_while_request_stays_in_original_channel(self):
        target_role = SimpleNamespace(id=900, name='Target Helper', mention='<@&900>')
        target = SimpleNamespace(send=mock.AsyncMock(return_value=SimpleNamespace(id=801)))
        target_guild = SimpleNamespace(
            id=201, roles=(target_role,), get_channel=lambda channel_id: target if channel_id == 800 else None,
        )
        self.bot.get_guild = lambda guild_id: {200: self.guild, 201: target_guild}.get(guild_id)
        self.lookup.return_value = staff_help.staff_help_workers.RelatedGame(
            game_id=42500, guild_id=201, name='Cross Guild', status='Incomplete',
        )
        with mock.patch('settings.guild_setting', side_effect=lambda guild_id, name: {
                'staff_help_channel': 800, 'helper_roles': ['Target Helper'],
        }[name]), mock.patch('settings.resolve_configured_role', return_value=target_role):
            result = await self.submit()
        self.assertEqual(result.relay_message_id, 801)
        self.assertEqual(result.source_message_url, self.url)
        self.public.send.assert_awaited_once()
        self.staff.send.assert_not_awaited()
        self.assertEqual(self.audit.await_args.kwargs['guild_id'], 200)
        self.assertEqual(self.audit.await_args.kwargs['game_id'], 42500)
        self.assertIn(self.url, next(f.value for f in target.send.await_args.kwargs['embed'].fields if f.name == 'Request'))

    async def test_maximum_form_keeps_complete_public_text_and_valid_staff_embed(self):
        self.guild.name = 'G' * 100
        self.lookup.return_value = staff_help.staff_help_workers.RelatedGame(
            game_id=42500, guild_id=200, name='N' * 80, status='Incomplete',
        )
        draft = replace(_draft(), summary='S' * 160, details='D' * 4000,
                        context='C' * 1000, requester_display_name='R' * 100)
        result = await self.submit(draft)
        self.assertTrue(result.delivered)
        self.assertEqual(self.public.send.await_args.kwargs['embed'].description, draft.details)
        self.assertLessEqual(len(self.staff.send.await_args.kwargs['embed']), 6000)

    async def test_staff_failure_preserves_public_post_and_tells_user_not_to_repeat(self):
        self.staff.send.side_effect = RuntimeError('private transport detail')
        with self.assertLogs('polybot.modules.staff_help', level='WARNING'):
            result = await self.submit()
        self.assertFalse(result.delivered)
        self.assertEqual(result.source_message_url, self.url)
        self.assertIn('do not submit it again', result.warning)
        self.public.send.assert_awaited_once()
        self.audit.assert_awaited_once()

    async def test_public_failure_still_delivers_to_staff(self):
        self.public.send.side_effect = RuntimeError('no send permission')
        with self.assertLogs('polybot.modules.staff_help', level='WARNING'):
            result = await self.submit()
        self.assertTrue(result.delivered)
        self.assertIsNone(result.source_message_url)
        self.assertIn('could not post', result.warning)
        self.staff.send.assert_awaited_once()

    async def test_no_delivery_raises_without_claiming_success(self):
        self.public.send.side_effect = RuntimeError()
        self.staff.send.side_effect = RuntimeError()
        with self.assertLogs('polybot.modules.staff_help', level='WARNING'):
            with self.assertRaises(staff_help.StaffHelpDeliveryError):
                await self.submit()

    async def test_audit_failure_prevents_discord_effects(self):
        self.audit.side_effect = RuntimeError('database unavailable')
        with self.assertRaises(staff_help.StaffHelpDeliveryError):
            await self.submit()
        self.public.send.assert_not_awaited()
        self.staff.send.assert_not_awaited()

    async def test_related_game_has_full_separate_card_without_filesystem_work(self):
        self.lookup.return_value = staff_help.staff_help_workers.RelatedGame(
            game_id=7, guild_id=200, name='Test', status='Completed', snapshot=snapshot(),
        )
        with mock.patch.object(staff_help.game_detail_views, '_resolve_asset') as media:
            result = await self.submit()
        media.assert_not_called()
        self.assertTrue(result.delivered)
        self.assertEqual(self.staff.send.await_count, 2)
        card = self.staff.send.await_args.kwargs
        self.assertIn('Game 7', card['embed'].title)
        self.assertIn('WINNER: Alpha', card['embed'].title)
        self.assertIn('Season notes', card['content'])
        self.assertGreaterEqual(len(card['embed'].fields), 3)
        self.assertFalse(card['allowed_mentions'].roles)
        self.assertEqual(self.audit.await_args.kwargs['game_id'], 7)

    async def test_game_card_failure_does_not_change_successful_request_delivery(self):
        self.lookup.return_value = staff_help.staff_help_workers.RelatedGame(
            game_id=7, guild_id=200, name='Test', status='Completed', snapshot=snapshot(),
        )
        self.staff.send.side_effect = [SimpleNamespace(id=600), RuntimeError('card failed')]
        with self.assertLogs('polybot.modules.staff_help', level='WARNING'):
            result = await self.submit()
        self.assertTrue(result.delivered)
        self.assertEqual(result.relay_message_id, 600)

    async def test_cancelled_interaction_finishes_one_audit_post_and_notification(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def public_send(**kwargs):
            started.set()
            await release.wait()
            return SimpleNamespace(id=601, jump_url=self.url)

        self.public.send.side_effect = public_send
        task = asyncio.create_task(self.submit())
        await started.wait()
        task.cancel()
        release.set()
        with self.assertLogs('polybot.modules.staff_help', level='WARNING'):
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.audit.assert_awaited_once()
        self.public.send.assert_awaited_once()
        self.staff.send.assert_awaited_once()

    async def test_bug_and_feature_never_post_publicly_or_write_game_log(self):
        central = SimpleNamespace(send=mock.AsyncMock(return_value=SimpleNamespace(id=701)))
        profile = SimpleNamespace(environment='production', allowed_guild_ids=(200,),
            server_settings=SimpleNamespace(polyelo_feedback_route={'guild_id': 200, 'channel_id': 700}))
        original = self.guild.get_channel
        self.guild.get_channel = lambda channel_id: central if channel_id == 700 else original(channel_id)
        with mock.patch.object(beta_feedback, 'store_report', new=mock.AsyncMock(
                return_value=SimpleNamespace(report_id='A' * 24))) as store:
            for category in ('bug', 'feature'):
                result = await staff_help.submit(self.bot, replace(_draft(), category=category), profile=profile)
                self.assertTrue(result.stored)
                self.assertTrue(result.delivered)
            self.assertEqual(store.await_count, 2)
        self.public.send.assert_not_awaited()
        self.staff.send.assert_not_awaited()
        self.audit.assert_not_awaited()


class ConversationAcknowledgementTests(unittest.TestCase):
    def test_private_acknowledgement_links_request_and_explains_partial_delivery(self):
        helpers = test_staff_help.ProductionStaffHelpModalTests()
        for delivered in (True, False):
            with self.subTest(delivered=delivered):
                modal, interaction = helpers._modal(), helpers._interaction()
                self.assertIn('posted in this channel', modal.category.description)
                result = staff_help.StaffHelpSubmission(
                    environment='production', delivered=delivered, stored=False,
                    source_message_url='https://discord.com/channels/200/300/601',
                    warning=None if delivered else 'The staff notification failed. Do not submit it again.',
                )
                with mock.patch.object(staff_help, 'submit', new=mock.AsyncMock(return_value=result)):
                    asyncio.run(modal.on_submit(interaction))
                text, kwargs = interaction.followup.sent[0]
                self.assertIn(result.source_message_url, text)
                self.assertIn('replying there', text)
                self.assertNotIn('submit another report', text)
                self.assertTrue(kwargs['ephemeral'])
                if not delivered:
                    self.assertIn(result.warning, text)
                    self.assertNotIn('sent to server staff', text)
