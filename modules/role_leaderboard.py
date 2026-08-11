"""Discord adapters and public presentation helpers for role leaderboards."""

from __future__ import annotations

import datetime
import logging

import discord

import settings
from modules import exceptions, role_leaderboard_workers, utilities
from modules.league import (
    coleader_role_name,
    free_agent_role_name,
    leader_role_name,
)


logger = logging.getLogger('polybot.' + __name__)

ROLE_LEADERBOARD_CONTROL_TIMEOUT = 300.0


def _setting(guild_id: int, name: str, default=None):
    try:
        return settings.guild_setting(int(guild_id), name)
    except (AttributeError, KeyError, TypeError, exceptions.CheckFailedError):
        return default


def _is_staff(member) -> bool:
    try:
        return bool(settings.is_staff(member))
    except (AttributeError, TypeError, exceptions.CheckFailedError):
        return False


def is_house_leader(member) -> bool:
    role_names = {
        str(getattr(role, 'name', ''))
        for role in tuple(getattr(member, 'roles', ()) or ())
    }
    return bool(role_names.intersection({leader_role_name, coleader_role_name}))


def requester_can_select_roles(member) -> bool:
    return _is_staff(member) or is_house_leader(member)


def _channel_allowed(
    member,
    guild_id: int,
    channel_id: int | None,
) -> bool:
    strict_channels = _setting(guild_id, 'bot_channels_strict', None)
    bot_channels = _setting(guild_id, 'bot_channels', None)
    if strict_channels is None and bot_channels is None:
        return True
    if _setting(guild_id, 'bot_channels_private', None) is None:
        private_channels = ()
    else:
        private_channels = _setting(guild_id, 'bot_channels_private', ()) or ()
    try:
        if settings.is_mod(member):
            return True
    except (AttributeError, TypeError, exceptions.CheckFailedError):
        pass
    channel_choices = (
        strict_channels
        if strict_channels is not None
        else bot_channels
    )
    try:
        allowed_channels = {
            int(value)
            for value in (*channel_choices, *private_channels)
        }
    except (TypeError, ValueError):
        return False
    return channel_id is not None and int(channel_id) in allowed_channels


def _server_allowed(member, guild_id: int) -> bool:
    try:
        polychampions_id = int(settings.server_ids['polychampions'])
    except (AttributeError, KeyError, TypeError, ValueError):
        polychampions_id = None
    return int(guild_id) == polychampions_id or _is_staff(member)


def native_access_error(member, guild_id: int, channel_id: int | None) -> str | None:
    """Retain the legacy role lookup's server and strict-channel policy."""

    if not _server_allowed(member, guild_id):
        return (
            'You\'re not permitted to use this command. Only staff may use '
            'role lookups on this server.'
        )
    if _channel_allowed(member, guild_id, channel_id):
        return None
    strict_channels = _setting(guild_id, 'bot_channels_strict', None)
    bot_channels = (
        strict_channels
        if strict_channels is not None
        else (_setting(guild_id, 'bot_channels', ()) or ())
    )
    tags = ' '.join(f'<#{int(value)}>' for value in bot_channels)
    return (
        'This command can only be used in a designated bot spam channel. '
        f'Try: {tags}'
    )


def _role_id(role) -> int | None:
    try:
        value = int(getattr(role, 'id'))
    except (AttributeError, TypeError, ValueError):
        return None
    return value if value > 0 else None


def _role_is_default(role) -> bool:
    is_default = getattr(role, 'is_default', None)
    if callable(is_default):
        try:
            if is_default():
                return True
        except Exception:
            pass
    return str(getattr(role, 'name', '')) == '@everyone'


def _role_snapshot(role) -> role_leaderboard_workers.RoleLeaderboardRoleSnapshot:
    role_id = _role_id(role)
    if role_id is None:
        raise role_leaderboard_workers.RoleLeaderboardValidationError(
            'The guild contains a role without a valid Discord ID.'
        )
    return role_leaderboard_workers.RoleLeaderboardRoleSnapshot(
        role_id=role_id,
        name=str(getattr(role, 'name', '')),
        managed=bool(getattr(role, 'managed', False)),
        is_default=_role_is_default(role),
    )


