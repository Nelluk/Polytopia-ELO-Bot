"""Experimental Components v2 player leaderboard interface."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import datetime
import math

import discord

from modules import leaderboard_workers


PAGE_SIZE = 8
ACCENT_COLOUR = discord.Colour.from_rgb(83, 126, 231)


class LeaderboardPreset:
    def __init__(
        self,
        key: str,
        label: str,
        description: str,
        *,
        scope: str,
        rating: str,
        era: str,
    ):
        self.key = key
        self.label = label
        self.description = description
        self.scope = scope
        self.rating = rating
        self.era = era


PRESETS = (
    LeaderboardPreset(
        'local-current',
        'This server · current ELO',
        'The standard local leaderboard',
        scope='local',
        rating='current',
        era='current',
    ),
    LeaderboardPreset(
        'global-current',
        'Global · current ELO',
        'Ratings combined across participating servers',
        scope='global',
        rating='current',
        era='current',
    ),
    LeaderboardPreset(
        'local-peak',
        'This server · peak ELO',
        'Highest current-era rating achieved',
        scope='local',
        rating='peak',
        era='current',
    ),
    LeaderboardPreset(
        'local-all-time',
        'This server · all-time ELO',
        'Permanent rating that is never reset',
        scope='local',
        rating='current',
        era='all-time',
    ),
    LeaderboardPreset(
        'global-all-time',
        'Global · all-time ELO',
        'Permanent cross-server rating',
        scope='global',
        rating='current',
        era='all-time',
    ),
)
PRESET_BY_KEY = {preset.key: preset for preset in PRESETS}


def _page_count(result: leaderboard_workers.PlayerLeaderboardResult) -> int:
    return max(1, math.ceil(len(result.rows) / PAGE_SIZE))


def _mode_summary(preset: LeaderboardPreset, population: str) -> str:
    population_label = (
        'recently active players'
        if population == 'active'
        else 'all registered players'
    )
    return f'{preset.label} · {population_label}'


def _rankings_text(
    result: leaderboard_workers.PlayerLeaderboardResult,
    page_index: int,
) -> tuple[str, int, int]:
    start = page_index * PAGE_SIZE
    rows = result.rows[start:start + PAGE_SIZE]
    medals = {1: '🥇', 2: '🥈', 3: '🥉'}
    lines = []
    for row in rows:
        marker = medals.get(row.rank, f'`{row.rank:>2}.`')
        team = f'{row.team_emoji} ' if row.team_emoji else ''
        lines.append(
            f'{marker} **{team}{row.name}**\n'
            f'> `{row.elo:>4} ELO`  ·  **{row.wins}W–{row.losses}L**'
        )
    if not lines:
        lines.append('*No ranked players match this view.*')
    return '\n'.join(lines), start + 1 if rows else 0, start + len(rows)


class Lb2JumpModal(discord.ui.Modal):
    """Jump to a page in an experimental Components v2 leaderboard."""

    def __init__(self, view: 'ExperimentalLeaderboardView'):
        super().__init__(title='Jump to leaderboard page', timeout=60.0)
        self.leaderboard_view = view
        self.page_number = discord.ui.TextInput(
            placeholder=str(view.page_index + 1),
            default=str(view.page_index + 1),
            min_length=1,
            max_length=max(1, len(str(view.page_count))),
        )
        self.add_item(discord.ui.Label(
            text=f'Page number (1–{view.page_count})',
            description='The public leaderboard will move to this page.',
            component=self.page_number,
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        view = self.leaderboard_view
        if interaction.user.id != view.requester_id:
            await interaction.response.send_message(
                'Only the requester can control this leaderboard.',
                ephemeral=True,
            )
            return
        try:
            page = int(self.page_number.value.strip())
        except ValueError:
            page = 0
        if page < 1 or page > view.page_count:
            await interaction.response.send_message(
                f'Enter a page number from 1 to {view.page_count}.',
                ephemeral=True,
            )
            return
        view.page_index = page - 1
        view.rebuild()
        await interaction.response.edit_message(view=view)


class ExperimentalLeaderboardView(discord.ui.LayoutView):
    """A requester-controlled, public Components v2 leaderboard."""

    def __init__(
        self,
        *,
        guild_id: int,
        requester_id: int,
        result: leaderboard_workers.PlayerLeaderboardResult,
        loader: Callable[
            [leaderboard_workers.PlayerLeaderboardRequest],
            Awaitable[leaderboard_workers.PlayerLeaderboardResult],
        ],
        active_cutoff: datetime.datetime,
        preset_key: str = 'local-current',
        population: str = 'active',
        timeout: float = 300.0,
    ):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.requester_id = requester_id
        self.loader = loader
        self.active_cutoff = active_cutoff
        self.preset_key = preset_key
        self.population = population
        self.result = result
        self.page_index = 0
        self.message: discord.Message | None = None
        self._cache = {(preset_key, population): result}
        self.rebuild()

    @property
    def page_count(self) -> int:
        return _page_count(self.result)

    def _request(self) -> leaderboard_workers.PlayerLeaderboardRequest:
        preset = PRESET_BY_KEY[self.preset_key]
        return leaderboard_workers.PlayerLeaderboardRequest(
            guild_id=self.guild_id,
            scope=preset.scope,
            rating=preset.rating,
            era=preset.era,
            population=self.population,
            active_cutoff=self.active_cutoff,
        )

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            'Only the requester can control this leaderboard.',
            ephemeral=True,
        )
        return False

    async def _load_selected_result(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        key = (self.preset_key, self.population)
        cached = self._cache.get(key)
        if cached is not None:
            self.result = cached
            return True
        await interaction.response.defer()
        try:
            self.result = await self.loader(self._request())
        except Exception as exc:
            await interaction.followup.send(
                f'Could not load that leaderboard view: {exc}',
                ephemeral=True,
            )
            return False
        self._cache[key] = self.result
        return True

    async def _edit_after_selection(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        self.page_index = 0
        if not await self._load_selected_result(interaction):
            return False
        self.rebuild()
        if interaction.response.is_done():
            await interaction.edit_original_response(view=self)
        else:
            await interaction.response.edit_message(view=self)
        return True

    async def _select_preset(
        self,
        interaction: discord.Interaction,
    ) -> None:
        previous = self.preset_key
        self.preset_key = self.preset_select.values[0]
        if not await self._edit_after_selection(interaction):
            self.preset_key = previous

    async def _toggle_population(
        self,
        interaction: discord.Interaction,
    ) -> None:
        previous = self.population
        self.population = 'all' if self.population == 'active' else 'active'
        if not await self._edit_after_selection(interaction):
            self.population = previous

    async def _previous_page(
        self,
        interaction: discord.Interaction,
    ) -> None:
        self.page_index -= 1
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _next_page(
        self,
        interaction: discord.Interaction,
    ) -> None:
        self.page_index += 1
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _open_page_modal(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await interaction.response.send_modal(Lb2JumpModal(self))

    async def _show_requester_rank(
        self,
        interaction: discord.Interaction,
    ) -> None:
        row = next(
            (
                row for row in self.result.rows
                if row.discord_id == self.requester_id
            ),
            None,
        )
        if row is None:
            await interaction.response.send_message(
                'Your player is not present in this leaderboard view.',
                ephemeral=True,
            )
            return
        self.page_index = (row.rank - 1) // PAGE_SIZE
        self.rebuild()
        await interaction.response.edit_message(view=self)

    def rebuild(self) -> None:
        self.clear_items()
        self.page_index = min(self.page_index, self.page_count - 1)
        preset = PRESET_BY_KEY[self.preset_key]
        rankings, start, end = _rankings_text(
            self.result,
            self.page_index,
        )

        self.preset_select = discord.ui.Select(
            placeholder='Choose a leaderboard view',
            options=[
                discord.SelectOption(
                    label=item.label,
                    value=item.key,
                    description=item.description,
                    default=item.key == self.preset_key,
                )
                for item in PRESETS
            ],
        )
        self.preset_select.callback = self._select_preset

        previous = discord.ui.Button(
            label='Previous',
            emoji='◀️',
            disabled=self.page_index == 0,
        )
        previous.callback = self._previous_page
        page = discord.ui.Button(
            label=f'Page {self.page_index + 1}/{self.page_count}',
            style=discord.ButtonStyle.primary,
        )
        page.callback = self._open_page_modal
        next_page = discord.ui.Button(
            label='Next',
            emoji='▶️',
            disabled=self.page_index == self.page_count - 1,
        )
        next_page.callback = self._next_page
        my_rank = discord.ui.Button(
            label='My rank',
            emoji='🎯',
            style=discord.ButtonStyle.success,
        )
        my_rank.callback = self._show_requester_rank
        population = discord.ui.Button(
            label=(
                'Show all players'
                if self.population == 'active'
                else 'Show active only'
            ),
            emoji='👥',
        )
        population.callback = self._toggle_population

        container = discord.ui.Container(
            discord.ui.TextDisplay(
                '# 🏆 Player Leaderboard\n'
                f'### {_mode_summary(preset, self.population)}\n'
                f'-# {self.result.total_ranked} ranked players'
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(rankings),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(
                f'-# Page {self.page_index + 1} of {self.page_count} · '
                f'showing {start}–{end} of {len(self.result.rows)} loaded'
            ),
            discord.ui.ActionRow(self.preset_select),
            discord.ui.ActionRow(
                previous,
                page,
                next_page,
                my_rank,
                population,
            ),
            accent_colour=ACCENT_COLOUR,
        )
        self.add_item(container)

    async def on_timeout(self) -> None:
        for item in self.walk_children():
            if isinstance(item, (discord.ui.Button, discord.ui.Select)):
                item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
