"""Focused coverage for P8.16 native league trade prices."""

import asyncio
from contextlib import nullcontext
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

import discord

from tests.test_newgame_worker import import_offline_runtime


workers = import_offline_runtime('modules.league_trade_price_workers')
service = import_offline_runtime('modules.league_trade_price')
league = import_offline_runtime('modules.league')


def root():
    return next(
        command for command in league.league.__cog_app_commands__
        if command.name == 'league'
    )


def role(name):
    return SimpleNamespace(name=name)


def member(member_id=10, *, roles=()):
    return SimpleNamespace(
        id=member_id,
        name=f'User{member_id}',
        display_name=f'User {member_id}',
        mention=f'<@{member_id}>',
        roles=tuple(roles),
    )


def request(**overrides):
    values = dict(
        guild_id=300,
        player_discord_id=11,
        player_display_name='Draftee',
        ending_season=None,
        leadership_adjustment=False,
    )
    values.update(overrides)
    return workers.TradePriceRequest(**values)


def fake_player():
    player = SimpleNamespace(id=5)
    player.polychamps_season_tier = mock.Mock(
        side_effect=lambda season: {5: 3, 6: 2, 7: 1}.get(season)
    )
    player.polychamps_season_record = mock.Mock(
        side_effect=lambda season: {
            5: (2, 1), 6: (3, 2), 7: (4, 1)
        }[season]
    )
    return player


class RegistrationAndServiceTests(unittest.TestCase):
    def test_native_shape_and_prefix_retirement(self):
        roster = root().get_command('roster')
        self.assertEqual(
            {command.name for command in roster.commands},
            {'promote', 'trade', 'draft', 'price'},
        )
        command = roster.get_command('price')
        self.assertEqual(
            [(row.name, row.type, row.required) for row in command.parameters],
            [
                ('player', discord.AppCommandOptionType.user, True),
                ('season', discord.AppCommandOptionType.integer, False),
            ],
        )
        prefix_names = {command.name for command in league.league.__cog_commands__}
        self.assertNotIn('tradeprice', prefix_names)
        self.assertFalse(any('playerprice' in command.aliases for command in league.league.__cog_commands__))

    def test_public_league_scope_and_leadership_capture(self):
        with mock.patch.object(service.house_show, '_league_scope', return_value=False):
            self.assertIn('configured league', service.access_error(300))
        with mock.patch.object(service.house_show, '_league_scope', return_value=True):
            self.assertIsNone(service.access_error(300))
        item = service.request(
            guild=SimpleNamespace(id=300),
            player=member(11, roles=(role('House Leader'),)),
            ending_season=7,
        )
        self.assertTrue(item.leadership_adjustment)
        self.assertEqual(item.ending_season, 7)

    def test_public_message_discloses_formula_inputs_and_inference(self):
        result = workers.TradePriceResult(
            player_discord_id=11,
            player_display_name='Draftee',
            ending_season=6,
            inference='previous_due_to_incomplete',
            current_season=7,
            leadership_adjustment=True,
            seasons=(
                workers.TradePriceSeason(4, None, 0, 0),
                workers.TradePriceSeason(5, 2, 3, 1),
                workers.TradePriceSeason(6, 1, 4, 2),
            ),
            price=42,
        )
        output = service.public_message(member(10), result)
        self.assertIn('**42**', output)
        self.assertIn('incomplete/unconfirmed Season 7', output)
        self.assertIn('Leadership adjustment: **applied**', output)
        self.assertIn('Season 5: Tier 2, 3-1 (4 games)', output)


