import discord
from discord.ext import commands
import modules.utilities as utilities
import settings
import modules.exceptions as exceptions
import modules.achievements as achievements
from modules import channels
from modules import completed_game_channel_purge
from modules import confirmation_publication
from modules import image_storage
from modules import leaderboard_views
from modules import leaderboard_workers
from modules import leaderboard_v2
from modules import team_leaderboard as team_leaderboard_service
from modules import team_leaderboard_views
from modules import team_leaderboard_workers
from modules import role_leaderboard as role_leaderboard_service
from modules import role_leaderboard_views
from modules import role_leaderboard_workers
from modules import player_views
from modules import player_workers
from modules import player_registration
from modules import player_registration_views
from modules import player_registration_workers
from modules import player_timezone
from modules import player_timezone_workers
from modules import player_timezone_values
from modules import league_inactivity_workers
from modules import legacy_name_workers
from modules import channel_reference_workers
from modules import member_identity_workers
from modules import member_join_workers
from modules import member_removal_workers
from modules import elo_workers
from modules import game_result_publication
from modules import interaction_lifecycle
from modules import interaction_bans
from modules import game_unwin
from modules import game_win
from modules import game_map
from modules import game_side
from modules import game_tribe
from modules import game_tribe_views
from modules import game_notes
from modules import game_notes_views
from modules import game_name
from modules import game_name_views
from modules import game_ping
from modules import game_ping_views
from modules import game_ping_workers
from modules import game_logs
from modules import game_logs_views
from modules import game_log_workers
from modules import game_workers
from modules import game_open
from modules import game_open_workers
from modules import game_open_views
from modules import game_record_views
from modules import game_search_views
from modules import game_search_workers
from modules import game_detail_views
from modules import game_detail_workers
from modules import game_detail_actions
from modules import team_show as team_show_service
from modules import team_show_workers
from modules import squad_show as squad_show_service
from modules import squad_show_views
from modules import squad_show_workers
from modules import squad_identity
from modules import squad_identity_workers
from modules import game_deletion
from modules import game_join_leave
from modules import game_join_workers
from modules import game_kick_workers
from modules import game_keep_active
from modules import game_keep_active_workers
from modules import game_keep_active_views
from modules import game_start, game_start_workers
from modules.elo_jobs import EloJobConflict
import peewee
import modules.models as models
from modules.models import Game, db, Player, Team, DiscordMember, Squad, GameSide, Tribe, Lineup
from modules.league import auto_grad_novas, refresh_league_team_channels, get_team_leadership
import modules.league as league
from itertools import groupby
import logging
import datetime
import asyncio
import re
from typing import Literal

logger = logging.getLogger('polybot.' + __name__)
elo_logger = logging.getLogger('polybot.elo')


GAME_SEARCH_VIEW_CHOICES = [
    discord.app_commands.Choice(name='All games', value='all'),
    discord.app_commands.Choice(
        name='Joinable for me',
        value='joinable',
    ),
    discord.app_commands.Choice(name='All open', value='all-open'),
    discord.app_commands.Choice(
        name='Waiting to start',
        value='waiting',
    ),
    discord.app_commands.Choice(name='My open games', value='mine'),
    discord.app_commands.Choice(name='Active games', value='active'),
    discord.app_commands.Choice(
        name='Completed games',
        value='completed',
    ),
    discord.app_commands.Choice(
        name='Unconfirmed results',
        value='unconfirmed',
    ),
]

GAME_MAP_TYPE_CHOICES = [
    discord.app_commands.Choice(name=map_type, value=map_type)
    for map_type in settings.map_types
]


class PolyGame(commands.Converter):
    """Parse a retained prefix game ID without touching the ORM."""

    async def convert(self, ctx, game_id):
        try:
            parsed_game_id = int(game_id)
        except (TypeError, ValueError):
            await ctx.send(f'Invalid game ID "{game_id}".')
            raise commands.UserInputError()
        if not -(2 ** 31) <= parsed_game_id < 2 ** 31:
            await ctx.send(f'Invalid game ID "{game_id}".')
            raise commands.UserInputError()
        return parsed_game_id


class NewGameRosterError(ValueError):
    """User-facing roster resolution or permission failure."""


_NEWGAME_DESTINATION_UNSET = object()


def _resolve_newgame_public_destination(ctx, destination):
    """Return a sender for committed new-game effects.

    Prefix invocations deliberately default to their existing context. Native
    record confirmations must pass a real channel explicitly so a deferred
    ephemeral interaction webhook cannot inherit its private visibility.
    """

    destination = (
        ctx
        if destination is _NEWGAME_DESTINATION_UNSET
        else destination
    )
    if destination is None or not callable(getattr(destination, 'send', None)):
        raise RuntimeError(
            'No public channel destination is available for committed '
            'new-game effects.'
        )
    return destination


async def resolve_newgame_roster(ctx, args, *, ranked_flag):
    """Resolve the shared prefix/slash roster grammar to Discord members."""

    if len(args) == 1:
        args_list = [str(ctx.author.id), 'vs', args[0]]
    else:
        args_list = list(args)

    player_groups = [
        list(group)
        for is_separator, group in groupby(
            args_list,
            lambda value: value.lower() in ('vs', 'versus'),
        )
        if not is_separator
    ]
    total_players = sum(map(len, player_groups))
    game_allowed, join_error_message = settings.can_user_join_game(
        user_level=settings.get_user_level(ctx.author),
        game_size=total_players,
        is_ranked=ranked_flag,
        is_host=True,
    )
    if not game_allowed:
        raise NewGameRosterError(join_error_message)

    discord_groups = []
    author_found = False
    for group in player_groups:
        discord_group = []
        for player_argument in group:
            guild_matches = await utilities.get_guild_member(
                ctx,
                player_argument,
            )
            if len(guild_matches) == 0:
                raise NewGameRosterError(
                    f'Could not match “{player_argument}” to a server '
                    'member. Try using an @mention.'
                )
            if len(guild_matches) > 1:
                raise NewGameRosterError(
                    f'More than one server match was found for '
                    f'“{player_argument}”. Use an @mention.'
                )
            member = guild_matches[0]
            if member == ctx.author:
                author_found = True
            discord_group.append(member)
        discord_groups.append(discord_group)

    if not author_found and not settings.is_staff(ctx.author):
        raise NewGameRosterError(
            'You cannot record a game that you are not participating in.'
        )
    return tuple(tuple(group) for group in discord_groups)


