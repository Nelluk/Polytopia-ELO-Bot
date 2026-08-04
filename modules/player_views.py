"""Components v2 presentation for immutable player workspace snapshots."""

from __future__ import annotations

import discord

from modules import components_v2, player_registration_workers, player_workers


PAGE_SIZE = 6
SECTIONS = (
    ('overview', 'Overview'),
    ('ratings', 'Ratings'),
    ('recent', 'Recent games'),
    ('incomplete', 'Incomplete'),
    ('completed', 'Completed'),
    ('season', 'Season games'),
    ('teams', 'Team & squads'),
)
SECTION_LABELS = dict(SECTIONS)


def _game_rows(
    snapshot: player_workers.PlayerWorkspaceSnapshot,
    section: str,
    completed_filter: str,
    season_filter: str = 'all',
):
    if section == 'recent':
        return snapshot.games[:25]
    if section == 'incomplete':
        return tuple(
            row for row in snapshot.games
            if row.status in ('Open', 'Incomplete', 'Unconfirmed')
        )
    if section == 'completed':
        rows = tuple(row for row in snapshot.games
                     if row.status == 'Completed')
        if completed_filter == 'wins':
            return tuple(row for row in rows if row.outcome == 'Win')
        if completed_filter == 'losses':
            return tuple(row for row in rows if row.outcome == 'Loss')
        return rows
    if section == 'season':
        rows = tuple(row for row in snapshot.games if row.season is not None)
        if season_filter != 'all':
            return tuple(
                row for row in rows if row.season == int(season_filter)
            )
        return rows
    return ()


def _game_text(rows) -> str:
    if not rows:
        return '*No games match this view.*'
    return '\n'.join(
        (
            f'**#{row.game_id} · {row.name}**  '
            f'`{row.status} · {row.outcome}`\n'
            f'> {row.date} · {"Ranked" if row.ranked else "Unranked"}'
            f'{" · Season " + str(row.season) if row.season else ""}\n'
            f'> {row.roster}'
        )
        for row in rows
    )