class WorkerTests(unittest.TestCase):
    def test_request_and_result_are_frozen_primitives(self):
        item = request()
        with self.assertRaises(FrozenInstanceError):
            item.guild_id = 1
        row = workers.TradePriceSeason(7, 1, 3, 2)
        self.assertEqual(row.games, 5)

    def test_player_read_uses_exact_select_and_never_upserts(self):
        query = mock.MagicMock()
        query.join.return_value = query
        query.where.return_value = query
        expected = SimpleNamespace(id=5)
        query.get.return_value = expected
        with mock.patch.object(
            workers.models.Player, 'select', return_value=query
        ), mock.patch.object(
            workers.models.Player, 'get_by_discord_id'
        ) as upsert_read:
            result = workers._load_player(request())
        self.assertIs(result, expected)
        upsert_read.assert_not_called()

    def test_legacy_default_inference_and_formula_inputs_are_preserved(self):
        player = fake_player()
        formula = mock.Mock(return_value=73)
        with mock.patch.object(
            workers.models.db, 'connection_context', return_value=nullcontext()
        ), mock.patch.object(
            workers, '_load_player', return_value=player
        ), mock.patch.object(
            workers, '_current_season', return_value=8
        ), mock.patch.object(
            workers, '_has_incomplete_current_game', return_value=True
        ), mock.patch.object(
            workers.utilities, 'trade_price_formula', formula
        ):
            result = workers._calculate(
                request(leadership_adjustment=True)
            )
        self.assertEqual(result.ending_season, 7)
        self.assertEqual(result.inference, 'previous_due_to_incomplete')
        self.assertEqual(result.price, 73)
        formula.assert_called_once_with(
            [(3, 3, 2), (2, 5, 3), (1, 5, 4)], True
        )

    def test_explicit_season_skips_incomplete_fallback(self):
        player = fake_player()
        with mock.patch.object(
            workers.models.db, 'connection_context', return_value=nullcontext()
        ), mock.patch.object(
            workers, '_load_player', return_value=player
        ), mock.patch.object(
            workers, '_current_season'
        ) as current_season, mock.patch.object(
            workers, '_has_incomplete_current_game'
        ) as incomplete, mock.patch.object(
            workers.utilities, 'trade_price_formula', return_value=50
        ):
            result = workers._calculate(request(ending_season=7))
        self.assertEqual(result.inference, 'explicit')
        current_season.assert_not_called()
        incomplete.assert_not_called()

    def test_slow_worker_keeps_event_loop_responsive(self):
        async def scenario():
            started = threading.Event()
            release = threading.Event()

            def slow(_request):
                started.set()
                release.wait(timeout=2)
                return workers.TradePriceResult(
                    11, 'Draftee', 7, 'explicit', 8, False, (), 42
                )

            with mock.patch.object(workers, '_calculate', side_effect=slow):
                task = asyncio.create_task(workers.run_trade_price(request()))
                while not started.is_set():
                    await asyncio.sleep(0.001)
                await asyncio.sleep(0.02)
                responsive = not task.done()
                release.set()
                result = await task
            return responsive, result

        responsive, result = asyncio.run(scenario())
        self.assertTrue(responsive)
        self.assertEqual(result.price, 42)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_defers_private_then_publishes_transparent_result(self):
        command = root().get_command('roster').get_command('price')
        channel = SimpleNamespace(id=50, send=mock.AsyncMock(return_value='message'))
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=300),
            channel=channel,
            user=member(),
            response=SimpleNamespace(
                defer=mock.AsyncMock(), send_message=mock.AsyncMock()
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
            delete_original_response=mock.AsyncMock(),
        )
        result = workers.TradePriceResult(
            11,
            'Draftee',
            7,
            'explicit',
            8,
            False,
            (workers.TradePriceSeason(7, 1, 4, 1),),
            42,
        )
        cog = league.league.__new__(league.league)
        with mock.patch.object(
            service, 'access_error', return_value=None
        ), mock.patch.object(
            service, 'run_trade_price', new=mock.AsyncMock(return_value=result)
        ):
            output = await command.callback(
                cog, interaction, member(11), None
            )
        self.assertEqual(output, result)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.delete_original_response.assert_awaited_once()
        channel.send.assert_awaited_once()
        self.assertIn('Trade price', channel.send.call_args.args[0])
        mentions = channel.send.call_args.kwargs['allowed_mentions']
        self.assertFalse(mentions.everyone)
        self.assertFalse(mentions.users)
        self.assertFalse(mentions.roles)
