"""Focused coverage for P8.14 promotion and trade roster cards."""

import asyncio
from contextlib import nullcontext
from dataclasses import FrozenInstanceError
from io import BytesIO
import inspect
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

import discord

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.league_roster_cards_workers')
service = import_offline_runtime('modules.league_roster_cards')
league = import_offline_runtime('modules.league')


def root():
    return next(
        command for command in league.league.__cog_app_commands__
        if command.name == 'league'
    )


def colour(value):
    return SimpleNamespace(value=value, __str__=lambda self: f'#{value:06x}')


def role(name='Ronin', value=0x123456):
    return SimpleNamespace(name=name, colour=colour(value))


def avatar(url):
    return SimpleNamespace(replace=lambda **_kwargs: url)


def member(member_id=10, *, roles=()):
    return SimpleNamespace(
        id=member_id,
        name=f'User {member_id}',
        display_name=f'User {member_id}',
        mention=f'<@{member_id}>',
        roles=tuple(roles),
        display_avatar=avatar(f'https://cdn.example/{member_id}.png'),
    )


def guild():
    return SimpleNamespace(id=300, roles=(role(),))


def request(**overrides):
    values = dict(
        guild_id=300,
        mode='trade',
        top_text='TRADE',
        bottom_text='ROSTER UPDATE',
        left=workers.ImageSource('url', 'https://cdn.example/left.png'),
        right=workers.ImageSource('url', 'https://cdn.example/right.png'),
        role_colours=(workers.RoleColourSnapshot('Ronin', '#123456'),),
    )
    values.update(overrides)
    return workers.RosterCardRequest(**values)


class RegistrationAndPermissionTests(unittest.TestCase):
    def test_native_group_has_typed_common_inputs_and_raw_url_overrides(self):
        roster = root().get_command('roster')
        self.assertEqual(
            {command.name for command in roster.commands},
            {'promote', 'trade', 'draft', 'price'},
        )
        promote = roster.get_command('promote')
        self.assertEqual(
            [(row.name, row.type, row.required) for row in promote.parameters],
            [
                ('player', discord.AppCommandOptionType.user, True),
                ('team', discord.AppCommandOptionType.string, True),
                ('top_text', discord.AppCommandOptionType.string, False),
                ('bottom_text', discord.AppCommandOptionType.string, False),
                ('player_image_url', discord.AppCommandOptionType.string, False),
                ('team_image_url', discord.AppCommandOptionType.string, False),
            ],
        )
        trade = roster.get_command('trade')
        self.assertEqual(
            [row.name for row in trade.parameters],
            [
                'left_player', 'right_player', 'top_text', 'bottom_text',
                'left_image_url', 'right_image_url',
            ],
        )

    def test_native_access_is_helper_or_above_and_strict_channel_aware(self):
        actor = member()
        with mock.patch.object(
            service.house_show, '_league_scope', return_value=True
        ), mock.patch.object(
            service.house_show, '_setting', return_value=None
        ), mock.patch.object(
            service.settings, 'is_staff', return_value=False
        ):
            self.assertIn('Only Helpers', service.access_error(actor, 300, 50))
        with mock.patch.object(
            service.house_show, '_league_scope', return_value=True
        ), mock.patch.object(
            service.house_show,
            '_setting',
            side_effect=lambda _guild, name, default=None: {
                'bot_channels_strict': [50], 'bot_channels': [60],
                'bot_channels_private': [70],
            }.get(name, default),
        ), mock.patch.object(service.settings, 'is_staff', return_value=True), mock.patch.object(
            service.settings, 'is_mod', return_value=False
        ):
            self.assertIsNone(service.access_error(actor, 300, 50))
            self.assertIsNone(service.access_error(actor, 300, 70))
            self.assertIn('designated bot spam', service.access_error(actor, 300, 60))

    def test_retained_prefix_promote_and_trade_are_staff_checked(self):
        command = next(row for row in league.league.__cog_commands__ if row.name == 'promote')
        self.assertIn('trade', command.aliases)
        staff_check = next(
            check for check in command.checks
            if not inspect.iscoroutinefunction(check)
        )
        ctx = SimpleNamespace(author=member())
        with mock.patch.object(service.settings, 'is_staff', return_value=False):
            self.assertFalse(staff_check(ctx))
        with mock.patch.object(service.settings, 'is_staff', return_value=True):
            self.assertTrue(staff_check(ctx))


