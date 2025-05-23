import discord
from discord.ext import commands
import modules.utilities as utilities
import settings
import modules.exceptions as exceptions
import modules.achievements as achievements
from modules import channels
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

logger = logging.getLogger('polybot.' + __name__)
elo_logger = logging.getLogger('polybot.elo')


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
                    await ctx.send(embed=embed)

                await ctx.send(f'Game with ID {game_id} is associated with a different Discord server: __{server_name}__.{game_summary_str}')
                raise commands.UserInputError()
            return game


class polygames(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        if settings.run_tasks:
            self.bg_task = bot.loop.create_task(self.task_purge_game_channels())
            self.bg_task2 = bot.loop.create_task(self.task_set_champion_role())

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

    @settings.in_bot_channel_strict()
    @commands.command(aliases=['leaderboard', 'leaderboards', 'lbglobal', 'lbg'])
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
        Includes players who have not played recently. By default the leaderboard drops players who have not played in 90 days.

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

        leaderboard = []
        max_flag, global_flag, version = False, False, None
        target_model = Player
        lb_title = 'Individual Leaderboard'
        date_cutoff = settings.date_cutoff

        if ctx.invoked_with == 'lbglobal' or ctx.invoked_with == 'lbg':
            filters = filters + 'GLOBAL'

        if 'GLOBAL' in filters.upper():
            global_flag = True
            lb_title = 'Global Leaderboard'
            target_model = DiscordMember

        if 'ALLPLAYERS' in filters.upper():
            lb_title += ' - Including Inactive Players'
            date_cutoff = datetime.date.min

        if 'MAX' in filters.upper():
            max_flag = True  # leaderboard ranked by player.max_elo
            lb_title += ' - Maximum ELO Achieved'

        if 'ALLTIME' in filters.upper():
            version = 'ALLTIME'  # leaderboard ranked by player.elo_alltime
            lb_title += ' - Alltime (not reset)'

        def process_leaderboard():
            utilities.connect()
            leaderboard_query = target_model.leaderboard(date_cutoff=date_cutoff, guild_id=ctx.guild.id, max_flag=max_flag, version=version)

            for counter, player in enumerate(leaderboard_query[:2000]):
                wins, losses = player.get_record(version=version)
                emoji_str = player.team.emoji if not global_flag and player.team else ''

                leaderboard.append(
                    (f'{(counter + 1):>3}. {emoji_str}{player.name}', f'`ELO {player.elo_field}\u00A0\u00A0\u00A0\u00A0W {wins} / L {losses}`')
                )
            return leaderboard, leaderboard_query.count()

        async with ctx.typing():
            leaderboard, leaderboard_size = await self.bot.loop.run_in_executor(None, process_leaderboard)

        # if ctx.guild.id != settings.server_ids['polychampions']:
        #     await ctx.send('Powered by PolyChampions. League server with a team focus and competitive players.\n'
        #         'Supporting up to 6-player team ELO games and automatic team channels. - <https://tinyurl.com/polychampions>')
        #     # link put behind url shortener to not show big invite embed
        await utilities.paginate(self.bot, ctx, title=f'**{lb_title}**\n{leaderboard_size} ranked players', message_list=leaderboard, page_start=0, page_end=10, page_size=10)

    @settings.in_bot_channel_strict()
    @commands.command(aliases=['recent', 'active', 'lbactivealltime'], hidden=True)
    @commands.cooldown(2, 30, commands.BucketType.channel)
    async def lbrecent(self, ctx):
        """ Display most active recent players

        Alternative command is `[p]lbactivealltime`
        """
        last_month = (datetime.datetime.now() + datetime.timedelta(days=-30))

        leaderboard = []

        query = Player.select(Player, peewee.fn.COUNT(Lineup.id).alias('count')).join(Lineup).join(Game).where(
            (Lineup.player == Player.id) & ((Game.date > last_month) | (Game.completed_ts > last_month)) & (Game.guild_id == ctx.guild.id)
        ).group_by(Player.id).order_by(-peewee.SQL('count'))

        if ctx.invoked_with == 'lbactivealltime':
            # special command to see all time active list by discord member
            query = DiscordMember.select(DiscordMember, peewee.fn.COUNT(Lineup.id).alias('count')).join(Player).join(Lineup).join(Game).where(
                (Lineup.player.discord_member == DiscordMember.id) & (Game.is_pending == 0)
            ).group_by(DiscordMember.id).order_by(-peewee.SQL('count'))

            for counter, discord_member in enumerate(query[:1000]):
                wins, losses = discord_member.get_record()
                leaderboard.append(
                    (f'{(counter + 1):>3}. {discord_member.name}', f'`ELO {discord_member.elo_moonrise}\u00A0\u00A0\u00A0\u00A0Games Played {discord_member.count}`')
                )
            title = '**Most active players of all time**'
        else:
            for counter, player in enumerate(query[:500]):
                wins, losses = player.get_record()
                emoji_str = player.team.emoji if player.team else ''
                leaderboard.append(
                    (f'{(counter + 1):>3}. {emoji_str}{player.name}', f'`ELO {player.elo_moonrise}\u00A0\u00A0\u00A0\u00A0Recent Games {player.count}`')
                )
            title = f'**Most Active Recent Players**\n{query.count()} players in past 30 days'

        # if ctx.guild.id != settings.server_ids['polychampions']:
        #     await ctx.send('Powered by PolyChampions. League server with a team focus and competitive players.\n'
        #         'Supporting up to 6-player team ELO games and automatic team channels. - <https://tinyurl.com/polychampions>')
        #     # link put behind url shortener to not show big invite embed
        await utilities.paginate(self.bot, ctx, title=title, message_list=leaderboard, page_start=0, page_end=10, page_size=10)

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
        `[p]lbsquad` - Current leaderboard. Squads who have not played a game in 90 days are not included.
        `[p]lbsquad alltime` - Alltime leaderboard.
        """

        leaderboard = []
        lb_title = 'Squad Leaderboard'
        date_cutoff = settings.date_cutoff

        if 'ALLTIME' in filters.upper():
            lb_title += ' - Alltime'
            date_cutoff = datetime.date.min

        def process_leaderboard():
            utilities.connect()
            squads = Squad.leaderboard(date_cutoff=date_cutoff, guild_id=ctx.guild.id)
            for counter, sq in enumerate(squads[:500]):
                wins, losses = sq.get_record()
                squad_members = sq.get_members()
                emoji_list = [p.team.emoji for p in squad_members if p.team is not None]
                emoji_string = ' '.join(emoji_list)
                squad_member_names = ' / '.join(sq.get_names())
                squad_name_str = f'{sq.name}\n' if sq.name else ''

                leaderboard.append(
                    (f'{(counter + 1):>3}. {squad_name_str}{emoji_string}{squad_member_names}', f'`#{sq.id} (ELO: {sq.elo:4}) W {wins} / L {losses}`')
                )
            return leaderboard, squads.count()

        async with ctx.typing():
            leaderboard, leaderboard_size = await self.bot.loop.run_in_executor(None, process_leaderboard)

        await utilities.paginate(self.bot, ctx, title=f'**{lb_title}**\n{leaderboard_size} ranked squads', message_list=leaderboard, page_start=0, page_end=10, page_size=10)

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

    @settings.in_bot_channel()
    @commands.command(brief='See details on a player', usage='player_name', aliases=['elo', 'rank'])
    async def player(self, ctx, *, args=None):
        """See your own player card or the card of another player
        This also will find results based on a game-code or in-game name, if set.

        **Examples**
        `[p]player` - See your own player card
        `[p]player Nelluk` - See Nelluk's card
        """
        # hidden argument: add 'alltime' as an argument to view alltime ELO.
        args_list = args.lower().split() if args else []

        if 'alltime' in args_list:
            alltime_flag = True
            args_list.remove('alltime')
        else:
            alltime_flag = False

        if len(args_list) == 0:
            # Player looking for info on themselves
            args_list.append(f'<@{ctx.author.id}>')

        # Otherwise look for a player matching whatever they entered
        player_mention = ' '.join(args_list)
        player_mention_safe = utilities.escape_role_mentions(player_mention)

        guild_matches = await utilities.get_guild_member(ctx, player_mention)
        if len(guild_matches) == 1:
            # If there is one exact match from active guild members, use their precise member ID to pull up the player
            # Helps in a scenario where there are two existing players in the DB with the same name, but only one is actively on the server
            player_mention = str(guild_matches[0].id)

        player_results = Player.string_matches(player_string=player_mention, guild_id=ctx.guild.id)

        if len(player_results) > 1:
            p_names = [f'{p.mention()} ({p.name})' for p in player_results]
            p_names_str = ', '.join(p_names[:10])
            return await ctx.send(f'Found {len(player_results)} players matching *{player_mention_safe}*. Be more specific or use an @Mention.\nFound: {p_names_str}', allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=False))
        elif len(player_results) == 0:
            # No Player matches - check for guild membership
            if len(guild_matches) > 1:
                p_names = [f'{p.mention} ({p.name})' for p in guild_matches]
                p_names_str = ', '.join(p_names[:10])
                return await ctx.send(f'There is more than one member found with name *{player_mention_safe}*. Be more specific or use an @Mention.\nFound: {p_names_str}', allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=False))
            if len(guild_matches) == 0:
                return await ctx.send(f'Could not find *{player_mention_safe}* by Discord name, Polytopia name, or Polytopia ID.')

            player, _ = Player.get_by_discord_id(discord_id=guild_matches[0].id, discord_name=guild_matches[0].name, discord_nick=guild_matches[0].nick, guild_id=ctx.guild.id)
            if not player:
                # Matching guild member but no Player or DiscordMember
                return await ctx.send(f'*{player_mention_safe}* was found in the server but is not registered with me. '
                    f'Players can register themselves with  `{ctx.prefix}setname Your In-Game Name`.')
            # if still running here that means there was a DiscordMember match not in current guild, and upserted into guild
        else:
            player = player_results[0]

        def async_create_player_embed():
            utilities.connect()
            wins, losses = player.get_record(version='alltime' if alltime_flag else None)
            rank, lb_length = player.leaderboard_rank(settings.date_cutoff)

            wins_g, losses_g = player.discord_member.get_record(version='alltime' if alltime_flag else None)
            rank_g, lb_length_g = player.discord_member.leaderboard_rank(settings.date_cutoff)

            polychamps_record = player.discord_member.get_polychamps_record()

            image = None
            air_record = []

            if alltime_flag:
                elo = player.elo_alltime
                g_elo = player.discord_member.elo_alltime
                elo_max = player.elo_max_alltime
                g_elo_max = player.discord_member.elo_max_alltime
            else:
                if models.is_post_moonrise():
                    elo = player.elo_moonrise
                    g_elo = player.discord_member.elo_moonrise
                    elo_max = player.elo_max_moonrise
                    g_elo_max = player.discord_member.elo_max_moonrise

                    air_record_g = player.discord_member.get_record(version='air')

                    if air_record_g[0] or air_record_g[1]:
                        air_record_l = player.get_record(version='air')
                        air_record = [('Global Record', f'W {air_record_g[0]} / L {air_record_g[1]}'),
                                      ('Global ELO', f'{player.discord_member.elo} / {player.discord_member.elo_max} Max'),
                                      ('Local Record', f'W {air_record_l[0]} / L {air_record_l[1]}'),
                                      ('Local ELO', f'{player.elo} / {player.elo_max} Max')]
                else:
                    elo = player.elo
                    g_elo = player.discord_member.elo
                    elo_max = player.elo_max
                    g_elo_max = player.discord_member.elo_max

            if rank is None:
                rank_str = 'Unranked'
            else:
                rank_str = f'{rank} of {lb_length}'

            results_str = f'ELO: {elo}\nW\u00A0{wins}\u00A0/\u00A0L\u00A0{losses}'

            if rank_g:
                rank_str = f'{rank_str}\n{rank_g} of {lb_length_g} *Global*'
                results_str = f'{results_str}\n**Global**\nELO: {g_elo}\nW\u00A0{wins_g}\u00A0/\u00A0L\u00A0{losses_g}'

            # embed = discord.Embed(title=f'Player card for __{player.name}__')
            embed = discord.Embed(description=f'__{"Alltime ELO " if alltime_flag else ""}Player card for <@{player.discord_member.discord_id}>__')
            embed.add_field(name='**Results**', value=results_str)
            embed.add_field(name='**Ranking**', value=rank_str)

            guild_member = ctx.guild.get_member(player.discord_member.discord_id)
            if guild_member:
                # embed.set_thumbnail(url=guild_member.avatar_url_as(size=512))
                embed.set_thumbnail(url=guild_member.display_avatar.replace(size=512, format='webp'))
            
            content_str = ''
            if player.team:
                team_str = f'{player.team.name} {player.team.emoji}' if player.team.emoji else player.team.name
                embed.add_field(name='**Last-known Team**', value=team_str)
            if player.discord_member.polytopia_name:
                embed.add_field(name='Polytopia Game Name', value=player.discord_member.polytopia_name[:1000])
                content_str = player.discord_member.polytopia_name  # Used as a single message before player card so users can easily copy/paste Poly ID
            if player.discord_member.name_steam:
                embed.add_field(name='Steam Name', value=player.discord_member.name_steam)

            if player.discord_member.trophies:
                if 'polympics2021' in player.discord_member.trophies:
                    embed.add_field(name='Polympics 2021 Trophies', value=player.discord_member.trophies['polympics2021'])

            if player.discord_member.timezone_offset:
                offset_str = f'UTC+{player.discord_member.timezone_offset}' if player.discord_member.timezone_offset > 0 else f'UTC{player.discord_member.timezone_offset}'
                embed.add_field(value=offset_str, name='Timezone Offset', inline=True)

            if polychamps_record:

                # Limiting record to first 4 entries; full season plus highest three tiers
                record_truncated = list(polychamps_record.items())[:4]
                pc_record_str_list = []
                for rec in record_truncated[1:4]:
                    tier_name = settings.tier_lookup(rec[0])[1]
                    pc_record_str_list.append(f'{tier_name} Tier: {rec[1][0]}W / {rec[1][1]}L')

                embed.add_field(value='\n'.join(pc_record_str_list), name=f'PolyChampions Record {record_truncated[0][1][0]}W / {record_truncated[0][1][1]}L', inline=True)


            misc_stats = []
            (winning_streak, losing_streak, v2_count, v3_count, duel_wins, duel_losses, wins_as_host, ranked_games_played) = player.discord_member.advanced_stats()
            if winning_streak or losing_streak:
                misc_stats.append(('Longest streaks', f'{winning_streak} wins, {losing_streak} losses'))
            if v2_count:
                misc_stats.append(('1v2 games won', v2_count))
            if v3_count:
                misc_stats.append(('1v3 games won', v3_count))
            if duel_wins or duel_losses:
                misc_stats.append(('1v1 games', f'W {duel_wins} / L {duel_losses}'))
            # misc_stats.append(('Wins as game host', f'W {wins_as_host} / L {ranked_games_played - wins_as_host} ({int((wins_as_host / ranked_games_played) * 100)}%)'))

            # TODO: maybe "adjusted ELO" for how big game is?

            if g_elo_max > 1000:
                misc_stats.append(('Max ELO achieved', f'{g_elo_max} G \u200b - \u200b {elo_max} L'))

            favorite_tribes = player.discord_member.favorite_tribes(limit=3)
            if favorite_tribes:
                tribes_str = ' '.join([f'{t["emoji"] if t["emoji"] else t["name"]}' for t in favorite_tribes])
                misc_stats.append(('Most-logged tribes', tribes_str))

            misc_stats = [f'`{stat[0]:.<25}` {stat[1]}' for stat in misc_stats]
            misc_stats = [stat.replace(".", "\u200b ") for stat in misc_stats]

            if misc_stats:
                embed.add_field(name='__Miscellaneous Global Stats__', value='\n'.join(misc_stats), inline=False)

            if air_record:
                air_record = [f'`{stat[0]:.<25}` {stat[1]}' for stat in air_record]
                air_record = [stat.replace(".", "\u200b ") for stat in air_record]
                embed.add_field(name='__Pre-Moonrise Reset Stats__', value='\n'.join(air_record), inline=False)

            global_elo_history_query = (Player
                .select(Game.completed_ts, Lineup.elo_after_game_global, Lineup.elo_after_game_global_moonrise, Lineup.elo_after_game_global_alltime)
                .join(Lineup)
                .join(Game)
                .where((Player.discord_member_id == player.discord_member_id) & ((Lineup.elo_after_game_global.is_null(False)) | (Lineup.elo_after_game_global_moonrise.is_null(False))))
                .order_by(Game.completed_ts))

            global_elo_history_dates = [l.completed_ts for l in global_elo_history_query.objects()]

            # if global_elo_history_dates:
            local_elo_history_query = (Lineup
                .select(Game.completed_ts, Lineup.elo_after_game, Lineup.elo_after_game_alltime, Lineup.elo_after_game_moonrise)
                .join(Game)
                .where((Lineup.player_id == player.id) & ((Lineup.elo_after_game.is_null(False)) | (Lineup.elo_after_game_moonrise.is_null(False))))
            )

            local_elo_history_dates = [l.completed_ts for l in local_elo_history_query.objects()]
            local_elo_history_elos = [l.elo_after_game_alltime for l in local_elo_history_query.objects()] if alltime_flag else [l.elo_after_game or l.elo_after_game_moonrise for l in local_elo_history_query.objects()]

            global_elo_history_elos = [l.elo_after_game_global_alltime for l in global_elo_history_query.objects()] if alltime_flag else [l.elo_after_game_global or l.elo_after_game_global_moonrise for l in global_elo_history_query.objects()]

            try:
                server_name = settings.guild_setting(guild_id=player.guild_id, setting_name='display_name')
            except exceptions.CheckFailedError:
                server_name = settings.guild_setting(guild_id=None, setting_name='display_name')

            if global_elo_history_dates or local_elo_history_dates:

                plt.style.use('default')

                plt.switch_backend('Agg')

                fig, ax = plt.subplots()
                fig.suptitle(f'{"Alltime" if alltime_flag else ""} ELO History (' + server_name + ')', fontsize=16)
                fig.autofmt_xdate()

                plt.plot(local_elo_history_dates, local_elo_history_elos, 'o', markersize=3, label=server_name)
                plt.plot(global_elo_history_dates, global_elo_history_elos, 'o', markersize=3, label='Global')

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

            games_list = Game.search(player_filter=[player])
            if not games_list:
                recent_games_str = 'No games played'
            else:
                recent_games_count = player.games_played(in_days=30).count()
                recent_games_str = f'__Most recent games ({len(games_list)} total, {recent_games_count} recently):__'
            embed.add_field(value='\u200b', name=recent_games_str, inline=False)

            game_list = utilities.summarize_game_list(games_list[:5])
            for game, result in game_list:
                embed.add_field(name=game, value=result, inline=False)

            if player.discord_member.discord_id != ctx.author.id:
                # Look up 1v1 record between ctx.author and the card target
                try:
                    author_player = Player.get_or_except(player_string=str(ctx.author.id), guild_id=ctx.guild.id)
                except exceptions.MyBaseException:
                    matchup_games = []  # author not registered
                else:
                    matchup_games = Game.search(player_filter=[player, author_player], size_filter=[1, 1]).limit(1)
            else:
                matchup_games = []

            return content_str, embed, image, matchup_games

        async with ctx.typing():
            content_str, embed, image, matchup_games = await self.bot.loop.run_in_executor(None, async_create_player_embed)

        field_counter = 0
        for field in embed.fields:
            field_counter += 1
        await ctx.send(content=content_str, file=image, embed=embed)

        if matchup_games:
            series_record = matchup_games[0].series_record()
            await ctx.send(f'Your local 1v1 record against this opponent: **{series_record[0][0].name()}** {series_record[0][1]} wins - **{series_record[1][0].name()}** {series_record[1][1]} wins')
        if settings.recalculation_mode:
            await ctx.send(f':warning: {ctx.author.mention} - I am currently recalculating the results of prior games. Results from player cards will be incomplete.')

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
        if team.image_url:
            embed.set_thumbnail(url=team.image_url)

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

        await ctx.send(file=image, embed=embed)

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
        elif ctx.invoked_with == 'setname' and 'YOUR' in new_id.upper() and 'GAME' in new_id.upper() and 'NAME' in new_id.upper():
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

        # Converting manually here to handle case of user passing a game name so info can be redirected to games() command
        game = await PolyGame().convert(ctx, game_search)

        embed, content = game.embed(guild=ctx.guild, prefix=ctx.prefix)
        return await ctx.send(embed=embed, content=content)

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

        # TODO: make all caps argument like OCEANS force it to a title search?
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
        Use `[p]newsteamgame` or `[p]newsteamgameunranked` to specify Steam platform.
        """

        if ctx.guild.id == 814317488418193478 and not settings.is_staff(ctx.author):
            return await ctx.send('For **The Polympics** only server staff may open games.')

        ranked_flag = not (ctx.invoked_with in ['newgameunranked', 'newsteamgameunranked'])
        is_mobile = ctx.invoked_with in ['newgame', 'newgameunranked']

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

        if len(args) == 1:
            args_list = [str(ctx.author.id), 'vs', args[0]]
        else:
            args_list = list(args)

        player_groups = [list(group) for k, group in groupby(args_list, lambda x: x.lower() in ('vs', 'versus')) if not k]
        # split ['foo', 'bar', 'vs', 'baz', 'bat'] into [['foo', 'bar']['baz', 'bat']]

        total_players = sum(map(len, player_groups))
        game_allowed, join_error_message = settings.can_user_join_game(
            user_level=settings.get_user_level(ctx.author), game_size=total_players, is_ranked=ranked_flag, is_host=True
        )
        if not game_allowed:
            return await ctx.send(join_error_message)

        discord_groups = []
        author_found = False
        for group in player_groups:
            # Convert each arg into a Discord guild member and build a new
            # list of lists, or return if any arg can't be matched.
            discord_group = []
            for p in group:
                guild_matches = await utilities.get_guild_member(ctx, p)
                if len(guild_matches) == 0:
                    return await ctx.send(
                        f'Could not match "**{p}**" to a server member. '
                        'Try using an @Mention.'
                    )
                if len(guild_matches) > 1:
                    return await ctx.send(
                        f'More than one server matches found for "**{p}**". '
                        'Try being more specific or using an @Mention.'
                    )
                if guild_matches[0] == ctx.author:
                    author_found = True
                discord_group.append(guild_matches[0])
            discord_groups.append(discord_group)

        if not author_found and not settings.is_staff(ctx.author):
            # TODO: possibly allow this in PolyChampions
            # (rickdaheals likes to do this)
            return await ctx.send(
                'You can\'t create a game that you are not a participant in.'
            )

        logger.info(
            'All input checks passed. Creating new game records with args: '
            f'{args}'
        )
        newgame = None
        with db.atomic():
            try:
                newgame, warnings = Game.create_game(
                    discord_groups, name=game_name, is_ranked=ranked_flag,
                    guild_id=ctx.guild.id, is_mobile=is_mobile,
                    mod_override=settings.is_mod(ctx.author)
                )
                if warnings:
                    await ctx.send('\n'.join(warnings))
                host_player, _ = Player.get_by_discord_id(
                    discord_id=ctx.author.id, guild_id=ctx.guild.id, discord_name=ctx.author.name, discord_nick=ctx.author.nick
                )
                if host_player:
                    newgame.host = host_player
                    newgame.save()
                else:
                    logger.error('Could not add host for newgame')
            except (peewee.PeeweeException, exceptions.CheckFailedError) as e:
                logger.error(f'Error creating new game: {e}')
                await ctx.send(f'Error creating new game: {e}')
            except ValueError as e:
                await ctx.send(e)

        if newgame:
            models.GameLog.write(
                game_id=newgame, guild_id=ctx.guild.id,
                message=(
                    f'{models.GameLog.member_string(ctx.author)} created '
                    f'game with `{ctx.invoked_with}` command with name '
                    f'*{discord.utils.escape_markdown(newgame.name)}*'
                )
            )
            await post_newgame_messaging(ctx, game=newgame)

    @settings.in_bot_channel_strict()
    @models.is_registered_member()
    @commands.command(usage='game_id winner_name', aliases=['lose'])
    async def win(self, ctx, winning_game: PolyGame = None, *, winning_side_name: str = None):
        """
        Declare winner of an existing game

        The win will be finalized when multiple players confirm the winner, or after approximately 24 hours if no other players confirm.

        If declaring your own victory it can be good practice to post a screenshot indicating that you are the last human player remaining,
        in case there is a later dispute over the outcome.

        **Examples:**
        `[p]win 2050 Home` - Declare *Home* team winner of game 2050
        `[p]win 2050 Nelluk` - Declare *Nelluk* winner of game 2050
        """
        if settings.recalculation_mode:
            logger.info('Skipping command due to settings.recalculation_mode')
            return await ctx.send(f':warning: {ctx.author.mention} - I am currently recalculating the results of prior games. No new game results can be logged. Please try again in a few minutes.')

        usage = ('Include both game ID and the name of the winning side. Example usage:\n'
                f'`{ctx.prefix}win 422 Nelluk`\n`{ctx.prefix}win 425 Home` *For a team game*\n')
        if ctx.invoked_with.lower() == 'lose':
            return await ctx.send(f'Games are always concluded using the `{ctx.prefix}win` command.\n{usage}')
        if not winning_game:
            return await ctx.send(f'{usage}\nYou can use the command `{ctx.prefix}incomplete` to view your unfinished games.')
        if winning_game.is_pending:
            return await ctx.send(f'Game {winning_game.id} is still a pending open game. It must be started using the `{ctx.prefix}start` command before it can be concluded.')
        if not winning_side_name:
            game_side_str = '\n'.join(winning_game.list_gameside_membership())
            return await ctx.send(f'{usage}\n__Sides in this game are:__\n{game_side_str}')

        try:
            winning_obj, winning_side = winning_game.gameside_by_name(name=winning_side_name)
            # winning_obj will be a Team or a Player depending on squad size
            # winning_side will be their GameSide
        except exceptions.MyBaseException as ex:
            return await ctx.send(f'{ex}')

        reset_confirmations_flag = False
        if winning_game.is_completed is True:
            if winning_game.is_confirmed is True:
                return await ctx.send(f'Game with ID {winning_game.id} is already marked as completed with winner **{winning_game.winner.name()}**')
            elif winning_game.winner != winning_side:
                (confirmed_count, side_count, _) = winning_game.confirmations_count()
                await ctx.send(f':warning: Unconfirmed game with ID {winning_game.id} had previously been marked with winner **{winning_game.winner.name()}**.\n'
                    f'{confirmed_count} of {side_count} sides had confirmed.')
                reset_confirmations_flag = True

        if winning_game.is_pending:
            return await ctx.send('This game has not started yet.')

        utilities.lock_game(winning_game.id)

        models.GameLog.write(game_id=winning_game, guild_id=ctx.guild.id, message=f'Win confirm logged by {models.GameLog.member_string(ctx.author)} for winner **{discord.utils.escape_markdown(winning_obj.name)}**')
        await winning_game.update_squad_channels(guild_list=settings.bot.guilds, guild_id=ctx.guild.id, message=f'A win claim has been placed by **{ctx.author.display_name}** for winner **{winning_obj.name}**')

        has_player, author_side = winning_game.has_player(discord_id=ctx.author.id)
        if settings.is_staff(ctx.author) and not has_player:
            confirm_win = True
        else:
            if not has_player:
                utilities.unlock_game(winning_game.id)
                return await ctx.send('You were not a participant in this game.')

            if reset_confirmations_flag:
                winning_game.confirmations_reset()

            new_confirmation = not author_side.win_confirmed  # To track if author had previously confirmed or not
            winning_side.win_confirmed = True
            author_side.win_confirmed = True
            winning_side.save()
            author_side.save()

            (confirmed_count, side_count, fully_confirmed) = winning_game.confirmations_count()

            if fully_confirmed:
                await ctx.send('All sides have confirmed this victory. Good game!')
                confirm_win = True
            else:
                confirm_win = False
                printed_side_name = winning_side.name() if '@' in winning_side_name else winning_side_name

                if winning_game.win_claimed_ts:
                    # this win had previously been claimed, dont ping lineup
                    conf_str = 'Your confirmation has been logged. ' if new_confirmation else ''
                    await ctx.send(f'{conf_str}**Game {winning_game.id}** *{winning_game.name}* is pending confirmation: {confirmed_count} of {side_count} sides have confirmed.\n'
                        f'Participants in the game should use the command __`{ctx.prefix}win {winning_game.id} {printed_side_name}`__ to confirm the victory.\n'
                        f'Please post a screenshot of your victory in case there is a dispute. If this win was claimed in error please use the `{ctx.prefix}staffhelp` command., '
                        f'or you can cancel your claim with the command `{ctx.prefix}unwin {winning_game.id}`')
                else:
                    winning_game.win_claimed_ts = datetime.datetime.now()
                    winning_game.save()
                    # first time this win has been claimed - ping lineup instructions
                    await ctx.send(f'**Game {winning_game.id}** *{winning_game.name}* concluded pending confirmation of winner **{winning_obj.name}**\n'
                        f'To confirm, have opponents use the command __`{ctx.prefix}win {winning_game.id} {printed_side_name}`__\n'
                        'If opponents do not dispute the win then the game will be confirmed automatically after a period of time.\n'
                        f'If this win was claimed falsely please use the `{ctx.prefix}staffhelp` command to contest, or you can cancel your claim with the command `{ctx.prefix}unwin {winning_game.id}`.\n'
                        f'*Game lineup*: {" ".join(winning_game.mentions())}')

        try:
            winning_game.declare_winner(winning_side=winning_side, confirm=confirm_win)
        except exceptions.CheckFailedError as e:
            utilities.unlock_game(winning_game.id)
            await ctx.send(f'*Error*: {e}')
        else:
            utilities.unlock_game(winning_game.id)
            if confirm_win:
                logger.debug(f'in $win {winning_game.id} cleanup with confirm_win')
                # Cleanup game channels and announce winners
                # try/except block is attempt at a bandaid where sometimes an InterfaceError/Cursor Closed exception would hit here, probably due to issues with async code

                try:
                    await post_win_messaging(ctx.guild, ctx.prefix, ctx.channel, winning_game)
                except peewee.PeeweeException as e:
                    logger.error(f'Error during win command triggering post_win_messaging - trying to reopen and run again: {e}')
                    db.connect(reuse_if_open=True)
                    await post_win_messaging(ctx.guild, ctx.prefix, ctx.channel, winning_game)
            else:
                logger.debug(f'no confirm_win cleanup for game {winning_game.id}')

    @settings.in_bot_channel()
    @models.is_registered_member()
    @commands.command(usage='game_id')
    async def unwin(self, ctx, game: PolyGame = None):
        """Reset a completed game to incomplete

        **Staff usage**:
        Reverts ELO changes from the completed game and any subsequent completed game.
        Resets the game as if it were still incomplete with no declared winner.

        **Player usage**:
        If you use the `[p]win` command on the wrong game or for the wrong winner, use this command to undo your mistake.

         **Examples**
        `[p]unwin 12500`
        """

        if game is None:
            return await ctx.send('No matching game was found.')

        if game.is_pending:
            return await ctx.send(f'Game {game.id} is marked as *pending / not started*. This command cannot be used.')
        if not game.is_completed:
            return await ctx.send(f'Game {game.id} is marked as *Incomplete*. This command cannot be used.')

        if settings.recalculation_mode:
            logger.info('Skipping command due to settings.recalculation_mode')
            return await ctx.send(f':warning: {ctx.author.mention} - I am currently recalculating the results of prior games. No new game results can be logged. Please try again in a few minutes.')

        if settings.is_staff(ctx.author):
            # Staff usage: reset any game to Incomplete state
            game.confirmations_reset()
            utilities.lock_game(game.id)
            models.GameLog.write(game_id=game, guild_id=ctx.guild.id, message=f'{models.GameLog.member_string(ctx.author)} staffer used unwin command.')
            if game.is_completed and game.is_confirmed:
                elo_logger.debug(f'unwin game {game.id}')
                async with ctx.typing():
                    with db.atomic():
                        timestamp = game.completed_ts
                        game.reverse_elo_changes()
                        game.completed_ts = None
                        game.is_confirmed = False
                        game.is_completed = False
                        game.winner = None
                        game.save()

                        await post_unwin_messaging(ctx.guild, ctx.prefix, ctx.channel, game, previously_confirmed=True)
                        if game.is_ranked:
                            settings.recalculation_mode = True
                            Game.recalculate_elo_since(timestamp=timestamp)
                            elo_logger.debug(f'unwin game {game.id} completed')
                            settings.recalculation_mode = False
                            utilities.unlock_game(game.id)
                            return await ctx.send(f'Game {game.id} has been marked as *Incomplete*. ELO changes have been reverted and ELO from all subsequent games recalculated.')

                        else:
                            elo_logger.debug(f'unwin game {game.id} completed (unranked)')
                            utilities.unlock_game(game.id)
                            return await ctx.send(f'Unranked game {game.id} has been marked as *Incomplete*.')

            elif game.is_completed:
                # Unconfirmed win
                game.completed_ts = None
                game.is_completed = False
                game.winner = None
                game.save()
                await post_unwin_messaging(ctx.guild, ctx.prefix, ctx.channel, game, previously_confirmed=False)
                utilities.unlock_game(game.id)
                return await ctx.send(f'Unconfirmed Game {game.id} has been marked as *Incomplete*.')

            else:
                return await ctx.send(f'Game {game.id} does not have a confirmed winner.')
        else:
            # non-staff usage: remove your own claim on a game's win
            has_player, author_side = game.has_player(discord_id=ctx.author.id)
            if not has_player:
                return await ctx.send(f'You are not a player in game {game.id} and do not have server staff permissions.')
            if game.is_confirmed:
                return await ctx.send(f'Game {game.id} has been confirmed already. Only server staff can use this command on confirmed games.')
            if not author_side.win_confirmed:
                return await ctx.send(f'Your side **{author_side.name()}** has no record of confirming a win from game {game.id} - this command cannot be used.')
            if game.is_pending:
                return await ctx.send(f'Game {game.id} is marked as *pending / not started*. This command cannot be used.')

            utilities.lock_game(game.id)
            if author_side == game.winner:
                logger.debug(f'Player {ctx.author.name} is removing their own win claim on game {game.id}')
                models.GameLog.write(game_id=game, guild_id=ctx.guild.id, message=f'{models.GameLog.member_string(ctx.author)} removes their self-win claim and confirmations have reset.')
                game.confirmations_reset()
                game.completed_ts = None
                game.is_completed = False
                game.winner = None
                game.save()
                await post_unwin_messaging(ctx.guild, ctx.prefix, ctx.channel, game, previously_confirmed=False)
                utilities.unlock_game(game.id)
                return await ctx.send(f'Your unconfirmed win in game {game.id} has been reset and the game is now marked as *Incomplete*.')
            else:
                # author removing win claim for a game pointing at another side as the winner
                logger.debug(f'Player {ctx.author.name} is removing win claim on game {game.id}')
                models.GameLog.write(game_id=game, guild_id=ctx.guild.id, message=f'{models.GameLog.member_string(ctx.author)} removed their confirmation of the game winner.')
                author_side.win_confirmed = False
                author_side.save()

                (confirmed_count, side_count, fully_confirmed) = game.confirmations_count()
                utilities.unlock_game(game.id)
                return await ctx.send(f'Your confirmation that **{game.winner.name()}** won game {game.id} has been *removed*. The win is still pending confirmation. '
                    f'{confirmed_count} of {side_count} sides are marked as confirming.')

    @settings.in_bot_channel()
    @models.is_registered_member()
    @commands.command(usage='game_id', aliases=['delete_game', 'delgame', 'delmatch', 'deletegame'])
    async def delete(self, ctx, game: PolyGame = None):
        """Deletes a game

        You can delete a game if you are the host and is has not started yet.
        Mods can delete completed games which will reverse any ELO changes they caused.
        **Example:**
        `[p]deletegame 25`
        """

        if not game:
            return await ctx.send(f'Game ID not provided. Usage: __`{ctx.prefix}delete GAME_ID`__')
        gid = game.id

        mention_list = game.mentions()
        if game.is_pending:
            is_hosted_by, host = game.is_hosted_by(ctx.author.id)
            if not is_hosted_by and not settings.is_staff(ctx.author):
                host_name = f' **{host.name}**' if host else ''
                return await ctx.send(f'Only the game host{host_name} or server staff can do this.')

            players, capacity = game.capacity()
            if players >= capacity:
                filled_str = 'full'
            else:
                filled_str = 'unfilled'

            await game.update_external_broadcasts(deleted=True)
            models.GameLog.write(game_id=game, guild_id=ctx.guild.id, message=f'{models.GameLog.member_string(ctx.author)} deleted the {filled_str} pending game.')
            await ctx.send(f'Deleting {filled_str} open game {game.id}\nNotifying players: {" ".join(mention_list)}')
            return game.delete_game()

        if not settings.is_mod(ctx.author):
            return await ctx.send('Only server mods can delete completed or in-progress games.')

        utilities.lock_game(gid)
        if game.winner and game.is_confirmed and game.is_ranked:
            await ctx.send(f'Deleting game with ID {game.id} and re-calculating ELO for all subsequent games. This will take a few seconds.')

        if game.announcement_message:
            game.name = f'~~{game.name}~~ GAME DELETED'
            await game.update_announcement(guild=ctx.guild, prefix=ctx.prefix)

        await game.delete_game_channels(self.bot.guilds, ctx.guild.id)
        models.GameLog.write(game_id=game, guild_id=ctx.guild.id, message=f'{models.GameLog.member_string(ctx.author)} deleted the game.')

        try:
            async with ctx.typing():
                await self.bot.loop.run_in_executor(None, game.delete_game)
                # Allows bot to remain responsive while this large operation is running.
                await ctx.send(f'Game with ID {gid} has been deleted and team/player ELO changes have been reverted, if applicable.\nNotifying players: {" ".join(mention_list)}')
        except discord.errors.NotFound:
            logger.warning('Game deleted while in game-related channel')
            await self.bot.loop.run_in_executor(None, game.delete_game)

        utilities.unlock_game(gid)

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

        arg_list = args.split()

        try:
            game = Game.by_channel_or_arg(chan_id=ctx.channel.id, arg=arg_list[0])
        except (ValueError, exceptions.MyBaseException) as e:
            return await ctx.send(f'{e}\n**Example usage:** `{ctx.prefix}{ctx.invoked_with} 1234 dry`\nYou can also omit the game ID if you use the command from a game-specific channel.')

        if str(game.id) == str(arg_list[0]):
            arg_list = arg_list[1:]  # Remove game ID from list if it was used for lookup
            if game.guild_id != ctx.guild.id and not game.uses_channel_id(ctx.channel.id):
                return await ctx.send(f'Game {game.id} is associated with a different discord server. Use this command from that server or a game-specific channel.')

        logger.debug(f'Attempting setmap for game {game.id}')

        if len(arg_list) != 1:
            return await ctx.send(f'Wrong number of arguments. See `{ctx.prefix}help setmaptype` for usage examples.')

        map_type_name = arg_list[0]

        if map_type_name.upper() == 'NONE':
            map_type = ''
        else:
            map_type = utilities.get_map_type(map_type_name)
            if not map_type:
                return await ctx.send(f'No matching map type found for "{discord.utils.escape_mentions(map_type_name)}". Check spelling or try a different name.')

        lineup_match = game.player(discord_id=ctx.author.id)
        if lineup_match and settings.get_user_level(ctx.author) > 2:
            logger.debug(f'Authorized since ctx.author is a player in the game')
        elif settings.get_user_level(ctx.author) > 3:
            logger.debug(f'Authorized since ctx.author is a power user')
        else:
            return await ctx.send(f'You are not authorized to set the map type for this game.')
        
        game.map_type = map_type
        game.save()

        await ctx.send(f'Map type for game {game.id} set to "{map_type}".')
        models.GameLog.write(game_id=game.id, guild_id=game.guild_id, message=f'{models.GameLog.member_string(ctx.author)} set map type to "{map_type}"')

        game = game.load_full_game()
        await game.update_announcement(guild=ctx.guild, prefix=ctx.prefix)
    

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

    async def game_search(self, ctx, mode: str, arg_list):

        target_list = [arg.replace('"', '') for arg in arg_list]  # should enable it to handle "multi word" args
        target_list = [i for i in target_list if len(i) > 2]  # strip 1-2 character arguments that match too easily to random players
        player_discord_id = None  # Filled by author.id if command is just bare $incomplete - list will include channel links

        if mode.upper() == 'ALLGAMES':
            status_filter, status_str = 0, 'game'
        elif mode.upper() == 'COMPLETE':
            status_filter, status_str = 1, 'completed game'
        elif mode.upper() == 'INCOMPLETE':
            status_filter, status_str = 2, 'incomplete game'
        elif mode.upper() == 'WINS':
            status_filter, status_str = 3, 'winning game'
        elif mode.upper() == 'LOSSES':
            status_filter, status_str = 4, 'losing game'
        else:
            logger.error(f'Invalid mode passed to game_search: {mode}. Using default of allgames/0')
            status_filter, status_str = 0, 'game'

        if len(target_list) == 1 and target_list[0].upper() == 'ALL':
            results_str = f'All {status_str}s'

            def async_game_search():
                utilities.connect()
                query = Game.search(status_filter=status_filter, guild_id=ctx.guild.id)
                if status_filter == 2:
                    query = list(query)  # reversing 'Incomplete' queries so oldest is at top
                    query.reverse()
                logger.debug(f'Searching games, status filter: {status_filter}')
                logger.debug(f'Returned {len(query)} results')
                list_name = f'All {status_str}s ({len(query)})'
                game_list = utilities.summarize_game_list(query[:500])
                return game_list, list_name

            game_list, list_name = await self.bot.loop.run_in_executor(None, async_game_search)
        else:
            if not target_list:
                # Target is person issuing command
                target_list.append(str(ctx.author.id))

            team_size_str, team_sizes = '', []
            for arg in target_list:
                m = re.fullmatch(r"\d+(?:(v|vs)\d+)+", arg.lower())
                if m:
                    # arg looks like '3v3' or '1v1v1'
                    team_size_str = m[0]
                    team_sizes = [int(x) for x in arg.lower().split(m[1])]
                    target_list.remove(arg)
                    continue

            results_title = []

            player_matches, team_matches, remaining_args = parse_players_and_teams(target_list, ctx.guild.id)
            p_names, t_names = [p.name for p in player_matches], [t.name for t in team_matches]

            if mode.upper() == 'INCOMPLETE' and len(player_matches) == 1:
                # show gameside channel links for one-player target of #incomplete command
                player_discord_id = player_matches[0].discord_member.discord_id

            if p_names:
                results_title.append(f'Including players: *{"* & *".join(p_names)}*')
            if t_names:
                results_title.append(f'Including teams: *{"* & *".join(t_names)}*')
            if remaining_args:
                remaining_args = [utilities.escape_role_mentions(x) for x in remaining_args]
                results_title.append(f'Included in name/notes: *{"* *".join(remaining_args)}*')
            if team_size_str:
                results_title.append(f'Game size: *{team_size_str}*')

            results_str = '\n'.join(results_title)
            if not results_title:
                results_str = 'No filters applied'

            def async_game_search():
                utilities.connect()
                query = Game.search(status_filter=status_filter, player_filter=player_matches, team_filter=team_matches, title_filter=remaining_args, guild_id=ctx.guild.id, size_filter=team_sizes)
                logger.debug(f'Searching games, status filter: {status_filter}, player_filter: {player_matches}, team_filter: {team_matches}, title_filter: {remaining_args}')
                logger.debug(f'Returned {len(query)} results')
                game_list = utilities.summarize_game_list(query[:500], player_discord_id=player_discord_id)
                list_name = f'{len(query)} {status_str}{"s" if len(query) != 1 else ""}\n{results_str}'
                return game_list, list_name

            game_list, list_name = await self.bot.loop.run_in_executor(None, async_game_search)

        if len(game_list) == 0:
            return await ctx.send(f'No results. See `{ctx.prefix}help {ctx.invoked_with}` for usage examples. Searched for:\n{results_str}')
        await utilities.paginate(self.bot, ctx, title=list_name, message_list=game_list, page_start=0, page_end=15, page_size=15)

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
            await channel.send(embed=embed)
            return await current_chan.send(f'Game concluded! See {channel.mention} for full details.')

    await current_chan.send(f'Game concluded! Congrats **{winning_game.winner.name()}**. Roster: {" ".join(winning_game.mentions())}{reminder_message}')
    await current_chan.send(embed=embed, content=content)


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
            announcement = await channel.send(embed=embed, content=content)
            await ctx.send(f'New {ranked_str}game ID **{game.id}** started! See {channel.mention} for full details.')
            game.announcement_message = announcement.id
            game.announcement_channel = announcement.channel.id
            game.save()
        else:
            await ctx.send(embed=embed, content=content)
            await ctx.send('Error loading game announcement channel from server settings. Please inform the bot owner.')
            logger.error(f'Could not load game_announce_channel channel for guild {ctx.guild.id}')

    else:
        await ctx.send(f'{announce_str}')
        await ctx.send(embed=embed, content=content)

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
