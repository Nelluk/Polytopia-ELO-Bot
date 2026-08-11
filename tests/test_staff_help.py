"""Offline coverage for environment-explicit ``/staffhelp`` delivery."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest import mock

from modules import beta_feedback
from modules import staff_help
from modules.beta_feedback_views import StaffHelpModal


DEVELOPMENT = SimpleNamespace(environment='development')
PRODUCTION = SimpleNamespace(environment='production')


def _draft(*, attachments=()):
    return beta_feedback.build_report_draft(
        category='help',
        summary='Please help with a game',
        details='The user supplied @everyone and <@&999> in these details.',
        context='/game show 42500',
        requester_id=100,
        requester_display_name='Reporter @everyone',
        guild_id=200,
        channel_id=300,
        source='slash',
        attachments=attachments,
        git_checkpoint='checkpoint-test',
    )


def _route_fakes():
    helper_role = SimpleNamespace(id=400, name='Helper', mention='<@&400>')
    channel = SimpleNamespace(id=500, send=mock.AsyncMock(
        return_value=SimpleNamespace(id=600),
    ))
    guild = SimpleNamespace(
        id=200,
        roles=(helper_role,),
        get_channel=lambda channel_id: channel if channel_id == 500 else None,
    )
    bot = SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == 200 else None)
    return bot, guild, channel, helper_role


def _setting(guild_id, name):
    if guild_id != 200:
        raise AssertionError(f'unexpected guild {guild_id}')
    return {
        'staff_help_channel': 500,
        'helper_roles': ['Helper', 'Other Helper'],
    }[name]


class StaffHelpBackendTests(unittest.TestCase):
    def test_runtime_selects_exactly_one_backend(self):
        stored = beta_feedback.NativeSubmission(
            report=SimpleNamespace(report_id='A' * 24),
            relay_ok=True,
        )
        with mock.patch.object(
                beta_feedback,
                'submit_native_report',
                new=mock.AsyncMock(return_value=stored)) as development_submit, \
                mock.patch.object(
                    staff_help,
                    'relay_production',
                    new=mock.AsyncMock(return_value=600)) as production_relay:
            development = asyncio.run(staff_help.submit(
                object(), _draft(), profile=DEVELOPMENT,
            ))
            self.assertTrue(development.stored)
            self.assertEqual(development.report_id, 'A' * 24)
            production_relay.assert_not_awaited()

            production = asyncio.run(staff_help.submit(
                object(), _draft(), profile=PRODUCTION,
            ))
            self.assertFalse(production.stored)
            self.assertTrue(production.delivered)
            self.assertEqual(production.relay_message_id, 600)
            self.assertEqual(development_submit.await_count, 1)
            production_relay.assert_awaited_once()

        with self.assertRaisesRegex(
                staff_help.StaffHelpConfigurationError,
                'explicit development or production'):
            asyncio.run(staff_help.submit(
                object(),
                _draft(),
                profile=SimpleNamespace(environment='staging'),
            ))

    def test_production_relay_uses_configured_channel_first_helper_and_one_send(self):
        bot, _guild, channel, helper_role = _route_fakes()
        attachment = beta_feedback.AttachmentInput(
            attachment_id=1,
            filename='evidence.png',
            content_type='image/png',
            extension='.png',
            data=b'PNG',
        )
        with mock.patch('settings.guild_setting', side_effect=_setting), \
                mock.patch.object(
                    beta_feedback,
                    'store_report',
                    new=mock.AsyncMock(),
                ) as store:
            message_id = asyncio.run(staff_help.relay_production(
                bot,
                _draft(attachments=(attachment,)),
            ))

        self.assertEqual(message_id, 600)
        store.assert_not_awaited()
        channel.send.assert_awaited_once()
        content = channel.send.await_args.args[0]
        kwargs = channel.send.await_args.kwargs
        self.assertIn(helper_role.mention, content)
        self.assertEqual(kwargs['allowed_mentions'].roles, (helper_role,))
        self.assertFalse(kwargs['allowed_mentions'].everyone)
        self.assertFalse(kwargs['allowed_mentions'].users)
        self.assertEqual(len(kwargs['files']), 1)
        embed = kwargs['embed']
        self.assertIn('@everyone', embed.description)
        self.assertIn('<@&999>', embed.description)
        self.assertIn('Please help with a game', embed.title)
        self.assertEqual(
            next(field.value for field in embed.fields if field.name == 'Source channel'),
            '<#300> (`300`)',
        )

    def test_missing_channel_or_first_helper_role_fails_closed(self):
        bot, guild, _channel, _helper_role = _route_fakes()
        cases = (
            ({'staff_help_channel': None, 'helper_roles': ['Helper']}, 'channel'),
            ({'staff_help_channel': 999, 'helper_roles': ['Helper']}, 'unavailable'),
            ({'staff_help_channel': 500, 'helper_roles': []}, 'helper role'),
            ({'staff_help_channel': 500, 'helper_roles': ['Missing']}, 'unavailable'),
        )
        for values, pattern in cases:
            with self.subTest(values=values), mock.patch(
                    'settings.guild_setting',
                    side_effect=lambda _guild_id, name: values[name],
            ), self.assertRaisesRegex(
                    staff_help.StaffHelpConfigurationError,
                    pattern):
                staff_help.resolve_production_route(bot, 200)

        unavailable_bot = SimpleNamespace(get_guild=lambda _guild_id: None)
        with self.assertRaisesRegex(
                staff_help.StaffHelpConfigurationError,
                'server is not available'):
            staff_help.resolve_production_route(unavailable_bot, 200)
        self.assertEqual(guild.id, 200)

        with mock.patch(
                'settings.guild_setting',
                side_effect=KeyError('SECRET setting detail'),
        ), self.assertRaisesRegex(
                staff_help.StaffHelpConfigurationError,
                'configuration is unavailable'):
            staff_help.resolve_production_route(bot, 200)

    def test_everyone_helper_role_fails_closed(self):
        bot, guild, _channel, _helper_role = _route_fakes()
        everyone = SimpleNamespace(
            id=200,
            name='@everyone',
            mention='<@&200>',
            is_default=lambda: True,
        )
        guild.roles = (everyone,)
        values = {
            'staff_help_channel': 500,
            'helper_roles': ['@everyone'],
        }
        with mock.patch(
                'settings.guild_setting',
                side_effect=lambda _guild_id, name: values[name],
        ), self.assertRaisesRegex(
                staff_help.StaffHelpConfigurationError,
                'everyone role'):
            staff_help.resolve_production_route(bot, 200)

    def test_production_send_failure_has_no_store_and_is_private_category(self):
        bot, _guild, channel, _helper_role = _route_fakes()
        channel.send.side_effect = RuntimeError('SECRET transport detail')
        with mock.patch('settings.guild_setting', side_effect=_setting), \
                mock.patch.object(
                    beta_feedback,
                    'store_report',
                    new=mock.AsyncMock(),
                ) as store, self.assertLogs(
                    'polybot.modules.staff_help',
                    level='WARNING',
                ) as logs, self.assertRaisesRegex(
                    staff_help.StaffHelpDeliveryError,
                    'could not be delivered'):
            asyncio.run(staff_help.submit(bot, _draft(), profile=PRODUCTION))

        store.assert_not_awaited()
        self.assertNotIn('SECRET transport detail', '\n'.join(logs.output))
        self.assertNotIn(_draft().details, '\n'.join(logs.output))

    def test_cancelled_production_relay_drains_single_send_before_release(self):
        bot, _guild, channel, _helper_role = _route_fakes()
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_send(*_args, **_kwargs):
            started.set()
            await release.wait()
            return SimpleNamespace(id=601)

        channel.send.side_effect = slow_send

        async def cancel_relay():
            with mock.patch('settings.guild_setting', side_effect=_setting):
                task = asyncio.create_task(staff_help.relay_production(bot, _draft()))
                await started.wait()
                task.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        with self.assertLogs('polybot.modules.staff_help', level='WARNING') as logs:
            asyncio.run(cancel_relay())
        self.assertEqual(channel.send.await_count, 1)
        self.assertIn('completed successfully', '\n'.join(logs.output))


class ProductionStaffHelpModalTests(unittest.TestCase):
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

    def _interaction(self):
        return SimpleNamespace(
            user=SimpleNamespace(id=100, display_name='Tester'),
            guild_id=200,
            channel_id=300,
            response=self.Response(),
            followup=self.Followup(),
        )

    def _modal(self):
        modal = StaffHelpModal(
            object(),
            requester_id=100,
            guild_id=200,
            channel_id=300,
            profile=PRODUCTION,
        )
        modal.category.component._value = 'help'
        modal.summary.component._value = 'A question'
        modal.details.component._value = 'Please help me.'
        modal.context.component._value = ''
        modal.files.component._values = []
        return modal

    def test_production_title_and_success_claim_only_discord_delivery(self):
        interaction = self._interaction()
        modal = self._modal()
        result = staff_help.StaffHelpSubmission(
            environment='production',
            delivered=True,
            stored=False,
            relay_message_id=600,
        )
        with mock.patch.object(
                beta_feedback,
                'capture_attachments',
                new=mock.AsyncMock(return_value=())), mock.patch.object(
                    staff_help,
                    'submit',
                    new=mock.AsyncMock(return_value=result)):
            asyncio.run(modal.on_submit(interaction))

        self.assertEqual(modal.title, 'Staff help')
        self.assertEqual(len(interaction.followup.sent), 1)
        content, kwargs = interaction.followup.sent[0]
        self.assertIn('sent to server staff', content)
        self.assertNotIn('recorded', content)
        self.assertTrue(kwargs['ephemeral'])

    def test_production_failure_never_claims_delivery_or_storage(self):
        interaction = self._interaction()
        modal = self._modal()
        with mock.patch.object(
                beta_feedback,
                'capture_attachments',
                new=mock.AsyncMock(return_value=())), mock.patch.object(
                    staff_help,
                    'submit',
                    new=mock.AsyncMock(
                        side_effect=staff_help.StaffHelpDeliveryError('failed')
                    )):
            asyncio.run(modal.on_submit(interaction))

        content, kwargs = interaction.followup.sent[0]
        self.assertIn('could not be sent', content)
        self.assertNotIn('recorded', content)
        self.assertNotIn('has been sent', content)
        self.assertTrue(kwargs['ephemeral'])


if __name__ == '__main__':
    unittest.main()
