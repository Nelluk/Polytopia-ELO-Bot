"""Private Components v2 review for inactive-member removal."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
import math

import discord

from modules import components_v2
from modules import league_inactive_kick as service
from modules import league_inactive_kick_workers as workers


PAGE_SIZE = 10
logger = logging.getLogger('polybot.' + __name__)


def _escape(value: object) -> str:
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value or ''))
    )


async def _private(interaction, content: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True)
    else:
        await interaction.response.send_message(content, ephemeral=True)


class KickConfirmationModal(discord.ui.Modal, title='Confirm member removal'):
    def __init__(self, workspace: 'InactiveKickWorkspace'):
        super().__init__(timeout=300.0)
        self.workspace = workspace
        self._submitted = False
        expected = workspace.result.confirmation_text
        self.confirmation = discord.ui.TextInput(
            label=f'Type {expected}',
            placeholder=expected,
            required=True,
            min_length=len(expected),
            max_length=len(expected),
        )
        self.add_item(self.confirmation)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self._submitted:
            return await _private(interaction, 'This confirmation was already submitted.')
        if not await self.workspace.authorize(interaction):
            return
        if self.workspace.is_finished() or self.workspace.terminal:
            return await _private(interaction, self.workspace.expired_message)
        if self.workspace.confirming:
            return await _private(
                interaction,
                'This inactive-member plan is already being executed.',
            )

        self._submitted = True
        self.workspace.confirming = True
        self.workspace.status = 'Refreshing every candidate before removal…'
        self.workspace.rebuild()
        await interaction.response.defer()
        try:
            outcome = await self.workspace.confirmer(
                interaction,
                self.workspace.result,
                str(self.confirmation.value),
            )
        except workers.InactiveKickError as exc:
            self.workspace.confirming = False
            self.workspace.status = 'Confirmation stopped before any removal.'
            self.workspace.rebuild()
            await interaction.edit_original_response(view=self.workspace)
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception:
            logger.exception('Unexpected inactive-member confirmation failure')
            self.workspace.confirming = False
            self.workspace.status = (
                'Confirmation failed unexpectedly; inspect logs before retrying.'
            )
            self.workspace.rebuild()
            await interaction.edit_original_response(view=self.workspace)
            await interaction.followup.send(
                'The removal workflow failed unexpectedly. No success is '
                'being claimed; inspect logs before retrying.',
                ephemeral=True,
            )
            return

        self.workspace.result = outcome.preview
        self.workspace.confirming = False
        if outcome.state == 'refreshed':
            self.workspace.page_index = 0
            self.workspace.status = (
                'The candidate set changed. Review it and confirm the new count.'
            )
        elif outcome.state == 'retryable':
            self.workspace.status = 'No member was removed; review before retrying.'
        else:
            self.workspace.terminal = True
            if outcome.state == 'complete':
                self.workspace.status = (
                    f'Complete: {outcome.kicked_count} member(s) removed.'
                )
            else:
                self.workspace.status = (
                    f'Reconciliation required after {outcome.kicked_count} '
                    'member removal(s).'
                )
        self.workspace.rebuild()
        await interaction.edit_original_response(view=self.workspace)
        await interaction.followup.send(outcome.private_message, ephemeral=True)
        if self.workspace.terminal:
            self.workspace.stop()


class InactiveKickWorkspace(components_v2.RequesterLayoutView):
    expired_message = (
        'This inactive-member preview expired. Run '
        '`/league maintenance kick-inactive` again.'
    )

    def __init__(
        self,
        *,
        result: workers.InactiveKickPreviewResult,
        requester_id: int,
        confirmer: Callable[
            [discord.Interaction, workers.InactiveKickPreviewResult, str],
            Awaitable[service.InactiveKickConfirmationOutcome],
        ],
        timeout: float = 300.0,
    ):
        super().__init__(requester_id=int(requester_id), timeout=timeout)
        self.result = result
        self.confirmer = confirmer
        self.status = 'Review all candidate and exclusion reasons.'
        self.confirming = False
        self.terminal = False
        self.rebuild()

    @property
    def page_count(self) -> int:
        return max(1, math.ceil(len(self.result.decisions) / PAGE_SIZE))

    def _page_rows(self):
        start = self.page_index * PAGE_SIZE
        return self.result.decisions[start:start + PAGE_SIZE]

    async def _ready(self, interaction) -> bool:
        if not await self.authorize(interaction):
            return False
        if self.is_finished() or self.terminal:
            await _private(interaction, self.expired_message)
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
        self.status = 'Cancelled. No member was removed.'
        self.rebuild()
        await interaction.response.edit_message(view=self)
        self.stop()

    async def _open_confirmation(self, interaction) -> None:
        if not await self._ready(interaction):
            return
        if self.confirming:
            return await _private(interaction, 'This plan is already running.')
        if not self.result.action_candidates:
            return await _private(interaction, 'There are no eligible members to remove.')
        await interaction.response.send_modal(KickConfirmationModal(self))

    def _rows_body(self) -> str:
        rows = self._page_rows()
        if not rows:
            return '*No members currently have the Inactive role.*'
        return '\n'.join(
            f'{"✅" if row.eligible else "🛡️"} '
            f'**{_escape(row.display_name)}** (`{row.member_id}`) — '
            f'{_escape(row.reason)}'
            + (
                f' · joined `{row.joined_days}` days ago'
                if row.joined_days is not None else ''
            )
            for row in rows
        )

    def rebuild(self) -> None:
        self.clear_items()
        self.page_index = min(max(0, self.page_index), self.page_count - 1)
        result = self.result
        summary = (
            '# Remove inactive members\n'
            f'**Eligible:** `{len(result.candidates)}` · '
            f'**Protected/excluded:** `{result.exclusion_count}`\n'
            f'**This run:** `{len(result.action_candidates)}` maximum · '
            f'**Deferred:** `{result.deferred_candidate_count}`\n'
            '**Policy:** unregistered 7+ days, or registered 30+ days with no '
            'tracked game in 60 days; pending/incomplete league games block '
            'removal. Unknown, managed, staff, leadership, bot, and owner '
            'roles/accounts are protected.\n'
            f'**Confirmation:** type `{result.confirmation_text}` exactly.'
        )
        children = [
            discord.ui.TextDisplay(summary),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(self._rows_body()),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(
                f'**Status:** {_escape(self.status)}\n'
                f'-# Page {self.page_index + 1}/{self.page_count} · '
                f'{len(result.decisions)} Inactive member(s) evaluated'
            ),
        ]
        if self.page_count > 1 and not self.terminal:
            previous = discord.ui.Button(
                label='Previous', emoji='◀️',
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
                label='Next', emoji='▶️',
                disabled=self.page_index == self.page_count - 1 or self.confirming,
            )
            next_page.callback = self._next
            children.append(discord.ui.ActionRow(previous, jump, next_page))
        if not self.terminal:
            confirm = discord.ui.Button(
                label='Continue to typed confirmation',
                style=discord.ButtonStyle.danger,
                disabled=self.confirming or not bool(result.action_candidates),
            )
            confirm.callback = self._open_confirmation
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
                discord.Colour.red()
                if not self.terminal else components_v2.DEFAULT_ACCENT
            ),
        ))


async def publish_private(interaction, view: InactiveKickWorkspace):
    message = await interaction.followup.send(
        view=view,
        ephemeral=True,
        wait=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    view.message = message
    return message
