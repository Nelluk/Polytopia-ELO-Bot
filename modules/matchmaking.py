import discord
from discord.ext import commands
import modules.models as models
import modules.utilities as utilities
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
    async def matchside(self, ctx, match: poly_match, side_lookup: str, *, args):
        if not match.is_hosted_by(ctx.author.id) or not settings.is_staff(ctx):
            return await ctx.send(f'Only the match host or server staff can do this.')

        # TODO: Have this command also allow side re-ordering
        # matchside m1 1 name ronin
        # matchside m1 ronin nelluk rickdaheals jonathan

        with models.db:
            matchside, _ = match.get_side(lookup=side_lookup)
            if not matchside:
                return await ctx.send(f'Can\'t find that side for match M{match.id}.')
            matchside.name = args
            matchside.save()

        return await ctx.send(f'Side {matchside.position} for Match M{match.id} has been named "{args}"')

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
    @commands.command(usage='match_id', aliases=['join'])
    async def joinmatch(self, ctx, match: poly_match, *args):
        """
        Join an open match
        **Example:**
        `[p]joinmatch M25`
        joinmatch m5                        # 0 args
        joinmatch m5 [ronin | 2]            # 1 args
        joinmatch m5 jonathan <ronin | 2>   # 2 args
        """
        if match is None:
            return await ctx.send(f'No matching match was found. Use {ctx.prefix}listmatches to see available matches.')

        if len(args) == 0:
            # ctx.author is joining a match, no side given
            target = str(ctx.author.id)
            side, side_open = match.first_open_side(), True
            if not side:
                return await ctx.send(f'Match M{match.id} is completely full!')
        elif len(args) == 1:
            # ctx.author is joining a match, with a side specified
            target = str(ctx.author.id)
            side, side_open = match.get_side(lookup=args[0])
        elif len(args) == 2:
            # author is putting a third party into this match
            # TODO: permissions on this?
            target = args[0]
            side, side_open = match.get_side(lookup=args[1])
            print(side, side_open)
        else:
            return await ctx.send(f'Invalid command. See `{ctx.prefix}help joinmatch` for usage examples.')

        if not side:
                return await ctx.send(f'Could not find side with "{args[1]}" in match M{match.id}. You can use a side number or name if available.')

        if not side_open:
            return await ctx.send(f'That side of match M{match.id} is already full. See `{ctx.prefix}match M{match.id}` for details.')

        try:
            target = models.Player.get_or_except(player_string=target, guild_id=ctx.guild.id)
        except exceptions.NoMatches:
            # No matching name in database. Warn if player is found in guild.
            matches = await utilities.get_guild_member(ctx, target)
            if len(matches) > 0:
                await ctx.send(f'"{matches[0].name}" was found in the server but is not registered with me. '
                    f'Players can be registered with `{ctx.prefix}setcode`')
            return await ctx.send(f'Could not find an ELO player matching "{target}".')  # Check for existing discord member esp if target==author
        except exceptions.TooManyMatches:
            return await ctx.send(f'More than one player found matching "{target}". be more specific or use an @Mention.')

        if match.player(target):
            return await ctx.send(f'You are already in match M{match.id}. If you are trying to change sides, use `{ctx.prefix}leavematch M{match.id}` first.')
        models.MatchPlayer.create(player=target, match=match, side=side)

        await ctx.send(f'Joining <@{target.discord_member.discord_id}> to side {side.position} of match M{match.id}')
        # TODO: Check if match full
        await ctx.send(embed=match.embed())

    @commands.command(usage='match_id')
    async def leavematch(self, ctx, match: poly_match):
        """
        Leave a match that you have joined
        **Example:**
        `[p]leavematch M25`
        """
        if match is None:
            return await ctx.send(f'No matching match was found. Use {ctx.prefix}listmatches to see available matches.')
        if match.is_hosted_by(ctx.author.id):
            # TODO: permission check for this
            # return await ctx.send(f'You can\'t leave your own match. Use `{ctx.prefix}delmatch` instead.')
            await ctx.send(f'**Warning:** You are leaving your own match. You will still be the host. '
                f'If you want to delete use `{ctx.prefix}deletematch M{match.id}`')

        if match.is_started:
            game_str = f' with game # {match.game.id}' if match.game else ''
            return await ctx.send(f'Match M{match.id} has already started{game_str}.')

        matchplayer = match.player(discord_id=ctx.author.id)
        if not matchplayer:
            return await ctx.send(f'You are not a member of match M{match.id}')

        with models.db:
            matchplayer.delete_instance()
            await ctx.send('Removing you from the match.')

    @commands.command(usage='match_id player')
    async def kick(self, ctx, match: poly_match, player: str):
        """
        Kick a player from an open match
        **Example:**
        `[p]kick M25 koric`
        """
        if match is None:
            return await ctx.send(f'No matching match was found. Use {ctx.prefix}listmatches to see available matches.')
        if not match.is_hosted_by(ctx.author.id) or not settings.is_staff(ctx):
            return await ctx.send(f'Only the match host or server staff can do this.')

        if match.is_started:
            game_str = f' with game # {match.game.id}' if match.game else ''
            return await ctx.send(f'Match M{match.id} has already started{game_str}.')

        try:
            target = models.Player.get_or_except(player_string=player, guild_id=ctx.guild.id)
        except exceptions.NoSingleMatch:
            return await ctx.send(f'Could not match "{player}" to an ELO player.')

        if target.discord_member.discord_id == ctx.author.id:
            return await ctx.send('Stop kicking yourself!')

        matchplayer = match.player(player=target)
        if not matchplayer:
            return await ctx.send(f'{target.name} is not a member of match M{match.id}.')

        with models.db:
            matchplayer.delete_instance()
            await ctx.send(f'Removing {target.name} from the match.')

    @commands.command(aliases=['deletematch'], usage='match_id')
    async def delmatch(self, ctx, match: poly_match):
        """Deletes a match that you host
        Staff can also delete any match.
        **Example:**
        `[p]delmatch M25`
        """

        if match is None:
            return await ctx.send(f'No matching match was found. Use {ctx.prefix}listmatches to see available matches.')

        if match.is_hosted_by(ctx.author.id) or settings.is_staff(ctx):
            # User is deleting their own match, or user has a staff role
            await ctx.send(f'Deleting match M{match.id}')
            match.delete_instance()
            return
        else:
            return await ctx.send(f'You only have permission to delete your own matches.')

    @settings.in_bot_channel()
    @commands.command(aliases=['listmatches', 'matchlist', 'openmatches', 'listmatch'])
    async def matches(self, ctx, *args):
        """
        List open matches, with filtering options.
        Full matches will still be listed until the host starts or deletes them with `[p]startmatch` / `[p]delmatch`
        **Example:**
        `[p]matches` - List all unexpired matches
        `[p]matches Nelluk` - List all unexpired matches where Nelluk is a participant/host
        `[p]matches Bardur` - List all unexpired matches where "Bardur" is in the match notes
        `[p]matches Ronin` - List all unexpired matches where "Ronin" is in one of the sides' name.
        """
        models.Match.purge_expired_matches()

        if args:
            # Return any match where args_str appears in match side names, match notes, or if args_str is a Player, a match where player is a participant or host
            arg_str = ' '.join(args)
            title_str = f'Current matches matching "{arg_str}"'
            try:
                target = models.Player.get_or_except(player_string=arg_str, guild_id=ctx.guild.id)
            except exceptions.NoSingleMatch:
                target = None

            arg_str = '%'.join(args)  # for SQL wildcard match
            match_list = models.Match.search(guild_id=ctx.guild.id, player=target, search=arg_str)

        else:
            title_str = 'All current matches'
            match_list = models.Match.active_list(guild_id=ctx.guild.id)

        embed = discord.Embed(title=f'{title_str}\nUse `{ctx.prefix}joinmatch M#` to join one or `{ctx.prefix}match M#` for more details.')
        embed.add_field(name=f'`{"ID":<8}{"Host":<40} {"Type":<7} {"Capacity":<7} {"Exp":>4}`', value='\u200b', inline=False)
        # for match in models.Match.active_list(guild_id=ctx.guild.id):
        for match in match_list:

            notes_str = match.notes if match.notes else "\u200b"
            players, capacity = match.capacity()
            capacity_str = f' {players}/{capacity}'
            expiration = int((match.expiration - datetime.datetime.now()).total_seconds() / 3600.0)

            embed.add_field(name=f'`{"M"f"{match.id}":<8}{match.host.name:<40} {match.size_string():<7} {capacity_str:<7} {expiration:>4}H`',
                value=f'{notes_str}')
        await ctx.send(embed=embed)

    @settings.in_bot_channel()
    @commands.command()
    async def startmatch(self, ctx, match: poly_match, name: str):

        if not match.is_hosted_by(ctx.author.id) or not settings.is_staff(ctx):
            return await ctx.send(f'Only the match host or server staff can do this.')

        if match.is_started:
            return await ctx.send(f'Match M{match.id} has already started{" with game # " +  match.game.id if match.game else ""}.')

        players, capacity = match.capacity()
        if players != capacity:
            return await ctx.send(f'Match M{match.id} is not full.\nCapacity {players}/{capacity}.')

        teams = []

        for side in match.sides:
            team = []
            for matchplayer in side.sideplayers:
                guild_member = ctx.guild.get_member(matchplayer.player.discord_member.discord_id)
                if not guild_member:
                    return await ctx.send(f'Player *{matchplayer.player.name}* not found on this server. (Maybe they left?)')
                team.append(guild_member)
            teams.append(team)

        print(teams)

        if len(teams) != 2 or len(teams[0]) != len(teams[1]):
            await ctx.send(f'This match is now marked as started, but will not be tracked as an ELO game since it does not have two equally-sized teams.')
        else:
            pass


def setup(bot):
    bot.add_cog(matchmaking(bot))
