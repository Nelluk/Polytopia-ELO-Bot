"""Shared House reads and public native list/detail workspace."""

from __future__ import annotations

import logging
import math
import time

import discord

from modules import exceptions, house_show_workers
import settings


logger = logging.getLogger('polybot.' + __name__)

HOUSE_PAGE_SIZE = 5
HOUSE_CONTROL_TIMEOUT = 300.0


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


def _league_scope(guild_id: int) -> bool:
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
    private = _setting(guild_id, 'bot_channels_private', ()) or ()
    try:
        allowed = {int(value) for value in (*bot_channels, *private)}
    except (TypeError, ValueError):
        return False
    return channel_id is not None and int(channel_id) in allowed


def native_access_error(member, guild_id: int, channel_id: int | None) -> str | None:
    if not _league_scope(guild_id):
        return 'House commands are available only in the configured league server.'
    if _channel_allowed(member, guild_id, channel_id):
        return None
    channels = _setting(guild_id, 'bot_channels', ()) or ()
    return (
        'This command can only be used in a designated ELO bot channel. Try: '
        + ' '.join(f'<#{int(value)}>' for value in channels)
    )


def capture_guild_snapshot(guild) -> house_show_workers.HouseGuildSnapshot:
    role_names = tuple(str(role.name) for role in tuple(guild.roles or ()))
    members = []
    for member in tuple(guild.members or ()):
        members.append(
            house_show_workers.HouseMemberSnapshot(
                discord_id=int(member.id),
                display_name=str(
                    getattr(member, 'display_name', None)
                    or getattr(member, 'name', None)
                    or f'user-{int(member.id)}'
                ),
                role_names=tuple(
                    str(role.name) for role in tuple(getattr(member, 'roles', ()) or ())
                ),
            )
        )
    return house_show_workers.HouseGuildSnapshot(
        guild_id=int(guild.id),
        members=tuple(members),
        role_names=role_names,
    )


def build_request(
    *,
    member,
    guild,
    house_lookup: str | None,
    require_selection: bool,
    channel_id: int | None,
) -> house_show_workers.HouseShowRequest:
    guild_id = int(guild.id)
    return house_show_workers.HouseShowRequest(
        guild_id=guild_id,
        requester_id=int(member.id),
        house_lookup=(str(house_lookup) if house_lookup is not None else None),
        require_selection=bool(require_selection),
        league_scope=_league_scope(guild_id),
        channel_allowed=_channel_allowed(member, guild_id, channel_id),
        inactive_role_name=(
            str(inactive.name)
            if (inactive := settings.resolve_configured_role(
                guild,
                'inactive_role',
            )) is not None
            else None
        ),
        guild_snapshot=capture_guild_snapshot(guild),
    )


def _escape(value: object) -> str:
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value or ''))
    )


def selected_house(result: house_show_workers.HouseShowResult):
    return next(
        (
            house for house in result.houses
            if house.house_id == result.selected_house_id
        ),
        None,
    )


def _team_value(team: house_show_workers.HouseTeamRow) -> str:
    tier = f'{_escape(team.tier_name)} Tier' if team.tier_name else 'No tier'
    state = 'Archived' if team.archived else 'Active'
    roster = ', '.join(
        (
            f'{_escape(row.display_name)} `{row.elo}`'
            if row.elo is not None
            else _escape(row.display_name)
        )
        for row in team.roster
    ) or '*No active role members*'
    if team.roster_truncated:
        roster += ', …'
    if not team.role_found:
        roster = f':warning: Missing exact Discord role.\n{roster}'
    value = f'{state} · {tier} · `{team.elo} ELO`\n{roster}'
    return value[:500]


def render_house_embed(
    result: house_show_workers.HouseShowResult,
    house_id: int,
) -> discord.Embed:
    house = next(house for house in result.houses if house.house_id == house_id)
    emoji = f'{house.emoji} ' if house.emoji else ''
    embed = discord.Embed(
        title=f'{emoji}House {_escape(house.name)}',
        description=(
            f'`{house.league_tokens}` league tokens · '
            f'{sum(not team.archived for team in house.teams)} active team(s)'
        ),
    )
    leadership = (
        ('Leaders', house.leaders),
        ('Co-Leaders', house.coleaders),
        ('Recruiters', house.recruiters),
    )
    for label, names in leadership:
        if names:
            embed.add_field(
                name=label,
                value=', '.join(_escape(name) for name in names)[:500],
                inline=False,
            )
    if not house.teams:
        embed.add_field(name='Teams', value='*No related teams*', inline=False)
    else:
        for team in house.teams[:8]:
            team_emoji = f'{team.emoji} ' if team.emoji else ''
            embed.add_field(
                name=f'{team_emoji}{_escape(team.name)}',
                value=_team_value(team),
                inline=False,
            )
        if len(house.teams) > 8:
            embed.add_field(
                name='Additional teams',
                value=f'{len(house.teams) - 8} team(s) omitted from this card.',
                inline=False,
            )
    if house.image_url and house.image_url.startswith(('https://', 'http://')):
        embed.set_thumbnail(url=house.image_url)
    warnings = []
    if not house.role_found:
        warnings.append(f'No exact Discord role named {house.name!r}.')
    if result.houses_truncated or result.teams_truncated:
        warnings.append('The bounded House directory was truncated.')
    if warnings:
        embed.set_footer(text=' '.join(warnings)[:2048])
    return embed


