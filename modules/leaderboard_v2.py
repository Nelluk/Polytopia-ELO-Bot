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


FILTER_DIMENSIONS = {
    'scope': (
        ('This server', 'local'),
        ('Global', 'global'),
    ),
    'rating': (
        ('Current', 'current'),
        ('Peak', 'peak'),
    ),
    'era': (
        ('Current era', 'current'),
        ('All time', 'all-time'),
    ),
    'population': (
        ('Active', 'active'),
        ('All registered', 'all'),
    ),
}


def _preset_for_dimensions(
    scope: str,
    rating: str,
    era: str,
) -> LeaderboardPreset | None:
    return next(
        (
            preset for preset in PRESETS
            if (
                preset.scope,
                preset.rating,
                preset.era,
            ) == (scope, rating, era)
        ),
        None,
    )


def _cache_key_for_filters(
    scope: str,
    rating: str,
    era: str,
    population: str,
) -> tuple[str, str]:
    valid_values = {
        name: {value for _, value in options}
        for name, options in FILTER_DIMENSIONS.items()
    }
    values = {
        'scope': scope,
        'rating': rating,
        'era': era,
        'population': population,
    }
    if any(values[name] not in valid_values[name] for name in values):
        raise ValueError('Unknown player leaderboard filter.')

    preset = _preset_for_dimensions(scope, rating, era)
    return (
        preset.key if preset is not None else f'{scope}:{rating}:{era}',
        population,
    )


def _filter_dimensions_for_preset(
    preset_key: str,
) -> tuple[str, str, str]:
    preset = PRESET_BY_KEY.get(preset_key)
    if preset is not None:
        return preset.scope, preset.rating, preset.era
    try:
        scope, rating, era = preset_key.split(':')
    except ValueError as exc:
        raise ValueError('Unknown player leaderboard preset.') from exc
    _cache_key_for_filters(scope, rating, era, 'active')
    return scope, rating, era


def _radio_options(
    dimension: str,
    selected: str,
) -> list[discord.RadioGroupOption]:
    return [
        discord.RadioGroupOption(
            label=label,
            value=value,
            default=value == selected,
        )
        for label, value in FILTER_DIMENSIONS[dimension]
    ]


def _mode_summary(preset: LeaderboardPreset, population: str) -> str:
    labels = {
        name: {value: label for label, value in options}[value]
        for name, options, value in (
            ('scope', FILTER_DIMENSIONS['scope'], preset.scope),
            ('rating', FILTER_DIMENSIONS['rating'], preset.rating),
            ('era', FILTER_DIMENSIONS['era'], preset.era),
            ('population', FILTER_DIMENSIONS['population'], population),
        )
    }
    return (
        f'**Scope:** {labels["scope"]} · '
        f'**Rating:** {labels["rating"]}\n'
        f'**Era:** {labels["era"]} · '
        f'**Population:** {labels["population"]}'
    )


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


