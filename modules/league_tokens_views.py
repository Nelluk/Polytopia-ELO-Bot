"""Requester-bound Components v2 workspace for league token balances."""

from __future__ import annotations

import math

import discord

from modules import components_v2, league_tokens_workers as workers


HOUSE_PAGE_SIZE = 10
LOG_PAGE_SIZE = 5


def _escape(value: object) -> str:
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value or ''))
    )


class LeagueTokensWorkspace(components_v2.RequesterLayoutView):
    """Public immutable token snapshot with requester-only navigation."""

    expired_message = (
        'This league-token workspace expired. Run `/league tokens` again.'
    )

    def __init__(
        self,
        *,
        result: workers.LeagueTokensReadResult,
        requester_id: int,
        banner: str | None = None,
        timeout: float = 300.0,
    ):
        super().__init__(requester_id=int(requester_id), timeout=timeout)
        self.result = result
        self.banner = str(banner) if banner else None
        self.selected_house_id = result.selected_house_id
        self.section = 'house' if self.selected_house_id is not None else 'houses'
        self.rebuild()

    @property
    def selected_house(self):
        return next(
            (
                row for row in self.result.houses
                if row.house_id == self.selected_house_id
            ),
            None,
        )

    @property
    def rows(self):
        if self.section == 'houses':
            return self.result.houses
        if self.section == 'recent':
            return self.result.logs
        return tuple(
            row for row in self.result.logs
            if row.house_id == self.selected_house_id
        )

    @property
    def page_size(self) -> int:
        return HOUSE_PAGE_SIZE if self.section == 'houses' else LOG_PAGE_SIZE

    @property
    def page_count(self) -> int:
        return max(1, math.ceil(len(self.rows) / self.page_size))

    def _page_rows(self):
        start = self.page_index * self.page_size
        return self.rows[start:start + self.page_size]

    async def _private(self, interaction, content: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)

    async def _ready(self, interaction) -> bool:
        if not await self.authorize(interaction):
            return False
        if self.is_finished():
            await self._private(interaction, self.expired_message)
            return False
        return True

    async def _refresh(self, interaction) -> None:
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _select_house(self, interaction) -> None:
        if not await self._ready(interaction):
            return
        try:
            selected = int(self.house_select.values[0])
        except (AttributeError, IndexError, TypeError, ValueError):
            await self._private(interaction, 'Choose a House from this page.')
            return
        if selected not in {row.house_id for row in self.result.houses}:
            await self._private(interaction, 'That House is not in this result.')
            return
        self.selected_house_id = selected
        self.section = 'house'
        self.page_index = 0
        await self._refresh(interaction)

    async def _show_houses(self, interaction) -> None:
        if not await self._ready(interaction):
            return
        self.selected_house_id = None
        self.section = 'houses'
        self.page_index = 0
        await self._refresh(interaction)

    async def _show_recent(self, interaction) -> None:
        if not await self._ready(interaction):
            return
        self.selected_house_id = None
        self.section = 'recent'
        self.page_index = 0
        await self._refresh(interaction)

    async def _previous(self, interaction) -> None:
        if not await self._ready(interaction):
            return
        self.page_index = max(0, self.page_index - 1)
        await self._refresh(interaction)

    async def _next(self, interaction) -> None:
        if not await self._ready(interaction):
            return
        self.page_index = min(self.page_count - 1, self.page_index + 1)
        await self._refresh(interaction)

    async def _jump(self, interaction) -> None:
        if not await self._ready(interaction):
            return
        await self.open_page_modal(interaction)

    def _body(self) -> str:
        rows = self._page_rows()
        if self.section == 'houses':
            return '\n'.join(
                f'{row.emoji} **{_escape(row.name)}** — `{row.balance}` tokens'.strip()
                for row in rows
            ) or '*No Houses are configured.*'

        return '\n\n'.join(
            f'`{_escape(row.timestamp)}`\n{_escape(row.message)[:700]}'
            for row in rows
        ) or '*No token changes are recorded for this view.*'

    def rebuild(self) -> None:
        self.clear_items()
        self.page_index = min(max(0, self.page_index), self.page_count - 1)
        title = '# 🪙 League tokens'
        if self.section == 'houses':
            subtitle = '**All House balances**'
        elif self.section == 'recent':
            subtitle = '**Recent token changes**'
        else:
            house = self.selected_house
            subtitle = (
                f'**{house.emoji} {_escape(house.name)} — `{house.balance}` tokens**'.strip()
                if house is not None
                else '**House token history**'
            )

        children = [discord.ui.TextDisplay(f'{title}\n{subtitle}')]
        if self.banner:
            children.extend([
                discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
                discord.ui.TextDisplay(self.banner),
            ])
        children.extend([
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(self._body()),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(
                f'-# Page {self.page_index + 1}/{self.page_count} · '
                f'{len(self.rows)} row(s) loaded'
                + (' · result truncated' if self.result.logs_truncated else '')
            ),
        ])

        if self.section == 'houses' and self._page_rows():
            self.house_select = discord.ui.Select(
                placeholder='Show one House token history',
                options=[
                    discord.SelectOption(
                        label=row.name[:100],
                        value=str(row.house_id),
                        emoji=(row.emoji or None),
                        description=f'{row.balance} league tokens',
                    )
                    for row in self._page_rows()
                ],
            )
            self.house_select.callback = self._select_house
            children.append(discord.ui.ActionRow(self.house_select))

        all_houses = discord.ui.Button(
            label='All balances',
            style=discord.ButtonStyle.secondary,
            disabled=self.section == 'houses',
        )
        all_houses.callback = self._show_houses
        recent = discord.ui.Button(
            label='Recent changes',
            style=discord.ButtonStyle.secondary,
            disabled=self.section == 'recent',
        )
        recent.callback = self._show_recent
        previous = discord.ui.Button(
            label='Previous',
            emoji='◀️',
            disabled=self.page_index == 0,
        )
        previous.callback = self._previous
        jump = discord.ui.Button(
            label=f'Page {self.page_index + 1}/{self.page_count}',
            style=discord.ButtonStyle.primary,
        )
        jump.callback = self._jump
        next_page = discord.ui.Button(
            label='Next',
            emoji='▶️',
            disabled=self.page_index == self.page_count - 1,
        )
        next_page.callback = self._next
        children.extend([
            discord.ui.ActionRow(all_houses, recent),
            discord.ui.ActionRow(previous, jump, next_page),
        ])
        self.add_item(discord.ui.Container(
            *children,
            accent_colour=components_v2.DEFAULT_ACCENT,
        ))


async def publish(interaction, view: LeagueTokensWorkspace):
    try:
        await interaction.delete_original_response()
    except Exception:
        pass
    channel = getattr(interaction, 'channel', None)
    sender = getattr(channel, 'send', None)
    if not callable(sender):
        raise workers.LeagueTokensPublicationError(
            'The public league-token workspace has no channel destination.'
        )
    try:
        message = await sender(view=view)
    except Exception as exc:
        raise workers.LeagueTokensPublicationError(
            'The public league-token workspace could not be published.'
        ) from exc
    view.message = message
    return message
