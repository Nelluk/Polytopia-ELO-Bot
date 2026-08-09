"""Requester-bound Components v2 pages for league season records."""

from __future__ import annotations

import discord

from modules import components_v2, league_season, league_season_workers as workers


class LeagueSeasonPublicationError(workers.LeagueSeasonError):
    """A loaded public season result could not be published."""


class LeagueSeasonWorkspace(components_v2.RequesterLayoutView):
    expired_message = (
        'This season-record workspace expired. Run `/league season` again.'
    )

    def __init__(self, *, result, requester_id: int, timeout: float = 300.0):
        super().__init__(requester_id=int(requester_id), timeout=timeout)
        self.result = result
        self.pages = league_season.native_pages(result)
        self.rebuild()

    @property
    def page_count(self) -> int:
        return max(1, len(self.pages))

    async def _previous(self, interaction):
        if not await self.authorize(interaction):
            return
        self.page_index = max(0, self.page_index - 1)
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _next(self, interaction):
        if not await self.authorize(interaction):
            return
        self.page_index = min(self.page_count - 1, self.page_index + 1)
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _jump(self, interaction):
        if not await self.authorize(interaction):
            return
        await self.open_page_modal(interaction)

    def rebuild(self) -> None:
        self.clear_items()
        self.page_index = min(max(0, self.page_index), self.page_count - 1)
        children = [
            discord.ui.TextDisplay(self.pages[self.page_index]),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(
                f'-# Page {self.page_index + 1}/{self.page_count} · '
                f'{sum(len(tier.teams) for tier in self.result.tiers)} team record(s)'
            ),
        ]
        if self.page_count > 1:
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
            children.append(discord.ui.ActionRow(previous, jump, next_page))
        self.add_item(discord.ui.Container(
            *children,
            accent_colour=components_v2.DEFAULT_ACCENT,
        ))


async def publish(interaction, view: LeagueSeasonWorkspace):
    try:
        await interaction.delete_original_response()
    except Exception:
        pass
    channel = getattr(interaction, 'channel', None)
    sender = getattr(channel, 'send', None)
    if not callable(sender):
        raise LeagueSeasonPublicationError(
            'The public season workspace has no channel destination.'
        )
    try:
        message = await sender(view=view)
    except Exception as exc:
        raise LeagueSeasonPublicationError(
            'The public season workspace could not be published.'
        ) from exc
    view.message = message
    return message
