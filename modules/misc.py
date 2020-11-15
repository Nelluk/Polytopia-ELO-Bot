import discord
from discord.ext import commands
import modules.models as models
import modules.utilities as utilities
import settings
import logging
import asyncio
import modules.exceptions as exceptions
import re
import datetime
import random
from modules.games import PolyGame
# import modules.imgen as imgen
# import modules.achievements as achievements

logger = logging.getLogger('polybot.' + __name__)


class misc(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        if settings.run_tasks:
            self.bg_task = bot.loop.create_task(self.task_broadcast_newbie_message())
            self.bg_task3 = bot.loop.create_task(self.task_broadcast_newbie_steam_message())

    @commands.command(hidden=True, aliases=['ts', 'blah'])
    @commands.is_owner()
    async def test(self, ctx, *, args: str = None):

        return await ctx.send('No op')

        cosmos = models.Team.get_or_except(team_name='The Cosmonauts', guild_id=ctx.guild.id, require_exact=True)

        games_6 = models.Game.search(team_filter=[cosmos], title_filter=['PS6'], status_filter=3)
        games_7 = models.Game.search(team_filter=[cosmos], title_filter=['PS7'], status_filter=3)
        output = []

        for g in games_6 + games_7:
            if g.is_ranked:
                output.append(f'COSMOSTS - Modifying game {g.id} - {g.name} - {g.notes} - {g.is_ranked}')
                g.is_ranked = False
                g.notes = g.notes + ' - Set to unranked Nov 8 2020 for team cheating scandal'
                g.save()
                models.GameLog.write(game_id=g, guild_id=g.guild_id, message=f'Previously a ranked Season 6 or Season 7 win - changed to unranked as part of Cosmonauts team cheating scandal punishment.')
            else:
                output.append(f'COSMOSTS - Skipping game {g.id} - Unranked (Already modified?)')

            logger.debug(output[-1:])

        await utilities.buffered_send(destination=ctx, content="\n".join(output))

        output = []

        games_8_wins = models.Game.search(team_filter=[cosmos], title_filter=['PS8'], status_filter=3)
        games_8 = models.Game.search(team_filter=[cosmos], title_filter=['PS8'])

        for g in games_8:
            output.append(f'COSMOSTS - Found S8 game {g.id} - {g.name} - {g.notes} - {g.is_ranked}')
            g.name = g.name.replace('Ps8W', 'Cancelled Season 8 Week ')
            if g in games_8_wins:
                output.append(f'COSMOSTS - Winning game to unrank and de-tag')
                g.is_ranked = False
                g.notes = g.notes + ' - Previously a game from Season 8 - Set to unranked win Nov 8 2020 for team cheating scandal'
                models.GameLog.write(game_id=g, guild_id=g.guild_id, message=f'Game renamed to *{g.name}* to remove it from Season 8 as part of Cosmonauts team cheating scandal punishment. Set to unranked as it was a win.')
            else:
                output.append(f'COSMOSTS - Losing or incomplete game to de-tag')
                g.notes = g.notes + ' - Previously a game from Season 8 - Set to unranked win Nov 8 2020 for team cheating scandal'
                models.GameLog.write(game_id=g, guild_id=g.guild_id, message=f'Game renamed to *{g.name}* to remove it from Season 8 as part of Cosmonauts team cheating scandal punishment.')

            logger.debug(output[-2:])

            g.save()

            await g.update_squad_channels(self.bot.guilds, ctx.guild.id)

        await utilities.buffered_send(destination=ctx, content="\n".join(output))

    @commands.command(hidden=True)
    @commands.is_owner()
    async def reset_ts_from(self, ctx, *, arg: str = None):

        import functools
        game = models.Game.get_or_none(id=arg)
        if not game:
            return print('no game')

        print(f'Loaded game {game.id}')
        await ctx.send(f'This may take a while...')
        settings.recalculation_mode = True
        async with ctx.typing():
            utilities.connect()
            await self.bot.loop.run_in_executor(None, functools.partial(models.Game.recalculate_elo_since, timestamp=game.completed_ts))
            # Allows bot to remain responsive while this large operation is running.
            await ctx.send(f'DB has been refreshed from {game.completed_ts} onward')
            settings.recalculation_mode = False

    @commands.command(usage=None)
    @settings.in_bot_channel_strict()
    async def guide(self, ctx):
        """
        Show an overview of what the bot is for

        Type `[p]guide` for an overview of what this bot is for and how to use it.
        """
        bot_desc = ('This bot is designed to improve Polytopia multiplayer by filling in gaps in two areas: competitive leaderboards, and matchmaking.\n'
                    # 'Its primary home is [PolyChampions](https://discord.gg/YcvBheSv), a server focused on team play organized into a league.\n'
                    f'To register as a player with the bot use __`{ctx.prefix}setname Mobile User Name`__ or  __`{ctx.prefix}steamname Steam User Name`__')

        embed = discord.Embed(title=f'PolyELO Bot Donation Link', url='https://cash.me/$Nelluk/3', description=bot_desc)

        embed.add_field(name='Matchmaking',
            value=f'This helps players organize and find games.\nFor example, use __`{ctx.prefix}opengame 1v1`__ to create an open 1v1 game that others can join.\n'
                f'To see a list of open games you can join use __`{ctx.prefix}opengames`__. Once the game is full the host would use __`{ctx.prefix}startgame`__ to close it and track it for the leaderboards.\n'
                f'See __`{ctx.prefix}help matchmaking`__ for all commands.', inline=False)

        embed.add_field(name='ELO Leaderboards',
            value='Win your games and climb the leaderboards! Earn sweet ELO points!\n'
                'ELO points are gained or lost based on your game results. You will gain more points if you defeat an opponent with a higher ELO.\n'
                f'Use __`{ctx.prefix}lb`__ to view the individual leaderboards. There is also a __`{ctx.prefix}lbsquad`__ squad leaderboard. Form a squad by playing with the same person in multiple games!'
                f'\nSee __`{ctx.prefix}help`__ for all commands.', inline=False)

        embed.add_field(name='Finishing tracked games',
            value=f'Use the __`{ctx.prefix}win`__ command to tell the bot that a game has concluded.\n'
            f'For example if Nelluk wins game 10150, he would type __`{ctx.prefix}win 10150 nelluk`__. The losing player can confirm using the same command. '
            f'Games are auto-confirmed after 24 hours, or sooner if the losing side manually confirms.', inline=False)

        embed.set_thumbnail(url=self.bot.user.avatar_url_as(size=512))
        embed.set_footer(text='Developer: Nelluk')
        await ctx.send(embed=embed)

    @commands.command(usage=None)
    @settings.in_bot_channel_strict()
    async def credits(self, ctx):
        """
        Display development credits
        """
        embed = discord.Embed(title=f'Support this project', url='https://www.buymeacoffee.com/nelluk')

        embed.add_field(name='Developer', value='Nelluk')
        embed.add_field(name='Source code', value='https://github.com/Nelluk/Polytopia-ELO-Bot')

        embed.add_field(name='Contributions', value='rickdaheals, koric, Gerenuk, alphaSeahorse, Octo, Artemis, theoldlove', inline=False)

        embed.set_thumbnail(url=self.bot.user.avatar_url_as(size=512))
        await ctx.send(embed=embed)

    @commands.command()
    @settings.in_bot_channel_strict()
    async def stats(self, ctx):
        """ Display statistics on games logged with this bot """

        embed = discord.Embed(title='PolyELO Statistics')
        last_month = (datetime.datetime.now() + datetime.timedelta(days=-30))
        last_quarter = (datetime.datetime.now() + datetime.timedelta(days=-90))
        last_week = (datetime.datetime.now() + datetime.timedelta(days=-7))

        games_played = models.Game.select().where(models.Game.is_completed == 1)
        games_played_90d = models.Game.select().where((models.Game.is_pending == 0) & (models.Game.date > last_quarter))
        games_played_30d = models.Game.select().where((models.Game.is_pending == 0) & (models.Game.date > last_month))
        games_played_7d = models.Game.select().where((models.Game.is_pending == 0) & (models.Game.date > last_week))

        incomplete_games = models.Game.select().where((models.Game.is_pending == 0) & (models.Game.is_completed == 0))

        participants_90d = models.Lineup.select(models.Lineup.player.discord_member).join(models.Game).join_from(models.Lineup, models.Player).join(models.DiscordMember).where(
            (models.Lineup.game.date > last_quarter)
        ).group_by(models.Lineup.player.discord_member).distinct()

        participants_30d = models.Lineup.select(models.Lineup.player.discord_member).join(models.Game).join_from(models.Lineup, models.Player).join(models.DiscordMember).where(
            (models.Lineup.game.date > last_month)
        ).group_by(models.Lineup.player.discord_member).distinct()

        participants_7d = models.Lineup.select(models.Lineup.player.discord_member).join(models.Game).join_from(models.Lineup, models.Player).join(models.DiscordMember).where(
            (models.Lineup.game.date > last_week)
        ).group_by(models.Lineup.player.discord_member).distinct()

        embed.add_field(value='\u200b', name=f'`{"----------------------------------":<35}` Global (Local)', inline=False)

        stats_0 = (f'`{"Total games completed:":<35}\u200b` {games_played.count()} ({games_played.where(models.Game.guild_id == ctx.guild.id).count()})\n'
                   f'`{"Incomplete games:":<35}\u200b` {incomplete_games.count()} ({incomplete_games.where(models.Game.guild_id == ctx.guild.id).count()})\n')
        embed.add_field(value='\u200b', name=stats_0[:256], inline=False)
        stats_1 = (f'`{"Games created in last 90 days:":<35}\u200b`\u200b {games_played_90d.count()} ({games_played_90d.where(models.Game.guild_id == ctx.guild.id).count()})\n'
                      f'`{"Games created in last 30 days:":<35}\u200b`\u200b {games_played_30d.count()} ({games_played_30d.where(models.Game.guild_id == ctx.guild.id).count()})\n'
                      f'`{"Games created in last 7 days:":<35}\u200b`\u200b {games_played_7d.count()} ({games_played_7d.where(models.Game.guild_id == ctx.guild.id).count()})\n'
                   )
        embed.add_field(value='\u200b', name=stats_1[:256], inline=False)

        stats_2 = (f'`{"Participants in last 90 days:":<35}\u200b` {participants_90d.count()} ({participants_90d.where(models.Game.guild_id == ctx.guild.id).count()})\n'
                   f'`{"Participants in last 30 days:":<35}\u200b` {participants_30d.count()} ({participants_30d.where(models.Game.guild_id == ctx.guild.id).count()})\n'
                   f'`{"Participants in last 7 days:":<35}\u200b` {participants_7d.count()} ({participants_7d.where(models.Game.guild_id == ctx.guild.id).count()})\n')
        embed.add_field(value='\u200b', name=stats_2[:256], inline=False)
        await ctx.send(embed=embed)

    @commands.command(hidden=True, usage='message', aliases=['pingmobile', 'pingsteam'])
    @commands.cooldown(1, 30, commands.BucketType.user)
    @settings.in_bot_channel_strict()
    @models.is_registered_member()
    async def pingall(self, ctx, *, message: str = None):
        """ Ping everyone in all of your incomplete games

        Not useable by all players.
        You can use `pingsteam` to only ping players in your Steam platform games,
        or `pingmobile` to only ping players in your Mobile games.

         **Examples**
        `[p]pingall My phone died and I will make all turns tomorrow`
        Send a message to everyone in all of your incomplete games
        `[p]pingall @Glouc3stershire Glouc is in Tahiti and will play again tomorrow`
        *Staff:* Send a message to everyone in another player's games
        `[p]pingmobile My phone died but I'm still taking turns on Steam!`

        """
        if not message:
            return await ctx.send(f'Message is required.')

        m = utilities.string_to_user_id(message.split()[0])

        if m:
            logger.debug(f'Third party use of {ctx.invoked_with}')
            # Staff member using command on third party
            if settings.get_user_level(ctx.author) <= 3:
                logger.debug('insufficient user level')
                return await ctx.send(f'You do not have permission to use this command on another player\'s games.')
            message = ' '.join(message.split()[1:])  # remove @Mention first word of message
            target = str(m)
            log_message = f'{models.GameLog.member_string(ctx.author)} used pingall on behalf of player ID `{target}` with message: '
        else:
            logger.debug('first party usage of pingall')
            # Play using command on their own games
            if settings.get_user_level(ctx.author) <= 2:
                logger.debug('insufficient user level')
                return await ctx.send(f'You do not have permission to use this command. You can ask a server staff member to use this command on your games for you.')
            target = str(ctx.author.id)
            log_message = f'{models.GameLog.member_string(ctx.author)} used {ctx.invoked_with} with message: '

        try:
            player_match = models.Player.get_or_except(player_string=target, guild_id=ctx.guild.id)
        except exceptions.NoSingleMatch:
            return await ctx.send(f'User <@{target}> is not a registered ELO player.')

        if ctx.invoked_with == 'pingall':
            platform_filter = 2
            title_str = 'Message to all players in unfinished games'
        elif ctx.invoked_with == 'pingmobile':
            platform_filter = 1
            title_str = 'Message to players in unfinished mobile games'
        elif ctx.invoked_with == 'pingsteam':
            platform_filter = 0
            title_str = 'Message to players in unfinished Steam games'
        else:
            raise ValueError(f'{ctx.invoked_with} is not a handled alias for this command')

        game_list = models.Game.search(player_filter=[player_match], status_filter=2, guild_id=ctx.guild.id, platform_filter=platform_filter)
        logger.debug(f'{len(game_list)} incomplete games for target')

        list_of_players = []
        for g in game_list:
            list_of_players += g.mentions()

        list_of_players = list(set(list_of_players))
        logger.debug(f'{len(list_of_players)} unique opponents for target')
        clean_message = utilities.escape_role_mentions(message)
        if len(list_of_players) > 100:
            await ctx.send(f'*Warning:* More than 100 unique players are addressed. Only the first 100 will be mentioned.')
        await ctx.send(f'{title_str} for <@{target}> ({player_match.name}): *{clean_message}*')

        recipient_message = f'Message recipients: {" ".join(list_of_players[:100])}'
        await ctx.send(recipient_message[:2000])

        for game in game_list:
            logger.debug(f'Sending message to game channels for game {game.id} from {ctx.invoked_with}')
            models.GameLog.write(game_id=game, guild_id=ctx.guild.id, message=f'{log_message} *{discord.utils.escape_markdown(clean_message)}*')
            await game.update_squad_channels(self.bot.guilds, game.guild_id, message=f'{title_str} for <@{target}> ({player_match.name}): *{clean_message}*')

    @commands.command(usage='game_id message')
    @models.is_registered_member()
    @commands.cooldown(1, 20, commands.BucketType.user)
    async def ping(self, ctx, *, args=''):
        """ Ping everyone in one of your games with a message

         **Examples**
        `[p]ping 100 I won't be able to take my turn today` - Send a message to everyone in game 100
        `[p]ping This game is amazing!` - You can omit the game ID if you send the command from a game-specific channel

        See `[p]help pingall` for a command to ping ALL incomplete games simultaneously.

        """

        usage = (f'**Example usage:** `{ctx.prefix}ping 100 Here\'s a nice note for everyone in game 100.`\n'
                    'You can also omit the game ID if you use the command from a game-specific channel.')

        if ctx.message.attachments:
            attachment_urls = '\n'.join([attachment.url for attachment in ctx.message.attachments])
            args += f'\n{attachment_urls}'

        if not args:
            ctx.command.reset_cooldown(ctx)
            return await ctx.send(usage)

        if settings.is_mod(ctx.author):
            ctx.command.reset_cooldown(ctx)

        args = args.split()
        try:
            game_id = int(args[0])
            message = ' '.join(args[1:])
        except ValueError:
            game_id = None
            message = ' '.join(args)

        # TODO:  should prioritize inferred game above an integer. currently something like '$ping 1 city island plz restart'
        # will try to ping game ID #1 even if done within a game channel

        inferred_game = None
        if not game_id:
            try:
                inferred_game = models.Game.by_channel_id(chan_id=ctx.message.channel.id)
            except exceptions.TooManyMatches:
                logger.error(f'More than one game with matching channel {ctx.message.channel.id}')
                return await ctx.send('Error looking up game based on current channel - please contact the bot owner.')
            except exceptions.NoMatches:
                ctx.command.reset_cooldown(ctx)
                logger.debug('Could not infer game from current channel.')
                return await ctx.send(f'Game ID was not included. {usage}')
            logger.debug(f'Inferring game {inferred_game.id} from ping command used in channel {ctx.message.channel.id}')

        if not message:
            ctx.command.reset_cooldown(ctx)
            return await ctx.send(f'Message was not included. {usage}')

        message = utilities.escape_role_mentions(message)

        if inferred_game:
            game = inferred_game
        else:
            game = await PolyGame().convert(ctx, int(game_id), allow_cross_guild=True)

        if not game.player(discord_id=ctx.author.id) and not settings.is_staff(ctx.author):
            ctx.command.reset_cooldown(ctx)
            return await ctx.send(f'You are not a player in game {game.id}')

        permitted_channels = settings.guild_setting(game.guild_id, 'bot_channels').copy()
        if game.game_chan:
            permitted_channels.append(game.game_chan)

        game_player_ids = [l.player.discord_member.discord_id for l in game.lineup]
        game_members = [ctx.guild.get_member(p_id) for p_id in game_player_ids]
        player_mentions = [f'<@{p_id}>' for p_id in game_player_ids]

        game_channels = [gs.team_chan for gs in game.gamesides]
        game_channels = [chan for chan in game_channels if chan]  # remove Nones

        mention_players_in_current_channel = True  # False when done from game channel, True otherwise

        if None not in game_members and all(ctx.channel.permissions_for(member).read_messages for member in game_members):
            logger.debug(f'Allowing ping since all members have read access to current channel')
            mention_players_in_current_channel = True
            print(1)
            # this case may behave unexpectedly if the channel is public and in central server, but a member has left the server
            # the second case should cover that
        elif ctx.channel.id in permitted_channels:
            logger.debug(f'Allowing ping since it is a bot channel or central game channel')
            mention_players_in_current_channel = True
            print('1.5')
        elif ctx.channel.id in game_channels and len(game_channels) >= len(game.gamesides):
            logger.debug(f'Allowing ping since it is within a game channel, and all sides have a game channel')
            mention_players_in_current_channel = False
            print(2)
        elif settings.is_mod(ctx.author) and len(game_channels) >= len(game.gamesides):
            logger.debug(f'Allowing ping since it is from a mod and all sides have a game channel')
            mention_players_in_current_channel = False
            print(3)
        else:
            logger.debug(f'Not allowing ping in {ctx.channel.id}')
            if len(game_channels) >= len(game.gamesides):
                permitted_channels = game_channels + permitted_channels

            channel_tags = [f'<#{chan_id}>' for chan_id in permitted_channels]
            ctx.command.reset_cooldown(ctx)

            if len(game_channels) < len(game.gamesides):
                error_str = 'Not all sides have access to a private channel. '
            else:
                error_str = ''

            return await ctx.send(f'This command can not be used in this channel. {error_str}Permitted channels: {" ".join(channel_tags)}')

        full_message = f'Message from {ctx.author.mention} (**{ctx.author.name}**) regarding game {game.id} **{game.name}**:\n*{message}*'
        models.GameLog.write(game_id=game, guild_id=game.guild_id, message=f'{models.GameLog.member_string(ctx.author)} pinged the game with message: *{discord.utils.escape_markdown(message)}*')

        try:
            if mention_players_in_current_channel:
                logger.debug(f'Ping triggered in non-private channel {ctx.channel.id}')
                await game.update_squad_channels(self.bot.guilds, ctx.guild.id, message=full_message, suppress_errors=True)
                await ctx.send(f'{full_message}\n{" ".join(player_mentions)}')
            else:
                logger.debug(f'Ping triggered in private channel {ctx.channel.id}')
                await game.update_squad_channels(self.bot.guilds, game.guild_id, message=f'{full_message}\n{" ".join(player_mentions)}', suppress_errors=False)
                if ctx.channel.id not in game_channels:
                    await ctx.send(f'Sending ping to game channels:\n{full_message}')
        except exceptions.CheckFailedError as e:
            channel_tags = [f'<#{chan_id}>' for chan_id in permitted_channels]
            return await ctx.send(f'{e}\nTry sending `{ctx.prefix}ping` from a public channel that all members can view: {" ".join(channel_tags)}')

    @commands.command(aliases=['helpstaff'], hidden=False)
    @commands.cooldown(2, 30, commands.BucketType.user)
    # @settings.guild_has_setting(setting_name='staff_help_channel')
    async def staffhelp(self, ctx, *, message: str = ''):
        """
        Get staff help on bot usage or game disputes

        The message will be relayed to a staff channel and someone should assist you shortly.
        You can attach screenshots or links to the message.

        **Example:**
        `[p]staffhelp Game 42500 was claimed incorrectly`
        `[p]staffhelp Game 42500 Does this screenshot show a restartable spawn?`
        """

        potential_game_id = re.search(r'\d{4,6}', message)
        game_id_search = potential_game_id[0] if potential_game_id else None
        try:
            related_game = models.Game.by_channel_or_arg(chan_id=ctx.channel.id, arg=game_id_search)
            guild_id = related_game.guild_id
            # Send game embed summary if message includes a numeric ID of a game or command is used in a game channel
        except (ValueError, exceptions.MyBaseException):
            related_game = None
            guild_id = ctx.guild.id

        guild = self.bot.get_guild(guild_id)
        if guild:
            channel = guild.get_channel(settings.guild_setting(guild_id, 'staff_help_channel'))
        else:
            channel = None

        if not channel:
            ctx.command.reset_cooldown(ctx)
            return await ctx.send(f'Cannot load staff channel. You will need to ping a staff member.')

        if ctx.message.attachments:
            attachment_urls = '\n'.join([attachment.url for attachment in ctx.message.attachments])
            message += f'\n{attachment_urls}'

        if not message or len(message) < 7:
            ctx.command.reset_cooldown(ctx)
            return await ctx.send(f'You must supply a help request, ie: `{ctx.prefix}{ctx.invoked_with} Game 42500 Does this screenshot show a restartable spawn?`')

        helper_role_str = 'server staff'
        helper_role_name = settings.guild_setting(guild_id, 'helper_roles')[0]
        helper_role = discord.utils.get(guild.roles, name=helper_role_name)
        helper_role_str = f'{helper_role.mention}' if helper_role else 'server staff'

        if ctx.channel.guild.id == guild_id:
            chan_str = f'({ctx.channel.name})'
        else:
            chan_str = f'({ctx.channel.name} on __{ctx.guild.name}__)'
        await channel.send(f'Attention {helper_role_str} - {ctx.author.mention} ({ctx.author.name}) asked for help from channel <#{ctx.channel.id}> {chan_str}:\n{ctx.message.jump_url}\n{message}')

        if related_game:
            embed, content = related_game.embed(guild=guild, prefix=ctx.prefix)
            await channel.send(embed=embed, content=content)
            game_id = related_game.id
        else:
            game_id = 0

        models.GameLog.write(game_id=game_id, guild_id=ctx.guild.id, message=f'{models.GameLog.member_string(ctx.author)} requested staffhelp: *{message}*')
        await ctx.send(f'Your message has been sent to server staff. Please wait patiently or send additional information on your issue.')

    @commands.command(hidden=True, aliases=['random_tribes', 'rtribe'], usage='game_size [-banned_tribe ...]')
    @settings.in_bot_channel()
    async def rtribes(self, ctx, size='1v1', *args):
        """Show a random tribe combination for a given game size.
        This tries to keep the sides roughly equal in power.
        **Example:**
        `[p]rtribes 2v2` - Shows Ai-mo/Imperius & Xin-xi/Luxidoor
        `[p]rtribes 2v2 -hoodrick -aquarion` - Remove Hoodrick and Aquarion from the random pool. This could cause problems if lots of tribes are removed.
        """

        m = re.match(r"(\d+)v(\d+)", size.lower())
        if m:
            # arg looks like '3v3'
            if int(m[1]) != int(m[2]):
                return await ctx.send(f'Invalid match format {size}. Sides must be equal.')
            if not 0 < int(m[1]) < 7:
                return await ctx.send(f'Invalid match size {size}. Accepts 1v1 through 6v6')
            team_size = int(m[1])
        else:
            team_size = 1
            args = list(args) + [size]
            # Handle case of no size argument, but with tribe bans

        tribes = [
            ('Bardur', 1),
            ('Kickoo', 1),
            ('Luxidoor', 1),
            ('Imperius', 1),
            ('Elyrion', 2),
            ('Yadakk', 2),
            ('Zebasi', 2),
            ('Hoodrick', 2),
            ('Aquarion', 2),
            ('Oumaji', 3),
            ('Quetzali', 3),
            ('Vengir', 3),
            ('Ai-mo', 3),
            ('Xin-xi', 3)
        ]
        for arg in args:
            # Remove tribes from tribe list. This could cause problems if too many tribes are removed.
            if arg[0] != '-':
                continue
            removal = next(t for t in tribes if t[0].upper() == arg[1:].upper())
            tribes.remove(removal)

        team_home, team_away = [], []

        tribe_groups = {}
        for tribe, group in tribes:
            tribe_groups.setdefault(group, set()).add(tribe)

        available_tribe_groups = list(tribe_groups.values())
        for _ in range(team_size):
            available_tribe_groups = [tg for tg in available_tribe_groups if len(tg) >= 2]

            this_tribe_group = random.choice(available_tribe_groups)

            new_home, new_away = random.sample(this_tribe_group, 2)
            this_tribe_group.remove(new_home)
            this_tribe_group.remove(new_away)

            team_home.append(new_home)
            team_away.append(new_away)

        await ctx.send(f'Home Team: {" / ".join(team_home)}\nAway Team: {" / ".join(team_away)}')

    async def task_broadcast_newbie_message(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            sleep_cycle = (60 * 60 * 3)
            await asyncio.sleep(10)

            for guild in self.bot.guilds:
                broadcast_channels = [guild.get_channel(chan) for chan in settings.guild_setting(guild.id, 'newbie_message_channels')]
                if not broadcast_channels:
                    continue

                prefix = settings.guild_setting(guild.id, 'command_prefix')
                # ranked_chan = settings.guild_setting(guild.id, 'ranked_game_channel')
                # unranked_chan = settings.guild_setting(guild.id, 'unranked_game_channel')
                bot_spam_chan = settings.guild_setting(guild.id, 'bot_channels_strict')[0]
                elo_guide_channel = 533391050014720040

                broadcast_message = (f'To register for ELO leaderboards and matchmaking use the command __`{prefix}setname Your Mobile Name`__')
                broadcast_message += f'\nTo get started with joining an open game, go to <#{bot_spam_chan}> and type __`{prefix}games`__'
                broadcast_message += f'\nFor full information go read <#{elo_guide_channel}>.'

                for broadcast_channel in broadcast_channels:
                    if broadcast_channel:
                        message = await broadcast_channel.send(broadcast_message, delete_after=(sleep_cycle - 5))
                        self.bot.purgable_messages = self.bot.purgable_messages[-20:] + [(guild.id, broadcast_channel.id, message.id)]

            await asyncio.sleep(sleep_cycle)

    async def task_broadcast_newbie_steam_message(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            sleep_cycle = (60 * 60 * 6)
            await asyncio.sleep(10)

            for guild in self.bot.guilds:
                broadcast_channel = guild.get_channel(settings.guild_setting(guild.id, 'steam_game_channel'))
                if not broadcast_channel:
                    continue

                prefix = settings.guild_setting(guild.id, 'command_prefix')
                elo_guide_channel = 533391050014720040

                broadcast_message = (f'To register for ELO leaderboards and matchmaking use the command __`{prefix}steamname Your Steam Name`__')
                broadcast_message += f'\nTo get started with joining an open game, type __`{prefix}games`__ or open your own with __`{prefix}opensteam`__'
                broadcast_message += f'\nFor full information go read <#{elo_guide_channel}>.'

                message = await broadcast_channel.send(broadcast_message, delete_after=(sleep_cycle - 5))
                self.bot.purgable_messages = self.bot.purgable_messages[-20:] + [(guild.id, broadcast_channel.id, message.id)]

            await asyncio.sleep(sleep_cycle)


def setup(bot):
    bot.add_cog(misc(bot))
