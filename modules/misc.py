import discord
from discord.ext import commands
import modules.models as models
# import modules.utilities as utilities
import settings
import logging
import peewee
# import modules.exceptions as exceptions
import re
import datetime
import random
from modules.games import PolyGame

logger = logging.getLogger('polybot.' + __name__)


class misc:
    def __init__(self, bot):
        self.bot = bot

    @commands.command(hidden=True, aliases=['ts'])
    @commands.is_owner()
    async def test(self, ctx, *, arg: str = None):

        print(self.bot.guilds)
        # await ctx.author.send(f'blah')
        # for b in range(1, 20):
        #     await ctx.author.send(f'blah {b}')

    @commands.command(usage=None)
    @settings.in_bot_channel_strict()
    async def guide(self, ctx):
        """
        Show an oveview of what the bot is for

        Type `[p]guide` for an overview of what this bot is for and how to use it.
        """
        bot_desc = ('This bot is designed to improve Polytopia multiplier by filling in gaps in two areas: competitive leaderboards, and matchmaking.\n'
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
            value='When an ELO game is concluded the best way to have the points count is by having the loser confirm the status.\n'
            f'For example once Nelluk defeats Scott in a 1v1 game, # **400**, Scott would use the command __`{ctx.prefix}win 400 nelluk`__.\n'
            f'If it is a team game (2v2 or larger), a member of the losing team would use __`{ctx.prefix}win 400 Home`__ for example.'
            'Use the player name for a 1v1, otherwise use a team name.'
            f'If the loser will not confirm the winner can use the same __`{ctx.prefix}win`__ command, and ask a server staff member to confirm it. Please have a game screenshot ready.')

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

        embed = discord.Embed(title='PolyELO Statistics')
        last_month = (datetime.datetime.now() + datetime.timedelta(days=-30))

        games_played = models.Game.select().where(models.Game.is_completed == 1)
        games_played_30d = models.Game.select().where((models.Game.is_pending == 0) & (models.Game.date > last_month))
        incomplete_games = models.Game.select().where((models.Game.is_pending == 0) & (models.Game.is_completed == 0))
        participants_30d = models.Lineup.select(models.Lineup.player).join(models.Game).where(
            (models.Lineup.game.date > last_month)
        ).group_by(models.Lineup.player).distinct()

        embed.add_field(value='\u200b', name=f'`{"----------------------------------":<35}` Global (Local)', inline=False)
        game_stats = (f'`{"Total games completed:":<35}\u200b` {games_played.count()} ({games_played.where(models.Game.guild_id == ctx.guild.id).count()})\n'
                      f'`{"Games created in last 30 days:":<35}\u200b`\u200b {games_played_30d.count()} ({games_played_30d.where(models.Game.guild_id == ctx.guild.id).count()})\n'
                      f'`{"Incomplete games:":<35}\u200b` {incomplete_games.count()} ({incomplete_games.where(models.Game.guild_id == ctx.guild.id).count()})\n'
                      )
        embed.add_field(value='\u200b', name=game_stats)

        stats_2 = (f'`{"Participants in last 30 days:":<35}\u200b` {participants_30d.count()} ({participants_30d.where(models.Game.guild_id == ctx.guild.id).count()})\n')
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
            return await ctx.send(f'Game ID was not included. Example usage: `{ctx.prefix}ping 100 Here\'s a nice note`')
        if not game.player(discord_id=ctx.author.id) and not settings.is_staff(ctx):
            return await ctx.send(f'You are not a player in game {game.id}')
        if not message:
            return await ctx.send(f'Message was not included. Example usage: `{ctx.prefix}ping 100 Here\'s a nice note`')

        player_mentions = [f'<@{l.player.discord_member.discord_id}>' for l in game.lineup]
        await ctx.send(f'Message from {ctx.author.mention} regarding game {game.id} **{game.name}**:\n*{message}*\n{" ".join(player_mentions)}')

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


def setup(bot):
    bot.add_cog(misc(bot))
