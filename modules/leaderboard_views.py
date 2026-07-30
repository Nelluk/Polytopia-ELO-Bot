"""Discord component rendering for immutable leaderboard snapshots."""

from __future__ import annotations

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


class PlayerLeaderboardView(discord.ui.View):
    """Requester-controlled public pagination for one immutable result."""

    def __init__(
        self,
        result: leaderboard_workers.PlayerLeaderboardResult,
        requester_id: int,
        *,
        timeout: float = 120.0,
    ):
        super().__init__(timeout=timeout)
        self.result = result
        self.requester_id = requester_id
        self.page_index = 0
        self.message: discord.Message | None = None
        self._update_buttons()

    @property
    def page_count(self) -> int:
        return leaderboard_workers.player_leaderboard_page(
            self.result,
            self.page_index,
        ).page_count

    def _update_buttons(self) -> None:
        last_page = self.page_count - 1
        self.first_page.disabled = self.page_index == 0
        self.previous_page.disabled = self.page_index == 0
        self.next_page.disabled = self.page_index == last_page
        self.last_page.disabled = self.page_index == last_page
        self.page_indicator.label = (
            f'{self.page_index + 1}/{self.page_count}'
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
            embed=player_leaderboard_embed(
                self.result,
                self.page_index,
            ),
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
        label='1/1',
        style=discord.ButtonStyle.secondary,
        disabled=True,
    )
    async def page_indicator(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        return

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
