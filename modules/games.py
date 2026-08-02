import discord
from discord.ext import commands
import modules.utilities as utilities
import settings
import modules.exceptions as exceptions
import modules.achievements as achievements
from modules import channels
from modules import image_storage
from modules import leaderboard_views
from modules import leaderboard_workers
from modules import leaderboard_v2
from modules import player_views
from modules import player_workers
from modules import elo_workers
from modules import game_win
from modules import game_map
from modules import game_workers
from modules import game_open
from modules import game_open_workers
from modules import game_open_views
from modules import game_record_views
from modules import game_search_views
from modules import game_search_workers
from modules import game_detail_views
from modules import game_detail_workers
from modules import game_detail_actions
from modules import game_deletion
from modules import game_join_leave
from modules import game_join_workers
from modules import game_kick_workers
from modules import game_start, game_start_workers
from modules.elo_jobs import EloJobConflict
import peewee
import modules.models as models
from modules.models import Game, db, Player, Team, DiscordMember, Squad, GameSide, Tribe, Lineup
from modules.league import auto_grad_novas, populate_league_team_channels, get_team_leadership
import modules.league as league
from itertools import groupby
import logging
import datetime
import asyncio
import re
from matplotlib import pyplot as plt
import io
import pandas as pd
import scipy.signal as signal
from typing import Literal

logger = logging.getLogger('polybot.' + __name__)
elo_logger = logging.getLogger('polybot.elo')


GAME_SEARCH_VIEW_CHOICES = [
    discord.app_commands.Choice(name='All games', value='all'),
    discord.app_commands.Choice(
        name='Joinable for me',
        value='joinable',
    ),
    discord.app_commands.Choice(name='All open', value='all-open'),
    discord.app_commands.Choice(
        name='Waiting to start',
        value='waiting',
    ),
    discord.app_commands.Choice(name='My open games', value='mine'),
    discord.app_commands.Choice(name='Active games', value='active'),
    discord.app_commands.Choice(
        name='Completed games',
        value='completed',
    ),
    discord.app_commands.Choice(
        name='Unconfirmed results',
        value='unconfirmed',
    ),
]

GAME_MAP_TYPE_CHOICES = [
    discord.app_commands.Choice(name=map_type, value=map_type)
    for map_type in settings.map_types
]


class PolyGame(commands.Converter):
    async def convert(self, ctx, game_id, allow_cross_guild=False):

        utilities.connect()
        try:
            game = Game.get(id=int(game_id))
        except (ValueError, peewee.DataError):
            await ctx.send(f'Invalid game ID "{game_id}".')
            raise commands.UserInputError()
        except peewee.DoesNotExist:
            await ctx.send(f'Game with ID {game_id} cannot be found.')
            raise commands.UserInputError()
        else:
            logger.debug(f'Game with ID {game_id} found.')
            if game.guild_id != ctx.guild.id and not allow_cross_guild:
                logger.warning('Game does not belong to same guild')
                try:
                    server_name = settings.guild_setting(guild_id=game.guild_id, setting_name='display_name')
                except exceptions.CheckFailedError:
                    server_name = settings.guild_setting(guild_id=None, setting_name='display_name')
                    # config['default'][setting_name]
                if game.is_pending:
                    game_summary_str = ''
                else:
                    game_name = f'*{game.name}*' if game.name and game.name.strip() else ''
                    game_summary_str = f'\n`{(str(game.date))}` - {game.size_string()} - {game.get_gamesides_string(include_emoji=False)} - {game_name} - {game.get_game_status_string()}'

                if not game.is_pending:
                    embed, _ = game.embed(guild=ctx.guild, prefix=ctx.prefix)
                    await image_storage.send_game_embed(ctx, game, embed=embed)

                await ctx.send(f'Game with ID {game_id} is associated with a different Discord server: __{server_name}__.{game_summary_str}')
                raise commands.UserInputError()
            return game


class NewGameRosterError(ValueError):
    """User-facing roster resolution or permission failure."""


async def resolve_newgame_roster(ctx, args, *, ranked_flag):
    """Resolve the shared prefix/slash roster grammar to Discord members."""

    if len(args) == 1:
        args_list = [str(ctx.author.id), 'vs', args[0]]
    else:
        args_list = list(args)

    player_groups = [
        list(group)
        for is_separator, group in groupby(
            args_list,
            lambda value: value.lower() in ('vs', 'versus'),
        )
        if not is_separator
    ]
    total_players = sum(map(len, player_groups))
    game_allowed, join_error_message = settings.can_user_join_game(
        user_level=settings.get_user_level(ctx.author),
        game_size=total_players,
        is_ranked=ranked_flag,
        is_host=True,
    )
    if not game_allowed:
        raise NewGameRosterError(join_error_message)

    discord_groups = []
    author_found = False
    for group in player_groups:
        discord_group = []
        for player_argument in group:
            guild_matches = await utilities.get_guild_member(
                ctx,
                player_argument,
            )
            if len(guild_matches) == 0:
                raise NewGameRosterError(
                    f'Could not match “{player_argument}” to a server '
                    'member. Try using an @mention.'
                )
            if len(guild_matches) > 1:
                raise NewGameRosterError(
                    f'More than one server match was found for '
                    f'“{player_argument}”. Use an @mention.'
                )
            member = guild_matches[0]
            if member == ctx.author:
                author_found = True
            discord_group.append(member)
        discord_groups.append(discord_group)

    if not author_found and not settings.is_staff(ctx.author):
        raise NewGameRosterError(
            'You cannot record a game that you are not participating in.'
        )
    return tuple(tuple(group) for group in discord_groups)


