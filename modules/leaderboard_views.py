"""Discord component rendering for immutable leaderboard snapshots."""

from __future__ import annotations

from collections.abc import Callable

import discord

from modules import leaderboard_workers


def player_leaderboard_embed(
    result: leaderboard_workers.PlayerLeaderboardResult,
    page_index: int,
) -> discord.Embed:
    """Render one player leaderboard page without database access."""

    page = leaderboard_workers.player_leaderboard_page(result, page_index)
    embed = discord.Embed(
        title=(
            f'**{page.title}**\n'
            f'{page.total_ranked} ranked players'
        )
    )
    for row in page.rows:
        embed.add_field(
            name=f'{row.rank:>3}. {row.team_emoji}{row.name}'[:256],
            value=(
                f'`ELO {row.elo}\u00a0\u00a0\u00a0\u00a0'
                f'W {row.wins} / L {row.losses}`'
            ),
            inline=False,
        )
    if not page.rows:
        embed.description = 'No ranked players found.'
    embed.set_footer(
        text=(
            f'Page {page.page_index + 1} of {page.page_count}'
            f' • showing {page.start_rank or 0}-{page.end_rank or 0}'
            f' of {page.loaded_count}'
        )
    )
    return embed


def activity_leaderboard_embed(
    result: leaderboard_workers.ActivityLeaderboardResult,
    page_index: int,
) -> discord.Embed:
    """Render one immutable activity page without database access."""

    rows, page_count, start, end = (
        leaderboard_workers.leaderboard_page_rows(
            result.rows,
            page_index,
            leaderboard_workers.DEFAULT_PAGE_SIZE,
        )
    )
    embed = discord.Embed(
        title=f'**{result.title}**\n{result.total_players} players',
    )
    for row in rows:
        count_label = (
            'Games Played'
            if result.view == 'global-all-time'
            else 'Recent Games'
        )
        embed.add_field(
            name=f'{row.rank:>3}. {row.team_emoji}{row.name}'[:256],
            value=(
                f'`ELO {row.elo}\u00a0\u00a0\u00a0\u00a0'
                f'{count_label} {row.games}`'
            ),
            inline=False,
        )
    if not rows:
        embed.description = 'No activity found.'
    embed.set_footer(
        text=(
            f'Page {page_index + 1} of {page_count}'
            f' • showing {(start + 1) if rows else 0}-{end}'
            f' of {len(result.rows)}'
        )
    )
    return embed


def squad_leaderboard_embed(
    result: leaderboard_workers.SquadLeaderboardResult,
    page_index: int,
) -> discord.Embed:
    """Render one immutable squad page without database access."""

    rows, page_count, start, end = (
        leaderboard_workers.leaderboard_page_rows(
            result.rows,
            page_index,
            leaderboard_workers.DEFAULT_PAGE_SIZE,
        )
    )
    embed = discord.Embed(
        title=f'**{result.title}**\n{result.total_squads} ranked squads',
    )
    for row in rows:
        squad_name = f'{row.squad_name}\n' if row.squad_name else ''
        emojis = ' '.join(row.member_emojis)
        member_names = ' / '.join(row.member_names)
        name = (
            f'{row.rank:>3}. {squad_name}'
            f'{emojis}{member_names}'
        )
        embed.add_field(
            name=name[:256],
            value=(
                f'`#{row.squad_id} (ELO: {row.elo:4}) '
                f'W {row.wins} / L {row.losses}`'
            ),
            inline=False,
        )
    if not rows:
        embed.description = 'No ranked squads found.'
    embed.set_footer(
        text=(
            f'Page {page_index + 1} of {page_count}'
            f' • showing {(start + 1) if rows else 0}-{end}'
            f' of {len(result.rows)}'
        )
    )
    return embed


class JumpToPageModal(discord.ui.Modal):
    """Validate a page number before updating a public leaderboard."""

    def __init__(self, leaderboard_view: '_LeaderboardView'):
        super().__init__(
            title='Jump to leaderboard page',
            timeout=60.0,
        )
        self.leaderboard_view = leaderboard_view
        self.page_number = discord.ui.TextInput(
            placeholder=str(leaderboard_view.page_index + 1),
            default=str(leaderboard_view.page_index + 1),
            min_length=1,
            max_length=max(1, len(str(leaderboard_view.page_count))),
        )
        self.page_label = discord.ui.Label(
            text=f'Page number (1-{leaderboard_view.page_count})',
            description='Enter the page to display publicly.',
            component=self.page_number,
        )
        self.add_item(self.page_label)

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ) -> None:
        view = self.leaderboard_view
        if interaction.user.id != view.requester_id:
            await interaction.response.send_message(
                'Only the requester can change this leaderboard page.',
                ephemeral=True,
            )
            return
        if view.is_finished():
            await interaction.response.send_message(
                'This leaderboard paginator has expired. Run the command '
                'again for a fresh result.',
                ephemeral=True,
            )
            return

        value = self.page_number.value.strip()
        try:
            requested_page = int(value)
        except ValueError:
            requested_page = 0
        if requested_page < 1 or requested_page > view.page_count:
            await interaction.response.send_message(
                f'Enter a page number from 1 to {view.page_count}.',
                ephemeral=True,
            )
            return

        view.page_index = requested_page - 1
        await view._show_page(interaction)