class polygames(commands.Cog):
    game_group = discord.app_commands.Group(
        name='game',
        description='Create, manage, and correct games.',
        guild_only=True,
    )
    game_manage_group = discord.app_commands.Group(
        name='manage',
        description='Manage pending-game membership and lifecycle.',
        parent=game_group,
        guild_only=True,
    )
    game_result_group = discord.app_commands.Group(
        name='result',
        description='Review or correct reported game results.',
        parent=game_group,
        guild_only=True,
    )
    leaderboard_group = discord.app_commands.Group(
        name='leaderboard',
        description='View competitive rankings and activity.',
        guild_only=True,
    )
    player_group = discord.app_commands.Group(
        name='player',
        description='View and manage player profiles.',
        guild_only=True,
    )
    squad_group = discord.app_commands.Group(
        name='squad',
        description='Find squads and view dense squad cards.',
        guild_only=True,
    )

    def __init__(self, bot):
        self.bot = bot
        game_keep_active_views.register_dynamic_item(bot)
        if settings.run_tasks:
            self.bg_task = asyncio.create_task(self.task_purge_game_channels())
            self.bg_task2 = asyncio.create_task(self.task_set_champion_role())

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author == self.bot.user:
            return

        if message.role_mentions and discord.utils.get(message.role_mentions, name='ELO-Helper'):
            await message.channel.send(f'{message.author.mention}, to receive staff help in the future please use `/staffhelp`, '
                '- since you have already pinged please wait for a response.')

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        try:
            result = await channel_reference_workers.run_channel_reference_cleanup(
                channel_reference_workers.ChannelDeleteRequest(
                    channel_id=int(channel.id),
                    guild_id=int(channel.guild.id),
                    channel_name=str(channel.name),
                )
            )
        except Exception:
            logger.exception(
                'Deleted-channel database reconciliation failed for guild %s '
                'channel %s %s',
                channel.guild.id,
                channel.id,
                channel.name,
            )
            return
        if result.cleared_side_count:
            logger.debug(
                'on_guild_channel_delete: detected deletion of game-side '
                'channel %s %s and removed %s reference(s) from the database',
                channel.id,
                channel.name,
                result.cleared_side_count,
            )
        if result.cleared_game_count:
            logger.debug(
                'on_guild_channel_delete: detected deletion of full-game '
                'channel %s %s and removed %s reference(s) from the database',
                channel.id,
                channel.name,
                result.cleared_game_count,
            )

    @commands.Cog.listener()
    async def on_member_join(self, member):
        try:
            result = await member_join_workers.run_member_join(
                member_join_workers.MemberJoinRequest(
                    guild_id=int(member.guild.id),
                    member_id=int(member.id),
                    discord_name=str(member.name),
                    discord_nick=(
                        str(member.nick) if member.nick is not None else None
                    ),
                )
            )
        except Exception:
            logger.exception(
                'Member-join database reconciliation failed for guild %s '
                'member %s',
                member.guild.id,
                member.id,
            )
            return

        if not result.registered:
            logger.debug(
                'on_member_join: %s joined guild %s but does not have an '
                'existing DiscordMember record.',
                member.display_name,
                member.guild.name,
            )
            return
        if result.local_player_created:
            logger.debug(
                'on_member_join: %s joined guild %s and Player was upserted '
                'as an existing DiscordMember.',
                member.display_name,
                member.guild.name,
            )
        logger.debug(
            'on_member_join: %s re-joined guild %s and has an existing '
            'Player entry.',
            member.display_name,
            member.guild.name,
        )

        async def fix_channel_perm(channel):
            try:
                await channels.add_member_to_channel(channel, member)
                logger.info(f'Re-adding {member.display_name} to channel {channel.id} {channel.name}')
                await channel.send(f'{member.mention} has been added back to this channel after rejoining the server. :partying_face:')
            except (discord.errors.Forbidden, discord.errors.HTTPException) as e:
                logger.warning(
                    'Tried to re-add %s to channel %s %s but got error: %s',
                    member.display_name,
                    channel.id,
                    channel.name,
                    e,
                )

        logger.debug(
            'on_member_join: %s existing side channel target(s)',
            len(result.side_channels),
        )
        for target in result.side_channels:
            logger.debug(
                'on_member_join: attempting to get_channel %s for game %s '
                '(side_channels)',
                target.channel_id,
                target.game_id,
            )
            channel = self.bot.get_channel(target.channel_id)
            if not channel:
                logger.debug('no channel found')
                continue
            elif channel.guild.id != member.guild.id:
                logger.debug('channel.guild.id != member.guild.id')
                continue
            await fix_channel_perm(channel)

        logger.debug(
            'on_member_join: %s existing game channel target(s)',
            len(result.game_channels),
        )
        for target in result.game_channels:
            logger.debug(
                'on_member_join: attempting to get_channel %s for game %s '
                '(game_channels)',
                target.channel_id,
                target.game_id,
            )
            channel = self.bot.get_channel(target.channel_id)
            if not channel:
                logger.debug('no channel found')
                continue
            elif channel.guild.id != member.guild.id:
                logger.debug('channel.guild.id != member.guild.id')
                continue
            await fix_channel_perm(channel)

        for missing in result.missing_side_channels:
            if missing.game.notes and 'live' in missing.game.notes.lower():
                logger.debug(
                    'Skipping channel recreation for live game %s',
                    missing.game.id,
                )
                continue
            target_guild = (
                discord.utils.get(
                    self.bot.guilds,
                    id=missing.preferred_guild_id,
                )
                if missing.preferred_guild_id else None
            )
            using_team_server = bool(
                target_guild is not None
                and not missing.force_pcplus_guild
                and int(target_guild.id) != int(missing.game.guild_id)
            )
            if target_guild is None:
                if missing.force_pcplus_guild:
                    logger.warning(
                        'Could not recreate game %s side %s because the '
                        'configured PCPLUS guild is unavailable.',
                        missing.game.id,
                        missing.gameside_id,
                    )
                    continue
                target_guild = member.guild
                using_team_server = False
            if (
                len(member.guild.text_channels) > 460
                and len(missing.players) < 3
                and not using_team_server
                and 'Nova' not in missing.team_name
            ):
                logger.warning(
                    'Skipping re-creation of 2-player side channel for game '
                    '%s because guild %s has %s text channels.',
                    missing.game.id,
                    member.guild.id,
                    len(member.guild.text_channels),
                )
                continue

            logger.debug(
                'on_member_join: recreating missing side channel for game %s '
                'side %s',
                missing.game.id,
                missing.gameside_id,
            )
            try:
                channel = await channels.create_game_channel(
                    target_guild,
                    game=missing.game,
                    team_name=missing.team_name,
                    player_list=missing.players,
                    using_team_server_flag=using_team_server,
                )
            except exceptions.MyBaseException as e:
                logger.warning(f'Channel creation error: {e}')
                continue
            if channel is None:
                continue
            try:
                await member_join_workers.run_persist_side_channel(
                    member_join_workers.PersistSideChannelRequest(
                        game_id=missing.game.id,
                        gameside_id=missing.gameside_id,
                        channel_id=int(channel.id),
                        channel_guild_id=int(target_guild.id),
                    )
                )
            except member_join_workers.MemberJoinConflictError:
                logger.warning(
                    'A concurrent member-join reconciliation claimed game '
                    '%s side %s; deleting duplicate channel %s.',
                    missing.game.id,
                    missing.gameside_id,
                    channel.id,
                )
                try:
                    await channel.delete(
                        reason='Duplicate member-join channel reconciliation',
                    )
                except discord.DiscordException:
                    logger.exception(
                        'Could not remove duplicate channel %s for game %s '
                        'side %s.',
                        channel.id,
                        missing.game.id,
                        missing.gameside_id,
                    )
                continue
            except Exception:
                logger.exception(
                    'Could not persist recreated channel %s for game %s side '
                    '%s; attempting compensation.',
                    channel.id,
                    missing.game.id,
                    missing.gameside_id,
                )
                try:
                    await channel.delete(
                        reason='Failed member-join channel reconciliation',
                    )
                except discord.DiscordException:
                    logger.exception(
                        'Could not compensate channel %s after persistence '
                        'failure.',
                        channel.id,
                    )
                continue
            await channels.greet_game_channel(
                target_guild,
                chan=channel,
                player_list=missing.players,
                roster_names=missing.roster_names,
                game=missing.game,
                full_game=False,
            )


    @commands.Cog.listener()
    async def on_member_remove(self, member):
        try:
            result = await member_removal_workers.run_member_removal(
                member_removal_workers.MemberRemovalRequest(
                    guild_id=int(member.guild.id),
                    member_id=int(member.id),
                    member_description=models.GameLog.member_string(member),
                )
            )
        except Exception:
            logger.exception(
                'Member-removal database cleanup failed for guild %s member %s',
                member.guild.id,
                member.id,
            )
            return

        if not result.registered:
            return
        if result.deleted_pending_count:
            logger.info(
                'Existing ELO player %s %s left guild %s - deleted %s '
                'pending-game Lineup record(s).',
                member.display_name,
                member.id,
                member.guild.name,
                result.deleted_pending_count,
            )

        if (
            result.incomplete_count
            and member.guild.id == settings.server_ids['polychampions']
        ):
            helper_role = settings.resolve_configured_role(
                member.guild,
                'helper_roles',
            )
            helper_mention = helper_role.mention if helper_role else 'Staff'
            try:
                await utilities.send_to_log_channel(
                    member.guild,
                    f'{helper_mention} - {member.mention} '
                    f'({member.display_name}) left the server and has '
                    f'{result.incomplete_count} incomplete games.',
                )
            except Exception:
                logger.exception(
                    'Committed member-removal cleanup could not notify staff '
                    'for guild %s member %s',
                    member.guild.id,
                    member.id,
                )

    @commands.Cog.listener()
    async def on_user_update(self, before, after):
        if before.name != after.name:
            try:
                result = await member_identity_workers.run_username_update(
                    member_identity_workers.UsernameUpdateRequest(
                        discord_id=int(after.id),
                        before_name=str(before.name),
                        after_name=str(after.name),
                        stored_name=utilities.escape_role_mentions(after.name),
                        member_description=models.GameLog.member_string(after),
                    )
                )
            except Exception:
                logger.exception(
                    'Could not persist username change for member %s',
                    after.id,
                )
                return
            if result.registered:
                logger.debug(
                    'Updated username metadata for member %s across %s Player '
                    'record(s)',
                    after.id,
                    len(result.updated_player_ids),
                )

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        banned_role = discord.utils.get(before.guild.roles, name='ELO Banned')
        banned_applied = (
            banned_role is not None
            and banned_role not in before.roles
            and banned_role in after.roles
        )
        banned_removed = (
            banned_role is not None
            and banned_role in before.roles
            and banned_role not in after.roles
        )
        if banned_applied or banned_removed:
            try:
                ban_result = await member_identity_workers.run_elo_ban_update(
                    member_identity_workers.EloBanUpdateRequest(
                        guild_id=int(after.guild.id),
                        member_id=int(after.id),
                        is_banned=banned_applied,
                        member_description=models.GameLog.member_string(after),
                    )
                )
            except Exception:
                logger.exception(
                    'Could not persist ELO Ban role change for guild %s '
                    'member %s',
                    after.guild.id,
                    after.id,
                )
                return
            if not ban_result.registered:
                return
            logger.info(
                'ELO Ban %s for player %s',
                'added' if banned_applied else 'removed',
                ban_result.player_id,
            )

        inactive_role = settings.resolve_configured_role(
            before.guild,
            'inactive_role',
        )
        inactive_applied = (
            inactive_role is not None
            and inactive_role not in before.roles
            and inactive_role in after.roles
        )
        inactive_removed = (
            inactive_role is not None
            and inactive_role in before.roles
            and inactive_role not in after.roles
        )
        if inactive_applied or inactive_removed:
            try:
                recorded = await league_inactivity_workers.record_inactive_role_change(
                    league_inactivity_workers.InactiveRoleAuditRequest(
                        guild_id=int(after.guild.id),
                        member_id=int(after.id),
                        role_name=str(inactive_role.name),
                        applied=inactive_applied,
                    )
                )
            except Exception:
                logger.exception(
                    'Could not record Inactive role change for guild %s member %s',
                    after.guild.id,
                    after.id,
                )
            else:
                if recorded is not None:
                    logger.info(
                        'Inactive role %s for member %s in guild %s',
                        'added' if inactive_applied else 'removed',
                        after.id,
                        after.guild.id,
                    )

        # Updates display name in DB if user changes their discord name or guild nick
        if before.nick == after.nick and before.name == after.name:
            return

        if before.nick != after.nick:
            try:
                nickname_result = (
                    await member_identity_workers.run_nickname_update(
                        member_identity_workers.NicknameUpdateRequest(
                            guild_id=int(after.guild.id),
                            member_id=int(after.id),
                            before_nick=before.nick,
                            after_name=str(after.name),
                            after_nick=after.nick,
                            member_description=(
                                models.GameLog.member_string(after)
                            ),
                        )
                    )
                )
            except Exception:
                logger.exception(
                    'Could not persist nickname change for guild %s member %s',
                    after.guild.id,
                    after.id,
                )
                return
            if nickname_result.registered:
                logger.debug(
                    'Updated nickname metadata for guild %s player %s',
                    after.guild.id,
                    nickname_result.player_id,
                )

    @staticmethod
    def _player_leaderboard_request(
        guild_id: int,
        invoked_with: str,
        filters: str = '',
    ) -> leaderboard_workers.PlayerLeaderboardRequest:
        filter_text = filters.upper()
        global_alias = invoked_with in ('lbglobal', 'lbg')
        return leaderboard_workers.PlayerLeaderboardRequest(
            guild_id=guild_id,
            scope=(
                'global'
                if global_alias or 'GLOBAL' in filter_text
                else 'local'
            ),
            rating='peak' if 'MAX' in filter_text else 'current',
            era=(
                'all-time'
                if 'ALLTIME' in filter_text
                else 'current'
            ),
            population=(
                'all'
                if 'ALLPLAYERS' in filter_text
                else 'active'
            ),
            active_cutoff=settings.date_cutoff,
        )

    @staticmethod
    def _player_leaderboard_entries(
        result: leaderboard_workers.PlayerLeaderboardResult,
    ) -> list[tuple[str, str]]:
        return [
            (
                f'{row.rank:>3}. {row.team_emoji}{row.name}',
                (
                    f'`ELO {row.elo}\u00a0\u00a0\u00a0\u00a0'
                    f'W {row.wins} / L {row.losses}`'
                ),
            )
            for row in result.rows
        ]

    async def _load_player_leaderboard(
        self,
        request: leaderboard_workers.PlayerLeaderboardRequest,
    ) -> leaderboard_workers.PlayerLeaderboardResult:
        return await leaderboard_workers.run_player_leaderboard(request)

    @staticmethod
    def _activity_leaderboard_request(
        guild_id: int,
        invoked_with: str,
    ) -> leaderboard_workers.ActivityLeaderboardRequest:
        return leaderboard_workers.ActivityLeaderboardRequest(
            guild_id=guild_id,
            view=(
                'global-all-time'
                if invoked_with == 'lbactivealltime'
                else 'server-30-days'
            ),
            recent_cutoff=(
                datetime.datetime.now()
                - datetime.timedelta(days=30)
            ),
        )

    @staticmethod
    def _activity_leaderboard_entries(
        result: leaderboard_workers.ActivityLeaderboardResult,
    ) -> list[tuple[str, str]]:
        count_label = (
            'Games Played'
            if result.view == 'global-all-time'
            else 'Recent Games'
        )
        return [
            (
                f'{row.rank:>3}. {row.team_emoji}{row.name}',
                (
                    f'`ELO {row.elo}\u00a0\u00a0\u00a0\u00a0'
                    f'{count_label} {row.games}`'
                ),
            )
            for row in result.rows
        ]

    async def _load_activity_leaderboard(
        self,
        request: leaderboard_workers.ActivityLeaderboardRequest,
    ) -> leaderboard_workers.ActivityLeaderboardResult:
        return await leaderboard_workers.run_activity_leaderboard(request)

    @staticmethod
    def _squad_leaderboard_request(
        guild_id: int,
        filters: str = '',
    ) -> leaderboard_workers.SquadLeaderboardRequest:
        return leaderboard_workers.SquadLeaderboardRequest(
            guild_id=guild_id,
            period=(
                'all-time'
                if 'ALLTIME' in filters.upper()
                else 'current'
            ),
            active_cutoff=settings.date_cutoff,
        )

    @staticmethod
    def _squad_leaderboard_entries(
        result: leaderboard_workers.SquadLeaderboardResult,
    ) -> list[tuple[str, str]]:
        entries = []
        for row in result.rows:
            squad_name = f'{row.squad_name}\n' if row.squad_name else ''
            emojis = ' '.join(row.member_emojis)
            member_names = ' / '.join(row.member_names)
            entries.append(
                (
                    (
                        f'{row.rank:>3}. {squad_name}'
                        f'{emojis}{member_names}'
                    ),
                    (
                        f'`#{row.squad_id} (ELO: {row.elo:4}) '
                        f'W {row.wins} / L {row.losses}`'
                    ),
                )
            )
        return entries

    async def _load_squad_leaderboard(
        self,
        request: leaderboard_workers.SquadLeaderboardRequest,
    ) -> leaderboard_workers.SquadLeaderboardResult:
        return await leaderboard_workers.run_squad_leaderboard(request)

    @settings.in_bot_channel_strict()
    @commands.command(
        aliases=['leaderboard', 'leaderboards', 'lbglobal', 'lbg'],
    )
    @commands.cooldown(2, 30, commands.BucketType.channel)
    async def lb(self, ctx, *, filters: str = ''):
        """ Display individual leaderboard

        Filters available:
        **global**
        Takes into account games played regardless of what server they were logged on.
        A player's global ELO is independent of their local server ELO.
        **max**
        Ranks leaderboard by a player's maximum ELO ever achieved
        **allplayers**
        Includes players who have not played recently. By default the leaderboard drops players who have not played in 365 days.

        Examples:
        `[p]lb` - Default local leaderboard
        `[p]lb global` - Global leaderboard
        `[p]lb max` - Local leaderboard for maximum historic ELO
        `[p]lb allplayers` - Local leaderboard including inactive players
        `[p]lb global max` - Leaderboard of maximum historic *global* ELO

        `[p]lbrecent` - Most active players of the last 30 days
        `[p]lbactivealltime` - Most active players of all time
        """

        """
        Hidden help info for now:

         **alltime**
        Ranks by the permanent Alltime ELO field, which is never reset. The standard ELO field was reset December 1st, 2020 for Moonrise release.

        `[p]lb alltime` - Local leaderboard by Alltime ELO
        `[p]lb alltime max` - Leaderboard of maximum historic Alltime ELO
        `[p]lb alltime global` - Global leaderboard by Alltime ELO
        `[p]lb global alltime allplayers max` - Global leaderboard, including inactive players, ranked by maximum hstoric Alltime ELO
        """

        request = self._player_leaderboard_request(
            guild_id=ctx.guild.id,
            invoked_with=ctx.invoked_with,
            filters=filters,
        )
        async with ctx.typing():
            try:
                result = await self._load_player_leaderboard(request)
            except (peewee.PeeweeException, ValueError) as exc:
                logger.exception('Could not load player leaderboard')
                return await ctx.send(
                    f'Could not load the player leaderboard: {exc}'
                )

        await utilities.paginate(
            self.bot,
            ctx,
            title=(
                f'**{result.title}**\n'
                f'{result.total_ranked} ranked players'
            ),
            message_list=self._player_leaderboard_entries(result),
            page_start=0,
            page_end=10,
            page_size=10,
        )

    @leaderboard_group.command(
        name='players',
        description='Explore individual player rankings.',
    )
    @discord.app_commands.checks.cooldown(
        2,
        30.0,
        key=lambda interaction: interaction.channel_id,
    )
    async def player_leaderboard_slash(
        self,
        interaction: discord.Interaction,
    ):
        """Interactive player leaderboard with cached filters."""

        await interaction.response.defer()
        ctx = await commands.Context.from_interaction(interaction)
        ctx.prefix = settings.guild_setting(
            interaction.guild.id,
            'command_prefix',
        )
        ctx.invoked_with = 'lb'
        if not await self.lb.can_run(ctx):
            return

        request = leaderboard_workers.PlayerLeaderboardRequest(
            guild_id=interaction.guild.id,
            scope='local',
            rating='current',
            era='current',
            population='active',
            active_cutoff=settings.date_cutoff,
        )
        try:
            result = await self._load_player_leaderboard(request)
        except (peewee.PeeweeException, ValueError) as exc:
            logger.exception('Could not load slash player leaderboard')
            return await interaction.followup.send(
                f'Could not load the player leaderboard: {exc}',
                ephemeral=True,
            )

        view = leaderboard_v2.PlayerLeaderboardWorkspace(
            guild_id=interaction.guild.id,
            requester_id=interaction.user.id,
            result=result,
            loader=self._load_player_leaderboard,
            active_cutoff=settings.date_cutoff,
        )
        view.message = await interaction.edit_original_response(view=view)

    @leaderboard_group.command(
        name='teams',
        description='Explore current team ELO rankings.',
    )
    @discord.app_commands.checks.cooldown(
        2,
        30.0,
        key=lambda interaction: interaction.channel_id,
    )
    async def team_leaderboard_slash(
        self,
        interaction: discord.Interaction,
    ):
        """Publish a public, requester-controlled team snapshot."""

        await interaction.response.defer(ephemeral=True)
        access_error = team_leaderboard_service.native_access_error(
            interaction.user,
            interaction.guild.id,
            interaction.channel_id,
        )
        if access_error is not None:
            return await interaction.followup.send(
                access_error,
                ephemeral=True,
            )

        try:
            request = (
                team_leaderboard_service.team_leaderboard_request_for_native(
                    interaction,
                )
            )
            result = await team_leaderboard_workers.run_team_leaderboard(
                request,
            )
            _page, graph = await team_leaderboard_service.render_page_graph(
                result,
                tier_number=None,
                include_archived=False,
                page_index=0,
            )
        except (
            peewee.PeeweeException,
            team_leaderboard_workers.TeamLeaderboardValidationError,
            ValueError,
        ) as exc:
            logger.exception('Could not load slash team leaderboard')
            return await interaction.followup.send(
                f'Could not load the team leaderboard: {exc}',
                ephemeral=True,
            )
        except Exception:
            logger.exception('Could not render slash team leaderboard')
            return await interaction.followup.send(
                'Could not load the team leaderboard. Please try again.',
                ephemeral=True,
            )

        view = team_leaderboard_views.TeamLeaderboardWorkspace(
            requester_id=interaction.user.id,
            result=result,
            tier_choices=team_leaderboard_service.configured_tier_choices(),
            graph=graph,
            timeout=team_leaderboard_service.TEAM_LEADERBOARD_CONTROL_TIMEOUT,
        )
        try:
            channel = await (
                interaction_lifecycle.resolve_public_interaction_channel(
                    interaction
                )
            )
            await interaction.delete_original_response()
            view.message = await channel.send(
                view=view,
                files=view.graph_files(),
            )
        except Exception:
            logger.exception('Could not publish slash team leaderboard')
            return await interaction.followup.send(
                'The team leaderboard was not published publicly. Please '
                'try again.',
                ephemeral=True,
            )

    @leaderboard_group.command(
        name='roles',
        description='Explore player ELO rankings by Discord role.',
    )
    @discord.app_commands.checks.cooldown(
        2,
        30.0,
        key=lambda interaction: interaction.channel_id,
    )
    async def role_leaderboard_slash(
        self,
        interaction: discord.Interaction,
    ):
        """Publish a public, requester-controlled role snapshot."""

        await interaction.response.defer(ephemeral=True)
        guild = getattr(interaction, 'guild', None)
        if guild is None:
            return await interaction.followup.send(
                'This command can only be used in a server.',
                ephemeral=True,
            )
        access_error = role_leaderboard_service.native_access_error(
            interaction.user,
            guild.id,
            interaction.channel_id,
        )
        if access_error is not None:
            return await interaction.followup.send(
                access_error,
                ephemeral=True,
            )

        try:
            request = role_leaderboard_service.request_for_native(
                guild=guild,
            )
            result = await role_leaderboard_workers.run_role_leaderboard(
                request,
            )
        except (
            peewee.PeeweeException,
            role_leaderboard_workers.RoleLeaderboardValidationError,
            ValueError,
        ) as exc:
            logger.exception('Could not load slash role leaderboard')
            return await interaction.followup.send(
                f'Could not load the role leaderboard: {exc}',
                ephemeral=True,
            )
        except Exception:
            logger.exception('Unexpected slash role leaderboard failure')
            return await interaction.followup.send(
                'Could not load the role leaderboard. Please try again.',
                ephemeral=True,
            )

        view = role_leaderboard_views.RoleLeaderboardWorkspace(
            guild_id=guild.id,
            requester_id=interaction.user.id,
            result=result,
            role_snapshots=request.role_snapshots,
            selected_role_ids=request.selected_role_ids,
            selected_role_names=request.selected_role_names,
            match_mode=request.match_mode,
            sort_key=request.sort_key,
            scope=request.scope,
            can_select_roles=(
                role_leaderboard_service.requester_can_select_roles(
                    interaction.user,
                )
            ),
            timeout=role_leaderboard_service.ROLE_LEADERBOARD_CONTROL_TIMEOUT,
        )
        try:
            await squad_show_service.publish_native(interaction, view)
        except Exception:
            logger.exception('Could not publish slash role leaderboard')
            return await interaction.followup.send(
                'Could not publish the role leaderboard. Please try again.',
                ephemeral=True,
            )

    @settings.in_bot_channel_strict()
    @commands.command(aliases=['recent', 'active', 'lbactivealltime'], hidden=True)
    @commands.cooldown(2, 30, commands.BucketType.channel)
    async def lbrecent(self, ctx):
        """ Display most active recent players

        Alternative command is `[p]lbactivealltime`
        """
        request = self._activity_leaderboard_request(
            guild_id=ctx.guild.id,
            invoked_with=ctx.invoked_with,
        )
        async with ctx.typing():
            try:
                result = await self._load_activity_leaderboard(request)
            except (peewee.PeeweeException, ValueError) as exc:
                logger.exception('Could not load activity leaderboard')
                return await ctx.send(
                    f'Could not load the activity leaderboard: {exc}'
                )

        await utilities.paginate(
            self.bot,
            ctx,
            title=(
                f'**{result.title}**\n'
                f'{result.total_players} players'
            ),
            message_list=self._activity_leaderboard_entries(result),
            page_start=0,
            page_end=10,
            page_size=10,
        )

    @leaderboard_group.command(
        name='activity',
        description='View recent server or all-time global game activity.',
    )
    @discord.app_commands.describe(
        view='Choose the complete scope and time window.',
    )
    @discord.app_commands.choices(
        view=[
            discord.app_commands.Choice(
                name='This server — past 30 days',
                value='server-30-days',
            ),
            discord.app_commands.Choice(
                name='Global — all time',
                value='global-all-time',
            ),
        ],
    )
    @discord.app_commands.checks.cooldown(
        2,
        30.0,
        key=lambda interaction: interaction.channel_id,
    )
    async def activity_leaderboard_slash(
        self,
        interaction: discord.Interaction,
        view: str = 'server-30-days',
    ):
        """Typed native activity leaderboard with component pagination."""

        await interaction.response.defer()
        ctx = await commands.Context.from_interaction(interaction)
        ctx.prefix = settings.guild_setting(
            interaction.guild.id,
            'command_prefix',
        )
        ctx.invoked_with = (
            'lbactivealltime'
            if view == 'global-all-time'
            else 'lbrecent'
        )
        if not await self.lbrecent.can_run(ctx):
            return

        request = leaderboard_workers.ActivityLeaderboardRequest(
            guild_id=interaction.guild.id,
            view=view,
            recent_cutoff=(
                datetime.datetime.now()
                - datetime.timedelta(days=30)
            ),
        )
        try:
            result = await self._load_activity_leaderboard(request)
        except (peewee.PeeweeException, ValueError) as exc:
            logger.exception('Could not load slash activity leaderboard')
            return await interaction.followup.send(
                f'Could not load the activity leaderboard: {exc}',
                ephemeral=True,
            )

        view_control = leaderboard_v2.ActivityLeaderboardWorkspace(
            requester_id=interaction.user.id,
            result=result,
        )
        view_control.message = await interaction.edit_original_response(
            view=view_control,
        )

    @settings.in_bot_channel_strict()
    @settings.guild_has_setting(setting_name='allow_teams')
    @commands.command(aliases=['teamlb', 'lbteamjr'])
    @commands.cooldown(2, 30, commands.BucketType.channel)
    async def lbteam(self, ctx, *, arg: str = None):
        """Display the current team leaderboard.

        Examples:
        `[p]lbteam` - Current team leaderboard
        `[p]lbteam silver` - Team leaderboard only including teams in the Silver league tier.
        `[p]lbteam old` - Include old (archived) teams in the leaderboard.
        `[p]lbteamjr` - Display team leaderboard for Junior teams
        """
        args = str(arg).lower().split() if arg else []
        remaining_args = [value for value in args if value != 'old']
        try:
            tier_number, _tier_name, include_archived = (
                team_leaderboard_service.parse_prefix_filters(arg)
            )
        except exceptions.NoMatches:
            invalid_tier = remaining_args[0] if remaining_args else str(arg)
            return await ctx.send(
                f'Could not match "**{invalid_tier}**" to the name or '
                f'number of a League tier. See `{ctx.prefix}help '
                f'{ctx.invoked_with}` for usage examples.'
            )

        request = team_leaderboard_service.team_leaderboard_request_for_prefix(
            ctx=ctx,
            tier_number=tier_number,
            include_archived=include_archived,
        )
        async with ctx.typing():
            try:
                result = await team_leaderboard_workers.run_team_leaderboard(
                    request,
                )
            except (
                peewee.PeeweeException,
                team_leaderboard_workers.TeamLeaderboardValidationError,
                ValueError,
            ) as exc:
                logger.exception('Could not load team leaderboard')
                return await ctx.send(
                    f'Could not load the team leaderboard: {exc}'
                )

        try:
            await team_leaderboard_service.publish_prefix(
                ctx,
                result,
                tier_number=tier_number,
                include_archived=include_archived,
            )
        except Exception:
            logger.exception('Could not render team leaderboard')
            return await ctx.send(
                'Could not render the team leaderboard. Please try again.'
            )

    @settings.in_bot_channel_strict()
    @settings.guild_has_setting(setting_name='allow_teams')
    @commands.command(aliases=['squadlb'])
    @commands.cooldown(2, 20, commands.BucketType.channel)
    async def lbsquad(self, ctx, *, filters: str = ''):
        """Display squad leaderboard

        A squad is any combination of players that have completed at least two games together.
        To set a squad name use `/squad name`.

        **Examples:**
        `[p]lbsquad` - Current leaderboard. Squads who have not played a game in 365 days are not included.
        `[p]lbsquad alltime` - Alltime leaderboard.
        """

        request = self._squad_leaderboard_request(
            guild_id=ctx.guild.id,
            filters=filters,
        )
        async with ctx.typing():
            try:
                result = await self._load_squad_leaderboard(request)
            except (peewee.PeeweeException, ValueError) as exc:
                logger.exception('Could not load squad leaderboard')
                return await ctx.send(
                    f'Could not load the squad leaderboard: {exc}'
                )

        await utilities.paginate(
            self.bot,
            ctx,
            title=(
                f'**{result.title}**\n'
                f'{result.total_squads} ranked squads'
            ),
            message_list=self._squad_leaderboard_entries(result),
            page_start=0,
            page_end=10,
            page_size=10,
        )

    @leaderboard_group.command(
        name='squads',
        description='View current or all-time squad rankings.',
    )
    @discord.app_commands.describe(
        period='Use current eligibility or include all squad history.',
    )
    @discord.app_commands.choices(
        period=[
            discord.app_commands.Choice(
                name='Current eligibility',
                value='current',
            ),
            discord.app_commands.Choice(
                name='All time',
                value='all-time',
            ),
        ],
    )
    @discord.app_commands.checks.cooldown(
        2,
        20.0,
        key=lambda interaction: interaction.channel_id,
    )
    async def squad_leaderboard_slash(
        self,
        interaction: discord.Interaction,
        period: str = 'current',
    ):
        """Typed native squad leaderboard with component pagination."""

        await interaction.response.defer()
        ctx = await commands.Context.from_interaction(interaction)
        ctx.prefix = settings.guild_setting(
            interaction.guild.id,
            'command_prefix',
        )
        ctx.invoked_with = 'lbsquad'
        if not await self.lbsquad.can_run(ctx):
            return

        request = leaderboard_workers.SquadLeaderboardRequest(
            guild_id=interaction.guild.id,
            period=period,
            active_cutoff=settings.date_cutoff,
        )
        try:
            result = await self._load_squad_leaderboard(request)
        except (peewee.PeeweeException, ValueError) as exc:
            logger.exception('Could not load slash squad leaderboard')
            return await interaction.followup.send(
                f'Could not load the squad leaderboard: {exc}',
                ephemeral=True,
            )

        view = leaderboard_views.SquadLeaderboardView(
            result,
            requester_id=interaction.user.id,
        )
        view.message = await interaction.edit_original_response(
            embed=leaderboard_views.squad_leaderboard_embed(result, 0),
            view=view,
        )

    @squad_group.command(
        name='show',
        description='Find squads or open a squad card.',
    )
    @discord.app_commands.describe(
        squad_id='Exact squad ID to view; omit to search by members.',
    )
    @discord.app_commands.checks.cooldown(
        2,
        20.0,
        key=lambda interaction: interaction.channel_id,
    )
    async def squad_show_slash(
        self,
        interaction: discord.Interaction,
        squad_id: int | None = None,
    ):
        """Publish a public, requester-controlled squad snapshot."""

        await interaction.response.defer(ephemeral=True)
        access_error = squad_show_service.native_access_error(
            interaction.user,
            interaction.guild.id,
            interaction.channel_id,
        )
        if access_error is not None:
            return await interaction.followup.send(
                access_error,
                ephemeral=True,
            )

        try:
            request = squad_show_service.build_request(
                member=interaction.user,
                guild=interaction.guild,
                squad_id=squad_id,
                channel_id=interaction.channel_id,
            )
            result = await squad_show_workers.run_squad_show(request)
        except (
            squad_show_workers.SquadShowValidationError,
            squad_show_workers.SquadShowLookupError,
            peewee.PeeweeException,
            ValueError,
        ) as exc:
            logger.exception('Could not load slash squad show')
            return await interaction.followup.send(
                str(exc),
                ephemeral=True,
            )
        except Exception:
            logger.exception('Unexpected slash squad-show failure')
            return await interaction.followup.send(
                'Could not load the squad workspace. Please run `/squad show` '
                'again.',
                ephemeral=True,
            )

        if not result.cards:
            return await interaction.followup.send(
                'No eligible squads matched those members.',
                ephemeral=True,
            )

        async def load_members(member_ids):
            member_request = squad_show_service.build_request(
                member=interaction.user,
                guild=interaction.guild,
                member_ids=tuple(member_ids),
                channel_id=interaction.channel_id,
            )
            return await squad_show_workers.run_squad_show(member_request)

        async def mutate_name(modal_interaction, card, name, clear):
            await self._execute_squad_name_mutation(
                modal_interaction,
                squad_id=card.squad_id,
                name=name,
                clear=clear,
                expected_name=card.squad_name,
                captured_can_edit=card.can_edit_name,
                workspace=view,
            )

        view = squad_show_views.SquadShowWorkspace(
            requester_id=interaction.user.id,
            result=result,
            member_loader=load_members,
            name_mutator=mutate_name,
            timeout=squad_show_service.SQUAD_SHOW_CONTROL_TIMEOUT,
        )
        try:
            await squad_show_service.publish_native(interaction, view)
        except Exception:
            logger.exception('Could not publish slash squad-show workspace')
            return await interaction.followup.send(
                'Could not publish the squad workspace. Please run '
                '`/squad show` again.',
                ephemeral=True,
            )

    async def _execute_squad_name_mutation(
        self,
        interaction: discord.Interaction,
        *,
        squad_id: int,
        name: str | None,
        clear: bool,
        expected_name: str | None = None,
        captured_can_edit: bool = False,
        workspace=None,
    ):
        """Run one shared worker write and all post-commit publication."""

        access_error = squad_show_service.native_access_error(
            interaction.user,
            interaction.guild.id,
            interaction.channel_id,
        )
        if access_error is not None:
            await interaction.followup.send(access_error, ephemeral=True)
            return None

        try:
            request = squad_identity.build_mutation_request(
                member=interaction.user,
                guild_id=interaction.guild.id,
                squad_id=squad_id,
                name=name,
                clear=clear,
                expected_name=expected_name,
                captured_can_edit=captured_can_edit,
            )
            result = await squad_identity.run_mutation(request)
        except (
            squad_identity_workers.SquadNameValidationError,
            squad_identity_workers.SquadNameLookupError,
            peewee.PeeweeException,
            ValueError,
        ) as exc:
            logger.exception('Could not mutate slash squad identity')
            await interaction.followup.send(str(exc), ephemeral=True)
            return None
        except Exception:
            logger.exception('Unexpected slash squad identity mutation failure')
            await interaction.followup.send(
                'Could not update the squad name. No public change was made; '
                'run `/squad name` again if the problem persists.',
                ephemeral=True,
            )
            return None

        refresh_failed = False
        if workspace is not None:
            try:
                refreshed_request = squad_show_service.build_request(
                    member=interaction.user,
                    guild=interaction.guild,
                    squad_id=squad_id,
                    channel_id=interaction.channel_id,
                )
                refreshed = await squad_show_workers.run_squad_show(
                    refreshed_request
                )
                if not refreshed.cards:
                    raise squad_show_workers.SquadShowLookupError(
                        'The committed squad card could not be reloaded.'
                    )
                await workspace.apply_refreshed_result(refreshed)
            except Exception:
                refresh_failed = True
                logger.exception(
                    'Committed squad name %s could not refresh its public card',
                    result.squad_id,
                )

        actor = squad_identity.capture_actor(interaction.user)
        message = squad_identity.mutation_message(result, actor=actor)
        if refresh_failed:
            message += '\n' + squad_identity.committed_refresh_warning(result)
        sender = squad_show_service.public_interaction_sender(interaction)
        try:
            await sender(message)
        except Exception:
            logger.exception(
                'Committed squad name %s could not publish public output',
                result.squad_id,
            )
            try:
                await interaction.followup.send(
                    squad_identity.committed_public_warning(result),
                    ephemeral=True,
                )
            except Exception:
                logger.exception(
                    'Could not send squad-name committed warning for %s',
                    result.squad_id,
                )
        return result

    @squad_group.command(
        name='name',
        description='Read or update a squad name.',
    )
    @discord.app_commands.describe(
        squad_id='Exact squad ID to read or update.',
        name='New squad name; omit when explicitly clearing.',
        clear='Explicitly remove the current squad name.',
    )
    @discord.app_commands.checks.cooldown(
        2,
        20.0,
        key=lambda interaction: interaction.channel_id,
    )
    async def squad_name_slash(
        self,
        interaction: discord.Interaction,
        squad_id: int,
        name: str | None = None,
        clear: bool = False,
    ):
        """Read or mutate one guild-scoped squad identity publicly."""

        await interaction.response.defer(ephemeral=True)
        access_error = squad_show_service.native_access_error(
            interaction.user,
            interaction.guild.id,
            interaction.channel_id,
        )
        if access_error is not None:
            return await interaction.followup.send(
                access_error,
                ephemeral=True,
            )

        if name is not None and clear:
            return await interaction.followup.send(
                'Choose either name or clear=true, not both.',
                ephemeral=True,
            )

        if name is None and not clear:
            try:
                request = squad_show_service.build_request(
                    member=interaction.user,
                    guild=interaction.guild,
                    squad_id=squad_id,
                    channel_id=interaction.channel_id,
                )
                result = await squad_show_workers.run_squad_show(request)
            except (
                squad_show_workers.SquadShowValidationError,
                squad_show_workers.SquadShowLookupError,
                peewee.PeeweeException,
                ValueError,
            ) as exc:
                logger.exception('Could not read slash squad identity')
                return await interaction.followup.send(
                    str(exc),
                    ephemeral=True,
                )
            except Exception:
                logger.exception('Unexpected slash squad identity read failure')
                return await interaction.followup.send(
                    'Could not load the squad name. Please run `/squad name` '
                    'again.',
                    ephemeral=True,
                )

            if not result.cards:
                return await interaction.followup.send(
                    'The requested squad could not be loaded.',
                    ephemeral=True,
                )
            actor = squad_identity.capture_actor(interaction.user)
            sender = squad_show_service.public_interaction_sender(interaction)
            try:
                await sender(squad_identity.read_message(result.cards[0], actor=actor))
            except Exception:
                logger.exception(
                    'Could not publish public squad-name read for %s',
                    squad_id,
                )
                await interaction.followup.send(
                    'The squad name was loaded, but the public read could not '
                    'be sent. Run `/squad name` again.',
                    ephemeral=True,
                )
            return

        await self._execute_squad_name_mutation(
            interaction,
            squad_id=squad_id,
            name=name,
            clear=clear,
        )

    async def _load_player_workspace(
        self,
        request: player_workers.PlayerWorkspaceRequest,
    ) -> player_workers.PlayerWorkspaceSnapshot:
        return await player_workers.run_player_workspace(request)

    @staticmethod
    def _game_search_requester_values(member):
        roles = tuple(getattr(member, 'roles', ()) or ())
        try:
            level = settings.get_user_level(member)
        except AttributeError:
            # Lightweight offline interaction doubles do not carry the full
            # Discord.Member surface. Real invocations always do.
            level = 0
        return {
            'requester_level': level,
            'requester_role_ids': tuple(role.id for role in roles),
            'requester_name': getattr(member, 'name', ''),
            'requester_nick': getattr(member, 'nick', None),
            'staff': settings.is_staff(member),
        }

    async def _load_game_search(
        self,
        request: game_search_workers.GameSearchRequest,
    ) -> game_search_workers.GameSearchSnapshot:
        return await asyncio.wait_for(
            game_search_workers.run_game_search(request),
            timeout=20.0,
        )

    async def _load_game_detail(
        self,
        request: game_detail_workers.GameDetailRequest,
    ) -> game_detail_workers.GameDetailSnapshot:
        return await asyncio.wait_for(
            game_detail_workers.run_game_detail(request),
            timeout=20.0,
        )

    def _game_detail_prefix(self, target, guild, *, slash: bool) -> str:
        """Resolve prefix configuration on the event-loop/display side."""

        if not slash:
            target_prefix = getattr(target, 'prefix', None)
            if isinstance(target_prefix, str) and target_prefix:
                return target_prefix
        try:
            return settings.guild_setting(
                guild_id=guild.id,
                setting_name='command_prefix',
            )
        except exceptions.CheckFailedError:
            return settings.guild_setting(
                guild_id=None,
                setting_name='command_prefix',
            )

    def _game_detail_error_message(self, error) -> str:
        if getattr(error, 'code', None) != 'cross_guild_pending':
            return str(error) or 'Could not load that game.'

        source_guild_id = getattr(error, 'source_guild_id', None)
        if source_guild_id is None:
            return str(error) or 'Could not load that game.'
        try:
            server_name = settings.guild_setting(
                guild_id=source_guild_id,
                setting_name='display_name',
            )
        except exceptions.CheckFailedError:
            try:
                server_name = settings.guild_setting(
                    guild_id=None,
                    setting_name='display_name',
                )
            except exceptions.CheckFailedError:
                server_name = f'guild {source_guild_id}'
        return f'{error} __{server_name}__.'

    async def _load_pending_game_card(
        self,
        interaction,
        *,
        guild,
        channel_id: int,
        game_id: int,
        prefix: str,
    ) -> game_detail_actions.PendingGameCardPayload:
        """Reload a card through the bounded immutable game-detail worker."""

        request = game_detail_workers.GameDetailRequest(
            guild_id=guild.id,
            channel_id=channel_id,
            requester_discord_id=interaction.user.id,
            game_id=game_id,
        )
        snapshot = await self._load_game_detail(request)
        display = game_detail_views.resolve_display(
            snapshot,
            guild=guild,
            bot=self.bot,
            prefix=prefix,
            join_emoji=getattr(settings, 'emoji_join_game', ''),
            presentation='slash',
        )
        return game_detail_actions.PendingGameCardPayload(
            snapshot=snapshot,
            rendered=game_detail_views.render_classic_game_detail(display),
        )

    async def _pending_card_join(
        self,
        interaction,
        *,
        game_id: int,
        prefix: str,
        side_arg: str | None,
    ) -> bool:
        """Delegate a card join to the established join worker/presenter."""

        if not await self._native_pending_game_channel_allowed(interaction):
            return False
        matchmaking_cog = self.bot.get_cog('matchmaking')
        if matchmaking_cog is None:
            await interaction.followup.send(
                'The join-game command handler is unavailable.',
                ephemeral=True,
            )
            return False
        try:
            result = await matchmaking_cog.execute_join(
                game_id=game_id,
                member=interaction.user,
                author_member=interaction.user,
                side_arg=side_arg,
                invoked_with='/game show Join',
                notification_member_id=interaction.user.id,
                prefix=prefix,
            )
        except game_join_workers.PendingGameJoinValidationError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return False
        except peewee.PeeweeException:
            logger.exception('Database failure in pending-card join %s', game_id)
            await interaction.followup.send(
                'The game could not be changed because the database operation '
                'failed. No public Discord effects were made.',
                ephemeral=True,
            )
            return False
        except Exception:
            logger.exception('Unexpected failure in pending-card join %s', game_id)
            await interaction.followup.send(
                'The game could not be changed. No public Discord effects were '
                'made.',
                ephemeral=True,
            )
            return False

        await self._publish_native_join_result(
            interaction,
            result,
            member=interaction.user,
            prefix=prefix,
            publish_card=False,
        )
        return True

    async def _publish_native_leave_result(
        self,
        interaction: discord.Interaction,
        result: game_join_workers.LeaveResult,
    ) -> None:
        """Publish an already-committed leave without creating a second card."""

        public_send = lambda content: interaction.followup.send(
            content,
            ephemeral=False,
        )
        if result.host_warning:
            await game_join_leave.send_post_commit_message(
                public_send,
                result.host_warning,
                game_id=result.game_id,
                effect='host-leave warning',
            )
        await game_join_leave.send_post_commit_message(
            public_send,
            result.message,
            game_id=result.game_id,
            effect='leave output',
        )

    async def _pending_card_leave(
        self,
        interaction,
        *,
        game_id: int,
        prefix: str,
    ) -> bool:
        """Delegate a card leave to the established leave worker/presenter."""

        if not await self._native_pending_game_channel_allowed(interaction):
            return False
        matchmaking_cog = self.bot.get_cog('matchmaking')
        if matchmaking_cog is None:
            await interaction.followup.send(
                'The leave-game command handler is unavailable.',
                ephemeral=True,
            )
            return False
        try:
            result = await matchmaking_cog.execute_leave(
                game_id=game_id,
                member=interaction.user,
                author_member=interaction.user,
                invoked_with='/game show Leave',
                prefix=prefix,
            )
        except game_join_workers.PendingGameLeaveValidationError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return False
        except peewee.PeeweeException:
            logger.exception('Database failure in pending-card leave %s', game_id)
            await interaction.followup.send(
                'The game could not be changed because the database operation '
                'failed. No public Discord effects were made.',
                ephemeral=True,
            )
            return False
        except Exception:
            logger.exception('Unexpected failure in pending-card leave %s', game_id)
            await interaction.followup.send(
                'The game could not be changed because the game service failed.',
                ephemeral=True,
            )
            return False

        await self._publish_native_leave_result(interaction, result)
        return True

    async def _pending_card_delete_prepare(
        self,
        interaction,
        *,
        game_id: int,
        prefix: str,
    ) -> bool:
        """Revalidate card deletion authorization before confirmation."""

        if not await self._native_pending_game_channel_allowed(interaction):
            return False
        try:
            request = game_deletion.build_request(
                game_id=game_id,
                member=interaction.user,
                guild_id=interaction.guild.id,
                prefix=prefix,
                invoked_with='/game show Delete',
            )
            classification = await game_deletion.authorize_delete(request)
            if classification.state != game_deletion.game_deletion_workers.PENDING:
                await interaction.followup.send(
                    'This game is no longer pending. Refresh the card or run '
                    '`/game show` again.',
                    ephemeral=True,
                )
                return False
        except game_deletion.GameDeletionValidationError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return False
        except peewee.PeeweeException:
            logger.exception(
                'Database failure preparing pending-card deletion %s',
                game_id,
            )
            await interaction.followup.send(
                'The game could not be deleted because the database operation '
                'failed. No public Discord effects were made.',
                ephemeral=True,
            )
            return False
        except Exception:
            logger.exception(
                'Unexpected failure preparing pending-card deletion %s',
                game_id,
            )
            await interaction.followup.send(
                'The game could not be deleted. No public Discord effects '
                'were made.',
                ephemeral=True,
            )
            return False
        return True

    async def _pending_card_delete(
        self,
        interaction,
        *,
        game_id: int,
        prefix: str,
    ) -> bool:
        """Run the shared deletion service and publish committed effects."""

        if not await self._native_pending_game_channel_allowed(interaction):
            return False
        try:
            request = game_deletion.build_request(
                game_id=game_id,
                member=interaction.user,
                guild_id=interaction.guild.id,
                prefix=prefix,
                invoked_with='/game show Delete',
            )
            result = await game_deletion.delete_game(request)
            await game_deletion.publish_result(
                result,
                send=lambda content: interaction.followup.send(
                    content,
                    ephemeral=False,
                ),
                guild=interaction.guild,
                bot=self.bot,
                prefix=prefix,
            )
        except EloJobConflict as exc:
            active_job = exc.active_job
            await interaction.followup.send(
                f':warning: ELO operation `{active_job.operation}` for game '
                f'`{active_job.game_id or "all"}` is already running. '
                'Please try again later.',
                ephemeral=True,
            )
            return False
        except game_deletion.GameDeletionValidationError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return False
        except peewee.PeeweeException:
            logger.exception(
                'Database failure deleting pending-card game %s',
                game_id,
            )
            await interaction.followup.send(
                'Game deletion failed and rolled back. No Discord channel '
                'updates were made.',
                ephemeral=True,
            )
            return False
        except Exception:
            logger.exception(
                'Unexpected failure deleting pending-card game %s',
                game_id,
            )
            await interaction.followup.send(
                'Game deletion failed. No Discord channel updates were made.',
                ephemeral=True,
            )
            return False
        return True

    async def _pending_card_winner(
        self,
        interaction,
        *,
        game_id: int,
        prefix: str,
        winning_side_id: int,
        winner_label: str,
    ) -> bool:
        """Route a card winner claim through the shared win application service."""

        if not await self._native_winner_game_channel_allowed(interaction):
            return False

        try:
            request = game_win.build_request(
                game_id=game_id,
                member=interaction.user,
                guild_id=interaction.guild.id,
                prefix=prefix,
                winner_text=winner_label,
                winning_side_id=winning_side_id,
                invoked_with='/game show Declare Winner',
            )

            async def public_send(content):
                await interaction.followup.send(content, ephemeral=False)

            async def error_send(content):
                await interaction.followup.send(content, ephemeral=True)

            result = await game_win.run_win(
                request,
                guild=interaction.guild,
                current_channel=getattr(interaction, 'channel', None),
                send_public=public_send,
                send_error=error_send,
                post_win_publisher=post_win_messaging,
                acknowledged=True,
            )
        except peewee.PeeweeException:
            logger.exception(
                'Database failure in in-progress card winner %s',
                game_id,
            )
            await interaction.followup.send(
                'The winner could not be recorded because the database '
                'operation failed. No public Discord effects were made.',
                ephemeral=True,
            )
            return False
        except Exception:
            logger.exception(
                'Unexpected failure in in-progress card winner %s',
                game_id,
            )
            await interaction.followup.send(
                'The winner could not be recorded. No public Discord effects '
                'were made.',
                ephemeral=True,
            )
            return False
        return bool(
            result is not None
            and getattr(result, 'public_effects_published', False)
        )

    async def _pending_card_start(
        self,
        interaction,
        *,
        guild,
        game_id: int,
        prefix: str,
        name: str,
    ) -> bool:
        """Delegate a card start to the established start worker/presenter."""

        ban_denial = interaction_bans.elo_ban_denial(
            interaction.user,
            configured_discord_ids=settings.discord_id_ban_list,
        )
        if ban_denial is not None:
            await interaction.followup.send(ban_denial, ephemeral=True)
            return False
        if not await self._native_pending_game_channel_allowed(interaction):
            return False
        matchmaking_cog = self.bot.get_cog('matchmaking')
        if matchmaking_cog is None:
            await interaction.followup.send(
                'The start-game command handler is unavailable.',
                ephemeral=True,
            )
            return False
        try:
            result = await matchmaking_cog.execute_start(
                game_id=game_id,
                guild=guild,
                requester=interaction.user,
                name=name,
                prefix=prefix,
                invoked_with='/game show Start',
            )
        except game_start_workers.GameStartValidationError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return False
        except peewee.PeeweeException:
            logger.exception('Database failure in pending-card start %s', game_id)
            await interaction.followup.send(
                'The game could not be started because the database operation '
                'failed. No public Discord effects were made.',
                ephemeral=True,
            )
            return False
        except exceptions.CheckFailedError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return False
        except Exception:
            logger.exception('Unexpected failure in pending-card start %s', game_id)
            await interaction.followup.send(
                'The game could not be started. No public Discord effects were '
                'made.',
                ephemeral=True,
            )
            return False

        await game_start.publish_start_result(
            result,
            output_context=game_start.native_output_context(
                interaction,
                prefix=prefix,
            ),
            guild=guild,
            prefix=prefix,
            bot_guilds=getattr(settings.bot, 'guilds', ()),
            presentation='slash',
        )
        return True

    def _pending_game_card_view(
        self,
        *,
        snapshot: game_detail_workers.GameDetailSnapshot,
        guild,
        channel_id: int,
        prefix: str,
    ) -> game_detail_actions.PendingGameCardView | None:
        if not (
            snapshot.is_pending
            or game_detail_actions.winner_action_eligible(snapshot)
        ):
            return None

        async def load_card(interaction):
            return await self._load_pending_game_card(
                interaction,
                guild=guild,
                channel_id=channel_id,
                game_id=snapshot.game_id,
                prefix=prefix,
            )

        async def on_join(interaction, side_arg):
            return await self._pending_card_join(
                interaction,
                game_id=snapshot.game_id,
                prefix=prefix,
                side_arg=side_arg,
            )

        async def on_leave(interaction):
            return await self._pending_card_leave(
                interaction,
                game_id=snapshot.game_id,
                prefix=prefix,
            )

        async def on_delete_prepare(interaction):
            return await self._pending_card_delete_prepare(
                interaction,
                game_id=snapshot.game_id,
                prefix=prefix,
            )

        async def on_delete(interaction):
            return await self._pending_card_delete(
                interaction,
                game_id=snapshot.game_id,
                prefix=prefix,
            )

        async def on_start(interaction, name):
            return await self._pending_card_start(
                interaction,
                guild=guild,
                game_id=snapshot.game_id,
                prefix=prefix,
                name=name,
            )

        async def on_winner(interaction, winning_side_id, winner_label):
            return await self._pending_card_winner(
                interaction,
                game_id=snapshot.game_id,
                prefix=prefix,
                winning_side_id=winning_side_id,
                winner_label=winner_label,
            )

        return game_detail_actions.PendingGameCardView(
            snapshot=snapshot,
            load_card=load_card,
            on_join=on_join,
            on_leave=on_leave,
            on_start=on_start,
            on_delete_prepare=on_delete_prepare,
            on_delete=on_delete,
            on_winner=on_winner,
        )

    async def _send_game_detail(
        self,
        target,
        *,
        guild,
        requester_id: int,
        channel_id: int,
        game_id: int | None,
        slash: bool = False,
    ) -> bool:
        request = game_detail_workers.GameDetailRequest(
            guild_id=guild.id,
            channel_id=channel_id,
            requester_discord_id=requester_id,
            game_id=game_id,
        )
        try:
            snapshot = await self._load_game_detail(request)
        except (
            game_detail_workers.GameDetailError,
            peewee.PeeweeException,
            asyncio.TimeoutError,
            ValueError,
        ) as exc:
            if isinstance(exc, asyncio.TimeoutError):
                message = 'Game detail lookup timed out. Please try again.'
            else:
                message = self._game_detail_error_message(exc)
            if slash:
                await target.followup.send(message, ephemeral=True)
            else:
                await target.send(message)
            return False
        except Exception:
            logger.exception('Unexpected failure loading game detail')
            message = 'Could not load that game. Please try again.'
            if slash:
                await target.followup.send(message, ephemeral=True)
            else:
                await target.send(message)
            return False

        prefix = self._game_detail_prefix(target, guild, slash=slash)
        display = game_detail_views.resolve_display(
            snapshot,
            guild=guild,
            bot=self.bot,
            prefix=prefix,
            join_emoji=getattr(settings, 'emoji_join_game', ''),
            presentation='slash' if slash else 'prefix',
        )
        classic = game_detail_views.render_classic_game_detail(display)
        kwargs = {
            'embed': classic.embed,
            'content': classic.content,
        }
        file = classic.new_file()
        if file is not None:
            kwargs['file'] = file
        view = (
            self._pending_game_card_view(
                snapshot=snapshot,
                guild=guild,
                channel_id=channel_id,
                prefix=prefix,
            )
            if slash
            else None
        )
        if view is not None:
            kwargs['view'] = view
        if slash:
            message = await target.edit_original_response(**kwargs)
        else:
            message = await target.send(**kwargs)
        if view is not None:
            view.message = message
            if (
                snapshot.is_pending
                and snapshot.status_label != 'Expired open game'
                and snapshot.pending_join_available
            ):
                try:
                    await game_open.add_join_reaction(message)
                except Exception:
                    logger.exception(
                        'Could not seed the join reaction on pending game %s '
                        'card; retain button and prefix fallback',
                        snapshot.game_id,
                    )
        return True

    async def _send_game_search_workspace(
        self,
        ctx,
        *,
        query: str,
        key: game_search_workers.GameSearchKey,
    ) -> bool:
        request_kwargs = {
            'guild_id': ctx.guild.id,
            'requester_discord_id': ctx.author.id,
            'query': query,
            **self._game_search_requester_values(ctx.author),
        }

        async def loader(filter_key):
            return await self._load_game_search(
                game_search_workers.GameSearchRequest(
                    **request_kwargs,
                    key=filter_key,
                )
            )

        try:
            snapshot = await loader(key)
        except (game_search_workers.GameSearchError,
                peewee.PeeweeException,
                asyncio.TimeoutError,
                ValueError) as exc:
            await ctx.send(str(exc) or 'Game search timed out.')
            return False
        view = game_search_views.GameSearchWorkspace(
            requester_id=ctx.author.id,
            initial_result=snapshot,
            loader=loader,
            can_view_unconfirmed=request_kwargs['staff'],
        )
        view.message = await ctx.send(view=view)
        return True

    @staticmethod
    def _player_avatar_url(guild, discord_id: int) -> str:
        """Freeze the current guild avatar URL without crossing into a worker."""

        get_member = getattr(guild, 'get_member', None)
        member = get_member(int(discord_id)) if callable(get_member) else None
        avatar = getattr(member, 'display_avatar', None) if member else None
        if avatar is None:
            return ''
        try:
            return str(avatar.replace(size=512, format='webp'))
        except (AttributeError, TypeError, ValueError):
            return str(avatar)

    async def _send_player_workspace(
        self,
        ctx,
        *,
        request: player_workers.PlayerWorkspaceRequest,
        initial_section: str = 'overview',
        completed_filter: str = 'all',
    ) -> bool:
        try:
            snapshot = await self._load_player_workspace(request)
        except (player_workers.PlayerNotFound,
                player_workers.AmbiguousPlayer,
                peewee.PeeweeException,
                ValueError) as exc:
            await ctx.send(str(exc))
            return False
        view = player_views.PlayerWorkspace(
            requester_id=ctx.author.id,
            snapshot=snapshot,
            initial_section=initial_section,
            completed_filter=completed_filter,
            can_edit=(
                ctx.author.id == snapshot.discord_id
                or settings.is_staff(ctx.author)
            ),
            avatar_url=self._player_avatar_url(ctx.guild, snapshot.discord_id),
        )
        view.message = await ctx.send(view=view)
        return True

    @player_group.command(
        name='show',
        description='Open a player profile and game-history workspace.',
    )
    @discord.app_commands.describe(
        member='Player to view; defaults to you.',
    )
    async def player_show_slash(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ):
        await interaction.response.defer()
        ctx = await commands.Context.from_interaction(interaction)
        ctx.prefix = settings.guild_setting(
            interaction.guild.id,
            'command_prefix',
        )
        ctx.invoked_with = 'player'
        if not await self.player.can_run(ctx):
            return
        target = member or interaction.user
        request = player_workers.PlayerWorkspaceRequest(
            guild_id=interaction.guild.id,
            discord_id=target.id,
            requester_discord_id=interaction.user.id,
        )
        try:
            snapshot = await self._load_player_workspace(request)
        except (player_workers.PlayerNotFound,
                player_workers.AmbiguousPlayer,
                peewee.PeeweeException,
                ValueError) as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        view = player_views.PlayerWorkspace(
            requester_id=interaction.user.id,
            snapshot=snapshot,
            can_edit=(
                interaction.user.id == snapshot.discord_id
                or settings.is_staff(interaction.user)
            ),
            avatar_url=self._player_avatar_url(
                interaction.guild,
                snapshot.discord_id,
            ),
        )
        view.message = await interaction.edit_original_response(view=view)

    @player_group.command(
        name='register',
        description='Register an account-wide canonical Polytopia name.',
    )
    @discord.app_commands.describe(
        member='Member to register; defaults to you. Staff only for others.',
    )
    async def player_register_slash(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ):
        target = member or interaction.user
        if int(target.id) != int(interaction.user.id):
            try:
                allowed = bool(settings.is_staff(interaction.user))
            except Exception:
                allowed = False
            if not allowed:
                return await interaction.response.send_message(
                    'Only server staff can register another member.',
                    ephemeral=True,
                )
        try:
            target_snapshot = player_registration.capture_member_snapshot(
                target,
            )
            modal = player_registration_views.PlayerRegistrationModal(
                guild_id=interaction.guild.id,
                requester_id=interaction.user.id,
                target_snapshot=target_snapshot,
            )
            await interaction.response.send_modal(modal)
        except Exception:
            logger.exception('Could not open player registration modal')
            is_done = getattr(interaction.response, 'is_done', None)
            if not callable(is_done) or not is_done():
                await interaction.response.send_message(
                    'The registration form could not be opened.',
                    ephemeral=True,
                )

    @player_group.command(
        name='timezone',
        description='View or set an account-wide fixed UTC offset.',
    )
    @discord.app_commands.describe(
        member='Member whose preference to view or set; defaults to you.',
        offset='Normalized fixed offset such as UTC-05:00.',
        clear='Clear the fixed offset instead of setting one.',
    )
    @discord.app_commands.autocomplete(
        offset=player_timezone.autocomplete_offsets,
    )
    async def player_timezone_slash(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        offset: str | None = None,
        clear: bool = False,
    ):
        target = member or interaction.user
        try:
            request = player_timezone.build_request(
                actor=interaction.user,
                target=target,
                guild_id=interaction.guild.id,
                offset=offset,
                clear=bool(clear),
                invoked_with='/player timezone',
                native=True,
            )
        except (
            player_timezone.TimezoneValidationError,
            player_timezone.TimezonePermissionError,
            ValueError,
        ) as exc:
            return await interaction.response.send_message(
                str(exc),
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)
        try:
            result = await player_timezone_workers.run_timezone_request(
                request,
            )
        except (
            player_timezone_workers.PlayerTimezoneValidationError,
            player_timezone_workers.PlayerTimezonePermissionError,
            player_timezone_workers.PlayerTimezoneNotFound,
            peewee.PeeweeException,
            ValueError,
        ) as exc:
            return await interaction.followup.send(
                str(exc),
                ephemeral=True,
            )
        except Exception:
            logger.exception('Unexpected player timezone failure')
            return await interaction.followup.send(
                'The timezone preference could not be saved or read.',
                ephemeral=True,
            )

        try:
            await player_timezone.public_interaction_sender(interaction)(
                player_timezone.public_message(request, result),
            )
        except Exception:
            logger.exception(
                'Player timezone committed/read but public output failed'
            )
            try:
                await interaction.followup.send(
                    (
                        'The timezone preference was committed, but the public '
                        'confirmation could not be posted. An operator can '
                        'verify the account preference and audit entry.'
                    )
                    if result.mutated
                    else (
                        'The timezone preference was read, but the public '
                        'result could not be posted. Please run the command '
                        'again.'
                    ),
                    ephemeral=True,
                )
            except Exception:
                logger.exception('Could not send timezone reconciliation')

    @settings.in_bot_channel()
    @commands.command(brief='See details on a player', usage='player_name', aliases=['elo', 'rank'])
    async def player(self, ctx, *, args=None):
        """See your own player card or the card of another player
        This also will find results based on a game-code or in-game name, if set.

        **Examples**
        `[p]player` - See your own player card
        `[p]player Nelluk` - See Nelluk's card
        """
        # The hidden legacy ``alltime`` modifier now deep-links the Ratings
        # section, where both current-era and permanent ratings are visible.
        tokens = args.split() if args else []
        all_time = any(token.lower() == 'alltime' for token in tokens)
        if all_time:
            args = ' '.join(
                token for token in tokens if token.lower() != 'alltime'
            ).strip() or None
        request = player_workers.PlayerWorkspaceRequest(
            guild_id=ctx.guild.id,
            discord_id=ctx.author.id if not args else None,
            player_query=args,
            requester_discord_id=ctx.author.id,
        )
        async with ctx.typing():
            await self._send_player_workspace(
                ctx,
                request=request,
                initial_section='ratings' if all_time else 'overview',
            )

    @settings.in_bot_channel()
    @settings.guild_has_setting(setting_name='allow_teams')
    @commands.command(usage='team_name')
    async def team(self, ctx, *, team_string: str = None):
        """See details on a team
        **Example:**
        `[p]team Ronin`
        `[p]team Ronin completed` - Show count of all completed ranked games for each member of team, rather than default recent game count.
        """

        if not team_string:
            return await ctx.send(
                team_show_service.legacy_no_team_message(prefix=ctx.prefix)
            )

        tokens = str(team_string).split()
        completed_flag = any(token.lower() == 'completed' for token in tokens)
        team_lookup = ' '.join(
            token for token in tokens if token.lower() != 'completed'
        ).strip()
        if not team_lookup:
            return await ctx.send(
                team_show_service.legacy_no_team_message(prefix=ctx.prefix)
            )

        request = team_show_service.build_request(
            member=ctx.author,
            guild=ctx.guild,
            team_lookup=team_lookup,
            activity_mode=(
                team_show_workers.TEAM_ACTIVITY_COMPLETED
                if completed_flag
                else team_show_workers.TEAM_ACTIVITY_RECENT
            ),
            native=False,
            invoked_with=str(ctx.invoked_with or 'team'),
            prefix=str(ctx.prefix),
            channel_id=getattr(getattr(ctx, 'channel', None), 'id', None),
        )
        try:
            async with ctx.typing():
                result = await team_show_service.run(request)
        except team_show_workers.TeamShowLookupError:
            return await ctx.send(
                team_show_service.legacy_lookup_message(
                    team_lookup,
                    prefix=ctx.prefix,
                )
            )
        except team_show_workers.TeamShowPermissionError as exc:
            return await ctx.send(str(exc))
        except peewee.PeeweeException:
            logger.exception('Database failure loading legacy team card')
            return await ctx.send('The team card could not be loaded.')
        except Exception:
            logger.exception('Unexpected failure loading legacy team card')
            return await ctx.send('The team card could not be loaded.')
        return await team_show_service.publish_prefix(ctx, result)

    @commands.command(
        brief='Register an account-wide canonical Polytopia name.',
        usage='[user] canonical_polytopia_name',
        aliases=['steamname', 'setcode'],
    )
    async def setname(self, ctx, *, args=None):
        """Compatibility adapter for the canonical registration service."""

        invoked_with = str(ctx.invoked_with or 'setname').lower()
        if invoked_with in ('steamname', 'setcode'):
            return await ctx.send(
                player_registration.deprecation_message(
                    invoked_with,
                    ctx.prefix,
                )
            )

        raw_args = str(args or '').strip()
        if not raw_args:
            return await ctx.send(
                f'**Usage:** `{ctx.prefix}setname YOUR POLYTOPIA NAME`\n'
                'This is the account-wide canonical Polytopia name. You can '
                'also use `/player register`.'
            )

        parts = raw_args.split(maxsplit=1)
        target_id = utilities.string_to_user_id(parts[0])
        if target_id is not None:
            try:
                is_staff = bool(settings.is_staff(ctx.author))
            except Exception:
                is_staff = False
            if not is_staff:
                return await ctx.send(
                    'You do not have permission to register another player.'
                )
            target_string = str(target_id)
            canonical_name = parts[1] if len(parts) == 2 else ''
        else:
            target_string = str(ctx.author.id)
            canonical_name = raw_args

        guild_matches = await utilities.get_guild_member(ctx, target_string)
        if len(guild_matches) == 0:
            return await ctx.send(
                f'Could not find any server member matching *{parts[0]}*. '
                'Try specifying with an @Mention.'
            )
        if len(guild_matches) > 1:
            return await ctx.send(
                f'Found {len(guild_matches)} server members matching '
                f'*{parts[0]}*. Try specifying with an @Mention.'
            )

        try:
            request = player_registration.build_request(
                actor=ctx.author,
                target=guild_matches[0],
                guild_id=ctx.guild.id,
                canonical_name=canonical_name,
                invoked_with='setname',
            )
            result = await player_registration_workers.run_player_registration(
                request
            )
        except (
            player_registration_workers.PlayerRegistrationValidationError,
            player_registration_workers.PlayerRegistrationPermissionError,
            peewee.PeeweeException,
            ValueError,
        ) as exc:
            logger.warning('Prefix canonical player registration failed: %s', exc)
            return await ctx.send(str(exc))
        except Exception:
            logger.exception('Unexpected prefix canonical player registration failure')
            return await ctx.send(
                'Registration failed before it could be confirmed. Please try '
                'again later.'
            )

        await ctx.send(player_registration.success_message(request, result))

    @commands.command(aliases=['code', 'getcode', 'name'], usage='player_name')
    async def getname(self, ctx, *, player_string: str = None):
        """Return the transitional canonical account-name read."""

        if str(ctx.invoked_with or '').lower() in ('code', 'getcode'):
            await ctx.send(
                ':warning: This legacy code lookup is deprecated. The value '
                'below is the transitional account-wide Polytopia name; '
                'stored legacy codes are preserved.'
            )

        if not player_string:
            player_string = str(ctx.author.id)

        player_string_safe = discord.utils.escape_mentions(player_string)

        guild_matches = await utilities.get_guild_member(ctx, player_string)

        if len(guild_matches) == 0:
            try:
                game_id = int(player_string)
            except ValueError:
                return await ctx.send(f'Could not find any server member matching *{player_string_safe}*. Try specifying with an @Mention')

            return await ctx.send(f'Could not find any server member matching *{player_string_safe}*. For player codes for a game, try `{ctx.prefix}codes {game_id}`')

        elif len(guild_matches) > 1:
            player_match = await legacy_name_workers.run_registered_name_match(
                player_string,
                ctx.guild.id,
            )
            if player_match.match_count == 1:
                player = player_match.player
                account_name = player.account_name
                await ctx.send(
                    f'Found {len(guild_matches)} server members matching '
                    f'*{player_string_safe}*, but only '
                    f'**{player.display_name}** is registered.'
                )
                return await ctx.send(
                    player_registration.safe_public_name(account_name)
                    if account_name else 'No account-wide canonical name set'
                )

            return await ctx.send(f'Found {len(guild_matches)} server members matching *{player_string_safe}*. Try specifying with an @Mention or more characters.')
        target_discord_member = guild_matches[0]

        discord_member = await legacy_name_workers.run_account_name(
            target_discord_member.id
        )

        if discord_member:
            account_name = (
                discord_member.account_name
            )
            await ctx.send(
                f'Account-wide Polytopia name for '
                f'**{discord_member.display_name}**:'
            )
            return await ctx.send(
                player_registration.safe_public_name(account_name)
                if account_name else 'No account-wide canonical name set'
            )
        else:
            return await ctx.send(
                f'Member **{target_discord_member.name}** is not registered.\n'
                f'Register with `{ctx.prefix}setname YOUR POLYTOPIA NAME` or '
                '`/player register`.'
            )

    @commands.command(aliases=['names', 'codes', 'getcodes'], usage='game_id')
    @models.is_registered_member()
    async def getnames(self, ctx, *, arg=''):
        """Print all player names associated with a game ID
        The names will be printed on separate line for ease of copying, and in the order that players should be added to the game.
        **Examples:**
        `[p]getnames 1250` - Get all player codes for players in game 1250
        `[p]names` - Get player names for the game associated with the current channel
        """
        
        if arg:
            try:
                game_id = int(arg)
            except ValueError:
                game_id = None
        else:
            game_id = None

        try:
            snapshot = await legacy_name_workers.run_game_names(
                game_id=game_id if game_id else None,
                channel_id=ctx.message.channel.id,
            )
        except legacy_name_workers.GameNamesLookupError as exc:
            if exc.code == 'channel_lookup':
                logger.error('Could not infer game from channel: %s', exc)
                return await ctx.send(
                    'Game ID not provided and cannot detect a game channel. '
                    f'Usage: __`{ctx.prefix}{ctx.invoked_with} GAME_ID`__'
                )
            if exc.code == 'not_found':
                return await ctx.send(str(exc))
            if exc.code == 'invalid_id':
                return await ctx.send(f'Invalid game ID "{game_id}".')
            return await ctx.send(f'**Error:** {exc}')

        warn_str = '\n*(List may take a few seconds to print due to discord anti-spam measures.)*' if len(snapshot.rows) > 2 else ''
        header_str = (
            f'Polytopia account names for **game {snapshot.game_id}**, in draft order:'
            f'{warn_str}'
        )

        first_loop = True
        async with ctx.typing():
            for row in snapshot.rows:
                account_name = row.account_name
                account_name_display = (
                    f'**{player_registration.safe_public_name(account_name)}**'
                    if account_name else '*No account-wide name set*'
                )
                in_game_name_str = (
                    f' (Polytopia account name: {account_name_display})'
                )

                if first_loop:
                    # header_str combined with first player's name in order to reduce number of ctx.send() that are done.
                    # More than 3-4 and they will drip out due to API rate limits
                    await ctx.send(f'{header_str}\n**{row.player_name}**{in_game_name_str} -- *Creates the game and invites everyone else*')
                    first_loop = False
                else:
                    effective_timezone = row.timezone
                    tz_str = (
                        f'`{effective_timezone}`'
                        if effective_timezone
                        else ''
                    )
                    await ctx.send(f'**{row.player_name}**{in_game_name_str} {tz_str}')
                await ctx.send(
                    player_registration.safe_public_name(account_name)
                    if account_name else 'No account-wide name set'
                )

    @commands.command(brief='Set player time zone', usage='UTC±#')
    @models.is_registered_member()
    async def settime(self, ctx, *args):
        """Sets your own timezone, or lets staff set a player's timezone
        This will be shown on your `[p]player` profile and can be used to order large games for faster player.

        **Examples:**
        `[p]settime UTC-5` - Set your own timezone to UTC-5  *(Eastern Standard Time)*
        `[p]settime Nelluk UTC-5` - Lets staff set in-game name of Nelluk to UTC-5

        *Accepts arguments like: UTC+05:00, GMT-5:30*
        """
        try:
            request = await player_timezone.build_prefix_request(ctx, args)
            result = await player_timezone_workers.run_timezone_request(request)
        except (
            player_timezone.TimezoneValidationError,
            player_timezone.TimezonePermissionError,
            player_timezone.TimezoneTargetError,
            player_timezone_workers.PlayerTimezoneValidationError,
            player_timezone_workers.PlayerTimezonePermissionError,
            player_timezone_workers.PlayerTimezoneNotFound,
            peewee.PeeweeException,
            ValueError,
        ) as exc:
            logger.warning('Prefix timezone preference failed: %s', exc)
            return await ctx.send(str(exc))
        except Exception:
            logger.exception('Unexpected prefix timezone preference failure')
            return await ctx.send(
                'The timezone preference could not be saved. Please try again later.'
            )

        await ctx.send(player_timezone.prefix_success_message(request, result))

    @commands.command(aliases=['match'], usage='game_id')
    async def game(self, ctx, *, game_search: str = None):
        # async def game(self, ctx, game: PolyGame = None):

        """See details on a specific game ID

        If you use something other than a numeric game ID with this command, it is assumed you are trying to use `allgames`, which allows you to search games by player, game name, result, or team. See `[p]help allgames`

        **Examples**:
        `[p]game 1251` - See details on game # 1251.
        """
        if not game_search:
            return await ctx.send(f'Game ID number must be supplied, example: __`{ctx.prefix}game 1250`__')
        if str(game_search).upper() == 'ID':
            await ctx.send(f'Invalid game ID "{game_search}". Use the numeric game ID *only*, example: `{ctx.prefix}{ctx.invoked_with} 1234`')
            raise commands.UserInputError()

        try:
            int(game_search)
        except ValueError:
            # User passed in non-numeric, probably searching by game title
            return await ctx.invoke(self.bot.get_command('allgames'), args=game_search)

        channel = getattr(ctx, 'channel', None)
        channel_id = getattr(channel, 'id', None)
        if channel_id is None and getattr(ctx, 'message', None) is not None:
            channel_id = getattr(ctx.message.channel, 'id', 0)
        return await self._send_game_detail(
            ctx,
            guild=ctx.guild,
            requester_id=ctx.author.id,
            channel_id=channel_id or 0,
            game_id=int(game_search),
        )

    @commands.command(name='keepactive', usage='game_id')
    async def keep_active(self, ctx, game_id: int = None):
        """Keep a started incomplete game active for another 30 days."""

        if game_id is None:
            return await ctx.send(
                f'Game ID number must be supplied, example: '
                f'`{ctx.prefix}keepactive 1250`'
            )
        if not callable(getattr(getattr(ctx, 'channel', None), 'send', None)):
            return await ctx.send(
                'The invocation channel cannot receive the public keep-active notice.'
            )
        try:
            result = await game_keep_active.run(game_keep_active.request(
                game_id=game_id,
                user=ctx.author,
                guild_id=getattr(ctx.guild, 'id', None),
                channel_id=getattr(ctx.channel, 'id', None),
                is_staff=settings.is_staff(ctx.author),
            ))
        except game_keep_active_workers.KeepActiveError as exc:
            return await ctx.send(str(exc))
        except Exception:
            logger.exception('Prefix keep-active failed for game %s', game_id)
            return await ctx.send(
                'The game could not be kept active. No database change was committed.'
            )
        try:
            await ctx.send(game_keep_active.success_message(result))
        except Exception:
            logger.exception(
                'Keep-active for game %s committed, but its prefix notice '
                'failed; reconcile manually and do not retry the mutation.',
                result.game_id,
            )

    @game_group.command(
        name='keep-active',
        description='Keep a started incomplete game active for 30 days.',
    )
    @discord.app_commands.describe(game_id='Started incomplete game to keep active.')
    async def keep_active_slash(self, interaction: discord.Interaction, game_id: int):
        await game_keep_active.run_slash(interaction, game_id=game_id)

    @game_group.command(
        name='show',
        description='Show one game in the standard game card.',
    )
    @discord.app_commands.describe(
        game_id='Game ID; omit it only in an unambiguous game channel.',
    )
    async def game_show_slash(
        self,
        interaction: discord.Interaction,
        game_id: int | None = None,
    ):
        """Show the shared public game-detail card."""

        # The legacy $game command has no bot-channel or registration check;
        # retain that visibility rule while the native group remains guild-only.
        await interaction.response.defer()
        await self._send_game_detail(
            interaction,
            guild=interaction.guild,
            requester_id=interaction.user.id,
            channel_id=interaction.channel_id or 0,
            game_id=game_id,
            slash=True,
        )

    @game_group.command(
        name='logs',
        description='View permission-aware game audit history.',
    )
    @discord.app_commands.describe(
        game_id='Optional game ID; required for non-staff participants.',
    )
    @discord.app_commands.checks.cooldown(
        1,
        10.0,
        key=lambda interaction: interaction.user.id,
    )
    async def game_logs_slash(
        self,
        interaction: discord.Interaction,
        game_id: int | None = None,
    ):
        """Publish a public requester-controlled audit-log workspace."""

        await interaction.response.defer(ephemeral=True)
        access_error = game_logs.native_access_error(
            interaction.user,
            interaction.guild.id,
            interaction.channel_id,
        )
        if access_error:
            return await interaction.followup.send(access_error, ephemeral=True)
        try:
            key = game_logs.initial_key(
                member=interaction.user,
                game_id=game_id,
            )

            async def loader(selected_key):
                request = game_logs.build_request(
                    member=interaction.user,
                    guild_id=interaction.guild.id,
                    key=selected_key,
                )
                try:
                    return await asyncio.wait_for(
                        game_log_workers.run_game_log_read(request),
                        timeout=20.0,
                    )
                except game_log_workers.GameLogReadError:
                    raise
                except asyncio.TimeoutError as exc:
                    raise game_log_workers.GameLogReadError(
                        'The audit logs took too long to load. Please try again.'
                    ) from exc
                except peewee.PeeweeException as exc:
                    logger.exception('Database failure reading native game logs')
                    raise game_log_workers.GameLogReadError(
                        'The audit logs could not be loaded. Please try again later.'
                    ) from exc
                except Exception as exc:
                    logger.exception('Unexpected native game-log read failure')
                    raise game_log_workers.GameLogReadError(
                        'The audit logs could not be loaded. Please try again later.'
                    ) from exc

            snapshot = await loader(key)
            requester_id = int(interaction.user.id)
            permission_snapshot = game_logs.build_request(
                member=interaction.user,
                guild_id=interaction.guild.id,
                key=key,
            )
            view = game_logs_views.GameLogsWorkspace(
                requester_id=requester_id,
                initial_result=snapshot,
                loader=loader,
                requester_is_staff=permission_snapshot.requester_is_staff,
                requester_is_owner=permission_snapshot.requester_is_owner,
                initial_game_id=game_id,
            )
            sender = squad_show_service.public_interaction_sender(interaction)
            view.message = await sender(
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return view
        except game_log_workers.GameLogReadError as exc:
            logger.warning('Native game-log read failed: %s', exc)
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception:
            logger.exception('Unexpected native game-log read failure')
            await interaction.followup.send(
                'The audit logs could not be loaded. Please try again later.',
                ephemeral=True,
            )

    @game_group.command(
        name='ping',
        description='Compose a private game notification for one or more games.',
    )
    @discord.app_commands.checks.cooldown(
        1,
        30.0,
        key=lambda interaction: interaction.user.id,
    )
    @discord.app_commands.describe(
        game_id='Optional incomplete game ID; omit it to infer or choose a game.',
    )
    async def game_ping_slash(
        self,
        interaction: discord.Interaction,
        game_id: int | None = None,
    ):
        """Open the requester-bound Components v2 game-ping composer."""

        await interaction.response.defer(ephemeral=True)
        requester = game_ping.capture_member(
            interaction.user,
            interaction.guild.id,
        )
        channel_id = int(interaction.channel_id or 0)

        async def load_target(target: game_ping_workers.MemberSnapshot):
            request = game_ping_workers.GamePingLoadRequest(
                guild_id=int(interaction.guild.id),
                requester=requester,
                target_id=int(target.discord_id),
                explicit_game_id=game_id,
                channel_id=channel_id,
                discover_all=True,
            )
            loaded = await asyncio.wait_for(
                game_ping_workers.run_ping_candidates(request),
                timeout=20.0,
            )
            facts = game_ping.capture_channel_facts(interaction, loaded)
            return loaded, facts

        try:
            loaded, facts = await load_target(requester)
        except (
            game_ping_workers.GamePingValidationError,
            game_ping_workers.GamePingLookupError,
            game_ping_workers.GamePingPermissionError,
            peewee.PeeweeException,
            asyncio.TimeoutError,
            ValueError,
        ) as exc:
            logger.warning('Native game-ping candidate load failed: %s', exc)
            return await interaction.followup.send(str(exc), ephemeral=True)
        except Exception:
            logger.exception('Unexpected native game-ping candidate load failure')
            return await interaction.followup.send(
                'The game-ping composer could not load the eligible games. '
                'Please try again later.',
                ephemeral=True,
            )

        loaded_ids = {candidate.game_id for candidate in loaded.games}
        selected_game_id = (
            loaded.inferred_game_id
            if loaded.inferred_game_id in loaded_ids
            else game_id if game_id in loaded_ids else None
        )
        if selected_game_id is None and loaded.games:
            selected_game_id = loaded.games[0].game_id

        async def reload_target(
            target_interaction: discord.Interaction,
            target: game_ping_workers.MemberSnapshot,
        ):
            # The interaction is requester-bound; use its current channel only
            # as a primitive lookup fact and never pass the live object to the
            # synchronous worker.
            return await load_target(target)

        async def confirm(
            _confirm_interaction: discord.Interaction,
            view: game_ping_views.GamePingComposerView,
        ):
            if view.draft is None:
                raise game_ping_workers.GamePingValidationError(
                    'Compose a message or attach a file before confirming.'
                )
            request = game_ping.build_commit_request(
                result=view.result,
                requester=requester,
                target=view.target,
                scope=view.scope,
                selected_game_id=view.selected_game_id,
                channel_facts=view.channel_facts,
                draft=view.draft,
                invoked_with='/game ping',
            )
            return await game_ping.confirm_and_deliver(
                request,
                guilds=self.bot.guilds,
                completion_destination=interaction.channel,
            )

        view = game_ping_views.GamePingComposerView(
            requester=requester,
            target=requester,
            result=loaded,
            channel_facts=facts,
            selected_game_id=selected_game_id,
            target_loader=reload_target,
            confirmer=confirm,
        )
        view.message = await interaction.edit_original_response(view=view)

    @game_group.command(
        name='start',
        description='Start a full game after creating it in Polytopia.',
    )
    @discord.app_commands.describe(
        game_id='Full open game to start.',
        name='Exact game name shown in Polytopia.',
    )
    async def game_start_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
        name: str,
    ):
        """Start through the same bounded transition used by the prefix."""

        await interaction.response.defer()
        ban_denial = interaction_bans.elo_ban_denial(
            interaction.user,
            configured_discord_ids=settings.discord_id_ban_list,
        )
        if ban_denial is not None:
            return await interaction.followup.send(
                ban_denial,
                ephemeral=True,
            )
        prefix = settings.guild_setting(
            interaction.guild.id,
            'command_prefix',
        )
        if not await self._native_pending_game_channel_allowed(interaction):
            return

        matchmaking_cog = self.bot.get_cog('matchmaking')
        if matchmaking_cog is None:
            return await interaction.followup.send(
                'The start-game command handler is unavailable.',
                ephemeral=True,
            )
        try:
            result = await matchmaking_cog.execute_start(
                game_id=game_id,
                guild=interaction.guild,
                requester=interaction.user,
                name=name,
                prefix=prefix,
                invoked_with='/game start',
            )
        except game_start_workers.GameStartValidationError as exc:
            return await interaction.followup.send(
                str(exc),
                ephemeral=True,
            )
        except peewee.PeeweeException:
            logger.exception('Database failure in native start %s', game_id)
            return await interaction.followup.send(
                'The game could not be started because the database operation '
                'failed. No public Discord effects were made.',
                ephemeral=True,
            )
        except exceptions.CheckFailedError as exc:
            logger.exception('Start validation failure in native %s', game_id)
            return await interaction.followup.send(str(exc), ephemeral=True)
        except Exception:
            logger.exception('Unexpected failure in native start %s', game_id)
            return await interaction.followup.send(
                'The game could not be started. No public Discord effects were '
                'made.',
                ephemeral=True,
            )

        await game_start.publish_start_result(
            result,
            output_context=game_start.native_output_context(
                interaction,
                prefix=prefix,
            ),
            guild=interaction.guild,
            prefix=prefix,
            bot_guilds=settings.bot.guilds,
            presentation='slash',
        )

    @game_group.command(
        name='open',
        description='Open a game for other players to join.',
    )
    @discord.app_commands.describe(
        size='Game shape, such as 1v1, 1v3, 1v1v1, or 6FFA.',
    )
    async def open_slash(
        self,
        interaction: discord.Interaction,
        size: str,
    ):
        """Open a game through a short native draft and shared worker."""

        await interaction.response.defer(ephemeral=True)
        ctx = await commands.Context.from_interaction(interaction)
        ctx.prefix = settings.guild_setting(
            interaction.guild.id,
            'command_prefix',
        )
        ctx.invoked_with = 'opengame'

        matchmaking_cog = self.bot.get_cog('matchmaking')
        open_command = (
            getattr(matchmaking_cog, 'opengame', None)
            if matchmaking_cog is not None
            else None
        )
        if open_command is None:
            return await interaction.edit_original_response(
                content='The open-game command handler is unavailable.',
            )
        if not await open_command.can_run(ctx):
            return

        try:
            sizes, _ = game_open_workers.parse_game_size_token(size)
        except game_open_workers.OpenGameSizeError as exc:
            return await interaction.edit_original_response(content=str(exc))

        requester = interaction.user
        requester_roles = tuple(getattr(requester, 'roles', ()))
        requester_snapshot = {
            'requester_id': requester.id,
            'requester_name': requester.name,
            'requester_nick': getattr(requester, 'nick', None),
            'requester_role_ids': tuple(role.id for role in requester_roles),
            'requester_role_names': tuple(
                role.name for role in requester_roles
            ),
            'requester_level': settings.get_user_level(requester),
            'requester_is_mod': settings.is_mod(requester),
            'requester_is_staff': settings.is_staff(requester),
            'requester_description': models.GameLog.member_string(requester),
        }
        default_expiration = game_open_workers.default_expiration_hours(
            sum(sizes)
        )
        unranked_channel = settings.guild_setting(
            interaction.guild.id,
            'unranked_game_channel',
        )
        default_ranked = not (
            unranked_channel
            and getattr(interaction, 'channel_id', None) == unranked_channel
        )

        def build_request(
            draft: game_open_views.OpenGameDraft,
        ) -> game_open_workers.OpenGameRequest:
            notes = utilities.escape_everyone_here_roles(
                draft.notes[:150].strip()
            )
            return game_open_workers.OpenGameRequest(
                guild_id=interaction.guild.id,
                requester_id=requester_snapshot['requester_id'],
                requester_name=requester_snapshot['requester_name'],
                requester_nick=requester_snapshot['requester_nick'],
                prefix=ctx.prefix,
                requester_role_ids=requester_snapshot['requester_role_ids'],
                requester_role_names=requester_snapshot[
                    'requester_role_names'
                ],
                requester_level=requester_snapshot['requester_level'],
                requester_is_mod=requester_snapshot['requester_is_mod'],
                requester_is_staff=requester_snapshot['requester_is_staff'],
                sides=tuple(
                    game_open_workers.OpenGameSide(side_size)
                    for side_size in draft.size
                ),
                expiration_hours=draft.expiration_hours,
                is_ranked=draft.ranked,
                is_mobile=True,
                notes=notes,
                notes_display=notes or '\u200b',
                log_notes_display=discord.utils.escape_markdown(
                    notes or '\u200b'
                ),
                requester_description=requester_snapshot[
                    'requester_description'
                ],
                invoked_with='/game open',
            )

        async def confirm_open_game(
            confirmation: discord.Interaction,
            draft: game_open_views.OpenGameDraft,
        ) -> None:
            request = build_request(draft)
            try:
                result = await game_open_workers.run_open_game_creation(
                    request
                )
            except game_open_workers.OpenGameValidationError as exc:
                await confirmation.followup.send(str(exc), ephemeral=True)
                return
            except (peewee.PeeweeException, exceptions.MyBaseException) as exc:
                logger.exception('Error creating native open game')
                await confirmation.followup.send(
                    f'Error opening game: {exc}. No public Discord effects '
                    'were made.',
                    ephemeral=True,
                )
                return
            except Exception:
                logger.exception('Unexpected error creating native open game')
                await confirmation.followup.send(
                    'Error opening game. No public Discord effects were made.',
                    ephemeral=True,
                )
                return

            async def send_public(message: str):
                channel = getattr(confirmation, 'channel', None)
                if channel is None:
                    raise RuntimeError(
                        'The open-game confirmation channel is unavailable.'
                    )
                # A public follow-up to an ephemeral component interaction can
                # render as a reply to a deleted/private message in Discord.
                # Publish committed effects as standalone channel messages.
                return await channel.send(message)

            await game_open.publish_open_game_result(
                result,
                prefix=ctx.prefix,
                send=send_public,
                add_completion_reaction=game_open.add_join_reaction,
                presentation='slash',
            )

        view = game_open_views.OpenGameView(
            requester_id=interaction.user.id,
            draft=game_open_views.OpenGameDraft(
                size=tuple(sizes),
                ranked=default_ranked,
                expiration_hours=default_expiration,
            ),
            confirmer=confirm_open_game,
        )
        view.message = await interaction.edit_original_response(view=view)

    async def _native_pending_game_channel_allowed(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        """Keep the prefix bot-channel restriction with native errors private."""

        bot_channels = settings.guild_setting(
            interaction.guild.id,
            'bot_channels',
        )
        private_channels = settings.guild_setting(
            interaction.guild.id,
            'bot_channels_private',
        ) or []
        if (
            bot_channels is None
            or settings.is_mod(interaction.user)
            or interaction.channel_id in (bot_channels or []) + private_channels
        ):
            return True
        channel_tags = ' '.join(
            f'<#{channel_id}>' for channel_id in (bot_channels or [])
        )
        await interaction.followup.send(
            'This command can only be used in a designated ELO bot channel. '
            f'Try: {channel_tags}' if channel_tags else
            'This command can only be used in a designated ELO bot channel.',
            ephemeral=True,
        )
        return False

    async def _native_winner_game_channel_allowed(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        """Mirror the strict channel check on prefix and slash win commands."""

        strict_channels = settings.guild_setting(
            interaction.guild.id,
            'bot_channels_strict',
        )
        if strict_channels is None:
            strict_channels = settings.guild_setting(
                interaction.guild.id,
                'bot_channels',
            )
        private_channels = settings.guild_setting(
            interaction.guild.id,
            'bot_channels_private',
        ) or []
        if (
            strict_channels is None
            or settings.is_mod(interaction.user)
            or interaction.channel_id in (strict_channels or []) + private_channels
        ):
            return True

        channel_tags = ' '.join(
            f'<#{channel_id}>' for channel_id in (strict_channels or [])
        )
        await interaction.followup.send(
            'This command can only be used in a designated bot spam channel. '
            f'Try: {channel_tags}' if channel_tags else
            'This command can only be used in a designated bot spam channel.',
            ephemeral=True,
        )
        return False

    async def _publish_native_join_result(
        self,
        interaction: discord.Interaction,
        result: game_join_workers.JoinResult,
        *,
        member,
        prefix: str,
        publish_card: bool = True,
    ) -> None:
        """Publish a committed join and surface post-commit reconciliation."""

        reconciliation = await game_join_leave.remove_inactive_role_after_commit(
            result,
            member,
        )
        messages = list(result.messages)
        if reconciliation:
            messages.append(reconciliation)

        card = None
        if publish_card:
            try:
                card = await game_join_leave.load_post_commit_game_card(
                    game_id=result.game_id,
                    guild=interaction.guild,
                    bot=getattr(self, 'bot', None),
                    prefix=prefix,
                    presentation='slash',
                    requester_id=result.member_id,
                    channel_id=getattr(interaction, 'channel_id', 0),
                )
            except Exception:
                logger.exception(
                    'Committed native join %s could not reload its game card',
                    result.game_id,
                )
                messages.append(
                    f':warning: Game {result.game_id} was joined successfully, '
                    'but its game card could not be updated. An operator must '
                    'reconcile the announcement.'
                )

        if result.is_full:
            public_send = lambda content: interaction.followup.send(
                content,
                ephemeral=False,
            )
            await game_join_leave.send_post_commit_message(
                public_send,
                f'Game {result.game_id} is now full and '
                f'<@{result.creator_id}> should create the game in Polytopia.',
                game_id=result.game_id,
                effect='full-game notice',
            )
            if result.host_id and result.host_id != result.creator_id:
                await game_join_leave.send_post_commit_message(
                    public_send,
                    f'Matchmaking host <@{result.host_id}> is not the game '
                    'creator.',
                    game_id=result.game_id,
                    effect='host-mismatch notice',
                )

        if card is not None:
            try:
                await game_join_leave.send_post_commit_game_card(
                    interaction.followup,
                    card,
                    content=(
                        card.rendered.content if result.is_full else None
                    ),
                )
            except Exception:
                logger.exception(
                    'Committed native join %s game card update failed',
                    result.game_id,
                )
                messages.append(
                    f':warning: Game {result.game_id} was joined successfully, '
                    'but its game card could not be sent. An operator must '
                    'reconcile the announcement.'
                )

        await game_join_leave.send_post_commit_message(
            lambda message: interaction.followup.send(
                message,
                ephemeral=False,
            ),
            '\n'.join(messages),
            game_id=result.game_id,
            effect='join output',
        )

    @game_group.command(
        name='join',
        description='Join an open game.',
    )
    @discord.app_commands.describe(
        game_id='Open game to join.',
        side='Optional side number or side name.',
        member='Optional player to place; level 4 or higher is required.',
    )
    async def game_join_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
        side: str | None = None,
        member: discord.Member | None = None,
    ):
        """Join through the same worker used by prefix and reactions."""

        await interaction.response.defer()
        prefix = settings.guild_setting(
            interaction.guild.id,
            'command_prefix',
        )
        if not await self._native_pending_game_channel_allowed(
            interaction,
        ):
            return

        requester = interaction.user
        target = member or requester
        if (
            member is not None
            and member.id != requester.id
            and settings.get_user_level(requester) < 4
        ):
            return await interaction.followup.send(
                'You do not have permissions to add another person to a game. '
                'Tell them to use the join command themselves.',
                ephemeral=True,
            )

        matchmaking_cog = self.bot.get_cog('matchmaking')
        if matchmaking_cog is None:
            return await interaction.followup.send(
                'The join-game command handler is unavailable.',
                ephemeral=True,
            )
        try:
            result = await matchmaking_cog.execute_join(
                game_id=game_id,
                member=target,
                author_member=requester,
                side_arg=side,
                invoked_with='/game join',
                notification_member_id=requester.id,
                prefix=prefix,
            )
        except game_join_workers.PendingGameJoinValidationError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception('Database failure in native join %s', game_id)
            return await interaction.followup.send(
                'The game could not be changed because the database operation '
                'failed. No public Discord effects were made.',
                ephemeral=True,
            )
        except Exception:
            logger.exception('Unexpected failure in native join %s', game_id)
            return await interaction.followup.send(
                'The game could not be changed. No public Discord effects were '
                'made.',
                ephemeral=True,
            )

        await self._publish_native_join_result(
            interaction,
            result,
            member=target,
            prefix=prefix,
        )

    @game_group.command(
        name='leave',
        description='Leave an open game.',
    )
    @discord.app_commands.describe(game_id='Open game to leave.')
    async def game_leave_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
    ):
        """Leave through the same worker used by prefix and reactions."""

        await interaction.response.defer()
        prefix = settings.guild_setting(
            interaction.guild.id,
            'command_prefix',
        )
        if not await self._native_pending_game_channel_allowed(
            interaction,
        ):
            return

        matchmaking_cog = self.bot.get_cog('matchmaking')
        if matchmaking_cog is None:
            return await interaction.followup.send(
                'The leave-game command handler is unavailable.',
                ephemeral=True,
            )
        try:
            result = await matchmaking_cog.execute_leave(
                game_id=game_id,
                member=interaction.user,
                author_member=interaction.user,
                invoked_with='/game leave',
                prefix=prefix,
            )
        except game_join_workers.PendingGameLeaveValidationError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except peewee.PeeweeException:
            logger.exception('Database failure in native leave %s', game_id)
            return await interaction.followup.send(
                'The game could not be changed because the database operation '
                'failed. No public Discord effects were made.',
                ephemeral=True,
            )
        except Exception:
            logger.exception('Unexpected failure in native leave %s', game_id)
            return await interaction.followup.send(
                'The game could not be changed. No public Discord effects were '
                'made.',
                ephemeral=True,
            )

        await self._publish_native_leave_result(interaction, result)

    @game_manage_group.command(
        name='kick',
        description='Remove a player from an open game.',
    )
    @discord.app_commands.describe(
        game_id='Open game from which to remove the player.',
        member='Player to remove from the open game.',
    )
    async def game_manage_kick_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
        member: discord.Member,
    ):
        """Remove a pending-game member through the shared kick worker."""

        await interaction.response.defer()
        prefix = settings.guild_setting(
            interaction.guild.id,
            'command_prefix',
        )
        if not await self._native_pending_game_channel_allowed(interaction):
            return

        matchmaking_cog = self.bot.get_cog('matchmaking')
        if matchmaking_cog is None:
            return await interaction.followup.send(
                'The kick-game command handler is unavailable.',
                ephemeral=True,
            )
        try:
            result = await matchmaking_cog.execute_kick(
                game_id=game_id,
                author_member=interaction.user,
                target_member=member,
                invoked_with='/game manage kick',
                prefix=prefix,
            )
        except game_kick_workers.PendingGameKickValidationError as exc:
            return await interaction.followup.send(
                str(exc),
                ephemeral=True,
            )
        except peewee.PeeweeException:
            logger.exception('Database failure in native kick %s', game_id)
            return await interaction.followup.send(
                'The game could not be changed because the database operation '
                'failed. No public game effects were made.',
                ephemeral=True,
            )
        except Exception:
            logger.exception('Unexpected failure in native kick %s', game_id)
            return await interaction.followup.send(
                'The game could not be changed. No public game effects were '
                'made.',
                ephemeral=True,
            )

        await game_join_leave.publish_kick_result(
            result,
            send=lambda content: interaction.followup.send(
                content,
                ephemeral=False,
            ),
            card_destination=interaction.followup,
            guild=interaction.guild,
            bot=self.bot,
            channel_id=interaction.channel_id,
            prefix=prefix,
            presentation='slash',
        )

    @game_group.command(
        name='search',
        description='Search games and refine the public results.',
    )
    @discord.app_commands.describe(
        query=(
            'Optional player, team, title/notes terms, or size such as 2v2.'
        ),
        view='Choose the initial search view.',
    )
    @discord.app_commands.choices(
        view=GAME_SEARCH_VIEW_CHOICES,
    )
    async def game_search_slash(
        self,
        interaction: discord.Interaction,
        query: str | None = None,
        view: str | None = None,
    ):
        await interaction.response.defer()
        ctx = await commands.Context.from_interaction(interaction)
        ctx.prefix = settings.guild_setting(
            interaction.guild.id,
            'command_prefix',
        )
        ctx.invoked_with = 'allgames'
        if not await self.allgames.can_run(ctx):
            return
        requester_values = self._game_search_requester_values(
            interaction.user,
        )
        initial_view = view or 'all'
        request_kwargs = {
            'guild_id': interaction.guild.id,
            'requester_discord_id': interaction.user.id,
            'query': (query or '').strip(),
            **requester_values,
        }

        async def loader(filter_key):
            return await self._load_game_search(
                game_search_workers.GameSearchRequest(
                    **request_kwargs,
                    key=filter_key,
                )
            )

        try:
            snapshot = await loader(
                game_search_workers.GameSearchKey(status=initial_view)
            )
        except (game_search_workers.GameSearchError,
                peewee.PeeweeException,
                asyncio.TimeoutError,
                ValueError) as exc:
            return await interaction.followup.send(
                str(exc) or 'Game search timed out.',
                ephemeral=True,
            )
        view = game_search_views.GameSearchWorkspace(
            requester_id=interaction.user.id,
            initial_result=snapshot,
            loader=loader,
            can_view_unconfirmed=request_kwargs['staff'],
        )
        view.message = await interaction.edit_original_response(view=view)

    @settings.in_bot_channel_strict()
    @models.is_registered_member()
    @commands.command(usage='player1 player2 ... ')
    async def allgames(self, ctx, *, args=None):
        """Search for games by participants or game name

        **Examples**:
        `[p]allgames Nelluk`
        `[p]allgames OCEANS OF FIRE` - Search by title - words in all caps are used to search title/notes.
        `[p]allgames Nelluk OCEANS` - See games that included player Nelluk and the word *OCEANS* in the game name or game notes.
        `[p]allgames Jets`
        `[p]allgames Nelluk 2v2` - Show all 2v2 games including Nelluk
        `[p]allgames Jets Ronin` - See games between those two teams
        `[p]allgames Nelluk rickdaheals frodakcin Jets Ronin` - See games in which three players and two teams were all involved

        You can also filter with separate commands: `[p]wins`, `[p]losses`, `[p]completed`, `[p]incomplete` - See `[p]help wins`, etc. for more detail.
        """

        if args:
            request = player_workers.PlayerWorkspaceRequest(
                guild_id=ctx.guild.id,
                player_query=args,
                requester_discord_id=ctx.author.id,
            )
            try:
                snapshot = await self._load_player_workspace(request)
            except (player_workers.PlayerNotFound,
                    player_workers.AmbiguousPlayer,
                    peewee.PeeweeException,
                    ValueError):
                pass
            else:
                view = player_views.PlayerWorkspace(
                    requester_id=ctx.author.id,
                    snapshot=snapshot,
                    initial_section='recent',
                    can_edit=(
                        ctx.author.id == snapshot.discord_id
                        or settings.is_staff(ctx.author)
                    ),
                    avatar_url=self._player_avatar_url(
                        ctx.guild,
                        snapshot.discord_id,
                    ),
                )
                view.message = await ctx.send(view=view)
                return

        # Complex and non-player searches retain the separate game-search path.
        target_list = args.split() if args else []
        await self.game_search(ctx=ctx, mode='ALLGAMES', arg_list=target_list)

    @settings.in_bot_channel_strict()
    @models.is_registered_member()
    @commands.command(aliases=['complete', 'completed'], hidden=False)
    async def incomplete(self, ctx, *, args=None):
        """List incomplete games for you or other players - also `[p]complete`
        **Example:**
        `[p]incomplete` - Lists incomplete games you are playing in
        `[p]incomplete all` - Lists all incomplete games
        `[p]incomplete Nelluk` - Lists all incomplete games for player Nelluk
        `[p]incomplete Nelluk anarchoRex` - Lists all incomplete games with both players
        `[p]incomplete Nelluk Jets` - Lists all incomplete games for Nelluk that include team Jets
        `[p]incomplete Ronin Jets` - Lists all incomplete games that include teams Ronin and Jets
        `[p]incomplete RONIN` - Search by title - words in all caps are used to search title/notes.

        You can also include a game size such as *2v2* to limit by size.
        """
        request = player_workers.PlayerWorkspaceRequest(
            guild_id=ctx.guild.id,
            discord_id=ctx.author.id if not args else None,
            player_query=args,
            requester_discord_id=ctx.author.id,
        )
        try:
            snapshot = await self._load_player_workspace(request)
        except (player_workers.PlayerNotFound,
                player_workers.AmbiguousPlayer,
                peewee.PeeweeException,
                ValueError):
            snapshot = None
        if snapshot is not None:
            section = (
                'completed'
                if ctx.invoked_with.upper() in ('COMPLETED', 'COMPLETE')
                else 'incomplete'
            )
            view = player_views.PlayerWorkspace(
                requester_id=ctx.author.id,
                snapshot=snapshot,
                initial_section=section,
                can_edit=(
                    ctx.author.id == snapshot.discord_id
                    or settings.is_staff(ctx.author)
                ),
                avatar_url=self._player_avatar_url(
                    ctx.guild,
                    snapshot.discord_id,
                ),
            )
            view.message = await ctx.send(view=view)
            return

        target_list = args.split() if args else []
        if ctx.invoked_with.upper() in ['COMPLETED', 'COMPLETE']:
            await self.game_search(ctx=ctx, mode='COMPLETE', arg_list=target_list)
        else:
            await self.game_search(ctx=ctx, mode='INCOMPLETE', arg_list=target_list)

    @settings.in_bot_channel_strict()
    @models.is_registered_member()
    @commands.command(aliases=['losses', 'loss'], hidden=False)
    async def wins(self, ctx, *, args=None):
        """List games that you or others have won - also `[p]losses`
        If any players names are listed, the first played is who the win is checked against. If no players listed, then the first team listed is checked for the win.
        **Example:**
        `[p]wins` - Lists all games you have won
        `[p]wins Nelluk` - Lists all wins for player Nelluk
        `[p]wins Nelluk anarchoRex` - Lists all games for both players, in which the first player is the winner
        `[p]wins Nelluk frodakcin Jets` - Lists all wins for Nelluk in which player frodakcin and team Jets participated
        `[p]wins Ronin Jets` - Lists all wins for team Ronin in which team Jets participated

        You can also include a game size such as *2v2* to limit by size.
        """
        request = player_workers.PlayerWorkspaceRequest(
            guild_id=ctx.guild.id,
            discord_id=ctx.author.id if not args else None,
            player_query=args,
            requester_discord_id=ctx.author.id,
        )
        try:
            snapshot = await self._load_player_workspace(request)
        except (player_workers.PlayerNotFound,
                player_workers.AmbiguousPlayer,
                peewee.PeeweeException,
                ValueError):
            snapshot = None
        if snapshot is not None:
            completed_filter = (
                'losses'
                if ctx.invoked_with.upper() in ('LOSS', 'LOSSES')
                else 'wins'
            )
            view = player_views.PlayerWorkspace(
                requester_id=ctx.author.id,
                snapshot=snapshot,
                initial_section='completed',
                completed_filter=completed_filter,
                can_edit=(
                    ctx.author.id == snapshot.discord_id
                    or settings.is_staff(ctx.author)
                ),
                avatar_url=self._player_avatar_url(
                    ctx.guild,
                    snapshot.discord_id,
                ),
            )
            view.message = await ctx.send(view=view)
            return

        target_list = args.split() if args else []
        if ctx.invoked_with.upper() in ['LOSS', 'LOSSES']:
            await self.game_search(ctx=ctx, mode='LOSSES', arg_list=target_list)
        else:
            await self.game_search(ctx=ctx, mode='WINS', arg_list=target_list)

    @settings.in_bot_channel()
    @models.is_registered_member()
    @commands.command(usage='"Name of Game" player1 player2 vs player3 player4', aliases=['newgameunranked', 'newsteamgame', 'newsteamgameunranked'])
    # @settings.is_user_check()
    async def newgame(self, ctx, game_name: str = None, *args):
        """Adds an existing game to the bot for tracking

        **Examples:**
        `[p]newgame "Name of Game" nelluk vs koric` - Sets up a 1v1 game
        `[p]newgame "Name of Game" koric` - Sets up a 1v1 game versus yourself and koric (shortcut)
        `[p]newgame "Name of Game" nelluk frodakcin vs bakalol ben` - Sets up a 2v2 game

        Use `[p]newgameunranked` to create the game as unranked
        Legacy `[p]newsteamgame` aliases remain accepted, but platform no
        longer changes game behavior because Polytopia supports cross-play.
        """

        return await self._run_newgame(ctx, game_name, *args)

    async def _run_newgame(
        self,
        ctx,
        game_name: str = None,
        *args,
        public_destination=_NEWGAME_DESTINATION_UNSET,
    ):
        record_confirmation = bool(
            getattr(ctx, '_game_record_confirmation', False)
        ) or public_destination is not _NEWGAME_DESTINATION_UNSET

        async def fail_record(message: str):
            """Keep native confirmation failures in the private draft."""

            if record_confirmation:
                return game_record_views.GameRecordConfirmationOutcome.retryable(
                    message,
                )
            return await ctx.send(message)

        if ctx.guild.id == 814317488418193478 and not settings.is_staff(ctx.author):
            return await fail_record(
                'For **The Polympics** only server staff may open games.'
            )

        ranked_flag = not (ctx.invoked_with in ['newgameunranked', 'newsteamgameunranked'])
        # Mobile and Steam now have full cross-play. Retain the legacy field
        # with its canonical compatibility value until the schema and all
        # historical filters are retired in a separate migration.
        is_mobile = True
        requester_is_staff = bool(settings.is_staff(ctx.author))

        example_usage = (f'Example usage:\n`{ctx.prefix}newgame "Name of Game" player1 VS player2` - Start a 1v1 game\n'
                         f'`{ctx.prefix}newgame "Name of Game" player1 player2 VS player3 player4` - Start a 2v2 game')

        if settings.get_user_level(ctx.author) <= 2:
            return await fail_record(
                'You are not authorized to use this command. Create and '
                f'join games with `{ctx.prefix}open` / `{ctx.prefix}join`'
            )
        if not game_name:
            return await fail_record(f'Invalid format. {example_usage}')
        if not args:
            return await fail_record(f'Invalid format. {example_usage}')

        if len(game_name.split()) < 2 and not requester_is_staff:
            if getattr(ctx, 'interaction', None) is not None:
                return await fail_record(
                    'Invalid game name. Enter the exact multi-word game '
                    'name shown in Polytopia.'
                )
            return await fail_record(
                'Invalid game name. Make sure to use "quotation marks" '
                f'around the full game name.\n{example_usage}'
            )
        if not utilities.is_valid_poly_gamename(input=game_name):
            if not requester_is_staff:
                return await fail_record(
                    'That name looks made up. :thinking: You need to '
                    'manually create the game __in Polytopia__, come back '
                    'and input the name of the new game you made.\n'
                    f'You can use `{ctx.prefix}code NAME` to get the code '
                    'of each player in this game.'
                )

        try:
            discord_groups = await resolve_newgame_roster(
                ctx,
                args,
                ranked_flag=ranked_flag,
            )
        except NewGameRosterError as exc:
            return await fail_record(str(exc))

        logger.info(
            'All input checks passed. Creating new game records with args: '
            f'{args}'
        )
        request = game_workers.NewGameRequest(
            guild_id=ctx.guild.id,
            name=game_name,
            is_ranked=ranked_flag,
            is_mobile=is_mobile,
            mod_override=settings.is_mod(ctx.author),
            requester_is_staff=requester_is_staff,
            requester_id=ctx.author.id,
            requester_name=ctx.author.name,
            requester_nick=ctx.author.nick,
            requester_description=models.GameLog.member_string(ctx.author),
            invoked_with=ctx.invoked_with,
            escaped_game_name=discord.utils.escape_markdown(game_name),
            sides=tuple(
                tuple(
                    game_workers.NewGameParticipant(
                        discord_id=member.id,
                        discord_name=member.name,
                        discord_nick=member.nick,
                        display_name=member.display_name,
                        role_names=tuple(role.name for role in member.roles),
                    )
                    for member in group
                )
                for group in discord_groups
            ),
        )
        try:
            async with ctx.typing():
                result = await game_workers.run_new_game_creation(request)
        except (peewee.PeeweeException, exceptions.CheckFailedError) as exc:
            logger.exception('Error creating new game')
            return await fail_record(f'Error creating new game: {exc}')
        except ValueError as exc:
            return await fail_record(str(exc))
        except Exception:
            logger.exception('Unexpected error creating new game')
            return await fail_record(
                'Error creating new game. No Discord announcements or '
                'channels were created.'
            )

        if not record_confirmation:
            if result.warnings:
                await ctx.send('\n'.join(result.warnings))
            newgame = Game.load_full_game(game_id=result.game_id)
            await post_newgame_messaging(ctx, game=newgame)
            return None

        try:
            destination = _resolve_newgame_public_destination(
                ctx,
                public_destination,
            )
            if result.warnings:
                await destination.send('\n'.join(result.warnings))
            newgame = Game.load_full_game(game_id=result.game_id)
            await post_newgame_messaging(
                ctx,
                game=newgame,
                destination=destination,
            )
        except Exception:
            logger.exception(
                'Game record committed but a public post-commit effect failed'
            )
            return game_record_views.GameRecordConfirmationOutcome.reconciliation(
                f'Game ID **{result.game_id}** was committed, but a later '
                'public update failed. Do not retry; an operator must '
                'reconcile the public effects.'
            )

        return game_record_views.GameRecordConfirmationOutcome.committed(
            f'Game ID **{result.game_id}** was recorded and the public '
            'post-commit effects completed.'
        )

    @game_group.command(
        name='record',
        description='Record an existing Polytopia game.',
    )
    @discord.app_commands.describe(
        game_name='The exact game name shown in Polytopia.',
        roster='Players separated into sides with “vs”. Mentions are safest.',
        ranked='Whether the game affects ELO.',
    )
    async def newgame_slash(
        self,
        interaction: discord.Interaction,
        game_name: str,
        roster: str,
        ranked: bool = True,
    ):
        """Flexible roster parser with an interaction review gate."""

        # Keep the draft, validation, permission, and pre-commit failure
        # states private. The committed game effects are published by the
        # existing post-commit path and do not depend on the invocation
        # channel's visibility setting.
        await interaction.response.defer(ephemeral=True)
        ctx = await commands.Context.from_interaction(interaction)
        ctx.prefix = settings.guild_setting(
            interaction.guild.id,
            'command_prefix',
        )
        public_destination = getattr(interaction, 'channel', None)

        # Reuse the existing command checks so registration, configured bot
        # channels, and any global checks remain identical during transition.
        if not await self.newgame.can_run(ctx):
            return

        async def build_preview(
            roster_value: str,
        ) -> game_record_views.GameRecordPreview:
            parsed_sides = game_record_views.parse_roster_string(roster_value)
            args = game_record_views.roster_arguments(parsed_sides)
            resolved_sides = await resolve_newgame_roster(
                ctx,
                args,
                ranked_flag=ranked,
            )
            return game_record_views.GameRecordPreview(
                game_name=game_name,
                roster=roster_value,
                ranked=ranked,
                sides=tuple(
                    tuple(
                        game_record_views.RosterMember(
                            discord_id=member.id,
                            display_name=discord.utils.escape_markdown(
                                member.display_name
                            ),
                        )
                        for member in side
                    )
                    for side in resolved_sides
                ),
            )

        try:
            preview = await build_preview(roster)
        except ValueError as exc:
            return await interaction.edit_original_response(content=str(exc))

        async def confirm_record(
            confirmation: discord.Interaction,
            draft: game_record_views.GameRecordPreview,
        ) -> game_record_views.GameRecordConfirmationOutcome:
            # Component interactions do not carry application-command data
            # and therefore cannot create a new commands.Context. Retain the
            # original slash context for worker input and checks, but pass the
            # invoking channel explicitly for every committed public effect.
            ctx.invoked_with = (
                'newgame' if draft.ranked else 'newgameunranked'
            )
            ctx._game_record_confirmation = True
            try:
                parsed_sides = game_record_views.parse_roster_string(
                    draft.roster
                )
            except ValueError as exc:
                return game_record_views.GameRecordConfirmationOutcome.retryable(
                    str(exc),
                )
            outcome = await self._run_newgame(
                ctx,
                draft.game_name,
                *game_record_views.roster_arguments(parsed_sides),
                public_destination=public_destination,
            )
            if outcome is None:
                return game_record_views.GameRecordConfirmationOutcome.committed(
                    'The game was recorded successfully.'
                )
            if not isinstance(outcome, game_record_views.GameRecordConfirmationOutcome):
                raise TypeError(
                    'The newgame record callback did not return a typed '
                    'confirmation outcome.'
                )
            return outcome

        view = game_record_views.GameRecordView(
            requester_id=interaction.user.id,
            preview=preview,
            confirmer=confirm_record,
        )
        view.message = await interaction.edit_original_response(view=view)

    @settings.in_bot_channel_strict()
    @commands.command(
        usage='game_id winner',
        aliases=['lose'],
    )
    async def win(self, ctx, game_id: int, *, winner: str):
        """
        Declare winner of an existing game

        The win will be finalized when multiple players confirm the winner, or after approximately 24 hours if no other players confirm.

        If declaring your own victory it can be good practice to post a screenshot indicating that you are the last human player remaining,
        in case there is a later dispute over the outcome.

        **Examples:**
        `[p]win 2050 Home` - Declare *Home* team winner of game 2050
        `[p]win 2050 Nelluk` - Declare *Nelluk* winner of game 2050
        """
        request = game_win.build_request(
            game_id=game_id,
            member=ctx.author,
            guild_id=ctx.guild.id,
            prefix=ctx.prefix,
            winner_text=winner,
            invoked_with=getattr(ctx, 'invoked_with', 'win'),
        )
        return await game_win.run_win(
            request,
            guild=ctx.guild,
            current_channel=ctx.channel,
            send_public=ctx.send,
            send_error=ctx.send,
            post_win_publisher=post_win_messaging,
            defer=(ctx.defer if ctx.interaction is not None else None),
            typing_context=ctx.typing,
        )

    @game_group.command(
        name='win',
        description='Declare or confirm the winner of a game.',
    )
    @discord.app_commands.describe(
        game_id='Game receiving the win claim.',
        winner='Player or side that won.',
    )
    async def win_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
        winner: str,
    ):
        # Registration/channel checks perform bounded validation. A slash
        # interaction is acknowledged before those checks and before the
        # shared worker preflight.
        await interaction.response.defer()
        ctx = await commands.Context.from_interaction(interaction)
        ctx.prefix = settings.guild_setting(
            interaction.guild.id,
            'command_prefix',
        )
        ctx.invoked_with = 'win'
        if not await self.win.can_run(ctx):
            return

        request = game_win.build_request(
            game_id=game_id,
            member=interaction.user,
            guild_id=interaction.guild.id,
            prefix=ctx.prefix,
            winner_text=winner,
            invoked_with='win',
        )

        async def public_send(content):
            await interaction.followup.send(content, ephemeral=False)

        async def error_send(content):
            await interaction.followup.send(content, ephemeral=True)

        return await game_win.run_win(
            request,
            guild=interaction.guild,
            current_channel=interaction.channel,
            send_public=public_send,
            send_error=error_send,
            post_win_publisher=post_win_messaging,
            acknowledged=True,
        )

    @settings.in_bot_channel()
    @models.is_registered_member()
    @commands.command(usage='game_id')
    async def unwin(self, ctx, game_id: int):
        """Reset a completed game to incomplete

        **Staff usage**:
        Reverts ELO changes from the completed game and any subsequent completed game.
        Resets the game as if it were still incomplete with no declared winner.

        **Player usage**:
        If you use the `[p]win` command on the wrong game or for the wrong winner, use this command to undo your mistake.

         **Examples**
        `[p]unwin 12500`
        """

        if ctx.interaction is not None:
            await ctx.defer()

        request = game_unwin.build_request(
            game_id=game_id,
            member=ctx.author,
            guild_id=ctx.guild.id,
            prefix=ctx.prefix,
        )
        return await game_unwin.run_unwin(
            request,
            guild=ctx.guild,
            current_channel=ctx.channel,
            send=ctx.send,
            post_unwin_publisher=post_unwin_messaging,
            typing_context=ctx.typing,
        )

    @game_result_group.command(
        name='undo',
        description='Reset a win claim or completed game.',
    )
    @discord.app_commands.describe(game_id='Game whose win should be reset.')
    async def unwin_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
    ):
        ctx = await commands.Context.from_interaction(interaction)
        ctx.prefix = settings.guild_setting(
            interaction.guild.id,
            'command_prefix',
        )
        ctx.invoked_with = 'unwin'
        if not await self.unwin.can_run(ctx):
            return
        await self.unwin.callback(self, ctx, game_id)

    @settings.in_bot_channel()
    @models.is_registered_member()
    @commands.command(
        usage='game_id',
        aliases=['delete_game', 'delgame', 'delmatch', 'deletegame'],
    )
    async def delete(self, ctx, game_id: int):
        """Deletes a game

        You can delete a game if you are the host and is has not started yet.
        Mods can delete completed games which will reverse any ELO changes they caused.
        **Example:**
        `[p]deletegame 25`
        """

        if ctx.interaction is not None:
            await ctx.defer()
        request = game_deletion.build_request(
            game_id=game_id,
            member=ctx.author,
            guild_id=ctx.guild.id,
            prefix=ctx.prefix,
            invoked_with=getattr(ctx, 'invoked_with', 'delete'),
        )
        try:
            typing = getattr(ctx, 'typing', None)
            if typing is None:
                result = await game_deletion.delete_game(request)
            else:
                async with typing():
                    result = await game_deletion.delete_game(request)
        except EloJobConflict as exc:
            active_job = exc.active_job
            return await ctx.send(
                f'ELO operation `{active_job.operation}` for game '
                f'`{active_job.game_id or "all"}` is already running.'
            )
        except game_deletion.GameDeletionValidationError as exc:
            return await ctx.send(str(exc))
        except peewee.PeeweeException:
            logger.exception('Database failure deleting game %s', game_id)
            return await ctx.send(
                'Game deletion failed and rolled back. No Discord channel '
                'updates were made.'
            )
        except Exception:
            logger.exception('Unexpected failure deleting game %s', game_id)
            return await ctx.send(
                'Game deletion failed. No Discord channel updates were made.'
            )

        await game_deletion.publish_result(
            result,
            send=ctx.send,
            guild=ctx.guild,
            bot=self.bot,
            prefix=ctx.prefix,
        )

    @game_manage_group.command(
        name='delete',
        description='Delete a game when your permissions allow it.',
    )
    @discord.app_commands.describe(game_id='Game to delete.')
    async def delete_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
    ):
        ctx = await commands.Context.from_interaction(interaction)
        ctx.prefix = settings.guild_setting(
            interaction.guild.id,
            'command_prefix',
        )
        ctx.invoked_with = 'delete'
        if not await self.delete.can_run(ctx):
            return
        await self.delete.callback(self, ctx, game_id)

    async def _delegate_administration_slash(
        self,
        interaction: discord.Interaction,
        method_name: str,
        *args,
    ):
        administration_cog = self.bot.get_cog('administration')
        if administration_cog is None:
            message = 'The administration command handler is unavailable.'
            if interaction.response.is_done():
                return await interaction.followup.send(
                    message,
                    ephemeral=True,
                )
            return await interaction.response.send_message(
                message,
                ephemeral=True,
            )
        method = getattr(administration_cog, method_name)
        return await method(interaction, *args)

    @game_result_group.command(
        name='confirm',
        description='Confirm the claimed winner of a game.',
    )
    @discord.app_commands.describe(game_id='Game whose winner to confirm.')
    async def confirm_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
    ):
        await self._delegate_administration_slash(
            interaction,
            'confirm_slash',
            game_id,
        )

    @game_group.command(
        name='ranked',
        description='Set whether an incomplete game is ranked.',
    )
    @discord.app_commands.describe(
        game_id='Incomplete game to correct.',
        ranked='True for ranked; false for unranked.',
    )
    async def set_ranked_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
        ranked: bool,
    ):
        await self._delegate_administration_slash(
            interaction,
            'set_ranked_slash',
            game_id,
            ranked,
        )

    @game_manage_group.command(
        name='unstart',
        description='Return an in-progress game to open matchmaking.',
    )
    @discord.app_commands.describe(
        game_id='In-progress game to return to matchmaking.',
    )
    async def unstart_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
    ):
        await self._delegate_administration_slash(
            interaction,
            'unstart_slash',
            game_id,
        )

    @game_manage_group.command(
        name='extend',
        description='Extend an open game deadline by 24 hours.',
    )
    @discord.app_commands.describe(game_id='Open game to extend.')
    async def extend_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
    ):
        await self._delegate_administration_slash(
            interaction,
            'extend_slash',
            game_id,
        )

    @commands.command(usage='game_id "New Name"')
    @models.is_registered_member()
    async def rename(self, ctx, *args):
        """Rename an existing game through the bounded ordinary-game worker.

        You can rename a game for which you are the host. You can omit the
        game ID if you use the command in a game-specific channel.
        **Example:**
        `[p]rename 52000 Mountains of Fire`
        `[p]rename 52000 None` - Remove a game's name. Required elevated permissions.
        """

        usage = (
            f'**Example usage:** `{ctx.prefix}rename 100 New Game Name`\n'
            'You can also omit the game ID if you use the command from a '
            'game-specific channel.'
        )
        if not args:
            return await ctx.send(usage)

        channel_id = int(
            getattr(getattr(ctx, 'message', None), 'channel', None).id
            if getattr(getattr(ctx, 'message', None), 'channel', None) is not None
            else getattr(getattr(ctx, 'channel', None), 'id', 0)
        )
        first_token = str(args[0]).strip('#')
        try:
            explicit_game_id = int(first_token)
        except (TypeError, ValueError):
            explicit_game_id = None
        new_game_name = (
            ' '.join(args[1:])
            if explicit_game_id is not None
            else ' '.join(args)
        )
        target_request = game_name.build_mutation_request(
            member=ctx.author,
            guild_id=ctx.guild.id,
            channel_id=channel_id,
            legacy_tokens=tuple(args),
            allow_related_channel=True,
            invoked_with=ctx.invoked_with or 'rename',
            prefix=ctx.prefix,
        )

        try:
            target = await game_workers.run_prepare_legacy_game_name(
                target_request,
            )
        except game_workers.GameNameLookupError as exc:
            if str(exc).startswith('Error looking up game based on current channel'):
                logger.error(
                    'More than one game with matching channel %s',
                    channel_id,
                )
                return await ctx.send(str(exc))
            if explicit_game_id is not None:
                return await ctx.send(
                    f'Game with ID {explicit_game_id} cannot be found.'
                )
            reset_cooldown = getattr(
                getattr(ctx, 'command', None),
                'reset_cooldown',
                None,
            )
            if callable(reset_cooldown):
                reset_cooldown(ctx)
            return await ctx.send(
                f'Game ID was not included and this does not appear to be a '
                f'game-specific channel.\n{usage}'
            )
        except game_workers.GameNameValidationError as exc:
            return await ctx.send(str(exc))
        except peewee.PeeweeException:
            logger.exception('Database failure resolving rename target')
            return await ctx.send('The game name target could not be loaded.')

        if not target.inferred_from_channel:
            if not await settings.is_bot_channel_strict(ctx):
                return await ctx.send(
                    'This command must be used in a bot spam channel or in a '
                    'game-specific channel.'
                )
            logger.debug(
                'Using explicit game %s for rename command in channel %s',
                target.game_id,
                channel_id,
            )
        else:
            logger.debug(
                'Inferring game %s from rename command used in channel %s',
                target.game_id,
                channel_id,
            )

        if not new_game_name:
            return await ctx.send(usage)
        clear = new_game_name.upper() == 'NONE'
        request = game_name.build_mutation_request(
            member=ctx.author,
            guild_id=ctx.guild.id,
            channel_id=channel_id,
            game_id=target.game_id,
            name=None if clear else new_game_name,
            clear=clear,
            allow_related_channel=target.inferred_from_channel,
            invoked_with=ctx.invoked_with or 'rename',
            prefix=ctx.prefix,
        )

        async def after_commit(result):
            game_guild = None
            bot = getattr(self, 'bot', None)
            get_guild = getattr(bot, 'get_guild', None)
            if callable(get_guild):
                game_guild = get_guild(result.guild_id)
            game_guild = game_guild or ctx.guild
            guild_list = getattr(bot, 'guilds', None) or (game_guild,)
            await game_name.publish_mutation_result(
                result,
                send=ctx.send,
                destination=ctx,
                guild=game_guild,
                bot=bot,
                guild_list=guild_list,
                prefix=ctx.prefix,
                requester_id=int(ctx.author.id),
                channel_id=int(ctx.channel.id),
            )

        try:
            await game_name.run_name_mutation(
                request,
                after_commit=after_commit,
            )
        except game_workers.GameNameValidationError as exc:
            message = str(exc)
            if message.startswith('This command requires bot registration first.'):
                message = message.replace(
                    '__`setname ',
                    f'__`{ctx.prefix}setname ',
                ).replace(
                    '__`steamname ',
                    f'__`{ctx.prefix}steamname ',
                )
            return await ctx.send(message)
        except exceptions.RecordLocked as exc:
            return await ctx.send(str(exc))
        except peewee.PeeweeException:
            logger.exception('Database failure setting game name')
            return await ctx.send(
                'The game name change failed and rolled back. No Discord '
                'announcement or card update was made.'
            )
        except Exception:
            logger.exception('Unexpected failure setting game name')
            return await ctx.send(
                'The game name change failed. No Discord announcement or card '
                'update was made.'
            )


    @commands.command(aliases=['setmaptype'], usage='game_id map_type')
    @models.is_registered_member()
    async def setmap(self, ctx, *, args: str = None):
        """Set map type for a game

        **Examples**
        `[p]setmap 2055 arch` - Sets the map type to 'Archipelago' for game 2055
        `[p]setmap dry` - Sets the map type while in a game-specific channel
        `[p]setmap none` - Clear the map type for the current game

        """

        if not args:
            return await ctx.send(f'No arguments provided. **Example usage:** `{ctx.prefix}{ctx.invoked_with} 1234 dry`')

        request = game_map.build_mutation_request(
            member=ctx.author,
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            legacy_tokens=tuple(args.split()),
            allow_related_channel=True,
            invoked_with=ctx.invoked_with,
        )

        async def after_commit(result):
            await game_map.publish_mutation_result(
                result,
                send=ctx.send,
                guild=ctx.guild,
                bot=getattr(self, 'bot', None),
                prefix=ctx.prefix,
                requester_id=int(ctx.author.id),
                channel_id=int(ctx.channel.id),
            )

        try:
            await game_map.run_map_mutation(
                request,
                after_commit=after_commit,
            )
        except game_workers.GameMapLookupError as exc:
            return await ctx.send(
                f'{exc}\n**Example usage:** `{ctx.prefix}{ctx.invoked_with} '
                '1234 dry`\nYou can also omit the game ID if you use the '
                'command from a game-specific channel.'
            )
        except game_workers.GameMapValidationError as exc:
            message = str(exc)
            if message.startswith(
                'This command requires bot registration first.'
            ):
                message = message.replace(
                    '__`setname ',
                    f'__`{ctx.prefix}setname ',
                ).replace(
                    '__`steamname ',
                    f'__`{ctx.prefix}steamname ',
                )
            elif 'No matching map type found' in message:
                raw_map_type = request.legacy_tokens[-1] if request.legacy_tokens else ''
                message = (
                    'No matching map type found for '
                    f'"{discord.utils.escape_mentions(raw_map_type)}". '
                    'Check spelling or try a different name.'
                )
            elif message.startswith('Wrong number of arguments.'):
                message = message.replace(
                    '`help setmaptype`',
                    f'`{ctx.prefix}help setmaptype`',
                )
            return await ctx.send(message)
        except exceptions.RecordLocked as exc:
            return await ctx.send(str(exc))
        except peewee.PeeweeException:
            logger.exception('Database failure setting map for prefix command')
            return await ctx.send(
                'The map change failed and rolled back. No Discord '
                'announcement or card update was made.'
            )
        except Exception:
            logger.exception('Unexpected failure setting map for prefix command')
            return await ctx.send(
                'The map change failed. No Discord announcement or card '
                'update was made.'
            )

    @game_group.command(
        name='map',
        description='View or update a game map type.',
    )
    @discord.app_commands.describe(
        game_id='Game whose map type to view or edit.',
        map_type='Map type to set; omit this to view the current value.',
        clear='Clear the current map type.',
    )
    @discord.app_commands.choices(map_type=GAME_MAP_TYPE_CHOICES)
    async def map_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
        map_type: str | None = None,
        clear: bool = False,
    ):
        """Read or edit one game map through the shared map service."""

        if map_type is not None and clear:
            return await interaction.response.send_message(
                'Choose either a map type or clear, not both.',
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)
        public_send = game_map.public_interaction_sender(interaction)
        channel_id = int(
            getattr(interaction, 'channel_id', None)
            or getattr(getattr(interaction, 'channel', None), 'id', 0)
            or 0
        )

        if map_type is None and not clear:
            request = game_map.build_read_request(
                member=interaction.user,
                guild_id=interaction.guild.id,
                channel_id=channel_id,
                game_id=game_id,
            )
            try:
                result = await game_map.run_map_read(request)
            except game_workers.GameMapValidationError as exc:
                return await interaction.followup.send(
                    str(exc),
                    ephemeral=True,
                )
            except peewee.PeeweeException:
                logger.exception('Database failure reading game map')
                return await interaction.followup.send(
                    'The current map type could not be loaded.',
                    ephemeral=True,
                )
            except Exception:
                logger.exception('Unexpected failure reading game map')
                return await interaction.followup.send(
                    'The current map type could not be loaded.',
                    ephemeral=True,
                )
            return await public_send(game_map.read_message(result))

        request = game_map.build_mutation_request(
            member=interaction.user,
            guild_id=interaction.guild.id,
            channel_id=channel_id,
            game_id=game_id,
            map_type=map_type,
            clear=clear,
            invoked_with='/game map',
        )

        async def after_commit(result):
            await game_map.publish_mutation_result(
                result,
                send=public_send,
                guild=interaction.guild,
                bot=getattr(self, 'bot', None),
                prefix=settings.guild_setting(
                    interaction.guild.id,
                    'command_prefix',
                ),
                requester_id=int(interaction.user.id),
                channel_id=int(channel_id),
                presentation='slash',
            )

        try:
            await game_map.run_map_mutation(
                request,
                after_commit=after_commit,
            )
        except game_workers.GameMapValidationError as exc:
            return await interaction.followup.send(
                str(exc),
                ephemeral=True,
            )
        except exceptions.RecordLocked as exc:
            return await interaction.followup.send(
                str(exc),
                ephemeral=True,
            )
        except peewee.PeeweeException:
            logger.exception('Database failure setting map from slash')
            return await interaction.followup.send(
                'The map change failed and rolled back. No Discord '
                'announcement or card update was made.',
                ephemeral=True,
            )
        except Exception:
            logger.exception('Unexpected failure setting map from slash')
            return await interaction.followup.send(
                'The map change failed. No Discord announcement or card '
                'update was made.',
                ephemeral=True,
            )

    @game_group.command(
        name='side',
        description='View or update one game side name and role restriction.',
    )
    @discord.app_commands.describe(
        game_id='Game whose side to view or edit.',
        side='Side number or an unambiguous side-name fragment.',
        role='Role that should be required to join this side.',
        name='Side name; omit it to use the role name when a role is set.',
        clear='Clear both the side name and role restriction.',
    )
    async def side_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
        side: str,
        role: discord.Role | None = None,
        name: str | None = None,
        clear: bool = False,
    ):
        """Read or edit one side through the shared worker service."""

        if clear and (role is not None or name is not None):
            return await interaction.response.send_message(
                'Choose either a side name or role restriction, not clear.',
                ephemeral=True,
            )

        role_guild_id = (
            getattr(getattr(role, 'guild', None), 'id', None)
            if role is not None else None
        )
        if role is not None and role_guild_id not in (None, interaction.guild.id):
            return await interaction.response.send_message(
                'The side restriction role must belong to this Discord server.',
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)
        actor = game_side.capture_actor(interaction.user)
        channel_id = int(
            getattr(interaction, 'channel_id', None)
            or getattr(getattr(interaction, 'channel', None), 'id', 0)
            or 0
        )
        try:
            prefix = settings.guild_setting(
                interaction.guild.id,
                'command_prefix',
            )
        except exceptions.CheckFailedError:
            prefix = '$'
        public_send = game_side.public_interaction_sender(interaction)

        if role is None and name is None and not clear:
            request = game_side.build_read_request(
                member=interaction.user,
                guild_id=interaction.guild.id,
                channel_id=channel_id,
                game_id=game_id,
                side_lookup=side,
            )
            try:
                result = await asyncio.wait_for(
                    game_side.run_side_read(request),
                    timeout=20,
                )
            except game_workers.GameSideValidationError as exc:
                return await interaction.followup.send(
                    str(exc),
                    ephemeral=True,
                )
            except asyncio.TimeoutError:
                return await interaction.followup.send(
                    'The current side configuration could not be loaded in '
                    'time. Run `/game side` again.',
                    ephemeral=True,
                )
            except peewee.PeeweeException:
                logger.exception('Database failure reading game side')
                return await interaction.followup.send(
                    'The current side configuration could not be loaded.',
                    ephemeral=True,
                )
            except Exception:
                logger.exception('Unexpected failure reading game side')
                return await interaction.followup.send(
                    'The current side configuration could not be loaded.',
                    ephemeral=True,
                )
            return await public_send(
                game_side.read_message(
                    result,
                    actor=actor,
                    guild=interaction.guild,
                )
            )

        request = game_side.build_mutation_request(
            member=interaction.user,
            guild_id=interaction.guild.id,
            channel_id=channel_id,
            game_id=game_id,
            side_lookup=side,
            side_name=name,
            role_id=(role.id if role is not None else None),
            role_name=(role.name if role is not None else None),
            role_guild_id=role_guild_id,
            clear=clear,
            native=True,
            invoked_with='/game side',
        )

        async def after_commit(result):
            await game_side.publish_mutation_result(
                result,
                send=public_send,
                destination=interaction.channel,
                guild=interaction.guild,
                bot=getattr(self, 'bot', None),
                prefix=prefix,
                requester_id=int(interaction.user.id),
                channel_id=int(channel_id),
                presentation='slash',
                actor=actor,
            )

        try:
            await game_side.run_side_mutation(
                request,
                after_commit=after_commit,
            )
        except game_workers.GameSideValidationError as exc:
            return await interaction.followup.send(
                str(exc),
                ephemeral=True,
            )
        except exceptions.RecordLocked as exc:
            return await interaction.followup.send(
                str(exc),
                ephemeral=True,
            )
        except peewee.PeeweeException:
            logger.exception('Database failure setting game side from slash')
            return await interaction.followup.send(
                'The side change failed and rolled back. No Discord '
                'announcement or card update was made.',
                ephemeral=True,
            )
        except Exception:
            logger.exception('Unexpected failure setting game side from slash')
            return await interaction.followup.send(
                'The side change failed. No Discord announcement or card '
                'update was made.',
                ephemeral=True,
            )

    @game_group.command(
        name='notes',
        description='View or update a game’s notes.',
    )
    @discord.app_commands.describe(
        game_id='Game whose notes to view or edit.',
    )
    async def notes_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
    ):
        """Publish a short-lived public current-notes workspace."""

        await interaction.response.defer(ephemeral=True)
        requester_actor = game_notes.capture_actor(interaction.user)
        channel_id = int(
            getattr(interaction, 'channel_id', None)
            or getattr(getattr(interaction, 'channel', None), 'id', 0)
            or 0
        )
        request = game_notes.build_read_request(
            member=interaction.user,
            guild_id=interaction.guild.id,
            channel_id=channel_id,
            game_id=game_id,
        )

        try:
            result = await asyncio.wait_for(
                game_notes.run_notes_read(request),
                timeout=20,
            )
        except game_workers.GameNotesValidationError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except asyncio.TimeoutError:
            return await interaction.followup.send(
                'The current notes could not be loaded in time. Run `/game '
                'notes` again.',
                ephemeral=True,
            )
        except peewee.PeeweeException:
            logger.exception('Database failure reading game notes')
            return await interaction.followup.send(
                'The current notes could not be loaded.',
                ephemeral=True,
            )
        except Exception:
            logger.exception('Unexpected failure reading game notes')
            return await interaction.followup.send(
                'The current notes could not be loaded.',
                ephemeral=True,
            )

        try:
            prefix = settings.guild_setting(
                interaction.guild.id,
                'command_prefix',
            )
        except exceptions.CheckFailedError:
            logger.warning(
                'Could not load the guild prefix for game-notes output; '
                'using the default prefix'
            )
            prefix = '$'

        async def private_failure(native_interaction, message: str) -> None:
            response = getattr(native_interaction, 'response', None)
            is_done = getattr(response, 'is_done', None)
            if callable(is_done) and is_done():
                await native_interaction.followup.send(
                    message,
                    ephemeral=True,
                )
            else:
                await native_interaction.response.send_message(
                    message,
                    ephemeral=True,
                )

        async def mutate(
            native_interaction,
            notes: str | None,
            expected_snapshot: game_workers.GameNotesReadResult,
            *,
            clear: bool,
        ):
            actor = game_notes.capture_actor(native_interaction.user)
            mutation = game_notes.build_mutation_request(
                member=native_interaction.user,
                guild_id=native_interaction.guild.id,
                channel_id=int(
                    getattr(native_interaction, 'channel_id', None)
                    or getattr(
                        getattr(native_interaction, 'channel', None),
                        'id',
                        0,
                    )
                    or 0
                ),
                game_id=expected_snapshot.game_id,
                notes=notes,
                clear=clear,
                expected_notes=expected_snapshot.notes,
                check_expected_notes=True,
                invoked_with='/game notes',
                prefix=prefix,
                mention_warning=(
                    not clear and game_notes.contains_note_mentions(notes)
                ),
            )
            public_send = game_notes.public_interaction_sender(
                native_interaction,
            )

            async def after_commit(committed):
                await game_notes.publish_mutation_result(
                    committed,
                    send=public_send,
                    actor=actor,
                    refresh_card=lambda value: game_notes.refresh_game_card(
                        value,
                        destination=native_interaction.channel,
                        guild=native_interaction.guild,
                        bot=getattr(self, 'bot', None),
                        prefix=prefix,
                        requester_id=int(native_interaction.user.id),
                        channel_id=int(
                            getattr(native_interaction, 'channel_id', None)
                            or getattr(native_interaction.channel, 'id', 0)
                            or 0
                        ),
                        presentation='slash',
                    ),
                )

            try:
                return await game_notes.run_notes_mutation(
                    mutation,
                    after_commit=after_commit,
                )
            except game_workers.GameNotesValidationError as exc:
                await private_failure(native_interaction, str(exc))
            except exceptions.RecordLocked as exc:
                await private_failure(native_interaction, str(exc))
            except peewee.PeeweeException:
                logger.exception('Database failure changing game notes')
                await private_failure(
                    native_interaction,
                    'The notes change failed and rolled back. No Discord '
                    'announcement or card update was made.',
                )
            except asyncio.TimeoutError:
                await private_failure(
                    native_interaction,
                    'The notes change did not finish in time. Run `/game notes` '
                    'again to verify the current value.',
                )
            except Exception:
                logger.exception('Unexpected failure changing game notes')
                await private_failure(
                    native_interaction,
                    'The notes change failed. No Discord announcement or card '
                    'update was made.',
                )
            return None

        async def edit_callback(
            native_interaction,
            notes,
            expected_snapshot,
        ):
            return await mutate(
                native_interaction,
                notes,
                expected_snapshot,
                clear=False,
            )

        async def clear_callback(
            native_interaction,
            expected_snapshot,
        ):
            return await mutate(
                native_interaction,
                None,
                expected_snapshot,
                clear=True,
            )

        workspace = game_notes_views.GameNotesWorkspaceView(
            result,
            requester_id=interaction.user.id,
            on_edit=edit_callback,
            on_clear=clear_callback,
            requester_actor=requester_actor,
        )
        public_send = game_notes.public_interaction_sender(interaction)
        try:
            workspace.message = await public_send(
                game_notes.read_message(result, actor=requester_actor),
                view=workspace,
            )
        except Exception:
            logger.exception('Could not publish the game-notes workspace')
            await interaction.followup.send(
                'The current notes were loaded but could not be published. '
                'Run `/game notes` again.',
                ephemeral=True,
            )
        return workspace

    @game_group.command(
        name='name',
        description='View or update a tracked Polytopia game name.',
    )
    @discord.app_commands.describe(
        game_id='Game whose tracked name to view or edit.',
    )
    async def name_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
    ):
        """Publish a short-lived public current-name workspace."""

        await interaction.response.defer(ephemeral=True)
        requester_actor = game_name.capture_actor(interaction.user)
        channel_id = int(
            getattr(interaction, 'channel_id', None)
            or getattr(getattr(interaction, 'channel', None), 'id', 0)
            or 0
        )
        request = game_name.build_read_request(
            member=interaction.user,
            guild_id=interaction.guild.id,
            channel_id=channel_id,
            game_id=game_id,
        )

        try:
            result = await asyncio.wait_for(
                game_name.run_name_read(request),
                timeout=20,
            )
        except game_workers.GameNameValidationError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except asyncio.TimeoutError:
            return await interaction.followup.send(
                'The current game name could not be loaded in time. Run '
                '`/game name` again.',
                ephemeral=True,
            )
        except peewee.PeeweeException:
            logger.exception('Database failure reading game name')
            return await interaction.followup.send(
                'The current game name could not be loaded.',
                ephemeral=True,
            )
        except Exception:
            logger.exception('Unexpected failure reading game name')
            return await interaction.followup.send(
                'The current game name could not be loaded.',
                ephemeral=True,
            )

        try:
            prefix = settings.guild_setting(
                interaction.guild.id,
                'command_prefix',
            )
        except exceptions.CheckFailedError:
            logger.warning(
                'Could not load the guild prefix for game-name output; using '
                'the default prefix'
            )
            prefix = '$'

        bot = getattr(self, 'bot', None) or getattr(settings, 'bot', None)
        guild_list = getattr(bot, 'guilds', None) or (interaction.guild,)

        async def private_failure(native_interaction, message: str) -> None:
            response = getattr(native_interaction, 'response', None)
            is_done = getattr(response, 'is_done', None)
            if callable(is_done) and is_done():
                await native_interaction.followup.send(
                    message,
                    ephemeral=True,
                )
            else:
                await native_interaction.response.send_message(
                    message,
                    ephemeral=True,
                )

        async def mutate(
            native_interaction,
            name,
            expected_snapshot: game_workers.GameNameReadResult,
            *,
            clear: bool,
        ):
            actor = game_name.capture_actor(native_interaction.user)
            mutation = game_name.build_mutation_request(
                member=native_interaction.user,
                guild_id=native_interaction.guild.id,
                channel_id=int(
                    getattr(native_interaction, 'channel_id', None)
                    or getattr(
                        getattr(native_interaction, 'channel', None),
                        'id',
                        0,
                    )
                    or 0
                ),
                game_id=expected_snapshot.game_id,
                name=name,
                clear=clear,
                expected_name=expected_snapshot.name,
                check_expected_name=True,
                invoked_with='/game name',
                prefix=prefix,
            )
            public_send = game_name.public_interaction_sender(
                native_interaction,
            )

            async def after_commit(committed):
                await game_name.publish_mutation_result(
                    committed,
                    send=public_send,
                    destination=native_interaction.channel,
                    guild=native_interaction.guild,
                    bot=getattr(self, 'bot', None),
                    guild_list=guild_list,
                    prefix=prefix,
                    requester_id=int(native_interaction.user.id),
                    channel_id=int(
                        getattr(native_interaction, 'channel_id', None)
                        or getattr(native_interaction.channel, 'id', 0)
                        or 0
                    ),
                    presentation='slash',
                    actor=actor,
                )

            try:
                return await game_name.run_name_mutation(
                    mutation,
                    after_commit=after_commit,
                )
            except game_workers.GameNameValidationError as exc:
                await private_failure(native_interaction, str(exc))
            except exceptions.RecordLocked as exc:
                await private_failure(native_interaction, str(exc))
            except peewee.PeeweeException:
                logger.exception('Database failure changing game name')
                await private_failure(
                    native_interaction,
                    'The game name change failed and rolled back. No Discord '
                    'announcement or card update was made.',
                )
            except asyncio.TimeoutError:
                await private_failure(
                    native_interaction,
                    'The game name change did not finish in time. Run `/game '
                    'name` again to verify the current value.',
                )
            except Exception:
                logger.exception('Unexpected failure changing game name')
                await private_failure(
                    native_interaction,
                    'The game name change failed. No Discord announcement or '
                    'card update was made.',
                )
            return None

        async def edit_callback(
            native_interaction,
            name,
            expected_snapshot,
        ):
            return await mutate(
                native_interaction,
                name,
                expected_snapshot,
                clear=False,
            )

        async def clear_callback(
            native_interaction,
            expected_snapshot,
        ):
            return await mutate(
                native_interaction,
                None,
                expected_snapshot,
                clear=True,
            )

        workspace = game_name_views.GameNameWorkspaceView(
            result,
            requester_id=interaction.user.id,
            on_edit=edit_callback,
            on_clear=clear_callback,
            requester_actor=requester_actor,
        )
        public_send = game_name.public_interaction_sender(interaction)
        try:
            workspace.message = await public_send(
                game_name.read_message(result, actor=requester_actor),
                view=workspace,
            )
        except Exception:
            logger.exception('Could not publish the game-name workspace')
            await interaction.followup.send(
                'The current game name was loaded but could not be published. '
                'Run `/game name` again.',
                ephemeral=True,
            )
        return workspace

    @game_group.command(
        name='tribe',
        description='View or update player tribes for a game.',
    )
    @discord.app_commands.describe(
        game_id='Game whose player-to-tribe mapping to view or edit.',
        bulk=(
            'Optional staff batch: alternating player and tribe values, '
            'for example "Player Bardur Other Elyrion".'
        ),
    )
    async def tribe_slash(
        self,
        interaction: discord.Interaction,
        game_id: int,
        bulk: str | None = None,
    ):
        """Read a public mapping or apply one native all-or-nothing batch."""

        await interaction.response.defer(ephemeral=True)
        actor = game_tribe.capture_actor(interaction.user)
        channel_id = int(
            getattr(interaction, 'channel_id', None)
            or getattr(getattr(interaction, 'channel', None), 'id', 0)
            or 0
        )
        try:
            prefix = settings.guild_setting(
                interaction.guild.id,
                'command_prefix',
            )
        except exceptions.CheckFailedError:
            prefix = '$'

        async def private_failure(native_interaction, message: str) -> None:
            response = getattr(native_interaction, 'response', None)
            is_done = getattr(response, 'is_done', None)
            if callable(is_done) and is_done():
                await native_interaction.followup.send(
                    message,
                    ephemeral=True,
                )
            else:
                await native_interaction.response.send_message(
                    message,
                    ephemeral=True,
                )

        async def mutate(
            native_interaction,
            *,
            assignments=(),
            expected_snapshot=(),
            raw_bulk=None,
            require_elevated=False,
        ):
            submitter = game_tribe.capture_actor(native_interaction.user)
            request = game_tribe.build_mutation_request(
                member=native_interaction.user,
                guild_id=native_interaction.guild.id,
                channel_id=int(
                    getattr(native_interaction, 'channel_id', None)
                    or getattr(
                        getattr(native_interaction, 'channel', None),
                        'id',
                        0,
                    )
                    or 0
                ),
                game_id=game_id,
                assignments=tuple(assignments),
                expected_snapshots=tuple(expected_snapshot),
                check_expected_snapshots=bool(expected_snapshot),
                raw_bulk=raw_bulk,
                native=True,
                require_elevated=require_elevated,
                invoked_with='/game tribe',
            )
            public_send = game_tribe.public_interaction_sender(
                native_interaction,
            )

            async def after_commit(committed):
                await game_tribe.publish_mutation_result(
                    committed,
                    send=public_send,
                    destination=getattr(
                        native_interaction,
                        'channel',
                        getattr(interaction, 'channel', None),
                    ),
                    guild=native_interaction.guild,
                    bot=getattr(self, 'bot', None),
                    prefix=prefix,
                    requester_id=int(native_interaction.user.id),
                    channel_id=int(
                        getattr(native_interaction, 'channel_id', None)
                        or getattr(
                            getattr(native_interaction, 'channel', None),
                            'id',
                            0,
                        )
                        or 0
                    ),
                    presentation='slash',
                    actor=submitter,
                )

            try:
                return await asyncio.wait_for(
                    game_tribe.run_tribe_mutation(
                        request,
                        after_commit=after_commit,
                    ),
                    timeout=20,
                )
            except game_workers.GameTribeValidationError as exc:
                await private_failure(native_interaction, str(exc))
            except exceptions.RecordLocked as exc:
                await private_failure(native_interaction, str(exc))
            except asyncio.TimeoutError:
                await private_failure(
                    native_interaction,
                    'The tribe change did not finish in time. Run `/game tribe '
                    f'{game_id}` again to verify the current mapping.',
                )
            except peewee.PeeweeException:
                logger.exception('Database failure changing game tribes')
                await private_failure(
                    native_interaction,
                    'The tribe change failed and rolled back. No Discord '
                    'announcement or card update was made.',
                )
            except Exception:
                logger.exception('Unexpected failure changing game tribes')
                await private_failure(
                    native_interaction,
                    'The tribe change failed. No Discord announcement or card '
                    'update was made.',
                )
            return None

        if bulk is not None:
            # This direct path deliberately does not open a modal or require a
            # second interaction: the write worker parses and validates the
            # whole raw batch before entering its atomic update loop.
            return await mutate(
                interaction,
                raw_bulk=bulk,
                require_elevated=True,
            )

        request = game_tribe.build_read_request(
            member=interaction.user,
            guild_id=interaction.guild.id,
            channel_id=channel_id,
            game_id=game_id,
        )
        try:
            result = await asyncio.wait_for(
                game_tribe.run_tribe_read(request),
                timeout=20,
            )
        except game_workers.GameTribeValidationError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        except asyncio.TimeoutError:
            return await interaction.followup.send(
                'The current player tribes could not be loaded in time. Run '
                f'`/game tribe {game_id}` again.',
                ephemeral=True,
            )
        except peewee.PeeweeException:
            logger.exception('Database failure reading game tribes')
            return await interaction.followup.send(
                'The current player tribes could not be loaded.',
                ephemeral=True,
            )
        except Exception:
            logger.exception('Unexpected failure reading game tribes')
            return await interaction.followup.send(
                'The current player tribes could not be loaded.',
                ephemeral=True,
            )

        async def self_callback(native_interaction, tribe_token, snapshot):
            assignment = game_workers.GameTribeAssignmentInput(
                player_token=f'<@{native_interaction.user.id}>',
                tribe_token=str(tribe_token),
            )
            return await mutate(
                native_interaction,
                assignments=(assignment,),
                expected_snapshot=game_tribe.expected_snapshots(snapshot),
            )

        async def single_callback(
            native_interaction,
            player_token,
            tribe_token,
            snapshot,
        ):
            assignment = game_workers.GameTribeAssignmentInput(
                player_token=str(player_token),
                tribe_token=str(tribe_token),
            )
            return await mutate(
                native_interaction,
                assignments=(assignment,),
                expected_snapshot=game_tribe.expected_snapshots(snapshot),
            )

        async def bulk_preview_callback(
            native_interaction,
            raw_text,
            snapshot,
        ):
            preview_request = game_tribe.build_mutation_request(
                member=native_interaction.user,
                guild_id=native_interaction.guild.id,
                channel_id=int(
                    getattr(native_interaction, 'channel_id', None)
                    or getattr(
                        getattr(native_interaction, 'channel', None),
                        'id',
                        0,
                    )
                    or 0
                ),
                game_id=snapshot.game_id,
                raw_bulk=raw_text,
                native=True,
                require_elevated=True,
                invoked_with='/game tribe bulk preview',
            )
            try:
                return await asyncio.wait_for(
                    game_tribe.run_tribe_preview(preview_request),
                    timeout=20,
                )
            except game_workers.GameTribeValidationError as exc:
                await private_failure(native_interaction, str(exc))
            except asyncio.TimeoutError:
                await private_failure(
                    native_interaction,
                    'The bulk tribe preview timed out. Run `/game tribe '
                    f'{snapshot.game_id}` again.',
                )
            except peewee.PeeweeException:
                logger.exception('Database failure previewing game tribes')
                await private_failure(
                    native_interaction,
                    'The bulk tribe preview could not be loaded.',
                )
            except Exception:
                logger.exception('Unexpected failure previewing game tribes')
                await private_failure(
                    native_interaction,
                    'The bulk tribe preview could not be loaded.',
                )
            return None

        async def bulk_confirm_callback(native_interaction, preview):
            assignments = tuple(preview.assignments)
            return await mutate(
                native_interaction,
                assignments=assignments,
                expected_snapshot=preview.expected_snapshots,
                require_elevated=True,
            )

        workspace = game_tribe_views.GameTribeWorkspaceView(
            result,
            requester_id=interaction.user.id,
            on_self=self_callback,
            on_single=single_callback,
            on_bulk_preview=bulk_preview_callback,
            on_bulk_confirm=bulk_confirm_callback,
            requester_actor=actor,
        )
        public_send = game_tribe.public_interaction_sender(interaction)
        try:
            workspace.message = await public_send(
                game_tribe.read_message(result, actor=actor),
                view=workspace,
            )
        except Exception:
            logger.exception('Could not publish the game-tribe workspace')
            await interaction.followup.send(
                'The current player tribes were loaded but could not be '
                f'published. Run `/game tribe {game_id}` again.',
                ephemeral=True,
            )
        return workspace

    @commands.command(aliases=['settribes'], usage='game_id player_name tribe_name [player2 tribe2 ... ]')
    async def settribe(self, ctx, *, args: str = None):
        """Set tribe of players for a game

        **Examples**
        `[p]settribe 2055 ai-mo` - Sets your own tribe for a game you are in
        `[p]settribe bardur` - Sets your own tribe while in a game channel

        **Staff usage:**
        `[p]settribe 2055 nelluk bardur` - Sets Nelluk to Bardur for game 2050
        `[p]settribe 2050 nelluk bardur rick lux anarcho none` - Sets several tribes at once. Use *none* to unset a tribe.
        `[p]settribe nelluk bardur rick lux` - Set several tribes in bulk while in a game channel.
        """

        if not args:
            return await ctx.send(f'No arguments provided. **Example usage:** `{ctx.prefix}{ctx.invoked_with} 1234 bardur`')

        request = game_tribe.build_mutation_request(
            member=ctx.author,
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            legacy_tokens=tuple(args.split()),
            allow_related_channel=True,
            native=False,
            legacy_partial=True,
            invoked_with=ctx.invoked_with or 'settribe',
        )

        async def after_commit(result):
            await game_tribe.publish_legacy_mutation_result(
                result,
                send=ctx.send,
                destination=ctx,
                guild=ctx.guild,
                bot=getattr(self, 'bot', None),
                prefix=ctx.prefix,
                requester_id=int(ctx.author.id),
                channel_id=int(ctx.channel.id),
                requester_level=request.requester_level,
            )

        try:
            await game_tribe.run_tribe_mutation(
                request,
                after_commit=after_commit,
            )
        except game_workers.GameTribeLookupError as exc:
            return await ctx.send(
                f'{exc}\n**Example usage:** `{ctx.prefix}{ctx.invoked_with} '
                '1234 bardur`\nYou can also omit the game ID if you use the '
                'command from a game-specific channel.'
            )
        except game_workers.GameTribeValidationError as exc:
            message = str(exc)
            if message.startswith(
                'This command requires bot registration first.'
            ):
                message = message.replace(
                    '__`setname ',
                    f'__`{ctx.prefix}setname ',
                ).replace(
                    '__`steamname ',
                    f'__`{ctx.prefix}steamname ',
                )
            elif message.startswith('Wrong number of arguments.'):
                message = (
                    f'Wrong number of arguments. See `{ctx.prefix}help '
                    'settribe` for usage examples.'
                )
            return await ctx.send(message)
        except exceptions.RecordLocked as exc:
            return await ctx.send(str(exc))
        except peewee.PeeweeException:
            logger.exception('Database failure setting game tribes')
            return await ctx.send(
                'The tribe changes failed and rolled back. No Discord '
                'announcement or card update was made.'
            )
        except Exception:
            logger.exception('Unexpected failure setting game tribes')
            return await ctx.send(
                'The tribe changes failed. No Discord announcement or card '
                'update was made.'
            )
    
    @settings.in_bot_channel()
    @commands.command(usage='search_term', aliases=['gamelog', 'gamelogs', 'global_logs', 'log'])
    # @commands.cooldown(1, 20, commands.BucketType.user)
    async def logs(self, ctx, *, search_term: str = None):
        """Lists or searches log entries through the shared bounded reader.

         **Examples**
        `[p]logs` - See all recent entries
        `[p]logs 1234` - See all entries related to a specific game
        `[p]logs Nelluk` - See all entries containing the term Nelluk
        `[p]logs Nelluk join` - See all entries containing both words
        `[p]logs Nelluk -Kamfer` - See all entries containing the first word but *not* the second word

        `[p]global_logs` - *Owner only*: Search or list log entries across all bot servers
        """

        try:
            return await game_logs.run_prefix(ctx, search_term)
        except game_log_workers.GameLogReadError as exc:
            return await ctx.send(str(exc))
        except peewee.PeeweeException:
            logger.exception('Database failure reading prefix game logs')
            return await ctx.send(
                'The audit logs could not be loaded. Please try again later.'
            )

    async def game_search(self, ctx, mode: str, arg_list):
        target_list = [
            arg.replace('"', '') for arg in arg_list
            if len(arg.replace('"', '')) > 2
        ]
        show_all = (
            len(target_list) == 1 and target_list[0].upper() == 'ALL'
        )
        if show_all:
            target_list = []
        elif not target_list:
            target_list = [str(ctx.author.id)]
        key = {
            'ALLGAMES': game_search_workers.GameSearchKey(),
            'COMPLETE': game_search_workers.GameSearchKey(
                status='completed',
            ),
            'INCOMPLETE': game_search_workers.GameSearchKey(
                status='unfinished',
            ),
            'WINS': game_search_workers.GameSearchKey(outcome='win'),
            'LOSSES': game_search_workers.GameSearchKey(outcome='loss'),
        }.get(mode.upper(), game_search_workers.GameSearchKey())
        await self._send_game_search_workspace(
            ctx,
            query=' '.join(target_list),
            key=key,
        )

    async def task_purge_game_channels(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            await asyncio.sleep(900)
            logger.debug('Task running: task_purge_game_channels')
            await self.run_completed_channel_purge_cycle()

            await asyncio.sleep(60 * 60 * 6)

    async def run_completed_channel_purge_cycle(self):
        """Contain one completed-channel cycle without hiding cancellation."""

        try:
            return await completed_game_channel_purge.purge_completed_game_channels(
                bot=self.bot,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                'Completed-game channel purge cycle failed; the next '
                'scheduled cycle remains available'
            )
            return None

    async def run_champion_role_cycle(self):
        """Contain one recurring champion cycle without hiding cancellation."""

        try:
            return await achievements.set_champion_role()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                'Champion role cycle failed; the next scheduled cycle '
                'remains available'
            )
            return None

    async def task_set_champion_role(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():

            await asyncio.sleep(97)
            logger.debug('Task running: task_set_champion_role')
            await self.run_champion_role_cycle()

            await asyncio.sleep(60 * 60 * 2)


async def post_win_messaging(
    guild,
    prefix,
    current_chan,
    snapshot,
):
    await confirmation_publication.publish_confirmed_game(
        guild=guild,
        prefix=prefix,
        current_channel=current_chan,
        snapshot=snapshot,
        bot=settings.bot,
    )


async def post_unwin_messaging(
    guild,
    prefix,
    current_chan,
    snapshot,
    previously_confirmed: bool = False,
):
    del guild, prefix
    await game_result_publication.publish_unwin_result(
        snapshot=snapshot,
        current_channel=current_chan,
        previously_confirmed=previously_confirmed,
        bot=settings.bot,
    )


async def post_newgame_messaging(
    ctx,
    game,
    *,
    destination=_NEWGAME_DESTINATION_UNSET,
):
    """Publish committed new-game effects to an explicit public destination."""

    destination = _resolve_newgame_public_destination(ctx, destination)

    season, season_str = game.is_season_game(), ''
    if season:
        try:
            tier_name = settings.tier_lookup(game.league_tier)[1]
        except exceptions.NoMatches:
            tier_name = 'Unknown'
        season_str = f'**{tier_name} Season {season[0]}** '

    embed, content = game.embed(guild=ctx.guild, prefix=ctx.prefix)
    ranked_str = 'unranked ' if not game.is_ranked else ''
    platform_str = '' if game.is_mobile else 'Steam '
    announce_str = f'New {season_str}{ranked_str}{platform_str}game ID **{game.id}** started! Roster: {" ".join(game.mentions())}'

    if settings.guild_setting(ctx.guild.id, 'game_announce_channel'):
        channel = ctx.guild.get_channel(settings.guild_setting(ctx.guild.id, 'game_announce_channel'))
        if channel:
            await channel.send(f'{announce_str}')
            announcement = await image_storage.send_game_embed(
                channel, game, embed=embed, content=content
            )
            await destination.send(
                f'New {ranked_str}game ID **{game.id}** started! '
                f'See {channel.mention} for full details.'
            )
            game.announcement_message = announcement.id
            game.announcement_channel = announcement.channel.id
            game.save()
        else:
            await image_storage.send_game_embed(
                destination, game, embed=embed, content=content
            )
            await destination.send(
                'Error loading game announcement channel from server '
                'settings. Please inform the bot owner.'
            )
            logger.error(f'Could not load game_announce_channel channel for guild {ctx.guild.id}')

    else:
        await destination.send(f'{announce_str}')
        await image_storage.send_game_embed(
            destination, game, embed=embed, content=content
        )

    if settings.guild_setting(ctx.guild.id, 'game_channel_categories'):
        try:
            await game.create_game_channels(settings.bot.guilds, ctx.guild.id)
        except exceptions.MyBaseException as e:
            await destination.send(f':warning: **Channel creation error:** {e}')

    if game.is_uncaught_season_game():
        await destination.send(
            f':bulb: This game looks like an incorrectly named '
            f'**Season Game**! You might want to use `{ctx.prefix}rename` '
            'and include the season tag at the beginning.'
        )
    if season and game.gamesides[0].team.is_hidden:
        await destination.send(
            f':warning: This game is marked as a **Season Game** but is '
            'not associated with a League Team. There are probably players '
            'with mixed roles on a side. I suggest you '
            f'`{ctx.prefix}unstart`, fix the roles, and '
            f're-`{ctx.prefix}start`.'
        )
    if game.guild_id == settings.server_ids['polychampions'] and game.smallest_team() > 1:
        await refresh_league_team_channels(
            settings.server_ids['polychampions']
        )

    await auto_grad_novas(ctx.guild, game, destination)


def parse_players_and_teams(input_list, guild_id: int):
    # Given a [List, of, string, args], try to match each one against a Team or a Player, and return lists of those matches
    # return any args that matched nothing back in edited input_list

    player_matches, team_matches = [], []
    for arg in list(input_list):  # Copy of list
        if arg.upper() in ['THE', 'OF', 'AND', '&']:
            input_list.remove(arg)
            continue
        if arg.isupper():
            continue  # UPPER CASE alphabetical are ignored for player/team comparison and assumed to be title searches
        teams = Team.get_by_name(arg, guild_id)
        if len(teams) == 1:
            team_matches.append(teams[0])
            logger.debug(f'parse_players_and_teams - Matched string {arg} to team {teams[0].id} {teams[0].name}')
            input_list.remove(arg)
        else:
            players = Player.string_matches(player_string=arg, guild_id=guild_id, include_poly_info=False)
            if len(players) > 0:
                player_matches.append(players[0])
                logger.debug(f'parse_players_and_teams - Matched string {arg} to player {players[0].id} {players[0].name} on team {players[0].team}')
                input_list.remove(arg)

    return player_matches, team_matches, input_list


async def setup(bot):
    await bot.add_cog(polygames(bot))
    # bot.load_extension('modules.games')
