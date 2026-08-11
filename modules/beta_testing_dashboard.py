"""Requester-bound compact dashboard for the development Beta Lab."""

from __future__ import annotations

import discord

from modules import beta_lab_workers, beta_testing_guide


_STATE_ICON = {
    'ready': '✅',
    'refreshable': '🔄',
    'missing': '⚠️',
    'blocked': '⛔',
}


def _safe(value: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(value)))


def overview_markdown(status: beta_lab_workers.BetaLabStatus) -> str:
    lines = [
        '# 🧪 Beta Lab',
        f'**Overall:** {status.overall.title()}',
        '',
    ]
    for pack in status.packs:
        lines.append(
            f'{_STATE_ICON.get(pack.state, "•")} **{_safe(pack.title)}** — '
            f'{_safe(pack.state.title())}'
        )
        lines.append(f'-# {_safe(pack.detail)}')
    snapshot = status.result_snapshot
    if snapshot is not None and snapshot.scenarios:
        lines.extend(('', '**Current result scenarios**'))
        for scenario in snapshot.scenarios:
            lines.append(
                f'- **{scenario.scenario.title()}**: game `{scenario.game_id}` '
                f'({_safe(scenario.status)})'
            )
        if snapshot.participants:
            lines.append(
                '-# Participants: '
                + ', '.join(
                    f'{_safe(item.display_name)} (`{item.user_id}`)'
                    for item in snapshot.participants
                )
            )
    lines.extend((
        '',
        '**Quick release pass**',
        '1. Exercise the three result scenarios above.',
        '2. Open, join, start, show, and delete one disposable game.',
        '3. Check player and Team leaderboard filters/pagination.',
        '4. Open one player, Team, and House workspace.',
        '5. Submit confusing behavior through `/staffhelp`.',
        '',
        '-# Choose a focused area below; the full checklist is navigable '
        'without posting pages into the channel.',
    ))
    return '\n'.join(lines)


class BetaTestingDashboard(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        requester_id: int,
        status: beta_lab_workers.BetaLabStatus,
        guide: beta_testing_guide.ChecklistGuide,
        timeout: float = 600.0,
    ):
        super().__init__(timeout=timeout)
        self.requester_id = int(requester_id)
        self.status = status
        self.guide = guide
        self.section_key: str | None = None
        self.page = 0
        self.message = None
        self.expired = False
        self.rebuild()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) == self.requester_id:
            return True
        await interaction.response.send_message(
            'Open `/whattotest` for your own private Beta Lab dashboard.',
            ephemeral=True,
        )
        return False

    def _section(self):
        return next(
            (
                section for section in self.guide.sections
                if section.key == self.section_key
            ),
            None,
        )

    def _body(self) -> str:
        section = self._section()
        if section is None:
            return overview_markdown(self.status)
        pages = beta_testing_guide.item_pages(section)
        self.page = min(self.page, len(pages) - 1)
        lines = [
            f'# {_safe(section.title)}',
            f'**Page {self.page + 1} of {len(pages)}**',
            '',
        ]
        first_item_number = sum(
            len(page) for page in pages[:self.page]
        ) + 1
        lines.extend(
            f'{index}. {_safe(item)}'
            for index, item in enumerate(
                pages[self.page],
                start=first_item_number,
            )
        )
        lines.append('')
        lines.append('-# Use the menu to switch areas or Overview to return.')
        return '\n'.join(lines)

    def rebuild(self) -> None:
        self.clear_items()
        section = self._section()
        pages = beta_testing_guide.item_pages(section) if section else ((),)
        selector = discord.ui.Select(
            placeholder='Choose a testing area',
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=item.title[:100],
                    value=item.key,
                    default=item.key == self.section_key,
                )
                for item in self.guide.sections[:25]
            ],
            disabled=self.expired,
        )
        selector.callback = self._select
        overview = discord.ui.Button(
            label='Overview',
            style=discord.ButtonStyle.primary,
            disabled=self.expired or section is None,
        )
        overview.callback = self._overview
        previous = discord.ui.Button(
            label='Previous',
            style=discord.ButtonStyle.secondary,
            disabled=self.expired or section is None or self.page <= 0,
        )
        previous.callback = self._previous
        following = discord.ui.Button(
            label='Next',
            style=discord.ButtonStyle.secondary,
            disabled=(
                self.expired
                or section is None
                or self.page >= len(pages) - 1
            ),
        )
        following.callback = self._next
        footer = (
            'This private dashboard expired; rerun `/whattotest`.'
            if self.expired else
            'Private tester workspace • no channel spam'
        )
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(self._body()),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.ActionRow(selector),
            discord.ui.ActionRow(overview, previous, following),
            discord.ui.TextDisplay(f'-# {footer}'),
            accent_colour=discord.Colour.blurple(),
        ))

    async def _select(self, interaction: discord.Interaction) -> None:
        selector = next(
            item for item in self.walk_children()
            if isinstance(item, discord.ui.Select)
        )
        self.section_key = selector.values[0]
        self.page = 0
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _overview(self, interaction: discord.Interaction) -> None:
        self.section_key = None
        self.page = 0
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _previous(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _next(self, interaction: discord.Interaction) -> None:
        self.page += 1
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def on_timeout(self) -> None:
        self.expired = True
        self.rebuild()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
