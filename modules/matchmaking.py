# import discord
from discord.ext import commands
import modules.models as models
import modules.utilities as utilities
import settings
import modules.exceptions as exceptions
from modules.games import post_newgame_messaging
import peewee
import re
import datetime
import random
import logging
import asyncio

logger = logging.getLogger('polybot.' + __name__)


class PolyMatch(commands.Converter):
    async def convert(self, ctx, match_id):

        try:
            match_id = int(match_id)
        except ValueError:
            if match_id.upper()[0] == 'M':
                match_id = match_id[1:]
            else:
                await ctx.send(f'Match with ID {match_id} cannot be found. Use {ctx.prefix}listmatches to see available matches.')
                raise commands.UserInputError()
        with models.db:
            try:
                match = models.Match.get(id=match_id)
                logger.debug(f'Match with ID {match_id} found.')

                if match.guild_id != ctx.guild.id:
                    await ctx.send(f'Match with ID {match_id} cannot be found on this server. Use {ctx.prefix}listmatches to see available matches.')
                    raise commands.UserInputError()

                return match
            except peewee.DoesNotExist:
                await ctx.send(f'Match with ID {match_id} cannot be found. Use {ctx.prefix}listmatches to see available matches.')
                raise commands.UserInputError()
            except ValueError:
                await ctx.send(f'Invalid Match ID "{match_id}".')
                raise commands.UserInputError()


