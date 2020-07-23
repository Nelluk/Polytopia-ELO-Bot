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
# import modules.achievements as achievements

logger = logging.getLogger('polybot.' + __name__)


class misc(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        if settings.run_tasks:
            self.bg_task = bot.loop.create_task(self.task_broadcast_newbie_message())
            self.bg_task = bot.loop.create_task(self.task_send_polychamps_invite())

    @commands.command(hidden=True, aliases=['ts', 'blah'])
    @commands.is_owner()
    async def test(self, ctx, *, args=None):
        from modules.league import get_league_roles
        team_roles, pro_roles, junior_roles = get_league_roles(ctx.guild)
        print(team_roles, pro_roles, junior_roles)
        league_role = discord.utils.get(ctx.guild.roles, name='League Member')
        pro_role = discord.utils.get(ctx.guild.roles, name='Pro Player')
        junior_role = discord.utils.get(ctx.guild.roles, name='Junior Player')
        for role in pro_roles:
            if role:
                for pro_member in role.members:
                    logger.debug(f'Applying pro/league roles to {pro_member.name}')
                    models.GameLog.write(guild_id=ctx.guild.id, message=f'{models.GameLog.member_string(pro_member)} is on pro team {role.name}')
                    await pro_member.add_roles(league_role, pro_role, reason='Applying league role to existing members')
        for role in junior_roles:
            if role:
                for junior_member in role.members:
                    logger.debug(f'Applying junior/league roles to {junior_member.name}')
                    await junior_member.add_roles(league_role, junior_role, reason='Applying league role to existing members')
                    models.GameLog.write(guild_id=ctx.guild.id, message=f'{models.GameLog.member_string(junior_member)} is on pro team {role.name}')

    @commands.command(usage=None)
    @settings.in_bot_channel_strict()
    async def guide(self, ctx):
        """
        Show an overview of what the bot is for

        Type `[p]guide` for an overview of what this bot is for and how to use it.
        """
        bot_desc = ('This bot is designed to improve Polytopia multiplayer by filling in gaps in two areas: competitive leaderboards, and matchmaking.\n'
                    # 'Its primary home is [PolyChampions](https://discord.gg/cX7Ptnv), a server focused on team play organized into a league.\n'
                    f'To register as a player with the bot use __`{ctx.prefix}setcode YOURPOLYCODEHERE`__')

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
        embed = discord.Embed(title=f'PolyELO Bot Donation Link', url='https://cash.me/$Nelluk/3')

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
        game_stats = (f'`{"Total games completed:":<35}\u200b` {games_played.count()} ({games_played.where(models.Game.guild_id == ctx.guild.id).count()})\n'
                      f'`{"Games created in last 90 days:":<35}\u200b`\u200b {games_played_90d.count()} ({games_played_90d.where(models.Game.guild_id == ctx.guild.id).count()})\n'
                      f'`{"Games created in last 30 days:":<35}\u200b`\u200b {games_played_30d.count()} ({games_played_30d.where(models.Game.guild_id == ctx.guild.id).count()})\n'
                      f'`{"Games created in last 7 days:":<35}\u200b`\u200b {games_played_7d.count()} ({games_played_7d.where(models.Game.guild_id == ctx.guild.id).count()})\n'
                      f'`{"Incomplete games:":<35}\u200b` {incomplete_games.count()} ({incomplete_games.where(models.Game.guild_id == ctx.guild.id).count()})\n'
                      )
        embed.add_field(value='\u200b', name=game_stats, inline=False)

        stats_2 = (f'`{"Participants in last 90 days:":<35}\u200b` {participants_90d.count()} ({participants_90d.where(models.Game.guild_id == ctx.guild.id).count()})\n'
                   f'`{"Participants in last 30 days:":<35}\u200b` {participants_30d.count()} ({participants_30d.where(models.Game.guild_id == ctx.guild.id).count()})\n'
                   f'`{"Participants in last 7 days:":<35}\u200b` {participants_7d.count()} ({participants_7d.where(models.Game.guild_id == ctx.guild.id).count()})\n')
        embed.add_field(value='\u200b', name=stats_2, inline=False)

        await ctx.send(embed=embed)

    @commands.command(hidden=True, usage='message')
    @commands.cooldown(1, 30, commands.BucketType.user)
    @settings.in_bot_channel_strict()
    @models.is_registered_member()
    async def pingall(self, ctx, *, message: str = None):
        """ Ping everyone in all of your incomplete games

        Not useable by all players.

         **Examples**
        `[p]pingall My phone died and I will make all turns tomorrow`
        Send a message to everyone in all of your incomplete games
        `[p]pingall @Glouc3stershire Glouc is in Tahiti and will play again tomorrow`
        *Staff:* Send a message to everyone in another player's games

        """
        if not message:
            return await ctx.send(f'Message is required.')

        m = utilities.string_to_user_id(message.split()[0])

        if m:
            logger.debug('Third party use of pingall')
            # Staff member using command on third party
            if settings.get_user_level(ctx) <= 3:
                logger.debug('insufficient user level')
                return await ctx.send(f'You do not have permission to use this command on another player\'s games.')
            message = ' '.join(message.split()[1:])  # remove @Mention first word of message
            target = str(m)
            log_message = f'{models.GameLog.member_string(ctx.author)} used pingall on behalf of player ID `{target}` with message: '
        else:
            logger.debug('first party usage of pingall')
            # Play using command on their own games
            if settings.get_user_level(ctx) <= 2:
                logger.debug('insufficient user level')
                return await ctx.send(f'You do not have permission to use this command. You can ask a server staff member to use this command on your games for you.')
            target = str(ctx.author.id)
            log_message = f'{models.GameLog.member_string(ctx.author)} used pingall with message: '

        logger.debug(f'pingall target is {target}')

        try:
            player_match = models.Player.get_or_except(player_string=target, guild_id=ctx.guild.id)
        except exceptions.NoSingleMatch:
            return await ctx.send(f'User <@{target}> is not a registered ELO player.')

        game_list = models.Game.search(player_filter=[player_match], status_filter=2, guild_id=ctx.guild.id)
        logger.debug(f'{len(game_list)} incomplete games for target')

        list_of_players = []
        for g in game_list:
            list_of_players += [f'<@{l.player.discord_member.discord_id}>' for l in g.lineup]

        list_of_players = list(set(list_of_players))
        logger.debug(f'{len(list_of_players)} unique opponents for target')
        clean_message = utilities.escape_role_mentions(message)
        if len(list_of_players) > 100:
            await ctx.send(f'*Warning:* More than 100 unique players are addressed. Only the first 100 will be mentioned.')
        await ctx.send(f'Message to all players in unfinished games for <@{target}>: *{clean_message}*')

        recipient_message = f'Message recipients: {" ".join(list_of_players[:100])}'
        await ctx.send(recipient_message[:2000])

        for game in game_list:
            logger.debug(f'Sending message to game channels for game {game.id} from pingall')
            models.GameLog.write(game_id=game, guild_id=ctx.guild.id, message=f'{log_message} *{discord.utils.escape_markdown(clean_message)}*')
            await game.update_squad_channels(self.bot.guilds, game.guild_id, message=f'Message to all players in unfinished games for <@{target}>: *{clean_message}*')

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

        if settings.is_mod(ctx):
            ctx.command.reset_cooldown(ctx)

        args = args.split()
        try:
            game_id = int(args[0])
            message = ' '.join(args[1:])
        except ValueError:
            game_id = None
            message = ' '.join(args)

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

        if not game.player(discord_id=ctx.author.id) and not settings.is_staff(ctx):
            ctx.command.reset_cooldown(ctx)
            return await ctx.send(f'You are not a player in game {game.id}')

        permitted_channels = settings.guild_setting(game.guild_id, 'bot_channels')
        permitted_channels_private = []
        if settings.guild_setting(game.guild_id, 'game_channel_categories'):
            if game.game_chan:
                permitted_channels = [game.game_chan] + permitted_channels
            if game.smallest_team() > 1:
                permitted_channels_private = [gs.team_chan for gs in game.gamesides]
                permitted_channels = permitted_channels_private + permitted_channels
                # allows ping command to be used in private team channels - only if there is no solo squad in the game which would mean they cant see the message
                # this also adjusts where the @Mention is placed (sent to all team channels instead of simply in the ctx.channel)
            elif ctx.channel.id in [gs.team_chan for gs in game.gamesides]:
                channel_tags = [f'<#{chan_id}>' for chan_id in permitted_channels]
                ctx.command.reset_cooldown(ctx)
                return await ctx.send(f'This command cannot be used in this channel because there is at least one solo player without access to a team channel.\n'
                    f'Permitted channels: {" ".join(channel_tags)}')

        if ctx.channel.id not in permitted_channels and ctx.channel.id not in settings.guild_setting(game.guild_id, 'bot_channels_private'):
            channel_tags = [f'<#{chan_id}>' for chan_id in permitted_channels]
            ctx.command.reset_cooldown(ctx)
            return await ctx.send(f'This command can not be used in this channel. Permitted channels: {" ".join(channel_tags)}')

        player_mentions = [f'<@{l.player.discord_member.discord_id}>' for l in game.lineup]
        full_message = f'Message from {ctx.author.mention} (**{ctx.author.name}**) regarding game {game.id} **{game.name}**:\n*{message}*'
        models.GameLog.write(game_id=game, guild_id=game.guild_id, message=f'{models.GameLog.member_string(ctx.author)} pinged the game with message: *{discord.utils.escape_markdown(message)}*')

        if ctx.channel.id in permitted_channels_private:
            logger.debug(f'Ping triggered in private channel {ctx.channel.id}')
            await game.update_squad_channels(self.bot.guilds, game.guild_id, message=f'{full_message}\n{" ".join(player_mentions)}')
        else:
            logger.debug(f'Ping triggered in non-private channel {ctx.channel.id}')
            await game.update_squad_channels(self.bot.guilds, ctx.guild.id, message=full_message)
            await ctx.send(f'{full_message}\n{" ".join(player_mentions)}')

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

    async def task_send_polychamps_invite(self):
        await self.bot.wait_until_ready()

        message = ('You have met the qualifications to be invited to the **PolyChampions** discord server! '
                   'PolyChampions is a competitive Polytopia server organized into a league, with a focus on team (2v2 and 3v3) games.'
                   '\n To join use this invite link: https://discord.gg/cX7Ptnv')
        while not self.bot.is_closed():
            sleep_cycle = (60 * 60 * 6)
            await asyncio.sleep(30)
            logger.info('Running task task_send_polychamps_invite')
            guild = discord.utils.get(self.bot.guilds, id=settings.server_ids['main'])
            if not guild:
                logger.warn('Could not load guild via server_id')
                break
            utilities.connect()
            dms = models.DiscordMember.members_not_on_polychamps()
            logger.info(f'{len(dms)} discordmember results')
            for dm in dms:
                wins_count, losses_count = dm.wins().count(), dm.losses().count()
                if wins_count < 5:
                    logger.debug(f'Skipping {dm.name} - insufficient winning games')
                    continue
                if dm.games_played(in_days=15).count() < 1:
                    logger.debug(f'Skipping {dm.name} - insufficient recent games')
                    continue
                if dm.elo_max > 1150:
                    logger.debug(f'{dm.name} qualifies due to higher ELO > 1150')
                elif wins_count > losses_count:
                    logger.debug(f'{dm.name} qualifies due to positive win ratio')
                else:
                    logger.debug(f'Skipping {dm.name} - ELO or W/L record insufficient')
                    continue

                logger.debug(f'Sending invite to {dm.name}')
                guild_member = guild.get_member(dm.discord_id)
                if not guild_member:
                    logger.debug(f'Could not load {dm.name} from guild {guild.id}')
                    continue
                try:
                    await guild_member.send(message)
                except discord.DiscordException as e:
                    logger.warn(f'Error DMing member: {e}')
                else:
                    dm.date_polychamps_invite_sent = datetime.datetime.today()
                    dm.save()
            await asyncio.sleep(sleep_cycle)

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

                broadcast_message = (f'To register for ELO leaderboards and matchmaking use the command __`{prefix}setcode YOURCODEHERE`__')
                broadcast_message += f'\nTo get started with joining an open game, go to <#{bot_spam_chan}> and type __`{prefix}games`__'
                broadcast_message += f'\nFor full information go read <#{elo_guide_channel}>.'

                for broadcast_channel in broadcast_channels:
                    if broadcast_channel:
                        message = await broadcast_channel.send(broadcast_message, delete_after=(sleep_cycle - 5))
                        self.bot.purgable_messages = self.bot.purgable_messages[-20:] + [(guild.id, broadcast_channel.id, message.id)]

            await asyncio.sleep(sleep_cycle)


def setup(bot):
    bot.add_cog(misc(bot))
