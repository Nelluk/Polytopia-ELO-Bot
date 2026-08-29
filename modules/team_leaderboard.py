"""Async adapters and retained prefix presentation for team rankings."""

from __future__ import annotations

import asyncio
from io import BytesIO
import logging
import uuid

import discord

import settings
from modules import exceptions, team_leaderboard_workers, team_record_scope


logger = logging.getLogger('polybot.' + __name__)

TEAM_LEADERBOARD_CONTROL_TIMEOUT = 300.0


def _setting(guild_id: int, name: str, default=None):
    try:
        return settings.guild_setting(int(guild_id), name)
    except (AttributeError, KeyError, TypeError, exceptions.CheckFailedError):
        return default


def _is_mod(member) -> bool:
    try:
        return bool(settings.is_mod(member))
    except (AttributeError, TypeError, exceptions.CheckFailedError):
        return False


def _channel_allowed(member, guild_id: int, channel_id: int | None) -> bool:
    bot_channels = _setting(guild_id, 'bot_channels', None)
    strict_channels = _setting(guild_id, 'bot_channels_strict', None)
    if strict_channels is None and bot_channels is None:
        return True
    if _is_mod(member):
        return True
    private_channels = _setting(guild_id, 'bot_channels_private', ()) or ()
    try:
        channel_choices = (
            strict_channels
            if strict_channels is not None
            else bot_channels
        )
        allowed_channels = {
            int(value)
            for value in (*channel_choices, *private_channels)
        }
    except (TypeError, ValueError):
        return False
    return channel_id is not None and int(channel_id) in allowed_channels


def native_access_error(
    member,
    guild_id: int,
    channel_id: int | None,
) -> str | None:
    """Mirror the retained prefix allow_teams and strict-channel checks."""

    if not bool(_setting(guild_id, 'allow_teams', False)):
        return 'Teams are not enabled on this server.'
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


def _role_color(role) -> str:
    value = getattr(role, 'color', None)
    if value is None:
        value = getattr(role, 'colour', None)
    numeric = getattr(value, 'value', None)
    if isinstance(numeric, int):
        return f'#{numeric:06x}'
    text = str(value or '').strip()
    return text if text and text != '0' else '#5865F2'


def _member_id(member) -> int | None:
    try:
        return int(getattr(member, 'id'))
    except (AttributeError, TypeError, ValueError):
        return None


def capture_role_snapshots(
    guild,
    *,
    inactive_role_name: str | None,
) -> tuple[team_leaderboard_workers.TeamLeaderboardRoleSnapshot, ...]:
    """Freeze role names/colors/counts before submitting a DB worker."""

    roles = tuple(getattr(guild, 'roles', ()) or ())
    inactive_role = next(
        (
            role for role in roles
            if inactive_role_name
            and str(getattr(role, 'name', '')) == str(inactive_role_name)
        ),
        None,
    )
    inactive_ids = {
        member_id
        for member in tuple(getattr(inactive_role, 'members', ()) or ())
        if (member_id := _member_id(member)) is not None
    }
    snapshots = []
    for role in roles:
        member_ids = tuple(
            member_id
            for member in tuple(getattr(role, 'members', ()) or ())
            if (member_id := _member_id(member)) is not None
        )
        snapshots.append(
            team_leaderboard_workers.TeamLeaderboardRoleSnapshot(
                role_name=str(getattr(role, 'name', '')),
                role_color=_role_color(role),
                active_member_count=sum(
                    member_id not in inactive_ids
                    for member_id in member_ids
                ),
            )
        )
    return tuple(snapshots)


def configured_tier_choices() -> tuple[tuple[int, str], ...]:
    """Return the configured tier vocabulary as immutable primitives."""

    return tuple(
        (int(number), str(name))
        for number, name in tuple(getattr(settings, 'league_tiers', ()) or ())
    )


def _database_guild_id(guild_id: int) -> int:
    record_guild_id = team_record_scope.persistent_team_guild_id(guild_id)
    if record_guild_id != int(guild_id):
        return record_guild_id
    try:
        if int(guild_id) == int(settings.server_ids['test']):
            return int(settings.server_ids['polychampions'])
    except (AttributeError, KeyError, TypeError, ValueError):
        pass
    return int(guild_id)


def _attachment_name() -> str:
    return f'team-elo-{uuid.uuid4().hex}.png'


