"""Requester-controlled Components v2 role leaderboard workspace."""

from __future__ import annotations

import discord

from modules import components_v2, role_leaderboard, role_leaderboard_workers
from modules.league import free_agent_role_name


PAGE_SIZE = role_leaderboard_workers.ROLE_LEADERBOARD_PAGE_SIZE


def _response_is_done(interaction: discord.Interaction) -> bool:
    response = getattr(interaction, 'response', None)
    value = getattr(response, 'is_done', False)
    return bool(value() if callable(value) else value)


def _safe(value: object) -> str:
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value))
    )


SORT_LABELS = {
    'global_elo': 'Global ELO',
    'local_elo': 'Local ELO',
    'total_games': 'Total games',
    'recent_games': 'Recent games · 14 days',
}
SCOPE_LABELS = {
    'global': 'Global ELO and W/L',
    'local': 'Local ELO and W/L',
}


class RoleLeaderboardPageJumpModal(discord.ui.Modal):
    """Requester-only page jump over an already loaded result."""

    def __init__(self, view: 'RoleLeaderboardWorkspace'):
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
            description='Move the public result to this page.',
            component=self.page_number,
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        view = self.target_view
        if not await view.authorize(interaction):
            return
        try:
            page = int(self.page_number.value.strip())
        except (AttributeError, TypeError, ValueError):
            page = 0
        if page < 1 or page > view.page_count:
            await view._private_error(
                interaction,
                f'Enter a page number from 1 to {view.page_count}.',
            )
            return
        view.page_index = page - 1
        view.rebuild()
        await interaction.response.edit_message(view=view)