def capture_guild_snapshot(
    guild,
    *,
    include_all_members: bool = True,
) -> tuple[
    tuple[role_leaderboard_workers.RoleLeaderboardRoleSnapshot, ...],
    tuple[role_leaderboard_workers.RoleLeaderboardMemberSnapshot, ...],
    int | None,
]:
    """Freeze current-guild roles/members before a worker is submitted."""

    roles = []
    role_ids = set()
    for role in tuple(getattr(guild, 'roles', ()) or ()):
        snapshot = _role_snapshot(role)
        if snapshot.role_id in role_ids:
            continue
        role_ids.add(snapshot.role_id)
        roles.append(snapshot)
    roles = tuple(roles)
    role_by_id = {role.role_id: role for role in roles}
    members = []
    for member in sorted(
        tuple(getattr(guild, 'members', ()) or ()),
        key=lambda value: int(getattr(value, 'id', 0)),
    )[:role_leaderboard_workers.MAX_ROLE_MEMBER_SNAPSHOTS]:
        member_id = _role_id(member)
        if member_id is None:
            continue
        member_role_ids = tuple(sorted({
            role_id
            for role in tuple(getattr(member, 'roles', ()) or ())
            if (role_id := _role_id(role)) is not None
            and role_id in role_by_id
        }))
        if include_all_members:
            members.append(
                role_leaderboard_workers.RoleLeaderboardMemberSnapshot(
                    discord_id=member_id,
                    name=str(
                        getattr(member, 'display_name', None)
                        or getattr(member, 'name', '')
                    ),
                    role_ids=member_role_ids,
                )
            )
        elif member_role_ids:
            members.append(
                role_leaderboard_workers.RoleLeaderboardMemberSnapshot(
                    discord_id=member_id,
                    name=str(
                        getattr(member, 'display_name', None)
                        or getattr(member, 'name', '')
                    ),
                    role_ids=member_role_ids,
                )
            )
    inactive_role = settings.resolve_configured_role(guild, 'inactive_role')
    inactive_role_id = (
        int(inactive_role.id) if inactive_role is not None else None
    )
    return roles, tuple(members), inactive_role_id


def _role_by_name(
    snapshots: tuple[role_leaderboard_workers.RoleLeaderboardRoleSnapshot, ...],
    name: str,
) -> role_leaderboard_workers.RoleLeaderboardRoleSnapshot | None:
    return next(
        (role for role in snapshots if role.name == str(name)),
        None,
    )


def _validate_selected_role_ids(
    role_ids: tuple[int, ...],
    role_snapshots: tuple[role_leaderboard_workers.RoleLeaderboardRoleSnapshot, ...],
) -> tuple[role_leaderboard_workers.RoleLeaderboardRoleSnapshot, ...]:
    if not 1 <= len(role_ids) <= role_leaderboard_workers.MAX_SELECTED_ROLES:
        raise role_leaderboard_workers.RoleLeaderboardValidationError(
            f'Select between 1 and {role_leaderboard_workers.MAX_SELECTED_ROLES} roles.'
        )
    if len(set(role_ids)) != len(role_ids):
        raise role_leaderboard_workers.RoleLeaderboardValidationError(
            'Selected roles must be unique.'
        )
    by_id = {role.role_id: role for role in role_snapshots}
    selected = []
    for role_id in role_ids:
        role = by_id.get(int(role_id))
        if role is None:
            raise role_leaderboard_workers.RoleLeaderboardValidationError(
                'One or more selected roles are outside this guild.'
            )
        if role.is_default or role.managed:
            raise role_leaderboard_workers.RoleLeaderboardValidationError(
                'Everyone, bot-managed, and integration roles cannot be used.'
            )
        selected.append(role)
    return tuple(selected)


def validate_role_values(
    values,
    *,
    guild_id: int,
    role_snapshots: tuple[role_leaderboard_workers.RoleLeaderboardRoleSnapshot, ...],
) -> tuple[int, ...]:
    """Validate RoleSelect objects at the interaction boundary."""

    role_ids = []
    for value in tuple(values or ()):
        role_id = _role_id(value)
        if role_id is None:
            raise role_leaderboard_workers.RoleLeaderboardValidationError(
                'The selected role is invalid.'
            )
        source_guild = getattr(value, 'guild', None)
        source_guild_id = getattr(source_guild, 'id', None)
        if source_guild_id is not None and int(source_guild_id) != int(guild_id):
            raise role_leaderboard_workers.RoleLeaderboardValidationError(
                'Roles must come from the current guild.'
            )
        role_ids.append(role_id)
    _validate_selected_role_ids(tuple(role_ids), role_snapshots)
    return tuple(role_ids)


def _global_guild_ids() -> tuple[int, ...]:
    return tuple(
        int(guild_id)
        for guild_id in tuple(settings.servers_included_in_global_lb())
    )


