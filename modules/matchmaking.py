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

        match_id = match_id.strip('#')

        try:
            match = models.Game.get(id=match_id)
            logger.debug(f'Game with ID {match_id} found.')

            if match.guild_id != ctx.guild.id:
                await ctx.send(f'Game with ID {match_id} is associated with a different Discord server. Use `{ctx.prefix}opengames` to see available matches.')
                raise commands.UserInputError()
            return match
        except peewee.DoesNotExist:
            await ctx.send(f'Game with ID {match_id} cannot be found. Use `{ctx.prefix}opengames` to see available matches.')
            raise commands.UserInputError()
        except ValueError:
            if match_id.upper() == 'ID':
                await ctx.send(f'Invalid Game ID "**{match_id}**". Use the numeric game ID *only*.')
            else:
                await ctx.send(f'Invalid Game ID "**{match_id}**".')
            raise commands.UserInputError()


class matchmaking():
    """
    Host open and find open games.
    """

    def __init__(self, bot):
        self.bot = bot
        self.bg_task = bot.loop.create_task(self.task_print_matchlist())
        self.bg_task2 = bot.loop.create_task(self.task_dm_game_creators())
        self.bg_task3 = bot.loop.create_task(self.task_create_empty_matchmaking_lobbies())

    @settings.in_bot_channel()
    @commands.command(aliases=['openmatch', 'open'], usage='size expiration rules')
    async def opengame(self, ctx, *, args=None):

        """
        Opens a game that others can join
        Expiration can be between 1H - 96H
        Size examples: 1v1, 2v2, 1v1v1v1v1, 3v3v3, 1v3

        **Examples:**
        `[p]opengame 1v1`

        `[p]opengame 1v1 48h`  (Expires in 48 hours)

        `[p]opengame 1v1 unranked`  (Add word *unranked* to have game not count for ELO)

        `[p]opengame 2v2 Large map, no bardur`  (Adds a note to the game)

        `[p]opengame 1v1 Large map 1200 elo min`
        (Add an ELO requirement for joining with `max` or `min`. Also `1200 global elo max` to check global elo.)

        `[p]opengame 1v1 For @Nelluk only`
        (Include one or more @Mentions in notes and only those people will be permitted to join.)

        `[p]opengame 2v2 for @The Ronin vs @The Jets`
        (Include one or more @Roles and the games sides will be locked to that specific role. For use with PolyChampions teams.)
        """

        team_size, is_ranked = False, True
        required_role_args = []
        expiration_hours_override = None
        note_args = []

        if not args:
            return await ctx.send('Game size is required. Include argument like *1v1v1* to specify size.'
                f'\nExample: `{ctx.prefix}opengame 1v1 large map`')

        host, _ = models.Player.get_by_discord_id(discord_id=ctx.author.id, discord_name=ctx.author.name, discord_nick=ctx.author.nick, guild_id=ctx.guild.id)
        if not host:
            # Matching guild member but no Player or DiscordMember
            return await ctx.send(f'You must be a registered player before hosting a match. Try `{ctx.prefix}setcode POLYCODE`')

        on_team, player_team = models.Player.is_in_team(guild_id=ctx.guild.id, discord_member=ctx.author)
        if settings.guild_setting(ctx.guild.id, 'require_teams') and not on_team:
            return await ctx.send(f'You must join a Team in order to participate in games on this server.')

        max_open = max(1, settings.get_user_level(ctx) * 2)
        if models.Game.select().where((models.Game.host == host) & (models.Game.is_pending == 1)).count() > max_open:
            return await ctx.send(f'You have too many open games already (max of {max_open}). Try using `{ctx.prefix}delete` on an existing one.')

        if settings.guild_setting(ctx.guild.id, 'unranked_game_channel') and ctx.channel.id == settings.guild_setting(ctx.guild.id, 'unranked_game_channel'):
            is_ranked = False

        for arg in args.split(' '):
            m = re.fullmatch(r"\d+(?:(v|vs)\d+)+", arg.lower())
            if m:
                # arg looks like '3v3' or '1v1v1'
                team_size_str = m[0]
                team_sizes = [int(x) for x in arg.lower().split(m[1])]  # split on 'vs' or 'v'; whichever the regexp detects
                if min(team_sizes) < 1:
                    return await ctx.send(f'Invalid game size **{team_size_str}**: Each side must have at least 1 player.')
                if sum(team_sizes) > 12:
                    return await ctx.send(f'Invalid game size **{team_size_str}**: Games can have a maximum of 12 players.')
                team_size = True
                continue
            m = re.match(r"(\d+)h", arg.lower())
            if m:
                # arg looks like '12h'
                if not 0 < int(m[1]) < 97:
                    return await ctx.send(f'Invalid expiration {arg}. Must be between 1H and 96H (One hour through four days).')
                expiration_hours_override = int(m[1])
                continue
            if arg.lower() == 'unranked':
                is_ranked = False
                continue
            m = re.match(r"<@&(\d+)>", arg)
            if m:
                # replace raw role tag <@&....> with name of role, so people dont get mentioned every time note is printed
                # also extracting roles from raw args instead of iterating over ctx.message.roles since that ordering is not reliable
                extracted_role = discord.utils.get(ctx.guild.roles, id=int(m[1]))
                if extracted_role:
                    note_args.append('**@' + extracted_role.name + '**')
                    required_role_args.append(extracted_role)
                else:
                    logger.warn(f'Detected role-like string {m[0]} in arguments but cannot match to an actual role. Skipping.')
                continue
            note_args.append(arg)

        if not team_size:
            return await ctx.send(f'Game size is required. Include argument like *1v1* to specify size')

        if settings.get_user_level(ctx) <= 1 and (is_ranked or sum(team_sizes) > 3):
            return await ctx.send(f'You can only host unranked games with a maximum of 3 players.\n{settings.levels_info}')

        if settings.get_user_level(ctx) <= 2:
            if sum(team_sizes) > 4 and is_ranked:
                return await ctx.send(f'You can only host ranked games of up to 4 players. More active players have permissons to host large games.\n{settings.levels_info}')
            if sum(team_sizes) > 6:
                return await ctx.send(f'You can only host unranked games of up to 6 players. More active players have permissons to host large games.\n{settings.levels_info}')

        if not settings.guild_setting(ctx.guild.id, 'allow_uneven_teams') and not all(x == team_sizes[0] for x in team_sizes):
            return await ctx.send('Uneven team games are not allowed on this server.')

        server_size_max = settings.guild_setting(ctx.guild.id, 'max_team_size')
        if max(team_sizes) > server_size_max:
            if settings.guild_setting(ctx.guild.id, 'allow_uneven_teams') and min(team_sizes) <= server_size_max:
                await ctx.send('**Warning:** Team sizes are uneven.')
            elif settings.is_mod(ctx):
                await ctx.send('Moderator over-riding server size limits')
            elif not is_ranked and max(team_sizes) <= server_size_max + 1:
                # Arbitrary rule, unranked games can go +1 from server_size_max
                logger.info('Opening unranked game that exceeds server_size_max')
            else:
                return await ctx.send(f'Maximum team size on this server is {server_size_max}.\n'
                    'For full functionality with support for up to 6-person teams and team channels check out PolyChampions - <https://tinyurl.com/polychampions>')

        required_roles = [None] * len(team_sizes)  # [None, None, None] for a 3-sided game
        required_role_names = [None] * len(team_sizes)
        required_role_message = ''

        if required_role_args and len(required_role_args) < len(team_sizes) and required_role_args[0] not in ctx.author.roles:
            # used for a case like: $opengame 1v1 me vs @The Novas   -- puts that role on side 2 if you dont have it
            logger.debug(f'Offsetting required_role_args')
            required_role_args.insert(0, None)

        for count, role in enumerate(required_role_args):
            if count >= len(team_sizes):
                break
            if not role:
                continue
            required_roles[count] = role.id
            required_role_names[count] = role.name
            required_role_message += f'**Side {count + 1}** will be locked to players with role *{role.name}*\n'

        if required_role_message:
            await ctx.send(required_role_message)

        game_notes = ' '.join(note_args)[:150].strip()
        notes_str = game_notes if game_notes else "\u200b"
        if expiration_hours_override:
            expiration_hours = expiration_hours_override
        else:
            if sum(team_sizes) < 4:
                expiration_hours = 24
            elif sum(team_sizes) < 6:
                expiration_hours = 48
            else:
                expiration_hours = 96
        expiration_timestamp = (datetime.datetime.now() + datetime.timedelta(hours=expiration_hours)).strftime("%Y-%m-%d %H:%M:%S")

        with models.db.atomic() as transaction:
            warning_message, fatal_warning = '', False
            host.team = player_team
            host.save()

            opengame = models.Game.create(host=host, expiration=expiration_timestamp, notes=game_notes, guild_id=ctx.guild.id, is_pending=True, is_ranked=is_ranked)
            for count, size in enumerate(team_sizes):
                models.GameSide.create(game=opengame, size=size, position=count + 1, required_role_id=required_roles[count], sidename=required_role_names[count])

            first_side = opengame.first_open_side(roles=[role.id for role in ctx.author.roles])
            if not first_side:
                if settings.get_user_level(ctx) >= 4:
                    warning_message = f'**Warning:** All sides in this game are locked to a specific @Role - and you don\'t have any of those roles. You are not a player in this game.'
                    fatal_warning = False
                else:
                    transaction.rollback()
                    warning_message = f'**Warning:** All sides in this game are locked to a specific @Role - and you don\'t have any of those roles. Game not created.'
                    fatal_warning = True
            else:
                models.Lineup.create(player=host, game=opengame, gameside=first_side)
                if first_side.position > 1:
                    warning_message = '**Warning:** You are not joined to side 1, due to the ordering of the role restrictions. Therefore you will not be the game host.'

        if warning_message and fatal_warning:
            # putting warning_message here because if they are await+sent inside the transaction block errors can occasionally occur - happens when async code is inside the transaction block
            return await ctx.send(warning_message)
        if warning_message:
            await ctx.send(warning_message)

        await ctx.send(f'Starting new {"unranked " if not is_ranked else ""}open game ID {opengame.id}. Size: {team_size_str}. Expiration: {expiration_hours} hours.\nNotes: *{notes_str}*\n'
            f'Other players can join this game with `{ctx.prefix}join {opengame.id}`.')

    @settings.in_bot_channel()
    @commands.command(aliases=['matchside', 'sidename'], usage='match_id side_number Side Name', hidden=True)
    async def gameside(self, ctx, game: PolyMatch, side_lookup: str, *, args=None):
        """
        Give a name to a side in an open game that you host
        **Example:**
        `[p]gameside 1025 2 Cool Team` - Names side 2 of Match 1025 as '*Cool Team*'
        `[p]gameside 1025 2 @The Ronin` - Locks side 2 to people with role `@The Ronin` and names side correspondingly
        `[p]gameside 1025 2 none` - Resets side to have no name or role locks
        """

        if not game.is_pending:
            return await ctx.send(f'The game has already started and can no longer be changed.')
        if not game.is_hosted_by(ctx.author.id)[0] and not settings.is_staff(ctx):
            return await ctx.send(f'Only the game host or server staff can do this.')

        # TODO: Have this command also allow side re-ordering
        # matchside m1 1 name ronin
        # matchside m1 ronin nelluk rickdaheals jonathan

        gameside, _ = game.get_side(lookup=side_lookup)
        if not gameside:
            return await ctx.send(f'Can\'t find that side for game {game.id}.')

        if args and args.lower() == 'none':
            args = None

        if len(ctx.message.role_mentions) == 1:
            # using a role to lock side
            gameside.required_role_id = ctx.message.role_mentions[0].id
            gameside.sidename = ctx.message.role_mentions[0].name
            msg = f'Side {gameside.position} for game {game.id} has been locked to role **@{gameside.sidename}** and named **{gameside.sidename}**'
        else:
            gameside.sidename = args
            gameside.required_role_id = None
            msg = f'Side {gameside.position} for game {game.id} has been named **{args}**'
        gameside.save()

        return await ctx.send(msg)

    @settings.in_bot_channel()
    @commands.command(usage='game_id', aliases=['joingame', 'joinmatch'])
    async def join(self, ctx, game: PolyMatch = None, *args):
        """
        Join an open game
        **Example:**
        `[p]join 1025` - Join open game 1025 to the first side with room
        `[p]join 1025 2` - Join open game 1025 to side number 2
        `[p]join 1025 rickdaheals 2` - Add a person to a game you are hosting. Side must be specified.
        """
        syntax = f'**Example usage**:\n__`{ctx.prefix}join 1025`__ - Join game 1025\n__`{ctx.prefix}join 1025 2`__ - Join game 1025, side 2'

        if settings.get_user_level(ctx) >= 4:
            syntax += f'\n__`{ctx.prefix}join 1025 Nelluk 2`__ - Add a third party to side 2 of your open game. Side must be specified.'

        if not game:
            return await ctx.send(f'No game ID provided. Use `{ctx.prefix}opengames` to list open games you can join.\n{syntax}')
        if not game.is_pending:
            return await ctx.send(f'The game has already started and can no longer be joined.')

        if len(args) == 0:
            # ctx.author is joining a game, no side given
            target = f'<@{ctx.author.id}>'
            side, side_open = game.first_open_side(roles=[role.id for role in ctx.author.roles]), True
            if not side:
                players, capacity = game.capacity()
                if players < capacity:
                    return await ctx.send(f'Game {game.id} is limited to specific roles. You are not allowed to join. See game notes for details: `{ctx.prefix}game {game.id}`')
                return await ctx.send(f'Game {game.id} is completely full!')

        elif len(args) == 1:
            # ctx.author is joining a match, with a side specified
            target = f'<@{ctx.author.id}>'
            side, side_open = game.get_side(lookup=args[0])
            if not side:
                return await ctx.send(f'Could not find side with "{args[0]}" in game {game.id}. You can use a side number or name if available.\n{syntax}')

        elif len(args) == 2:
            # author is putting a third party into this match
            if settings.get_user_level(ctx) < 4:
                return await ctx.send('You do not have permissions to add another person to a game. Tell them to use the command:\n'
                    f'`{ctx.prefix}join {game.id} {args[1]}` to join themselves.')
            target = args[0]
            side, side_open = game.get_side(lookup=args[1])
            if not side:
                return await ctx.send(f'Could not find side with "{args[1]}" in game {game.id}. You can use a side number or name if available.\n{syntax}')
        else:
            return await ctx.send(f'Invalid usage.\n{syntax}')

        if not side_open:
            return await ctx.send(f'That side of game {game.id} is already full. See `{ctx.prefix}game {game.id}` for details.')

        guild_matches = await utilities.get_guild_member(ctx, target)
        if len(guild_matches) > 1:
            return await ctx.send(f'There is more than one player found with name "{target}". Specify user with @Mention.')
        if len(guild_matches) == 0:
            return await ctx.send(f'Could not find \"{target}\" on this server.')

        on_team, player_team = models.Player.is_in_team(guild_id=ctx.guild.id, discord_member=guild_matches[0])
        if settings.guild_setting(ctx.guild.id, 'require_teams') and not on_team:
            return await ctx.send(f'**{guild_matches[0].name}** must join a Team in order to participate in games on this server.')

        if side.required_role_id and not discord.utils.get(guild_matches[0].roles, id=side.required_role_id):
            if settings.get_user_level(ctx) >= 5:
                await ctx.send(f'Side {side.position} of game {game.id} is limited to players with the **@{side.sidename}** role. *Overriding restriction due to staff privileges.*')
            else:
                return await ctx.send(f'Side {side.position} of game {game.id} is limited to players with the **@{side.sidename}** role. You are not allowed to join.')

        player, _ = models.Player.get_by_discord_id(discord_id=guild_matches[0].id, discord_name=guild_matches[0].name, discord_nick=guild_matches[0].nick, guild_id=ctx.guild.id)
        if not player:
            # Matching guild member but no Player or DiscordMember
            return await ctx.send(f'*{guild_matches[0].name}* was found in the server but is not registered with me. '
                f'Players can be register themselves with `{ctx.prefix}setcode POLYTOPIA_CODE`.')

        if not player.discord_member.polytopia_id:
            return await ctx.send(f'**{player.name}** does not have a Polytopia game code on file. Use `{ctx.prefix}setcode` to set one.')

        if player.is_banned or player.discord_member.is_banned:
            if settings.is_mod(ctx):
                await ctx.send(f'**{player.name}** has been **ELO Banned** -- *moderator over-ride* :thinking:')
            else:
                return await ctx.send(f'**{player.name}** has been **ELO Banned** and cannot join any new games. :cry:')

        if game.has_player(player)[0]:
            return await ctx.send(f'**{player.name}** is already in game {game.id}. If you are trying to change sides, use `{ctx.prefix}leave {game.id}` first.')

        if game.is_hosted_by(player.discord_member.discord_id)[0] and side.position != 1:
            await ctx.send('**Warning:** Since you are not joining side 1 you will not be the game creator.')

        _, game_size = game.capacity()
        if settings.get_user_level(ctx) <= 1:
            if (game.is_ranked and game_size) > 3 or (not game.is_ranked and game_size > 6):
                return await ctx.send(f'You are a restricted user (*level 1*) - complete a few more ELO games to have more permissions.\n{settings.levels_info}')
        elif settings.get_user_level(ctx) <= 2:
            if (game.is_ranked and game_size) > 6 or (not game.is_ranked and game_size > 12):
                return await ctx.send(f'You are a restricted user (*level 2*) - complete a few more ELO games to have more permissions.\n{settings.levels_info}')

        min_elo, max_elo = 0, 3000
        min_elo_g, max_elo_g = 0, 3000
        notes = game.notes if game.notes else ''

        m = re.search(r'(\d+) elo max', notes, re.I)
        if m:
            max_elo = int(m[1])
        m = re.search(r'(\d+) elo min', notes, re.I)
        if m:
            min_elo = int(m[1])

        m = re.search(r'(\d+) global elo max', notes, re.I)
        if m:
            max_elo_g = int(m[1])
        m = re.search(r'(\d+) global elo min', notes, re.I)
        if m:
            min_elo_g = int(m[1])

        if player.elo < min_elo or player.elo > max_elo:
            if not game.is_hosted_by(ctx.author.id)[0] and not settings.is_mod(ctx):
                return await ctx.send(f'This game has an ELO restriction of {min_elo} - {max_elo} and **{player.name}** has an ELO of **{player.elo}**. Cannot join! :cry:')
            await ctx.send(f'This game has an ELO restriction of {min_elo} - {max_elo}. Bypassing because you are game host or a mod.')

        if player.discord_member.elo < min_elo_g or player.discord_member.elo > max_elo_g:
            if not game.is_hosted_by(ctx.author.id)[0] and not settings.is_mod(ctx):
                return await ctx.send(f'This game has a global ELO restriction of {min_elo_g} - {max_elo_g} and **{player.name}** has a global ELO of **{player.discord_member.elo}**. Cannot join! :cry:')
            await ctx.send(f'This game has an ELO restriction of {min_elo_g} - {max_elo_g}. Bypassing because you are game host or a mod.')

        # list of ID strings that are allowed to join game, e.g. ['272510639124250625', '481527584107003904']
        player_restricted_list = re.findall(r'<@!?(\d+)>', notes)

        if player_restricted_list and str(player.discord_member.discord_id) not in player_restricted_list and (len(player_restricted_list) >= game_size - 1):
            # checking length of player_restricted_list compared to game capacity.. only using restriction if capacity is at least game_size - 1
            # if its game_size - 1, assuming that the host is the 'other' person
            # this isnt really ideal.. could have some games where the restriction should be honored but people are allowed to join.. but better than making the lock too restrictive
            return await ctx.send(f'Game {game.id} is limited to specific players. You are not allowed to join. See game notes for details: `{ctx.prefix}game {game.id}`')

        logger.info(f'Checks passed. Joining player {player.discord_member.discord_id} to side {side.position} of game {game.id}')

        with models.db.atomic():
            models.Lineup.create(player=player, game=game, gameside=side)
            player.team = player_team  # update player record with detected team in case its changed since last game.
            logger.debug(f'Associating team {player_team} with player {player.id} {player.name}')
            player.save()
        await ctx.send(f'Joining <@{player.discord_member.discord_id}> to side {side.position} of game {game.id}')

        players, capacity = game.capacity()
        if players >= capacity:
            creating_player = game.creating_player()
            await ctx.send(f'Game {game.id} is now full and <@{creating_player.discord_member.discord_id}> should create the game in Polytopia.')

            if game.host and game.host != creating_player:
                await ctx.send(f'Matchmaking host <@{game.host.discord_member.discord_id}> is not the game creator.')

        embed, content = game.embed(guild=ctx.guild, prefix=ctx.prefix)
        await ctx.send(embed=embed, content=content)

    @settings.in_bot_channel()
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

            if settings.get_user_level(ctx) < 4:
                return await ctx.send('You do not have permissions to leave your own match.\n'
                    f'If you want to delete use `{ctx.prefix}delete {game.id}`')

            await ctx.send(f'**Warning:** You are leaving your own game. You will still be the host. '
                f'If you want to delete use `{ctx.prefix}delete {game.id}`')

        if not game.is_pending:
            return await ctx.send(f'Game {game.id} has already started and cannot be left.')

        lineup = game.player(discord_id=ctx.author.id)
        if not lineup:
            return await ctx.send(f'You are not a member of game {game.id}')

        lineup.delete_instance()
        await ctx.send('Removing you from the game.')

    @settings.in_bot_channel()
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
            game.notes = notes[:150] if notes else None
        else:
            # Preserve original notes and indicate they've been edited, if game is in progress
            old_notes_redacted = f'{"~~" + old_notes.replace("~", "") + "~~"} ' if old_notes else ''
            game.notes = f'{old_notes_redacted}{notes[:150]}' if notes else old_notes_redacted
        game.save()

        await ctx.send(f'Updated notes for game {game.id} to: {game.notes}\nPrevious notes were: {old_notes}')
        embed, content = game.embed(guild=ctx.guild, prefix=ctx.prefix)
        await ctx.send(embed=embed, content=content)

    @settings.in_bot_channel()
    @commands.command(usage='game_id player')
    async def kick(self, ctx, game: PolyMatch, player: str):
        """
        Kick a player from an open game
        **Example:**
        `[p]kick 25 koric`
        """
        is_hosted_by, host = game.is_hosted_by(ctx.author.id)
        if not is_hosted_by and not settings.is_staff(ctx):
            host_name = f' **{host.name}**' if host else ''
            return await ctx.send(f'Only the game host{host_name} or server staff can do this.')

        if not game.is_pending:
            return await ctx.send(f'Game {game.id} has already started.')

        lineup = game.player(name=player)

        if not lineup:
            return await ctx.send(f'Could not find a match for **{player}** in game {game.id}.')

        if lineup.player.discord_member.discord_id == ctx.author.id:
            return await ctx.send('Stop kicking yourself!')

        await ctx.send(f'Removing **{lineup.player.name}** from the game.')
        lineup.delete_instance()

        if game.expiration < (datetime.datetime.now() + datetime.timedelta(hours=2)):
            # This catches the case of kicking someone from a full game, so that the game wont immediately get purged due to not being full
            game.expiration = (datetime.datetime.now() + datetime.timedelta(hours=24))
            game.save()
            await ctx.send(f'Game {game.id} expiration has been reset to 24 hours from now')

    @settings.in_bot_channel()
    @commands.command(aliases=['games', 'listmatches', 'matchlist', 'openmatches', 'listmatch', 'matches'])
    async def opengames(self, ctx, *args):
        """
        List current open games

        Full games will still be listed until the host starts or deletes them with `[p]startgame` / `[p]deletegame`

        **Example:**
        `[p]opengames` - List all open games that are not yet full
        `[p]opengames waiting` - Lists open games that are full but not yet started
        `[p]opengames all` - List all pending open games, including full
        `[p]opengames me` - List unstarted opengames that you have joined
        You can also add keywords **ranked** or **unranked** to filter by those types of games.
        """
        models.Game.purge_expired_games()

        ranked_filter, ranked_str = 2, ''
        ranked_chan = settings.guild_setting(ctx.guild.id, 'ranked_game_channel')
        unranked_chan = settings.guild_setting(ctx.guild.id, 'unranked_game_channel')

        if ctx.channel.id == unranked_chan or any(arg.upper() == 'UNRANKED' for arg in args):
            ranked_filter = 0
            ranked_str = ' **unranked**'
        elif ctx.channel.id == ranked_chan or any(arg.upper() == 'RANKED' for arg in args):
            ranked_filter = 1
            ranked_str = ' **ranked**'

        if len(args) > 0 and args[0].upper() == 'ALL':
            title_str = f'All{ranked_str} open games'
            game_list = models.Game.search_pending(status_filter=0, guild_id=ctx.guild.id, ranked_filter=ranked_filter)

        elif len(args) > 0 and args[0].upper() == 'WAITING':
            title_str = f'Open{ranked_str} games waiting to start'
            game_list = models.Game.search_pending(status_filter=1, guild_id=ctx.guild.id, ranked_filter=ranked_filter)

        elif len(args) > 0 and args[0].upper() == 'ME':
            title_str = f'Open games joined by **{ctx.author.name}**'
            game_list = models.Game.search_pending(guild_id=ctx.guild.id, player_discord_id=ctx.author.id)

        else:
            title_str = f'Current{ranked_str} open games with available spots'
            game_list = models.Game.search_pending(status_filter=2, guild_id=ctx.guild.id, ranked_filter=ranked_filter)

        title_str_full = title_str + f'\nUse __`{ctx.prefix}join ID`__ to join one or __`{ctx.prefix}game ID`__ for more details.'
        gamelist_fields = [(f'`{"ID":<8}{"Host":<40} {"Type":<7} {"Capacity":<7} {"Exp":>4}` ', '\u200b')]

        for game in game_list:

            notes_str = game.notes if game.notes else '\u200b'
            players, capacity = game.capacity()
            player_restricted_list = re.findall(r'<@!?(\d+)>', notes_str)

            if player_restricted_list and str(ctx.author.id) not in player_restricted_list and (len(player_restricted_list) >= capacity - 1) and not game.is_hosted_by(ctx.author.id)[0]:
                # skipping games that the command issuer is not invited to
                continue

            capacity_str = f' {players}/{capacity}'
            expiration = int((game.expiration - datetime.datetime.now()).total_seconds() / 3600.0)
            expiration = 'Exp' if expiration < 0 else f'{expiration}H'
            ranked_str = '*Unranked*' if not game.is_ranked else ''
            ranked_str = ranked_str + ' - ' if game.notes and ranked_str else ranked_str
            creating_player = game.creating_player()
            host_name = creating_player.name[:35] if creating_player else '<Vacant>'
            gamelist_fields.append((f'`{f"{game.id}":<8}{host_name:<40} {game.size_string():<7} {capacity_str:<7} {expiration:>5}`',
                f'{ranked_str}{notes_str}\n \u200b'))

        self.bot.loop.create_task(utilities.paginate(self.bot, ctx, title=title_str_full, message_list=gamelist_fields, page_start=0, page_end=15, page_size=15))
        # paginator done as a task because otherwise it will not let the waitlist message send until after pagination is complete (20+ seconds)

        if ctx.guild.id != settings.server_ids['polychampions']:
            await asyncio.sleep(1)
            await ctx.send('Powered by PolyChampions. League server with a focus on team play:\n'
                '<https://tinyurl.com/polychampions>')

        # Alert user if a game they are hosting OR should be creating is waiting to be created
        waitlist_hosting = [f'{g.id}' for g in models.Game.search_pending(status_filter=1, guild_id=ctx.guild.id, host_discord_id=ctx.author.id)]
        waitlist_creating = [f'{g.game}' for g in models.Game.waiting_for_creator(creator_discord_id=ctx.author.id)]
        waitlist = set(waitlist_hosting + waitlist_creating)

        if waitlist:
            await asyncio.sleep(1)
            await ctx.send(f'{ctx.author.mention}, you have full games waiting to start: **{", ".join(waitlist)}**\n'
                f'Type __`{ctx.prefix}game IDNUM`__ for more details, ie `{ctx.prefix}game {(waitlist_hosting + waitlist_creating)[0]}`')

    @settings.in_bot_channel()
    @commands.command(aliases=['startmatch', 'start'], usage='game_id Name of Poly Game')
    async def startgame(self, ctx, game: PolyMatch = None, *, name: str = None):
        """
        Start a full game and track it for ELO
        Use this command after you have created the game in Polytopia.
        **Example:**
        `[p]startgame 100 Fields of Fire`
        """

        syntax = (f'**Example usage**:\n__`{ctx.prefix}start 1025 Name of Game`__')

        if not game:
            return await ctx.send(f'No game ID provided. Use `{ctx.prefix}opengames me` to list open games you have waiting to start.\n{syntax}')

        is_hosted_by, host = game.is_hosted_by(ctx.author.id)
        if not is_hosted_by and not settings.is_staff(ctx) and not game.is_created_by(ctx.author.id):
            creating_player = game.creating_player()
            if creating_player and host:
                if host != creating_player:
                    return await ctx.send(f'Only the game host **{host.name}**, creating player **{creating_player.name}**, or server staff can do this.')
                else:
                    return await ctx.send(f'Only the game host **{host.name}** or server staff can do this.')
            elif creating_player:
                return await ctx.send(f'Only the creating player **{creating_player.name}**, or server staff can do this.')
            elif host:
                return await ctx.send(f'Only the game host **{host.name}** or server staff can do this.')
            else:
                return await ctx.send(f'Only the game host or server staff can do this.')

        if not name:
            return await ctx.send(f'Game name is required. The game must be created **in Polytopia** first to get the correct name.\n{syntax}')

        if not utilities.is_valid_poly_gamename(input=name):
            return await ctx.send('That name looks made up. :thinking: You need to manually create the game __in Polytopia__, come back and input the name of the new game you made.\n'
                f'You can use `{ctx.prefix}codes {game.id}` to get the code of each player in this game in an easy-to-copy format.')

        if not game.is_pending:
            return await ctx.send(f'Game {game.id} has already started with name **{game.name}**')

        players, capacity = game.capacity()
        if players != capacity:
            return await ctx.send(f'Game {game.id} is not full.\nCapacity {players}/{capacity}.')

        sides, mentions = [], []

        for side in game.ordered_side_list():
            current_side = []
            for gameplayer in side.ordered_player_list():
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
            for team_group, allied_team, side in zip(teams_for_each_discord_member, list_of_final_teams, game.ordered_side_list()):
                side_players = []
                for team, lineup in zip(team_group, side.ordered_player_list()):
                    logger.debug(f'setting player {lineup.player.id} {lineup.player.name} to team {team}')
                    lineup.player.team = team
                    lineup.player.save()
                    side_players.append(lineup.player)

                if len(side_players) > 1:
                    squad = models.Squad.upsert(player_list=side_players, guild_id=ctx.guild.id)
                    side.squad = squad

                side.team = allied_team
                side.save()

            game.name = name
            game.date = datetime.datetime.today()
            game.is_pending = False
            game.save()

        logger.info(f'Game {game.id} closed and being tracked for ELO')
        await post_newgame_messaging(ctx, game=game)

    async def task_dm_game_creators(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            await asyncio.sleep(60 * 60 * 10)
            logger.debug('Task running: task_dm_game_creators')
            full_games = models.Game.search_pending(status_filter=1, ranked_filter=1)
            logger.debug(f'Starting task_dm_game_creators on {len(full_games)} games')
            for game in full_games:
                guild = discord.utils.get(self.bot.guilds, id=game.guild_id)
                creating_player = game.creating_player()

                if not guild:
                    logger.error(f'Couldnt load guild ID {game.guild_id}')
                    continue

                creating_guild_member = guild.get_member(creating_player.discord_member.discord_id)
                if not creating_guild_member:
                    logger.warn(f'Couldnt load creator for game {game.id} in server {guild.name}. Maybe they left the server?')
                    continue

                bot_channel = settings.guild_setting(guild.id, 'bot_channels_strict')[0]
                prefix = settings.guild_setting(guild.id, 'command_prefix')

                message = (f'__You have a ranked game on **{guild.name}** that is waiting to be created.__'
                           f'\nPlease visit the server\'s bot channel at this link: <https://discordapp.com/channels/{guild.id}/{bot_channel}/>'
                           f'\nType the command __`{prefix}game {game.id}`__ for more details. Remember. you must manually **create the game within Polytopia** using the supplied '
                           f'friend codes, come back to the channel, and use the command __`{prefix}game {game.id} Name of Game`__ to mark the game as started.'
                           f'\n\nYou can use the command __`{prefix}codes {game.id}`__ to get each player\'s friend code in an easy-to-copy format.')

                try:
                    await creating_guild_member.send(message)
                    await creating_guild_member.send('I do not respond to DMed commands. You must issue commands in the channel linked above.')
                    logger.info(f'Sending reminder DM to {creating_guild_member.name} {creating_guild_member.id} to start game {game.id}')
                except discord.DiscordException as e:
                    logger.warn(f'Error DMing creator of waiting game: {e}')

    async def task_create_empty_matchmaking_lobbies(self):
        # Keep open games list populated with vacant lobbies as specified in settings.lobbies

        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            await asyncio.sleep(60)
            logger.debug('Task running: task_create_empty_matchmaking_lobbies')
            unhosted_game_list = models.Game.search_pending(status_filter=2, host_discord_id=0)
            for lobby in settings.lobbies:
                matching_lobby = False
                for g in unhosted_game_list:
                    if (g.guild_id == lobby['guild'] and g.size_string() == lobby['size_str'] and
                            g.is_ranked == lobby['ranked'] and g.notes == lobby['notes']):

                        players_in_lobby = g.capacity()[0]
                        # if remake_partial == True, lobby will be regenerated if anybody is in it.
                        # if remake_partial == False, lobby will only be regenerated once it is full

                        if lobby['remake_partial'] and players_in_lobby > 0:
                            pass  # Leave matching_lobby as current value. So it will be remade if no other open games change it
                        else:
                            matching_lobby = True  # Lobby meets desired criteria, so nothing new will be created

                if not matching_lobby:
                    logger.info(f'creating new lobby {lobby}')
                    guild = discord.utils.get(self.bot.guilds, id=lobby['guild'])
                    if not guild:
                        logger.warn(f'Bot not a member of guild {lobby["guild"]}')
                        continue
                    expiration_hours = lobby.get('exp', 30)
                    expiration_timestamp = (datetime.datetime.now() + datetime.timedelta(hours=expiration_hours)).strftime("%Y-%m-%d %H:%M:%S")
                    role_locks = lobby.get('role_locks', [None] * len(lobby['size']))
                    with models.db.atomic():
                        opengame = models.Game.create(host=None, notes=lobby['notes'],
                                                      guild_id=lobby['guild'], is_pending=True,
                                                      is_ranked=lobby['ranked'], expiration=expiration_timestamp)
                        for count, size in enumerate(lobby['size']):
                            role_lock_id = role_locks[count]
                            role_lock_name = None
                            if role_lock_id:
                                role_lock = discord.utils.get(guild.roles, id=role_lock_id)
                                if not role_lock:
                                    logger.warn(f'Lock to role {role_lock_id} was specified, but that role is not found in guild {guild.id} {guild.name}')
                                    role_lock_id = None
                                else:
                                    # successfully found role - using its ID to lock a side and its name for the role side
                                    role_lock_name = role_lock.name

                            models.GameSide.create(game=opengame, size=size, position=count + 1, required_role_id=role_lock_id, sidename=role_lock_name)

    async def task_print_matchlist(self):
        await self.bot.wait_until_ready()
        sleep_cycle = (60 * 60 * 1)

        while not self.bot.is_closed():
            await asyncio.sleep(5)
            logger.debug('Task running: task_print_matchlist')
            models.Game.purge_expired_games()
            for guild in self.bot.guilds:
                broadcast_channels = [guild.get_channel(chan) for chan in settings.guild_setting(guild.id, 'match_challenge_channels')]
                if not broadcast_channels:
                    continue

                ranked_chan = settings.guild_setting(guild.id, 'ranked_game_channel')
                unranked_chan = settings.guild_setting(guild.id, 'unranked_game_channel')

                for chan in broadcast_channels:
                    if not chan:
                        continue
                    if chan.id == ranked_chan:
                        game_list = models.Game.search_pending(status_filter=2, ranked_filter=1, guild_id=chan.guild.id)[:12]
                        list_title = 'Current ranked open games'
                    elif chan.id == unranked_chan:
                        game_list = models.Game.search_pending(status_filter=2, ranked_filter=0, guild_id=chan.guild.id)[:12]
                        list_title = 'Current unranked open games'
                    else:
                        game_list = models.Game.search_pending(status_filter=2, ranked_filter=2, guild_id=chan.guild.id)[:12]
                        list_title = 'Current open games'
                    if not game_list:
                        continue

                    pfx = settings.guild_setting(guild.id, 'command_prefix')

                    embed = discord.Embed(title=f'{list_title}\n'
                        f'Use __`{pfx}join ID`__ to join one or __`{pfx}game ID`__ for more details.')
                    embed.add_field(name=f'`{"ID":<8}{"Host":<40} {"Type":<7} {"Capacity":<7} {"Exp":>4} `', value='\u200b', inline=False)
                    for game in game_list:

                        notes_str = game.notes if game.notes else '\u200b'
                        players, capacity = game.capacity()
                        player_restricted_list = re.findall(r'<@!?(\d+)>', notes_str)

                        if player_restricted_list and (len(player_restricted_list) >= capacity - 1) and len(game_list) > 15:
                            # skipping invite-only games IF the games list is large
                            continue

                        capacity_str = f' {players}/{capacity}'
                        expiration = int((game.expiration - datetime.datetime.now()).total_seconds() / 3600.0)
                        expiration = 'Exp' if expiration < 0 else f'{expiration}H'
                        creating_player = game.creating_player()
                        host_name = creating_player.name[:35] if creating_player else '<Vacant>'
                        ranked_str = '*Unranked*' if not game.is_ranked else ''
                        ranked_str = ranked_str + ' - ' if game.notes and ranked_str else ranked_str

                        embed.add_field(name=f'`{game.id:<8}{host_name:<40} {game.size_string():<7} {capacity_str:<7} {expiration:>5}`', value=f'{ranked_str}{notes_str}\n \u200b')

                    try:
                        message = await chan.send(embed=embed, delete_after=sleep_cycle)
                    except discord.DiscordException as e:
                        logger.warn(f'Error broadcasting game list: {e}')
                    else:
                        logger.info(f'Broadcast game list to channel {chan.id} in message {message.id}')

            await asyncio.sleep(sleep_cycle)


def setup(bot):
    bot.add_cog(matchmaking(bot))