def build_request(
    *,
    member,
    guild,
    tier_number: int | None = None,
    include_archived: bool = False,
    load_all_filters: bool = False,
    channel_id: int | None = None,
) -> team_leaderboard_workers.TeamLeaderboardRequest:
    """Capture Discord/config primitives for one bounded team read."""

    guild_id = int(guild.id)
    inactive_role = settings.resolve_configured_role(guild, 'inactive_role')
    inactive_role_name = (
        str(inactive_role.name) if inactive_role is not None else None
    )
    return team_leaderboard_workers.TeamLeaderboardRequest(
        guild_id=guild_id,
        database_guild_id=_database_guild_id(guild_id),
        include_archived=bool(include_archived),
        tier_number=(int(tier_number) if tier_number is not None else None),
        role_snapshots=capture_role_snapshots(
            guild,
            inactive_role_name=(
                str(inactive_role_name) if inactive_role_name else None
            ),
        ),
        graph_attachment_name=_attachment_name(),
        load_all_filters=bool(load_all_filters),
        team_enabled=bool(_setting(guild_id, 'allow_teams', False)),
        channel_allowed=_channel_allowed(
            member,
            guild_id,
            channel_id,
        ),
        require_role_match=True,
    )


def build_request_for_context(
    *,
    member,
    guild,
    channel_id: int | None,
    tier_number: int | None = None,
    include_archived: bool = False,
    load_all_filters: bool = False,
) -> team_leaderboard_workers.TeamLeaderboardRequest:
    """Context-aware request builder used by prefix and native adapters."""

    return build_request(
        member=member,
        guild=guild,
        channel_id=channel_id,
        tier_number=tier_number,
        include_archived=include_archived,
        load_all_filters=load_all_filters,
    )


def parse_prefix_filters(
    arg: str | None,
) -> tuple[int | None, str | None, bool]:
    """Preserve ``old`` plus tier-name/number prefix grammar."""

    args = str(arg).lower().split() if arg else []
    include_archived = 'old' in args
    remaining = [value for value in args if value != 'old']
    if not remaining:
        return None, None, include_archived
    tier_number, tier_name = settings.tier_lookup(remaining[0])
    return int(tier_number), str(tier_name), include_archived


def team_leaderboard_request_for_prefix(
    *,
    ctx,
    tier_number: int | None,
    include_archived: bool,
) -> team_leaderboard_workers.TeamLeaderboardRequest:
    """Capture the exact prefix policy and selected filters."""

    return build_request_for_context(
        member=ctx.author,
        guild=ctx.guild,
        channel_id=ctx.message.channel.id,
        tier_number=tier_number,
        include_archived=include_archived,
    )


def team_leaderboard_request_for_native(
    interaction: discord.Interaction,
) -> team_leaderboard_workers.TeamLeaderboardRequest:
    """Capture the native default snapshot before worker submission."""

    return build_request_for_context(
        member=interaction.user,
        guild=interaction.guild,
        channel_id=interaction.channel_id,
        include_archived=True,
        load_all_filters=True,
    )


def _prefix_embed(
    page: team_leaderboard_workers.TeamLeaderboardPage,
    graph: team_leaderboard_workers.TeamLeaderboardGraph,
) -> discord.Embed:
    embed = discord.Embed(title=f'**{page.title}**')
    for row in page.rows:
        embed.add_field(
            name=(
                f'{row.team_emoji} {row.rank:>3}. **{row.team_name}** '
                f'({row.member_count})\n'
                f'`ELO: {row.elo:<5} W {row.wins} / L {row.losses}`'
            )[:256],
            value='\u200b',
            inline=False,
        )
    if not page.rows:
        embed.description = 'No ranked teams found.'
    embed.set_footer(
        text=(
            f'Page {page.page_index + 1} of {page.page_count}'
            f' • showing {page.start_rank or 0}-{page.end_rank or 0}'
            f' of {page.total_teams}'
        )
    )
    if graph.png_bytes:
        embed.set_image(url=f'attachment://{graph.filename}')
    return embed


def _graph_file(
    graph: team_leaderboard_workers.TeamLeaderboardGraph,
) -> discord.File | None:
    if not graph.png_bytes:
        return None
    return discord.File(
        BytesIO(graph.png_bytes),
        filename=graph.filename,
    )