def render_list_embed(
    result: house_show_workers.HouseShowResult,
    page: int,
) -> discord.Embed:
    total_pages = max(1, math.ceil(len(result.houses) / HOUSE_PAGE_SIZE))
    page = min(max(0, int(page)), total_pages - 1)
    start = page * HOUSE_PAGE_SIZE
    visible = result.houses[start:start + HOUSE_PAGE_SIZE]
    embed = discord.Embed(
        title='PolyChampions Houses',
        description='Select a House below for its leadership, teams, ELO, and roster.',
    )
    for house in visible:
        active = tuple(team for team in house.teams if not team.archived)
        leader_text = ', '.join(_escape(name) for name in house.leaders) or 'None listed'
        team_text = ', '.join(
            f'{team.emoji} {_escape(team.name)}'.strip() for team in active
        ) or 'No active teams'
        embed.add_field(
            name=f'{house.emoji} {_escape(house.name)}'.strip(),
            value=(
                f'Leader: {leader_text}\n'
                f'{team_text}\n'
                f'`{house.league_tokens}` tokens'
            )[:1024],
            inline=False,
        )
    footer = f'Page {page + 1}/{total_pages} · {len(result.houses)} Houses'
    if result.houses_truncated or result.teams_truncated:
        footer += ' · bounded result truncated'
    embed.set_footer(text=footer)
    return embed


