import discord
from discord.ext import commands
import modules.models as models
import settings
import modules.exceptions as exceptions
import peewee
import re
import datetime
# import random
import logging

logger = logging.getLogger('polybot.' + __name__)


class matchmaking():
    """
    Helps players find other players.
    """

    def __init__(self, bot):
        self.bot = bot

    def poly_match(match_id):
        # Give game ID integer return matching Match or None. Can be used as a converter function for discord command input:
        # https://discordpy.readthedocs.io/en/rewrite/ext/commands/commands.html#basic-converters

        try:
            match_id = int(match_id)
        except ValueError:
            if match_id.upper()[0] == 'M':
                match_id = match_id[1:]
            else:
                logger.warn(f'Match with ID {match_id} cannot be found.')
                return None
        with models.db:
            try:
                match = models.Match.get(id=match_id)  # not sure the prefetch will work
                logger.debug(f'Match with ID {match_id} found.')
                return match
            except peewee.DoesNotExist:
                logger.warn(f'Match with ID {match_id} cannot be found.')
                return None
            except ValueError:
                logger.error(f'Invalid Match ID "{match_id}".')
                return None

    @settings.in_bot_channel()
    @commands.command(usage='size expiration rules')
    async def openmatch(self, ctx, *args):

        """
        Opens a matchmaking session for others to find
        Expiration can be between 1H - 96H
        Size can be between 1v1 and 6v6

        **Examples:**
        `[p]openmatch 1v1`
        `[p]openmatch 2v2 48h`  (Expires in 48 hours)
        `[p]openmatch 2v2 Large map, no bardur`  (Adds a note to the game)
        """
        # TODO: quote mark in this example fails:
        # $openmatch 1v1 let’s discuss the details

        team_size = False
        expiration_hours = 24
        note_args, team_objs = [], []

        try:
            match_host = models.Player.get_or_except(str(ctx.author.id), ctx.guild.id)
        except exceptions.NoSingleMatch:
            return await ctx.send(f'You must be a registered player before hosting a match. Try `{ctx.prefix}setcode POLYCODE`')

        if models.Match.select().where(models.Match.host == match_host).count() > 4:
            return await ctx.send(f'You have too many open matches already. Try using `{ctx.prefix}delmatch` on an existing one.')

        for arg in args:
            m = re.fullmatch(r"\d+(?:(v|vs)\d+)+", arg.lower())
            if m:
                # arg looks like '3v3' or '1v1v1'
                team_size_str = m[0]
                team_sizes = [int(x) for x in arg.lower().split(m[1])]  # split on 'vs' or 'v'; whichever the regexp detects
                if max(team_sizes) > 6:
                    return await ctx.send(f'Invalid match size {team_size_str}: Teams cannot be larger than 6 players.')
                if sum(team_sizes) > 12:
                    return await ctx.send(f'Invalid match size {team_size_str}: Games can have a maximum of 12 players.')
                if len(team_sizes) > 8:
                    return await ctx.send(f'Invalid match size {team_size_str}: Games cannot have more than 8 teams.')
                team_size = True
                continue
            m = re.match(r"(\d+)h", arg.lower())
            if m:
                # arg looks like '12h'
                if not 0 < int(m[1]) < 97:
                    return await ctx.send(f'Invalid expiration {arg}. Must be between 1H and 96H (One hour through four days).')
                expiration_hours = int(m[1])
                continue
            note_args.append(arg)

        if not team_size:
            return await ctx.send(f'Match size is required. Include argument like *2v2* to specify size')

        match_notes = ' '.join(note_args)[:100]
        notes_str = match_notes if match_notes else "\u200b"
        expiration_timestamp = (datetime.datetime.now() + datetime.timedelta(hours=expiration_hours)).strftime("%Y-%m-%d %H:%M:%S")
        match = models.Match.create(host=match_host, expiration=expiration_timestamp, notes=match_notes, guild_id=ctx.guild.id)
        for count, size in enumerate(team_sizes):
            team_objs.append(models.MatchSide.create(match=match, size=size, position=count + 1))

        models.MatchPlayer.create(player=match_host, match=match, side=team_objs[0])
        await ctx.send(f'Starting new open match ID M{match.id}. Size: {team_size_str}. Expiration: {expiration_hours} hours.\nNotes: *{notes_str}*')

    @commands.command(usage='match_id side_number Side Name')
    async def matchside(self, ctx, match: poly_match, side_num: int, *, args):
        if not match.is_hosted_by(ctx.author.id) or not settings.is_staff(ctx):
            return await ctx.send(f'Only the match host or server staff can do this.')

        if not (1 <= side_num <= len(match.sides)):
            return await ctx.send(f'Invalid side_number. Expecting 1-{len(match.sides)}.')

        with models.db:
            matchside = match.get_side(num=side_num)
            matchside.name = args
            matchside.save()

        return await ctx.send(f'Side {side_num} for Match M{match.id} has been named "{args}"')

    @settings.in_bot_channel()
    @commands.command(usage='match_id')
    async def match(self, ctx, match: poly_match):
        """Display details on a match"""

        if match is None:
            return await ctx.send(f'No matching match was found. Use {ctx.prefix}listmatches to see available matches.')
        # if len(match.matchplayer) >= (match.team_size * 2):
        #         await ctx.send(f'Match M{match.id} is now full and the host should start the game with `{ctx.prefix}startmatch M{match.id}`.')
        await ctx.send(embed=match.embed())

    @settings.in_bot_channel()
    @commands.command(usage='match_id')
    async def joinmatch(self, ctx, match: poly_match, *args):
        """
        Join an open match
        **Example:**
        `[p]joinmatch M25`
        joinmatch m5
        joinmatch m5 [ronin | 2]
        joinmatch m5 jonathan [ronin | 2]
        """

        if match is None:
            return await ctx.send(f'No matching match was found. Use {ctx.prefix}listmatches to see available matches.')


def setup(bot):
    bot.add_cog(matchmaking(bot))
