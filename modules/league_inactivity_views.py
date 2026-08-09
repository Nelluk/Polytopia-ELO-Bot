"""Private Components v2 preview for league inactivity maintenance."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
import math

import discord

from modules import components_v2, league_inactivity, league_inactivity_workers as workers


PAGE_SIZE = 10
logger = logging.getLogger('polybot.' + __name__)


def _escape(value: object) -> str:
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value or ''))
    )


class InactivityPreviewWorkspace(components_v2.RequesterLayoutView):
    """Requester-only preview that revalidates before Discord mutations."""

    expired_message = (
        'This inactivity preview expired. Run '
        '`/league maintenance mark-inactive` again.'
    )

    def __init__(
        self,
        *,
        result: workers.InactivityPreviewResult,
        requester_id: int,
        confirmer: Callable[
            [discord.Interaction, workers.InactivityPreviewResult],
            Awaitable[league_inactivity.InactivityConfirmationOutcome],
        ],
        timeout: float = 300.0,
    ):
        super().__init__(requester_id=int(requester_id), timeout=timeout)
        self.result = result
        self.confirmer = confirmer
        self.status = 'Review this private preview before confirming.'
        self.confirming = False
        self.terminal = False
        self.rebuild()

    @property
    def page_count(self) -> int:
        return max(1, math.ceil(len(self.result.candidates) / PAGE_SIZE))

    def _page_rows(self):
        start = self.page_index * PAGE_SIZE
        return self.result.candidates[start:start + PAGE_SIZE]

    async def _private(self, interaction, content: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)

    async def _ready(self, interaction) -> bool:
        if not await self.authorize(interaction):
            return False
        if self.is_finished() or self.terminal:
            await self._private(interaction, self.expired_message)
            return False
        return True

    async def _previous(self, interaction) -> None:
        if not await self._ready(interaction):
            return
        self.page_index = max(0, self.page_index - 1)
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _next(self, interaction) -> None:
        if not await self._ready(interaction):
            return
        self.page_index = min(self.page_count - 1, self.page_index + 1)
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _jump(self, interaction) -> None:
        if not await self._ready(interaction):
            return
        await self.open_page_modal(interaction)

    async def _cancel(self, interaction) -> None:
        if not await self._ready(interaction):
            return
        self.terminal = True
        self.status = 'Cancelled. No roles were changed.'
        self.rebuild()
        await interaction.response.edit_message(view=self)
        self.stop()

    async def _confirm(self, interaction) -> None:
        if not await self._ready(interaction):
            return
        if self.confirming:
            await self._private(
                interaction,
                'This preview is already being revalidated.',
            )
            return
        if not self.result.candidates:
            await self._private(interaction, 'There are no candidates to mark.')
            return

        self.confirming = True
        self.status = 'Refreshing activity and roles before applying changes…'
        self.rebuild()
        await interaction.response.defer()
        try:
            outcome = await self.confirmer(interaction, self.result)
        except workers.LeagueInactivityError as exc:
            self.confirming = False
            self.status = 'Confirmation failed before any new role was applied.'
            self.rebuild()
            await interaction.edit_original_response(view=self)
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception:
            logger.exception('Unexpected inactivity confirmation failure')
            self.confirming = False
            self.status = 'Confirmation failed unexpectedly before completion.'
            self.rebuild()
            await interaction.edit_original_response(view=self)
            await interaction.followup.send(
                'Inactivity maintenance failed unexpectedly. No successful '
                'role change is being claimed; staff should inspect the log '
                'before retrying.',
                ephemeral=True,
            )
            return

        self.result = outcome.preview
        self.confirming = False
        if outcome.state == 'refreshed':
            self.page_index = 0
            self.status = 'Candidate list changed. Review and confirm again.'
        elif outcome.state == 'retryable':
            self.status = 'No role was applied. The preview remains available.'
        else:
            self.terminal = True
            if outcome.state == 'applied':
                self.status = (
                    f'Complete: {outcome.succeeded_count} role change(s) '
                    'succeeded and the public summary was posted.'
                )
            else:
                self.status = (
                    f'Reconciliation required: {outcome.succeeded_count} '
                    'role change(s) succeeded but public reporting failed.'
                )
        self.rebuild()
        await interaction.edit_original_response(view=self)
        await interaction.followup.send(
            outcome.private_message,
            ephemeral=True,
        )
        if self.terminal:
            self.stop()

    def _candidate_body(self) -> str:
        rows = self._page_rows()
        if not rows:
            return '*No members currently qualify.*'
        return '\n'.join(
            f'**{_escape(row.display_name)}** (`{row.member_id}`) — joined '
            f'`{row.joined_days}` days ago'
            for row in rows
        )

    def rebuild(self) -> None:
        self.clear_items()
        self.page_index = min(max(0, self.page_index), self.page_count - 1)
        result = self.result
        warning = ''
        if result.missing_protected_role_names:
            warning = (
                '\n⚠️ Missing configured protected role(s): '
                + ', '.join(
                    f'`{_escape(name)}`'
                    for name in result.missing_protected_role_names
                )
            )
        deferred = result.deferred_candidate_count
        summary = (
            f'# Mark inactive members\n'
            f'**Candidate role:** `{_escape(result.inactive_role_name)}`\n'
            f'**Candidates:** `{len(result.candidates)}` '
            f'(up to `{workers.MAX_ACTION_CANDIDATES}` this run; '
            f'`{deferred}` deferred)\n'
            f'**Excluded:** active `{result.active_count}` · recent join '
            f'`{result.recent_join_count}` · already inactive '
            f'`{result.already_inactive_count}` · protected '
            f'`{result.protected_count}` · bot/owner/unknown join '
            f'`{result.omitted_count}`\n'
            f'**Policy:** no game started in this guild for 60 days and no '
            f'incomplete game.{warning}'
        )
        children = [
            discord.ui.TextDisplay(summary),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(self._candidate_body()),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(
                f'**Status:** {_escape(self.status)}\n'
                f'-# Page {self.page_index + 1}/{self.page_count} · '
                f'{result.total_member_count} guild member(s) captured'
            ),
        ]

        if self.page_count > 1 and not self.terminal:
            previous = discord.ui.Button(
                label='Previous',
                emoji='◀️',
                disabled=self.page_index == 0 or self.confirming,
            )
            previous.callback = self._previous
            jump = discord.ui.Button(
                label=f'Page {self.page_index + 1}/{self.page_count}',
                style=discord.ButtonStyle.primary,
                disabled=self.confirming,
            )
            jump.callback = self._jump
            next_page = discord.ui.Button(
                label='Next',
                emoji='▶️',
                disabled=(
                    self.page_index == self.page_count - 1 or self.confirming
                ),
            )
            next_page.callback = self._next
            children.append(discord.ui.ActionRow(previous, jump, next_page))

        if not self.terminal:
            confirm = discord.ui.Button(
                label='Confirm refreshed plan',
                style=discord.ButtonStyle.danger,
                disabled=self.confirming or not bool(result.candidates),
            )
            confirm.callback = self._confirm
            cancel = discord.ui.Button(
                label='Cancel',
                style=discord.ButtonStyle.secondary,
                disabled=self.confirming,
            )
            cancel.callback = self._cancel
            children.append(discord.ui.ActionRow(confirm, cancel))

        self.add_item(discord.ui.Container(
            *children,
            accent_colour=(
                discord.Colour.orange()
                if not self.terminal
                else components_v2.DEFAULT_ACCENT
            ),
        ))


async def publish_private(
    interaction: discord.Interaction,
    view: InactivityPreviewWorkspace,
):
    message = await interaction.followup.send(
        view=view,
        ephemeral=True,
        wait=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    view.message = message
    return message
