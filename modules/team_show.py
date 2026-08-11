"""Shared async service and dense presentation for ``/team show``."""

from __future__ import annotations

from io import BytesIO
import logging
import time

import discord

import settings
from modules import (
    exceptions,
    interaction_lifecycle,
    team_emoji,
    team_show_workers,
)


logger = logging.getLogger('polybot.' + __name__)

TEAM_SHOW_CONTROL_TIMEOUT = 300.0


def _setting(guild_id: int, name: str, default=None):
    try:
        value = settings.guild_setting(int(guild_id), name)
    except (AttributeError, KeyError, TypeError, exceptions.CheckFailedError):
        return default
    return value


def _is_mod(member) -> bool:
    try:
        return bool(settings.is_mod(member))
    except (AttributeError, TypeError, exceptions.CheckFailedError):
        return False


def _role_id(role) -> int | None:
    value = getattr(role, 'id', None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _member_id(member) -> int:
    return int(getattr(member, 'id'))


def _member_mention(member, member_id: int) -> str:
    mention = getattr(member, 'mention', None)
    if callable(mention):
        mention = mention()
    return str(mention or f'<@{int(member_id)}>')


def _capture_member(member) -> team_show_workers.TeamShowMemberSnapshot:
    member_id = _member_id(member)
    name = str(
        getattr(member, 'name', None)
        or getattr(member, 'display_name', None)
        or f'user-{member_id}'
    )
    display_name = str(
        getattr(member, 'display_name', None)
        or getattr(member, 'name', None)
        or f'user-{member_id}'
    )
    return team_show_workers.TeamShowMemberSnapshot(
        discord_id=member_id,
        name=name,
        display_name=display_name,
        mention=_member_mention(member, member_id),
    )


def capture_guild_snapshot(guild) -> team_show_workers.TeamShowGuildSnapshot:
    """Capture exact role membership and stable member text on the event loop."""

    member_snapshots = {}
    role_snapshots = []
    for role in tuple(getattr(guild, 'roles', ()) or ()):
        role_members = tuple(getattr(role, 'members', ()) or ())
        member_ids = []
        for member in role_members:
            member_id = _member_id(member)
            member_ids.append(member_id)
            member_snapshots.setdefault(member_id, _capture_member(member))
        role_snapshots.append(
            team_show_workers.TeamShowRoleSnapshot(
                role_id=_role_id(role),
                role_name=str(getattr(role, 'name', '')),
                member_ids=tuple(member_ids),
            )
        )

    # A role's member cache is the authoritative membership source used by the
    # legacy card.  Include guild.members as a display fallback for a role
    # cache that contains IDs but lacks a full member object.
    for member in tuple(getattr(guild, 'members', ()) or ()):
        member_id = _member_id(member)
        member_snapshots.setdefault(member_id, _capture_member(member))

    return team_show_workers.TeamShowGuildSnapshot(
        guild_id=int(getattr(guild, 'id')),
        roles=tuple(role_snapshots),
        members=tuple(
            member_snapshots[member_id]
            for member_id in sorted(member_snapshots)
        ),
    )


def _leadership_enabled(guild_id: int) -> bool:
    try:
        return int(guild_id) in {
            int(settings.server_ids['polychampions']),
            int(settings.server_ids['test']),
        }
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def _channel_allowed(member, guild_id: int, channel_id: int | None) -> bool:
    bot_channels = _setting(guild_id, 'bot_channels', None)
    if bot_channels is None or _is_mod(member):
        return True
    private_channels = _setting(guild_id, 'bot_channels_private', ()) or ()
    try:
        allowed_channels = {
            int(value)
            for value in (*bot_channels, *private_channels)
        }
    except (TypeError, ValueError):
        return False
    return channel_id is not None and int(channel_id) in allowed_channels


def native_access_error(
    member,
    guild_id: int,
    channel_id: int | None,
) -> str | None:
    """Return private pre-defer errors for the retained prefix boundaries."""

    if not bool(_setting(guild_id, 'allow_teams', False)):
        return 'Teams are not enabled on this server.'
    if _channel_allowed(member, guild_id, channel_id):
        return None
    bot_channels = _setting(guild_id, 'bot_channels', ()) or ()
    tags = ' '.join(f'<#{int(value)}>' for value in bot_channels)
    return (
        'This command can only be used in a designated ELO bot channel. '
        f'Try: {tags}'
    )


def build_request(
    *,
    member,
    guild,
    team_lookup: str | None = None,
    activity_mode: str = team_show_workers.TEAM_ACTIVITY_RECENT,
    native: bool = True,
    invoked_with: str = '/team show',
    prefix: str = '$',
    channel_id: int | None = None,
) -> team_show_workers.TeamShowRequest:
    """Capture every Discord value before the bounded worker starts."""

    guild_id = int(guild.id)
    team_enabled = bool(_setting(guild_id, 'allow_teams', False))
    inactive_role = settings.resolve_configured_role(guild, 'inactive_role')
    return team_show_workers.TeamShowRequest(
        guild_id=guild_id,
        requester_id=int(member.id),
        team_lookup=(str(team_lookup) if team_lookup is not None else None),
        activity_mode=str(activity_mode),
        team_enabled=team_enabled,
        channel_allowed=_channel_allowed(member, guild_id, channel_id),
        leadership_enabled=_leadership_enabled(guild_id),
        inactive_role_name=(
            str(inactive_role.name) if inactive_role is not None else None
        ),
        guild_snapshot=capture_guild_snapshot(guild),
        team_elo_reset_label=str(getattr(settings, 'team_elo_reset_date', '')),
        requester_description=team_emoji.capture_actor(member).identity,
        native=bool(native),
        invoked_with=str(invoked_with),
        prefix=str(prefix),
    )


async def run(request: team_show_workers.TeamShowRequest):
    return await team_show_workers.run_team_show(request)


def _escape(value: object) -> str:
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value)),
    )