async def _render_graph(
    result: team_leaderboard_workers.TeamLeaderboardResult,
    *,
    tier_number: int | None,
    include_archived: bool,
    page_index: int,
) -> team_leaderboard_workers.TeamLeaderboardGraph:
    page = team_leaderboard_workers.team_leaderboard_page(
        result,
        tier_number=tier_number,
        include_archived=include_archived,
        page_index=page_index,
    )
    return await team_leaderboard_workers.run_team_leaderboard_graph(
        page,
        result.graph_attachment_name,
    )


async def render_page_graph(
    result: team_leaderboard_workers.TeamLeaderboardResult,
    *,
    tier_number: int | None,
    include_archived: bool,
    page_index: int,
) -> tuple[
    team_leaderboard_workers.TeamLeaderboardPage,
    team_leaderboard_workers.TeamLeaderboardGraph,
]:
    """Materialize a page and its bounded graph from one immutable result."""

    page = team_leaderboard_workers.team_leaderboard_page(
        result,
        tier_number=tier_number,
        include_archived=include_archived,
        page_index=page_index,
    )
    graph = await team_leaderboard_workers.run_team_leaderboard_graph(
        page,
        result.graph_attachment_name,
    )
    return page, graph


async def publish_prefix(
    ctx,
    result: team_leaderboard_workers.TeamLeaderboardResult,
    *,
    tier_number: int | None,
    include_archived: bool,
) -> None:
    """Retain reaction pagination while using one immutable loaded snapshot."""

    page_index = 0
    graph = await _render_graph(
        result,
        tier_number=tier_number,
        include_archived=include_archived,
        page_index=page_index,
    )
    page = team_leaderboard_workers.team_leaderboard_page(
        result,
        tier_number=tier_number,
        include_archived=include_archived,
        page_index=page_index,
    )
    file = _graph_file(graph)
    message = await ctx.send(
        embed=_prefix_embed(page, graph),
        **({'file': file} if file is not None else {}),
    )
    if page.page_count <= 1:
        return

    for emoji in ('⏪', '⬅', '➡', '⏩'):
        await message.add_reaction(emoji)

    bot = getattr(ctx, 'bot', None) or getattr(settings, 'bot', None)
    current_reaction = None
    current_user = None
    try:
        while True:
            def check(reaction, user):
                emoji = str(reaction.emoji)
                return (
                    int(getattr(user, 'id', 0))
                    == int(getattr(ctx.author, 'id', 0))
                    and int(getattr(reaction.message, 'id', 0))
                    == int(getattr(message, 'id', 0))
                    and emoji in {'⏪', '⬅', '➡', '⏩'}
                )

            reaction, user = await bot.wait_for(
                'reaction_add',
                timeout=45.0,
                check=check,
            )
            current_reaction, current_user = reaction, user
            emoji = str(reaction.emoji)
            if emoji == '⏪':
                page_index = 0
            elif emoji == '⏩':
                page_index = page.page_count - 1
            elif emoji == '➡':
                page_index = min(page_index + 1, page.page_count - 1)
            elif emoji == '⬅':
                page_index = max(page_index - 1, 0)

            graph = await _render_graph(
                result,
                tier_number=tier_number,
                include_archived=include_archived,
                page_index=page_index,
            )
            page = team_leaderboard_workers.team_leaderboard_page(
                result,
                tier_number=tier_number,
                include_archived=include_archived,
                page_index=page_index,
            )
            file = _graph_file(graph)
            edit_kwargs = {
                'embed': _prefix_embed(page, graph),
                'attachments': ([file] if file is not None else []),
            }
            await message.edit(**edit_kwargs)
            try:
                await current_reaction.remove(current_user)
            except (discord.Forbidden, discord.HTTPException):
                logger.warning(
                    'Could not remove team leaderboard pagination reaction'
                )
    except asyncio.TimeoutError:
        try:
            await message.clear_reactions()
        except (discord.Forbidden, discord.HTTPException):
            logger.warning(
                'Could not clear team leaderboard pagination reactions'
            )


def page_and_embed(
    result: team_leaderboard_workers.TeamLeaderboardResult,
    *,
    tier_number: int | None,
    include_archived: bool,
    page_index: int,
    graph: team_leaderboard_workers.TeamLeaderboardGraph,
):
    """Return the classic prefix page and embed from one snapshot."""

    page = team_leaderboard_workers.team_leaderboard_page(
        result,
        tier_number=tier_number,
        include_archived=include_archived,
        page_index=page_index,
    )
    return page, _prefix_embed(page, graph)