class matchmaking():
    """
    Helps players find other players.
    """

    def __init__(self, bot):
        self.bot = bot

    # @settings.in_bot_channel()
    @commands.command(usage='size expiration rules')
    async def openmatch(self, ctx, *, args):

        """
        Opens a matchmaking session for others to find
        Expiration can be between 1H - 96H
        Size examples: 1v1, 2v2, 1v1v1v1v1, 3v3v3

        **Examples:**
        `[p]openmatch 1v1`
        `[p]openmatch 2v2 48h`  (Expires in 48 hours)
        `[p]openmatch 2v2 Large map, no bardur`  (Adds a note to the game)
        """

        team_size = False
        expiration_hours = 24
        note_args, team_objs = [], []

        try:
            match_host = models.Player.get_or_except(str(ctx.author.id), ctx.guild.id)
        except exceptions.NoSingleMatch:
            return await ctx.send(f'You must be a registered player before hosting a match. Try `{ctx.prefix}setcode POLYCODE`')

        if models.Match.select().where(
            (models.Match.host == match_host) & (models.Match.is_started == 0)
        ).count() > 30:
            return await ctx.send(f'You have too many open matches already. Try using `{ctx.prefix}delmatch` on an existing one.')

        for arg in args.split(' '):
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

        server_size_max = settings.guild_setting(ctx.guild.id, 'max_team_size')
        if max(team_sizes) > server_size_max and ctx.guild.id != settings.server_ids['polychampions']:
            return await ctx.send(f'Maximium team size on this server is {server_size_max}.\n'
                'For full functionality with support for up to 6-person teams and team channels check out PolyChampions - <https://tinyurl.com/polychampions>')

        match_notes = ' '.join(note_args)[:100]
        notes_str = match_notes if match_notes else "\u200b"
        expiration_timestamp = (datetime.datetime.now() + datetime.timedelta(hours=expiration_hours)).strftime("%Y-%m-%d %H:%M:%S")
        match = models.Match.create(host=match_host, expiration=expiration_timestamp, notes=match_notes, guild_id=ctx.guild.id)
        for count, size in enumerate(team_sizes):
            team_objs.append(models.MatchSide.create(match=match, size=size, position=count + 1))

        models.MatchPlayer.create(player=match_host, match=match, side=team_objs[0])
        await ctx.send(f'Starting new open match ID M{match.id}. Size: {team_size_str}. Expiration: {expiration_hours} hours.\nNotes: *{notes_str}*')

    @commands.command(usage='match_id side_number Side Name')
    async def matchside(self, ctx, match: PolyMatch, side_lookup: str, *, args):
        """
        Give a name to a side in a match you host
        **Example:**
        `[p]matchside m25 2 Ronin` - Names side 2 of Match M25 as 'The Ronin'
        """

        if not match.is_hosted_by(ctx.author.id) and not settings.is_staff(ctx):
            return await ctx.send(f'Only the match host or server staff can do this.')

        # TODO: Have this command also allow side re-ordering
        # matchside m1 1 name ronin
        # matchside m1 ronin nelluk rickdaheals jonathan

        matchside, _ = match.get_side(lookup=side_lookup)
        if not matchside:
            return await ctx.send(f'Can\'t find that side for match M{match.id}.')
        matchside.name = args
        matchside.save()

        return await ctx.send(f'Side {matchside.position} for Match M{match.id} has been named "{args}"')

    # @settings.in_bot_channel()
    @commands.command(usage='match_id')
    async def match(self, ctx, match: PolyMatch):
        """Display details on a match"""

        # if len(match.matchplayer) >= (match.team_size * 2):
        #         await ctx.send(f'Match M{match.id} is now full and the host should start the game with `{ctx.prefix}startmatch M{match.id}`.')
        embed, content = match.embed(ctx)
        await ctx.send(embed=embed, content=content)

    # @settings.in_bot_channel()
    @commands.command(usage='match_id', aliases=['join'])
    async def joinmatch(self, ctx, match: PolyMatch, *args):
        """
        Join an open match
        **Example:**
        `[p]joinmatch m25`
        `[p]joinmatch m5 ronin` - Join match m5 to the side named 'ronin'
        `[p]joinmatch m5 ronin 2` - Join match m5 to side number 2
        `[p]joinmatch m5 rickdaheals jets` - Add a person to your match. Side must be specified.
        """
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
            if not side:
                return await ctx.send(f'Could not find side with "{args[0]}" in match M{match.id}. You can use a side number or name if available.')
        elif len(args) == 2:
            # author is putting a third party into this match
            if not settings.is_matchmaking_power_user(ctx):
                return await ctx.send('You do not have permissions to add another person to a match. Tell them to use the command:\n'
                    f'`{ctx.prefix}joinmatch M{match.id} {args[1]}` to join themselves.')
            target = args[0]
            side, side_open = match.get_side(lookup=args[1])
            if not side:
                return await ctx.send(f'Could not find side with "{args[1]}" in match M{match.id}. You can use a side number or name if available.\n'
                    f'Syntax: `{ctx.prefix}join M{match.id} <player> <side>`')
        else:
            return await ctx.send(f'Invalid command. See `{ctx.prefix}help joinmatch` for usage examples.')

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

        players, capacity = match.capacity()
        if players >= capacity:
            await ctx.send(f'Match M{match.id} is now full and the host <@{match.host.discord_member.discord_id}> should start the game.')
        # TODO: output correct ordering
        embed, content = match.embed(ctx)
        await ctx.send(embed=embed, content=content)

    @commands.command(usage='match_id', aliases=['leave'])
    async def leavematch(self, ctx, match: PolyMatch):
        """
        Leave a match that you have joined
        **Example:**
        `[p]leavematch M25`
        """
        if match.is_hosted_by(ctx.author.id):

            if not settings.is_matchmaking_power_user(ctx):
                return await ctx.send('You do not have permissions to leave your own match.\n'
                    f'If you want to delete use `{ctx.prefix}deletematch M{match.id}`')

            await ctx.send(f'**Warning:** You are leaving your own match. You will still be the host. '
                f'If you want to delete use `{ctx.prefix}deletematch M{match.id}`')

        if match.is_started:
            game_str = f' with game # {match.game.id}' if match.game else ''
            return await ctx.send(f'Match M{match.id} has already started{game_str}.')

        matchplayer = match.player(discord_id=ctx.author.id)
        if not matchplayer:
            return await ctx.send(f'You are not a member of match M{match.id}')

        matchplayer.delete_instance()
        await ctx.send('Removing you from the match.')

    @commands.command(usage='match_id', aliases=['notes'])
    async def matchnotes(self, ctx, match: PolyMatch, *, notes: str = None):
        """
        Edit notes for a match you host
        **Example:**
        `[p]matchnotes M25 Large map`
        """

        if not match.is_hosted_by(ctx.author.id) and not settings.is_staff(ctx):
            return await ctx.send(f'Only the match host or server staff can do this.')

        old_notes = match.notes
        match.notes = notes[:100] if notes else None
        match.save()

        await ctx.send(f'Updated notes for match M{match.id} to: {match.notes}\nPrevious notes were: {old_notes}')
        embed, content = match.embed(ctx)
        await ctx.send(embed=embed, content=content)

    @commands.command(usage='match_id player')
    async def kick(self, ctx, match: PolyMatch, player: str):
        """
        Kick a player from an open match
        **Example:**
        `[p]kick M25 koric`
        """
        if not match.is_hosted_by(ctx.author.id) and not settings.is_staff(ctx):
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

        matchplayer.delete_instance()
        await ctx.send(f'Removing {target.name} from the match.')

    @commands.command(aliases=['deletematch'], usage='match_id')
    async def delmatch(self, ctx, match: PolyMatch):
        """Deletes a match that you host
        Staff can also delete any match.
        **Example:**
        `[p]delmatch M25`
        """
        if match.is_hosted_by(ctx.author.id) or settings.is_staff(ctx):
            # User is deleting their own match, or user has a staff role
            await ctx.send(f'Deleting match M{match.id}')
            match.delete_instance()
            return
        else:
            return await ctx.send(f'You only have permission to delete your own matches.')

    # @settings.in_bot_channel()
    @commands.command(aliases=['listmatches', 'matchlist', 'openmatches', 'listmatch'])
    async def matches(self, ctx, *args):
        """
        List current matches, with filtering options.
        Full matches will still be listed until the host starts or deletes them with `[p]startmatch` / `[p]delmatch`
        Add **OPEN** or **FULL** to command to filter by open/full matches
        Anything else will compare to current participants, the host, or the match notes.
        **Example:**
        `[p]matches` - List all unexpired matches
        `[p]matches Nelluk` - List all unexpired matches where Nelluk is a participant/host
        `[p]matches Nelluk full` - List all unexpired matches with Nelluk that are full
        `[p]matches Bardur` - List all unexpired matches where "Bardur" is in the match notes
        `[p]matches Ronin` - List all unexpired matches where "Ronin" is in one of the sides' name.
        `[p]matches waiting` - *Special filter* - all full matches without a game, including expired. Purged after a few days.
        """
        models.Match.purge_expired_matches()

        args_list = [arg.upper() for arg in args]
        if len(args_list) == 1 and args_list[0] == 'WAITING':
            match_list = models.Match.waiting_to_start()
            title_str = 'Matches that are full and waiting for host to start (including expired)'
        elif args:
            if 'FULL' in args_list:
                args_list.remove('FULL')
                status_filter = 2
                status_word = ' *full*'
            elif 'CLOSED' in args_list:
                args_list.remove('CLOSED')
                status_filter = 2
                status_word = ' *full*'
            elif 'OPEN' in args_list:
                args_list.remove('OPEN')
                status_filter = 1
                status_word = ' *open*'
            else:
                status_filter = None
                status_word = ''

            # Return any match where args_str appears in match side names, match notes, or if args_str is a Player, a match where player is a participant or host
            arg_str = ' '.join(args_list)
            title_str = f'Current{status_word} matches matching "{arg_str}"'
            try:
                target = models.Player.get_or_except(player_string=arg_str, guild_id=ctx.guild.id)
            except exceptions.NoSingleMatch:
                target = None

            arg_str = '%'.join(args_list)  # for SQL wildcard match
            match_list = models.Match.search(guild_id=ctx.guild.id, player=target, search=arg_str, status=status_filter)

        else:
            title_str = 'All current matches'
            # match_list = models.Match.active_list(guild_id=ctx.guild.id)
            match_list = models.Match.search(guild_id=ctx.guild.id, player=None, search=None, status=None)

        title_str_full = title_str + f'\nUse `{ctx.prefix}joinmatch M#` to join one or `{ctx.prefix}match M#` for more details.'
        matchlist_fields = [(f'`{"ID":<8}{"Host":<40} {"Type":<7} {"Capacity":<7} {"Exp":>4}`', '\u200b')]

        for match in match_list:

            notes_str = match.notes if match.notes else "\u200b"
            players, capacity = match.capacity()
            capacity_str = f' {players}/{capacity}'
            expiration = int((match.expiration - datetime.datetime.now()).total_seconds() / 3600.0)
            expiration = 'Exp' if expiration < 0 else f'{expiration}H'

            matchlist_fields.append((f'`{"M"f"{match.id}":<8}{match.host.name:<40} {match.size_string():<7} {capacity_str:<7} {expiration:>5}`',
                notes_str))

        self.bot.loop.create_task(utilities.paginate(self.bot, ctx, title=title_str_full, message_list=matchlist_fields, page_start=0, page_end=15, page_size=15))
        # paginator done as a task because otherwise it will not let the waitlist message send until after pagination is complete (20+ seconds)

        waitlist = [f'M{m.id}' for m in models.Match.waiting_to_start(host_discord_id=ctx.author.id)]
        if waitlist:
            await asyncio.sleep(1)
            await ctx.send(f'You have full matches waiting to start: **{", ".join(waitlist)}**\n'
                f'Type `{ctx.prefix}match M#` for more details.')

    # @settings.in_bot_channel()
    @commands.command(usage='match_id Name of Poly Game')
    async def startmatch(self, ctx, match: PolyMatch, *, name: str = None):
        """
        Start match and track game with ELO bot
        Use this command after you have created the game in Polytopia.
        If the game is a compatible type (currently requires two equal teams) the game will be added as an ELO game.
        **Example:**
        `[p]startmatch M5 Fields of Fire`
        """

        if not match.is_hosted_by(ctx.author.id) and not settings.is_staff(ctx):
            return await ctx.send(f'Only the match host or server staff can do this.')

        if not name:
            return await ctx.send(f'Game name is required. Example: `{ctx.prefix}startmatch M{match.id} Name of Game`')

        if match.is_started:
            return await ctx.send(f'Match M{match.id} has already started{" with game # " +  str(match.game.id) if match.game else ""}.')

        players, capacity = match.capacity()
        if players != capacity:
            return await ctx.send(f'Match M{match.id} is not full.\nCapacity {players}/{capacity}.')

        teams, mentions = [], []

        for side in match.sides:
            team = []
            for matchplayer in side.sideplayers:
                guild_member = ctx.guild.get_member(matchplayer.player.discord_member.discord_id)
                if not guild_member:
                    return await ctx.send(f'Player *{matchplayer.player.name}* not found on this server. (Maybe they left?)')
                team.append(guild_member)
                mentions.append(guild_member.mention)
            teams.append(team)

        if len(teams) != 2 or len(teams[0]) != len(teams[1]):
            logger.info(f'Match M{match.id} started as non-ELO game')
            notes_str = f'\n**Notes:** {match.notes}' if match.notes else ''
            await ctx.send(f'Match M{match.id} started as non-ELO game "**{name.title()}**."\nRoster: {" ".join(mentions)}{notes_str}'
                '\nThis match is now marked as started, but will not be tracked as an ELO game since it does not have two equally-sized teams.')

            match.is_started = True
            match.save()

            embed, content = match.embed(ctx)
            await ctx.send(embed=embed, content=content)
        else:
            newgame = models.Game.create_game(teams,
                name=name, guild_id=ctx.guild.id,
                require_teams=settings.guild_setting(ctx.guild.id, 'require_teams'))
            logger.info(f'Match M{match.id} started as ELO game {newgame.id}')
            match.is_started = True
            match.game = newgame
            match.save()
            await post_newgame_messaging(ctx, game=newgame)

    @commands.command(aliases=['rtribes', 'rtribe'], usage='game_size [-banned_tribe ...]')
    async def random_tribes(self, ctx, size='1v1', *args):
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
    bot.add_cog(matchmaking(bot))
