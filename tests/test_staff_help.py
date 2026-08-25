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


def _production_profile(*, route=None, allowed_guild_ids=(200,)):
    return SimpleNamespace(
        environment='production',
        allowed_guild_ids=tuple(allowed_guild_ids),
        server_settings=SimpleNamespace(
            polyelo_feedback_route=(
                {'guild_id': 200, 'channel_id': 700}
                if route is None else route
            ),
        ),
    )


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
                    new=mock.AsyncMock(
                        return_value=(600, 'server_staff')
                    )) as production_relay:
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
            self.assertEqual(production.destination, 'server_staff')
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
                    staff_help.staff_help_workers,
                    'run_find_related_game',
                    new=mock.AsyncMock(return_value=None),
                ), \
                mock.patch.object(
                    beta_feedback,
                    'store_report',
                    new=mock.AsyncMock(),
                ) as store:
            message_id, destination = asyncio.run(staff_help.relay_production(
                bot,
                _draft(attachments=(attachment,)),
            ))

        self.assertEqual(message_id, 600)
        self.assertEqual(destination, 'server_staff')
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
            '<#300> (`300`) — '
            '[Open source channel](https://discord.com/channels/200/300)',
        )
        self.assertNotIn('Related message', {field.name for field in embed.fields})

    def test_production_relay_highlights_discord_message_link_from_context(self):
        bot, _guild, channel, _helper_role = _route_fakes()
        message_url = (
            'https://discord.com/channels/'
            '447883341463814144/448317497473630229/1409623401123456789'
        )
        draft = beta_feedback.build_report_draft(
            category='help',
            summary='Please review this message',
            details='The linked conversation explains the issue.',
            context=f'This is the relevant post: {message_url}',
            requester_id=100,
            requester_display_name='Reporter',
            guild_id=200,
            channel_id=300,
            source='slash',
        )
        with mock.patch('settings.guild_setting', side_effect=_setting), \
                mock.patch.object(
                    staff_help.staff_help_workers,
                    'run_find_related_game',
                    new=mock.AsyncMock(return_value=None),
                ):
            asyncio.run(staff_help.relay_production(bot, draft))

        fields = {
            field.name: field.value
            for field in channel.send.await_args.kwargs['embed'].fields
        }
        self.assertEqual(
            fields['Related message'],
            f'[Open related message]({message_url})',
        )
        self.assertIn(message_url, fields['Related context'])

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

    def test_central_route_is_exact_allowlisted_bot_level_configuration(self):
        bot, guild, _channel, _helper_role = _route_fakes()
        central = SimpleNamespace(id=700, send=mock.AsyncMock())
        original_get_channel = guild.get_channel
        guild.get_channel = lambda channel_id: (
            central if channel_id == 700 else original_get_channel(channel_id)
        )
        route = staff_help.resolve_polyelo_feedback_route(
            bot,
            profile=_production_profile(),
        )
        self.assertEqual((route.guild_id, route.channel_id), (200, 700))
        self.assertIs(route.channel, central)

        for profile, pattern in (
            (_production_profile(route={}), 'not configured'),
            (_production_profile(
                route={'guild_id': 200, 'channel_id': 700},
                allowed_guild_ids=(201,),
            ), 'not allowlisted'),
            (_production_profile(
                route={'guild_id': True, 'channel_id': 700},
            ), 'guild_id is invalid'),
        ):
            with self.subTest(pattern=pattern), self.assertRaisesRegex(
                    staff_help.StaffHelpConfigurationError, pattern):
                staff_help.resolve_polyelo_feedback_route(bot, profile=profile)

    def test_preflight_allows_either_route_but_refuses_when_both_are_missing(self):
        bot, _guild, _channel, _helper_role = _route_fakes()
        with mock.patch.object(
                staff_help,
                'resolve_production_route',
                side_effect=staff_help.StaffHelpConfigurationError('local')), \
                mock.patch.object(
                    staff_help,
                    'resolve_polyelo_feedback_route',
                    return_value=SimpleNamespace(channel_id=700),
                ):
            self.assertIsNone(staff_help.availability_error(
                bot,
                200,
                profile=_production_profile(),
            ))

        with mock.patch.object(
                staff_help,
                'resolve_production_route',
                side_effect=staff_help.StaffHelpConfigurationError('local')), \
                mock.patch.object(
                    staff_help,
                    'resolve_polyelo_feedback_route',
                    side_effect=staff_help.StaffHelpConfigurationError('central'),
                ):
            self.assertIn('not configured', staff_help.availability_error(
                bot,
                200,
                profile=_production_profile(),
            ))

    def test_production_send_failure_has_no_store_and_is_private_category(self):
        bot, _guild, channel, _helper_role = _route_fakes()
        channel.send.side_effect = RuntimeError('SECRET transport detail')
        with mock.patch('settings.guild_setting', side_effect=_setting), \
                mock.patch.object(
                    staff_help.staff_help_workers,
                    'run_find_related_game',
                    new=mock.AsyncMock(return_value=None),
                ), \
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
            with mock.patch('settings.guild_setting', side_effect=_setting), \
                    mock.patch.object(
                        staff_help.staff_help_workers,
                        'run_find_related_game',
                        new=mock.AsyncMock(return_value=None),
                    ):
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

    def test_product_feedback_uses_only_central_route_without_mentions_or_store(self):
        source_bot, source_guild, local_channel, helper_role = _route_fakes()
        central_channel = SimpleNamespace(
            id=700,
            send=mock.AsyncMock(return_value=SimpleNamespace(id=701)),
        )
        source_guild.name = 'Source Guild'
        original_get_channel = source_guild.get_channel
        source_guild.get_channel = lambda channel_id: (
            central_channel if channel_id == 700 else original_get_channel(channel_id)
        )
        profile = _production_profile()
        draft = beta_feedback.build_report_draft(
            category='bug',
            summary='A broken command',
            details='Expected a card but received an error.',
            context='/game show 42500',
            requester_id=100,
            requester_display_name='Tester',
            guild_id=200,
            channel_id=300,
            source='slash',
            game_id=42500,
            command_reference='/game',
            git_checkpoint='abcdef1',
        )
        related = staff_help.staff_help_workers.RelatedGame(
            game_id=42500,
            guild_id=200,
            name='Test Game',
            status='Incomplete',
        )
        with mock.patch.object(
                staff_help.staff_help_workers,
                'run_find_related_game',
                new=mock.AsyncMock(return_value=related)), mock.patch.object(
                    beta_feedback,
                    'store_report',
                    new=mock.AsyncMock(),
                ) as store:
            message_id, destination = asyncio.run(staff_help.relay_production(
                source_bot,
                draft,
                profile=profile,
            ))

        self.assertEqual((message_id, destination), (701, 'polyelo_bug'))
        store.assert_not_awaited()
        central_channel.send.assert_awaited_once()
        local_channel.send.assert_not_awaited()
        kwargs = central_channel.send.await_args.kwargs
        self.assertFalse(kwargs['allowed_mentions'].everyone)
        self.assertFalse(kwargs['allowed_mentions'].roles)
        self.assertIn('PolyELO bug report', kwargs['embed'].title)
        fields = {field.name: field.value for field in kwargs['embed'].fields}
        self.assertEqual(fields['Source server'], 'Source Guild (`200`)')
        self.assertEqual(
            fields['Source channel'],
            '<#300> (`300`) — '
            '[Open source channel](https://discord.com/channels/200/300)',
        )
        self.assertIn('42500', fields['Related game'])
        self.assertEqual(fields['Bot checkpoint'], '`abcdef1`')
        self.assertNotIn(helper_role.mention, central_channel.send.await_args.args[0])

    def test_local_help_routes_to_related_game_guild_using_runtime_settings(self):
        source_channel = SimpleNamespace(id=500, send=mock.AsyncMock())
        target_channel = SimpleNamespace(
            id=800,
            send=mock.AsyncMock(return_value=SimpleNamespace(id=801)),
        )
        source_role = SimpleNamespace(id=400, name='Helper', mention='<@&400>')
        target_role = SimpleNamespace(
            id=900, name='Target Helper', mention='<@&900>'
        )
        source_guild = SimpleNamespace(
            id=200,
            name='Source',
            roles=(source_role,),
            get_channel=lambda channel_id: source_channel if channel_id == 500 else None,
        )
        target_guild = SimpleNamespace(
            id=201,
            name='Target',
            roles=(target_role,),
            get_channel=lambda channel_id: target_channel if channel_id == 800 else None,
        )
        bot = SimpleNamespace(get_guild=lambda guild_id: {
            200: source_guild,
            201: target_guild,
        }.get(guild_id))
        related = staff_help.staff_help_workers.RelatedGame(
            game_id=42500,
            guild_id=201,
            name='Cross Guild Game',
            status='Incomplete',
        )

        def setting(guild_id, name):
            self.assertEqual(guild_id, 201)
            return {
                'staff_help_channel': 800,
                'helper_roles': ['Target Helper'],
            }[name]

        with mock.patch.object(
                staff_help.staff_help_workers,
                'run_find_related_game',
                new=mock.AsyncMock(return_value=related)), mock.patch(
                    'settings.guild_setting', side_effect=setting
                ), mock.patch(
                    'settings.resolve_configured_role', return_value=target_role
                ):
            message_id, destination = asyncio.run(staff_help.relay_production(
                bot,
                _draft(),
            ))

        self.assertEqual((message_id, destination), (801, 'server_staff'))
        target_channel.send.assert_awaited_once()
        self.assertIn(target_role.mention, target_channel.send.await_args.args[0])
        source_channel.send.assert_not_awaited()


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
            destination='server_staff',
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

        self.assertEqual(modal.title, 'Staff help / PolyELO feedback')
        self.assertEqual(len(interaction.followup.sent), 1)
        content, kwargs = interaction.followup.sent[0]
        self.assertIn('sent to server staff', content)
        self.assertNotIn('recorded', content)
        self.assertTrue(kwargs['ephemeral'])

    def test_production_product_acknowledgement_names_maintainers(self):
        interaction = self._interaction()
        modal = self._modal()
        modal.category.component._value = 'bug'
        result = staff_help.StaffHelpSubmission(
            environment='production',
            delivered=True,
            stored=False,
            destination='polyelo_bug',
            relay_message_id=700,
        )
        with mock.patch.object(
                beta_feedback,
                'capture_attachments',
                new=mock.AsyncMock(return_value=())), mock.patch.object(
                    staff_help,
                    'submit',
                    new=mock.AsyncMock(return_value=result)):
            asyncio.run(modal.on_submit(interaction))

        content, kwargs = interaction.followup.sent[0]
        self.assertIn('PolyELO maintainers', content)
        self.assertNotIn('server staff', content)
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

    def test_product_delivery_failure_names_maintainer_destination(self):
        interaction = self._interaction()
        modal = self._modal()
        modal.category.component._value = 'feature'
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
        self.assertIn('PolyELO maintainers', content)
        self.assertNotIn('has been sent', content)
        self.assertTrue(kwargs['ephemeral'])


if __name__ == '__main__':
    unittest.main()