class PlayerWorkspace(components_v2.RequesterLayoutView):
    """Public player profile with database-free section navigation."""

    unauthorized_message = 'Only the requester can control this player view.'

    def __init__(
        self,
        *,
        requester_id: int,
        snapshot: player_workers.PlayerWorkspaceSnapshot,
        initial_section: str = 'overview',
        completed_filter: str = 'all',
        can_edit: bool = False,
        timeout: float = 300.0,
    ):
        super().__init__(requester_id=requester_id, timeout=timeout)
        self.snapshot = snapshot
        self.section = initial_section
        self.completed_filter = completed_filter
        self.season_filter = 'all'
        self.can_edit = can_edit
        self.rebuild()

    @property
    def rows(self):
        return _game_rows(
            self.snapshot,
            self.section,
            self.completed_filter,
            self.season_filter,
        )

    @property
    def page_count(self) -> int:
        return components_v2.page_count(self.rows, PAGE_SIZE)

    async def _select_section(self, interaction: discord.Interaction) -> None:
        self.section = self.section_select.values[0]
        self.page_index = 0
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _select_result(self, interaction: discord.Interaction) -> None:
        self.completed_filter = self.result_select.values[0]
        self.page_index = 0
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _select_season(self, interaction: discord.Interaction) -> None:
        self.season_filter = self.season_select.values[0]
        self.page_index = 0
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _profile_actions(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            'Profile editing remains available through the permission-checked '
            '`/player register` (or `$setname`) and `settime` commands while '
            'the remaining native edit workflows are modernized. The '
            'registered Polytopia name is account-wide.',
            ephemeral=True,
        )

    def _body(self) -> str:
        snapshot = self.snapshot
        if self.section == 'overview':
            team = (
                f'{snapshot.team_emoji} {snapshot.team_name}'.strip()
                if snapshot.team_name else 'No team'
            )
            timezone = snapshot.timezone or 'Not set'
            if snapshot.polytopia_name:
                polytopia_name = discord.utils.escape_markdown(
                    player_registration_workers.safe_public_name(
                        snapshot.polytopia_name
                    ),
                    as_needed=True,
                )
                polytopia_name_line = (
                    '**Canonical Polytopia name (account-wide):** '
                    f'{polytopia_name}'
                )
            else:
                polytopia_name_line = (
                    '**Canonical Polytopia name (account-wide):** *Not set*'
                )
            return (
                f'## <@{snapshot.discord_id}>\n'
                f'{polytopia_name_line}\n'
                f'**Team:** {team}\n'
                f'**Timezone:** {timezone}\n\n'
                f'**Local:** `{snapshot.local_elo} ELO` · '
                f'{snapshot.local_wins}W–{snapshot.local_losses}L\n'
                f'**Global:** `{snapshot.global_elo} ELO` · '
                f'{snapshot.global_wins}W–{snapshot.global_losses}L'
            )
        if self.section == 'ratings':
            local_rank = (
                f'#{snapshot.local_rank}/{snapshot.local_ranked_count}'
                if snapshot.local_rank else 'Unranked'
            )
            global_rank = (
                f'#{snapshot.global_rank}/{snapshot.global_ranked_count}'
                if snapshot.global_rank else 'Unranked'
            )
            return (
                f'## Ratings\n'
                f'**This server:** `{snapshot.local_elo}` current · '
                f'`{snapshot.local_peak}` peak · {local_rank}\n'
                f'**Global:** `{snapshot.global_elo}` current · '
                f'`{snapshot.global_peak}` peak · {global_rank}\n'
                f'**All-time local:** `{snapshot.local_all_time}` · '
                f'`{snapshot.local_all_time_peak}` peak\n'
                f'**All-time global:** `{snapshot.global_all_time}` · '
                f'`{snapshot.global_all_time_peak}` peak'
            )
        if self.section == 'teams':
            squads = (
                '\n'.join(f'- {name}' for name in snapshot.squad_names)
                if snapshot.squad_names else '*No recent squad context.*'
            )
            team = (
                f'{snapshot.team_emoji} {snapshot.team_name}'.strip()
                if snapshot.team_name else 'No team'
            )
            return f'## Team\n{team}\n\n## Squads\n{squads}'
        page_rows, _, _ = components_v2.page_slice(
            self.rows,
            self.page_index,
            PAGE_SIZE,
        )
        suffix = (
            f' · {self.completed_filter.title()}'
            if self.section == 'completed' else ''
        )
        return (
            f'## {SECTION_LABELS[self.section]}{suffix}\n'
            f'{_game_text(page_rows)}'
        )

    def rebuild(self) -> None:
        self.clear_items()
        self.page_index = min(self.page_index, self.page_count - 1)
        self.section_select = discord.ui.Select(
            placeholder='Choose a player view',
            options=[
                discord.SelectOption(
                    label=label,
                    value=key,
                    default=key == self.section,
                )
                for key, label in SECTIONS
            ],
        )
        self.section_select.callback = self._select_section
        components = [
            discord.ui.TextDisplay(
                f'# 👤 {self.snapshot.display_name}\n'
                f'-# Player workspace'
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(self._body()),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.ActionRow(self.section_select),
        ]
        if self.section == 'completed':
            self.result_select = discord.ui.Select(
                placeholder='Completed-game result',
                options=[
                    discord.SelectOption(
                        label=label,
                        value=value,
                        default=value == self.completed_filter,
                    )
                    for value, label in (
                        ('all', 'All completed'),
                        ('wins', 'Wins'),
                        ('losses', 'Losses'),
                    )
                ],
            )
            self.result_select.callback = self._select_result
            components.append(discord.ui.ActionRow(self.result_select))
        if self.section == 'season':
            seasons = sorted(
                {
                    row.season for row in self.snapshot.games
                    if row.season is not None
                },
                reverse=True,
            )
            self.season_select = discord.ui.Select(
                placeholder='Choose a season',
                options=[
                    discord.SelectOption(
                        label='All recorded seasons',
                        value='all',
                        default=self.season_filter == 'all',
                    ),
                    *[
                        discord.SelectOption(
                            label=f'Season {season}',
                            value=str(season),
                            default=self.season_filter == str(season),
                        )
                        for season in seasons[:24]
                    ],
                ],
            )
            self.season_select.callback = self._select_season
            components.append(discord.ui.ActionRow(self.season_select))
        if self.section in ('recent', 'incomplete', 'completed', 'season'):
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
            components.append(discord.ui.ActionRow(previous, page, next_page))
        if self.can_edit and self.section == 'overview':
            actions = discord.ui.Button(
                label='Profile actions',
                style=discord.ButtonStyle.secondary,
            )
            actions.callback = self._profile_actions
            components.append(discord.ui.ActionRow(actions))
        components.append(discord.ui.TextDisplay(
            '-# Results are public. Controls expire; rerun `/player show` '
            'for a fresh snapshot.'
        ))
        self.add_item(discord.ui.Container(
            *components,
            accent_colour=components_v2.DEFAULT_ACCENT,
        ))