class HouseWorkspace(discord.ui.View):
    """Requester-bound list/detail navigation over one immutable snapshot."""

    def __init__(
        self,
        result: house_show_workers.HouseShowResult,
        *,
        requester_id: int,
        detail_house_id: int | None = None,
        timeout: float = HOUSE_CONTROL_TIMEOUT,
    ):
        super().__init__(timeout=timeout)
        self.result = result
        self.requester_id = int(requester_id)
        self.detail_house_id = detail_house_id
        self.page = 0
        if detail_house_id is not None:
            for index, house in enumerate(result.houses):
                if house.house_id == detail_house_id:
                    self.page = index // HOUSE_PAGE_SIZE
                    break
        self.expires_at = time.monotonic() + float(timeout)
        self.message = None
        self._expired = False
        self._rebuild()

    def _page_houses(self):
        start = self.page * HOUSE_PAGE_SIZE
        return self.result.houses[start:start + HOUSE_PAGE_SIZE]

    def _rebuild(self) -> None:
        self.clear_items()
        options = [
            discord.SelectOption(
                label=house.name[:100],
                value=str(house.house_id),
                emoji=(house.emoji or None),
            )
            for house in self._page_houses()
        ]
        if options:
            selector = discord.ui.Select(
                placeholder='Show a House from this page',
                options=options,
                row=0,
            )
            selector.callback = self._house_selected
            self.add_item(selector)

        if self.detail_house_id is not None:
            back = discord.ui.Button(
                label='Back to list',
                style=discord.ButtonStyle.secondary,
                row=1,
            )
            back.callback = self._back_to_list
            self.add_item(back)

        total_pages = max(1, math.ceil(len(self.result.houses) / HOUSE_PAGE_SIZE))
        previous = discord.ui.Button(
            label='Previous',
            style=discord.ButtonStyle.secondary,
            disabled=self.page <= 0,
            row=1,
        )
        next_button = discord.ui.Button(
            label='Next',
            style=discord.ButtonStyle.secondary,
            disabled=self.page >= total_pages - 1,
            row=1,
        )
        previous.callback = self._previous
        next_button.callback = self._next
        self.add_item(previous)
        self.add_item(next_button)

    def embed(self) -> discord.Embed:
        if self.detail_house_id is not None:
            return render_house_embed(self.result, self.detail_house_id)
        return render_list_embed(self.result, self.page)

    async def _send_private(self, interaction, content: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self._expired or self.is_finished() or time.monotonic() >= self.expires_at:
            self._expired = True
            await self._send_private(
                interaction,
                'This House workspace expired. Run `/house show` or `/house list` again.',
            )
            return False
        if int(interaction.user.id) != self.requester_id:
            await self._send_private(
                interaction,
                'Only the member who opened this House workspace can use its controls.',
            )
            return False
        return True

    async def _refresh(self, interaction) -> None:
        self._rebuild()
        try:
            await interaction.response.edit_message(embed=self.embed(), view=self)
        except Exception:
            logger.exception('Could not refresh House workspace')
            await self._send_private(
                interaction,
                'The House workspace could not be refreshed. Run the command again.',
            )

    async def _house_selected(self, interaction) -> None:
        if not await self.interaction_check(interaction):
            return
        selector = next(item for item in self.children if isinstance(item, discord.ui.Select))
        self.detail_house_id = int(selector.values[0])
        await self._refresh(interaction)

    async def _back_to_list(self, interaction) -> None:
        if not await self.interaction_check(interaction):
            return
        self.detail_house_id = None
        await self._refresh(interaction)

    async def _previous(self, interaction) -> None:
        if not await self.interaction_check(interaction):
            return
        self.page = max(0, self.page - 1)
        self.detail_house_id = None
        await self._refresh(interaction)

    async def _next(self, interaction) -> None:
        if not await self.interaction_check(interaction):
            return
        total_pages = max(1, math.ceil(len(self.result.houses) / HOUSE_PAGE_SIZE))
        self.page = min(total_pages - 1, self.page + 1)
        self.detail_house_id = None
        await self._refresh(interaction)

    async def on_timeout(self) -> None:
        self._expired = True
        for item in self.children:
            item.disabled = True
        self.stop()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except Exception:
                logger.debug('Expired House workspace could not be disabled', exc_info=True)


async def publish_native(interaction, result, *, detail_house_id: int | None):
    view = HouseWorkspace(
        result,
        requester_id=interaction.user.id,
        detail_house_id=detail_house_id,
    )
    try:
        await interaction.delete_original_response()
    except Exception:
        logger.debug('Private House acknowledgement could not be deleted', exc_info=True)
    channel = getattr(interaction, 'channel', None)
    sender = getattr(channel, 'send', None)
    if not callable(sender):
        raise house_show_workers.HouseShowPublicationError(
            'The public House workspace has no available channel destination.'
        )
    try:
        message = await sender(embed=view.embed(), view=view)
    except Exception as exc:
        raise house_show_workers.HouseShowPublicationError(
            'The public House workspace could not be published. Run the command again.'
        ) from exc
    view.message = message
    return message


def render_prefix_house(result: house_show_workers.HouseShowResult) -> str:
    house = selected_house(result)
    if house is None:
        raise house_show_workers.HouseShowLookupError('No House was selected.')
    lines = [
        f'{house.emoji} **House {_escape(house.name)}** {house.emoji}'.strip(),
        f'**Leaders**: {", ".join(_escape(name) for name in house.leaders)}',
        f'**Co-Leaders**: {", ".join(_escape(name) for name in house.coleaders)}',
        f'**Recruiters**: {", ".join(_escape(name) for name in house.recruiters)}',
    ]
    for team in house.teams:
        tier = f'{team.tier_name} Tier' if team.tier_name else 'No tier'
        state = 'Archived ' if team.archived else ''
        lines.append(
            f'\n__{state}{tier} Team__ {_escape(team.name)} {team.emoji} '
            f'`{team.elo} ELO`'
        )
        lines.extend(
            f'{_escape(row.display_name)}'
            + (f' `{row.elo}`' if row.elo is not None else '')
            for row in team.roster
        )
    return '\n'.join(lines)


def render_prefix_list(result: house_show_workers.HouseShowResult) -> str:
    lines = ['**PolyChampions Houses**']
    for house in result.houses:
        leaders = ', '.join(_escape(name) for name in house.leaders)
        if leaders:
            lines.append(f'\n**House {_escape(house.name)}**\n**House Leader:** {leaders}')
        else:
            lines.append(f'\n**House {_escape(house.name)}**')
        for team in house.teams:
            tier = f'{team.tier_name} Tier' if team.tier_name else 'No tier'
            lines.append(
                f'- {_escape(team.name)} {team.emoji} - {tier} - ELO: {team.elo}'
            )
        if not house.teams:
            lines.append('*No related Teams*')
    return '\n'.join(lines)
