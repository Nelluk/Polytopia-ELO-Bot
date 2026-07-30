"""Offline coverage for the approved native command taxonomy."""

from types import SimpleNamespace
import unittest
from unittest import mock

import discord
from discord.ext import commands

from tests.test_newgame_worker import import_offline_runtime


games = import_offline_runtime('modules.games')
administration = import_offline_runtime('modules.administration')


def app_group(cog_class, name):
    return next(
        command
        for command in cog_class.__cog_app_commands__
        if command.name == name
    )


class SlashTaxonomyRegistrationTests(unittest.TestCase):
    def test_current_native_surface_uses_domain_roots(self):
        game_group = app_group(games.polygames, 'game')
        leaderboard_group = app_group(games.polygames, 'leaderboard')
        elo_group = app_group(administration.administration, 'elo')

        self.assertEqual(
            [command.name for command in games.polygames.__cog_app_commands__],
            ['game', 'leaderboard', 'lb2'],
        )
        self.assertEqual(
            [
                command.name
                for command
                in administration.administration.__cog_app_commands__
            ],
            ['elo'],
        )
        self.assertEqual(
            {command.name for command in game_group.commands},
            {
                'create',
                'win',
                'unwin',
                'delete',
                'confirm',
                'unconfirmed',
                'set-ranked',
                'extend',
                'unstart',
            },
        )
        self.assertEqual(
            {command.name for command in elo_group.commands},
            {'recalculate', 'status'},
        )
        self.assertEqual(
            {command.name for command in leaderboard_group.commands},
            {'activity', 'players', 'squads'},
        )

    def test_typed_shapes_and_prefix_aliases_are_preserved(self):
        game_group = app_group(games.polygames, 'game')
        elo_group = app_group(administration.administration, 'elo')

        self.assertEqual(
            [
                (parameter.name, parameter.type)
                for parameter
                in game_group.get_command('win').parameters
            ],
            [
                ('game_id', discord.AppCommandOptionType.integer),
                ('winner', discord.AppCommandOptionType.string),
            ],
        )
        self.assertEqual(
            [
                (parameter.name, parameter.type)
                for parameter
                in game_group.get_command('set-ranked').parameters
            ],
            [
                ('game_id', discord.AppCommandOptionType.integer),
                ('ranked', discord.AppCommandOptionType.boolean),
            ],
        )
        self.assertEqual(
            [
                (parameter.name, parameter.type)
                for parameter
                in elo_group.get_command('recalculate').parameters
            ],
            [
                ('game_id', discord.AppCommandOptionType.integer),
                ('confirm', discord.AppCommandOptionType.boolean),
            ],
        )

        prefix = {
            command.name: command
            for command in games.polygames.__cog_commands__
        }
        self.assertTrue(all(
            isinstance(prefix[name], commands.Command)
            for name in ('newgame', 'win', 'unwin', 'delete')
        ))
        self.assertEqual(prefix['win'].aliases, ['lose'])
        self.assertEqual(
            set(prefix['delete'].aliases),
            {'delete_game', 'delgame', 'delmatch', 'deletegame'},
        )


class SlashTaxonomyAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_game_admin_subcommand_delegates_to_existing_handler(self):
        handler = mock.AsyncMock()
        admin_cog = SimpleNamespace(confirm_slash=handler)
        cog = games.polygames.__new__(games.polygames)
        cog.bot = SimpleNamespace(
            get_cog=lambda name: (
                admin_cog if name == 'administration' else None
            )
        )
        interaction = SimpleNamespace()
        command = app_group(games.polygames, 'game').get_command('confirm')

        await command.callback(cog, interaction, 42)

        handler.assert_awaited_once_with(interaction, 42)

    async def test_game_win_adapter_reuses_prefix_checks_and_callback(self):
        events = []

        async def can_run(ctx):
            events.append('checks')
            return True

        async def callback(cog, ctx, game_id, *, winner):
            events.append('callback')
            self.assertEqual(game_id, 42)
            self.assertEqual(winner, 'Alpha')
            self.assertEqual(ctx.prefix, '$')
            self.assertEqual(ctx.invoked_with, 'win')

        prefix_command = SimpleNamespace(
            can_run=can_run,
            callback=callback,
        )
        cog = SimpleNamespace(win=prefix_command)
        context = SimpleNamespace()
        interaction = SimpleNamespace(guild=SimpleNamespace(id=300))
        command = app_group(games.polygames, 'game').get_command('win')

        with mock.patch.object(
            games.commands.Context,
            'from_interaction',
            new=mock.AsyncMock(return_value=context),
        ), mock.patch.object(
            games.settings,
            'guild_setting',
            return_value='$',
        ):
            await command.callback(cog, interaction, 42, 'Alpha')

        self.assertEqual(events, ['checks', 'callback'])

    async def test_failed_prefix_check_stops_game_adapter(self):
        prefix_command = SimpleNamespace(
            can_run=mock.AsyncMock(return_value=False),
            callback=mock.AsyncMock(),
        )
        cog = SimpleNamespace(unwin=prefix_command)
        context = SimpleNamespace()
        interaction = SimpleNamespace(guild=SimpleNamespace(id=300))
        command = app_group(games.polygames, 'game').get_command('unwin')

        with mock.patch.object(
            games.commands.Context,
            'from_interaction',
            new=mock.AsyncMock(return_value=context),
        ), mock.patch.object(
            games.settings,
            'guild_setting',
            return_value='$',
        ):
            await command.callback(cog, interaction, 42)

        prefix_command.callback.assert_not_awaited()
