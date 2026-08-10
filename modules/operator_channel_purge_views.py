"""Private exact-selection workspace for manual channel cleanup."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import math

import discord

from modules import components_v2
from modules import operator_channel_purge as service
from modules import operator_channel_purge_workers as workers


PAGE_SIZE = 10


def _escape(value) -> str:
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value or ''))
    )


async def _private(interaction, message):
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


class PurgeConfirmationModal(discord.ui.Modal, title='Confirm channel purge'):
    def __init__(self, workspace: 'ManualChannelPurgeWorkspace'):
        super().__init__(timeout=180.0)
        self.workspace = workspace
        expected = f'PURGE {len(workspace.selected_keys)}'
        self.confirmation = discord.ui.TextInput(
            label=f'Type {expected}',
            placeholder=expected,
            required=True,
            min_length=len(expected),
            max_length=len(expected),
        )
        self.add_item(self.confirmation)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        workspace = self.workspace
        if not await workspace.authorize(interaction):
            return
        if workspace.terminal or workspace.is_finished():
            return await _private(interaction, workspace.expired_message)
        if workspace.busy:
            return await _private(interaction, 'This purge is already running.')

        workspace.busy = True
        workspace.status = 'Refreshing selected channels before deletion…'
        workspace.rebuild()
        await interaction.response.defer()
        await interaction.edit_original_response(view=workspace)
        try:
            outcome = await workspace.confirmer(
                interaction,
                workspace.preview,
                tuple(sorted(workspace.selected_keys)),
                str(self.confirmation.value),
            )
        except workers.ManualChannelPurgeError as exc:
            workspace.busy = False
            workspace.status = str(exc)
            workspace.rebuild()
            await interaction.edit_original_response(view=workspace)
            return await interaction.followup.send(str(exc), ephemeral=True)
        except Exception:
            workspace.busy = False
            workspace.status = (
                'The purge stopped without a trustworthy result. Inspect '
                'logs and rerun preview before retrying.'
            )
            workspace.rebuild()
            await interaction.edit_original_response(view=workspace)
            return await interaction.followup.send(
                workspace.status, ephemeral=True
            )

        workspace.preview = outcome.preview
        workspace.selected_keys = set(outcome.selected_keys)
        workspace.busy = False
        if outcome.state == 'refreshed':
            workspace.page_index = 0
            workspace.status = (
                'Candidate state changed. Review and confirm the refreshed set.'
            )
        else:
            workspace.terminal = True
            workspace.status = (
                'Purge complete.'
                if outcome.state == 'complete' else
                'Purge finished with failures or reconciliation required.'
            )
        workspace.rebuild()
        await interaction.edit_original_response(view=workspace)
        await interaction.followup.send(
            outcome.private_message,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        if workspace.terminal:
            workspace.stop()


class ManualChannelPurgeWorkspace(components_v2.RequesterLayoutView):
    expired_message = (
        'This channel-purge preview expired. Run '
        '`/operator channels purge` again.'
    )

    def __init__(
        self,
        *,
        requester_id: int,
        preview: workers.ManualPurgePreview,
        refresher: Callable[
            [discord.Interaction, str],
            Awaitable[workers.ManualPurgePreview],
        ],
        confirmer: Callable[..., Awaitable[service.ManualPurgeOutcome]],
        timeout: float = 300.0,
    ):
        super().__init__(requester_id=int(requester_id), timeout=timeout)
        self.preview = preview
        self.refresher = refresher
        self.confirmer = confirmer
        self.selected_keys: set[str] = set()
        self.busy = False
        self.terminal = False
        self.status = 'Select exact channels, then review the typed confirmation.'
        self.rebuild()

    @property
    def page_count(self):
        return max(1, math.ceil(len(self.preview.candidates) / PAGE_SIZE))

    def _page_candidates(self):
        start = self.page_index * PAGE_SIZE
        return self.preview.candidates[start:start + PAGE_SIZE]

    async def _ready(self, interaction):
        if not await self.authorize(interaction):
            return False
        if self.terminal or self.is_finished():
            await _private(interaction, self.expired_message)
            return False
        if self.busy:
            await _private(interaction, 'This preview is currently refreshing or running.')
            return False
        return True

    async def _select(self, interaction, select):
        if not await self._ready(interaction):
            return
        page_keys = {row.key for row in self._page_candidates()}
        updated = (self.selected_keys - page_keys) | set(select.values)
        if len(updated) > workers.MAX_SELECTED_CHANNELS:
            return await _private(
                interaction,
                f'Select at most {workers.MAX_SELECTED_CHANNELS} channels.',
            )
        self.selected_keys = updated
        self.status = f'{len(self.selected_keys)} channel(s) selected.'
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _previous(self, interaction):
        if not await self._ready(interaction):
            return
        self.page_index = max(0, self.page_index - 1)
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _next(self, interaction):
        if not await self._ready(interaction):
            return
        self.page_index = min(self.page_count - 1, self.page_index + 1)
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _refresh(self, interaction):
        if not await self._ready(interaction):
            return
        self.busy = True
        self.status = 'Refreshing candidate and protection state…'
        self.rebuild()
        await interaction.response.defer()
        await interaction.edit_original_response(view=self)
        try:
            preview = await self.refresher(interaction, self.preview.mode)
        except Exception as exc:
            self.busy = False
            self.status = f'Refresh failed: {exc}'
            self.rebuild()
            await interaction.edit_original_response(view=self)
            return
        valid_keys = {row.key for row in preview.candidates}
        self.selected_keys.intersection_update(valid_keys)
        self.preview = preview
        self.page_index = min(self.page_index, self.page_count - 1)
        self.busy = False
        self.status = (
            f'Refreshed. {len(self.selected_keys)} prior selection(s) remain.'
        )
        self.rebuild()
        await interaction.edit_original_response(view=self)

    async def _confirm(self, interaction):
        if not await self._ready(interaction):
            return
        if not self.selected_keys:
            return await _private(interaction, 'Select at least one exact channel.')
        await interaction.response.send_modal(PurgeConfirmationModal(self))

    async def _cancel(self, interaction):
        if not await self._ready(interaction):
            return
        self.terminal = True
        self.status = 'Cancelled. No channel was deleted.'
        self.rebuild()
        await interaction.response.edit_message(view=self)
        self.stop()

    def _candidate_line(self, row):
        activity = (
            f'<t:{int(row.last_activity_at.timestamp())}:R>'
            if row.last_activity_at else 'no messages'
        )
        target = (
            f'Game `{row.game_id}` {row.kind}'
            if row.game_id else 'untracked/orphan'
        )
        selected = '☑️' if row.key in self.selected_keys else '⬜'
        return (
            f'{selected} **#{_escape(row.channel_name)}** '
            f'(`{row.channel_id}`) — {target}; {activity}\n'
            f'-# {_escape(row.reason)}'
        )

    def rebuild(self):
        self.clear_items()
        self.page_index = min(max(0, self.page_index), self.page_count - 1)
        candidates = self._page_candidates()
        mode_notes = {
            workers.STALE: 'Tracked channel with no activity for 30 days.',
            workers.CAPACITY: (
                f'Central tracked channel while guild count exceeds '
                f'{workers.CAPACITY_THRESHOLD}.'
            ),
            workers.ORPHAN: (
                'Configured game-category channel with no database reference; '
                'orphans are never selected automatically.'
            ),
            workers.MISSING: (
                'Tracked database reference whose Discord channel is absent.'
            ),
        }
        lines = '\n'.join(self._candidate_line(row) for row in candidates)
        if not lines:
            lines = '*No eligible channels in this mode.*'
        children = [
            discord.ui.TextDisplay(
                '# Manual game-channel purge\n'
                f'**Mode:** `{self.preview.mode}` — '
                f'{mode_notes[self.preview.mode]}\n'
                f'**Guild channels:** `{self.preview.guild_channel_count}` · '
                f'**Eligible:** `{len(self.preview.candidates)}` · '
                f'**Excluded/protected:** `{len(self.preview.exclusions)}` · '
                f'**Selected:** `{len(self.selected_keys)}`/'
                f'`{workers.MAX_SELECTED_CHANNELS}`\n'
                '**Safety:** every selected target is refreshed before any '
                'deletion; changed targets stop the run.'
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(lines),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(
                f'**Status:** {_escape(self.status)}\n'
                f'-# Page {self.page_index + 1}/{self.page_count} · '
                f'preview `{self.preview.fingerprint[:12]}`'
            ),
        ]
        if candidates and not self.terminal:
            options = [
                discord.SelectOption(
                    label=f'#{row.channel_name}'[:100],
                    value=row.key,
                    description=(
                        f'{row.channel_id} · {row.reason}'[:100]
                    ),
                    default=row.key in self.selected_keys,
                )
                for row in candidates
            ]
            select = discord.ui.Select(
                placeholder='Select exact channels on this page',
                min_values=0,
                max_values=len(options),
                options=options,
                disabled=self.busy,
            )
            select.callback = lambda interaction: self._select(interaction, select)
            children.append(discord.ui.ActionRow(select))
        if self.page_count > 1 and not self.terminal:
            previous = discord.ui.Button(
                label='Previous',
                disabled=self.busy or self.page_index == 0,
            )
            previous.callback = self._previous
            next_page = discord.ui.Button(
                label='Next',
                disabled=self.busy or self.page_index == self.page_count - 1,
            )
            next_page.callback = self._next
            children.append(discord.ui.ActionRow(previous, next_page))
        if not self.terminal:
            refresh = discord.ui.Button(
                label='Refresh',
                style=discord.ButtonStyle.secondary,
                disabled=self.busy,
            )
            refresh.callback = self._refresh
            confirm = discord.ui.Button(
                label='Review deletion',
                style=discord.ButtonStyle.danger,
                disabled=self.busy or not self.selected_keys,
            )
            confirm.callback = self._confirm
            cancel = discord.ui.Button(
                label='Cancel',
                style=discord.ButtonStyle.secondary,
                disabled=self.busy,
            )
            cancel.callback = self._cancel
            children.append(discord.ui.ActionRow(refresh, confirm, cancel))
        self.add_item(discord.ui.Container(
            *children,
            accent_colour=(
                discord.Colour.red()
                if not self.terminal else components_v2.DEFAULT_ACCENT
            ),
        ))


async def publish_private(interaction, view):
    message = await interaction.followup.send(
        view=view,
        ephemeral=True,
        wait=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    view.message = message
    return message
