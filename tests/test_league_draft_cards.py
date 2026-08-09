"""Focused coverage for P8.15 native league draft cards."""

import asyncio
from contextlib import nullcontext
from dataclasses import FrozenInstanceError
from io import BytesIO
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

import discord

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.league_draft_cards_workers')
service = import_offline_runtime('modules.league_draft_cards')
league = import_offline_runtime('modules.league')


def root():
    return next(
        command for command in league.league.__cog_app_commands__
        if command.name == 'league'
    )


def colour(value=0x123456):
    return SimpleNamespace(value=value, __str__=lambda self: f'#{value:06x}')


def role(name='Ronin', value=0x123456):
    return SimpleNamespace(name=name, colour=colour(value), color=colour(value))


def avatar(url):
    return SimpleNamespace(replace=lambda **_kwargs: url)


def member(member_id=10, *, roles=()):
    return SimpleNamespace(
        id=member_id,
        name=f'User{member_id}',
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
        player_discord_id=11,
        player_name='Draftee',
        player_avatar_url='https://cdn.example/11.png',
        team_name='Ronin',
        role_colours=(
            workers.RoleColourSnapshot('Ronin', '#123456'),
            workers.RoleColourSnapshot('Lightning', '#654321'),
        ),
    )
    values.update(overrides)
    return workers.DraftCardRequest(**values)


class RegistrationAndPermissionTests(unittest.TestCase):
    def test_native_shape_and_prefix_retirement(self):
        roster = root().get_command('roster')
        self.assertEqual(
            {command.name for command in roster.commands},
            {'promote', 'trade', 'draft', 'price'},
        )
        command = roster.get_command('draft')
        self.assertEqual(
            [(row.name, row.type, row.required) for row in command.parameters],
            [
                ('player', discord.AppCommandOptionType.user, True),
                ('team', discord.AppCommandOptionType.string, True),
            ],
        )
        self.assertNotIn(
            'draft', {command.name for command in league.league.__cog_commands__}
        )

    def test_access_preserves_staff_and_drafter_role_parity(self):
        actor = member(roles=(role('Member'),))
        with mock.patch.object(service.house_show, '_league_scope', return_value=True), mock.patch.object(
            service.settings, 'is_staff', return_value=False
        ):
            self.assertIn('Only Drafters', service.access_error(actor, 300))
            actor.roles = (role('Drafter'),)
            self.assertIsNone(service.access_error(actor, 300))
        with mock.patch.object(service.house_show, '_league_scope', return_value=True), mock.patch.object(
            service.settings, 'is_staff', return_value=True
        ):
            self.assertIsNone(service.access_error(member(), 300))


class WorkerTests(unittest.TestCase):
    def test_request_and_result_use_frozen_primitive_boundaries(self):
        item = request()
        with self.assertRaises(FrozenInstanceError):
            item.guild_id = 1
        self.assertIsInstance(item.role_colours, tuple)

    def test_worker_reloads_exact_team_and_player_and_preserves_summary(self):
        discord_member = SimpleNamespace(
            elo_moonrise=1200,
            get_record=mock.Mock(return_value=(5, 3)),
        )
        player = SimpleNamespace(
            elo_moonrise=1100,
            discord_member=discord_member,
            get_record=mock.Mock(return_value=(2, 1)),
        )
        team = SimpleNamespace(
            id=7,
            name='Ronin',
            house_id=4,
            house=SimpleNamespace(name='Lightning'),
        )
        rendered = SimpleNamespace(fp=BytesIO(b'png-data'), close=mock.Mock())
        with mock.patch.object(
            workers.models.db, 'connection_context', return_value=nullcontext()
        ), mock.patch.object(
            workers.models.Team, 'get_or_except', return_value=team
        ) as team_lookup, mock.patch.object(
            workers.models.Player, 'get_or_except', return_value=player
        ) as player_lookup, mock.patch.object(
            workers.image_storage, 'resolve_image', return_value='/tmp/team.png'
        ), mock.patch.object(
            workers.imgen, 'player_draft_card_from_sources', return_value=rendered
        ) as render:
            result = workers._render(request())
        self.assertEqual(result.image_bytes, b'png-data')
        self.assertEqual(result.filename, 'ronin-selects-draftee.png')
        self.assertTrue(team_lookup.call_args.kwargs['require_exact'])
        self.assertEqual(player_lookup.call_args.kwargs['player_string'], '11')
        kwargs = render.call_args.kwargs
        self.assertEqual(kwargs['selecting_string'], 'Lightning')
        self.assertIn('1100 ELO', kwargs['player_summary'])
        self.assertIn('1200 ELO', kwargs['player_summary'])

    def test_missing_exact_discord_team_role_is_private_lookup_failure(self):
        team = SimpleNamespace(
            id=7, name='Ronin', house_id=None, house=None
        )
        with mock.patch.object(
            workers.models.db, 'connection_context', return_value=nullcontext()
        ), mock.patch.object(
            workers.models.Team, 'get_or_except', return_value=team
        ), mock.patch.object(
            workers.image_storage, 'resolve_image', return_value='/tmp/team.png'
        ):
            with self.assertRaisesRegex(
                workers.DraftCardLookupError, 'exact Discord role'
            ):
                workers._render(request(role_colours=()))

    def test_slow_render_keeps_event_loop_responsive(self):
        async def scenario():
            started = threading.Event()
            release = threading.Event()

            def slow(_request):
                started.set()
                release.wait(timeout=2)
                return workers.DraftCardResult(
                    'Draftee', 'Ronin', b'png', 'draft.png'
                )

            with mock.patch.object(workers, '_render', side_effect=slow):
                task = asyncio.create_task(workers.run_draft_card(request()))
                while not started.is_set():
                    await asyncio.sleep(0.001)
                await asyncio.sleep(0.02)
                responsive = not task.done()
                release.set()
                await asyncio.sleep(0.05)
                result = await task
            return responsive, result

        responsive, result = asyncio.run(scenario())
        self.assertTrue(responsive)
        self.assertEqual(result.image_bytes, b'png')


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_success_defers_private_then_publishes_attributed_card(self):
        command = root().get_command('roster').get_command('draft')
        channel = SimpleNamespace(id=50, send=mock.AsyncMock(return_value='message'))
        interaction = SimpleNamespace(
            guild=guild(),
            channel=channel,
            user=member(),
            response=SimpleNamespace(
                defer=mock.AsyncMock(), send_message=mock.AsyncMock()
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
            delete_original_response=mock.AsyncMock(),
        )
        cog = league.league.__new__(league.league)
        output = workers.DraftCardResult(
            'Draftee', 'Ronin', b'png', 'draft-card.png'
        )
        with mock.patch.object(service, 'access_error', return_value=None), mock.patch.object(
            service, 'run_draft_card', new=mock.AsyncMock(return_value=output)
        ):
            result = await command.callback(
                cog, interaction, member(11), 'Ronin'
            )
        self.assertEqual(result, output)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.delete_original_response.assert_awaited_once()
        channel.send.assert_awaited_once()
        caption = channel.send.call_args.args[0]
        self.assertIn('<@10>', caption)
        self.assertIn('Draftee', caption)
        self.assertIsInstance(channel.send.call_args.kwargs['file'], discord.File)