class RoleLeaderboardWorkspace(components_v2.RequesterLayoutView):
    """Public role rankings whose controls belong to the invoking user."""

    unauthorized_message = 'Only the requester can control this role view.'

    def __init__(
        self,
        *,
        guild_id: int,
        requester_id: int,
        result: role_leaderboard_workers.RoleLeaderboardResult,
        role_snapshots: tuple[
            role_leaderboard_workers.RoleLeaderboardRoleSnapshot,
            ...
        ],
        selected_role_ids: tuple[int, ...],
        selected_role_names: tuple[str, ...],
        match_mode: str = 'all',
        sort_key: str = 'global_elo',
        scope: str = 'global',
        can_select_roles: bool = False,
        timeout: float = role_leaderboard.ROLE_LEADERBOARD_CONTROL_TIMEOUT,
    ):
        super().__init__(requester_id=requester_id, timeout=timeout)
        self.guild_id = int(guild_id)
        self.result = result
        self.role_snapshots = tuple(role_snapshots)
        self.role_by_id = {
            role.role_id: role for role in self.role_snapshots
        }
        self.selected_role_ids = tuple(selected_role_ids)
        self.selected_role_names = tuple(selected_role_names)
        self.match_mode = match_mode
        self.sort_key = sort_key
        self.scope = scope
        self.can_select_roles = bool(can_select_roles)
        self.free_agent_role_id = next(
            (
                role.role_id for role in self.role_snapshots
                if role.name == free_agent_role_name
            ),
            None,
        )
        self.message: discord.Message | None = None
        self.rebuild()

    @property
    def page_count(self) -> int:
        page = role_leaderboard_workers.role_leaderboard_page(
            self.result,
            selected_role_ids=self.selected_role_ids,
            selected_role_names=self.selected_role_names,
            match_mode=self.match_mode,
            sort_key=self.sort_key,
            scope=self.scope,
            page_index=0,
            page_size=PAGE_SIZE,
        )
        return page.page_count

    @property
    def current_page(self) -> role_leaderboard_workers.RoleLeaderboardPage:
        return role_leaderboard_workers.role_leaderboard_page(
            self.result,
            selected_role_ids=self.selected_role_ids,
            selected_role_names=self.selected_role_names,
            match_mode=self.match_mode,
            sort_key=self.sort_key,
            scope=self.scope,
            page_index=self.page_index,
            page_size=PAGE_SIZE,
        )

    async def _private_error(
        self,
        interaction: discord.Interaction,
        message: str,
    ) -> None:
        if _response_is_done(interaction):
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def authorize(self, interaction: discord.Interaction) -> bool:
        if self.is_finished():
            await self._private_error(interaction, self.expired_message)
            return False
        if int(getattr(interaction.user, 'id', 0)) != self.requester_id:
            await self._private_error(interaction, self.unauthorized_message)
            return False
        return True

    async def _edit_public(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.edit_message(view=self)
        except Exception as exc:
            await self._private_error(
                interaction,
                f'Could not update the role leaderboard: {exc}',
            )

    async def _apply_state(
        self,
        interaction: discord.Interaction,
        *,
        match_mode: str | None = None,
        sort_key: str | None = None,
        scope: str | None = None,
    ) -> None:
        if not await self.authorize(interaction):
            return
        previous = (self.match_mode, self.sort_key, self.scope, self.page_index)
        if match_mode is not None:
            if match_mode not in role_leaderboard_workers.VALID_MATCH_MODES:
                await self._private_error(
                    interaction,
                    'Choose one of the displayed role matching modes.',
                )
                return
            self.match_mode = match_mode
        if sort_key is not None:
            if sort_key not in role_leaderboard_workers.VALID_SORTS:
                await self._private_error(
                    interaction,
                    'Choose one of the displayed leaderboard sorts.',
                )
                return
            self.sort_key = sort_key
        if scope is not None:
            if scope not in role_leaderboard_workers.VALID_SCOPES:
                await self._private_error(
                    interaction,
                    'Choose one of the displayed ELO scopes.',
                )
                return
            self.scope = scope
        self.page_index = 0
        try:
            self.rebuild()
        except Exception as exc:
            self.match_mode, self.sort_key, self.scope, self.page_index = previous
            self.rebuild()
            await self._private_error(
                interaction,
                f'Could not update the role leaderboard: {exc}',
            )
            return
        await self._edit_public(interaction)

    async def _select_match(self, interaction: discord.Interaction) -> None:
        await self._apply_state(
            interaction,
            match_mode=self.match_select.values[0],
        )

    async def _select_sort(self, interaction: discord.Interaction) -> None:
        await self._apply_state(
            interaction,
            sort_key=self.sort_select.values[0],
        )

    async def _select_scope(self, interaction: discord.Interaction) -> None:
        await self._apply_state(
            interaction,
            scope=self.scope_select.values[0],
        )

    async def _select_roles(self, interaction: discord.Interaction) -> None:
        if not await self.authorize(interaction):
            return
        if not self.can_select_roles:
            await self._private_error(
                interaction,
                'Only staff and House Leaders/Co-Leaders can choose arbitrary roles.',
            )
            return
        try:
            role_ids = role_leaderboard.validate_role_values(
                self.role_select.values,
                guild_id=self.guild_id,
                role_snapshots=self.role_snapshots,
            )
        except role_leaderboard_workers.RoleLeaderboardValidationError as exc:
            await self._private_error(interaction, str(exc))
            return
        self.selected_role_ids = tuple(role_ids)
        self.selected_role_names = tuple(
            self.role_by_id[role_id].name
            for role_id in self.selected_role_ids
        )
        self.page_index = 0
        self.rebuild()
        await self._edit_public(interaction)

    async def _free_agents(self, interaction: discord.Interaction) -> None:
        if not await self.authorize(interaction):
            return
        if self.free_agent_role_id is None:
            await self._private_error(
                interaction,
                f'Could not find the configured {free_agent_role_name} role.',
            )
            return
        self.selected_role_ids = (self.free_agent_role_id,)
        self.selected_role_names = (free_agent_role_name,)
        self.match_mode = 'all'
        self.page_index = 0
        self.rebuild()
        await self._edit_public(interaction)

    async def _previous_page(self, interaction: discord.Interaction) -> None:
        if not await self.authorize(interaction):
            return
        self.page_index = max(0, self.page_index - 1)
        self.rebuild()
        await self._edit_public(interaction)

    async def _next_page(self, interaction: discord.Interaction) -> None:
        if not await self.authorize(interaction):
            return
        self.page_index = min(self.page_count - 1, self.page_index + 1)
        self.rebuild()
        await self._edit_public(interaction)

    async def _open_page_modal(self, interaction: discord.Interaction) -> None:
        if not await self.authorize(interaction):
            return
        await interaction.response.send_modal(RoleLeaderboardPageJumpModal(self))

    def rebuild(self) -> None:
        self.clear_items()
        self.page_index = min(self.page_index, self.page_count - 1)
        page = self.current_page
        scope_label = SCOPE_LABELS[page.scope]
        rows = []
        for row in page.rows:
            if page.scope == 'global':
                elo = row.global_elo
                wins, losses = row.global_wins, row.global_losses
            else:
                elo = row.local_elo
                wins, losses = row.local_wins, row.local_losses
            rows.append(
                f'`{row.rank:>3}.` **{_safe(row.name)}**\n'
                f'> `{elo} ELO` · **{wins}W–{losses}L** · '
                f'`{row.total_games} total · {row.recent_games} recent`'
            )
        rankings = '\n'.join(rows) or '*No registered players match these roles.*'
        role_label = ', '.join(_safe(name) for name in page.selected_role_names)
        footer = (
            f'Page {page.page_index + 1} of {page.page_count} · '
            f'showing {page.start_rank or 0}–{page.end_rank or 0} '
            f'of {page.total_matched} matched · {page.loaded_count} loaded'
        )
        if page.truncated:
            footer += ' · result capped at 2,000 loaded players'

        self.sort_select = discord.ui.Select(
            placeholder='Sort by',
            options=[
                discord.SelectOption(
                    label=label,
                    value=value,
                    default=value == page.sort_key,
                )
                for value, label in SORT_LABELS.items()
            ],
        )
        self.sort_select.callback = self._select_sort
        self.scope_select = discord.ui.Select(
            placeholder='ELO and W/L scope',
            options=[
                discord.SelectOption(
                    label=label,
                    value=value,
                    default=value == page.scope,
                )
                for value, label in SCOPE_LABELS.items()
            ],
        )
        self.scope_select.callback = self._select_scope
        free_agents = discord.ui.Button(
            label='Free Agents',
            style=discord.ButtonStyle.secondary,
            disabled=self.free_agent_role_id is None,
        )
        free_agents.callback = self._free_agents
        previous = discord.ui.Button(
            label='Previous',
            emoji='◀️',
            disabled=self.page_index == 0,
        )
        previous.callback = self._previous_page
        jump = discord.ui.Button(
            label=f'Jump to page · {self.page_index + 1}/{self.page_count}',
            style=discord.ButtonStyle.primary,
        )
        jump.callback = self._open_page_modal
        next_page = discord.ui.Button(
            label='Next',
            emoji='▶️',
            disabled=self.page_index == self.page_count - 1,
        )
        next_page.callback = self._next_page

        children = [
            discord.ui.TextDisplay(
                '# 🏆 Role Leaderboard\n'
                f'**Roles:** {role_label or "selected roles"}\n'
                f'**Matching:** {page.match_mode.title()} · '
                f'**Sort:** {SORT_LABELS[page.sort_key]} · '
                f'**Display:** {scope_label}'
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(rankings),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(footer),
            discord.ui.TextDisplay('**Common filters**'),
            discord.ui.ActionRow(self.sort_select),
            discord.ui.ActionRow(self.scope_select),
        ]
        if self.can_select_roles:
            self.match_select = discord.ui.Select(
                placeholder='Match all or any selected roles',
                options=[
                    discord.SelectOption(
                        label='All selected roles',
                        value='all',
                        default=page.match_mode == 'all',
                    ),
                    discord.SelectOption(
                        label='Any selected role',
                        value='any',
                        default=page.match_mode == 'any',
                    ),
                ],
            )
            self.match_select.callback = self._select_match
            self.role_select = discord.ui.RoleSelect(
                placeholder='Choose 1–5 roles',
                min_values=1,
                max_values=role_leaderboard_workers.MAX_SELECTED_ROLES,
            )
            self.role_select.callback = self._select_roles
            children.extend((
                discord.ui.ActionRow(self.match_select),
                discord.ui.ActionRow(self.role_select),
            ))
        children.append(
            discord.ui.ActionRow(free_agents, previous, jump, next_page)
        )
        self.add_item(discord.ui.Container(*children))
