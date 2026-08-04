"""Offline coverage for the approved native command taxonomy."""

from types import SimpleNamespace
import unittest
from unittest import mock

import discord
from discord.ext import commands

from tests.test_newgame_worker import import_offline_runtime


games = import_offline_runtime('modules.games')
administration = import_offline_runtime('modules.administration')
misc = import_offline_runtime('modules.misc')


def app_group(cog_class, name):
    return next(
        command
        for command in cog_class.__cog_app_commands__
        if command.name == name
    )


class SlashTaxonomyRegistrationTests(unittest.TestCase):
    def test_staffhelp_is_an_exact_no_option_tools_support_root(self):
        command = app_group(misc.misc, 'staffhelp')

        self.assertEqual(command.name, 'staffhelp')
        self.assertEqual(command.parameters, [])
        self.assertTrue(command.guild_only)
        self.assertEqual(
            [command.name for command in misc.misc.__cog_app_commands__],
            ['staffhelp'],
        )

    def test_staffhelp_has_no_retired_prefix_registration(self):
        prefix_commands = misc.misc.__cog_commands__

        self.assertNotIn('staffhelp', {command.name for command in prefix_commands})
        self.assertFalse(
            any('helpstaff' in command.aliases for command in prefix_commands)
        )

    def test_current_native_surface_uses_domain_roots(self):
        game_group = app_group(games.polygames, 'game')
        leaderboard_group = app_group(games.polygames, 'leaderboard')
        player_group = app_group(games.polygames, 'player')
        elo_group = app_group(administration.administration, 'elo')
        team_group = app_group(administration.administration, 'team')

        self.assertEqual(
            [command.name for command in games.polygames.__cog_app_commands__],
            ['game', 'leaderboard', 'player'],
        )
        self.assertEqual(
            [
                command.name
                for command
                in administration.administration.__cog_app_commands__
            ],
            ['elo', 'team'],
        )
        self.assertEqual(
            {command.name for command in game_group.commands},
            {
                'record',
                'open',
                'join',
                'leave',
                'search',
                'show',
                'start',
                'win',
                'unwin',
                'delete',
                'confirm',
                'unconfirmed',
                'set-ranked',
                'map',
                'side',
                'notes',
                'name',
                'tribe',
                'extend',
                'unstart',
                'manage',
            },
        )
        self.assertEqual(
            {command.name for command in game_group.get_command('manage').commands},
            {'kick'},
        )
        self.assertEqual(
            {command.name for command in elo_group.commands},
            {'recalculate', 'status'},
        )
        self.assertEqual(
            {command.name for command in team_group.commands},
            {
                'create',
                'show',
                'emoji',
                'image',
                'name',
                'server',
                'tier',
                'house',
            },
        )
        self.assertEqual(
            {command.name for command in leaderboard_group.commands},
            {'activity', 'players', 'squads'},
        )
        self.assertEqual(
            {command.name for command in player_group.commands},
            {'show', 'register', 'timezone'},
        )

    def test_typed_shapes_and_prefix_aliases_are_preserved(self):
        game_group = app_group(games.polygames, 'game')
        player_group = app_group(games.polygames, 'player')
        elo_group = app_group(administration.administration, 'elo')
        team_group = app_group(administration.administration, 'team')

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
                (parameter.name, parameter.type, parameter.required)
                for parameter
                in game_group.get_command('join').parameters
            ],
            [
                ('game_id', discord.AppCommandOptionType.integer, True),
                ('side', discord.AppCommandOptionType.string, False),
                ('member', discord.AppCommandOptionType.user, False),
            ],
        )
        self.assertEqual(
            [
                (parameter.name, parameter.type, parameter.required)
                for parameter
                in game_group.get_command('leave').parameters
            ],
            [
                ('game_id', discord.AppCommandOptionType.integer, True),
            ],
        )
        self.assertEqual(
            [
                (parameter.name, parameter.type, parameter.required)
                for parameter
                in game_group.get_command('manage').get_command('kick').parameters
            ],
            [
                ('game_id', discord.AppCommandOptionType.integer, True),
                ('member', discord.AppCommandOptionType.user, True),
            ],
        )
        self.assertEqual(
            [
                (parameter.name, parameter.type, parameter.required)
                for parameter in game_group.get_command('start').parameters
            ],
            [
                ('game_id', discord.AppCommandOptionType.integer, True),
                ('name', discord.AppCommandOptionType.string, True),
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
                (parameter.name, parameter.type, parameter.required)
                for parameter
                in game_group.get_command('notes').parameters
            ],
            [
                ('game_id', discord.AppCommandOptionType.integer, True),
            ],
        )
        self.assertEqual(
            [
                (parameter.name, parameter.type, parameter.required)
                for parameter
                in game_group.get_command('side').parameters
            ],
            [
                ('game_id', discord.AppCommandOptionType.integer, True),
                ('side', discord.AppCommandOptionType.string, True),
                ('role', discord.AppCommandOptionType.role, False),
                ('name', discord.AppCommandOptionType.string, False),
                ('clear', discord.AppCommandOptionType.boolean, False),
            ],
        )
        self.assertEqual(
            [
                (parameter.name, parameter.type, parameter.required)
                for parameter
                in game_group.get_command('name').parameters
            ],
            [
                ('game_id', discord.AppCommandOptionType.integer, True),
            ],
        )
        self.assertEqual(
            [
                (parameter.name, parameter.type, parameter.required)
                for parameter
                in game_group.get_command('tribe').parameters
            ],
            [
                ('game_id', discord.AppCommandOptionType.integer, True),
                ('bulk', discord.AppCommandOptionType.string, False),
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
        self.assertEqual(
            [
                (parameter.name, parameter.type, parameter.required)
                for parameter
                in team_group.get_command('emoji').parameters
            ],
            [
                ('team', discord.AppCommandOptionType.string, False),
                ('emoji', discord.AppCommandOptionType.string, False),
                ('clear', discord.AppCommandOptionType.boolean, False),
            ],
        )
        self.assertEqual(
            [
                (parameter.name, parameter.type, parameter.required)
                for parameter
                in team_group.get_command('create').parameters
            ],
            [
                ('name', discord.AppCommandOptionType.string, True),
            ],
        )
        self.assertEqual(
            [
                (parameter.name, parameter.type, parameter.required)
                for parameter
                in team_group.get_command('name').parameters
            ],
            [
                ('team', discord.AppCommandOptionType.string, False),
                ('name', discord.AppCommandOptionType.string, False),
            ],
        )
        self.assertEqual(
            [
                (parameter.name, parameter.type, parameter.required)
                for parameter
                in team_group.get_command('server').parameters
            ],
            [
                ('team', discord.AppCommandOptionType.string, False),
                ('server_id', discord.AppCommandOptionType.integer, False),
                ('clear', discord.AppCommandOptionType.boolean, False),
            ],
        )
        self.assertEqual(
            [
                (parameter.name, parameter.type, parameter.required)
                for parameter
                in team_group.get_command('tier').parameters
            ],
            [
                ('team', discord.AppCommandOptionType.string, False),
                ('tier', discord.AppCommandOptionType.string, False),
            ],
        )
        self.assertEqual(
            [
                (parameter.name, parameter.type, parameter.required)
                for parameter
                in team_group.get_command('house').parameters
            ],
            [
                ('team', discord.AppCommandOptionType.string, False),
                ('house', discord.AppCommandOptionType.string, False),
                ('clear', discord.AppCommandOptionType.boolean, False),
            ],
        )
        self.assertEqual(
            [
                (parameter.name, parameter.type, parameter.required)
                for parameter
                in team_group.get_command('show').parameters
            ],
            [
                ('team', discord.AppCommandOptionType.string, False),
            ],
        )
        self.assertEqual(
            [
                (parameter.name, parameter.type, parameter.required)
                for parameter
                in team_group.get_command('image').parameters
            ],
            [
                ('team', discord.AppCommandOptionType.string, False),
                ('image', discord.AppCommandOptionType.attachment, False),
                ('clear', discord.AppCommandOptionType.boolean, False),
            ],
        )
        self.assertEqual(
            [
                (parameter.name, parameter.type, parameter.required)
                for parameter
                in player_group.get_command('timezone').parameters
            ],
            [
                ('member', discord.AppCommandOptionType.user, False),
                ('offset', discord.AppCommandOptionType.string, False),
                ('clear', discord.AppCommandOptionType.boolean, False),
            ],
        )
        autocomplete_callbacks = {
            command.name: command._params['team'].autocomplete
            for command in team_group.commands
            if 'team' in command._params
        }
        self.assertEqual(
            set(autocomplete_callbacks),
            {'show', 'emoji', 'image', 'name', 'server', 'tier', 'house'},
        )
        self.assertEqual(
            {
                autocomplete_callbacks[name]
                for name in {'show', 'emoji', 'image', 'name', 'server', 'tier'}
            },
            {administration.team_attributes_service.autocomplete_teams},
        )
        self.assertIs(
            autocomplete_callbacks['house'],
            administration.team_attributes_service.autocomplete_house_teams,
        )
        self.assertIs(
            team_group.get_command('house')._params['house'].autocomplete,
            administration.team_attributes_service.autocomplete_houses,
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
        administration_prefix = {
            command.name: command
            for command in administration.administration.__cog_commands__
        }
        self.assertNotIn('team_add', administration_prefix)
        self.assertFalse(any(
            'team_add_junior' in command.aliases
            for command in administration_prefix.values()
        ))
        self.assertIn('team_image', administration_prefix)
        self.assertEqual(
            administration_prefix['team_image'].clean_params['team_name'].annotation,
            str,
        )
        league = import_offline_runtime('modules.league')
        league_prefix = {
            command.name: command for command in league.league.__cog_commands__
        }
        self.assertNotIn('team_house', league_prefix)
        self.assertEqual(league_prefix['team_edit'].aliases, ['team_tier'])


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

    async def test_game_win_adapter_reuses_prefix_checks_and_shared_service(self):
        events = []

        async def can_run(ctx):
            events.append('checks')
            self.assertEqual(events[0], 'defer')
            return True

        prefix_command = SimpleNamespace(
            can_run=can_run,
        )
        cog = SimpleNamespace(win=prefix_command)
        context = SimpleNamespace()
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=300),
            user=SimpleNamespace(id=901),
            channel=SimpleNamespace(),
            response=SimpleNamespace(
                defer=mock.AsyncMock(side_effect=lambda: events.append('defer')),
            ),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )
        command = app_group(games.polygames, 'game').get_command('win')

        with mock.patch.object(
            games.commands.Context,
            'from_interaction',
            new=mock.AsyncMock(return_value=context),
        ), mock.patch.object(
            games.settings,
            'guild_setting',
            return_value='$',
        ), mock.patch.object(
            games.game_win,
            'build_request',
            side_effect=lambda **kwargs: (
                events.append('build') or SimpleNamespace()
            ),
        ) as build_request, mock.patch.object(
            games.game_win,
            'run_win',
            new=mock.AsyncMock(
                side_effect=lambda *args, **kwargs: events.append('service'),
            ),
        ) as run_win:
            await command.callback(cog, interaction, 42, 'Alpha')

        self.assertEqual(events, ['defer', 'checks', 'build', 'service'])
        build_request.assert_called_once_with(
            game_id=42,
            member=interaction.user,
            guild_id=300,
            prefix='$',
            winner_text='Alpha',
            invoked_with='win',
        )
        run_win.assert_awaited_once()

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
