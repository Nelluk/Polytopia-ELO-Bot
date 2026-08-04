"""Requester-controlled Components v2 team leaderboard workspace."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from io import BytesIO

import discord

from modules import components_v2, team_leaderboard_workers


ACCENT_COLOUR = components_v2.DEFAULT_ACCENT
PAGE_SIZE = team_leaderboard_workers.TEAM_LEADERBOARD_PAGE_SIZE


def _response_is_done(interaction: discord.Interaction) -> bool:
    value = getattr(interaction.response, 'is_done', False)
    return bool(value() if callable(value) else value)


class TeamLeaderboardPageJumpModal(discord.ui.Modal):
    """Requester-bound page jump that also refreshes the bounded graph."""

    def __init__(self, view: 'TeamLeaderboardWorkspace'):
        super().__init__(title='Jump to page', timeout=60.0)
        self.target_view = view
        self.page_number = discord.ui.TextInput(
            placeholder=str(view.page_index + 1),
            default=str(view.page_index + 1),
            min_length=1,
            max_length=max(1, len(str(view.page_count))),
        )
        self.add_item(discord.ui.Label(
            text=f'Page number (1–{view.page_count})',
            description='The public result will move to this page.',
            component=self.page_number,
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        view = self.target_view
        if not await view.authorize(interaction):
            return
        if view.is_finished():
            await interaction.response.send_message(
                view.expired_message,
                ephemeral=True,
            )
            return
        try:
            page = int(self.page_number.value.strip())
        except (AttributeError, TypeError, ValueError):
            page = 0
        if page < 1 or page > view.page_count:
            await interaction.response.send_message(
                f'Enter a page number from 1 to {view.page_count}.',
                ephemeral=True,
            )
            return
        await view._move_page(interaction, page - 1)


class TeamLeaderboardWorkspace(components_v2.RequesterLayoutView):
    """Public team rankings whose controls belong to the invoking user."""

    def __init__(
        self,
        *,
        requester_id: int,
        result: team_leaderboard_workers.TeamLeaderboardResult,
        tier_choices: tuple[tuple[int, str], ...],
        graph: team_leaderboard_workers.TeamLeaderboardGraph,
        graph_loader: Callable[
            [
                team_leaderboard_workers.TeamLeaderboardPage,
                str,
            ],
            Awaitable[team_leaderboard_workers.TeamLeaderboardGraph],
        ] = team_leaderboard_workers.run_team_leaderboard_graph,
        timeout: float = 300.0,
    ):
        super().__init__(requester_id=requester_id, timeout=timeout)
        self.result = result
        self.tier_choices = tuple(tier_choices)
        self.graph = graph
        self.graph_loader = graph_loader
        self.tier_number: int | None = None
        self.include_archived = False
        self.rebuild()

    @property
    def page_count(self) -> int:
        return team_leaderboard_workers.team_leaderboard_page(
            self.result,
            tier_number=self.tier_number,
            include_archived=self.include_archived,
            page_index=0,
        ).page_count

    @property
    def current_page(self) -> team_leaderboard_workers.TeamLeaderboardPage:
        return team_leaderboard_workers.team_leaderboard_page(
            self.result,
            tier_number=self.tier_number,
            include_archived=self.include_archived,
            page_index=self.page_index,
        )

    @property
    def filter_value(self) -> str:
        population = 'archived' if self.include_archived else 'active'
        tier = 'all' if self.tier_number is None else str(self.tier_number)
        return f'{population}:{tier}'

    def graph_files(self) -> list[discord.File]:
        if not self.graph.png_bytes:
            return []
        return [
            discord.File(
                BytesIO(self.graph.png_bytes),
                filename=self.graph.filename,
            )
        ]

    def _filter_options(self) -> list[discord.SelectOption]:
        options = [
            discord.SelectOption(
                label='Active · All tiers',
                value='active:all',
                description='Current teams in every configured tier.',
            ),
        ]
        options.extend(
            discord.SelectOption(
                label=f'Active · {name}',
                value=f'active:{number}',
                description=f'Current teams in the {name} tier.',
            )
            for number, name in self.tier_choices
        )
        options.append(discord.SelectOption(
            label='Include archived · All tiers',
            value='archived:all',
            description='Current and archived teams in every tier.',
        ))
        options.extend(
            discord.SelectOption(
                label=f'Include archived · {name}',
                value=f'archived:{number}',
                description=f'Current and archived {name} teams.',
            )
            for number, name in self.tier_choices
        )
        for option in options:
            option.default = option.value == self.filter_value
        return options[:25]

    @staticmethod
    def _parse_filter(value: str) -> tuple[int | None, bool] | None:
        try:
            population, tier = value.split(':', 1)
        except (AttributeError, ValueError):
            return None
        if population not in {'active', 'archived'}:
            return None
        if tier == 'all':
            return None, population == 'archived'
        try:
            tier_number = int(tier)
        except (TypeError, ValueError):
            return None
        if tier_number <= 0:
            return None
        return tier_number, population == 'archived'

    async def _private_error(
        self,
        interaction: discord.Interaction,
        message: str,
    ) -> None:
        if _response_is_done(interaction):
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def _render_interaction(
        self,
        interaction: discord.Interaction,
        *,
        page_index: int,
    ) -> bool:
        if not _response_is_done(interaction):
            await interaction.response.defer()
        try:
            page = team_leaderboard_workers.team_leaderboard_page(
                self.result,
                tier_number=self.tier_number,
                include_archived=self.include_archived,
                page_index=page_index,
            )
            graph = await self.graph_loader(
                page,
                self.result.graph_attachment_name,
            )
        except Exception as exc:
            await self._private_error(
                interaction,
                f'Could not update the team leaderboard: {exc}',
            )
            return False

        self.page_index = page_index
        self.graph = graph
        self.rebuild()
        await interaction.edit_original_response(
            view=self,
            attachments=self.graph_files(),
        )
        return True

    async def _apply_filter(
        self,
        interaction: discord.Interaction,
        value: str,
    ) -> None:
        parsed = self._parse_filter(value)
        if parsed is None:
            await self._private_error(
                interaction,
                'Choose one of the displayed team leaderboard filters.',
            )
            return
        if (
            parsed[0] is not None
            and parsed[0] not in {number for number, _name in self.tier_choices}
        ):
            await self._private_error(
                interaction,
                'Choose one of the displayed team leaderboard filters.',
            )
            return
        if not await self.authorize(interaction):
            return
        if self.is_finished():
            await self._private_error(interaction, self.expired_message)
            return

        previous = (self.tier_number, self.include_archived, self.page_index)
        self.tier_number, self.include_archived = parsed
        if await self._render_interaction(interaction, page_index=0):
            return
        self.tier_number, self.include_archived, self.page_index = previous
        self.rebuild()

    async def _select_filter(self, interaction: discord.Interaction) -> None:
        await self._apply_filter(interaction, self.filter_select.values[0])

    async def _reset_filters(self, interaction: discord.Interaction) -> None:
        await self._apply_filter(interaction, 'active:all')

    async def _move_page(
        self,
        interaction: discord.Interaction,
        page_index: int,
    ) -> None:
        if not await self.authorize(interaction):
            return
        if self.is_finished():
            await self._private_error(interaction, self.expired_message)
            return
        if page_index < 0 or page_index >= self.page_count:
            await self._private_error(
                interaction,
                f'Enter a page number from 1 to {self.page_count}.',
            )
            return
        previous = self.page_index
        if await self._render_interaction(interaction, page_index=page_index):
            return
        self.page_index = previous
        self.rebuild()

    async def _previous_page(self, interaction: discord.Interaction) -> None:
        await self._move_page(interaction, max(0, self.page_index - 1))

    async def _next_page(self, interaction: discord.Interaction) -> None:
        await self._move_page(
            interaction,
            min(self.page_count - 1, self.page_index + 1),
        )

    async def _open_page_modal(self, interaction: discord.Interaction) -> None:
        if not await self.authorize(interaction):
            return
        if self.is_finished():
            await self._private_error(interaction, self.expired_message)
            return
        await interaction.response.send_modal(
            TeamLeaderboardPageJumpModal(self),
        )

    def rebuild(self) -> None:
        self.clear_items()
        self.page_index = min(self.page_index, self.page_count - 1)
        page = self.current_page
        population = (
            'Include archived'
            if self.include_archived
            else 'Active'
        )
        tier = (
            'All configured tiers'
            if self.tier_number is None
            else next(
                (
                    name for number, name in self.tier_choices
                    if number == self.tier_number
                ),
                f'Tier {self.tier_number}',
            )
        )
        lines = []
        for row in page.rows:
            lines.append(
                f'`{row.rank:>3}.` {row.team_emoji} **{row.team_name}** '
                f'({row.member_count})\n'
                f'> `{row.elo} ELO` · **{row.wins}W–{row.losses}L**'
            )
        rankings = '\n\n'.join(lines) or '*No ranked teams match this view.*'

        self.filter_select = discord.ui.Select(
            placeholder='Common filters',
            options=self._filter_options(),
        )
        self.filter_select.callback = self._select_filter
        reset = discord.ui.Button(
            label='Reset filters',
            style=discord.ButtonStyle.secondary,
            disabled=self.filter_value == 'active:all',
        )
        reset.callback = self._reset_filters
        previous = discord.ui.Button(
            label='Previous',
            emoji='◀️',
            disabled=self.page_index == 0,
        )
        previous.callback = self._previous_page
        page_button = discord.ui.Button(
            label=f'Jump to page · {self.page_index + 1}/{self.page_count}',
            style=discord.ButtonStyle.primary,
        )
        page_button.callback = self._open_page_modal
        next_page = discord.ui.Button(
            label='Next',
            emoji='▶️',
            disabled=self.page_index == self.page_count - 1,
        )
        next_page.callback = self._next_page
        self.previous_button = previous
        self.page_button = page_button
        self.next_button = next_page

        children = [
            discord.ui.TextDisplay(
                '# 🏆 Team Leaderboard\n'
                f'**Rating:** Current ELO · **Population:** {population}\n'
                f'**Tier:** {tier}\n'
                f'-# {page.total_teams} matching teams · '
                f'current-reset W–L'
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(rankings),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(
                f'-# Page {page.page_index + 1} of {page.page_count} · '
                f'showing {page.start_rank or 0}–{page.end_rank or 0} of '
                f'{page.total_teams}'
            ),
            discord.ui.TextDisplay('**Common filters**'),
            discord.ui.ActionRow(self.filter_select),
            discord.ui.ActionRow(reset, previous, page_button, next_page),
        ]
        if self.graph.png_bytes:
            children.extend([
                discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(
                        f'attachment://{self.graph.filename}',
                        description='Current-page team ELO history',
                    ),
                ),
            ])
        self.add_item(discord.ui.Container(
            *children,
            accent_colour=ACCENT_COLOUR,
        ))
