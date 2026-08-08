"""Requester-bound Components v2 workspace for game audit logs."""

from __future__ import annotations

import discord

from modules import components_v2, game_logs, game_log_workers


PAGE_SIZE = 10


class GameLogSearchModal(discord.ui.Modal, title='Search audit logs'):
    include = discord.ui.Label(
        text='Required terms',
        description='Space-separated terms; every term must match.',
        component=discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=game_logs.MAX_SEARCH_LENGTH,
            placeholder='player name join',
        ),
    )
    exclude = discord.ui.Label(
        text='Exclude one term',
        description='Entries containing this term are omitted.',
        component=discord.ui.TextInput(
            required=False,
            max_length=game_log_workers.MAX_QUERY_TERM_LENGTH,
            placeholder='leave',
        ),
    )

    def __init__(self, view: 'GameLogsWorkspace'):
        super().__init__(timeout=120.0)
        self.target_view = view
        self.include.component.default = ' '.join(view.result.key.include_terms)
        self.exclude.component.default = view.result.key.exclude_term

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
            include, embedded_exclude = game_logs.parse_search_terms(
                str(self.include.component.value or '')
            )
            explicit_exclude = str(self.exclude.component.value or '').strip()
            if embedded_exclude and explicit_exclude:
                raise game_log_workers.GameLogReadError(
                    'Use the Exclude field rather than two excluded terms.'
                )
            key = game_log_workers.GameLogKey(
                scope=view.result.key.scope,
                game_id=view.result.key.game_id,
                include_terms=include,
                exclude_term=explicit_exclude or embedded_exclude,
            )
        except game_log_workers.GameLogReadError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if not await view.load_key(interaction, key):
            return
        view.page_index = 0
        view.rebuild()
        await interaction.edit_original_response(view=view)


class GameLogsWorkspace(components_v2.CachedRequesterLayoutView):
    """Public immutable audit results with requester-only refinements."""

    unauthorized_message = 'Only the requester can control this log view.'

    def __init__(
        self,
        *,
        requester_id: int,
        initial_result: game_log_workers.GameLogSnapshot,
        loader,
        requester_is_staff: bool,
        requester_is_owner: bool,
        initial_game_id: int | None,
        timeout: float = 300.0,
    ):
        self.requester_is_staff = bool(requester_is_staff)
        self.requester_is_owner = bool(requester_is_owner)
        self.initial_game_id = initial_game_id
        super().__init__(
            requester_id=requester_id,
            initial_key=initial_result.key,
            initial_result=initial_result,
            loader=loader,
            timeout=timeout,
        )
        self.rebuild()

    @property
    def page_count(self) -> int:
        return components_v2.page_count(self.result.rows, PAGE_SIZE)

    def _scope_options(self):
        options = []
        if self.initial_game_id is not None:
            options.append(('game', f'Game {self.initial_game_id}'))
        if self.requester_is_staff or self.requester_is_owner:
            options.append(('guild', 'This server'))
        if self.requester_is_owner:
            options.append(('global', 'All bot servers'))
        return options

    async def _change_scope(self, interaction: discord.Interaction) -> None:
        scope = self.scope_select.values[0]
        key = game_log_workers.GameLogKey(
            scope=scope,
            game_id=self.initial_game_id if scope == 'game' else None,
            include_terms=self.result.key.include_terms,
            exclude_term=self.result.key.exclude_term,
        )
        if not await self.load_key(interaction, key):
            return
        self.page_index = 0
        self.rebuild()
        await interaction.edit_original_response(view=self)

    async def _open_search(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(GameLogSearchModal(self))

    async def _reset_search(self, interaction: discord.Interaction) -> None:
        key = game_log_workers.GameLogKey(
            scope=self.result.key.scope,
            game_id=self.result.key.game_id,
        )
        if not await self.load_key(interaction, key):
            return
        self.page_index = 0
        self.rebuild()
        await interaction.edit_original_response(view=self)

    def rebuild(self) -> None:
        self.clear_items()
        self.page_index = min(self.page_index, self.page_count - 1)
        rows, start, end = components_v2.page_slice(
            self.result.rows,
            self.page_index,
            PAGE_SIZE,
        )
        scope_label = {
            'game': f'Game {self.result.key.game_id}',
            'guild': 'This server',
            'global': 'All bot servers',
        }[self.result.key.scope]
        count = len(self.result.rows)
        truncation = (
            f' · first {game_log_workers.MAX_LOG_ROWS} shown'
            if self.result.truncated else ''
        )
        components = [
            discord.ui.TextDisplay('# 📜 Game audit logs'),
            discord.ui.TextDisplay(
                f'**Scope:** {scope_label}\n'
                f'{game_logs.filter_summary(self.result.key)}\n'
                f'**Results:** {count}{truncation} · showing {start}–{end}'
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
        ]
        if not rows:
            components.append(discord.ui.TextDisplay('*No audit entries match this view.*'))
        else:
            for row in rows:
                guild = (
                    f' · guild `{row.guild_id}`'
                    if self.result.key.scope == 'global' else ''
                )
                clipped = '\n-# Entry text was truncated in this view.' if row.message_truncated else ''
                components.append(discord.ui.TextDisplay(
                    f'**`{row.timestamp}`**{guild}\n'
                    f'{game_logs.safe_log_text(row.message)}{clipped}'
                ))

        scope_options = self._scope_options()
        if len(scope_options) > 1:
            self.scope_select = discord.ui.Select(
                placeholder='Audit scope',
                options=[
                    discord.SelectOption(
                        label=label,
                        value=value,
                        default=self.result.key.scope == value,
                    )
                    for value, label in scope_options
                ],
            )
            self.scope_select.callback = self._change_scope
            components.append(discord.ui.ActionRow(self.scope_select))

        search = discord.ui.Button(label='Search', emoji='🔎')
        search.callback = self._open_search
        reset = discord.ui.Button(
            label='Clear search',
            disabled=(
                not self.result.key.include_terms
                and not self.result.key.exclude_term
            ),
        )
        reset.callback = self._reset_search
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
        components.extend([
            discord.ui.ActionRow(search, reset),
            discord.ui.ActionRow(previous, page, next_page),
            discord.ui.TextDisplay(
                '-# Results are public; controls belong to the requester and '
                'expire after five minutes. Protected entries are never shown.'
            ),
        ])
        self.add_item(discord.ui.Container(
            *components,
            accent_colour=components_v2.DEFAULT_ACCENT,
        ))