class WorkerTests(unittest.TestCase):
    def test_request_and_result_are_frozen_primitive_boundaries(self):
        item = request()
        with self.assertRaises(FrozenInstanceError):
            item.guild_id = 1
        self.assertIsInstance(item.role_colours, tuple)

    def test_team_override_still_resolves_team_but_uses_raw_url(self):
        team = SimpleNamespace(id=7, name='Ronin', image_url='https://stored/team.png')
        rendered = SimpleNamespace(fp=BytesIO(b'png-data'), close=mock.Mock())
        team_source = workers.ImageSource(
            'team', 'ron', fallback_url='https://override.example/team.png'
        )
        item = request(mode='promote', right=team_source)
        with mock.patch.object(workers.models.db, 'connection_context', return_value=nullcontext()), mock.patch.object(
            workers.models.Team, 'get_by_name', return_value=[team]
        ) as lookup, mock.patch.object(
            workers.image_storage, 'resolve_image', return_value='https://stored/team.png'
        ), mock.patch.object(workers.imgen, 'arrow_card', return_value=rendered) as arrow:
            result = workers._render(item)
        self.assertEqual(result.image_bytes, b'png-data')
        self.assertEqual(lookup.call_args.kwargs['guild_id'], 300)
        self.assertEqual(arrow.call_args.args[3], 'https://override.example/team.png')
        self.assertEqual(arrow.call_args.args[4], (('u', '#00ff00'),))

    def test_raw_url_rejects_non_http_scheme(self):
        item = request(left=workers.ImageSource('url', 'file:///etc/passwd'))
        with self.assertRaises(workers.RosterCardValidationError):
            workers._render(item)

    def test_slow_render_keeps_event_loop_responsive(self):
        async def scenario():
            started = threading.Event()
            release = threading.Event()

            def slow(_request):
                started.set()
                release.wait(timeout=2)
                return workers.RosterCardResult('trade', b'png', 'trade-card.png')

            with mock.patch.object(workers, '_render', side_effect=slow):
                task = asyncio.create_task(workers.run_roster_card(request()))
                while not started.is_set():
                    await asyncio.sleep(0.001)
                heartbeats = 0
                for _ in range(3):
                    await asyncio.sleep(0.01)
                    heartbeats += 1
                release.set()
                # This headless runner can briefly suppress the executor
                # completion callback's self-pipe wake-up.
                await asyncio.sleep(0.05)
                result = await task
            return result, heartbeats

        result, heartbeats = asyncio.run(scenario())
        self.assertEqual(result.filename, 'trade-card.png')
        self.assertEqual(heartbeats, 3)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_prefix_raw_url_remains_an_unambiguous_advanced_source(self):
        ctx = SimpleNamespace()
        with mock.patch.object(service.utilities, 'get_guild_member') as lookup:
            source = await service.prefix_lookup_source(
                ctx, 'https://images.example/card.png'
            )
        self.assertEqual(source.kind, 'url')
        self.assertEqual(source.value, 'https://images.example/card.png')
        lookup.assert_not_called()

    async def test_native_success_defers_private_then_publishes_attributed_card(self):
        command = root().get_command('roster').get_command('trade')
        channel = SimpleNamespace(id=50, send=mock.AsyncMock(return_value='message'))
        interaction = SimpleNamespace(
            guild=guild(),
            channel=channel,
            user=member(),
            response=SimpleNamespace(defer=mock.AsyncMock(), send_message=mock.AsyncMock()),
            followup=SimpleNamespace(send=mock.AsyncMock()),
            delete_original_response=mock.AsyncMock(),
        )
        cog = league.league.__new__(league.league)
        output = workers.RosterCardResult('trade', b'png', 'trade-card.png')
        with mock.patch.object(service, 'access_error', return_value=None), mock.patch.object(
            service, 'run_roster_card', new=mock.AsyncMock(return_value=output)
        ):
            result = await command.callback(
                cog, interaction, member(11), member(12), None, None, None, None
            )
        self.assertEqual(result, output)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.delete_original_response.assert_awaited_once()
        channel.send.assert_awaited_once()
        self.assertIn('<@10>', channel.send.call_args.args[0])
        self.assertIsInstance(channel.send.call_args.kwargs['file'], discord.File)
