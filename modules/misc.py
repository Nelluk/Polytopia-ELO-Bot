import discord
from discord.ext import commands
import modules.models as models
# import modules.utilities as utilities
import settings
import logging
import asyncio
import modules.exceptions as exceptions
import re
import datetime
import random
import csv
from modules.games import PolyGame

logger = logging.getLogger('polybot.' + __name__)


class misc:
    def __init__(self, bot):
        self.bot = bot
        self.bg_task = bot.loop.create_task(self.task_broadcast_newbie_message())

    @commands.command(hidden=True, aliases=['ts'])
    @commands.is_owner()
    async def test(self, ctx, *, arg=None):

        # m = re.search(r'UTC|GMT(\+\d)', arg, re.I)
        m = re.search(r'(?:GMT|UTC)([+-][0-9]{1,2}(?::[0-9]{2}\b)?)', arg, re.I)
        if m:
            print(m, m[0], m[1])
            # max_elo = int(m[1])

    @commands.command(hidden=True, aliases=['bge'])
    async def bulk_global_elo(self, ctx, *, args=None):
        """
        Given a list of players, return that list sorted by player's global ELO. Implemented for koric to aide in tournament seeding.

        `[p]bulk_global elo nelluk koric rickdaheals`
        """
        if not args:
            return await ctx.send(f'Include list of players, example: `{ctx.prefix}bge nelluk koric` - @mentions and raw user IDs are supported')
        player_stats = []
        for arg in args.split(' '):
            try:
                player_match = models.Player.get_or_except(player_string=arg, guild_id=ctx.guild.id)
            except exceptions.NoSingleMatch:
                await ctx.send(f'Could not match *{arg}* to a player. Specify user with @Mention or user ID.')
                continue

            player_stats.append((player_match.discord_member.elo, player_match))

        player_stats.sort(key=lambda tup: tup[0], reverse=True)     # sort the list descending by ELO
        print(player_stats)

        message = '__Player name - Global ELO - Local games played__\n'
        for player in player_stats:
            message += f'{player[1].name} - {player[0]} - {player[1].games_played().count()}\n'

        return await ctx.send(message)

    @commands.command(usage=None)
    @settings.in_bot_channel_strict()
    async def guide(self, ctx):
        """
        Show an overview of what the bot is for

        Type `[p]guide` for an overview of what this bot is for and how to use it.
        """
        bot_desc = ('This bot is designed to improve Polytopia multiplayer by filling in gaps in two areas: competitive leaderboards, and matchmaking.\n'
                    'Its primary home is [PolyChampions](https://discord.gg/cX7Ptnv), a server focused on team play organized into a league.\n'
                    f'To register as a player with the bot use __`{ctx.prefix}setcode YOURPOLYCODEHERE`__')

        embed = discord.Embed(title=f'PolyELO Bot Guide', url='https://discord.gg/cX7Ptnv', description=bot_desc)

        embed.add_field(name='Matchmaking',
            value=f'This helps players organize and arrange games.\nFor example, use __`{ctx.prefix}opengame 1v1`__ to create an open 1v1 game that others can join.\n'
                f'To see a list of open games you can join use __`{ctx.prefix}opengames`__. Once the game is full the host would use __`{ctx.prefix}startgame`__ to close it and track it for the leaderboards.\n'
                f'See __`{ctx.prefix}help matchmaking`__ for all commands.')

        embed.add_field(name='ELO Leaderboards',
            value='Win your games and climb the leaderboards! Earn sweet ELO points!\n'
                'ELO points are gained or lost based on your game results. You will gain more points if you defeat an opponent with a higher ELO.\n'
                f'Use __`{ctx.prefix}lb`__ to view the individual leaderboards. There is also a __`{ctx.prefix}lbsquad`__ squad leaderboard. Form a squad by playing with the same person in multiple games!'
                f'\nSee __`{ctx.prefix}help`__ for all commands.')

        embed.add_field(name='Finishing tracked games',
            value=f'Use the __`{ctx.prefix}win`__ command to tell the bot that a game has concluded.\n'
            f'For example if Nelluk wins game 400, he would type __`{ctx.prefix}win 400 nelluk`__. The losing player must confirm using the same command. '
            f'It is a good idea to take screenshots showing your victories in case the loser will not confirm a game, so server staff can confirm it for you.')

        embed.set_thumbnail(url=self.bot.user.avatar_url_as(size=512))
        embed.set_footer(text='Developer: Nelluk')
        await ctx.send(embed=embed)

    @commands.command(usage=None)
    @settings.in_bot_channel_strict()
    async def credits(self, ctx):
        """
        Display development credits
        """
        embed = discord.Embed(title=f'PolyELO Bot Credits', url='https://discord.gg/cX7Ptnv')

        embed.add_field(name='Developer', value='Nelluk')

        embed.add_field(name='Contributions', value='rickdaheals, koric, Gerenuk, Octo', inline=False)

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
                      f'`{"Games created in last 30 days:":<35}\u200b`\u200b {games_played_30d.count()} ({games_played_30d.where(models.Game.guild_id == ctx.guild.id).count()})\n'
                      f'`{"Games created in last 7 days:":<35}\u200b`\u200b {games_played_7d.count()} ({games_played_7d.where(models.Game.guild_id == ctx.guild.id).count()})\n'
                      f'`{"Incomplete games:":<35}\u200b` {incomplete_games.count()} ({incomplete_games.where(models.Game.guild_id == ctx.guild.id).count()})\n'
                      )
        embed.add_field(value='\u200b', name=game_stats)

        stats_2 = (f'`{"Participants in last 90 days:":<35}\u200b` {participants_90d.count()} ({participants_90d.where(models.Game.guild_id == ctx.guild.id).count()})\n'
                   f'`{"Participants in last 30 days:":<35}\u200b` {participants_30d.count()} ({participants_30d.where(models.Game.guild_id == ctx.guild.id).count()})\n'
                   f'`{"Participants in last 7 days:":<35}\u200b` {participants_7d.count()} ({participants_7d.where(models.Game.guild_id == ctx.guild.id).count()})\n')
        embed.add_field(value='\u200b', name=stats_2)

        await ctx.send(embed=embed)

    @commands.command(usage='game_id')
    @settings.in_bot_channel()
    @commands.cooldown(1, 60, commands.BucketType.user)
    async def ping(self, ctx, game: PolyGame = None, *, message: str = None):
        """ Ping everyone in one of your games with a message

         **Examples**
        `[p]ping 100 I won't be able to take my turn today` - Send a message to everyone in game 100
        """
        if not game:
            ctx.command.reset_cooldown(ctx)
            return await ctx.send(f'Game ID was not included. Example usage: `{ctx.prefix}ping 100 Here\'s a nice note`')
        if not game.player(discord_id=ctx.author.id) and not settings.is_staff(ctx):
            ctx.command.reset_cooldown(ctx)
            return await ctx.send(f'You are not a player in game {game.id}')
        if not message:
            ctx.command.reset_cooldown(ctx)
            return await ctx.send(f'Message was not included. Example usage: `{ctx.prefix}ping 100 Here\'s a nice note`')

        player_mentions = [f'<@{l.player.discord_member.discord_id}>' for l in game.lineup]
        full_message = f'Message from {ctx.author.mention} regarding game {game.id} **{game.name}**:\n*{message}*'

        await ctx.send(f'{full_message}\n{" ".join(player_mentions)}')
        await game.update_squad_channels(ctx, message=full_message)

    @commands.command(aliases=['gex', 'gameexport'])
    @settings.is_mod_check()
    @commands.cooldown(2, 300, commands.BucketType.guild)
    async def game_export(self, ctx):
        """Mod: Export list of completed games to CSV file
        Will be a CSV file that can be opened as a spreadsheet. Might be useful to somebody who wants to do their own tracking.
        """
        await ctx.send('Writing game data to file. This will take a few moments...')

        filename = 'games_export.csv'
        async with ctx.typing():
            with open(filename, mode='w') as export_file:
                game_writer = csv.writer(export_file, delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)

                header = ['game_id', 'game_name', 'game_type', 'game_date', 'completed_timestamp', 'side_id', 'side_name', 'player_name', 'winner', 'player_elo', 'player_elo_change', 'squad_elo', 'squad_elo_change', 'tribe']
                game_writer.writerow(header)

                query = models.Lineup.select().join(models.Game).where(
                    (models.Game.is_confirmed == 1) & (models.Game.guild_id == ctx.guild.id)
                ).order_by(models.Lineup.game_id).order_by(models.Lineup.gameside_id)

                for q in query:
                    is_winner = True if q.game.winner == q.gameside_id else False
                    row = [q.game_id, q.game.name, q.game.size_string(),
                           str(q.game.date), str(q.game.completed_ts), q.gameside_id,
                           q.gameside.name(), q.player.name, is_winner, q.player.elo,
                           q.elo_change_player, q.gameside.squad_id if q.gameside.squad else '', q.gameside.squad.elo if q.gameside.squad else '',
                           q.tribe.tribe.name if q.tribe else '']

                    game_writer.writerow(row)

        await ctx.send(f'Game data written to file **{filename}** in bot.py directory')

        # pb = Pastebin(pastebin_api)
        # pb_url = pb.create_paste_from_file(filepath='games_export.csv', api_paste_private=0, api_paste_expire_date='1D', api_paste_name='Polytopia Game Data')
        # await ctx.send(f'Game data has been exported to the following URL: {pb_url}')

    @commands.command(aliases=['random_tribes', 'rtribe'], usage='game_size [-banned_tribe ...]')
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
            sleep_cycle = (60 * 60 * 2)
            await asyncio.sleep(10)

            for guild in self.bot.guilds:
                broadcast_channels = [guild.get_channel(chan) for chan in settings.guild_setting(guild.id, 'newbie_message_channels')]
                if not broadcast_channels:
                    continue

                prefix = settings.guild_setting(guild.id, 'command_prefix')
                ranked_chan = settings.guild_setting(guild.id, 'ranked_game_channel')
                unranked_chan = settings.guild_setting(guild.id, 'unranked_game_channel')
                bot_spam_chan = settings.guild_setting(guild.id, 'bot_channels_strict')[0]

                broadcast_message = ('I am here to help improve Polytopia multiplayer with matchmaking and leaderboards!\n'
                    f'To **register your code** with me, type __`{prefix}setcode YOURCODEHERE`__')

                if ranked_chan:
                    broadcast_message += (f'\n\nTo find **ranked** games that count for the leaderboard, join <#{ranked_chan}>. '
                        f'Type __`{prefix}opengames`__ to see what games are available to join. '
                        f'To host your own game, try __`{prefix}opengame 1v1`__ to host a 1v1 duel. '
                        'I will alert you once someone else has joined, and then you will add your opponent\'s friend code and create the game in Polytopia.')

                if unranked_chan:
                    broadcast_message += (f'\n\nYou can also find unranked games - use the same commands as above in <#{unranked_chan}>. '
                        'Start here if you are new to Polytopia multiplayer.')

                broadcast_message += f'\n\nFor full information go to <#{bot_spam_chan}> and type __`$guide`__ or __`$help`__'

                for broadcast_channel in broadcast_channels:
                    if broadcast_channel:
                        await broadcast_channel.send(broadcast_message, delete_after=(sleep_cycle - 5))

            await asyncio.sleep(sleep_cycle)


def setup(bot):
    bot.add_cog(misc(bot))