class PlayerLeaderboardAdvancedFiltersModal(discord.ui.Modal):
    """Requester-bound modal for the complete legacy filter matrix."""

    def __init__(self, workspace: 'PlayerLeaderboardWorkspace'):
        super().__init__(title='Advanced leaderboard filters', timeout=60.0)
        self.workspace = workspace
        self._submitted = False
        scope, rating, era = _filter_dimensions_for_preset(
            workspace.preset_key,
        )

        self.scope = discord.ui.Label(
            text='Scope',
            description='Where to rank players.',
            component=discord.ui.RadioGroup(
                custom_id='leaderboard-filters-scope',
                options=_radio_options('scope', scope),
            ),
        )
        self.rating = discord.ui.Label(
            text='Rating',
            description='Which ELO value to rank.',
            component=discord.ui.RadioGroup(
                custom_id='leaderboard-filters-rating',
                options=_radio_options('rating', rating),
            ),
        )
        self.era = discord.ui.Label(
            text='Era',
            description='Current reset or permanent history.',
            component=discord.ui.RadioGroup(
                custom_id='leaderboard-filters-era',
                options=_radio_options('era', era),
            ),
        )
        self.population = discord.ui.Label(
            text='Population',
            description='Active players or every registration.',
            component=discord.ui.RadioGroup(
                custom_id='leaderboard-filters-population',
                options=_radio_options('population', workspace.population),
            ),
        )
        self.add_item(self.scope)
        self.add_item(self.rating)
        self.add_item(self.era)
        self.add_item(self.population)

    async def _send_private(
        self,
        interaction: discord.Interaction,
        content: str,
    ) -> None:
        is_done = getattr(interaction.response, 'is_done', None)
        if callable(is_done) and is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        workspace = self.workspace
        if interaction.user.id != workspace.requester_id:
            await self._send_private(
                interaction,
                'Only the requester can change this leaderboard filter.',
            )
            return
        if workspace.is_finished():
            await self._send_private(
                interaction,
                workspace.expired_message,
            )
            return
        if self._submitted:
            await self._send_private(
                interaction,
                'This filter form was already submitted. Run the command again '
                'for a fresh leaderboard.',
            )
            return

        values = (
            self.scope.component.value,
            self.rating.component.value,
            self.era.component.value,
            self.population.component.value,
        )
        try:
            cache_key = _cache_key_for_filters(*values)
        except ValueError:
            await self._send_private(
                interaction,
                'Choose one option in each leaderboard filter.',
            )
            return

        self._submitted = True
        await workspace._apply_cache_key(interaction, cache_key)


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
        scope, rating, era = _filter_dimensions_for_preset(self.preset_key)
        return leaderboard_workers.PlayerLeaderboardRequest(
            guild_id=self.guild_id,
            scope=scope,
            rating=rating,
            population=self.population,
            active_cutoff=self.active_cutoff,
            era=era,
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
        previous_page = self.page_index
        if not await self._load_selected_result(interaction):
            self.page_index = previous_page
            return False
        self.page_index = 0
        self.rebuild()
        if interaction.response.is_done():
            await interaction.edit_original_response(view=self)
        else:
            await interaction.response.edit_message(view=self)
        return True

    async def _apply_cache_key(
        self,
        interaction: discord.Interaction,
        cache_key: tuple[str, str],
    ) -> bool:
        previous_state = (
            self.preset_key,
            self.population,
            self.page_index,
            self.result,
        )
        self.preset_key, self.population = cache_key
        if await self._edit_after_selection(interaction):
            return True
        (
            self.preset_key,
            self.population,
            self.page_index,
            self.result,
        ) = previous_state
        return False

    async def _select_preset(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await self._apply_cache_key(
            interaction,
            (self.preset_select.values[0], self.population),
        )

    async def _toggle_population(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await self._apply_cache_key(
            interaction,
            (
                self.preset_key,
                'all' if self.population == 'active' else 'active',
            ),
        )

    async def _open_advanced_filters(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if not await self.authorize(interaction):
            return
        if self.is_finished():
            await interaction.response.send_message(
                self.expired_message,
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            PlayerLeaderboardAdvancedFiltersModal(self),
        )

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
            scope, rating, era = _filter_dimensions_for_preset(
                self.preset_key,
            )
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
            placeholder='Common filters',
            options=[
                discord.SelectOption(
                    label=item.label,
                    value=item.key,
                    description=item.description,
                    default=(
                        item.key == self.preset_key
                        or (
                            item.scope,
                            item.rating,
                            item.era,
                        ) == (
                            preset.scope,
                            preset.rating,
                            preset.era,
                        )
                    ),
                )
                for item in PRESETS
            ],
        )
        self.preset_select.callback = self._select_preset
        advanced = discord.ui.Button(
            label='Advanced filters...',
            style=discord.ButtonStyle.secondary,
        )
        advanced.callback = self._open_advanced_filters

        previous = discord.ui.Button(
            label='Previous',
            emoji='◀️',
            disabled=self.page_index == 0,
        )
        previous.callback = self._previous_page
        page = discord.ui.Button(
            label=f'Jump to page · {self.page_index + 1}/{self.page_count}',
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
                f'{_mode_summary(preset, self.population)}\n'
                f'-# {self.result.total_ranked} ranked players'
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(rankings),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(
                f'-# Page {self.page_index + 1} of {self.page_count} · '
                f'showing {start}–{end} of {len(self.result.rows)} loaded\n'
                '-# Rating and Population change the view; they do not '
                'redefine W–L.'
            ),
            discord.ui.TextDisplay('**Common filters**'),
            discord.ui.ActionRow(self.preset_select),
            discord.ui.ActionRow(advanced),
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