class _LeaderboardView(discord.ui.View):
    """Shared requester-controlled buttons for a public leaderboard."""

    def __init__(
        self,
        requester_id: int,
        page_count: int,
        render_page: Callable[[int], discord.Embed],
        *,
        timeout: float = 120.0,
    ):
        super().__init__(timeout=timeout)
        self.requester_id = requester_id
        self.page_index = 0
        self.page_count = page_count
        self.render_page = render_page
        self.message: discord.Message | None = None
        self._update_buttons()

    def _update_buttons(self) -> None:
        last_page = self.page_count - 1
        self.first_page.disabled = self.page_index == 0
        self.previous_page.disabled = self.page_index == 0
        self.next_page.disabled = self.page_index == last_page
        self.last_page.disabled = self.page_index == last_page
        self.page_indicator.label = (
            f'Page {self.page_index + 1}/{self.page_count}'
        )

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            'Only the requester can change this leaderboard page.',
            ephemeral=True,
        )
        return False

    async def _show_page(self, interaction: discord.Interaction) -> None:
        self._update_buttons()
        await interaction.response.edit_message(
            embed=self.render_page(self.page_index),
            view=self,
        )

    @discord.ui.button(
        label='First',
        style=discord.ButtonStyle.secondary,
    )
    async def first_page(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        self.page_index = 0
        await self._show_page(interaction)

    @discord.ui.button(
        label='Previous',
        style=discord.ButtonStyle.primary,
    )
    async def previous_page(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        self.page_index -= 1
        await self._show_page(interaction)

    @discord.ui.button(
        label='Page 1/1',
        style=discord.ButtonStyle.secondary,
    )
    async def page_indicator(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(
            JumpToPageModal(self),
        )

    @discord.ui.button(
        label='Next',
        style=discord.ButtonStyle.primary,
    )
    async def next_page(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        self.page_index += 1
        await self._show_page(interaction)

    @discord.ui.button(
        label='Last',
        style=discord.ButtonStyle.secondary,
    )
    async def last_page(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        self.page_index = self.page_count - 1
        await self._show_page(interaction)

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class PlayerLeaderboardView(_LeaderboardView):
    """Public component pagination for a player ELO leaderboard."""

    def __init__(
        self,
        result: leaderboard_workers.PlayerLeaderboardResult,
        requester_id: int,
        *,
        timeout: float = 120.0,
    ):
        self.result = result
        page_count = leaderboard_workers.player_leaderboard_page(
            result,
            0,
        ).page_count
        super().__init__(
            requester_id,
            page_count,
            lambda page_index: player_leaderboard_embed(
                result,
                page_index,
            ),
            timeout=timeout,
        )


class ActivityLeaderboardView(_LeaderboardView):
    """Public component pagination for player activity."""

    def __init__(
        self,
        result: leaderboard_workers.ActivityLeaderboardResult,
        requester_id: int,
        *,
        timeout: float = 120.0,
    ):
        self.result = result
        _, page_count, _, _ = (
            leaderboard_workers.leaderboard_page_rows(
                result.rows,
                0,
                leaderboard_workers.DEFAULT_PAGE_SIZE,
            )
        )
        super().__init__(
            requester_id,
            page_count,
            lambda page_index: activity_leaderboard_embed(
                result,
                page_index,
            ),
            timeout=timeout,
        )


class SquadLeaderboardView(_LeaderboardView):
    """Public component pagination for squad rankings."""

    def __init__(
        self,
        result: leaderboard_workers.SquadLeaderboardResult,
        requester_id: int,
        *,
        timeout: float = 120.0,
    ):
        self.result = result
        _, page_count, _, _ = (
            leaderboard_workers.leaderboard_page_rows(
                result.rows,
                0,
                leaderboard_workers.DEFAULT_PAGE_SIZE,
            )
        )
        super().__init__(
            requester_id,
            page_count,
            lambda page_index: squad_leaderboard_embed(
                result,
                page_index,
            ),
            timeout=timeout,
        )
