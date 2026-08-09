import discord
from discord.ext import commands, tasks
import modules.models as models
import modules.utilities as utilities
import modules.image_storage as image_storage
import settings
import modules.exceptions as exceptions
from modules.games import post_newgame_messaging
from modules.league import broadcast_team_game_to_server
from modules import game_open, game_open_workers
from modules import game_notes, game_workers
from modules import game_side
from modules import game_join_leave, game_join_workers, game_kick_workers
from modules import game_search_workers
from modules import game_start, game_start_workers
from modules import game_reaction_workers
from modules import game_expiration
import peewee
import re
import datetime
import logging
import asyncio
import shlex  # for parsing $opengame arguments with quotation marks

logger = logging.getLogger('polybot.' + __name__)


class PolyMatch(commands.Converter):
    async def convert(self, ctx, match_id: int):

        match_id = match_id.strip('#')

        utilities.connect()
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
        except (ValueError, peewee.DataError):
            if match_id.upper() == 'ID':
                await ctx.send(f'Invalid Game ID "**{match_id}**". Use the numeric game ID *only*.')
            else:
                await ctx.send(f'Invalid Game ID "**{match_id}**".')
            raise commands.UserInputError()


class matchmaking(commands.Cog):
    """
    Host open and find open games.
    """

    ignorable_join_reactions = set()  # Set of entries indicating reactions that, if removed, should be ignored.
    # an entry will be (message_id, user_id)
    # keys are added here when a join reaction is placed, and removed if the join reaction is valid.

    def __init__(self, bot):
        self.bot = bot
        if settings.run_tasks:
            self.bg_task = asyncio.create_task(self.task_print_matchlist())
            self.bg_task2 = asyncio.create_task(self.task_dm_game_creators())
            self.bg_task3 = asyncio.create_task(self.task_create_empty_matchmaking_lobbies())
            self.task_purge_expired_games.start()  # new task style

    @staticmethod
    def parse_joingame_message(message: str) -> int | None:
        """Parse the advertised game ID without touching the database."""
        m = settings.re_join_game.search(message.lower())
        if not m:
            return None
        return int(m[1])

    async def load_reaction_game(self, game_id: int):
        return await game_reaction_workers.run_load_reaction_game(
            game_reaction_workers.ReactionGameRequest(game_id=int(game_id))
        )

    async def execute_join(
        self,
        *,
        game_id: int,
        member,
        author_member=None,
        side_arg=None,
        log_note: str = '',
        invoked_with: str = 'join',
        notification_member_id: int | None = None,
        prefix: str | None = None,
    ):
        """Run the shared join application service for any adapter."""

        request = game_join_leave.build_join_request(
            game_id=game_id,
            member=member,
            author_member=author_member,
            side_arg=side_arg,
            log_note=log_note,
            invoked_with=invoked_with,
            notification_member_id=notification_member_id,
            prefix=prefix,
        )
        return await game_join_leave.join(request)

    async def execute_leave(
        self,
        *,
        game_id: int,
        member,
        author_member=None,
        log_note: str = '',
        invoked_with: str = 'leave',
        prefix: str | None = None,
    ):
        """Run the shared leave application service for any adapter."""

        request = game_join_leave.build_leave_request(
            game_id=game_id,
            member=member,
            author_member=author_member,
            log_note=log_note,
            invoked_with=invoked_with,
            prefix=prefix,
        )
        return await game_join_leave.leave(request)

    async def execute_kick(
        self,
        *,
        game_id: int,
        author_member,
        target_member=None,
        target_query: str | None = None,
        invoked_with: str = 'kick',
        prefix: str | None = None,
    ):
        """Run the shared pending-game kick application service."""

        request = game_join_leave.build_kick_request(
            game_id=game_id,
            author_member=author_member,
            target_member=target_member,
            target_query=target_query,
            invoked_with=invoked_with,
            prefix=prefix,
        )
        return await game_join_leave.kick(request)

    async def execute_start(
        self,
        *,
        game_id: int,
        guild,
        requester,
        name: str | None,
        prefix: str | None = None,
        invoked_with: str = 'start',
    ):
        """Run the shared bounded pending-to-started transition."""

        return await game_start.execute_start(
            game_id=game_id,
            guild=guild,
            requester=requester,
            name=name,
            prefix=prefix,
            invoked_with=invoked_with,
        )

    def prefix_side_exists(self, *, game_id: int, guild_id: int, token: str) -> bool:
        """Disambiguate a legacy name token before the worker revalidates it."""

        try:
            game = models.Game.get_or_none(id=game_id)
            if not game or game.guild_id != guild_id:
                return False
            side, _ = game.get_side(lookup=token)
            return side is not None
        except (AttributeError, TypeError, ValueError, peewee.PeeweeException):
            return False

    @commands.Cog.listener()
    async def on_message(self, message):
        # Add ⚔️ join emoji to valid messages
        game_id = self.parse_joingame_message(message.content)
        if not game_id or message.guild is None:
            return
        try:
            game = await self.load_reaction_game(game_id)
        except Exception:
            logger.exception(
                'Could not load join-game message routing for game %s',
                game_id,
            )
            return
        if not game.exists or not game.is_pending:
            return
        if (
            message.guild.id == game.guild_id
            or message.guild.id in game.external_server_ids
        ):
            # current guild is compatible with game guild (either same guild or a related external server)
            await message.add_reaction(settings.emoji_join_game)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):

        if payload.emoji.name != settings.emoji_join_game:
            return

        if payload.user_id == self.bot.user.id:
            return

        if (payload.message_id, payload.user_id) in self.ignorable_join_reactions:
            logger.debug('Ignoring reaction removal due to ignorable_join_reactions')
            return self.ignorable_join_reactions.discard((payload.message_id, payload.user_id))

        guild = self.bot.get_guild(payload.guild_id)
        member = guild.get_member(payload.user_id)
        channel = member.guild.get_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id) if channel else None
        if not message:
            return

        if message.author.id == settings.bot_id_beta and self.bot.user.id != settings.bot_id_beta:
            # have production bot ignore messages from beta bot
            return

        if self.bot.user.id == settings.bot_id_beta and message.author.id != settings.bot_id_beta:
            # have beta bot ignore messages that are not from it
            return

        game_id = self.parse_joingame_message(message.content)

        if not game_id:
            return  # Message being reacted to is not parsed as a Join Game message

        if channel.name == 'polychamps-game-announcements':
            feedback_destination = member
        else:
            feedback_destination = channel

        try:
            game = await self.load_reaction_game(game_id)
        except Exception:
            logger.exception(
                'Could not load reaction-leave routing for game %s',
                game_id,
            )
            return await feedback_destination.send(
                f'Game {game_id} could not be loaded. Please try again.'
            )

        logger.debug(f'Matchmaking on_raw_reaction_removed: Joingame emoji removed from a Join Game message by {member.display_name}. Game ID {game_id}. Game loaded? {"yes" if game.exists else "no"}')

        if not game.exists:
            return await feedback_destination.send(
                f'Game {game_id} cannot be found or has been deleted.'
            )

        joining_member = member
        if member.guild.id != game.guild_id:
            valid_external_servers = game.external_server_ids
            game_guild = self.bot.get_guild(game.guild_id)
            if member.guild.id not in valid_external_servers or not game_guild:
                return await feedback_destination.send(
                    f'Game {game.game_id} is associated with another server.'
                )
            joining_member = game_guild.get_member(member.id)
            if not joining_member:
                return await feedback_destination.send(
                    f'You are not a member of game {game.game_id}'
                )

        try:
            result = await self.execute_leave(
                game_id=game.game_id,
                member=joining_member,
                author_member=joining_member,
                log_note='(via reaction)',
                invoked_with='reaction',
                prefix=settings.guild_setting(
                    game.guild_id,
                    'command_prefix',
                ),
            )
        except game_join_workers.PendingGameLeaveValidationError as exc:
            return await feedback_destination.send(str(exc))
        except peewee.PeeweeException:
            logger.exception(
                'Database failure leaving game %s via reaction',
                game.game_id,
            )
            return await feedback_destination.send(
                f'Game {game.game_id} could not be changed because the database '
                'operation failed.'
            )
        except Exception:
            logger.exception(
                'Unexpected failure leaving game %s via reaction',
                game.game_id,
            )
            return await feedback_destination.send(
                f'Game {game.game_id} could not be changed.'
            )

        if result.host_warning:
            await game_join_leave.send_post_commit_message(
                feedback_destination.send,
                result.host_warning,
                game_id=result.game_id,
                effect='host-leave warning',
            )
        await game_join_leave.send_post_commit_message(
            feedback_destination.send,
            f'Removing you from game {result.game_id}.',
            game_id=result.game_id,
            effect='leave output',
        )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):

        if payload.emoji.name != settings.emoji_join_game:
            return

        if payload.user_id == self.bot.user.id:
            return

        channel = payload.member.guild.get_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id) if channel else None
        if not message:
            return

        if message.author.id == 479029527553638401 and self.bot.user.id != 479029527553638401:
            # have beta bot ignore non-beta messages and production bot ignore beta messages
            return

        if self.bot.user.id == 479029527553638401 and message.author.id != 479029527553638401:
            # have beta bot ignore non-beta messages and production bot ignore beta messages
            return

        game_id = self.parse_joingame_message(message.content)

        if not game_id:
            return  # Message being reacted to is not parsed as a Join Game message

        self.ignorable_join_reactions.add((payload.message_id, payload.user_id))

        if channel.name == 'polychamps-game-announcements':
            feedback_destination = payload.member
        else:
            feedback_destination = channel

        try:
            game = await self.load_reaction_game(game_id)
        except Exception:
            logger.exception(
                'Could not load reaction-join routing for game %s',
                game_id,
            )
            self.ignorable_join_reactions.discard(
                (payload.message_id, payload.user_id)
            )
            return await feedback_destination.send(
                f'{payload.member.mention}, game {game_id} could not be loaded. '
                'Please try again.'
            )

        logger.debug(f'Matchmaking on_raw_reaction_add: Joingame emoji added to a Join Game message by {payload.member.display_name}. Game ID {game_id}. Game loaded? {"yes" if game.exists else "no"}')

        if not game.exists:
            await feedback_destination.send(f'{payload.member.mention}, it looks like you tried to join game {game_id}, but a game with that ID does not exist. Maybe it was deleted?')
            return await message.remove_reaction(payload.emoji.name, payload.member)

        if payload.member.guild.id == game.guild_id:
            # reaction in same guild as game is associated with
            guild = payload.member.guild
            joining_member = payload.member
            announce_channel = channel
        else:
            # guild does not match game guild. check to see if its a valid external server (PolyChamps teams)
            valid_external_servers = game.external_server_ids
            guild = self.bot.get_guild(game.guild_id)
            if not guild:
                return logger.warning(f'Matchmaking on_raw_reaction_add: could not load server {game.guild_id}')
            if payload.member.guild.id in valid_external_servers:
                logger.debug(f'Matchmaking on_raw_reaction_add: Join reacted from external server {payload.member.guild.name} from game server {guild.name} ')
                joining_member = guild.get_member(payload.member.id)
                if not joining_member:
                    logger.warning(f'{payload.member.guild.name} is not found as a member of game guild.')
                    await feedback_destination.send(f'{payload.member.mention}, it looks like you tried to join game {game_id}, but it is associated with another server: __{guild.name}__, and you are not a member of that server. ')
                    return await message.remove_reaction(payload.emoji.name, payload.member)

                announce_channel_id = settings.guild_setting(guild.id, 'game_announce_channel')
                announce_channel = guild.get_channel(announce_channel_id) if announce_channel_id else None
                if not announce_channel:
                    logger.warning(f'Guild {guild.id} {guild.name} does not have game_announce_channel configured')
                    await feedback_destination.send(f'{payload.member.mention}, it looks like you tried to join game {game_id}, but __{guild.name}__ does not have game_announce_channel configured. Joining via reaction is disabled. You will need to use the `join` command in a bot channel.')
                    return await message.remove_reaction(payload.emoji.name, payload.member)

            else:
                await feedback_destination.send(f'{payload.member.mention}, it looks like you tried to join game {game_id}, but it is associated with another server: __{guild.name}__ ')
                return await message.remove_reaction(payload.emoji.name, payload.member)

        prefix = settings.guild_setting(guild.id, 'command_prefix')
        try:
            result = await self.execute_join(
                game_id=game_id,
                member=joining_member,
                author_member=joining_member,
                side_arg=None,
                log_note='(via reaction)',
                invoked_with='reaction',
                notification_member_id=joining_member.id,
                prefix=prefix,
            )
        except game_join_workers.PendingGameJoinValidationError as exc:
            message_str = str(exc)
            logger.debug(f'Join by reaction failed: {message_str}')
            if 'already in game' in message_str:
                self.ignorable_join_reactions.discard(
                    (payload.message_id, payload.user_id)
                )
                return await feedback_destination.send(
                    f':warning: {joining_member.mention}:\n{message_str}'
                )
            await message.remove_reaction(payload.emoji.name, payload.member)
            return await feedback_destination.send(
                f':no_entry_sign: {joining_member.mention} could not join '
                f'game:\n{message_str}'
            )
        except peewee.PeeweeException:
            logger.exception(
                'Database failure joining game %s via reaction',
                game_id,
            )
            self.ignorable_join_reactions.discard(
                (payload.message_id, payload.user_id)
            )
            return await feedback_destination.send(
                f':no_entry_sign: {joining_member.mention} could not join '
                f'game {game_id}: the database operation failed.'
            )
        except Exception:
            logger.exception(
                'Unexpected failure joining game %s via reaction',
                game_id,
            )
            self.ignorable_join_reactions.discard(
                (payload.message_id, payload.user_id)
            )
            return await feedback_destination.send(
                f':no_entry_sign: {joining_member.mention} could not join '
                f'game {game_id}.'
            )

        reconciliation = await game_join_leave.remove_inactive_role_after_commit(
            result,
            joining_member,
        )
        message_list = list(result.messages)
        if reconciliation:
            message_list.append(reconciliation)

        try:
            committed_game = models.Game.load_full_game(
                game_id=result.game_id
            )
            embed, content = committed_game.embed(
                guild=guild,
                prefix=prefix,
            )
            content = f'{content}\n' if content else ''
        except Exception:
            logger.exception(
                'Committed join %s could not reload its game card',
                result.game_id,
            )
            committed_game = None
            embed, content = None, ''
            message_list.append(
                f':warning: Game {result.game_id} was joined successfully, '
                'but its game card could not be reloaded. An operator must '
                'reconcile the announcement.'
            )

        if result.is_full and committed_game is not None:
            announce_message = (
                f'Game {result.game_id} is now full and '
                f'<@{result.creator_id}> should create the game in Polytopia.'
            )
            if result.host_id and result.host_id != result.creator_id:
                announce_message += (
                    f'\nMatchmaking host <@{result.host_id}> is not the game '
                    'creator.'
                )
            try:
                await image_storage.send_game_embed(
                    announce_channel,
                    committed_game,
                    embed=embed,
                    content=f'{content}{announce_message}',
                )
            except Exception:
                logger.exception(
                    'Committed join %s full-game announcement failed',
                    result.game_id,
                )
                message_list.append(
                    f':warning: Game {result.game_id} was joined successfully, '
                    'but its full-game announcement could not be updated. An '
                    'operator must reconcile the announcement.'
                )

        if feedback_destination == payload.member:
            message_list.append(':bulb: I do not respond to PM commands. You will need to use a bot command channel in the appropriate server.')
        message_str = '\n'.join(message_list)

        logger.debug(f'Join by reaction success: {message_str}')
        self.ignorable_join_reactions.discard((payload.message_id, payload.user_id))
        if committed_game is not None:
            try:
                return await image_storage.send_game_embed(
                    feedback_destination,
                    committed_game,
                    embed=embed,
                    content=f'{message_str}',
                )
            except Exception:
                logger.exception(
                    'Committed join %s game card update failed',
                    result.game_id,
                )
                message_list.append(
                    f':warning: Game {result.game_id} was joined successfully, '
                    'but its game card could not be sent. An operator must '
                    'reconcile the announcement.'
                )
                message_str = '\n'.join(message_list)
        return await game_join_leave.send_post_commit_message(
            feedback_destination.send,
            message_str,
            game_id=result.game_id,
            effect='reaction fallback output',
        )

    @settings.in_bot_channel()
    @models.is_registered_member()
    @commands.command(aliases=['openmatch', 'open', 'opensteam'], usage='size expiration rules')
    async def opengame(self, ctx, *, args=None):

        """
        Opens a game that others can join
        Expiration can be between 1H - 168H
        Size examples: 1v1, 2v2, 1v1v1v1v1, 3v3v3, 1v3

        `opensteam` remains a compatibility alias; all newly opened games
        use the canonical cross-play behavior.

        **Examples:**
        `[p]opengame 1v1`

        `[p]opengame 1v1 48h`  (Expires in 48 hours)

        `[p]opengame 6FFA` (6 player free-for-all)

        `[p]opengame 1v1 unranked`  (Add word *unranked* to have game not count for ELO)

        `[p]opengame 2v2 Large map, no bardur`  (Adds a note to the game)

        `[p]opengame 1v1 Large map 1200 elo min`
        (Add an ELO requirement for joining with `max` or `min`. Also `1200 global elo max` to check global elo.)

        `[p]opengame 1v1 For @Nelluk only`
        (Include one or more @Mentions in notes and only those people will be permitted to join.)

        <START POLYCHAMPS>
        `[p]opengame 2v2 for @The Ronin vs @The Jets`
        (Include one or more @Roles and the games sides will be locked to that specific role. For use with PolyChampions teams.)

        `[p]opengame 2v2  role1="The Ronin" vs role=Jets`
        `[p]opengame 2v2  role="The Ronin" role="Junior Player"`
        (Use `role=RoleName`, `role#=RoleName`, `role="Full Role Name"` as an alternate way to lock sides to a role.
        This allows you to specify a role without a mention, as well as specify exactly which sides get which role.)
        """

        team_size, is_ranked = False, True
        # ``is_mobile`` is retained only as the legacy schema compatibility
        # value. It is not a platform selector for new open games.
        is_mobile = True
        roles_specified_implicity, roles_specified_explicitly = False, False
        required_role_args = []
        required_roles = []
        required_role_message = ''
        expiration_hours_override = None
        note_args = []

        if ctx.guild.id == 814317488418193478 and not settings.is_staff(ctx.author):
            return await ctx.send('For **The Polympics** only server staff may open games.')

        if args == 'games':
            return await ctx.invoke(self.bot.get_command('opengames'))

        if not args:
            return await ctx.send('Game size is required. Include argument like *1v1v1* to specify size.'
                f'\nExample: `{ctx.prefix}opengame 1v1 large map`'
                f'\nUse `{ctx.prefix}opengames` to list available open games.')

        if settings.guild_setting(ctx.guild.id, 'unranked_game_channel') and ctx.channel.id == settings.guild_setting(ctx.guild.id, 'unranked_game_channel'):
            is_ranked = False

        args = args.replace("'", "\\'").replace("“", "\"").replace("”", "\"")  # Escape single quotation marks for shlex.split() parsing
        if args.count('"') % 2 != 0:
            return await ctx.send(':no_entry_sign: Unbalanced "quotation marks" found. Cannot parse command.')
        # for arg in args.split(' '):
        try:
            parsed_args = shlex.split(args)
        except ValueError:
            return await ctx.send(':no_entry_sign: Unbalanced "quotation marks" found. Cannot parse command.')

        for arg in parsed_args:
            # Keep quoted phrases together, ie 'foo foo bar "baz bat" whatever' becomes ['foo', 'foo', 'bar', 'baz bat', 'whatever']
            v_shape = re.fullmatch(r"\d+(?:(v|vs)\d+)+", arg.lower())
            ffa_shape = re.fullmatch(r"\d+ffa", arg.lower())
            if v_shape or ffa_shape:
                try:
                    team_sizes, normalized_size = (
                        game_open_workers.parse_game_size_token(arg)
                    )
                except game_open_workers.OpenGameSizeError as exc:
                    return await ctx.send(str(exc))
                team_size_str = (
                    arg.lower() if v_shape else normalized_size
                )
                team_sizes = list(team_sizes)
                team_size = True
                required_roles = [None] * len(team_sizes)  # [None, None, None] for a 3-sided game
                required_role_names = [None] * len(team_sizes)
                continue
            m = re.match(r"(\d+)h", arg.lower())
            if m:
                # arg looks like '12h'
                if not 0 < int(m[1]) < 169:
                    return await ctx.send(f'Invalid expiration {arg}. Must be between 1H and 168H (One hour through seven days).')
                expiration_hours_override = int(m[1])
                continue
            if arg.lower()[:8] == 'unranked':
                is_ranked = False
                continue
            m = re.match(r"<@&(\d+)>", arg)
            if m:
                # arg looks like <@&123478951> role mention
                # replace raw role tag <@&....> with name of role, so people dont get mentioned every time note is printed
                # also extracting roles from raw args instead of iterating over ctx.message.roles since that ordering is not reliable
                if roles_specified_explicitly:
                    return await ctx.send(':no_entry_sign: Roles were assigned via both mention and explicit argument - use one or the other but not both.')
                roles_specified_implicity = True
                extracted_role = ctx.guild.get_role(int(m[1]))
                if extracted_role:
                    note_args.append('**@' + extracted_role.name + '**')
                    required_role_args.append(extracted_role)
                else:
                    logger.warning(f'Detected role-like string {m[0]} in arguments but cannot match to an actual role. Skipping.')
                continue
            m = re.match(r"role(\d?\d?)=(.*$)", arg)
            if m:
                # arg looks like role=Word, role1=Two Words, role10=Some Long Role Name
                logger.debug(f'Explicit role argument used. Name {m[2]} and explicit position: {m[1]}')
                if roles_specified_implicity:
                    return await ctx.send(':no_entry_sign: Roles were assigned via both mention and explicit argument - use one or the other but not both.')
                roles_specified_explicitly = True
                if m[1]:
                    # role ordering specified with an integer
                    role_position = int(m[1]) - 1  # Convert to 0-based index
                    if role_position < 0:
                        return await ctx.send(f':no_entry_sign: Role position of {role_position + 1} is invalid. Use numbers 1+ or omit numbers entirely. ')
                    if role_position + 1 > len(required_roles):
                        return await ctx.send(f':no_entry_sign: Role position of {role_position + 1} is invalid. The game does not have that many sides.')
                    logger.debug(f'Position {role_position} explicitly assigned to explicit role')

                else:
                    # role ordering unspecified - look for first side with no associated role lock
                    try:
                        role_position = required_roles.index(None)
                    except ValueError:
                        return await ctx.send(f':no_entry_sign: Role name of *{m[2]}* was specified but there are not enough sides to assign it.')
                    else:
                        logger.debug(f'Auto-assigning position {role_position} to explicit role.')

                role = utilities.guild_role_by_name(ctx.guild, m[2], allow_partial=True)
                if not role:
                    return await ctx.send(f':no_entry_sign: Role name of *{m[2]}* was specified but cannot be found.')
                logger.debug(f'Role named {role.name} {role.id} loaded')

                required_roles[role_position] = role.id
                required_role_names[role_position] = role.name
                required_role_message += f'**Side {role_position + 1}** will be locked to players with role *{role.name}*\n'
                note_args.append('**@' + role.name + '**')
                continue

            note_args.append(arg)

        if not team_size:
            return await ctx.send('Game size is required. Include argument like *1v1* to specify size')

        if required_role_args and len(required_role_args) < len(team_sizes) and required_role_args[0] not in ctx.author.roles:
            # used for a case like: $opengame 1v1 me vs @The Novas   -- puts that role on side 2 if you dont have it
            logger.debug('Offsetting required_role_args')
            required_role_args.insert(0, None)

        for count, role in enumerate(required_role_args):
            if count >= len(team_sizes):
                break
            if not role:
                continue
            required_roles[count] = role.id
            required_role_names[count] = role.name
            required_role_message += f'**Side {count + 1}** will be locked to players with role *{role.name}*\n'

        game_notes = utilities.escape_everyone_here_roles(' '.join(note_args)[:150].strip())
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
        request = game_open_workers.OpenGameRequest(
            guild_id=ctx.guild.id,
            requester_id=ctx.author.id,
            requester_name=ctx.author.name,
            requester_nick=getattr(ctx.author, 'nick', None),
            prefix=ctx.prefix,
            requester_role_ids=tuple(role.id for role in ctx.author.roles),
            requester_role_names=tuple(role.name for role in ctx.author.roles),
            requester_level=settings.get_user_level(ctx.author),
            requester_is_mod=settings.is_mod(ctx.author),
            requester_is_staff=settings.is_staff(ctx.author),
            sides=tuple(
                game_open_workers.OpenGameSide(
                    size=size,
                    required_role_id=required_roles[index],
                    required_role_name=required_role_names[index],
                )
                for index, size in enumerate(team_sizes)
            ),
            expiration_hours=expiration_hours,
            is_ranked=is_ranked,
            is_mobile=is_mobile,
            notes=game_notes,
            notes_display=notes_str,
            log_notes_display=discord.utils.escape_markdown(notes_str),
            requester_description=models.GameLog.member_string(ctx.author),
            invoked_with=ctx.invoked_with,
            role_lock_message=required_role_message,
            size_display=team_size_str,
        )
        try:
            result = await game_open_workers.run_open_game_creation(request)
        except game_open_workers.OpenGameValidationError as exc:
            return await ctx.send(str(exc))
        except (peewee.PeeweeException, exceptions.MyBaseException) as exc:
            logger.exception('Error creating open game')
            return await ctx.send(
                f'Error opening game: {exc}. No Discord announcements or '
                'reactions were created.'
            )
        except Exception:
            logger.exception('Unexpected error creating open game')
            return await ctx.send(
                'Error opening game. No Discord announcements or reactions '
                'were created.'
            )

        async def broadcast():
            opengame = models.Game.load_full_game(game_id=result.game_id)
            await broadcast_team_game_to_server(ctx, opengame)

        await game_open.publish_open_game_result(
            result,
            prefix=ctx.prefix,
            send=ctx.send,
            broadcast=broadcast,
        )

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
            return await ctx.send('The game has already started and can no longer be changed.')
        if not game.is_hosted_by(ctx.author.id)[0] and not settings.is_staff(ctx.author):
            return await ctx.send('Only the game host or server staff can do this.')

        # TODO: Have this command also allow side re-ordering
        # matchside m1 1 name ronin
        # matchside m1 ronin nelluk rickdaheals jonathan

        role_mentions = tuple(
            getattr(getattr(ctx, 'message', None), 'role_mentions', ()) or ()
        )
        role = role_mentions[0] if len(role_mentions) == 1 else None
        clear = False
        side_name = args
        if role is None and args and args.lower() == 'none':
            clear = True
            side_name = None

        request = game_side.build_mutation_request(
            member=ctx.author,
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            game_id=game.id,
            side_lookup=side_lookup,
            side_name=(None if role is not None else side_name),
            role_id=(role.id if role is not None else None),
            role_name=(role.name if role is not None else None),
            role_guild_id=(
                getattr(getattr(role, 'guild', None), 'id', None)
                if role is not None else None
            ),
            clear=clear,
            native=False,
            invoked_with=getattr(ctx, 'invoked_with', None) or 'gameside',
        )

        async def after_commit(result):
            await game_side.publish_mutation_result(
                result,
                send=ctx.send,
                destination=ctx,
                guild=ctx.guild,
                prefix=ctx.prefix,
            )

        try:
            await game_side.run_side_mutation(
                request,
                after_commit=after_commit,
            )
        except game_workers.GameSideLookupError as exc:
            return await ctx.send(str(exc))
        except game_workers.GameSideValidationError as exc:
            return await ctx.send(str(exc))
        except exceptions.RecordLocked as exc:
            return await ctx.send(str(exc))
        except peewee.PeeweeException:
            logger.exception('Database failure setting game side')
            return await ctx.send(
                'The side change failed and rolled back. No Discord '
                'announcement or card update was made.'
            )
        except Exception:
            logger.exception('Unexpected failure setting game side')
            return await ctx.send(
                'The side change failed. No Discord announcement or card '
                'update was made.'
            )

    @settings.in_bot_channel()
    @commands.command(usage='game_id', aliases=['joingame', 'joinmatch'])
    async def join(self, ctx, game_id: str = None, *args):
        """Join an open game through the shared pending-game service."""

        syntax = (
            f'**Example usage**:\n__`{ctx.prefix}join 1025`__ - Join game '
            f'1025\n__`{ctx.prefix}join 1025 2`__ - Join game 1025, side 2'
        )
        if settings.get_user_level(ctx.author) >= 4:
            syntax += (
                f'\n__`{ctx.prefix}join 1025 Nelluk 2`__ - Add a third '
                'party to side 2 of your open game.'
            )

        if not game_id:
            return await ctx.send(
                f'No game ID provided. Use `{ctx.prefix}opengames` to list '
                f'open games you can join.\n{syntax}'
            )

        try:
            parsed_game_id = int(str(game_id).strip('#'))
        except (TypeError, ValueError):
            return await ctx.send(
                f'Invalid Game ID **{game_id}**.\n{syntax}'
            )

        named_side_candidate = False
        third_party_candidate = False
        if len(args) == 0:
            target = f'<@{ctx.author.id}>'
            side_arg = None
        elif len(args) == 1:
            token = args[0]
            try:
                numeric_token = int(token)
            except (TypeError, ValueError):
                numeric_token = None

            if numeric_token is not None:
                target = f'<@{ctx.author.id}>'
                # Keep the legacy numeric grammar: positive in-range values
                # select a side; zero and values above the configured maximum
                # mean the author's first open side, but still required the
                # old level-4 path because ``side_arg`` was falsey.
                side_arg = (
                    str(numeric_token)
                    if numeric_token <= settings.max_game_size
                    and numeric_token != 0
                    else None
                )
                if (
                    not side_arg
                    and settings.get_user_level(ctx.author) < 4
                ):
                    return await ctx.send(
                        'You do not have permissions to add another person to '
                        'a game. Tell them to use the command:\n'
                        f'`{ctx.prefix}join {parsed_game_id}` to join '
                        'themselves.'
                    )
            else:
                target = token
                side_arg = None
                third_party_candidate = True
                named_side_candidate = numeric_token is None
        elif len(args) == 2:
            if settings.get_user_level(ctx.author) < 4:
                return await ctx.send(
                    'You do not have permissions to add another person to a '
                    'game. Tell them to use the command:\n'
                    f'`{ctx.prefix}join {parsed_game_id} {args[1]}` to join '
                    'themselves.'
                )
            target, side_arg = args
        else:
            return await ctx.send(f'Invalid usage.\n{syntax}')

        if third_party_candidate and settings.get_user_level(ctx.author) < 4:
            if named_side_candidate and self.prefix_side_exists(
                game_id=parsed_game_id,
                guild_id=ctx.guild.id,
                token=args[0],
            ):
                # A low-level requester may select a named side when the
                # token is not also being used as a third-party member name.
                target = f'<@{ctx.author.id}>'
                side_arg = args[0]
                third_party_candidate = False
            else:
                return await ctx.send(
                    'You do not have permissions to add another person to a '
                    'game. Tell them to use the command:\n'
                    f'`{ctx.prefix}join {parsed_game_id}` to join themselves.'
                )

        guild_matches = await utilities.get_guild_member(ctx, target)
        if len(guild_matches) > 1:
            return await ctx.send(
                f'There is more than one player found with name "{target}". '
                f'Specify user with @Mention.\n{syntax}'
            )
        if len(guild_matches) == 0:
            named_side = (
                named_side_candidate
                and self.prefix_side_exists(
                    game_id=parsed_game_id,
                    guild_id=ctx.guild.id,
                    token=args[0],
                )
            )
            if named_side:
                side_arg = args[0]
                target = f'<@{ctx.author.id}>'
            elif third_party_candidate and settings.get_user_level(ctx.author) < 4:
                return await ctx.send(
                    'You do not have permissions to add another person to a '
                    'game. Tell them to use the command:\n'
                    f'`{ctx.prefix}join {parsed_game_id}` to join themselves.'
                )
            else:
                return await ctx.send(
                    f'Could not find "{target}" on this server.\n{syntax}'
                )
        else:
            if third_party_candidate and settings.get_user_level(ctx.author) < 4:
                return await ctx.send(
                    'You do not have permissions to add another person to a '
                    'game. Tell them to use the command:\n'
                    f'`{ctx.prefix}join {parsed_game_id}` to join themselves.'
                )
            joining_member = guild_matches[0]

        if len(guild_matches) == 0:
            guild_matches = await utilities.get_guild_member(
                ctx,
                f'<@{ctx.author.id}>',
            )
            if len(guild_matches) != 1:
                return await ctx.send(
                    f'Could not find <@{ctx.author.id}> on this server.\n'
                    f'{syntax}'
            )
            joining_member = guild_matches[0]

        try:
            result = await self.execute_join(
                game_id=parsed_game_id,
                member=joining_member,
                author_member=ctx.author,
                side_arg=side_arg,
                invoked_with=ctx.invoked_with or 'join',
                notification_member_id=ctx.author.id,
                prefix=ctx.prefix,
            )
        except game_join_workers.PendingGameJoinValidationError as exc:
            message = str(exc)
            logger.debug('join via command failed: %s', message)
            if 'already in game' in message:
                return await ctx.send(f':warning: {message}')
            return await ctx.send(
                f':no_entry_sign: Could not join game:\n{message}'
            )
        except peewee.PeeweeException:
            logger.exception('Database failure joining game %s', parsed_game_id)
            return await ctx.send(
                ':no_entry_sign: Could not join game because the database '
                'operation failed.'
            )
        except Exception:
            logger.exception('Unexpected failure joining game %s', parsed_game_id)
            return await ctx.send(
                ':no_entry_sign: Could not join game.'
            )

        reconciliation = await game_join_leave.remove_inactive_role_after_commit(
            result,
            joining_member,
        )
        message_list = list(result.messages)
        if reconciliation:
            message_list.append(reconciliation)

        committed_game = None
        embed = content = None
        try:
            committed_game = models.Game.load_full_game(result.game_id)
            embed, content = committed_game.embed(
                guild=ctx.guild,
                prefix=ctx.prefix,
            )
        except Exception:
            logger.exception(
                'Committed join %s could not reload its game card',
                result.game_id,
            )
            message_list.append(
                f':warning: Game {result.game_id} was joined successfully, '
                'but its game card could not be updated. An operator must '
                'reconcile the announcement.'
            )

        if result.is_full:
            await game_join_leave.send_post_commit_message(
                ctx.send,
                f'Game {result.game_id} is now full and '
                f'<@{result.creator_id}> should create the game in Polytopia.',
                game_id=result.game_id,
                effect='full-game notice',
            )
            if result.host_id and result.host_id != result.creator_id:
                await game_join_leave.send_post_commit_message(
                    ctx.send,
                    f'Matchmaking host <@{result.host_id}> is not the game '
                    'creator.',
                    game_id=result.game_id,
                    effect='host-mismatch notice',
                )

        if committed_game is not None:
            try:
                await image_storage.send_game_embed(
                    ctx,
                    committed_game,
                    embed=embed,
                    content=content if result.is_full else None,
                )
            except Exception:
                logger.exception(
                    'Committed join %s game card update failed',
                    result.game_id,
                )
                message_list.append(
                    f':warning: Game {result.game_id} was joined successfully, '
                    'but its game card could not be sent. An operator must '
                    'reconcile the announcement.'
                )

        return await game_join_leave.send_post_commit_message(
            ctx.send,
            '\n'.join(message_list),
            game_id=result.game_id,
            effect='join output',
        )

    @settings.in_bot_channel()
    @commands.command(usage='game_id')
    async def leave(self, ctx, game_id: str = None):
        """Leave a pending game through the shared lifecycle service."""

        if not game_id:
            return await ctx.send(
                f'No game ID provided. Use `{ctx.prefix}leave ID` to leave a '
                'specific game.'
            )
        try:
            parsed_game_id = int(str(game_id).strip('#'))
        except (TypeError, ValueError):
            return await ctx.send(f'Invalid Game ID **{game_id}**.')

        try:
            result = await self.execute_leave(
                game_id=parsed_game_id,
                member=ctx.author,
                author_member=ctx.author,
                invoked_with=ctx.invoked_with or 'leave',
                prefix=ctx.prefix,
            )
        except game_join_workers.PendingGameLeaveValidationError as exc:
            return await ctx.send(str(exc))
        except peewee.PeeweeException:
            logger.exception('Database failure leaving game %s', parsed_game_id)
            return await ctx.send(
                f'Game {parsed_game_id} could not be changed because the '
                'database operation failed.'
            )
        except Exception:
            logger.exception('Unexpected failure leaving game %s', parsed_game_id)
            return await ctx.send(f'Game {parsed_game_id} could not be changed.')

        if result.host_warning:
            await game_join_leave.send_post_commit_message(
                ctx.send,
                result.host_warning,
                game_id=result.game_id,
                effect='host-leave warning',
            )
        return await game_join_leave.send_post_commit_message(
            ctx.send,
            result.message,
            game_id=result.game_id,
            effect='leave output',
        )

    @settings.in_bot_channel()
    @models.is_registered_member()
    @commands.command(
        hidden=True,
        usage='game_id [notes]',
        aliases=['notes', 'matchnotes'],
    )
    # clean_content converter flattens and user/role tags.
    async def gamenotes(
        self,
        ctx,
        *,
        args: discord.ext.commands.clean_content = None,
    ):
        """
        Edit notes for an open game you host.
        **Example:**
        `[p]gamenotes 1234 Large map, no bans` - Update notes for game 1234
        `[p]gamenotes 1234 none` - Delete notes for game 1234
        """

        raw_args = str(args or '').strip()
        first_token, separator, remainder = raw_args.partition(' ')
        explicit_game_id = first_token.strip('#')
        inferred_from_channel = False

        try:
            game_id = int(explicit_game_id)
        except ValueError:
            game_id = None

        if game_id is not None:
            notes = remainder.strip() if separator else None
        else:
            # Preserve the old PolyMatch invalid-ID surface while allowing the
            # established game-channel grammar used by adjacent game commands.
            target_request = game_notes.build_mutation_request(
                member=ctx.author,
                guild_id=ctx.guild.id,
                channel_id=ctx.channel.id,
                game_id=None,
                legacy_tokens=(),
                allow_related_channel=True,
                invoked_with=ctx.invoked_with or 'gamenotes',
                prefix=ctx.prefix,
            )
            try:
                target = await game_workers.run_prepare_legacy_game_notes(
                    target_request,
                )
            except game_workers.GameNotesLookupError:
                if explicit_game_id.upper() == 'ID':
                    return await ctx.send(
                        f'Invalid Game ID "**{explicit_game_id}**". Use the numeric '
                        'game ID *only*.'
                    )
                if first_token:
                    return await ctx.send(
                        f'Invalid Game ID "**{explicit_game_id}**".'
                    )
                return await ctx.send(
                    f'Include new note or *none* to delete existing note. '
                    f'Usage: `{ctx.prefix}{ctx.invoked_with} game_id These '
                    'are my new notes`'
                )
            except peewee.PeeweeException:
                logger.exception('Database failure inferring notes game')
                return await ctx.send(
                    'The notes target could not be loaded.'
                )
            game_id = target.game_id
            notes = raw_args or None
            inferred_from_channel = True

        if not notes:
            read_request = game_notes.build_read_request(
                member=ctx.author,
                guild_id=ctx.guild.id,
                channel_id=ctx.channel.id,
                game_id=game_id,
                allow_related_channel=inferred_from_channel,
            )
            try:
                await game_notes.run_notes_read(read_request)
            except game_workers.GameNotesValidationError as exc:
                message = str(exc)
                if 'different discord server' in message:
                    message = (
                        f'Game with ID {game_id} is associated with a different '
                        f'Discord server. Use `{ctx.prefix}opengames` to see '
                        'available matches.'
                    )
                elif 'cannot be found' in message:
                    message = (
                        f'Game with ID {game_id} cannot be found. Use '
                        f'`{ctx.prefix}opengames` to see available matches.'
                    )
                return await ctx.send(message)
            except peewee.PeeweeException:
                logger.exception('Database failure reading game notes usage')
                return await ctx.send('The notes target could not be loaded.')
            return await ctx.send(
                f'Include new note or *none* to delete existing note. Usage: '
                f'`{ctx.prefix}{ctx.invoked_with} {game_id} These are my new '
                'notes`'
            )

        clear = notes.lower() == 'none'
        request = game_notes.build_mutation_request(
            member=ctx.author,
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            game_id=int(game_id),
            notes=None if clear else notes,
            clear=clear,
            invoked_with=ctx.invoked_with or 'gamenotes',
            prefix=ctx.prefix,
            truncate=True,
            legacy_none=True,
            allow_related_channel=inferred_from_channel,
            mention_warning=bool(
                ctx.message.mentions or ctx.message.role_mentions
            ),
        )

        async def after_commit(result):
            await game_notes.publish_mutation_result(
                result,
                send=ctx.send,
                refresh_card=lambda value: game_notes.refresh_game_card(
                    value,
                    destination=ctx,
                    guild=ctx.guild,
                    prefix=ctx.prefix,
                ),
            )

        try:
            await game_notes.run_notes_mutation(
                request,
                after_commit=after_commit,
            )
        except game_workers.GameNotesValidationError as exc:
            message = str(exc)
            if 'different discord server' in message:
                message = (
                    f'Game with ID {game_id} is associated with a different '
                    f'Discord server. Use `{ctx.prefix}opengames` to see '
                    'available matches.'
                )
            elif 'cannot be found' in message:
                message = (
                    f'Game with ID {game_id} cannot be found. Use '
                    f'`{ctx.prefix}opengames` to see available matches.'
                )
            return await ctx.send(message)
        except exceptions.RecordLocked as exc:
            return await ctx.send(str(exc))
        except peewee.PeeweeException:
            logger.exception('Database failure setting game notes')
            return await ctx.send(
                'The notes change failed and rolled back. No Discord '
                'announcement or card update was made.'
            )
        except Exception:
            logger.exception('Unexpected failure setting game notes')
            return await ctx.send(
                'The notes change failed. No Discord announcement or card '
                'update was made.'
            )

    @settings.in_bot_channel()
    @models.is_registered_member()
    @commands.command(usage='game_id player')
    async def kick(self, ctx, game_id: str, player: str):
        """
        Kick a player from an open game
        **Example:**
        `[p]kick 25 koric`
        """
        try:
            parsed_game_id = int(str(game_id).strip('#'))
        except (TypeError, ValueError):
            return await ctx.send(f'Invalid Game ID "{game_id}".')

        try:
            result = await self.execute_kick(
                game_id=parsed_game_id,
                author_member=ctx.author,
                target_query=player,
                invoked_with=ctx.invoked_with or 'kick',
                prefix=ctx.prefix,
            )
        except game_kick_workers.PendingGameKickValidationError as exc:
            return await ctx.send(str(exc))
        except peewee.PeeweeException:
            logger.exception('Database failure kicking game %s', parsed_game_id)
            return await ctx.send(
                f'Game {parsed_game_id} could not be changed because the '
                'database operation failed. No public game effects were made.'
            )
        except Exception:
            logger.exception('Unexpected failure kicking game %s', parsed_game_id)
            return await ctx.send(
                f'Game {parsed_game_id} could not be changed. No public game '
                'effects were made.'
            )

        await game_join_leave.publish_kick_result(
            result,
            send=ctx.send,
            card_destination=ctx,
            guild=ctx.guild,
            prefix=ctx.prefix,
        )

    @settings.in_bot_channel()
    @commands.command(aliases=['opengames', 'novagames', 'nova'])
    async def games(self, ctx, *args):
        """
        List joinable open games

        Full games will still be listed until the host starts or deletes them with `[p]startgame` / `[p]deletegame`

        **Example:**
        `[p]opengames` - List all open games that you are able to join
        `[p]opengames waiting` - Lists open games that are full but not yet started
        `[p]opengames all` - List all open games with open space, even games you cannot join due to restrictions
        `[p]opengames me` - List unstarted opengames that you have joined
        You can also add keywords **ranked** or **unranked** or **steam** to filter by those types of games.
        """
        args = tuple(args)
        ranked_filter, ranked_str = 2, ''
        platform_filter = 2  # Retained for legacy Steam/channel parsing.
        platform_str = ''
        ranked_chan = settings.guild_setting(
            ctx.guild.id,
            'ranked_game_channel',
        )
        unranked_chan = settings.guild_setting(
            ctx.guild.id,
            'unranked_game_channel',
        )
        steam_chan = settings.guild_setting(
            ctx.guild.id,
            'steam_game_channel',
        )

        invoked_with = str(ctx.invoked_with).lower()
        if invoked_with == 'nova' and args and args[0].lower() == 'games':
            # Preserve the historical '$nova games' redirect.
            args = args[1:]

        channel_id = getattr(getattr(ctx, 'channel', None), 'id', None)
        if (
            channel_id == unranked_chan
            or any(arg.upper() == 'UNRANKED' for arg in args)
        ):
            ranked_filter = 0
            ranked_str = ' **unranked**'
        elif (
            channel_id == ranked_chan
            or any(arg.upper() == 'RANKED' for arg in args)
        ):
            ranked_filter = 1
            ranked_str = ' **ranked**'
        elif (
            channel_id == steam_chan
            or any(arg.upper() == 'STEAM' for arg in args)
        ):
            platform_filter = 0
            platform_str = ' **Steam**'

        if args and args[0].upper() == 'WAITING':
            mode = 'waiting'
            title_str = f'Open{ranked_str} games waiting to start'
        elif args and args[0].upper() == 'ME':
            mode = 'mine'
            title_str = f'Open games joined by **{ctx.author.name}**'
        elif invoked_with in ('novagames', 'nova'):
            if args and args[0].upper() == 'ALL':
                mode = 'nova-all'
                title_str = (
                    f'Current pending Nova games\nUse `{ctx.prefix}games` '
                    'for all joinable games.'
                )
            else:
                mode = 'nova-joinable'
                title_str = (
                    f'Current joinable Nova games\nUse `{ctx.prefix}'
                    'novagames all` to view all Nova Games or '
                    f'`{ctx.prefix}games` for all joinable games.'
                )
        else:
            if args and args[0].upper() == 'ALL':
                mode = 'all-open'
                filter_str = ''
            else:
                mode = 'joinable'
                filter_str = ' joinable'
            title_str = (
                f'Current{filter_str}{ranked_str}{platform_str} open games '
                'with available spots'
            )

        author_roles = tuple(getattr(ctx.author, 'roles', ()) or ())
        request = game_search_workers.GameSearchRequest(
            guild_id=ctx.guild.id,
            requester_discord_id=ctx.author.id,
            key=game_search_workers.GameSearchKey(status=mode),
            requester_level=settings.get_user_level(ctx.author),
            requester_role_ids=tuple(role.id for role in author_roles),
            requester_name=getattr(ctx.author, 'name', ''),
            requester_nick=getattr(ctx.author, 'nick', None),
            staff=settings.is_staff(ctx.author),
            ranked_filter=ranked_filter,
            # Nova historically ignored platform filtering. Keep that
            # compatibility while preserving Steam parsing for ordinary views.
            platform_filter=(2 if mode.startswith('nova-') else platform_filter),
            include_waitlist=True,
        )

        async with ctx.typing():
            try:
                snapshot = await asyncio.wait_for(
                    game_search_workers.run_game_search(request),
                    timeout=20.0,
                )
            except (
                game_search_workers.GameSearchError,
                peewee.PeeweeException,
                asyncio.TimeoutError,
                ValueError,
            ) as exc:
                return await ctx.send(
                    str(exc) or 'Game search timed out.'
                )

        gamelist_fields = [
            (
                f'`{"ID":<8}{"Host":<40} {"Type":<7} '
                f'{"Capacity":<7} {"Exp":>4}` ',
                '\u200b',
            )
        ]
        for row in snapshot.rows:
            notes_str = row.notes if row.notes else '\u200b'
            ranked_display = '*Unranked*' if not row.ranked else ''
            ranked_display = (
                ranked_display + ' - '
                if row.notes and ranked_display
                else ranked_display
            )
            gamelist_fields.append((
                f'`{f"{row.game_id}":<8}{row.host_name:<40} '
                f'{row.size:<7} {f" {row.players}/{row.capacity}":<7} '
                f'{row.expiration:>5}`',
                f'{row.platform_emoji} {ranked_display}{notes_str}\n \u200b',
            ))

        if mode == 'joinable' and snapshot.filtered_count:
            count = snapshot.filtered_count
            noun = 'game' if count == 1 else 'games'
            verb = 'was' if count == 1 else 'were'
            title_str += (
                f'\n{count} {noun} that you cannot join {verb} filtered. '
                f'See `{ctx.prefix}{ctx.invoked_with} all` for an unfiltered '
                'list.'
            )

        title_str_full = (
            title_str
            + f'\nUse __`{ctx.prefix}join ID`__ to join one or '
            f'__`{ctx.prefix}game ID`__ for more details.'
        )

        asyncio.create_task(
            utilities.paginate(
                self.bot,
                ctx,
                title=title_str_full[:255],
                message_list=gamelist_fields,
                page_start=0,
                page_end=15,
                page_size=15,
            )
        )
        # The worker returned all database-derived waitlist data before the
        # legacy paginator and reminder are scheduled.
        if snapshot.waitlist_ids:
            await asyncio.sleep(1)
            if len(snapshot.waitlist_ids) == 1:
                start_str = (
                    f'Type __`{ctx.prefix}game '
                    f'{snapshot.waitlist_ids[0]}`__ for more details.'
                )
            else:
                start_str = (
                    f'Type __`{ctx.prefix}game IDNUM`__ for more details, '
                    f'ie `{ctx.prefix}game {snapshot.waitlist_ids[0]}`'
                )
            await ctx.send(
                f'{ctx.author.mention}, you have full games waiting to start: '
                f'**{", ".join(snapshot.waitlist_ids)}**\n{start_str}'
            )

    @settings.in_bot_channel()
    @models.is_registered_member()
    @commands.command(aliases=['startgame'], usage='game_id Name of Poly Game')
    async def start(self, ctx, game_id: str = None, *, name: str = None):
        """
        Start a full game and track it for ELO
        Use this command after you have created the game in Polytopia.
        **Example:**
        `[p]startgame 100 Fields of Fire`
        """

        syntax = (
            f'**Example usage**:\n__`{ctx.prefix}start 1025 Name of Game`__'
        )

        if not game_id:
            return await ctx.send(
                f'No game ID provided. Use `{ctx.prefix}opengames me` to list '
                f'open games you have waiting to start.\n{syntax}'
            )

        raw_game_id = str(game_id).strip('#')
        try:
            parsed_game_id = int(raw_game_id)
        except (TypeError, ValueError):
            if raw_game_id.upper() == 'ID':
                return await ctx.send(
                    f'Invalid Game ID "**{raw_game_id}**". Use the numeric '
                    'game ID *only*.'
                )
            return await ctx.send(f'Invalid Game ID "**{raw_game_id}**".')

        try:
            result = await self.execute_start(
                game_id=parsed_game_id,
                guild=ctx.guild,
                requester=ctx.author,
                name=name,
                prefix=ctx.prefix,
                invoked_with=ctx.invoked_with,
            )
        except game_start_workers.GameStartValidationError as exc:
            return await ctx.send(str(exc))
        except (peewee.PeeweeException, exceptions.CheckFailedError) as exc:
            logger.exception('Error starting game %s', parsed_game_id)
            return await ctx.send(f'Error starting game: {exc}')
        except Exception:
            logger.exception('Unexpected error starting game %s', parsed_game_id)
            return await ctx.send(
                'Error starting game. No Discord announcements, channels, or '
                'other post-commit effects were attempted.'
            )

        await game_start.publish_start_result(
            result,
            output_context=ctx,
            guild=ctx.guild,
            prefix=ctx.prefix,
            bot_guilds=settings.bot.guilds,
        )

    async def task_dm_game_creators(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            await asyncio.sleep(60 * 60 * 12)
            logger.debug('Task running: task_dm_game_creators')
            utilities.connect()
            full_games = models.Game.search_pending(status_filter=1, ranked_filter=1)
            logger.debug(f'Starting task_dm_game_creators on {len(full_games)} games')
            for game in full_games:
                last_joiner = models.GameLog.search(keywords=f'_{game.id}_ joined', guild_id=game.guild_id, limit=1).first()
                if last_joiner and last_joiner.message_ts > (datetime.datetime.now() + datetime.timedelta(hours=-12)):
                    logger.debug(f'Skipping task_dm_game_creators for game {game.id} - most recent joiner joined too recently.')
                    continue

                guild = self.bot.get_guild(game.guild_id)
                creating_player = game.creating_player()
                # TODO: ? only trigger if game is <23hours til expiration
                if not guild:
                    logger.error(f'Couldnt load guild ID {game.guild_id}')
                    continue

                creating_guild_member = guild.get_member(creating_player.discord_member.discord_id)
                if not creating_guild_member:
                    logger.warning(f'Couldnt load creator for game {game.id} in server {guild.name}. Maybe they left the server?')
                    continue

                bot_channel = settings.guild_setting(guild.id, 'bot_channels_strict')[0]
                prefix = settings.guild_setting(guild.id, 'command_prefix')

                embed, _ = game.embed(guild=guild, prefix=prefix)

                message = (f'__You have a ranked game on **{guild.name}** that is waiting to be created.__'
                           f'\nPlease visit the server\'s bot channel at this link: <https://discordapp.com/channels/{guild.id}/{bot_channel}/>'
                           f'\nType the command __`{prefix}game {game.id}`__ for more details. Remember. you must manually **create the game within Polytopia**, '
                           f'come back to discord, and use the command __`{prefix}start {game.id} Name of Game`__ to mark the game as started.'
                           f'\n\nYou can use the command __`{prefix}names {game.id}`__ to get each player\'s in-game name in an easy-to-copy format.'
                           '\n\n*(I do not respond to DMed commands. You must issue commands in the channel linked above.)*')

                try:
                    await image_storage.send_game_embed(
                        creating_guild_member,
                        game,
                        embed=embed,
                        content=message,
                    )
                    logger.info(f'Sending reminder DM to {creating_guild_member.name} {creating_guild_member.id} to start game {game.id}')
                except discord.DiscordException as e:
                    logger.warning(f'Error DMing creator of waiting game: {e}')

    async def task_create_empty_matchmaking_lobbies(self):
        # Keep open games list populated with vacant lobbies as specified in settings.lobbies

        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            await asyncio.sleep(60)
            logger.debug('Task running: task_create_empty_matchmaking_lobbies')
            utilities.connect()
            unhosted_game_list = models.Game.search_pending(status_filter=2, host_discord_id=0)
            for lobby in settings.lobbies:
                matching_lobby = False
                for g in unhosted_game_list:
                    if (g.guild_id == lobby['guild'] and g.size_string() == lobby['size_str'] and
                            g.is_ranked == lobby['ranked'] and g.notes == lobby['notes']):
                        # TODO: could be improved by comparing g.size to lobby['size'] now that Game.size is a field

                        players_in_lobby = g.capacity()[0]
                        # if remake_partial == True, lobby will be regenerated if anybody is in it.
                        # if remake_partial == False, lobby will only be regenerated once it is full

                        if lobby['remake_partial'] and players_in_lobby > 0:
                            pass  # Leave matching_lobby as current value. So it will be remade if no other open games change it
                        else:
                            matching_lobby = True  # Lobby meets desired criteria, so nothing new will be created

                if not matching_lobby:
                    logger.info(f'creating new lobby {lobby}')
                    guild = self.bot.get_guild(lobby['guild'])
                    if not guild:
                        logger.warning(f'Bot not a member of guild {lobby["guild"]}')
                        continue
                    expiration_hours = lobby.get('exp', 30)
                    expiration_timestamp = (datetime.datetime.now() + datetime.timedelta(hours=expiration_hours)).strftime("%Y-%m-%d %H:%M:%S")
                    role_locks = lobby.get('role_locks', [None] * len(lobby['size']))
                    with models.db.atomic():
                        opengame = models.Game.create(host=None, notes=lobby['notes'],
                                                      guild_id=lobby['guild'], is_pending=True,
                                                      is_ranked=lobby['ranked'], expiration=expiration_timestamp, size=lobby['size'])
                        notes_str = f'*{discord.utils.escape_markdown(opengame.notes)}*' if opengame.notes else ''
                        models.GameLog.write(game_id=opengame, guild_id=guild.id, message=f'I created an {"unranked" if not lobby["ranked"] else ""} empty {lobby["size_str"]} lobby. {notes_str}')
                        for count, size in enumerate(lobby['size']):
                            role_lock_id = role_locks[count]
                            role_lock_name = None
                            if role_lock_id:
                                role_lock = guild.get_role(role_lock_id)
                                if not role_lock:
                                    logger.warning(f'Lock to role {role_lock_id} was specified, but that role is not found in guild {guild.id} {guild.name}')
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
            utilities.connect()
            # models.Game.purge_expired_games()
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

                        embed.add_field(name=f'`{game.id:<8}{host_name:<40} {game.size_string():<7} {capacity_str:<7} {expiration:>5}`', value=f'{game.platform_emoji()} {ranked_str}{notes_str}\n \u200b', inline=False)
                    try:
                        message = await chan.send(embed=embed, delete_after=sleep_cycle)
                    except discord.DiscordException as e:
                        logger.warning(f'Error broadcasting game list: {e}')
                    else:
                        logger.info(f'Broadcast game list to channel {chan.id} in message {message.id}')
                        self.bot.purgable_messages = self.bot.purgable_messages[-20:] + [(guild.id, chan.id, message.id)]

            await asyncio.sleep(sleep_cycle)

    @tasks.loop(minutes=10)
    async def task_purge_expired_games(self):
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            logger.debug(f'Running task_purge_expired_games for guild {guild.name}')
            await game_expiration.purge_expired_games_for_guild(
                bot=self.bot,
                guild=guild,
                as_of=datetime.datetime.now(),
            )


async def setup(bot):
    await bot.add_cog(matchmaking(bot))