def _roster_line(row: team_show_workers.TeamShowRosterRow, *, completed: bool) -> str:
    games = row.completed_games if completed else row.recent_games
    if not row.registered:
        elo = '-'
        rank = '-'
        game_value = '-'
    else:
        elo = str(row.elo)
        rank = f'#{row.rank}' if row.rank else '-'
        game_value = str(games)
    line = (
        f'`{row.name[:23]:.<25}{elo:.<8}{rank:.<6}{game_value:.<4}`'
    )
    return line.replace('.', '\u200b ')


def _sorted_roster_rows(
    result: team_show_workers.TeamShowResult,
    *,
    completed: bool,
) -> tuple[team_show_workers.TeamShowRosterRow, ...]:
    """Sort each presentation by its visible metric, keeping stable ties."""

    return tuple(
        sorted(
            result.roster_rows,
            key=(
                lambda row: (
                    row.completed_games if completed else row.recent_games
                )
            ),
            reverse=True,
        )
    )


def render_embed(
    result: team_show_workers.TeamShowResult,
    *,
    completed: bool | None = None,
) -> discord.Embed:
    """Build the established dense card from one immutable result."""

    if completed is None:
        completed = result.activity_mode == team_show_workers.TEAM_ACTIVITY_COMPLETED
    house_str = (
        f'\nHouse {result.house_name} {result.house_emoji or ""}'
        if result.house_name
        else ''
    )
    embed = discord.Embed(
        title=f'Team card for **{result.team_name}** '
        f'{result.team_emoji}{house_str}'
    )
    embed.add_field(
        name='Results',
        value=(
            f'ELO: {result.elo}   Wins {result.wins} / '
            f'Losses {result.losses}'
        ),
        inline=False,
    )

    if result.team_role_found:
        roster_rows = _sorted_roster_rows(result, completed=bool(completed))
        header = (
            '__Player - ELO - Ranking - Completed Games__'
            if completed
            else '__Player - ELO - Ranking - Recent Games__'
        )
        members = '\n'.join(
            _roster_line(row, completed=bool(completed))
            for row in roster_rows[:50]
        ) or '\u200b'
        embed.description = (
            f'**Members({len(result.roster_rows)})**\n'
            f'{header}\n{members}'
        )[:4000]

    for label, values in (
        ('**House Leader**', result.leaders),
        ('**House Co-Leaders**', result.coleaders),
        ('**Team Recruiters**', result.recruiters),
        ('**Team Captains**', result.captains),
    ):
        if values:
            embed.add_field(name=label, value=', '.join(values), inline=True)

    if result.local_image_bytes is not None:
        embed.set_thumbnail(
            url=f'attachment://team-logo-{result.team_id}.png'
        )
    elif result.image_url:
        embed.set_thumbnail(url=result.image_url)

    if result.graph_bytes:
        embed.set_image(url=f'attachment://team-elo-{result.team_id}.png')

    embed.add_field(name='**Recent games**', value='\u200b', inline=False)
    for game, summary in result.recent_games:
        embed.add_field(name=game, value=summary)

    return embed


def render_files(result: team_show_workers.TeamShowResult) -> tuple[discord.File, ...]:
    files = []
    if result.graph_bytes:
        files.append(
            discord.File(
                BytesIO(result.graph_bytes),
                filename=f'team-elo-{result.team_id}.png',
            )
        )
    if result.local_image_bytes is not None:
        files.append(
            discord.File(
                BytesIO(result.local_image_bytes),
                filename=f'team-logo-{result.team_id}.png',
            )
        )
    return tuple(files)


