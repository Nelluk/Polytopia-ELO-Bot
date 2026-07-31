"""Components v2 presentation for immutable game-search snapshots."""

from __future__ import annotations

import discord

from modules import components_v2, game_search_workers


PAGE_SIZE = 6


def _row_text(row: game_search_workers.GameSearchRow) -> str:
    notes = f'\n> *{row.notes[:120]}*' if row.notes else ''
    channel = f' · {row.channel_mention}' if row.channel_mention else ''
    return (
        f'**#{row.game_id} · {row.name}** '
        f'`{row.status.title()}`\n'
        f'> {row.date} · {row.size} · '
        f'{"Ranked" if row.ranked else "Unranked"}'
        f'{(" · " + row.outcome) if row.outcome != "—" else ""}\n'
        f'> {row.roster}{channel}{notes}'
    )


class GameSearchWorkspace(components_v2.CachedRequesterLayoutView):
    """Public game results with requester-only cached filter controls."""

    unauthorized_message = 'Only the requester can control this game search.'

    def __init__(
        self,
        *,
        requester_id: int,
        initial_result: game_search_workers.GameSearchSnapshot,
        loader,
        can_view_unconfirmed: bool = False,
        timeout: float = 300.0,
    ):
        super().__init__(
            requester_id=requester_id,
            initial_key=initial_result.key,
            initial_result=initial_result,
            loader=loader,
            timeout=timeout,
        )
        self.can_view_unconfirmed = can_view_unconfirmed
        self.rebuild()

    @property
    def page_count(self) -> int:
        return components_v2.page_count(self.result.rows, PAGE_SIZE)

    async def _change_filter(
        self,
        interaction: discord.Interaction,
        *,
        status: str | None = None,
        outcome: str | None = None,
        size: str | None = None,
    ) -> None:
        current = self.result.key
        key = game_search_workers.GameSearchKey(
            status=status or current.status,
            outcome=outcome or current.outcome,
            size=size or current.size,
        )
        if not await self.load_key(interaction, key):
            return
        self.page_index = 0
        self.rebuild()
        if interaction.response.is_done():
            await interaction.edit_original_response(view=self)
        else:
            await interaction.response.edit_message(view=self)

    async def _select_status(self, interaction: discord.Interaction) -> None:
        await self._change_filter(
            interaction,
            status=self.status_select.values[0],
        )

    async def _select_outcome(self, interaction: discord.Interaction) -> None:
        await self._change_filter(
            interaction,
            outcome=self.outcome_select.values[0],
        )

    async def _select_size(self, interaction: discord.Interaction) -> None:
        await self._change_filter(
            interaction,
            size=self.size_select.values[0],
        )

    def rebuild(self) -> None:
        self.clear_items()
        self.page_index = min(self.page_index, self.page_count - 1)
        rows, _, _ = components_v2.page_slice(
            self.result.rows,
            self.page_index,
            PAGE_SIZE,
        )
        body = (
            '\n'.join(_row_text(row) for row in rows)
            if rows else '*No games match this view.*'
        )
        key = self.result.key
        status_label = {
            'unfinished': 'Incomplete/open',
        }.get(key.status, key.status.title())
        count = len(self.result.rows)
        query = self.result.query or 'none'
        truncated = ' · first 500 shown' if self.result.truncated else ''
        components = [
            discord.ui.TextDisplay('# 🔎 Game search'),
            discord.ui.TextDisplay(
                f'**Query:** `{query}`\n'
                f'**Resolved:** {self.result.description}\n'
                f'**Filters:** {status_label} · '
                f'{key.outcome.title()} · {key.size}\n'
                f'**Results:** {count}{truncated}'
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(body),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
        ]
        status_options = [
            ('all', 'All statuses'),
            ('open', 'Open'),
            ('active', 'Active'),
            ('completed', 'Completed'),
        ]
        if self.can_view_unconfirmed:
            status_options.append(('unconfirmed', 'Unconfirmed result'))
        self.status_select = discord.ui.Select(
            placeholder='Game status',
            options=[
                discord.SelectOption(
                    label=label,
                    value=value,
                    default=key.status == value,
                )
                for value, label in status_options
            ],
        )
        self.status_select.callback = self._select_status
        self.outcome_select = discord.ui.Select(
            placeholder='Result for first player/team',
            options=[
                discord.SelectOption(
                    label=label,
                    value=value,
                    default=key.outcome == value,
                )
                for value, label in (
                    ('any', 'Any result'),
                    ('win', 'Wins'),
                    ('loss', 'Losses'),
                )
            ],
        )
        self.outcome_select.callback = self._select_outcome
        self.size_select = discord.ui.Select(
            placeholder='Common game size',
            options=[
                discord.SelectOption(
                    label=label,
                    value=value,
                    default=key.size == value,
                )
                for value, label in (
                    ('any', 'Any size'),
                    ('1v1', '1v1'),
                    ('2v2', '2v2'),
                    ('3v3', '3v3'),
                    ('4v4', '4v4'),
                )
            ],
        )
        self.size_select.callback = self._select_size
        components.extend([
            discord.ui.ActionRow(self.status_select),
            discord.ui.ActionRow(self.outcome_select),
            discord.ui.ActionRow(self.size_select),
        ])
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
        components.append(discord.ui.TextDisplay(
            '-# Results are public. Controls expire; rerun `/game search` '
            'for a fresh snapshot.'
        ))
        self.add_item(discord.ui.Container(
            *components,
            accent_colour=components_v2.DEFAULT_ACCENT,
        ))