class polygames(commands.Cog):
    game_group = discord.app_commands.Group(
        name='game',
        description='Create, manage, and correct games.',
        guild_only=True,
    )
    game_manage_group = discord.app_commands.Group(
        name='manage',
        description='Manage pending-game membership and lifecycle.',
        parent=game_group,
        guild_only=True,
    )
    leaderboard_group = discord.app_commands.Group(
        name='leaderboard',
        description='View competitive rankings and activity.',
        guild_only=True,
    )
    player_group = discord.app_commands.Group(
        name='player',
        description='View and manage player profiles.',
        guild_only=True,
    )

    def __init__(self, bot):
        self.bot = bot
        if settings.run_tasks:
            self.bg_task = asyncio.create_task(self.task_purge_game_channels())
            self.bg_task2 = asyncio.create_task(self.task_set_champion_role())

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author == self.bot.user:
            return

        if message.role_mentions and discord.utils.get(message.role_mentions, name='ELO-Helper'):
            prefix = settings.guild_setting(message.guild.id, 'command_prefix')
            await message.channel.send(f'{message.author.mention}, to receive staff help in the future please use the `{prefix}staffhelp` command, '
                '- since you have already pinged please wait for a response.')

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        query = GameSide.update(team_chan=None).where(GameSide.team_chan == channel.id)
        res = query.execute()
        if res:
            logger.debug(f'on_guild_channel_delete: detected deletion of gameside channel {channel.id} {channel.name} and removed reference from db')

        query = Game.update(game_chan=None).where(Game.game_chan == channel.id)
        res = query.execute()
        if res:
            logger.debug(f'on_guild_channel_delete: detected deletion of game channel {channel.id} {channel.name} and removed reference from db')

    @commands.Cog.listener()
    async def on_member_join(self, member):
        player, upserted = models.Player.get_by_discord_id(discord_id=member.id, discord_name=member.name, discord_nick=member.nick, guild_id=member.guild.id)
        if player:
            if upserted:
                logger.debug(f'on_member_join: {member.display_name} joined guild {member.guild.name} and Player was upserted as an existing DiscordMember.')
            logger.debug(f'on_member_join: {member.display_name} re-joined guild {member.guild.name} and has an existing Player entry.')
        else:
            return logger.debug(f'on_member_join: {member.display_name} joined guild {member.guild.name} but does not have an existing DiscordMember record.')

        # add re-joining player back to any relevant game channels

        async def fix_channel_perm(channel, member):
            try:
                await channels.add_member_to_channel(channel, member)
                logger.info(f'Re-adding {member.display_name} to channel {channel.id} {channel.name}')
                await channel.send(f'{member.mention} has been added back to this channel after rejoining the server. :partying_face:')
            except (discord.errors.Forbidden, discord.errors.HTTPException) as e:
                logger.warn(f'Tried to re-add {member.display_name} to channel {channel.id} {channel.name} but got error: {e}')

        pending_lineups_with_side_channels = Lineup.select().join(GameSide).join(Game).where(
            (Game.is_completed == 0) & (Lineup.player == player) & (GameSide.team_chan > 0) &
            ((GameSide.team_chan_external_server == member.guild.id) | (Game.guild_id == member.guild.id))
        )

        logger.debug(f'pending_lineups_with_side_channels {len(pending_lineups_with_side_channels)} ')
        for lineup in pending_lineups_with_side_channels:

            logger.debug(f'on_member_join: attempting to get_channel {lineup.gameside.team_chan} for game {lineup.game.id} (side_channels)')

            channel = self.bot.get_channel(lineup.gameside.team_chan)
            if not channel:
                logger.debug('no channel found')
                continue
            elif channel.guild.id != member.guild.id:
                logger.debug('channel.guild.id != member.guild.id')
                continue

            await fix_channel_perm(channel, member)
            logger.debug(f'on_member_join: fix_channel_perm for existing channel on rejoin')

        pending_lineups_with_game_channels = Lineup.select().join(Game).where(
            (Game.is_completed == 0) & (Lineup.player == player) & (Game.game_chan > 0) & (Game.guild_id == member.guild.id)
        )
        logger.debug(f'pending_lineups_with_game_channels {len(pending_lineups_with_game_channels)} ')
        for lineup in pending_lineups_with_game_channels:

            logger.debug(f'on_member_join: attempting to get_channel {lineup.game.game_chan} for game {lineup.game.id} (game_channels)')
            channel = self.bot.get_channel(lineup.game.game_chan)
            if not channel:
                logger.debug('no channel found')
                continue
            elif channel.guild.id != member.guild.id:
                logger.debug('channel.guild.id != member.guild.id')
                continue

            await fix_channel_perm(channel, member)
            logger.debug(f'on_member_join: fix_channel_perm for existing channel on rejoin')

        pending_lineups_with_no_channels = Lineup.select().join(GameSide).join(Game).where(
            (Game.is_completed == 0) & (Lineup.player == player) & (GameSide.team_chan == None) &
            ((GameSide.team_chan_external_server == member.guild.id) | (Game.guild_id == member.guild.id))
        )
        logger.debug(f'pending_lineups_with_no_channels {len(pending_lineups_with_no_channels)} ')
        for lineup in pending_lineups_with_no_channels:
            logger.debug(f'on_member_join: no channel found for lineup {lineup.id} - recreating deleted channels')
            try:
                await lineup.game.create_game_channels(settings.bot.guilds, member.guild.id, side=lineup.gameside)
            except exceptions.MyBaseException as e:
                logger.warning(f'Channel creation error: {e}')


    @commands.Cog.listener()
    async def on_member_remove(self, member):

        try:
            leaving_player = Player.get_or_except(player_string=member.id, guild_id=member.guild.id)
        except exceptions.NoSingleMatch:
            return

        pending_lineups = Lineup.select().join(Game).where(
            (Lineup.game.is_pending == 1) & (Lineup.player == leaving_player)
        )

        incomplete_lineups = Lineup.select().join(Game).where(
            (Lineup.game.is_pending == 0) & (Lineup.game.is_completed == 0) & (Lineup.player == leaving_player)
        )

        if pending_lineups:
            for l in pending_lineups:
                models.GameLog.write(game_id=l.game.id, guild_id=member.guild.id, message=f'{models.GameLog.member_string(member)} left the game while leaving the server.')

            q = Lineup.delete().where(models.Lineup.id.in_(pending_lineups))

            logger.info(f'Existing ELO player {member.display_name} {member.id} left guild {member.guild.name} - deleted Lineup records for {q.execute()} pending games.')

        if incomplete_lineups and member.guild.id == settings.server_ids['polychampions']:
            helper_role_name = settings.guild_setting(member.guild.id, 'helper_roles')[0]
            helper_role = discord.utils.get(member.guild.roles, name=helper_role_name)
            helper_mention = helper_role.mention if helper_role else 'Staff'
            await utilities.send_to_log_channel(member.guild, f'{helper_mention} - {member.mention} ({member.display_name}) left the server and has {len(incomplete_lineups)} incomplete games.')

    @commands.Cog.listener()
    async def on_user_update(self, before, after):
        if before.name != after.name:
            logger.debug(f'Attempting to change member discordname for {before.name} to {after.name}')
            # update Discord Member Name, and update display name for each Guild/Player they share with the bot
            utilities.connect()
            try:
                discord_member = DiscordMember.select().where(DiscordMember.discord_id == after.id).get()
            except peewee.DoesNotExist:
                return
            discord_member.update_name(new_name=utilities.escape_role_mentions(after.name))
            models.GameLog.write(game_id=0, guild_id=0, message=f'{models.GameLog.member_string(after)} changed username from "{before.name}"" to "{after.name}"')

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        player_query = Player.select().join(DiscordMember).where(
            (DiscordMember.discord_id == after.id) & (Player.guild_id == after.guild.id)
        )

        banned_role = discord.utils.get(before.guild.roles, name='ELO Banned')
        if banned_role not in before.roles and banned_role in after.roles:
            utilities.connect()
            try:
                player = player_query.get()
            except peewee.DoesNotExist:
                return
            player.is_banned = True
            player.save()
            logger.info(f'ELO Ban added for player {player.id} {player.name}')
            models.GameLog.write(game_id=0, guild_id=after.guild.id, message=f'{models.GameLog.member_string(after)} had *ELO Banned* role applied.')

        if banned_role in before.roles and banned_role not in after.roles:
            utilities.connect()
            try:
                player = player_query.get()
            except peewee.DoesNotExist:
                return
            player.is_banned = False
            player.save()
            logger.info(f'ELO Ban removed for player {player.id} {player.name}')
            models.GameLog.write(game_id=0, guild_id=after.guild.id, message=f'{models.GameLog.member_string(after)} had *ELO Banned* role removed.')

        inactive_role = discord.utils.get(before.guild.roles, name=settings.guild_setting(before.guild.id, 'inactive_role'))
        if inactive_role not in before.roles and inactive_role in after.roles:
            utilities.connect()
            try:
                player = player_query.get()
            except peewee.DoesNotExist:
                return
            logger.info(f'Inactive role added for player {player.id} {player.name}')
            models.GameLog.write(game_id=0, guild_id=after.guild.id, message=f'{models.GameLog.member_string(after)} had *{inactive_role.name}* role applied.')

        if inactive_role in before.roles and inactive_role not in after.roles:
            utilities.connect()
            try:
                player = player_query.get()
            except peewee.DoesNotExist:
                return
            logger.info(f'Inactive removed for player {player.id} {player.name}')
            models.GameLog.write(game_id=0, guild_id=after.guild.id, message=f'{models.GameLog.member_string(after)} had *{inactive_role.name}* role removed.')

        # Updates display name in DB if user changes their discord name or guild nick
        if before.nick == after.nick and before.name == after.name:
            return

        if before.nick != after.nick:
            logger.debug(f'Attempting to change member nick for {before.name}({before.nick}) to {after.name}({after.nick})')
            utilities.connect()
            # update nick in guild's Player record
            try:
                player = player_query.get()
            except peewee.DoesNotExist:
                return
            player.generate_display_name(player_name=after.name, player_nick=after.nick)
            models.GameLog.write(game_id=0, guild_id=after.guild.id, message=f'{models.GameLog.member_string(after)} had changed nickname from "{before.nick}" to "{after.nick}"')

    @staticmethod
    def _player_leaderboard_request(
        guild_id: int,
        invoked_with: str,
        filters: str = '',
    ) -> leaderboard_workers.PlayerLeaderboardRequest:
        filter_text = filters.upper()
        global_alias = invoked_with in ('lbglobal', 'lbg')
        return leaderboard_workers.PlayerLeaderboardRequest(
            guild_id=guild_id,
            scope=(
                'global'
                if global_alias or 'GLOBAL' in filter_text
                else 'local'
            ),
            rating='peak' if 'MAX' in filter_text else 'current',
            era=(
                'all-time'
                if 'ALLTIME' in filter_text
                else 'current'
            ),
            population=(
                'all'
                if 'ALLPLAYERS' in filter_text
                else 'active'
            ),
            active_cutoff=settings.date_cutoff,
        )

    @staticmethod
    def _player_leaderboard_entries(
        result: leaderboard_workers.PlayerLeaderboardResult,
    ) -> list[tuple[str, str]]:
        return [
            (
                f'{row.rank:>3}. {row.team_emoji}{row.name}',
                (
                    f'`ELO {row.elo}\u00a0\u00a0\u00a0\u00a0'
                    f'W {row.wins} / L {row.losses}`'
                ),
            )
            for row in result.rows
        ]

    async def _load_player_leaderboard(
        self,
        request: leaderboard_workers.PlayerLeaderboardRequest,
    ) -> leaderboard_workers.PlayerLeaderboardResult:
        return await leaderboard_workers.run_player_leaderboard(request)

    @staticmethod
    def _activity_leaderboard_request(
        guild_id: int,
        invoked_with: str,
    ) -> leaderboard_workers.ActivityLeaderboardRequest:
        return leaderboard_workers.ActivityLeaderboardRequest(
            guild_id=guild_id,
            view=(
                'global-all-time'
                if invoked_with == 'lbactivealltime'
                else 'server-30-days'
            ),
            recent_cutoff=(
                datetime.datetime.now()
                - datetime.timedelta(days=30)
            ),
        )

    @staticmethod
    def _activity_leaderboard_entries(
        result: leaderboard_workers.ActivityLeaderboardResult,
    ) -> list[tuple[str, str]]:
        count_label = (
            'Games Played'
            if result.view == 'global-all-time'
            else 'Recent Games'
        )
        return [
            (
                f'{row.rank:>3}. {row.team_emoji}{row.name}',
                (
                    f'`ELO {row.elo}\u00a0\u00a0\u00a0\u00a0'
                    f'{count_label} {row.games}`'
                ),
            )
            for row in result.rows
        ]

    async def _load_activity_leaderboard(
        self,
        request: leaderboard_workers.ActivityLeaderboardRequest,
    ) -> leaderboard_workers.ActivityLeaderboardResult:
        return await leaderboard_workers.run_activity_leaderboard(request)

    @staticmethod
    def _squad_leaderboard_request(
        guild_id: int,
        filters: str = '',
    ) -> leaderboard_workers.SquadLeaderboardRequest:
        return leaderboard_workers.SquadLeaderboardRequest(
            guild_id=guild_id,
            period=(
                'all-time'
                if 'ALLTIME' in filters.upper()
                else 'current'
            ),
            active_cutoff=settings.date_cutoff,
        )

    @staticmethod
    def _squad_leaderboard_entries(
        result: leaderboard_workers.SquadLeaderboardResult,
    ) -> list[tuple[str, str]]:
        entries = []
        for row in result.rows:
            squad_name = f'{row.squad_name}\n' if row.squad_name else ''
            emojis = ' '.join(row.member_emojis)
            member_names = ' / '.join(row.member_names)
            entries.append(
                (
                    (
                        f'{row.rank:>3}. {squad_name}'
                        f'{emojis}{member_names}'
                    ),
                    (
                        f'`#{row.squad_id} (ELO: {row.elo:4}) '
                        f'W {row.wins} / L {row.losses}`'
                    ),
                )
            )
        return entries

    async def _load_squad_leaderboard(
        self,
        request: leaderboard_workers.SquadLeaderboardRequest,
    ) -> leaderboard_workers.SquadLeaderboardResult:
        return await leaderboard_workers.run_squad_leaderboard(request)

    @settings.in_bot_channel_strict()
    @commands.command(
        aliases=['leaderboard', 'leaderboards', 'lbglobal', 'lbg'],
    )
    @commands.cooldown(2, 30, commands.BucketType.channel)
    async def lb(self, ctx, *, filters: str = ''):
        """ Display individual leaderboard

        Filters available:
        **global**
        Takes into account games played regardless of what server they were logged on.
        A player's global ELO is independent of their local server ELO.
        **max**
        Ranks leaderboard by a player's maximum ELO ever achieved
        **allplayers**
        Includes players who have not played recently. By default the leaderboard drops players who have not played in 365 days.

        Examples:
        `[p]lb` - Default local leaderboard
        `[p]lb global` - Global leaderboard
        `[p]lb max` - Local leaderboard for maximum historic ELO
        `[p]lb allplayers` - Local leaderboard including inactive players
        `[p]lb global max` - Leaderboard of maximum historic *global* ELO

        `[p]lbrecent` - Most active players of the last 30 days
        `[p]lbactivealltime` - Most active players of all time
        """

        """
        Hidden help info for now:

         **alltime**
        Ranks by the permanent Alltime ELO field, which is never reset. The standard ELO field was reset December 1st, 2020 for Moonrise release.

        `[p]lb alltime` - Local leaderboard by Alltime ELO
        `[p]lb alltime max` - Leaderboard of maximum historic Alltime ELO
        `[p]lb alltime global` - Global leaderboard by Alltime ELO
        `[p]lb global alltime allplayers max` - Global leaderboard, including inactive players, ranked by maximum hstoric Alltime ELO
        """

        request = self._player_leaderboard_request(
            guild_id=ctx.guild.id,
            invoked_with=ctx.invoked_with,
            filters=filters,
        )
        async with ctx.typing():
            try:
                result = await self._load_player_leaderboard(request)
            except (peewee.PeeweeException, ValueError) as exc:
                logger.exception('Could not load player leaderboard')
                return await ctx.send(
                    f'Could not load the player leaderboard: {exc}'
                )

        await utilities.paginate(
            self.bot,
            ctx,
            title=(
                f'**{result.title}**\n'
                f'{result.total_ranked} ranked players'
            ),
            message_list=self._player_leaderboard_entries(result),
            page_start=0,
            page_end=10,
            page_size=10,
        )

    @leaderboard_group.command(
        name='players',
        description='Explore individual player rankings.',
    )
    @discord.app_commands.checks.cooldown(
        2,
        30.0,
        key=lambda interaction: interaction.channel_id,
    )
    async def player_leaderboard_slash(
        self,
        interaction: discord.Interaction,
    ):
        """Interactive player leaderboard with cached filters."""

        await interaction.response.defer()
        ctx = await commands.Context.from_interaction(interaction)
        ctx.prefix = settings.guild_setting(
            interaction.guild.id,
            'command_prefix',
        )
        ctx.invoked_with = 'lb'
        if not await self.lb.can_run(ctx):
            return

        request = leaderboard_workers.PlayerLeaderboardRequest(
            guild_id=interaction.guild.id,
            scope='local',
            rating='current',
            era='current',
            population='active',
            active_cutoff=settings.date_cutoff,
        )
        try:
            result = await self._load_player_leaderboard(request)
        except (peewee.PeeweeException, ValueError) as exc:
            logger.exception('Could not load slash player leaderboard')
            return await interaction.followup.send(
                f'Could not load the player leaderboard: {exc}',
                ephemeral=True,
            )

        view = leaderboard_v2.PlayerLeaderboardWorkspace(
            guild_id=interaction.guild.id,
            requester_id=interaction.user.id,
            result=result,
            loader=self._load_player_leaderboard,
            active_cutoff=settings.date_cutoff,
        )
        view.message = await interaction.edit_original_response(view=view)

    @settings.in_bot_channel_strict()
    @commands.command(aliases=['recent', 'active', 'lbactivealltime'], hidden=True)
    @commands.cooldown(2, 30, commands.BucketType.channel)
    async def lbrecent(self, ctx):
        """ Display most active recent players

        Alternative command is `[p]lbactivealltime`
        """
        request = self._activity_leaderboard_request(
            guild_id=ctx.guild.id,
            invoked_with=ctx.invoked_with,
        )
        async with ctx.typing():
            try:
                result = await self._load_activity_leaderboard(request)
            except (peewee.PeeweeException, ValueError) as exc:
                logger.exception('Could not load activity leaderboard')
                return await ctx.send(
                    f'Could not load the activity leaderboard: {exc}'
                )

        await utilities.paginate(
            self.bot,
            ctx,
            title=(
                f'**{result.title}**\n'
                f'{result.total_players} players'
            ),
            message_list=self._activity_leaderboard_entries(result),
            page_start=0,
            page_end=10,
            page_size=10,
        )

    @leaderboard_group.command(
        name='activity',
        description='View recent server or all-time global game activity.',
    )
    @discord.app_commands.describe(
        view='Choose the complete scope and time window.',
    )
    @discord.app_commands.choices(
        view=[
            discord.app_commands.Choice(
                name='This server — past 30 days',
                value='server-30-days',
            ),
            discord.app_commands.Choice(
                name='Global — all time',
                value='global-all-time',
            ),
        ],
    )
    @discord.app_commands.checks.cooldown(
        2,
        30.0,
        key=lambda interaction: interaction.channel_id,
    )
    async def activity_leaderboard_slash(
        self,
        interaction: discord.Interaction,
        view: str = 'server-30-days',
    ):
        """Typed native activity leaderboard with component pagination."""

        await interaction.response.defer()
        ctx = await commands.Context.from_interaction(interaction)
        ctx.prefix = settings.guild_setting(
            interaction.guild.id,
            'command_prefix',
        )
        ctx.invoked_with = (
            'lbactivealltime'
            if view == 'global-all-time'
            else 'lbrecent'
        )
        if not await self.lbrecent.can_run(ctx):
            return

        request = leaderboard_workers.ActivityLeaderboardRequest(
            guild_id=interaction.guild.id,
            view=view,
            recent_cutoff=(
                datetime.datetime.now()
                - datetime.timedelta(days=30)
            ),
        )
        try:
            result = await self._load_activity_leaderboard(request)
        except (peewee.PeeweeException, ValueError) as exc:
            logger.exception('Could not load slash activity leaderboard')
            return await interaction.followup.send(
                f'Could not load the activity leaderboard: {exc}',
                ephemeral=True,
            )

        view_control = leaderboard_v2.ActivityLeaderboardWorkspace(
            requester_id=interaction.user.id,
            result=result,
        )
        view_control.message = await interaction.edit_original_response(
            view=view_control,
        )

    @settings.in_bot_channel_strict()
    @settings.guild_has_setting(setting_name='allow_teams')
    @commands.command(aliases=['teamlb', 'lbteamjr'])
    @commands.cooldown(2, 30, commands.BucketType.channel)
    async def lbteam(self, ctx, *, arg: str = None):
        """display team leaderboard

        Examples:
        `[p]lbteam` - Default team leaderboard, which resets occasionally
        `[p]lbteam silver` - Team leaderboard only including teams in the Silver league tier.
        `[p]lbteam old` - Include old (archived) teams in the leaderboard.
        `[p]lbteamjr` - Display team leaderboard for Junior teams
        """
        args = arg.lower().split() if arg else []
        alltime = False  # Removed option to show pre-reset ELO during refactor May 2024
        
        tier_number, tier_name, tier_string = None, None, ''
        archived_arg = (Team.is_archived == 0)
        footer_message = ''

        if 'old' in args:
            archived_arg = (True)

        remaining_args = [arg for arg in args if arg not in ['old']]

        if len(remaining_args) > 0:
            try:
                tier_number, tier_name = settings.tier_lookup(remaining_args[0])
                tier_string = f' - {tier_name} Tier '
            except exceptions.NoMatches as e:
                return await ctx.send(f'Could not match "**{remaining_args[0]}**" to the name or number of a League tier. See `{ctx.prefix}help {ctx.invoked_with}` for usage examples.')

        embed = discord.Embed(title=f'**Team Leaderboard{tier_string}**')
        fig, ax = plt.subplots(figsize=(12, 8))
        plt.style.use('default')
        fig.suptitle('Team ELO History', fontsize=16)
        fig.autofmt_xdate()

        guild_check = settings.server_ids['polychampions'] if ctx.guild.id == settings.server_ids['test'] else ctx.guild.id

        if tier_number:
            query = Team.select().where(
                (Team.is_hidden == 0) & (archived_arg) & 
                (Team.guild_id == guild_check) & (Team.league_tier == tier_number)
            ).order_by(-Team.elo)
        else:
            query = Team.select().where(
                (Team.is_hidden == 0) & (archived_arg) &
                (Team.guild_id == guild_check) & (Team.league_tier.is_null(False))
            ).order_by(-Team.elo)

        async with ctx.typing():
            for counter, team in enumerate(query):
                if counter > 24:
                    footer_message = f'Only first 25 teams shown. You can specify a tier, example: {ctx.prefix}lb platinum'
                    continue
                team_role = discord.utils.get(ctx.guild.roles, name=team.name)
                if not team_role:
                    logger.error(f'Could not find matching role for team {team.name}')
                    continue
                member_count = 0
                mia_role = discord.utils.get(ctx.guild.roles, name=settings.guild_setting(ctx.guild.id, 'inactive_role'))
                for team_member in team_role.members:
                    if mia_role and mia_role in team_member.roles:
                        continue
                    member_count += 1
                team_name_str = f'**{team.name}**   ({member_count})'  # Show team name with number of members without MIA role
                wins, losses = team.get_record(alltime=alltime)

                elo = team.elo_alltime if alltime else team.elo
                embed.add_field(name=f'{team.emoji} {(counter + 1):>3}. {team_name_str}\n`ELO: {elo:<5} W {wins} / L {losses}`', value='\u200b', inline=False)

                team_elo_history_query = (GameSide
                        .select(Game.completed_ts, (GameSide.team_elo_after_game_alltime if alltime else GameSide.team_elo_after_game).alias('elo'))
                        .join(Game)
                        .where((GameSide.team_id == team.id) & ((GameSide.team_elo_after_game_alltime if alltime else GameSide.team_elo_after_game).is_null(False)))
                        .order_by(Game.completed_ts))

                if team_elo_history_query:
                    team_elo_history = pd.DataFrame(team_elo_history_query.dicts())
                    team_elo_history_resampled = team_elo_history.set_index('completed_ts').resample('D').mean().interpolate().reset_index()
                    filter_length = max(int(len(team_elo_history_resampled.index) / 3), 1)
                    filter_length = filter_length if filter_length % 2 != 0 else filter_length - 1
                    poly_order = 2 if filter_length > 2 else 0

                    plt.plot(team_elo_history['completed_ts'],
                                team_elo_history['elo'],
                                'o', markersize=3, alpha=.05, color=str(team_role.color))

                    plt.plot(team_elo_history_resampled['completed_ts'],
                                signal.savgol_filter(team_elo_history_resampled['elo'].values, filter_length, poly_order),
                                '-', linewidth=2, label=team.name, color=str(team_role.color))

        ax.yaxis.grid()

        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        ax.spines['left'].set_visible(False)

        plt.legend(loc="best")

        plt.savefig('graph.png', transparent=False)
        plt.close(fig)

        embed.set_image(url='attachment://graph.png')

        with open('graph.png', 'rb') as f:
            file = io.BytesIO(f.read())

        image = discord.File(file, filename='graph.png')

        if footer_message:
            embed.set_footer(text=footer_message)
        await ctx.send(embed=embed, file=image)

    @settings.in_bot_channel_strict()
    @settings.guild_has_setting(setting_name='allow_teams')
    @commands.command(aliases=['squadlb'])
    @commands.cooldown(2, 20, commands.BucketType.channel)
    async def lbsquad(self, ctx, *, filters: str = ''):
        """Display squad leaderboard

        A squad is any combination of players that have completed at least two games together.
        To set a squad name see `[p]help squadname`

        **Examples:**
        `[p]lbsquad` - Current leaderboard. Squads who have not played a game in 365 days are not included.
        `[p]lbsquad alltime` - Alltime leaderboard.
        """

        request = self._squad_leaderboard_request(
            guild_id=ctx.guild.id,
            filters=filters,
        )
        async with ctx.typing():
            try:
                result = await self._load_squad_leaderboard(request)
            except (peewee.PeeweeException, ValueError) as exc:
                logger.exception('Could not load squad leaderboard')
                return await ctx.send(
                    f'Could not load the squad leaderboard: {exc}'
                )

        await utilities.paginate(
            self.bot,
            ctx,
            title=(
                f'**{result.title}**\n'
                f'{result.total_squads} ranked squads'
            ),
            message_list=self._squad_leaderboard_entries(result),
            page_start=0,
            page_end=10,
            page_size=10,
        )

    @leaderboard_group.command(
        name='squads',
        description='View current or all-time squad rankings.',
    )
    @discord.app_commands.describe(
        period='Use current eligibility or include all squad history.',
    )
    @discord.app_commands.choices(
        period=[
            discord.app_commands.Choice(
                name='Current eligibility',
                value='current',
            ),
            discord.app_commands.Choice(
                name='All time',
                value='all-time',
            ),
        ],
    )
    @discord.app_commands.checks.cooldown(
        2,
        20.0,
        key=lambda interaction: interaction.channel_id,
    )
    async def squad_leaderboard_slash(
        self,
        interaction: discord.Interaction,
        period: str = 'current',
    ):
        """Typed native squad leaderboard with component pagination."""

        await interaction.response.defer()
        ctx = await commands.Context.from_interaction(interaction)
        ctx.prefix = settings.guild_setting(
            interaction.guild.id,
            'command_prefix',
        )
        ctx.invoked_with = 'lbsquad'
        if not await self.lbsquad.can_run(ctx):
            return

        request = leaderboard_workers.SquadLeaderboardRequest(
            guild_id=interaction.guild.id,
            period=period,
            active_cutoff=settings.date_cutoff,
        )
        try:
            result = await self._load_squad_leaderboard(request)
        except (peewee.PeeweeException, ValueError) as exc:
            logger.exception('Could not load slash squad leaderboard')
            return await interaction.followup.send(
                f'Could not load the squad leaderboard: {exc}',
                ephemeral=True,
            )

        view = leaderboard_views.SquadLeaderboardView(
            result,
            requester_id=interaction.user.id,
        )
        view.message = await interaction.edit_original_response(
            embed=leaderboard_views.squad_leaderboard_embed(result, 0),
            view=view,
        )

    @settings.in_bot_channel()
    @settings.guild_has_setting(setting_name='allow_teams')
    @commands.command(brief='Set a squad name', usage='squad_id New Squad Name', hidden=True)
    async def squadname(self, ctx, *, args=None):
        """Set a name for your squad

        **Examples:**
        `[p]squadname 5 The Desperados` - Set a name for squad 5
        `[p]squadname 5 None` - Delete an existing name
        """

        args = args.split() if args else []
        usage = f'**Example**: `{ctx.prefix}{ctx.invoked_with} 500 The Super Cool Squad`'
        if not args:
            return await ctx.send(f'No squad ID number supplied. You can use `{ctx.prefix}squad` or `{ctx.prefix}lbsquad` to look up squad IDs.\n{usage}')

        try:
            # Argument is an int, so show squad by ID
            squad_id = int(args[0])
            squad = Squad.get(id=squad_id)
            new_squad_name = discord.utils.escape_markdown(' '.join(args[1:])[:50])
        except ValueError:
            return await ctx.send(f'No squad ID number supplied. You can use `{ctx.prefix}squad` or `{ctx.prefix}lbsquad` to look up squad IDs.\n{usage}')
        except peewee.DoesNotExist:
            return await ctx.send(f'Squad with ID {squad_id} cannot be found.')

        logger.debug(f'Loaded squad {squad.id} for squadname command')

        if squad.guild_id != ctx.guild.id:
            return await ctx.send(f'Squad with ID {squad_id} is affiliated with a different Discord server.')

        if not squad.has_player(discord_id=ctx.author.id) and not settings.is_staff(ctx.author):
            return await ctx.send('A squad name can only be set by server staff or a member of that squad.')

        old_squad_name = squad.name if squad.name else '`None`'
        if not new_squad_name:
            return await ctx.send(f'No name given. The current name is *{old_squad_name}*\n{usage}')

        if new_squad_name.upper() == 'NONE':
            new_squad_name = ''
            new_squad_name_str = '`None`'
        else:
            new_squad_name_str = f'*{new_squad_name}*'

        squad.name = new_squad_name
        squad.save()

        models.GameLog.write(game_id=0, guild_id=ctx.guild.id, message=f'{models.GameLog.member_string(ctx.author)} set squadname of squad {squad.id} to {new_squad_name}')
        await ctx.send(f'Squad name for {squad.id} set to {new_squad_name_str}.')

    @settings.in_bot_channel()
    @settings.guild_has_setting(setting_name='allow_teams')
    @commands.command(brief='Find squads or see details on a squad', usage='player1 [player2] [player3]', aliases=['squads'])
    async def squad(self, ctx, *args):
        """Find squads with specific players, or see details on a squad

        A squad is any combination of players that have completed at least two games together.
        To set a squad name see `[p]help squadname`

        **Examples:**
        `[p]squad 5` - details on squad 5
        `[p]squad Nelluk` - squads containing Nelluk
        `[p]squad Nelluk jd` - squad containing both players
        """
        if not args:
            return await ctx.send(f'Use `{ctx.prefix}{ctx.invoked_with} player [player2]` to search for squads by membership, or `{ctx.prefix}lbsquad` for the squad leaderboard.')
        try:
            # Argument is an int, so show squad by ID
            squad_id = int(''.join(args))
            squad = Squad.get(id=squad_id)
        except ValueError:
            squad_id = None
            # Args is not an int, which means search by game name
        except peewee.DoesNotExist:
            return await ctx.send(f'Squad with ID {squad_id} cannot be found.')

        if squad_id is None:
            # Search by player names
            squad_players = []
            for p_name in args:

                try:
                    squad_players.append(Player.get_or_except(p_name, guild_id=ctx.guild.id))
                except exceptions.NoSingleMatch as e:
                    return await ctx.send(e)

            squad_list = Squad.get_all_matching_squads(squad_players, guild_id=ctx.guild.id)
            if len(squad_list) == 0:
                return await ctx.send(f'Found no squads containing players: {" / ".join([p.name for p in squad_players])}')
            if len(squad_list) > 1:
                # more than one match, so display a paginating list
                squadlist = []
                for squadside in squad_list[:50]:
                    squad = squadside.squad
                    wins, losses = squad.get_record()
                    squad_name_str = f' - *{squad.name}*\n' if squad.name else ' - '
                    squadlist.append(
                        (f'`#{squad.id:>3}`{squad_name_str}{" / ".join(squad.get_names()):40}', f'`(ELO: {squad.elo}) W {wins} / L {losses}`')
                    )
                await utilities.paginate(self.bot, ctx, title=f'Found {len(squad_list)} matches. Try `{ctx.prefix}squad #`:', message_list=squadlist, page_start=0, page_end=10, page_size=10)
                return

            # Exact matching squad found by player name
            squad = squad_list[0].squad

        if squad.guild_id != ctx.guild.id:
            return await ctx.send(f'Squad with ID {squad_id} is affiliated with a different Discord server.')

        wins, losses = squad.get_record()
        rank, lb_length = squad.leaderboard_rank(settings.date_cutoff)

        if rank is None:
            rank_str = 'Unranked'
        else:
            rank_str = f'{rank} of {lb_length}'

        names_with_emoji = [f'{p.team.emoji} **{p.name}**' if p.team is not None else f'**{p.name}**' for p in squad.get_members()]

        squad_name_str = f'\n*{squad.name}*' if squad.name else ''
        embed = discord.Embed(title=f'Squad card for Squad {squad.id}{squad_name_str}', description=f'{"  /  ".join(names_with_emoji)}'[:2048])
        embed.add_field(name='Results', value=f'ELO: {squad.elo},  W {wins} / L {losses}', inline=True)
        embed.add_field(name='Ranking', value=rank_str, inline=True)
        recent_games = GameSide.select(Game).join(Game).where(
            (GameSide.squad == squad)
        ).order_by(-Game.date)

        embed.add_field(value='\u200b', name='Most recent games', inline=False)
        game_list = utilities.summarize_game_list(recent_games[:10])

        for game, result in game_list:
            embed.add_field(name=game, value=result, inline=False)

        await ctx.send(embed=embed)

    async def _load_player_workspace(
        self,
        request: player_workers.PlayerWorkspaceRequest,
    ) -> player_workers.PlayerWorkspaceSnapshot:
        return await player_workers.run_player_workspace(request)

    @staticmethod
    def _game_search_requester_values(member):
        roles = tuple(getattr(member, 'roles', ()) or ())
        try:
            level = settings.get_user_level(member)
        except AttributeError:
            # Lightweight offline interaction doubles do not carry the full
            # Discord.Member surface. Real invocations always do.
            level = 0
        return {
            'requester_level': level,
            'requester_role_ids': tuple(role.id for role in roles),
            'requester_name': getattr(member, 'name', ''),
            'requester_nick': getattr(member, 'nick', None),
            'staff': settings.is_staff(member),
        }

    async def _load_game_search(
        self,
        request: game_search_workers.GameSearchRequest,
    ) -> game_search_workers.GameSearchSnapshot:
        return await asyncio.wait_for(
            game_search_workers.run_game_search(request),
            timeout=20.0,
        )

    async def _load_game_detail(
        self,
        request: game_detail_workers.GameDetailRequest,
    ) -> game_detail_workers.GameDetailSnapshot:
        return await asyncio.wait_for(
            game_detail_workers.run_game_detail(request),
            timeout=20.0,
        )

    def _game_detail_prefix(self, target, guild, *, slash: bool) -> str:
        """Resolve prefix configuration on the event-loop/display side."""

        if not slash:
            target_prefix = getattr(target, 'prefix', None)
            if isinstance(target_prefix, str) and target_prefix:
                return target_prefix
        try:
            return settings.guild_setting(
                guild_id=guild.id,
                setting_name='command_prefix',
            )
        except exceptions.CheckFailedError:
            return settings.guild_setting(
                guild_id=None,
                setting_name='command_prefix',
            )

    def _game_detail_error_message(self, error) -> str:
        if getattr(error, 'code', None) != 'cross_guild_pending':
            return str(error) or 'Could not load that game.'

        source_guild_id = getattr(error, 'source_guild_id', None)
        if source_guild_id is None:
            return str(error) or 'Could not load that game.'
        try:
            server_name = settings.guild_setting(
                guild_id=source_guild_id,
                setting_name='display_name',
            )
        except exceptions.CheckFailedError:
            try:
                server_name = settings.guild_setting(
                    guild_id=None,
                    setting_name='display_name',
                )
            except exceptions.CheckFailedError:
                server_name = f'guild {source_guild_id}'
        return f'{error} __{server_name}__.'

    async def _load_pending_game_card(
        self,
        interaction,
        *,
        guild,
        channel_id: int,
        game_id: int,
        prefix: str,
    ) -> game_detail_actions.PendingGameCardPayload:
        """Reload a card through the bounded immutable game-detail worker."""

        request = game_detail_workers.GameDetailRequest(
            guild_id=guild.id,
            channel_id=channel_id,
            requester_discord_id=interaction.user.id,
            game_id=game_id,
        )
        snapshot = await self._load_game_detail(request)
        display = game_detail_views.resolve_display(
            snapshot,
            guild=guild,
            bot=self.bot,
            prefix=prefix,
            join_emoji=getattr(settings, 'emoji_join_game', ''),
        )
        return game_detail_actions.PendingGameCardPayload(
            snapshot=snapshot,
            rendered=game_detail_views.render_classic_game_detail(display),
        )

    async def _pending_card_join(
        self,
        interaction,
        *,
        game_id: int,
        prefix: str,
        side_arg: str | None,
    ) -> bool:
        """Delegate a card join to the established join worker/presenter."""

        if not await self._native_pending_game_channel_allowed(interaction):
            return False
        matchmaking_cog = self.bot.get_cog('matchmaking')
        if matchmaking_cog is None:
            await interaction.followup.send(
                'The join-game command handler is unavailable.',
                ephemeral=True,
            )
            return False
        try:
            result = await matchmaking_cog.execute_join(
                game_id=game_id,
                member=interaction.user,
                author_member=interaction.user,
                side_arg=side_arg,
                invoked_with='/game show Join',
                notification_member_id=interaction.user.id,
                prefix=prefix,
            )
        except game_join_workers.PendingGameJoinValidationError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return False
        except peewee.PeeweeException:
            logger.exception('Database failure in pending-card join %s', game_id)
            await interaction.followup.send(
                'The game could not be changed because the database operation '
                'failed. No public Discord effects were made.',
                ephemeral=True,
            )
            return False
        except Exception:
            logger.exception('Unexpected failure in pending-card join %s', game_id)
            await interaction.followup.send(
                'The game could not be changed. No public Discord effects were '
                'made.',
                ephemeral=True,
            )
            return False

        await self._publish_native_join_result(
            interaction,
            result,
            member=interaction.user,
            prefix=prefix,
            publish_card=False,
        )
        return True

    async def _publish_native_leave_result(
        self,
        interaction: discord.Interaction,
        result: game_join_workers.LeaveResult,
    ) -> None:
        """Publish an already-committed leave without creating a second card."""

        public_send = lambda content: interaction.followup.send(
            content,
            ephemeral=False,
        )
        if result.host_warning:
            await game_join_leave.send_post_commit_message(
                public_send,
                result.host_warning,
                game_id=result.game_id,
                effect='host-leave warning',
            )
        await game_join_leave.send_post_commit_message(
            public_send,
            result.message,
            game_id=result.game_id,
            effect='leave output',
        )

    async def _pending_card_leave(
        self,
        interaction,
        *,
        game_id: int,
        prefix: str,
    ) -> bool:
        """Delegate a card leave to the established leave worker/presenter."""

        if not await self._native_pending_game_channel_allowed(interaction):
            return False
        matchmaking_cog = self.bot.get_cog('matchmaking')
        if matchmaking_cog is None:
            await interaction.followup.send(
                'The leave-game command handler is unavailable.',
                ephemeral=True,
            )
            return False
        try:
            result = await matchmaking_cog.execute_leave(
                game_id=game_id,
                member=interaction.user,
                author_member=interaction.user,
                invoked_with='/game show Leave',
                prefix=prefix,
            )
        except game_join_workers.PendingGameLeaveValidationError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return False
        except peewee.PeeweeException:
            logger.exception('Database failure in pending-card leave %s', game_id)
            await interaction.followup.send(
                'The game could not be changed because the database operation '
                'failed. No public Discord effects were made.',
                ephemeral=True,
            )
            return False
        except Exception:
            logger.exception('Unexpected failure in pending-card leave %s', game_id)
            await interaction.followup.send(
                'The game could not be changed because the game service failed.',
                ephemeral=True,
            )
            return False

        await self._publish_native_leave_result(interaction, result)
        return True

    async def _pending_card_delete_prepare(
        self,
        interaction,
        *,
        game_id: int,
        prefix: str,
    ) -> bool:
        """Revalidate card deletion authorization before confirmation."""

        if not await self._native_pending_game_channel_allowed(interaction):
            return False
        try:
            request = game_deletion.build_request(
                game_id=game_id,
                member=interaction.user,
                guild_id=interaction.guild.id,
                prefix=prefix,
                invoked_with='/game show Delete',
            )
            classification = await game_deletion.authorize_delete(request)
            if classification.state != game_deletion.game_deletion_workers.PENDING:
                await interaction.followup.send(
                    'This game is no longer pending. Refresh the card or run '
                    '`/game show` again.',
                    ephemeral=True,
                )
                return False
        except game_deletion.GameDeletionValidationError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return False
        except peewee.PeeweeException:
            logger.exception(
                'Database failure preparing pending-card deletion %s',
                game_id,
            )
            await interaction.followup.send(
                'The game could not be deleted because the database operation '
                'failed. No public Discord effects were made.',
                ephemeral=True,
            )
            return False
        except Exception:
            logger.exception(
                'Unexpected failure preparing pending-card deletion %s',
                game_id,
            )
            await interaction.followup.send(
                'The game could not be deleted. No public Discord effects '
                'were made.',
                ephemeral=True,
            )
            return False
        return True

    async def _pending_card_delete(
        self,
        interaction,
        *,
        game_id: int,
        prefix: str,
    ) -> bool:
        """Run the shared deletion service and publish committed effects."""

        if not await self._native_pending_game_channel_allowed(interaction):
            return False
        try:
            request = game_deletion.build_request(
                game_id=game_id,
                member=interaction.user,
                guild_id=interaction.guild.id,
                prefix=prefix,
                invoked_with='/game show Delete',
            )
            result = await game_deletion.delete_game(request)
            await game_deletion.publish_result(
                result,
                send=lambda content: interaction.followup.send(
                    content,
                    ephemeral=False,
                ),
                guild=interaction.guild,
                bot=self.bot,
                prefix=prefix,
            )
        except EloJobConflict as exc:
            active_job = exc.active_job
            await interaction.followup.send(
                f':warning: ELO operation `{active_job.operation}` for game '
                f'`{active_job.game_id or "all"}` is already running. '
                'Please try again later.',
                ephemeral=True,
            )
            return False
        except game_deletion.GameDeletionValidationError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return False
        except peewee.PeeweeException:
            logger.exception(
                'Database failure deleting pending-card game %s',
                game_id,
            )
            await interaction.followup.send(
                'Game deletion failed and rolled back. No Discord channel '
                'updates were made.',
                ephemeral=True,
            )
            return False
        except Exception:
            logger.exception(
                'Unexpected failure deleting pending-card game %s',
                game_id,
            )
            await interaction.followup.send(
                'Game deletion failed. No Discord channel updates were made.',
                ephemeral=True,
            )
            return False
        return True

    async def _pending_card_winner(
        self,
        interaction,
        *,
        game_id: int,
        prefix: str,
        winning_side_id: int,
        winner_label: str,
    ) -> bool:
        """Route a card winner claim through the shared win application service."""

        if not await self._native_winner_game_channel_allowed(interaction):
            return False

        try:
            request = game_win.build_request(
                game_id=game_id,
                member=interaction.user,
                guild_id=interaction.guild.id,
                prefix=prefix,
                winner_text=winner_label,
                winning_side_id=winning_side_id,
                invoked_with='/game show Declare Winner',
            )

            async def public_send(content):
                await interaction.followup.send(content, ephemeral=False)

            async def error_send(content):
                await interaction.followup.send(content, ephemeral=True)

            result = await game_win.run_win(
                request,
                guild=interaction.guild,
                current_channel=getattr(interaction, 'channel', None),
                send_public=public_send,
                send_error=error_send,
                post_win_publisher=post_win_messaging,
                acknowledged=True,
            )
        except peewee.PeeweeException:
            logger.exception(
                'Database failure in in-progress card winner %s',
                game_id,
            )
            await interaction.followup.send(
                'The winner could not be recorded because the database '
                'operation failed. No public Discord effects were made.',
                ephemeral=True,
            )
            return False
        except Exception:
            logger.exception(
                'Unexpected failure in in-progress card winner %s',
                game_id,
            )
            await interaction.followup.send(
                'The winner could not be recorded. No public Discord effects '
                'were made.',
                ephemeral=True,
            )
            return False
        return bool(
            result is not None
            and getattr(result, 'public_effects_published', False)
        )

    async def _pending_card_start(
        self,
        interaction,
        *,
        guild,
        game_id: int,
        prefix: str,
        name: str,
    ) -> bool:
        """Delegate a card start to the established start worker/presenter."""

        if not await self._native_pending_game_channel_allowed(interaction):
            return False
        matchmaking_cog = self.bot.get_cog('matchmaking')
        if matchmaking_cog is None:
            await interaction.followup.send(
                'The start-game command handler is unavailable.',
                ephemeral=True,
            )
            return False
        try:
            result = await matchmaking_cog.execute_start(
                game_id=game_id,
                guild=guild,
                requester=interaction.user,
                name=name,
                prefix=prefix,
                invoked_with='/game show Start',
            )
        except game_start_workers.GameStartValidationError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return False
        except peewee.PeeweeException:
            logger.exception('Database failure in pending-card start %s', game_id)
            await interaction.followup.send(
                'The game could not be started because the database operation '
                'failed. No public Discord effects were made.',
                ephemeral=True,
            )
            return False
        except exceptions.CheckFailedError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return False
        except Exception:
            logger.exception('Unexpected failure in pending-card start %s', game_id)
            await interaction.followup.send(
                'The game could not be started. No public Discord effects were '
                'made.',
                ephemeral=True,
            )
            return False

        await game_start.publish_start_result(
            result,
            output_context=game_start.native_output_context(
                interaction,
                prefix=prefix,
            ),
            guild=guild,
            prefix=prefix,
            bot_guilds=getattr(settings.bot, 'guilds', ()),
        )
        return True

    def _pending_game_card_view(
        self,
        *,
        snapshot: game_detail_workers.GameDetailSnapshot,
        guild,
        channel_id: int,
        prefix: str,
    ) -> game_detail_actions.PendingGameCardView | None:
        if not (
            snapshot.is_pending
            or game_detail_actions.winner_action_eligible(snapshot)
        ):
            return None

        async def load_card(interaction):
            return await self._load_pending_game_card(
                interaction,
                guild=guild,
                channel_id=channel_id,
                game_id=snapshot.game_id,
                prefix=prefix,
            )

        async def on_join(interaction, side_arg):
            return await self._pending_card_join(
                interaction,
                game_id=snapshot.game_id,
                prefix=prefix,
                side_arg=side_arg,
            )

        async def on_leave(interaction):
            return await self._pending_card_leave(
                interaction,
                game_id=snapshot.game_id,
                prefix=prefix,
            )

        async def on_delete_prepare(interaction):
            return await self._pending_card_delete_prepare(
                interaction,
                game_id=snapshot.game_id,
                prefix=prefix,
            )

        async def on_delete(interaction):
            return await self._pending_card_delete(
                interaction,
                game_id=snapshot.game_id,
                prefix=prefix,
            )

        async def on_start(interaction, name):
            return await self._pending_card_start(
                interaction,
                guild=guild,
                game_id=snapshot.game_id,
                prefix=prefix,
                name=name,
            )

        async def on_winner(interaction, winning_side_id, winner_label):
            return await self._pending_card_winner(
                interaction,
                game_id=snapshot.game_id,
                prefix=prefix,
                winning_side_id=winning_side_id,
                winner_label=winner_label,
            )

        return game_detail_actions.PendingGameCardView(
            snapshot=snapshot,
            load_card=load_card,
            on_join=on_join,
            on_leave=on_leave,
            on_start=on_start,
            on_delete_prepare=on_delete_prepare,
            on_delete=on_delete,
            on_winner=on_winner,
        )

    async def _send_game_detail(
        self,
        target,
        *,
        guild,
        requester_id: int,
        channel_id: int,
        game_id: int | None,
        slash: bool = False,
    ) -> bool:
        request = game_detail_workers.GameDetailRequest(
            guild_id=guild.id,
            channel_id=channel_id,
            requester_discord_id=requester_id,
            game_id=game_id,
        )
        try:
            snapshot = await self._load_game_detail(request)
        except (
            game_detail_workers.GameDetailError,
            peewee.PeeweeException,
            asyncio.TimeoutError,
            ValueError,
        ) as exc:
            if isinstance(exc, asyncio.TimeoutError):
                message = 'Game detail lookup timed out. Please try again.'
            else:
                message = self._game_detail_error_message(exc)
            if slash:
                await target.followup.send(message, ephemeral=True)
            else:
                await target.send(message)
            return False
        except Exception:
            logger.exception('Unexpected failure loading game detail')
            message = 'Could not load that game. Please try again.'
            if slash:
                await target.followup.send(message, ephemeral=True)
            else:
                await target.send(message)
            return False

        prefix = self._game_detail_prefix(target, guild, slash=slash)
        display = game_detail_views.resolve_display(
            snapshot,
            guild=guild,
            bot=self.bot,
            prefix=prefix,
            join_emoji=getattr(settings, 'emoji_join_game', ''),
        )
        classic = game_detail_views.render_classic_game_detail(display)
        kwargs = {
            'embed': classic.embed,
            'content': classic.content,
        }
        file = classic.new_file()
        if file is not None:
            kwargs['file'] = file
        view = (
            self._pending_game_card_view(
                snapshot=snapshot,
                guild=guild,
                channel_id=channel_id,
                prefix=prefix,
            )
            if slash
            else None
        )
        if view is not None:
            kwargs['view'] = view
        if slash:
            message = await target.edit_original_response(**kwargs)
        else:
            message = await target.send(**kwargs)
        if view is not None:
            view.message = message
            if (
                snapshot.is_pending
                and snapshot.status_label != 'Expired open game'
                and snapshot.pending_join_available
            ):
                try:
                    await game_open.add_join_reaction(message)
                except Exception:
                    logger.exception(
                        'Could not seed the join reaction on pending game %s '
                        'card; retain button and prefix fallback',
                        snapshot.game_id,
                    )
        return True

    async def _send_game_search_workspace(
        self,
        ctx,
        *,
        query: str,
        key: game_search_workers.GameSearchKey,
    ) -> bool:
        request_kwargs = {
            'guild_id': ctx.guild.id,
            'requester_discord_id': ctx.author.id,
            'query': query,
            **self._game_search_requester_values(ctx.author),
        }

        async def loader(filter_key):
            return await self._load_game_search(
                game_search_workers.GameSearchRequest(
                    **request_kwargs,
                    key=filter_key,
                )
            )

        try:
            snapshot = await loader(key)
        except (game_search_workers.GameSearchError,
                peewee.PeeweeException,
                asyncio.TimeoutError,
                ValueError) as exc:
            await ctx.send(str(exc) or 'Game search timed out.')
            return False
        view = game_search_views.GameSearchWorkspace(
            requester_id=ctx.author.id,
            initial_result=snapshot,
            loader=loader,
            can_view_unconfirmed=request_kwargs['staff'],
        )
        view.message = await ctx.send(view=view)
        return True

    async def _send_player_workspace(
        self,
        ctx,
        *,
        request: player_workers.PlayerWorkspaceRequest,
        initial_section: str = 'overview',
        completed_filter: str = 'all',
    ) -> bool:
        try:
            snapshot = await self._load_player_workspace(request)
        except (player_workers.PlayerNotFound,
                player_workers.AmbiguousPlayer,
                peewee.PeeweeException,
                ValueError) as exc:
            await ctx.send(str(exc))
            return False
        view = player_views.PlayerWorkspace(
            requester_id=ctx.author.id,
            snapshot=snapshot,
            initial_section=initial_section,
            completed_filter=completed_filter,
            can_edit=(
                ctx.author.id == snapshot.discord_id
                or settings.is_staff(ctx.author)
            ),
        )
        view.message = await ctx.send(view=view)
        return True

    @player_group.command(
        name='show',
        description='Open a player profile and game-history workspace.',
    )
    @discord.app_commands.describe(
        member='Player to view; defaults to you.',
    )
    async def player_show_slash(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ):
        await interaction.response.defer()
        ctx = await commands.Context.from_interaction(interaction)
        ctx.prefix = settings.guild_setting(
            interaction.guild.id,
            'command_prefix',
        )
        ctx.invoked_with = 'player'
        if not await self.player.can_run(ctx):
            return
        target = member or interaction.user
        request = player_workers.PlayerWorkspaceRequest(
            guild_id=interaction.guild.id,
            discord_id=target.id,
        )
        try:
            snapshot = await self._load_player_workspace(request)
        except (player_workers.PlayerNotFound,
                player_workers.AmbiguousPlayer,
                peewee.PeeweeException,
                ValueError) as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        view = player_views.PlayerWorkspace(
            requester_id=interaction.user.id,
            snapshot=snapshot,
            can_edit=(
                interaction.user.id == snapshot.discord_id
                or settings.is_staff(interaction.user)
            ),
        )
        view.message = await interaction.edit_original_response(view=view)

    @settings.in_bot_channel()
    @commands.command(brief='See details on a player', usage='player_name', aliases=['elo', 'rank'])
    async def player(self, ctx, *, args=None):
        """See your own player card or the card of another player
        This also will find results based on a game-code or in-game name, if set.

        **Examples**
        `[p]player` - See your own player card
        `[p]player Nelluk` - See Nelluk's card
        """
        # The hidden legacy ``alltime`` modifier now deep-links the Ratings
        # section, where both current-era and permanent ratings are visible.
        tokens = args.split() if args else []
        all_time = any(token.lower() == 'alltime' for token in tokens)
        if all_time:
            args = ' '.join(
                token for token in tokens if token.lower() != 'alltime'
            ).strip() or None
        request = player_workers.PlayerWorkspaceRequest(
            guild_id=ctx.guild.id,
            discord_id=ctx.author.id if not args else None,
            player_query=args,
        )
        async with ctx.typing():
            await self._send_player_workspace(
                ctx,
                request=request,
                initial_section='ratings' if all_time else 'overview',
            )

    @settings.in_bot_channel()
    @settings.guild_has_setting(setting_name='allow_teams')
    @commands.command(usage='team_name')
    async def team(self, ctx, *, team_string: str = None):
        """See details on a team
        **Example:**
        `[p]team Ronin`
        `[p]team Ronin completed` - Show count of all completed ranked games for each member of team, rather than default recent game count.
        """

        if not team_string:
            return await ctx.send(f'No team name supplied. Use `{ctx.prefix}lbteam` for the team leaderboard. **Example:** `{ctx.prefix}team Ronin`')

        if 'completed' in team_string:
            team_string = team_string.replace('completed', '').strip()
            completed_flag = True
        else:
            completed_flag = False

        try:
            team = Team.get_or_except(team_string, ctx.guild.id)
        except exceptions.NoSingleMatch:
            return await ctx.send(f'Couldn\'t find a team name matching *{discord.utils.escape_mentions(team_string)}*. Check spelling or be more specific. **Example:** `{ctx.prefix}team Ronin`')

        house_str = f'\nHouse {team.house.name} {team.house.emoji}' if team.house and team.house.name else ''
        embed = discord.Embed(title=f'Team card for **{team.name}** {team.emoji}{house_str}')
        team_role = discord.utils.get(ctx.guild.roles, name=team.name)
        mia_role = discord.utils.get(ctx.guild.roles, name=settings.guild_setting(ctx.guild.id, 'inactive_role'))
        # leader_role = discord.utils.get(ctx.guild.roles, name='Team Leader')
        # coleader_role = discord.utils.get(ctx.guild.roles, name='Team Co-Leader')
        member_stats = []
        leaders_list, coleaders_list, recruiters_list, captains_list = [], [], [], []
        image = None

        wins, losses = team.get_record(alltime=False)
        embed.add_field(name='Results', value=f'ELO: {team.elo}   Wins {wins} / Losses {losses}', inline=False)

        if team_role:
            async with ctx.typing():
                if ctx.guild.id == settings.server_ids['polychampions'] or ctx.guild.id == settings.server_ids['test']:
                    leaders_list, coleaders_list, recruiters_list, captains_list = get_team_leadership(team)
                    leaders_list = [member.mention for member in leaders_list]
                    coleaders_list = [member.mention for member in coleaders_list]
                    recruiters_list = [member.mention for member in recruiters_list]
                    captains_list = [member.mention for member in captains_list]

                if completed_flag:
                    header_str = '__Player - ELO - Ranking - Completed Games__'
                else:
                    header_str = '__Player - ELO - Ranking - Recent Games__'
                for member in team_role.members:
                    if mia_role and mia_role in member.roles:
                        continue
                        # skip members tagged @MIA

                    # Create a list of members - pull ELO score from database if they are registered, or with 0 ELO if they are not
                    p = Player.string_matches(player_string=str(member.id), guild_id=ctx.guild.id)
                    if len(p) == 0:
                        member_stats.append((member.name, 0, f'`{member.name[:23]:.<25}{"-":.<8}{"-":.<6}{"-":.<4}`'))
                    else:
                        wins, losses = p[0].get_record()
                        lb_rank = p[0].leaderboard_rank(date_cutoff=settings.date_cutoff)[0]
                        rank_str = f'#{lb_rank}' if lb_rank else '-'
                        if completed_flag:
                            games_played = p[0].completed_game_count()
                        else:
                            games_played = p[0].games_played(in_days=30).count()
                        member_stats.append(({p[0].discord_member.name}, games_played, f'`{p[0].discord_member.name[:23]:.<25}{p[0].elo_moonrise:.<8}{rank_str:.<6}{games_played:.<4}`'))

                member_stats.sort(key=lambda tup: tup[1], reverse=True)     # sort the list descending by recent games played
                members_sorted = [str(x[2].replace(".", "\u200b ")) for x in member_stats[:50]]    # create list of strings like 'Nelluk  1277 #3  21'.
                # replacing '.' with "\u200b " (alternated zero width space with a normal space) so discord wont strip spaces

                members_str = "\n".join(members_sorted) if len(members_sorted) > 0 else '\u200b'
                embed.description = f'**Members({len(member_stats)})**\n{header_str}\n{members_str}'[:4000]
        else:
            await ctx.send(f':no_entry_sign: No matching discord role "{team.name}" could be found. Player membership cannot be detected.')

        if leaders_list:
            embed.add_field(name='**House Leader**', value=', '.join(leaders_list), inline=True)
        if coleaders_list:
            embed.add_field(name='**House Co-Leaders**', value=', '.join(coleaders_list), inline=True)
        if recruiters_list:
            embed.add_field(name='**Team Recruiters**', value=', '.join(recruiters_list), inline=True)
        if captains_list:
            embed.add_field(name='**Team Captains**', value=', '.join(captains_list), inline=True)
        team_logo = image_storage.set_entity_thumbnail(embed, 'team', team)

        embed.add_field(name='**Recent games**', value='\u200b', inline=False)

        recent_games = Game.search(team_filter=[team])

        game_list = utilities.summarize_game_list(recent_games[:5])

        for game, result in game_list:
            embed.add_field(name=game, value=result)

        alltime_team_elo_history_query = (GameSide
                .select(Game.completed_ts, GameSide.team_elo_after_game_alltime)
                .join(Game)
                .where((GameSide.team_id == team.id) & (GameSide.team_elo_after_game_alltime.is_null(False)))
                .order_by(Game.completed_ts))

        alltime_team_elo_history_dates = [l.completed_ts for l in alltime_team_elo_history_query.objects()]

        if alltime_team_elo_history_dates:
            alltime_team_elo_history_elos = [l.team_elo_after_game_alltime for l in alltime_team_elo_history_query.objects()]

            team_elo_history_query = (GameSide
                .select(Game.completed_ts, GameSide.team_elo_after_game)
                .join(Game)
                .where((GameSide.team_id == team.id) & (GameSide.team_elo_after_game.is_null(False)))
                .order_by(Game.completed_ts))

            team_elo_history_dates = [l.completed_ts for l in team_elo_history_query.objects()]
            team_elo_history_elos = [l.team_elo_after_game for l in team_elo_history_query.objects()]

            plt.style.use('default')

            plt.switch_backend('Agg')

            fig, ax = plt.subplots()
            fig.suptitle('ELO History (' + team.name + ')', fontsize=16)
            fig.autofmt_xdate()

            plt.plot(team_elo_history_dates, team_elo_history_elos, 'o', markersize=3, label=f'Since {settings.team_elo_reset_date}')
            plt.plot(alltime_team_elo_history_dates, alltime_team_elo_history_elos, 'o', markersize=3, label='Alltime')

            ax.yaxis.grid()
            ax.spines['top'].set_visible(False)
            ax.spines['right'].set_visible(False)
            ax.spines['left'].set_visible(False)

            plt.legend(loc="best")

            plt.savefig('graph.png', transparent=False)
            plt.close(fig)

            embed.set_image(url='attachment://graph.png')

            with open('graph.png', 'rb') as f:
                file = io.BytesIO(f.read())

            image = discord.File(file, filename='graph.png')

        files = []
        if image:
            files.append(image)
        if team_logo:
            files.append(team_logo.to_discord_file())
        if files:
            await ctx.send(files=files, embed=embed)
        else:
            await ctx.send(embed=embed)

    @commands.command(brief='Sets a Polytopia account name and registers user with the bot', usage='[user] polytopia_code', aliases=['steamname', 'setcode'])
    async def setname(self, ctx, *, args=None):
        """
        Sets your own Polytopia code, or allows a staff member to set a player's code. This also will register the player with the bot if not already.
        **Examples:**
        `[p]setname <Your In-Game Name Here>`
        `[p]steamname <Your Steam Name Here>`
        `[p]setname @Nelluk Nelluk` *Staff usage*
        `[p]setcode @Nelluk none` - Server staff can delete a code if it is invalid for some reason

        Also use `[p]steamname` and `[p]setcode` for setting Steam name or old-style friend code
        """
        args = args.split() if args else []
        if ctx.invoked_with == 'setcode':
            code_type = 'Polytopia Player ID'
            code_example = 'YOURCODEHERE'
            db_field = DiscordMember.polytopia_id
        elif ctx.invoked_with == 'steamname':
            code_type = 'Steam username'
            code_example = 'Your Steam Name'
            db_field = DiscordMember.name_steam
        elif ctx.invoked_with == 'setname':
            code_type = 'mobile username'
            code_example = 'Your Mobile Name'
            db_field = DiscordMember.polytopia_name
        if not args:
            return await ctx.send(f'**Usage:** `{ctx.prefix}{ctx.invoked_with} {code_example}`\nUse `{ctx.prefix}code` to quickly display your own code and in-game name.')

        m = utilities.string_to_user_id(args[0])
        if m:
            logger.debug(f'Third party use of {ctx.invoked_with}')
            # Staff member using command on third party
            if settings.is_staff(ctx.author) is False:
                logger.debug('insufficient user level')
                return await ctx.send('You do not have permission to set another player\'s name or code.')
            new_id = ' '.join(args[1:])
            target_string = str(m)
            log_by_str = f' by {models.GameLog.member_string(ctx.author)}'
        else:
            # Player using command on their own games
            new_id = ' '.join(args)
            target_string = str(ctx.author.id)
            log_by_str = ''

        # Try to find matching guild/server member
        # TODO: It would be good to be able to change the code of a player who is no longer a server member
        guild_matches = await utilities.get_guild_member(ctx, target_string)
        if len(guild_matches) == 0:
            return await ctx.send(f'Could not find any server member matching *{args[0]}*. Try specifying with an @Mention')
        elif len(guild_matches) > 1:
            return await ctx.send(f'Found {len(guild_matches)} server members matching *{args[0]}*. Try specifying with an @Mention')
        target_discord_member = guild_matches[0]

        if new_id.lower() == 'none' and settings.is_staff(ctx.author):
            new_id = None
        elif (len(new_id) != 16 or new_id.isalnum() is False) and ctx.invoked_with == 'setcode':
            # Very basic polytopia code sanity checking. Making sure it is 16-character alphanumeric.
            return await ctx.send(f'Polytopia code `{new_id}` does not appear to be a valid code. Copy your unique code from the **Profile** tab of the **Polytopia app**.')
        elif ctx.invoked_with == 'setname' and (new_id.upper().strip() == 'YOUR MOBILE NAME' or ('YOUR' in new_id.upper() and 'GAME' in new_id.upper() and 'NAME' in new_id.upper())):
            return await ctx.send(':warning: This name doesn\'t look right. You need to use *your* in-game name (`Multiplayer > Profile > Alias` in the Polytopia app)')
        elif ctx.invoked_with == 'steamname' and 'STEAM' in new_id.upper() and 'NAME' in new_id.upper():
            await ctx.send(':warning: This name doesn\'t look right. You need to use *your* Steam name.')

        _, team_list = Player.get_teams_of_players(guild_id=ctx.guild.id, list_of_players=[target_discord_member])

        player, created = Player.upsert(discord_id=target_discord_member.id,
                                        discord_name=target_discord_member.name,
                                        discord_nick=target_discord_member.nick,
                                        guild_id=ctx.guild.id,
                                        team=team_list[0])
        if ctx.invoked_with == 'setcode':
            player.discord_member.polytopia_id = new_id
            register_str = f'{code_type} `{player.discord_member.polytopia_id}`'
            warning_str = f':warning: Also set your mobile in-game name with `{ctx.prefix}setname Your Mobile Name` - This will be required soon.\n'
        elif ctx.invoked_with == 'steamname':
            player.discord_member.name_steam = discord.utils.escape_mentions(new_id[:200]) if new_id else None
            register_str = f'{code_type} `{player.discord_member.name_steam}`'
            warning_str = ''
        elif ctx.invoked_with == 'setname':
            player.discord_member.polytopia_name = discord.utils.escape_mentions(new_id[:200]) if new_id else None
            register_str = f'{code_type} `{player.discord_member.polytopia_name}`'
            warning_str = ''

        player.discord_member.save()

        models.GameLog.write(game_id=0, guild_id=0, message=f'{models.GameLog.member_string(player.discord_member)} {code_type} {"set" if created else "updated"} to `{new_id}` {log_by_str}')

        if created:
            await ctx.send(f'Player **{player.name}** added to system with {register_str} and ELO **{player.elo_moonrise}**\n{warning_str}'
                f'To find games to join use the `{ctx.prefix}games` command.')
        else:
            await ctx.send(f'Player **{player.name}** updated in system with {register_str}.')

        players_with_id = DiscordMember.select().where(db_field ** new_id)
        if players_with_id.count() > 1 and new_id:
            helper_role_name = settings.guild_setting(ctx.guild.id, 'helper_roles')[0]
            helper_role = discord.utils.get(ctx.guild.roles, name=helper_role_name)
            helper_role_str = f'someone with the {helper_role.mention} role' if helper_role else 'server staff'
            p_names = [f'<@{p.discord_id}> ({p.name})' for p in players_with_id]
            await ctx.send(':warning: This polytopia code is already entered in the database. '
                f'If you need help using this bot please contact {helper_role_str} or <@{settings.owner_id}>.\nDuplicated players: {", ".join(p_names)}')

    @commands.command(aliases=['code', 'getcode', 'name'], usage='player_name')
    async def getname(self, ctx, *, player_string: str = None):
        """Get game ID of a player
        Just returns the code and nothing else so it can easily be copied."""

        if not player_string:
            player_string = str(ctx.author.id)

        player_string_safe = discord.utils.escape_mentions(player_string)

        guild_matches = await utilities.get_guild_member(ctx, player_string)

        if len(guild_matches) == 0:
            try:
                game_id = int(player_string)
            except ValueError:
                return await ctx.send(f'Could not find any server member matching *{player_string_safe}*. Try specifying with an @Mention')

            return await ctx.send(f'Could not find any server member matching *{player_string_safe}*. For player codes for a game, try `{ctx.prefix}codes {game_id}`')

        elif len(guild_matches) > 1:
            player_matches = Player.string_matches(player_string=player_string, guild_id=ctx.guild.id)
            if len(player_matches) == 1:
                if player_matches[0].discord_member.polytopia_name:
                    in_game_name_str = f' (In-game name: **{player_matches[0].discord_member.polytopia_name}**)'
                else:
                    in_game_name_str = ''
                if player_matches[0].discord_member.name_steam:
                    in_game_name_str += f' (Steam name: **{player_matches[0].discord_member.name_steam}**)'
                await ctx.send(f'Found {len(guild_matches)} server members matching *{player_string_safe}*, but only **{player_matches[0].name}** {in_game_name_str} is registered.')
                return await ctx.send(player_matches[0].discord_member.polytopia_id or 'No mobile code set')

            return await ctx.send(f'Found {len(guild_matches)} server members matching *{player_string_safe}*. Try specifying with an @Mention or more characters.')
        target_discord_member = guild_matches[0]

        discord_member = DiscordMember.get_or_none(discord_id=target_discord_member.id)

        if discord_member:
            if discord_member.name_steam:
                in_game_name_str = f' (Steam name: **{discord_member.name_steam}**)'
            else:
                in_game_name_str = ''
            if discord_member.polytopia_id:
                in_game_name_str += f' (Old-style code: `{discord_member.polytopia_id}`)'
            await ctx.send(f'Mobile name for **{discord_member.name}**{in_game_name_str}:')
            return await ctx.send(discord_member.polytopia_name or 'None set')
        else:
            return await ctx.send(f'Member **{target_discord_member.name}** is not registered.\n'
                f'Register your own or in-game name with `{ctx.prefix}setname MOBILE NAME HERE` or `{ctx.prefix}steamname STEAM NAME HERE`')

    @commands.command(aliases=['names', 'codes', 'getcodes'], usage='game_id')
    @models.is_registered_member()
    async def getnames(self, ctx, *, arg=''):
        """Print all player names associated with a game ID
        The names will be printed on separate line for ease of copying, and in the order that players should be added to the game.
        **Examples:**
        `[p]getnames 1250` - Get all player codes for players in game 1250
        `[p]names` - Get player names for the game associated with the current channel
        """
        
        if arg:
            try:
                game_id = int(arg)
            except ValueError:
                game_id = None
        else:
            game_id = None

        inferred_game = None
        if not game_id:
            try:
                inferred_game = models.Game.by_channel_id(chan_id=ctx.message.channel.id)
            except exceptions.NoSingleMatch as e:
                logger.error(f'Could not infer game from channel: {e}')
                return await ctx.send(f'Game ID not provided and cannot detect a game channel. Usage: __`{ctx.prefix}{ctx.invoked_with} GAME_ID`__')
            logger.debug(f'Inferring game {inferred_game.id} from getnames command used in channel {ctx.message.channel.id}')
        
        if inferred_game:
            game = inferred_game
        else:
            game = await PolyGame().convert(ctx, int(game_id), allow_cross_guild=True)

        try:
            ordered_player_list = game.draft_order()
        except exceptions.MyBaseException as e:
            return await ctx.send(f'**Error:** {e}')

        warn_str = '\n*(List may take a few seconds to print due to discord anti-spam measures.)*' if len(ordered_player_list) > 2 else ''
        header_str = f'In-game names for **game {game.id}**, in draft order:{warn_str}'

        first_loop = True
        async with ctx.typing():
            for p in ordered_player_list:
                dm_obj = p['player'].discord_member
                if game.is_mobile:
                    # if dm_obj.polytopia_name and dm_obj.polytopia_name.lower() != p['player'].name.lower():
                    #     in_game_name_str = f' (In-game name: **{dm_obj.polytopia_name}**)'
                    # else:
                    #     in_game_name_str = ''
                    if dm_obj.polytopia_id:
                        in_game_name_str = f' (Old-style code: `{dm_obj.polytopia_id}`)'
                    else:
                        in_game_name_str = ''
                else:
                    if dm_obj.name_steam:
                        in_game_name_str = f'\nSteam name: **{dm_obj.name_steam}**'
                    else:
                        in_game_name_str = '\n *Steam name not set*'

                if first_loop:
                    # header_str combined with first player's name in order to reduce number of ctx.send() that are done.
                    # More than 3-4 and they will drip out due to API rate limits
                    await ctx.send(f'{header_str}\n**{p["player"].name}**{in_game_name_str} -- *Creates the game and invites everyone else*')
                    first_loop = False
                else:
                    if dm_obj.timezone_offset:
                        tz_str = f'`UTC+{dm_obj.timezone_offset}`' if dm_obj.timezone_offset > 0 else f'`UTC{dm_obj.timezone_offset}`'
                    else:
                        tz_str = ''
                    await ctx.send(f'**{p["player"].name}**{in_game_name_str} {tz_str}')
                if game.is_mobile:
                    await ctx.send(dm_obj.polytopia_name or 'No name set')

    @commands.command(brief='Set player time zone', usage='UTC±#')
    @models.is_registered_member()
    async def settime(self, ctx, *args):
        """Sets your own timezone, or lets staff set a player's timezone
        This will be shown on your `[p]player` profile and can be used to order large games for faster player.

        **Examples:**
        `[p]settime UTC-5` - Set your own timezone to UTC-5  *(Eastern Standard Time)*
        `[p]settime Nelluk UTC-5` - Lets staff set in-game name of Nelluk to UTC-5

        *Accepts arguments like: UTC+05:00, GMT-5:30*
        """

        if len(args) == 1:
            # User setting code for themselves. No special permissions required.
            target_string = f'<@{ctx.author.id}>'
            tz_string = args[0]
        elif len(args) == 2:
            # User changing another user's code. Admin permissions required.
            if args[0].upper() in ('GMT', 'UTC'):
                # catching the case of someone doing '$settime UTC +5'
                target_string = f'<@{ctx.author.id}>'
                tz_string = (args[0] + args[1]).replace(' ', '')
            elif settings.is_staff(ctx.author) is False:
                return await ctx.send('You do not have permission to trigger this command.')
            else:
                target_string = args[0]
                tz_string = args[1]
        else:
            # Unexpected input
            return await ctx.send(f'Wrong number of arguments. Use `{ctx.prefix}settime my_time_zone_offset`. Example: `{ctx.prefix}settime UTC-5:00` for Eastern Standard Time.')

        try:
            player_target = Player.get_or_except(target_string, ctx.guild.id)
        except exceptions.NoSingleMatch as ex:
            return await ctx.send(f'{ex}\nExample usage: `{ctx.prefix}settime @Player time_zone_offset`')

        m = re.search(r'(?:GMT|UTC)([+-][0-9]{1,2})(:[0-9]{2}\b)?', tz_string, re.I)
        if m:
            offset = int(m[1])
            if m[2] and m[2] == ':30':
                if m[1][:1] == '+':
                    offset = offset + .5
                else:
                    offset = offset - .5
        elif tz_string.upper() in ['UTC', 'GMT']:
            offset = 0
            # case of "$settime UTC"
        else:
            return await ctx.send(f'Could not interpret input. Use `{ctx.prefix}settime my_time_zone_offset`.\nExample: `{ctx.prefix}settime UTC-5:00` for Eastern Standard Time.')

        player_target.discord_member.timezone_offset = offset
        player_target.discord_member.save()
        offset_str = 'UTC+' if offset >= 0 else 'UTC'
        await ctx.send(f'Player **{player_target.name}** updated in system with timezone offset **{offset_str}{offset}**.')

    @commands.command(aliases=['match'], usage='game_id')
    async def game(self, ctx, *, game_search: str = None):
        # async def game(self, ctx, game: PolyGame = None):

        """See details on a specific game ID

        If you use something other than a numeric game ID with this command, it is assumed you are trying to use `allgames`, which allows you to search games by player, game name, result, or team. See `[p]help allgames`

        **Examples**:
        `[p]game 1251` - See details on game # 1251.
        """
        if not game_search:
            return await ctx.send(f'Game ID number must be supplied, example: __`{ctx.prefix}game 1250`__')
        if str(game_search).upper() == 'ID':
            await ctx.send(f'Invalid game ID "{game_search}". Use the numeric game ID *only*, example: `{ctx.prefix}{ctx.invoked_with} 1234`')
            raise commands.UserInputError()

        try:
            int(game_search)
        except ValueError:
            # User passed in non-numeric, probably searching by game title
            return await ctx.invoke(self.bot.get_command('allgames'), args=game_search)

        channel = getattr(ctx, 'channel', None)
        channel_id = getattr(channel, 'id', None)
        if channel_id is None and getattr(ctx, 'message', None) is not None:
            channel_id = getattr(ctx.message.channel, 'id', 0)
        return await self._send_game_detail(
            ctx,
            guild=ctx.guild,
            requester_id=ctx.author.id,
            channel_id=channel_id or 0,
            game_id=int(game_search),
        )

    @game_group.command(
        name='show',
        description='Show one game in the standard game card.',
    )
    @discord.app_commands.describe(
        game_id='Game ID; omit it only in an unambiguous game channel.',
    )
    async def game_show_slash(
        self,
        interaction: discord.Interaction,
        game_id: int | None = None,
    ):
        """Show the shared public game-detail card."""

        # The legacy $game command has no bot-channel or registration check;
        # retain that visibility rule while the native group remains guild-only.
        await interaction.response.defer()
        await self._send_game_detail(
            interaction,
            guild=interaction.guild,
            requester_id=interaction.user.id,
            channel_id=interaction.channel_id or 0,
            game_id=game_id,
            slash=True,
        )

    @game_group.command(
        name='start',
        description='Start a full game after creating it in Polytopia.',
    )
    @discord.app_commands.describe(
        game_id='Full open game to start.',
        name='Exact game name shown in Polytopia.',
    )
    async def game_start_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
        name: str,
    ):
        """Start through the same bounded transition used by the prefix."""

        await interaction.response.defer()
        prefix = settings.guild_setting(
            interaction.guild.id,
            'command_prefix',
        )
        if not await self._native_pending_game_channel_allowed(interaction):
            return

        matchmaking_cog = self.bot.get_cog('matchmaking')
        if matchmaking_cog is None:
            return await interaction.followup.send(
                'The start-game command handler is unavailable.',
                ephemeral=True,
            )
        try:
            result = await matchmaking_cog.execute_start(
                game_id=game_id,
                guild=interaction.guild,
                requester=interaction.user,
                name=name,
                prefix=prefix,
                invoked_with='/game start',
            )
        except game_start_workers.GameStartValidationError as exc:
            return await interaction.followup.send(
                str(exc),
                ephemeral=True,
            )
        except peewee.PeeweeException:
            logger.exception('Database failure in native start %s', game_id)
            return await interaction.followup.send(
                'The game could not be started because the database operation '
                'failed. No public Discord effects were made.',
                ephemeral=True,
            )
        except exceptions.CheckFailedError as exc:
            logger.exception('Start validation failure in native %s', game_id)
            return await interaction.followup.send(str(exc), ephemeral=True)
        except Exception:
            logger.exception('Unexpected failure in native start %s', game_id)
            return await interaction.followup.send(
                'The game could not be started. No public Discord effects were '
                'made.',
                ephemeral=True,
            )

        await game_start.publish_start_result(
            result,
            output_context=game_start.native_output_context(
                interaction,
                prefix=prefix,
            ),
            guild=interaction.guild,
            prefix=prefix,
            bot_guilds=settings.bot.guilds,
        )

    @game_group.command(
        name='open',
        description='Open a game for other players to join.',
    )
    @discord.app_commands.describe(
        size='Game shape, such as 1v1, 1v3, 1v1v1, or 6FFA.',
    )
    async def open_slash(
        self,
        interaction: discord.Interaction,
        size: str,
    ):
        """Open a game through a short native draft and shared worker."""

        await interaction.response.defer(ephemeral=True)
        ctx = await commands.Context.from_interaction(interaction)
        ctx.prefix = settings.guild_setting(
            interaction.guild.id,
            'command_prefix',
        )
        ctx.invoked_with = 'opengame'

        matchmaking_cog = self.bot.get_cog('matchmaking')
        open_command = (
            getattr(matchmaking_cog, 'opengame', None)
            if matchmaking_cog is not None
            else None
        )
        if open_command is None:
            return await interaction.edit_original_response(
                content='The open-game command handler is unavailable.',
            )
        if not await open_command.can_run(ctx):
            return

        try:
            sizes, _ = game_open_workers.parse_game_size_token(size)
        except game_open_workers.OpenGameSizeError as exc:
            return await interaction.edit_original_response(content=str(exc))

        requester = interaction.user
        requester_roles = tuple(getattr(requester, 'roles', ()))
        requester_snapshot = {
            'requester_id': requester.id,
            'requester_name': requester.name,
            'requester_nick': getattr(requester, 'nick', None),
            'requester_role_ids': tuple(role.id for role in requester_roles),
            'requester_role_names': tuple(
                role.name for role in requester_roles
            ),
            'requester_level': settings.get_user_level(requester),
            'requester_is_mod': settings.is_mod(requester),
            'requester_is_staff': settings.is_staff(requester),
            'requester_description': models.GameLog.member_string(requester),
        }
        default_expiration = game_open_workers.default_expiration_hours(
            sum(sizes)
        )
        unranked_channel = settings.guild_setting(
            interaction.guild.id,
            'unranked_game_channel',
        )
        default_ranked = not (
            unranked_channel
            and getattr(interaction, 'channel_id', None) == unranked_channel
        )

        def build_request(
            draft: game_open_views.OpenGameDraft,
        ) -> game_open_workers.OpenGameRequest:
            notes = utilities.escape_everyone_here_roles(
                draft.notes[:150].strip()
            )
            return game_open_workers.OpenGameRequest(
                guild_id=interaction.guild.id,
                requester_id=requester_snapshot['requester_id'],
                requester_name=requester_snapshot['requester_name'],
                requester_nick=requester_snapshot['requester_nick'],
                prefix=ctx.prefix,
                requester_role_ids=requester_snapshot['requester_role_ids'],
                requester_role_names=requester_snapshot[
                    'requester_role_names'
                ],
                requester_level=requester_snapshot['requester_level'],
                requester_is_mod=requester_snapshot['requester_is_mod'],
                requester_is_staff=requester_snapshot['requester_is_staff'],
                sides=tuple(
                    game_open_workers.OpenGameSide(side_size)
                    for side_size in draft.size
                ),
                expiration_hours=draft.expiration_hours,
                is_ranked=draft.ranked,
                is_mobile=True,
                notes=notes,
                notes_display=notes or '\u200b',
                log_notes_display=discord.utils.escape_markdown(
                    notes or '\u200b'
                ),
                requester_description=requester_snapshot[
                    'requester_description'
                ],
                invoked_with='/game open',
            )

        async def confirm_open_game(
            confirmation: discord.Interaction,
            draft: game_open_views.OpenGameDraft,
        ) -> None:
            request = build_request(draft)
            try:
                result = await game_open_workers.run_open_game_creation(
                    request
                )
            except game_open_workers.OpenGameValidationError as exc:
                await confirmation.followup.send(str(exc), ephemeral=True)
                return
            except (peewee.PeeweeException, exceptions.MyBaseException) as exc:
                logger.exception('Error creating native open game')
                await confirmation.followup.send(
                    f'Error opening game: {exc}. No public Discord effects '
                    'were made.',
                    ephemeral=True,
                )
                return
            except Exception:
                logger.exception('Unexpected error creating native open game')
                await confirmation.followup.send(
                    'Error opening game. No public Discord effects were made.',
                    ephemeral=True,
                )
                return

            async def send_public(message: str):
                return await confirmation.followup.send(
                    message,
                    ephemeral=False,
                    wait=True,
                )

            await game_open.publish_open_game_result(
                result,
                prefix=ctx.prefix,
                send=send_public,
                add_completion_reaction=game_open.add_join_reaction,
            )

        view = game_open_views.OpenGameView(
            requester_id=interaction.user.id,
            draft=game_open_views.OpenGameDraft(
                size=tuple(sizes),
                ranked=default_ranked,
                expiration_hours=default_expiration,
            ),
            confirmer=confirm_open_game,
        )
        view.message = await interaction.edit_original_response(view=view)

    async def _native_pending_game_channel_allowed(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        """Keep the prefix bot-channel restriction with native errors private."""

        bot_channels = settings.guild_setting(
            interaction.guild.id,
            'bot_channels',
        )
        private_channels = settings.guild_setting(
            interaction.guild.id,
            'bot_channels_private',
        ) or []
        if (
            bot_channels is None
            or settings.is_mod(interaction.user)
            or interaction.channel_id in (bot_channels or []) + private_channels
        ):
            return True
        channel_tags = ' '.join(
            f'<#{channel_id}>' for channel_id in (bot_channels or [])
        )
        await interaction.followup.send(
            'This command can only be used in a designated ELO bot channel. '
            f'Try: {channel_tags}' if channel_tags else
            'This command can only be used in a designated ELO bot channel.',
            ephemeral=True,
        )
        return False

    async def _native_winner_game_channel_allowed(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        """Mirror the strict channel check on prefix and slash win commands."""

        strict_channels = settings.guild_setting(
            interaction.guild.id,
            'bot_channels_strict',
        )
        if strict_channels is None:
            strict_channels = settings.guild_setting(
                interaction.guild.id,
                'bot_channels',
            )
        private_channels = settings.guild_setting(
            interaction.guild.id,
            'bot_channels_private',
        ) or []
        if (
            strict_channels is None
            or settings.is_mod(interaction.user)
            or interaction.channel_id in (strict_channels or []) + private_channels
        ):
            return True

        channel_tags = ' '.join(
            f'<#{channel_id}>' for channel_id in (strict_channels or [])
        )
        await interaction.followup.send(
            'This command can only be used in a designated bot spam channel. '
            f'Try: {channel_tags}' if channel_tags else
            'This command can only be used in a designated bot spam channel.',
            ephemeral=True,
        )
        return False

    async def _publish_native_join_result(
        self,
        interaction: discord.Interaction,
        result: game_join_workers.JoinResult,
        *,
        member,
        prefix: str,
        publish_card: bool = True,
    ) -> None:
        """Publish a committed join and surface post-commit reconciliation."""

        reconciliation = await game_join_leave.remove_inactive_role_after_commit(
            result,
            member,
        )
        messages = list(result.messages)
        if reconciliation:
            messages.append(reconciliation)

        committed_game = None
        embed = content = None
        if publish_card:
            try:
                committed_game = models.Game.load_full_game(result.game_id)
                embed, content = committed_game.embed(
                    guild=interaction.guild,
                    prefix=prefix,
                )
            except Exception:
                logger.exception(
                    'Committed native join %s could not reload its game card',
                    result.game_id,
                )
                messages.append(
                    f':warning: Game {result.game_id} was joined successfully, '
                    'but its game card could not be updated. An operator must '
                    'reconcile the announcement.'
                )

        if result.is_full:
            public_send = lambda content: interaction.followup.send(
                content,
                ephemeral=False,
            )
            await game_join_leave.send_post_commit_message(
                public_send,
                f'Game {result.game_id} is now full and '
                f'<@{result.creator_id}> should create the game in Polytopia.',
                game_id=result.game_id,
                effect='full-game notice',
            )
            if result.host_id and result.host_id != result.creator_id:
                await game_join_leave.send_post_commit_message(
                    public_send,
                    f'Matchmaking host <@{result.host_id}> is not the game '
                    'creator.',
                    game_id=result.game_id,
                    effect='host-mismatch notice',
                )

        if committed_game is not None:
            try:
                await image_storage.send_game_embed(
                    interaction.followup,
                    committed_game,
                    embed=embed,
                    content=content if result.is_full else None,
                )
            except Exception:
                logger.exception(
                    'Committed native join %s game card update failed',
                    result.game_id,
                )
                messages.append(
                    f':warning: Game {result.game_id} was joined successfully, '
                    'but its game card could not be sent. An operator must '
                    'reconcile the announcement.'
                )

        await game_join_leave.send_post_commit_message(
            lambda message: interaction.followup.send(
                message,
                ephemeral=False,
            ),
            '\n'.join(messages),
            game_id=result.game_id,
            effect='join output',
        )

    @game_group.command(
        name='join',
        description='Join an open game.',
    )
    @discord.app_commands.describe(
        game_id='Open game to join.',
        side='Optional side number or side name.',
        member='Optional player to place; level 4 or higher is required.',
    )
    async def game_join_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
        side: str | None = None,
        member: discord.Member | None = None,
    ):
        """Join through the same worker used by prefix and reactions."""

        await interaction.response.defer()
        prefix = settings.guild_setting(
            interaction.guild.id,
            'command_prefix',
        )
        if not await self._native_pending_game_channel_allowed(
            interaction,
        ):
            return

        requester = interaction.user
        target = member or requester
        if (
            member is not None
            and member.id != requester.id
            and settings.get_user_level(requester) < 4
        ):
            return await interaction.followup.send(
                'You do not have permissions to add another person to a game. '
                'Tell them to use the join command themselves.',
                ephemeral=True,
            )

        matchmaking_cog = self.bot.get_cog('matchmaking')
        if matchmaking_cog is None:
            return await interaction.followup.send(
                'The join-game command handler is unavailable.',
                ephemeral=True,
            )
        try:
            result = await matchmaking_cog.execute_join(
                game_id=game_id,
                member=target,
                author_member=requester,
                side_arg=side,
                invoked_with='/game join',
                notification_member_id=requester.id,
                prefix=prefix,
            )
        except game_join_workers.PendingGameJoinValidationError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception('Database failure in native join %s', game_id)
            return await interaction.followup.send(
                'The game could not be changed because the database operation '
                'failed. No public Discord effects were made.',
                ephemeral=True,
            )
        except Exception:
            logger.exception('Unexpected failure in native join %s', game_id)
            return await interaction.followup.send(
                'The game could not be changed. No public Discord effects were '
                'made.',
                ephemeral=True,
            )

        await self._publish_native_join_result(
            interaction,
            result,
            member=target,
            prefix=prefix,
        )

    @game_group.command(
        name='leave',
        description='Leave an open game.',
    )
    @discord.app_commands.describe(game_id='Open game to leave.')
    async def game_leave_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
    ):
        """Leave through the same worker used by prefix and reactions."""

        await interaction.response.defer()
        prefix = settings.guild_setting(
            interaction.guild.id,
            'command_prefix',
        )
        if not await self._native_pending_game_channel_allowed(
            interaction,
        ):
            return

        matchmaking_cog = self.bot.get_cog('matchmaking')
        if matchmaking_cog is None:
            return await interaction.followup.send(
                'The leave-game command handler is unavailable.',
                ephemeral=True,
            )
        try:
            result = await matchmaking_cog.execute_leave(
                game_id=game_id,
                member=interaction.user,
                author_member=interaction.user,
                invoked_with='/game leave',
                prefix=prefix,
            )
        except game_join_workers.PendingGameLeaveValidationError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception('Database failure in native leave %s', game_id)
            return await interaction.followup.send(
                'The game could not be changed because the database operation '
                'failed. No public Discord effects were made.',
                ephemeral=True,
            )
        except Exception:
            logger.exception('Unexpected failure in native leave %s', game_id)
            return await interaction.followup.send(
                'The game could not be changed. No public Discord effects were '
                'made.',
                ephemeral=True,
            )

        await self._publish_native_leave_result(interaction, result)

    @game_manage_group.command(
        name='kick',
        description='Remove a player from an open game.',
    )
    @discord.app_commands.describe(
        game_id='Open game from which to remove the player.',
        member='Player to remove from the open game.',
    )
    async def game_manage_kick_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
        member: discord.Member,
    ):
        """Remove a pending-game member through the shared kick worker."""

        await interaction.response.defer()
        prefix = settings.guild_setting(
            interaction.guild.id,
            'command_prefix',
        )
        if not await self._native_pending_game_channel_allowed(interaction):
            return

        matchmaking_cog = self.bot.get_cog('matchmaking')
        if matchmaking_cog is None:
            return await interaction.followup.send(
                'The kick-game command handler is unavailable.',
                ephemeral=True,
            )
        try:
            result = await matchmaking_cog.execute_kick(
                game_id=game_id,
                author_member=interaction.user,
                target_member=member,
                invoked_with='/game manage kick',
                prefix=prefix,
            )
        except game_kick_workers.PendingGameKickValidationError as exc:
            return await interaction.followup.send(
                str(exc),
                ephemeral=True,
            )
        except peewee.PeeweeException:
            logger.exception('Database failure in native kick %s', game_id)
            return await interaction.followup.send(
                'The game could not be changed because the database operation '
                'failed. No public game effects were made.',
                ephemeral=True,
            )
        except Exception:
            logger.exception('Unexpected failure in native kick %s', game_id)
            return await interaction.followup.send(
                'The game could not be changed. No public game effects were '
                'made.',
                ephemeral=True,
            )

        await game_join_leave.publish_kick_result(
            result,
            send=lambda content: interaction.followup.send(
                content,
                ephemeral=False,
            ),
            card_destination=interaction.followup,
            guild=interaction.guild,
            prefix=prefix,
        )

    @game_group.command(
        name='search',
        description='Search games and refine the public results.',
    )
    @discord.app_commands.describe(
        query=(
            'Optional player, team, title/notes terms, or size such as 2v2.'
        ),
        view='Choose the initial search view.',
    )
    @discord.app_commands.choices(
        view=GAME_SEARCH_VIEW_CHOICES,
    )
    async def game_search_slash(
        self,
        interaction: discord.Interaction,
        query: str | None = None,
        view: str | None = None,
    ):
        await interaction.response.defer()
        ctx = await commands.Context.from_interaction(interaction)
        ctx.prefix = settings.guild_setting(
            interaction.guild.id,
            'command_prefix',
        )
        ctx.invoked_with = 'allgames'
        if not await self.allgames.can_run(ctx):
            return
        requester_values = self._game_search_requester_values(
            interaction.user,
        )
        initial_view = view or 'all'
        request_kwargs = {
            'guild_id': interaction.guild.id,
            'requester_discord_id': interaction.user.id,
            'query': (query or '').strip(),
            **requester_values,
        }

        async def loader(filter_key):
            return await self._load_game_search(
                game_search_workers.GameSearchRequest(
                    **request_kwargs,
                    key=filter_key,
                )
            )

        try:
            snapshot = await loader(
                game_search_workers.GameSearchKey(status=initial_view)
            )
        except (game_search_workers.GameSearchError,
                peewee.PeeweeException,
                asyncio.TimeoutError,
                ValueError) as exc:
            return await interaction.followup.send(
                str(exc) or 'Game search timed out.',
                ephemeral=True,
            )
        view = game_search_views.GameSearchWorkspace(
            requester_id=interaction.user.id,
            initial_result=snapshot,
            loader=loader,
            can_view_unconfirmed=request_kwargs['staff'],
        )
        view.message = await interaction.edit_original_response(view=view)

    @settings.in_bot_channel_strict()
    @models.is_registered_member()
    @commands.command(usage='player1 player2 ... ')
    async def allgames(self, ctx, *, args=None):
        """Search for games by participants or game name

        **Examples**:
        `[p]allgames Nelluk`
        `[p]allgames OCEANS OF FIRE` - Search by title - words in all caps are used to search title/notes.
        `[p]allgames Nelluk OCEANS` - See games that included player Nelluk and the word *OCEANS* in the game name or game notes.
        `[p]allgames Jets`
        `[p]allgames Nelluk 2v2` - Show all 2v2 games including Nelluk
        `[p]allgames Jets Ronin` - See games between those two teams
        `[p]allgames Nelluk rickdaheals frodakcin Jets Ronin` - See games in which three players and two teams were all involved

        You can also filter with separate commands: `[p]wins`, `[p]losses`, `[p]completed`, `[p]incomplete` - See `[p]help wins`, etc. for more detail.
        """

        if args:
            request = player_workers.PlayerWorkspaceRequest(
                guild_id=ctx.guild.id,
                player_query=args,
            )
            try:
                snapshot = await self._load_player_workspace(request)
            except (player_workers.PlayerNotFound,
                    player_workers.AmbiguousPlayer,
                    peewee.PeeweeException,
                    ValueError):
                pass
            else:
                view = player_views.PlayerWorkspace(
                    requester_id=ctx.author.id,
                    snapshot=snapshot,
                    initial_section='recent',
                    can_edit=(
                        ctx.author.id == snapshot.discord_id
                        or settings.is_staff(ctx.author)
                    ),
                )
                view.message = await ctx.send(view=view)
                return

        # Complex and non-player searches retain the separate game-search path.
        target_list = args.split() if args else []
        await self.game_search(ctx=ctx, mode='ALLGAMES', arg_list=target_list)

    @settings.in_bot_channel_strict()
    @models.is_registered_member()
    @commands.command(aliases=['complete', 'completed'], hidden=False)
    async def incomplete(self, ctx, *, args=None):
        """List incomplete games for you or other players - also `[p]complete`
        **Example:**
        `[p]incomplete` - Lists incomplete games you are playing in
        `[p]incomplete all` - Lists all incomplete games
        `[p]incomplete Nelluk` - Lists all incomplete games for player Nelluk
        `[p]incomplete Nelluk anarchoRex` - Lists all incomplete games with both players
        `[p]incomplete Nelluk Jets` - Lists all incomplete games for Nelluk that include team Jets
        `[p]incomplete Ronin Jets` - Lists all incomplete games that include teams Ronin and Jets
        `[p]incomplete RONIN` - Search by title - words in all caps are used to search title/notes.

        You can also include a game size such as *2v2* to limit by size.
        """
        request = player_workers.PlayerWorkspaceRequest(
            guild_id=ctx.guild.id,
            discord_id=ctx.author.id if not args else None,
            player_query=args,
        )
        try:
            snapshot = await self._load_player_workspace(request)
        except (player_workers.PlayerNotFound,
                player_workers.AmbiguousPlayer,
                peewee.PeeweeException,
                ValueError):
            snapshot = None
        if snapshot is not None:
            section = (
                'completed'
                if ctx.invoked_with.upper() in ('COMPLETED', 'COMPLETE')
                else 'incomplete'
            )
            view = player_views.PlayerWorkspace(
                requester_id=ctx.author.id,
                snapshot=snapshot,
                initial_section=section,
                can_edit=(
                    ctx.author.id == snapshot.discord_id
                    or settings.is_staff(ctx.author)
                ),
            )
            view.message = await ctx.send(view=view)
            return

        target_list = args.split() if args else []
        if ctx.invoked_with.upper() in ['COMPLETED', 'COMPLETE']:
            await self.game_search(ctx=ctx, mode='COMPLETE', arg_list=target_list)
        else:
            await self.game_search(ctx=ctx, mode='INCOMPLETE', arg_list=target_list)

    @settings.in_bot_channel_strict()
    @models.is_registered_member()
    @commands.command(aliases=['losses', 'loss'], hidden=False)
    async def wins(self, ctx, *, args=None):
        """List games that you or others have won - also `[p]losses`
        If any players names are listed, the first played is who the win is checked against. If no players listed, then the first team listed is checked for the win.
        **Example:**
        `[p]wins` - Lists all games you have won
        `[p]wins Nelluk` - Lists all wins for player Nelluk
        `[p]wins Nelluk anarchoRex` - Lists all games for both players, in which the first player is the winner
        `[p]wins Nelluk frodakcin Jets` - Lists all wins for Nelluk in which player frodakcin and team Jets participated
        `[p]wins Ronin Jets` - Lists all wins for team Ronin in which team Jets participated

        You can also include a game size such as *2v2* to limit by size.
        """
        request = player_workers.PlayerWorkspaceRequest(
            guild_id=ctx.guild.id,
            discord_id=ctx.author.id if not args else None,
            player_query=args,
        )
        try:
            snapshot = await self._load_player_workspace(request)
        except (player_workers.PlayerNotFound,
                player_workers.AmbiguousPlayer,
                peewee.PeeweeException,
                ValueError):
            snapshot = None
        if snapshot is not None:
            completed_filter = (
                'losses'
                if ctx.invoked_with.upper() in ('LOSS', 'LOSSES')
                else 'wins'
            )
            view = player_views.PlayerWorkspace(
                requester_id=ctx.author.id,
                snapshot=snapshot,
                initial_section='completed',
                completed_filter=completed_filter,
                can_edit=(
                    ctx.author.id == snapshot.discord_id
                    or settings.is_staff(ctx.author)
                ),
            )
            view.message = await ctx.send(view=view)
            return

        target_list = args.split() if args else []
        if ctx.invoked_with.upper() in ['LOSS', 'LOSSES']:
            await self.game_search(ctx=ctx, mode='LOSSES', arg_list=target_list)
        else:
            await self.game_search(ctx=ctx, mode='WINS', arg_list=target_list)

    @settings.in_bot_channel()
    @models.is_registered_member()
    @commands.command(usage='"Name of Game" player1 player2 vs player3 player4', aliases=['newgameunranked', 'newsteamgame', 'newsteamgameunranked'])
    # @settings.is_user_check()
    async def newgame(self, ctx, game_name: str = None, *args):
        """Adds an existing game to the bot for tracking

        **Examples:**
        `[p]newgame "Name of Game" nelluk vs koric` - Sets up a 1v1 game
        `[p]newgame "Name of Game" koric` - Sets up a 1v1 game versus yourself and koric (shortcut)
        `[p]newgame "Name of Game" nelluk frodakcin vs bakalol ben` - Sets up a 2v2 game

        Use `[p]newgameunranked` to create the game as unranked
        Legacy `[p]newsteamgame` aliases remain accepted, but platform no
        longer changes game behavior because Polytopia supports cross-play.
        """

        if ctx.guild.id == 814317488418193478 and not settings.is_staff(ctx.author):
            return await ctx.send('For **The Polympics** only server staff may open games.')

        ranked_flag = not (ctx.invoked_with in ['newgameunranked', 'newsteamgameunranked'])
        # Mobile and Steam now have full cross-play. Retain the legacy field
        # with its canonical compatibility value until the schema and all
        # historical filters are retired in a separate migration.
        is_mobile = True

        example_usage = (f'Example usage:\n`{ctx.prefix}newgame "Name of Game" player1 VS player2` - Start a 1v1 game\n'
                         f'`{ctx.prefix}newgame "Name of Game" player1 player2 VS player3 player4` - Start a 2v2 game')

        if settings.get_user_level(ctx.author) <= 2:
            return await ctx.send(
                'You are not authorized to use this command. Create and '
                f'join games with `{ctx.prefix}open` / `{ctx.prefix}join`'
            )
        if not game_name:
            return await ctx.send(f'Invalid format. {example_usage}')
        if not args:
            return await ctx.send(f'Invalid format. {example_usage}')

        if len(game_name.split(' ')) < 2 and ctx.author.id != settings.owner_id:
            if getattr(ctx, 'interaction', None) is not None:
                return await ctx.send(
                    'Invalid game name. Enter the exact multi-word game '
                    'name shown in Polytopia.'
                )
            return await ctx.send(
                'Invalid game name. Make sure to use "quotation marks" '
                f'around the full game name.\n{example_usage}'
            )
        if not utilities.is_valid_poly_gamename(input=game_name):
            if settings.get_user_level(ctx.author) <= 2:
                return await ctx.send(
                    'That name looks made up. :thinking: You need to '
                    'manually create the game __in Polytopia__, come back '
                    'and input the name of the new game you made.\n'
                    f'You can use `{ctx.prefix}code NAME` to get the code '
                    'of each player in this game.'
                )
            await ctx.send(
                ':warning: That game name looks made up - you are allowed '
                'to override due to your user level.'
            )

        try:
            discord_groups = await resolve_newgame_roster(
                ctx,
                args,
                ranked_flag=ranked_flag,
            )
        except NewGameRosterError as exc:
            return await ctx.send(str(exc))

        logger.info(
            'All input checks passed. Creating new game records with args: '
            f'{args}'
        )
        request = game_workers.NewGameRequest(
            guild_id=ctx.guild.id,
            name=game_name,
            is_ranked=ranked_flag,
            is_mobile=is_mobile,
            mod_override=settings.is_mod(ctx.author),
            requester_id=ctx.author.id,
            requester_name=ctx.author.name,
            requester_nick=ctx.author.nick,
            requester_description=models.GameLog.member_string(ctx.author),
            invoked_with=ctx.invoked_with,
            escaped_game_name=discord.utils.escape_markdown(game_name),
            sides=tuple(
                tuple(
                    game_workers.NewGameParticipant(
                        discord_id=member.id,
                        discord_name=member.name,
                        discord_nick=member.nick,
                        display_name=member.display_name,
                        role_names=tuple(role.name for role in member.roles),
                    )
                    for member in group
                )
                for group in discord_groups
            ),
        )
        try:
            async with ctx.typing():
                result = await game_workers.run_new_game_creation(request)
        except (peewee.PeeweeException, exceptions.CheckFailedError) as exc:
            logger.exception('Error creating new game')
            return await ctx.send(f'Error creating new game: {exc}')
        except ValueError as exc:
            return await ctx.send(str(exc))
        except Exception:
            logger.exception('Unexpected error creating new game')
            return await ctx.send(
                'Error creating new game. No Discord announcements or '
                'channels were created.'
            )

        if result.warnings:
            await ctx.send('\n'.join(result.warnings))
        newgame = Game.load_full_game(game_id=result.game_id)
        await post_newgame_messaging(ctx, game=newgame)

    @game_group.command(
        name='record',
        description='Record an existing Polytopia game.',
    )
    @discord.app_commands.describe(
        game_name='The exact game name shown in Polytopia.',
        roster='Players separated into sides with “vs”. Mentions are safest.',
        ranked='Whether the game affects ELO.',
    )
    async def newgame_slash(
        self,
        interaction: discord.Interaction,
        game_name: str,
        roster: str,
        ranked: bool = True,
    ):
        """Flexible roster parser with an interaction review gate."""

        # The game roster and its eventual competitive-state mutation are
        # public server activity. Keep the preview public while the view
        # itself enforces requester-only controls.
        await interaction.response.defer()
        ctx = await commands.Context.from_interaction(interaction)
        ctx.prefix = settings.guild_setting(
            interaction.guild.id,
            'command_prefix',
        )

        # Reuse the existing command checks so registration, configured bot
        # channels, and any global checks remain identical during transition.
        if not await self.newgame.can_run(ctx):
            return

        async def build_preview(
            roster_value: str,
        ) -> game_record_views.GameRecordPreview:
            parsed_sides = game_record_views.parse_roster_string(roster_value)
            args = game_record_views.roster_arguments(parsed_sides)
            resolved_sides = await resolve_newgame_roster(
                ctx,
                args,
                ranked_flag=ranked,
            )
            return game_record_views.GameRecordPreview(
                game_name=game_name,
                roster=roster_value,
                ranked=ranked,
                sides=tuple(
                    tuple(
                        game_record_views.RosterMember(
                            discord_id=member.id,
                            display_name=discord.utils.escape_markdown(
                                member.display_name
                            ),
                        )
                        for member in side
                    )
                    for side in resolved_sides
                ),
            )

        try:
            preview = await build_preview(roster)
        except ValueError as exc:
            return await interaction.edit_original_response(content=str(exc))

        async def confirm_record(
            confirmation: discord.Interaction,
            roster_value: str,
        ) -> None:
            # Component interactions do not carry application-command data
            # and therefore cannot create a new commands.Context. Retain the
            # original slash context; its interaction webhook remains valid
            # for this short-lived confirmation flow.
            ctx.invoked_with = (
                'newgame' if ranked else 'newgameunranked'
            )
            parsed_sides = game_record_views.parse_roster_string(roster_value)
            await self.newgame.callback(
                self,
                ctx,
                game_name,
                *game_record_views.roster_arguments(parsed_sides),
            )

        view = game_record_views.GameRecordView(
            requester_id=interaction.user.id,
            preview=preview,
            confirmer=confirm_record,
        )
        view.message = await interaction.edit_original_response(view=view)

    @settings.in_bot_channel_strict()
    @commands.command(
        usage='game_id winner',
        aliases=['lose'],
    )
    async def win(self, ctx, game_id: int, *, winner: str):
        """
        Declare winner of an existing game

        The win will be finalized when multiple players confirm the winner, or after approximately 24 hours if no other players confirm.

        If declaring your own victory it can be good practice to post a screenshot indicating that you are the last human player remaining,
        in case there is a later dispute over the outcome.

        **Examples:**
        `[p]win 2050 Home` - Declare *Home* team winner of game 2050
        `[p]win 2050 Nelluk` - Declare *Nelluk* winner of game 2050
        """
        request = game_win.build_request(
            game_id=game_id,
            member=ctx.author,
            guild_id=ctx.guild.id,
            prefix=ctx.prefix,
            winner_text=winner,
            invoked_with=getattr(ctx, 'invoked_with', 'win'),
        )
        return await game_win.run_win(
            request,
            guild=ctx.guild,
            current_channel=ctx.channel,
            send_public=ctx.send,
            send_error=ctx.send,
            post_win_publisher=post_win_messaging,
            defer=(ctx.defer if ctx.interaction is not None else None),
            typing_context=ctx.typing,
        )

    @game_group.command(
        name='win',
        description='Declare or confirm the winner of a game.',
    )
    @discord.app_commands.describe(
        game_id='Game receiving the win claim.',
        winner='Player or side that won.',
    )
    async def win_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
        winner: str,
    ):
        # Registration/channel checks perform bounded validation. A slash
        # interaction is acknowledged before those checks and before the
        # shared worker preflight.
        await interaction.response.defer()
        ctx = await commands.Context.from_interaction(interaction)
        ctx.prefix = settings.guild_setting(
            interaction.guild.id,
            'command_prefix',
        )
        ctx.invoked_with = 'win'
        if not await self.win.can_run(ctx):
            return

        request = game_win.build_request(
            game_id=game_id,
            member=interaction.user,
            guild_id=interaction.guild.id,
            prefix=ctx.prefix,
            winner_text=winner,
            invoked_with='win',
        )

        async def public_send(content):
            await interaction.followup.send(content, ephemeral=False)

        async def error_send(content):
            await interaction.followup.send(content, ephemeral=True)

        return await game_win.run_win(
            request,
            guild=interaction.guild,
            current_channel=interaction.channel,
            send_public=public_send,
            send_error=error_send,
            post_win_publisher=post_win_messaging,
            acknowledged=True,
        )

    @settings.in_bot_channel()
    @models.is_registered_member()
    @commands.command(usage='game_id')
    async def unwin(self, ctx, game_id: int):
        """Reset a completed game to incomplete

        **Staff usage**:
        Reverts ELO changes from the completed game and any subsequent completed game.
        Resets the game as if it were still incomplete with no declared winner.

        **Player usage**:
        If you use the `[p]win` command on the wrong game or for the wrong winner, use this command to undo your mistake.

         **Examples**
        `[p]unwin 12500`
        """

        if ctx.interaction is not None:
            await ctx.defer()

        coordinator = settings.elo_job_coordinator
        active_job = coordinator.active_job
        if active_job is not None:
            logger.info('Skipping unwin due to active ELO job: %s', active_job)
            return await ctx.send(
                f':warning: {ctx.author.mention} - ELO operation '
                f'`{active_job.operation}` for game '
                f'`{active_job.game_id or "all"}` is already running. '
                'Please try again in a few minutes.'
            )

        requester_description = models.GameLog.member_string(ctx.author)
        lock_acquired = False

        def lock_game():
            nonlocal lock_acquired
            utilities.lock_game(game_id)
            lock_acquired = True

        def unlock_game():
            if lock_acquired:
                utilities.unlock_game(game_id)

        try:
            async with ctx.typing():
                result = await coordinator.run(
                    operation='unwin',
                    game_id=game_id,
                    requester_id=ctx.author.id,
                    requester_name=ctx.author.display_name,
                    worker=elo_workers.unwin_game,
                    worker_args=(
                        game_id,
                        ctx.guild.id,
                        ctx.author.id,
                        requester_description,
                        settings.is_staff(ctx.author),
                    ),
                    before_submit=lock_game,
                    after_complete=unlock_game,
                )
        except EloJobConflict as exc:
            active_job = exc.active_job
            return await ctx.send(
                f':warning: {ctx.author.mention} - ELO operation '
                f'`{active_job.operation}` for game '
                f'`{active_job.game_id or "all"}` is already running. '
                'Please try again in a few minutes.'
            )
        except elo_workers.UnwinValidationError as exc:
            return await ctx.send(str(exc))
        except peewee.PeeweeException:
            logger.exception('Database failure while processing unwin %s', game_id)
            return await ctx.send(
                f'Game {game_id} could not be reset because the database '
                'operation failed. No Discord channel updates were made.'
            )
        except Exception:
            logger.exception('Unexpected failure while processing unwin %s', game_id)
            return await ctx.send(
                f'Game {game_id} could not be reset. No Discord channel '
                'updates were made.'
            )

        if result.post_unwin_messaging:
            game = Game.load_full_game(game_id=result.game_id)
            await post_unwin_messaging(
                ctx.guild,
                ctx.prefix,
                ctx.channel,
                game,
                previously_confirmed=result.previously_confirmed,
            )
        await ctx.send(result.message)

    @game_group.command(
        name='unwin',
        description='Reset a win claim or completed game.',
    )
    @discord.app_commands.describe(game_id='Game whose win should be reset.')
    async def unwin_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
    ):
        ctx = await commands.Context.from_interaction(interaction)
        ctx.prefix = settings.guild_setting(
            interaction.guild.id,
            'command_prefix',
        )
        ctx.invoked_with = 'unwin'
        if not await self.unwin.can_run(ctx):
            return
        await self.unwin.callback(self, ctx, game_id)

    @settings.in_bot_channel()
    @models.is_registered_member()
    @commands.command(
        usage='game_id',
        aliases=['delete_game', 'delgame', 'delmatch', 'deletegame'],
    )
    async def delete(self, ctx, game_id: int):
        """Deletes a game

        You can delete a game if you are the host and is has not started yet.
        Mods can delete completed games which will reverse any ELO changes they caused.
        **Example:**
        `[p]deletegame 25`
        """

        if ctx.interaction is not None:
            await ctx.defer()
        request = game_deletion.build_request(
            game_id=game_id,
            member=ctx.author,
            guild_id=ctx.guild.id,
            prefix=ctx.prefix,
            invoked_with=getattr(ctx, 'invoked_with', 'delete'),
        )
        try:
            typing = getattr(ctx, 'typing', None)
            if typing is None:
                result = await game_deletion.delete_game(request)
            else:
                async with typing():
                    result = await game_deletion.delete_game(request)
        except EloJobConflict as exc:
            active_job = exc.active_job
            return await ctx.send(
                f'ELO operation `{active_job.operation}` for game '
                f'`{active_job.game_id or "all"}` is already running.'
            )
        except game_deletion.GameDeletionValidationError as exc:
            return await ctx.send(str(exc))
        except peewee.PeeweeException:
            logger.exception('Database failure deleting game %s', game_id)
            return await ctx.send(
                'Game deletion failed and rolled back. No Discord channel '
                'updates were made.'
            )
        except Exception:
            logger.exception('Unexpected failure deleting game %s', game_id)
            return await ctx.send(
                'Game deletion failed. No Discord channel updates were made.'
            )

        await game_deletion.publish_result(
            result,
            send=ctx.send,
            guild=ctx.guild,
            bot=self.bot,
            prefix=ctx.prefix,
        )

    @game_group.command(
        name='delete',
        description='Delete a game when your permissions allow it.',
    )
    @discord.app_commands.describe(game_id='Game to delete.')
    async def delete_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
    ):
        ctx = await commands.Context.from_interaction(interaction)
        ctx.prefix = settings.guild_setting(
            interaction.guild.id,
            'command_prefix',
        )
        ctx.invoked_with = 'delete'
        if not await self.delete.can_run(ctx):
            return
        await self.delete.callback(self, ctx, game_id)

    async def _delegate_administration_slash(
        self,
        interaction: discord.Interaction,
        method_name: str,
        *args,
    ):
        administration_cog = self.bot.get_cog('administration')
        if administration_cog is None:
            message = 'The administration command handler is unavailable.'
            if interaction.response.is_done():
                return await interaction.followup.send(
                    message,
                    ephemeral=True,
                )
            return await interaction.response.send_message(
                message,
                ephemeral=True,
            )
        method = getattr(administration_cog, method_name)
        return await method(interaction, *args)

    @game_group.command(
        name='confirm',
        description='Confirm the claimed winner of a game.',
    )
    @discord.app_commands.describe(game_id='Game whose winner to confirm.')
    async def confirm_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
    ):
        await self._delegate_administration_slash(
            interaction,
            'confirm_slash',
            game_id,
        )

    @game_group.command(
        name='unconfirmed',
        description='List games with claimed but unconfirmed winners.',
    )
    async def unconfirmed_slash(
        self,
        interaction: discord.Interaction,
    ):
        await self._delegate_administration_slash(
            interaction,
            'unconfirmed_slash',
        )

    @game_group.command(
        name='set-ranked',
        description='Set whether an incomplete game is ranked.',
    )
    @discord.app_commands.describe(
        game_id='Incomplete game to correct.',
        ranked='True for ranked; false for unranked.',
    )
    async def set_ranked_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
        ranked: bool,
    ):
        await self._delegate_administration_slash(
            interaction,
            'set_ranked_slash',
            game_id,
            ranked,
        )

    @game_group.command(
        name='unstart',
        description='Return an in-progress game to open matchmaking.',
    )
    @discord.app_commands.describe(
        game_id='In-progress game to return to matchmaking.',
    )
    async def unstart_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
    ):
        await self._delegate_administration_slash(
            interaction,
            'unstart_slash',
            game_id,
        )

    @game_group.command(
        name='extend',
        description='Extend an open game deadline by 24 hours.',
    )
    @discord.app_commands.describe(game_id='Open game to extend.')
    async def extend_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
    ):
        await self._delegate_administration_slash(
            interaction,
            'extend_slash',
            game_id,
        )

    @commands.command(usage='game_id "New Name"')
    @models.is_registered_member()
    async def rename(self, ctx, *args):
        """Renames an existing game (due to restarts)

        You can rename a game for which you are the host. You can omit the game ID if you use the command in a game-specific channel.
        **Example:**
        `[p]rename 52000 Mountains of Fire`
        `[p]rename 52000 None` - Remove a game's name. Required elevated permissions.
        """

        usage = (f'**Example usage:** `{ctx.prefix}rename 100 New Game Name`\n'
                    'You can also omit the game ID if you use the command from a game-specific channel.')
        if not args:
            return await ctx.send(usage)
        try:
            game_id = int(args[0])
            new_game_name = ' '.join(args[1:])
        except ValueError:
            game_id = None
            new_game_name = ' '.join(args)

        inferred_game = None
        try:
            inferred_game = Game.by_channel_id(chan_id=ctx.message.channel.id)
        except exceptions.TooManyMatches:
            logger.error(f'More than one game with matching channel {ctx.message.channel.id}')
            return await ctx.send('Error looking up game based on current channel - please contact the bot owner.')
        except exceptions.NoMatches:
            if game_id:
                game = await PolyGame().convert(ctx, int(game_id), allow_cross_guild=False)
                if not await settings.is_bot_channel_strict(ctx):
                    return await ctx.send('This command must be used in a bot spam channel or in a game-specific channel.')
            else:
                ctx.command.reset_cooldown(ctx)
                return await ctx.send(f'Game ID was not included and this does not appear to be a game-specific channel.\n{usage}')
        else:
            game = inferred_game
            logger.debug(f'Inferring game {inferred_game.id} from rename command used in channel {ctx.message.channel.id}')

        if game.is_pending:
            return await ctx.send('This game has not started yet.')

        if not new_game_name:
            return await ctx.send(usage)
        if new_game_name.upper() == 'NONE':
            if settings.get_user_level(ctx.author) <= 3:
                return await ctx.send('You do not have permissions to delete a game name.')
            new_game_name = None
        is_hosted_by, host = game.is_hosted_by(ctx.author.id)
        if not is_hosted_by and not settings.is_staff(ctx.author) and not game.is_created_by(discord_id=ctx.author.id):
            # host_name = f' **{host.name}**' if host else ''
            return await ctx.send(f'Only the game creator **{game.creating_player().name}** or server staff can do this.')
        if new_game_name and not utilities.is_valid_poly_gamename(input=new_game_name):
            if settings.get_user_level(ctx.author) <= 2:
                return await ctx.send('That name looks made up. :thinking: You need to manually create the game __in Polytopia__, come back and input the name of the new game you made.\n'
                    f'You can use `{ctx.prefix}code NAME` to get the code of each player in this game.')
            await ctx.send(':warning: That game name looks made up - you are allowed to override due to your user level.')

        old_game_name = game.name
        game.name = new_game_name
        game_guild = self.bot.get_guild(game.guild_id)
        if not game_guild:
            logger.error(f'Error attempting in rename command for game {game.id} - could not load guild {game.guild_id}')
            return await ctx.send('Error loading guild associated with this game. Please contact the bot owner.')

        game.save()
        if game.update_league_fields():
            league_warning = f'\n:warning: Detected a difference in the season game status. New status is:\nGame season: `{game.league_season}`, Team tier: `{game.league_tier}`,  Playoff game? `{game.league_playoff}`'
        else:
            league_warning = ''

        await game.update_squad_channels(self.bot.guilds, game_guild.id)
        await game.update_announcement(guild=game_guild, prefix=ctx.prefix)
        models.GameLog.write(game_id=game, guild_id=game.guild_id, message=f'{models.GameLog.member_string(ctx.author)} renamed the game to *{discord.utils.escape_markdown(str(new_game_name))}*')

        new_game_name = game.name if game.name else 'None'
        old_game_name = old_game_name if old_game_name else 'None'

        await ctx.send(f'Game ID {game.id} has been renamed to "**{new_game_name}**" from "**{old_game_name}**"{league_warning}')


    @commands.command(aliases=['setmaptype'], usage='game_id map_type')
    @models.is_registered_member()
    async def setmap(self, ctx, *, args: str = None):
        """Set map type for a game

        **Examples**
        `[p]setmap 2055 arch` - Sets the map type to 'Archipelago' for game 2055
        `[p]setmap dry` - Sets the map type while in a game-specific channel
        `[p]setmap none` - Clear the map type for the current game

        """

        if not args:
            return await ctx.send(f'No arguments provided. **Example usage:** `{ctx.prefix}{ctx.invoked_with} 1234 dry`')

        request = game_map.build_mutation_request(
            member=ctx.author,
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            legacy_tokens=tuple(args.split()),
            allow_related_channel=True,
            invoked_with=ctx.invoked_with,
        )

        async def after_commit(result):
            await game_map.publish_mutation_result(
                result,
                send=ctx.send,
                guild=ctx.guild,
                prefix=ctx.prefix,
            )

        try:
            await game_map.run_map_mutation(
                request,
                after_commit=after_commit,
            )
        except game_workers.GameMapLookupError as exc:
            return await ctx.send(
                f'{exc}\n**Example usage:** `{ctx.prefix}{ctx.invoked_with} '
                '1234 dry`\nYou can also omit the game ID if you use the '
                'command from a game-specific channel.'
            )
        except game_workers.GameMapValidationError as exc:
            message = str(exc)
            if message.startswith(
                'This command requires bot registration first.'
            ):
                message = message.replace(
                    '__`setname ',
                    f'__`{ctx.prefix}setname ',
                ).replace(
                    '__`steamname ',
                    f'__`{ctx.prefix}steamname ',
                )
            elif 'No matching map type found' in message:
                raw_map_type = request.legacy_tokens[-1] if request.legacy_tokens else ''
                message = (
                    'No matching map type found for '
                    f'"{discord.utils.escape_mentions(raw_map_type)}". '
                    'Check spelling or try a different name.'
                )
            elif message.startswith('Wrong number of arguments.'):
                message = message.replace(
                    '`help setmaptype`',
                    f'`{ctx.prefix}help setmaptype`',
                )
            return await ctx.send(message)
        except exceptions.RecordLocked as exc:
            return await ctx.send(str(exc))
        except peewee.PeeweeException:
            logger.exception('Database failure setting map for prefix command')
            return await ctx.send(
                'The map change failed and rolled back. No Discord '
                'announcement or card update was made.'
            )
        except Exception:
            logger.exception('Unexpected failure setting map for prefix command')
            return await ctx.send(
                'The map change failed. No Discord announcement or card '
                'update was made.'
            )

    @game_group.command(
        name='map',
        description='View or update a game map type.',
    )
    @discord.app_commands.describe(
        game_id='Game whose map type to view or edit.',
        map_type='Map type to set; omit this to view the current value.',
        clear='Clear the current map type.',
    )
    @discord.app_commands.choices(map_type=GAME_MAP_TYPE_CHOICES)
    async def map_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
        map_type: str | None = None,
        clear: bool = False,
    ):
        """Read or edit one game map through the shared map service."""

        if map_type is not None and clear:
            return await interaction.response.send_message(
                'Choose either a map type or clear, not both.',
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)
        public_send = game_map.public_interaction_sender(interaction)
        channel_id = int(
            getattr(interaction, 'channel_id', None)
            or getattr(getattr(interaction, 'channel', None), 'id', 0)
            or 0
        )

        if map_type is None and not clear:
            request = game_map.build_read_request(
                member=interaction.user,
                guild_id=interaction.guild.id,
                channel_id=channel_id,
                game_id=game_id,
            )
            try:
                result = await game_map.run_map_read(request)
            except game_workers.GameMapValidationError as exc:
                return await interaction.followup.send(
                    str(exc),
                    ephemeral=True,
                )
            except peewee.PeeweeException:
                logger.exception('Database failure reading game map')
                return await interaction.followup.send(
                    'The current map type could not be loaded.',
                    ephemeral=True,
                )
            except Exception:
                logger.exception('Unexpected failure reading game map')
                return await interaction.followup.send(
                    'The current map type could not be loaded.',
                    ephemeral=True,
                )
            return await public_send(game_map.read_message(result))

        request = game_map.build_mutation_request(
            member=interaction.user,
            guild_id=interaction.guild.id,
            channel_id=channel_id,
            game_id=game_id,
            map_type=map_type,
            clear=clear,
            invoked_with='/game map',
        )

        async def after_commit(result):
            await game_map.publish_mutation_result(
                result,
                send=public_send,
                guild=interaction.guild,
                prefix=settings.guild_setting(
                    interaction.guild.id,
                    'command_prefix',
                ),
            )

        try:
            await game_map.run_map_mutation(
                request,
                after_commit=after_commit,
            )
        except game_workers.GameMapValidationError as exc:
            return await interaction.followup.send(
                str(exc),
                ephemeral=True,
            )
        except exceptions.RecordLocked as exc:
            return await interaction.followup.send(
                str(exc),
                ephemeral=True,
            )
        except peewee.PeeweeException:
            logger.exception('Database failure setting map from slash')
            return await interaction.followup.send(
                'The map change failed and rolled back. No Discord '
                'announcement or card update was made.',
                ephemeral=True,
            )
        except Exception:
            logger.exception('Unexpected failure setting map from slash')
            return await interaction.followup.send(
                'The map change failed. No Discord announcement or card '
                'update was made.',
                ephemeral=True,
            )
    

    @commands.command(aliases=['settribes'], usage='game_id player_name tribe_name [player2 tribe2 ... ]')
    @models.is_registered_member()
    async def settribe(self, ctx, *, args: str = None):
        """Set tribe of players for a game

        **Examples**
        `[p]settribe 2055 ai-mo` - Sets your own tribe for a game you are in
        `[p]settribe bardur` - Sets your own tribe while in a game channel

        **Staff usage:**
        `[p]settribe 2055 nelluk bardur` - Sets Nelluk to Bardur for game 2050
        `[p]settribe 2050 nelluk bardur rick lux anarcho none` - Sets several tribes at once. Use *none* to unset a tribe.
        `[p]settribe nelluk bardur rick lux` - Set several tribes in bulk while in a game channel.
        """

        if not args:
            return await ctx.send(f'No arguments provided. **Example usage:** `{ctx.prefix}{ctx.invoked_with} 1234 bardur`')

        if settings.get_user_level(ctx.author) < 4:
            perm_str = f'You only have permissions to set your own tribe. **Example usage:** `{ctx.prefix}{ctx.invoked_with} 1234 bardur`'
        else:
            perm_str = ''

        arg_list = args.split()

        try:
            game = Game.by_channel_or_arg(chan_id=ctx.channel.id, arg=arg_list[0])
        except (ValueError, exceptions.MyBaseException) as e:
            return await ctx.send(f'{e}\n**Example usage:** `{ctx.prefix}{ctx.invoked_with} 1234 bardur`\nYou can also omit the game ID if you use the command from a game-specific channel.')

        if str(game.id) == str(arg_list[0]):
            arg_list = arg_list[1:]  # Remove game ID from list if it was used for lookup
            if game.guild_id != ctx.guild.id and not game.uses_channel_id(ctx.channel.id):
                return await ctx.send(f'Game {game.id} is associated with a different discord server. Use this command from that server or a game-specific channel.')

        logger.debug(f'Attempting settribe for game {game.id}')

        if settings.get_user_level(ctx.author) < 4 or len(arg_list) == 1:
            # if non-priviledged user, force the command to be about the ctx.author
            arg_list = [f'<@{ctx.author.id}>', arg_list[0] if arg_list else ' ']

        if len(arg_list) % 2 != 0 or len(arg_list) == 0:
            return await ctx.send(f'Wrong number of arguments. See `{ctx.prefix}help settribe` for usage examples.')

        for i in range(0, len(arg_list), 2):
            # iterate over args two at a time

            player_name = arg_list[i]
            tribe_name = arg_list[i + 1]

            if tribe_name.upper() == 'NONE':
                tribe = None

            else:
                tribe = Tribe.get_by_name(name=tribe_name)
                if not tribe:
                    await ctx.send(f'Matching Tribe not found matching "{discord.utils.escape_mentions(tribe_name)}". Check spelling or be more specific. {perm_str}')
                    continue

            lineup_match = game.player(name=player_name)

            if not lineup_match:
                await ctx.send(f'Matching player not found in game {game.id} matching "{utilities.escape_role_mentions(player_name)}". Check spelling or be more specific. {perm_str}')
                continue

            lineup_match.tribe = tribe
            lineup_match.save()
            await ctx.send(f'Player **{lineup_match.player.name}** assigned to tribe *{tribe.name if tribe else "None"}* in game {game.id} {tribe.emoji if tribe else ""}')
            models.GameLog.write(game_id=game.id, guild_id=game.guild_id, message=f'{models.GameLog.member_string(ctx.author)} assigned tribe of player {models.GameLog.member_string(lineup_match.player.discord_member)} to *{tribe.name if tribe else "None"}*')

        game = game.load_full_game()
        await game.update_announcement(guild=ctx.guild, prefix=ctx.prefix)
    
    @settings.in_bot_channel()
    @commands.command(usage='search_term', aliases=['gamelog', 'gamelogs', 'global_logs', 'log'])
    # @commands.cooldown(1, 20, commands.BucketType.user)
    async def logs(self, ctx, *, search_term: str = None):
        """Lists or searches log entries. Non-staff users must provide a game ID for a game they are in.

         **Examples**
        `[p]logs` - See all recent entries
        `[p]logs 1234` - See all entries related to a specific game
        `[p]logs Nelluk` - See all entries containing the term Nelluk
        `[p]logs Nelluk join` - See all entries containing both words
        `[p]logs Nelluk -Kamfer` - See all entries containing the first word but *not* the second word

        `[p]global_logs` - *Owner only*: Search or list log entries across all bot servers
        """

        if not settings.is_staff(ctx.author):
            if not search_term or not search_term.strip().isnumeric():
                return await ctx.send('You do not have permission to view these logs.')

            game = Game.get_or_none((Game.id == int(search_term)) & (Game.guild_id == ctx.guild.id))
            if not game:
                return await ctx.send('No matching game was found.')

            is_player, _ = game.has_player(discord_id=ctx.author.id)
            if not is_player:
                return await ctx.send('You do not have permission to view these logs.')

        paginated_message_list = []

        search_term = re.sub(r'\b(\d{4,6})\b', r'_\1_', search_term, count=1) if search_term else None
        # Above finds a 4-6 digit number in search_term and adds underscores around it
        # This will cause it to match against the __GAMEID__ the log entries are prefixed with and not substrings from
        # user IDs

        search_term = re.sub(r'<@[!&]?([0-9]{17,21})>', '\\1', search_term) if search_term else None
        # replace @Mentions <@272510639124250625> with just the ID 272510639124250625

        negative_parameter = re.search(r'-(\S+)', search_term) if search_term else ''
        # look for the first term preceded by a - character
        if negative_parameter:
            negative_term = negative_parameter[1]
            search_term = search_term.replace(negative_parameter[0], '').replace('  ', ' ').strip()
            negative_title_str = f'\nExcluding entries containing *{negative_term}*'
        else:
            negative_term = None
            negative_title_str = ''

        if search_term:
            title_str = f'Searching for log entries containing *{search_term}*{negative_title_str}'.replace('_', '')
        else:
            title_str = f'All recent log entries{negative_title_str}'

        guild_id = ctx.guild.id
        if ctx.invoked_with == 'global_logs':
            if ctx.author.id == settings.owner_id:
                guild_id = None  # search globally, owner only
            else:
                return await ctx.send('Only the bot owner can search global logs.')

        entries = models.GameLog.search(keywords=search_term, negative_keyword=negative_term, guild_id=guild_id)
        for entry in entries:
            paginated_message_list.append((f'`{entry.message_ts.strftime("%Y-%m-%d %H:%M:%S")}`', entry.message[:500]))

        await utilities.paginate(self.bot, ctx, title=title_str, message_list=paginated_message_list, page_start=0, page_end=10, page_size=10)

    async def game_search(self, ctx, mode: str, arg_list):
        target_list = [
            arg.replace('"', '') for arg in arg_list
            if len(arg.replace('"', '')) > 2
        ]
        show_all = (
            len(target_list) == 1 and target_list[0].upper() == 'ALL'
        )
        if show_all:
            target_list = []
        elif not target_list:
            target_list = [str(ctx.author.id)]
        key = {
            'ALLGAMES': game_search_workers.GameSearchKey(),
            'COMPLETE': game_search_workers.GameSearchKey(
                status='completed',
            ),
            'INCOMPLETE': game_search_workers.GameSearchKey(
                status='unfinished',
            ),
            'WINS': game_search_workers.GameSearchKey(outcome='win'),
            'LOSSES': game_search_workers.GameSearchKey(outcome='loss'),
        }.get(mode.upper(), game_search_workers.GameSearchKey())
        await self._send_game_search_workspace(
            ctx,
            query=' '.join(target_list),
            key=key,
        )

    async def task_purge_game_channels(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            # purge game channels from games that were concluded at least 24 hours ago
            # restricted games to those that concluded less than 14 days ago
            # previously was limiting it to 7 days, but made a change May 2023 to check season game status more efficiently instead of
            # once per game, which should make this task more efficient.
            
            await asyncio.sleep(900)
            logger.debug('Task running: task_purge_game_channels')
            yesterday = (datetime.datetime.now() + datetime.timedelta(hours=-24))
            last_week = (datetime.datetime.now() + datetime.timedelta(days=-14))

            utilities.connect()
            old_games = Game.select().join(GameSide, on=(GameSide.game == Game.id)).where(
                (Game.is_confirmed == 1) & (Game.completed_ts < yesterday) & (Game.completed_ts > last_week) &
                ((GameSide.team_chan.is_null(False)) | (Game.game_chan.is_null(False)))
            )

            logger.info(f'running task_purge_game_channels on {len(old_games)} games')

            for game in old_games:
                if game.league_season:
                    logger.debug(f'Skipping purge of game {game.id} since it is a season game')
                    continue
                guild = self.bot.get_guild(game.guild_id)
                if guild:
                    await game.delete_game_channels(self.bot.guilds, game.guild_id)

            await asyncio.sleep(60 * 60 * 6)

    async def task_set_champion_role(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():

            await asyncio.sleep(97)
            logger.debug('Task running: task_set_champion_role')
            utilities.connect()
            await achievements.set_champion_role()

            await asyncio.sleep(60 * 60 * 2)


async def post_win_messaging(guild, prefix, current_chan, winning_game):

    purge_message = '*This channel will be purged soon.* Purging will be skipped if the channel or its category has "archive" in the name, or has "Manage Channel" denied to me.'
    reminder_message = ''
    if winning_game.is_season_game():
        reminder_message = f'\n:bulb: Please use `{prefix}setmap` to log the map and `{prefix}settribes` to log the tribes that were selected.'
        purge_message = f'This channel will not be purged as it is a Season game.\n{reminder_message}'
    elif winning_game.is_uncaught_season_game():
        reminder_message = f'\n:bulb: This game looks like an incorrectly named **Season Game**! You might want to use `{prefix}rename` and include the season tag at the beginning.'

    await winning_game.update_squad_channels(guild_list=settings.bot.guilds, guild_id=guild.id, message=f'The game is over with **{winning_game.winner.name()}** victorious. {purge_message}')
    models.GameLog.write(game_id=winning_game.id, guild_id=winning_game.guild_id, message='Win is confirmed and ELO changes processed.')
    embed, content = winning_game.embed(guild=guild, prefix=prefix)

    for l in winning_game.lineup:
        await achievements.set_experience_role(l.player.discord_member)

    logger.debug(f'calling auto_grad_novas from post_win_messaging for game {winning_game.id}')
    await auto_grad_novas(guild, winning_game, current_chan)
    
    if settings.guild_setting(guild.id, 'game_announce_channel') is not None:
        channel = guild.get_channel(settings.guild_setting(guild.id, 'game_announce_channel'))
        if channel is not None:
            await channel.send(f'Game concluded! Congrats **{winning_game.winner.name()}**. Roster: {" ".join(winning_game.mentions())}')
            await image_storage.send_game_embed(channel, winning_game, embed=embed)
            return await current_chan.send(f'Game concluded! See {channel.mention} for full details.')

    await current_chan.send(f'Game concluded! Congrats **{winning_game.winner.name()}**. Roster: {" ".join(winning_game.mentions())}{reminder_message}')
    await image_storage.send_game_embed(
        current_chan, winning_game, embed=embed, content=content
    )


async def post_unwin_messaging(guild, prefix, current_chan, game, previously_confirmed: bool = False):

    await game.update_squad_channels(guild_list=settings.bot.guilds, guild_id=guild.id, message='The game has reset to *Incomplete* status.')

    if previously_confirmed:
        for l in game.lineup:
            await achievements.set_experience_role(l.player.discord_member)

    await current_chan.send(f'Game reset to *Incomplete*. Previously claimed win has been canceled.  Notifying game roster: {" ".join(game.mentions())}')


async def post_newgame_messaging(ctx, game):

    season, season_str = game.is_season_game(), ''
    if season:
        try:
            tier_name = settings.tier_lookup(game.league_tier)[1]
        except exceptions.NoMatches:
            tier_name = 'Unknown'
        season_str = f'**{tier_name} Season {season[0]}** '

    embed, content = game.embed(guild=ctx.guild, prefix=ctx.prefix)
    ranked_str = 'unranked ' if not game.is_ranked else ''
    platform_str = '' if game.is_mobile else 'Steam '
    announce_str = f'New {season_str}{ranked_str}{platform_str}game ID **{game.id}** started! Roster: {" ".join(game.mentions())}'

    if settings.guild_setting(ctx.guild.id, 'game_announce_channel'):
        channel = ctx.guild.get_channel(settings.guild_setting(ctx.guild.id, 'game_announce_channel'))
        if channel:
            await channel.send(f'{announce_str}')
            announcement = await image_storage.send_game_embed(
                channel, game, embed=embed, content=content
            )
            await ctx.send(f'New {ranked_str}game ID **{game.id}** started! See {channel.mention} for full details.')
            game.announcement_message = announcement.id
            game.announcement_channel = announcement.channel.id
            game.save()
        else:
            await image_storage.send_game_embed(
                ctx, game, embed=embed, content=content
            )
            await ctx.send('Error loading game announcement channel from server settings. Please inform the bot owner.')
            logger.error(f'Could not load game_announce_channel channel for guild {ctx.guild.id}')

    else:
        await ctx.send(f'{announce_str}')
        await image_storage.send_game_embed(
            ctx, game, embed=embed, content=content
        )

    if settings.guild_setting(ctx.guild.id, 'game_channel_categories'):
        try:
            await game.create_game_channels(settings.bot.guilds, ctx.guild.id)
        except exceptions.MyBaseException as e:
            await ctx.send(f':warning: **Channel creation error:** {e}')

    if game.is_uncaught_season_game():
        await ctx.send(f':bulb: This game looks like an incorrectly named **Season Game**! You might want to use `{ctx.prefix}rename` and include the season tag at the beginning.')
    if season and game.gamesides[0].team.is_hidden:
        await ctx.send(f':warning: This game is marked as a **Season Game** but is not associated with a League Team. There are probably players with mixed roles on a side. I suggest you `{ctx.prefix}unstart`, fix the roles, and re-`{ctx.prefix}start`.')
    if game.guild_id == settings.server_ids['polychampions'] and game.smallest_team() > 1:
        populate_league_team_channels()

    await auto_grad_novas(ctx.guild, game, ctx)


def parse_players_and_teams(input_list, guild_id: int):
    # Given a [List, of, string, args], try to match each one against a Team or a Player, and return lists of those matches
    # return any args that matched nothing back in edited input_list

    player_matches, team_matches = [], []
    for arg in list(input_list):  # Copy of list
        if arg.upper() in ['THE', 'OF', 'AND', '&']:
            input_list.remove(arg)
            continue
        if arg.isupper():
            continue  # UPPER CASE alphabetical are ignored for player/team comparison and assumed to be title searches
        teams = Team.get_by_name(arg, guild_id)
        if len(teams) == 1:
            team_matches.append(teams[0])
            logger.debug(f'parse_players_and_teams - Matched string {arg} to team {teams[0].id} {teams[0].name}')
            input_list.remove(arg)
        else:
            players = Player.string_matches(player_string=arg, guild_id=guild_id, include_poly_info=False)
            if len(players) > 0:
                player_matches.append(players[0])
                logger.debug(f'parse_players_and_teams - Matched string {arg} to player {players[0].id} {players[0].name} on team {players[0].team}')
                input_list.remove(arg)

    return player_matches, team_matches, input_list


async def setup(bot):
    await bot.add_cog(polygames(bot))
    # bot.load_extension('modules.games')