def render_content(result: team_show_workers.TeamShowResult) -> str | None:
    if not result.missing_role_name:
        return None
    return (
        f':no_entry_sign: No matching discord role '
        f'"{_escape(result.missing_role_name)}" could be found. '
        'Player membership cannot be detected.'
    )


class TeamShowView(discord.ui.View):
    """One requester-bound, expiry-safe activity toggle over the dense card."""

    def __init__(
        self,
        result: team_show_workers.TeamShowResult,
        *,
        requester_id: int,
        completed: bool | None = None,
        timeout: float = TEAM_SHOW_CONTROL_TIMEOUT,
    ):
        super().__init__(timeout=timeout)
        self.result = result
        self.requester_id = int(requester_id)
        self.completed = (
            result.activity_mode == team_show_workers.TEAM_ACTIVITY_COMPLETED
            if completed is None
            else bool(completed)
        )
        self.expires_at = time.monotonic() + float(timeout)
        self.message = None
        self._expired = False
        self.activity_button = discord.ui.Button(
            label=(
                'Show recent 30 days'
                if self.completed
                else 'Show all completed games'
            ),
            style=discord.ButtonStyle.secondary,
            custom_id=f'team-show:{result.team_id}:{self.requester_id}',
        )
        self.activity_button.callback = self._activity_clicked
        self.add_item(self.activity_button)

    async def _send_private(self, interaction, content: str) -> None:
        response = getattr(interaction, 'response', None)
        is_done = getattr(response, 'is_done', None)
        if callable(is_done) and is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await response.send_message(content, ephemeral=True)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if (
            self._expired
            or self.is_finished()
            or time.monotonic() >= self.expires_at
        ):
            self._expired = True
            self.activity_button.disabled = True
            await self._send_private(
                interaction,
                'This team card control expired. Run `/team show` again for a '
                'fresh card.',
            )
            return False
        if int(interaction.user.id) != self.requester_id:
            await self._send_private(
                interaction,
                'Only the member who opened this team card can use its control.',
            )
            return False
        return True

    async def _activity_clicked(self, interaction: discord.Interaction) -> None:
        if not await self.interaction_check(interaction):
            return
        self.completed = not self.completed
        self.activity_button.label = (
            'Show recent 30 days'
            if self.completed
            else 'Show all completed games'
        )
        try:
            await interaction.response.edit_message(
                embed=render_embed(self.result, completed=self.completed),
                view=self,
            )
        except Exception:
            logger.exception('Could not refresh team card activity view')
            await self._send_private(
                interaction,
                'The team card could not be refreshed. Run `/team show` again.',
            )

    async def on_timeout(self) -> None:
        self._expired = True
        self.activity_button.disabled = True
        self.stop()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except Exception:
                logger.debug('Expired team card message could not be disabled', exc_info=True)


def _send_kwargs(result: team_show_workers.TeamShowResult, *, requester_id: int):
    view = TeamShowView(
        result,
        requester_id=requester_id,
    )
    files = render_files(result)
    kwargs = {
        'content': render_content(result),
        'embed': render_embed(result),
        'view': view,
    }
    if files:
        kwargs['files'] = files
    return kwargs, view


async def publish_prefix(ctx, result: team_show_workers.TeamShowResult):
    kwargs, view = _send_kwargs(result, requester_id=ctx.author.id)
    message = await ctx.send(**kwargs)
    view.message = message
    return message


async def publish_native(
    interaction: discord.Interaction,
    result: team_show_workers.TeamShowResult,
):
    kwargs, view = _send_kwargs(result, requester_id=interaction.user.id)
    channel = await interaction_lifecycle.resolve_public_interaction_channel(
        interaction
    )
    delete_original = getattr(interaction, 'delete_original_response', None)
    if callable(delete_original):
        try:
            await delete_original()
        except Exception:
            logger.debug('Private team-show acknowledgement could not be deleted', exc_info=True)
    message = await channel.send(**kwargs)
    view.message = message
    return message


def legacy_lookup_message(
    team_lookup: str,
    *,
    prefix: str,
) -> str:
    return (
        f'Couldn\'t find a team name matching *'
        f'{discord.utils.escape_mentions(str(team_lookup))}*. '
        f'Check spelling or be more specific. **Example:** '
        f'{prefix}team Ronin'
    )


def legacy_no_team_message(*, prefix: str) -> str:
    return (
        f'No team name supplied. Use `{prefix}lbteam` for the team leaderboard. '
        f'**Example:** `{prefix}team Ronin`'
    )
