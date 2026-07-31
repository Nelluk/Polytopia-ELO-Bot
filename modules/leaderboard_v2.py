"""Components v2 leaderboard interfaces."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import datetime
import discord

from modules import components_v2, leaderboard_workers


PAGE_SIZE = 8
ACCENT_COLOUR = components_v2.DEFAULT_ACCENT


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


FILTER_KEYS = tuple(
    f'{scope}:{rating}:{era}:{population}'
    for scope in ('local', 'global')
    for rating in ('current', 'peak')
    for era in ('current', 'all-time')
    for population in ('active', 'all')
)


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


class PlayerLeaderboardWorkspace(components_v2.CachedRequesterLayoutView):
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
        initial_key = (preset_key, population)
        super().__init__(
            requester_id=requester_id,
            initial_key=initial_key,
            initial_result=result,
            loader=self._load_request_key,
            timeout=timeout,
        )
        self.guild_id = guild_id
        self.result_loader = loader
        self.active_cutoff = active_cutoff
        self.preset_key = preset_key
        self.population = population
        self.rebuild()

    @property
    def page_count(self) -> int:
        return components_v2.page_count(self.result.rows, PAGE_SIZE)

    async def _load_request_key(self, key):
        preset_key, population = key
        preset = PRESET_BY_KEY.get(preset_key)
        if preset is None:
            scope, rating, era = preset_key.split(':')
            preset = LeaderboardPreset(
                preset_key,
                f'{scope.title()} · {rating} · {era}',
                'Advanced filter combination',
                scope=scope,
                rating=rating,
                era=era,
            )
        request = leaderboard_workers.PlayerLeaderboardRequest(
            guild_id=self.guild_id,
            scope=preset.scope,
            rating=preset.rating,
            era=preset.era,
            population=population,
            active_cutoff=self.active_cutoff,
        )
        return await self.result_loader(request)

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

    async def _load_selected_result(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        return await self.load_key(
            interaction,
            (self.preset_key, self.population),
        )

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

    async def _select_advanced(
        self,
        interaction: discord.Interaction,
    ) -> None:
        previous_preset = self.preset_key
        previous_population = self.population
        scope, rating, era, population = (
            self.advanced_select.values[0].split(':')
        )
        self.preset_key = f'{scope}:{rating}:{era}'
        self.population = population
        if not await self._edit_after_selection(interaction):
            self.preset_key = previous_preset
            self.population = previous_population

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
        await self.open_page_modal(interaction)

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
        preset = PRESET_BY_KEY.get(self.preset_key)
        if preset is None:
            scope, rating, era = self.preset_key.split(':')
            preset = LeaderboardPreset(
                self.preset_key,
                f'{scope.title()} · {rating} · {era}',
                'Advanced filters',
                scope=scope,
                rating=rating,
                era=era,
            )
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
        current_advanced = (
            f'{preset.scope}:{preset.rating}:{preset.era}:{self.population}'
        )
        self.advanced_select = discord.ui.Select(
            placeholder='Advanced filters · all 16 legacy combinations',
            options=[
                discord.SelectOption(
                    label=(
                        f'{scope.title()} · {rating} · {era} · {population}'
                    )[:100],
                    value=key,
                    default=key == current_advanced,
                )
                for key in FILTER_KEYS
                for scope, rating, era, population in [key.split(':')]
            ],
        )
        self.advanced_select.callback = self._select_advanced

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
            discord.ui.ActionRow(self.advanced_select),
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


# Compatibility name for tests and retained internal imports during P7.6.
ExperimentalLeaderboardView = PlayerLeaderboardWorkspace


class ActivityLeaderboardWorkspace(components_v2.RequesterLayoutView):
    """Components v2 pagination for one immutable activity snapshot."""

    def __init__(
        self,
        *,
        requester_id: int,
        result: leaderboard_workers.ActivityLeaderboardResult,
        timeout: float = 300.0,
    ):
        super().__init__(requester_id=requester_id, timeout=timeout)
        self.result = result
        self.rebuild()

    @property
    def page_count(self) -> int:
        return components_v2.page_count(self.result.rows, PAGE_SIZE)

    def rebuild(self) -> None:
        self.clear_items()
        self.page_index = min(self.page_index, self.page_count - 1)
        rows, start, end = components_v2.page_slice(
            self.result.rows,
            self.page_index,
            PAGE_SIZE,
        )
        count_label = (
            'games played'
            if self.result.view == 'global-all-time'
            else 'recent games'
        )
        lines = [
            (
                f'`{row.rank:>2}.` **{row.team_emoji}{row.name}**\n'
                f'> `{row.elo:>4} ELO` · **{row.games} {count_label}**'
            )
            for row in rows
        ] or ['*No activity found.*']
        previous = discord.ui.Button(
            label='Previous',
            emoji='◀️',
            disabled=self.page_index == 0,
        )
        previous.callback = self.show_previous
        page = discord.ui.Button(
            label=f'Page {self.page_index + 1}/{self.page_count}',
            style=discord.ButtonStyle.primary,
        )
        page.callback = self.open_page_modal
        next_page = discord.ui.Button(
            label='Next',
            emoji='▶️',
            disabled=self.page_index == self.page_count - 1,
        )
        next_page.callback = self.show_next
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(
                f'# 📊 {self.result.title}\n'
                f'-# {self.result.total_players} players'
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay('\n'.join(lines)),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(
                f'-# Page {self.page_index + 1} of {self.page_count} · '
                f'showing {start}–{end} of {len(self.result.rows)}'
            ),
            discord.ui.ActionRow(previous, page, next_page),
            accent_colour=ACCENT_COLOUR,
        ))