def _build_request(
    *,
    guild,
    selected_role_ids: tuple[int, ...] | None,
    match_mode: str,
    sort_key: str,
    include_all_members: bool,
) -> role_leaderboard_workers.RoleLeaderboardRequest:
    roles, members, inactive_role_id = capture_guild_snapshot(
        guild,
        include_all_members=include_all_members,
    )
    free_agent = _role_by_name(roles, free_agent_role_name)
    if free_agent is None:
        raise role_leaderboard_workers.RoleLeaderboardValidationError(
            f'Could not find the configured {free_agent_role_name} role.'
        )
    if selected_role_ids is None:
        selected_role_ids = (free_agent.role_id,)
    selected = _validate_selected_role_ids(tuple(selected_role_ids), roles)
    if include_all_members:
        candidate_members = members
    else:
        selected_set = set(selected_role_ids)
        candidate_members = tuple(
            member for member in members
            if (
                selected_set.issubset(member.role_ids)
                if match_mode == 'all'
                else bool(selected_set.intersection(member.role_ids))
            )
        )
    return role_leaderboard_workers.RoleLeaderboardRequest(
        guild_id=int(guild.id),
        selected_role_ids=tuple(role.role_id for role in selected),
        selected_role_names=tuple(role.name for role in selected),
        match_mode=str(match_mode),
        sort_key=str(sort_key),
        scope='global',
        member_snapshots=tuple(candidate_members),
        role_snapshots=roles,
        inactive_role_id=inactive_role_id,
        global_guild_ids=_global_guild_ids(),
        recent_cutoff=(
            datetime.datetime.now()
            - datetime.timedelta(days=14)
        ),
    )


def request_for_native(*, guild, selected_role_ids=None, match_mode='all'):
    """Capture the all-member native snapshot; refinements stay DB-free."""

    return _build_request(
        guild=guild,
        selected_role_ids=(
            tuple(selected_role_ids)
            if selected_role_ids is not None
            else None
        ),
        match_mode=match_mode,
        sort_key='global_elo',
        include_all_members=True,
    )


def request_for_prefix(ctx, arg: str | None = None):
    """Capture the retained Free Agents prefix convenience read."""

    args = str(arg).split() if arg else []
    if '-file' in {value.lower() for value in args}:
        raise role_leaderboard_workers.RoleLeaderboardValidationError(
            'CSV/file export is deferred for the native role leaderboard.'
        )
    sort_key = 'global_elo'
    if args:
        sort_key = {
            'g_elo': 'global_elo',
            'elo': 'local_elo',
            'games': 'total_games',
            'recent': 'recent_games',
        }.get(args[0].lower(), sort_key)
    return _build_request(
        guild=ctx.guild,
        selected_role_ids=None,
        match_mode='all',
        sort_key=sort_key,
        include_all_members=False,
    )


def _safe(value: object) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(value)))


def prefix_rows(
    result: role_leaderboard_workers.RoleLeaderboardResult,
    request: role_leaderboard_workers.RoleLeaderboardRequest,
) -> tuple[role_leaderboard_workers.RoleLeaderboardRow, ...]:
    page = role_leaderboard_workers.role_leaderboard_page(
        result,
        selected_role_ids=request.selected_role_ids,
        selected_role_names=request.selected_role_names,
        match_mode=request.match_mode,
        sort_key=request.sort_key,
        scope='global',
        page_size=max(1, len(result.rows)),
        # Keep the retained prefix convenience command's historical
        # low-to-high ordering while native Components use descending ranks.
        descending=False,
    )
    return page.rows


def prefix_row_text(row: role_leaderboard_workers.RoleLeaderboardRow) -> str:
    return (
        f' <@{row.discord_id}> **{_safe(row.name)}**\n'
        f'     {row.recent_games} games in the last 14 days, '
        f'{row.total_games} all-time\n'
        f'     ELO: {row.global_elo} global / {row.local_elo} local\n'
        f'     __W {row.global_wins} / L {row.global_losses}__ global '
        f'— __W {row.local_wins} / L {row.local_losses}__ local\n'
    )


async def publish_prefix(ctx, result, request) -> None:
    rows = prefix_rows(result, request)
    if not rows:
        await ctx.send('No matching players found.')
        return
    await ctx.send(
        f'Listing {len(rows)} active members with the ALL match of the '
        f'following roles: **{"/".join(request.selected_role_names)}** '
        f'(sorted by {request.sort_key})...',
        allowed_mentions=discord.AllowedMentions.none(),
    )
    await utilities.buffered_send(
        destination=ctx,
        content=''.join(prefix_row_text(row) for row in rows),
        allowed_mentions=discord.AllowedMentions.none(),
    )
