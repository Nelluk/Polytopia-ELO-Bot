import discord
from discord.ext import commands
import modules.models as models
import modules.utilities as utilities
import settings
# import modules.exceptions as exceptions
from modules.games import post_newgame_messaging
import peewee
import re
import datetime
import logging
import asyncio

logger = logging.getLogger('polybot.' + __name__)


class PolyMatch(commands.Converter):
    async def convert(self, ctx, match_id: int):

        with models.db:
            try:
                match = models.Game.get(id=match_id)
                logger.debug(f'Game with ID {match_id} found.')

                if match.guild_id != ctx.guild.id:
                    await ctx.send(f'Game with ID {match_id} cannot be found on this server. Use {ctx.prefix}opengames to see available matches.')
                    raise commands.UserInputError()
                return match
            except peewee.DoesNotExist:
                await ctx.send(f'Game with ID {match_id} cannot be found. Use {ctx.prefix}opengames to see available matches.')
                raise commands.UserInputError()
            except ValueError:
                await ctx.send(f'Invalid Game ID "{match_id}".')
                raise commands.UserInputError()


class matchmaking():
    """
    Host open and find open games.
    """

    def __init__(self, bot):
        self.bot = bot
        self.bg_task = bot.loop.create_task(self.task_print_matchlist())

    # @settings.in_bot_channel()
    @commands.command(aliases=['openmatch'], usage='size expiration rules')
    async def opengame(self, ctx, *, args=None):

        """
        Opens a game that others can join
        Expiration can be between 1H - 96H
        Size examples: 1v1, 2v2, 1v1v1v1v1, 3v3v3

        **Examples:**
        `[p]opengame 1v1`
        `[p]opengame 1v1 48h`  (Expires in 48 hours)
        `[p]opengame 1v1 unranked`  (Add word *unranked* to have game not count for ELO)
        `[p]opengame 2v2 Large map, no bardur`  (Adds a note to the game)
        """

        team_size, is_ranked = False, True
        expiration_hours = 24
        note_args = []

        if not args:
            return await ctx.send('Game size is required. Include argument like *2v2* to specify size.'
                f'\nExample: `{ctx.prefix}opengame 1v1 large map`')

        host, _ = models.Player.get_by_discord_id(discord_id=ctx.author.id, discord_name=ctx.author.name, discord_nick=ctx.author.nick, guild_id=ctx.guild.id)
        if not host:
            # Matching guild member but no Player or DiscordMember
            return await ctx.send(f'You must be a registered player before hosting a match. Try `{ctx.prefix}setcode POLYCODE`')

        if settings.guild_setting(ctx.guild.id, 'require_teams') and not models.Player.is_in_team(guild_id=ctx.guild.id, discord_member=ctx.author):
            return await ctx.send(f'You must join a Team in order to participate in games on this server.')

        # if models.Match.select().where(
        if models.Game.select().where(
            (models.Game.host == host) & (models.Game.is_pending == 1)
        ).count() > 5:
            return await ctx.send(f'You have too many open games already. Try using `{ctx.prefix}delgame` on an existing one.')

        for arg in args.split(' '):
            m = re.fullmatch(r"\d+(?:(v|vs)\d+)+", arg.lower())
            if m:
                # arg looks like '3v3' or '1v1v1'
                team_size_str = m[0]
                team_sizes = [int(x) for x in arg.lower().split(m[1])]  # split on 'vs' or 'v'; whichever the regexp detects
                if max(team_sizes) > 6:
                    return await ctx.send(f'Invalid game size {team_size_str}: Teams cannot be larger than 6 players.')
                if sum(team_sizes) > 12:
                    return await ctx.send(f'Invalid game size {team_size_str}: Games can have a maximum of 12 players.')
                team_size = True
                continue
            m = re.match(r"(\d+)h", arg.lower())
            if m:
                # arg looks like '12h'
                if not 0 < int(m[1]) < 97:
                    return await ctx.send(f'Invalid expiration {arg}. Must be between 1H and 96H (One hour through four days).')
                expiration_hours = int(m[1])
                continue
            if arg.lower() == 'unranked':
                is_ranked = False
                continue
            note_args.append(arg)

        if not team_size:
            return await ctx.send(f'Game size is required. Include argument like *1v1* to specify size')

        # if is_ranked and not await settings.is_user(ctx):
        #     return await ctx.send('You can only create *unranked* games until you participate in the server more. Added *unranked* to the end of your command.')

        # if sum(team_sizes) > 2 and (not settings.is_power_user(ctx)) and ctx.guild.id != settings.server_ids['polychampions']:
        #     return await ctx.send('You only have permissions to create 1v1 games. More active server members can create larger games.')

        server_size_max = settings.guild_setting(ctx.guild.id, 'max_team_size')
        if max(team_sizes) > server_size_max:
            if settings.is_mod(ctx):
                await ctx.send('Moderator over-riding server size limits')
            else:
                return await ctx.send(f'Maximum team size on this server is {server_size_max}.\n'
                    'For full functionality with support for up to 6-person teams and team channels check out PolyChampions - <https://tinyurl.com/polychampions>')

        game_notes = ' '.join(note_args)[:100]
        notes_str = game_notes if game_notes else "\u200b"
        expiration_timestamp = (datetime.datetime.now() + datetime.timedelta(hours=expiration_hours)).strftime("%Y-%m-%d %H:%M:%S")

        with models.db.atomic():
            opengame = models.Game.create(host=host, expiration=expiration_timestamp, notes=game_notes, guild_id=ctx.guild.id, is_pending=True, is_ranked=is_ranked)
            for count, size in enumerate(team_sizes):
                models.GameSide.create(game=opengame, size=size, position=count + 1)

            first_side = opengame.first_open_side()
            models.Lineup.create(player=host, game=opengame, gameside=first_side)
        await ctx.send(f'Starting new {"unranked " if not is_ranked else ""}open game ID {opengame.id}. Size: {team_size_str}. Expiration: {expiration_hours} hours.\nNotes: *{notes_str}*\n'
            f'Other players can join this game with `{ctx.prefix}join {opengame.id}`.')

    @commands.command(aliases=['matchside'], usage='match_id side_number Side Name', hidden=True)
    async def gameside(self, ctx, game: PolyMatch, side_lookup: str, *, args):
        """
        Give a name to a side in an open game that you host
        **Example:**
        `[p]gameside m25 2 Ronin` - Names side 2 of Match M25 as 'The Ronin'
        """

        if not game.is_pending:
            return await ctx.send(f'The game has already started and this can no longer be changed.')
        if not game.is_hosted_by(ctx.author.id)[0] and not settings.is_staff(ctx):
            return await ctx.send(f'Only the game host or server staff can do this.')

        # TODO: Have this command also allow side re-ordering
        # matchside m1 1 name ronin
        # matchside m1 ronin nelluk rickdaheals jonathan

        gameside, _ = game.get_side(lookup=side_lookup)
        if not gameside:
            return await ctx.send(f'Can\'t find that side for game {game.id}.')
        gameside.sidename = args
        gameside.save()

        return await ctx.send(f'Side {gameside.position} for game {game.id} has been named "{args}"')

    # @settings.in_bot_channel()
    @commands.command(usage='game_id', aliases=['joingame', 'joinmatch'])
    async def join(self, ctx, game: PolyMatch = None, *args):
        """
        Join an open game
        **Example:**
        `[p]joingame 25` - Join open game 25 to the first side with room
        `[p]joingame 5 ronin` - Join open game 5 to the side named 'ronin'
        `[p]joingame 5 2` - Join open game 5 to side number 2
        `[p]joingame 5 rickdaheals 2` - Add a person to a game you are hosting. Side must be specified.
        """
        if not game:
            return await ctx.send(f'No game ID provided. Use `{ctx.prefix}opengames` to list open games you can join.')
        if not game.is_pending:
            return await ctx.send(f'The game has already started and this can no longer be joined.')

        if len(args) == 0:
            # ctx.author is joining a game, no side given
            target = f'<@{ctx.author.id}>'
            side, side_open = game.first_open_side(), True
            if not side:
                return await ctx.send(f'Game {game.id} is completely full!')

        elif len(args) == 1:
            # ctx.author is joining a match, with a side specified
            target = f'<@{ctx.author.id}>'
            side, side_open = game.get_side(lookup=args[0])
            if not side:
                return await ctx.send(f'Could not find side with "{args[0]}" in game {game.id}. You can use a side number or name if available.')

        elif len(args) == 2:
            # author is putting a third party into this match
            if not settings.is_matchmaking_power_user(ctx):
                return await ctx.send('You do not have permissions to add another person to a game. Tell them to use the command:\n'
                    f'`{ctx.prefix}join {game.id} {args[1]}` to join themselves.')
            target = args[0]
            side, side_open = game.get_side(lookup=args[1])
            if not side:
                return await ctx.send(f'Could not find side with "{args[1]}" in game {game.id}. You can use a side number or name if available.\n'
                    f'Syntax: `{ctx.prefix}join {game.id} <player> <side>`')
        else:
            return await ctx.send(f'Invalid command. See `{ctx.prefix}help joingame` for usage examples.')

        if not side_open:
            return await ctx.send(f'That side of game {game.id} is already full. See `{ctx.prefix}game {game.id}` for details.')

        guild_matches = await utilities.get_guild_member(ctx, target)
        if len(guild_matches) > 1:
            return await ctx.send(f'There is more than one player found with name "{target}". Specify user with @Mention.')
        if len(guild_matches) == 0:
            return await ctx.send(f'Could not find \"{target}\" on this server.')

        if settings.guild_setting(ctx.guild.id, 'require_teams') and not models.Player.is_in_team(guild_id=ctx.guild.id, discord_member=guild_matches[0]):
            return await ctx.send(f'**{guild_matches[0].name}** must join a Team in order to participate in games on this server.')

        player, _ = models.Player.get_by_discord_id(discord_id=guild_matches[0].id, discord_name=guild_matches[0].name, discord_nick=guild_matches[0].nick, guild_id=ctx.guild.id)
        if not player:
            # Matching guild member but no Player or DiscordMember
            return await ctx.send(f'"{guild_matches[0].name}" was found in the server but is not registered with me. '
                f'Players can be register themselves with `{ctx.prefix}setcode POLYTOPIA_CODE`.')

        if game.has_player(player)[0]:
            return await ctx.send(f'**{player.name}** is already in game {game.id}. If you are trying to change sides, use `{ctx.prefix}leave {game.id}` first.')

        if game.is_hosted_by(player.discord_member.discord_id)[0] and side.position != 1:
            return await ctx.send('It looks like you are the host trying to rejoin this game. The host is required to be on side 1. Clear out space in side 1 and use:'
                                 f'\n`{ctx.prefix}join {game.id} 1`')

        logger.info(f'Checks passed. Joining player {player.discord_member.discord_id} to side {side.position} of game {game.id}')
        models.Lineup.create(player=player, game=game, gameside=side)
        await ctx.send(f'Joining <@{player.discord_member.discord_id}> to side {side.position} of game {game.id}')

        players, capacity = game.capacity()
        if players >= capacity:
            creating_player = game.creating_player()
            await ctx.send(f'Game {game.id} is now full and <@{creating_player.discord_member.discord_id}> should create the game in Polytopia.')

            if game.host != creating_player:
                await ctx.send(f'Matchmaking host <@{game.host.discord_member.discord_id}> is not in the game lineup.')

        embed, content = game.embed(ctx)
        await ctx.send(embed=embed, content=content)

    @commands.command(usage='game_id', aliases=['leavegame', 'leavematch'])
    async def leave(self, ctx, game: PolyMatch = None):
        """
        Leave a game that you have joined

        **Example:**
        `[p]leavegame 25`
        """
        if not game:
            return await ctx.send(f'No game ID provided. Use `{ctx.prefix}leave ID` to leave a specific game.')

        if game.is_hosted_by(ctx.author.id)[0]:

            if not settings.is_matchmaking_power_user(ctx):
                return await ctx.send('You do not have permissions to leave your own match.\n'
                    f'If you want to delete use `{ctx.prefix}deletegame {game.id}`')

            await ctx.send(f'**Warning:** You are leaving your own game. You will still be the host. '
                f'If you want to delete use `{ctx.prefix}deletegame {game.id}`')

        if not game.is_pending:
            return await ctx.send(f'Game {game.id} has already started and cannot be left.')

        lineup = game.player(discord_id=ctx.author.id)
        if not lineup:
            return await ctx.send(f'You are not a member of game {game.id}')

        lineup.delete_instance()
        await ctx.send('Removing you from the game.')

    @commands.command(usage='game_id', aliases=['notes', 'matchnotes'])
    async def gamenotes(self, ctx, game: PolyMatch, *, notes: str = None):
        """
        Edit notes for an open game you host
        **Example:**
        `[p]gamenotes 100 Large map, no bans`
        """

        if not game.is_hosted_by(ctx.author.id)[0] and not settings.is_staff(ctx):
            return await ctx.send(f'Only the game host or server staff can do this.')

        old_notes = game.notes
        if game.is_pending:
            game.notes = notes[:100] if notes else None
        else:
            # Preserve original notes and indicate they've been edited, if game is in progress
            old_notes_redacted = f'{"~~" + old_notes.replace("~", "") + "~~"} ' if old_notes else ''
            game.notes = f'{old_notes_redacted}{notes[:100]}' if notes else old_notes_redacted
        game.save()

        await ctx.send(f'Updated notes for game {game.id} to: {game.notes}\nPrevious notes were: {old_notes}')
        embed, content = game.embed(ctx)
        await ctx.send(embed=embed, content=content)

    @commands.command(usage='game_id player')
    async def kick(self, ctx, game: PolyMatch, player: str):
        """
        Kick a player from an open game
        **Example:**
        `[p]kick 25 koric`
        """
        if not game.is_hosted_by(ctx.author.id)[0] and not settings.is_staff(ctx):
            return await ctx.send(f'Only the game host or server staff can do this.')

        if not game.is_pending:
            return await ctx.send(f'Game {game.id} has already started.')

        lineup = game.player(name=player)

        if not lineup:
            return await ctx.send(f'Could not find a match for **{player}** in game {game.id}.')

        if lineup.player.discord_member.discord_id == ctx.author.id:
            return await ctx.send('Stop kicking yourself!')

        await ctx.send(f'Removing **{lineup.player.name}** from the game.')
        lineup.delete_instance()

    # @settings.in_bot_channel()
    @commands.command(aliases=['listmatches', 'matchlist', 'openmatches', 'listmatch', 'matches'])
    async def opengames(self, ctx, *args):
        """
        List current open games

        Full games will still be listed until the host starts or deletes them with `[p]startgame` / `[p]deletegame`

        **Example:**
        `[p]opengames` - List all unexpired games that haven't started yet
        `[p]opengames open` - List all open games that still have openings
        `[p]opengames waiting` - Lists open games that are full but not yet started
        """
        syntax = (f'`{ctx.prefix}opengames` - List all unexpired games that haven\'t started yet\n'
                  f'`{ctx.prefix}opengames open` - List all open games that still have openings\n'
                  f'`{ctx.prefix}opengames waiting` - Lists open games that are full but not yet started')
        models.Game.purge_expired_games()

        if len(args) > 0 and args[0].upper() == 'OPEN':
            title_str = f'Current open games with available spots'
            game_list = models.Game.select().where(
                (models.Game.id.in_(models.Game.subq_open_games_with_capacity())) & (models.Game.is_pending == 1) & (models.Game.guild_id == ctx.guild.id)
            ).order_by(-models.Game.id).prefetch(models.GameSide)

        elif len(args) > 0 and args[0].upper() == 'WAITING':
            title_str = f'Full games waiting to start'
            game_list = models.Game.waiting_to_start(guild_id=ctx.guild.id)
        elif len(args) == 0:
            title_str = f'Current open games'
            game_list = models.Game.select().where(
                (models.Game.is_pending == 1) & (models.Game.guild_id == ctx.guild.id)
            )
        else:
            return await ctx.send(f'Syntax error. Example usage:\n{syntax}')

        title_str_full = title_str + f'\nUse `{ctx.prefix}join #` to join one or `{ctx.prefix}game #` for more details.'
        gamelist_fields = [(f'`{"ID":<8}{"Host":<40} {"Type":<7} {"Capacity":<7} {"Exp":>4}` ', '\u200b')]

        for game in game_list:

            notes_str = game.notes if game.notes else "\u200b"
            players, capacity = game.capacity()
            capacity_str = f' {players}/{capacity}'
            expiration = int((game.expiration - datetime.datetime.now()).total_seconds() / 3600.0)
            expiration = 'Exp' if expiration < 0 else f'{expiration}H'
            ranked = ' ' if game.is_ranked else 'U'

            gamelist_fields.append((f'`{f"{game.id}":<8}{game.host.name:<40} {game.size_string():<7} {capacity_str:<7} {expiration:>5} {ranked}`',
                notes_str))

        self.bot.loop.create_task(utilities.paginate(self.bot, ctx, title=title_str_full, message_list=gamelist_fields, page_start=0, page_end=15, page_size=15))
        # paginator done as a task because otherwise it will not let the waitlist message send until after pagination is complete (20+ seconds)

        waitlist = [f'{g.id}' for g in models.Game.waiting_to_start(guild_id=ctx.guild.id, host_discord_id=ctx.author.id)]
        if ctx.guild.id != settings.server_ids['polychampions']:
            await asyncio.sleep(1)
            await ctx.send('Powered by PolyChampions. League server with a focus on team play:\n'
                '<https://tinyurl.com/polychampions>')
        if waitlist:
            await asyncio.sleep(1)
            await ctx.send(f'You have full games waiting to start: **{", ".join(waitlist)}**\n'
                f'Type `{ctx.prefix}game #` for more details.')

    # @settings.in_bot_channel()
    @commands.command(aliases=['startmatch', 'start'], usage='game_id Name of Poly Game')
    async def startgame(self, ctx, game: PolyMatch, *, name: str = None):
        """
        Start a full game and track it for ELO
        Use this command after you have created the game in Polytopia.
        **Example:**
        `[p]startgame 100 Fields of Fire`
        """

        if not game.is_hosted_by(ctx.author.id)[0] and not settings.is_staff(ctx):
            return await ctx.send(f'Only the match host or server staff can do this.')

        if not name:
            return await ctx.send(f'Game name is required. Example: `{ctx.prefix}startgame {game.id} Name of Game`')

        if not game.is_pending:
            return await ctx.send(f'Game {game.id} has already started with name **{game.name}**')

        players, capacity = game.capacity()
        if players != capacity:
            return await ctx.send(f'Game {game.id} is not full.\nCapacity {players}/{capacity}.')

        sides, mentions = [], []

        for side in game.gamesides:
            # TODO: This won't necessarily respect side ordering
            current_side = []
            for gameplayer in side.lineup:
                guild_member = ctx.guild.get_member(gameplayer.player.discord_member.discord_id)
                if not guild_member:
                    return await ctx.send(f'Player *{gameplayer.player.name}* not found on this server. (Maybe they left?)')
                current_side.append(guild_member)
                mentions.append(guild_member.mention)
            sides.append(current_side)

        teams_for_each_discord_member, list_of_final_teams = models.Game.pregame_check(discord_groups=sides,
                                                                guild_id=ctx.guild.id,
                                                                require_teams=settings.guild_setting(ctx.guild.id, 'require_teams'))

        with models.db.atomic():
            # Convert game from pending matchmaking session to in-progress game
            for team_group, allied_team, side in zip(teams_for_each_discord_member, list_of_final_teams, game.gamesides):
                side_players = []
                for team, lineup in zip(team_group, side.lineup):
                    lineup.team = team
                    lineup.save()
                    side_players.append(lineup.player)

                if len(side_players) > 1:
                    squad = models.Squad.upsert(player_list=side_players, guild_id=ctx.guild.id)
                    side.squad = squad

                side.team = allied_team
                side.save()

            game.name = name
            game.is_pending = False
            game.save()

        logger.info(f'Game {game.id} closed and being tracked for ELO')
        await post_newgame_messaging(ctx, game=game)

    async def task_print_matchlist(self):

        await self.bot.wait_until_ready()
        challenge_channels = [g.get_channel(settings.guild_setting(g.id, 'match_challenge_channel')) for g in self.bot.guilds]
        while not self.bot.is_closed():
            await asyncio.sleep(60 * 60 * 2)  # delay before and after loop so bot wont spam if its being restarted several times
            for chan in challenge_channels:
                if not chan:
                    continue

                models.Game.purge_expired_games()
                game_list = models.Game.select().where(
                    (models.Game.id.in_(models.Game.subq_open_games_with_capacity())) & (models.Game.is_pending == 1) & (models.Game.guild_id == chan.guild.id)
                ).order_by(-models.Game.id).prefetch(models.GameSide)[:12]
                if not game_list:
                    continue

                pfx = settings.guild_setting(chan.guild.id, 'command_prefix')
                embed = discord.Embed(title='Recent open games\n'
                    f'Use `{pfx}join #` to join one or `{pfx}game #` for more details.')
                embed.add_field(name=f'`{"ID":<8}{"Host":<40} {"Type":<7} {"Capacity":<7} {"Exp":>4} `', value='\u200b', inline=False)
                for game in game_list:

                    notes_str = game.notes if game.notes else "\u200b"
                    players, capacity = game.capacity()
                    capacity_str = f' {players}/{capacity}'
                    expiration = int((game.expiration - datetime.datetime.now()).total_seconds() / 3600.0)
                    expiration = 'Exp' if expiration < 0 else f'{expiration}H'
                    ranked = ' ' if game.is_ranked else 'U'

                    embed.add_field(name=f'`{game.id:<8}{game.host.name:<40} {game.size_string():<7} {capacity_str:<7} {expiration:>5} {ranked}`', value=notes_str)

                await chan.send(embed=embed)

            await asyncio.sleep(60 * 60 * 2)


def setup(bot):
    bot.add_cog(matchmaking(bot))
