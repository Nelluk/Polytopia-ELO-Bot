"""Components v2 presentation for immutable player profile snapshots."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from io import BytesIO
import logging
import re

import discord

from modules import components_v2, player_registration_workers, player_workers


logger = logging.getLogger(__name__)


PAGE_SIZE = 6
BADGE_PAGE_SIZE = 10
BASE_SECTIONS = (
    ('overview', 'Overview'),
    ('ratings', 'Ratings'),
    ('analytics', 'Analytics'),
    ('recent', 'Recent games'),
    ('incomplete', 'Incomplete'),
    ('completed', 'Completed'),
    ('season', 'Season games'),
    ('teams', 'Team & Squads'),
)
SECTIONS = BASE_SECTIONS
SECTION_LABELS = dict(BASE_SECTIONS) | {'badges': 'Badges'}


def _safe_badge(value: object) -> str:
    raw = str(value or '')
    match = re.fullmatch(
        r'(?P<emoji><a?:[A-Za-z0-9_]{2,32}:\d+>)(?: (?P<label>.*))?',
        raw,
    )
    if match is not None:
        label = match.group('label')
        return match.group('emoji') + (
            ' ' + discord.utils.escape_mentions(
                discord.utils.escape_markdown(label),
            )
            if label else ''
        )
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(raw),
    )


def _safe_text(value: object) -> str:
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value or '')),
    )


def _squad_members_text(
    member_names: tuple[str, ...],
    *,
    limit: int,
) -> str:
    text = ' / '.join(_safe_text(member) for member in member_names)
    if not text:
        return 'No registered members'
    return text if len(text) <= limit else f'{text[:limit - 1]}…'


def _response_is_done(interaction: discord.Interaction) -> bool:
    value = getattr(interaction.response, 'is_done', False)
    return bool(value() if callable(value) else value)


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


def _game_text(rows, *, include_channels: bool = False) -> str:
    if not rows:
        return '*No games match this view.*'
    rendered = []
    for row in rows:
        channel = (
            f'\n> <#{row.channel_id}>'
            if include_channels and row.channel_id is not None
            else ''
        )
        rendered.append(
            f'**#{row.game_id} · {row.name}**  '
            f'`{row.status} · {row.outcome}`\n'
            f'> {row.date} · {"Ranked" if row.ranked else "Unranked"}'
            f'{" · Season " + str(row.season) if row.season else ""}\n'
            f'> {row.roster}{channel}'
        )
    return '\n'.join(rendered)


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
        avatar_url: str = '',
        history_graph_loader: Callable[
            [
                player_workers.PlayerWorkspaceSnapshot,
                str,
            ],
            Awaitable[player_workers.PlayerHistoryGraph],
        ] = player_workers.run_player_history_graph,
        timeout: float = 300.0,
    ):
        super().__init__(requester_id=requester_id, timeout=timeout)
        self.snapshot = snapshot
        self.section = initial_section
        self.completed_filter = completed_filter
        self.season_filter = 'all'
        self.can_edit = can_edit
        self.avatar_url = str(avatar_url or '')
        self.history_era = 'current'
        self.selected_squad_id: int | None = None
        self.history_graph_loader = history_graph_loader
        self.history_graphs: dict[str, player_workers.PlayerHistoryGraph] = {}
        self.rebuild()

    @property
    def rows(self):
        if self.section == 'badges':
            return self.snapshot.badges
        return _game_rows(
            self.snapshot,
            self.section,
            self.completed_filter,
            self.season_filter,
        )

    @property
    def page_count(self) -> int:
        page_size = BADGE_PAGE_SIZE if self.section == 'badges' else PAGE_SIZE
        return components_v2.page_count(self.rows, page_size)

    async def _select_section(self, interaction: discord.Interaction) -> None:
        selected = self.section_select.values[0]
        if selected == 'analytics':
            await self._show_analytics(interaction, era=self.history_era)
            return
        self.section = selected
        self.page_index = 0
        self.selected_squad_id = None
        self.rebuild()
        await interaction.response.edit_message(view=self, attachments=[])

    async def _private_error(
        self,
        interaction: discord.Interaction,
        message: str,
    ) -> None:
        if _response_is_done(interaction):
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    def history_graph_files(self) -> list[discord.File]:
        graph = self.history_graphs.get(self.history_era)
        if graph is None or not graph.png_bytes:
            return []
        return [
            discord.File(
                BytesIO(graph.png_bytes),
                filename=graph.filename,
            )
        ]

    async def _show_analytics(
        self,
        interaction: discord.Interaction,
        *,
        era: str,
    ) -> None:
        if era not in {'current', 'all_time'}:
            await self._private_error(
                interaction,
                'Choose current-reset or all-time ELO history.',
            )
            return
        previous = (self.section, self.history_era, self.page_index)
        deferred = False
        try:
            graph = self.history_graphs.get(era)
            if graph is None:
                if not _response_is_done(interaction):
                    await interaction.response.defer()
                    deferred = True
                graph = await self.history_graph_loader(self.snapshot, era)
                self.history_graphs[era] = graph
            self.section = 'analytics'
            self.history_era = era
            self.page_index = 0
            self.rebuild()
            if deferred or _response_is_done(interaction):
                await interaction.edit_original_response(
                    view=self,
                    attachments=self.history_graph_files(),
                )
            else:
                await interaction.response.edit_message(
                    view=self,
                    attachments=self.history_graph_files(),
                )
        except Exception:
            logger.exception(
                'Could not render player analytics for requester %s target %s',
                self.requester_id,
                self.snapshot.discord_id,
            )
            self.section, self.history_era, self.page_index = previous
            self.rebuild()
            message = (
                'Could not render the player analytics. Run `/player show` '
                'again or retry this section.'
            )
            if deferred:
                await interaction.followup.send(message, ephemeral=True)
            else:
                await self._private_error(interaction, message)

    async def _select_history_era(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await self._show_analytics(
            interaction,
            era=self.history_era_select.values[0],
        )

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

    async def _select_squad(self, interaction: discord.Interaction) -> None:
        selected = self.squad_select.values[0]
        self.selected_squad_id = (
            None if selected == 'all' else int(selected)
        )
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _profile_actions(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            'Profile editing remains available through the permission-checked '
            '`/player register` and `/player timezone` commands. `$setname` '
            'and `$settime` remain available for compatibility. The '
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
            timezone_line = (
                f'**Timezone:** {snapshot.timezone}\n'
                if snapshot.timezone else ''
            )
            if snapshot.polytopia_name:
                polytopia_name = player_registration_workers.safe_public_name(
                    snapshot.polytopia_name
                )
                polytopia_name_line = (
                    '**Polytopia name:** '
                    f'`{polytopia_name.replace("`", "ˋ")}`'
                )
            else:
                polytopia_name_line = '**Polytopia name:** *Not set*'
            badge_block = ''
            if snapshot.badges:
                shown = '\n'.join(
                    f'- {_safe_badge(value)}'
                    for value in snapshot.badges[:6]
                )
                more = len(snapshot.badges) - 6
                badge_block = f'\n\n**Badges:**\n{shown}'
                if more > 0:
                    badge_block += f'\n…and {more} more — open Badges'
            return (
                f'## <@{snapshot.discord_id}>\n'
                f'{polytopia_name_line}\n'
                f'**Last-known team:** {team}\n'
                f'{timezone_line}\n'
                f'**Local:** `{snapshot.local_elo} ELO` · '
                f'{snapshot.local_wins}W–{snapshot.local_losses}L\n'
                f'**Global:** `{snapshot.global_elo} ELO` · '
                f'{snapshot.global_wins}W–{snapshot.global_losses}L'
                f'{badge_block}'
            )
        if self.section == 'badges':
            page_rows, start, end = components_v2.page_slice(
                snapshot.badges,
                self.page_index,
                BADGE_PAGE_SIZE,
            )
            body = '\n'.join(
                f'{index}. {_safe_badge(value)}'
                for index, value in enumerate(page_rows, start=start)
            )
            return (
                '## Badges\n'
                f'{body}\n'
                f'-# Showing {start}–{end} of {len(snapshot.badges)}'
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
                f'`{snapshot.local_all_time_peak}` peak · '
                f'{snapshot.local_all_time_wins}W–'
                f'{snapshot.local_all_time_losses}L\n'
                f'**All-time global:** `{snapshot.global_all_time}` · '
                f'`{snapshot.global_all_time_peak}` peak · '
                f'{snapshot.global_all_time_wins}W–'
                f'{snapshot.global_all_time_losses}L'
            )
        if self.section == 'analytics':
            if snapshot.discord_id == self.requester_id:
                matchup = (
                    'View another member with `/player show` to compare your '
                    'confirmed ranked local 1v1 record.'
                )
            elif snapshot.head_to_head is None:
                matchup = (
                    'No requester comparison is available. The requester may '
                    'not be registered in this server.'
                )
            elif snapshot.head_to_head.total_games == 0:
                matchup = (
                    f'No confirmed ranked local 1v1 games between '
                    f'<@{snapshot.head_to_head.requester_discord_id}> and '
                    f'<@{snapshot.head_to_head.target_discord_id}>.'
                )
            else:
                matchup = (
                    f'<@{snapshot.head_to_head.requester_discord_id}> '
                    f'**{snapshot.head_to_head.requester_wins}** – '
                    f'**{snapshot.head_to_head.target_wins}** '
                    f'<@{snapshot.head_to_head.target_discord_id}> '
                    f'({snapshot.head_to_head.total_games} games)'
                )
            era = (
                'Current-reset'
                if self.history_era == 'current'
                else 'All-time'
            )
            local_points = sum(
                getattr(point, f'{self.history_era}_elo') is not None
                for point in snapshot.local_history
            )
            global_points = sum(
                getattr(point, f'{self.history_era}_elo') is not None
                for point in snapshot.global_history
            )
            bounded = (
                '\n-# History is bounded to the newest 500 points per scope.'
                if snapshot.history_truncated else ''
            )
            return (
                f'## {era} ELO history\n'
                f'**{snapshot.guild_display_name}:** {local_points} points · '
                f'**Global:** {global_points} points{bounded}\n\n'
                f'## Your local 1v1 record\n{matchup}'
            )
        if self.section == 'teams':
            team = (
                f'{snapshot.team_emoji} {snapshot.team_name}'.strip()
                if snapshot.team_name else 'No team'
            )
            selected = next(
                (
                    squad for squad in snapshot.squads
                    if squad.squad_id == self.selected_squad_id
                ),
                None,
            )
            if selected is not None:
                name = (_safe_text(selected.name) or 'Unnamed squad')[:80]
                members = _squad_members_text(
                    selected.member_names,
                    limit=600,
                )
                last_played = (
                    f'\n**Last activity:** `{selected.last_played}`'
                    if selected.last_played else ''
                )
                squads = (
                    f'## Squad #{selected.squad_id} · {name}\n'
                    f'**Members:** {members}\n'
                    f'**Games together:** `{selected.games_played}`\n'
                    f'**Confirmed ranked record:** '
                    f'`{selected.wins}W – {selected.losses}L`\n'
                    f'**Current squad ELO:** `{selected.elo}`'
                    f'{last_played}\n'
                    f'-# Run `/squad show squad_id:{selected.squad_id}` for '
                    'leaderboard rank and recent games.'
                )
            elif snapshot.squads:
                rows = []
                for squad in snapshot.squads:
                    name = (_safe_text(squad.name) or 'Unnamed squad')[:80]
                    members = _squad_members_text(
                        squad.member_names,
                        limit=220,
                    )
                    activity = (
                        f' · last {squad.last_played}'
                        if squad.last_played else ''
                    )
                    rows.append(
                        f'**#{squad.squad_id} · {name}**\n'
                        f'> {members}\n'
                        f'> `{squad.games_played} games` · '
                        f'`{squad.wins}W–{squad.losses}L` · '
                        f'`{squad.elo} ELO`{activity}'
                    )
                count_label = (
                    f'showing {len(snapshot.squads)} most-played of '
                    f'{snapshot.squad_total} eligible squads'
                )
                squads = (
                    f'## Squads played with\n'
                    f'-# {count_label}\n\n'
                    f'{"\n\n".join(rows)}'
                )
            else:
                squads = '## Squads played with\n*No eligible squads found.*'
            return (
                f'## Last-known team\n{team}\n\n'
                f'{squads}\n'
                '-# Squads are game lineups, not current membership; '
                'eligibility follows the server squad-game threshold.'
            )
        page_rows, _, _ = components_v2.page_slice(
            self.rows,
            self.page_index,
            PAGE_SIZE,
        )
        suffix = (
            f' · {self.completed_filter.title()}'
            if self.section == 'completed' else ''
        )
        game_text = _game_text(
            page_rows,
            include_channels=(self.section == 'incomplete'),
        )
        if self.section == 'season':
            if snapshot.polychamps_tier_records:
                tier_rows = '\n'.join(
                    f'**{_safe_text(record.name)}:** '
                    f'`{record.wins}W–{record.losses}L`'
                    for record in snapshot.polychamps_tier_records
                )
                record_text = (
                    '## PolyChampions season record\n'
                    f'**Career total:** `{snapshot.polychamps_wins}W–'
                    f'{snapshot.polychamps_losses}L`\n'
                    f'{tier_rows}'
                )
            else:
                record_text = (
                    '## PolyChampions season record\n'
                    '*No completed PolyChampions season games.*'
                )
            return (
                f'{record_text}\n\n'
                f'## {SECTION_LABELS[self.section]}\n{game_text}'
            )
        return f'## {SECTION_LABELS[self.section]}{suffix}\n{game_text}'

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
                for key, label in (
                    BASE_SECTIONS
                    + ((('badges', 'Badges'),) if self.snapshot.badges else ())
                )
            ],
        )
        self.section_select.callback = self._select_section
        heading = (
            f'# 👤 {self.snapshot.display_name}\n'
            f'-# Player profile'
        )
        if self.section == 'overview' and self.avatar_url:
            profile_content = discord.ui.Section(
                discord.ui.TextDisplay(heading),
                discord.ui.TextDisplay(self._body()),
                accessory=discord.ui.Thumbnail(
                    self.avatar_url,
                    description='Current Discord avatar',
                ),
            )
        else:
            profile_content = discord.ui.TextDisplay(heading)
        components = [
            profile_content,
            *(
                () if self.section == 'overview' and self.avatar_url else (
                    discord.ui.Separator(
                        spacing=discord.SeparatorSpacing.small,
                    ),
                    discord.ui.TextDisplay(self._body()),
                )
            ),
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
        if self.section == 'analytics':
            self.history_era_select = discord.ui.Select(
                placeholder='ELO history era',
                options=[
                    discord.SelectOption(
                        label='Current-reset ELO',
                        value='current',
                        default=self.history_era == 'current',
                    ),
                    discord.SelectOption(
                        label='All-time ELO',
                        value='all_time',
                        default=self.history_era == 'all_time',
                    ),
                ],
            )
            self.history_era_select.callback = self._select_history_era
            components.append(discord.ui.ActionRow(self.history_era_select))
            graph = self.history_graphs.get(self.history_era)
            if graph is not None and graph.png_bytes:
                components.extend([
                    discord.ui.Separator(
                        spacing=discord.SeparatorSpacing.small,
                    ),
                    discord.ui.MediaGallery(
                        discord.MediaGalleryItem(
                            f'attachment://{graph.filename}',
                            description=(
                                'Current-reset ELO history'
                                if self.history_era == 'current'
                                else 'All-time ELO history'
                            ),
                        ),
                    ),
                ])
        if self.section == 'teams' and self.snapshot.squads:
            self.squad_select = discord.ui.Select(
                placeholder='Open a squad summary',
                options=[
                    discord.SelectOption(
                        label='Most-played squads',
                        value='all',
                        default=self.selected_squad_id is None,
                    ),
                    *[
                        discord.SelectOption(
                            label=(
                                f'#{squad.squad_id} · '
                                f'{squad.name or "Unnamed squad"}'
                            )[:100],
                            value=str(squad.squad_id),
                            description=(
                                f'{" / ".join(squad.member_names)} · '
                                f'{squad.games_played} games · '
                                f'{squad.wins}W–{squad.losses}L'
                            )[:100],
                            default=(
                                squad.squad_id == self.selected_squad_id
                            ),
                        )
                        for squad in self.snapshot.squads
                    ],
                ],
            )
            self.squad_select.callback = self._select_squad
            components.append(discord.ui.ActionRow(self.squad_select))
        if self.section in (
            'recent', 'incomplete', 'completed', 'season', 'badges'
        ):
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
