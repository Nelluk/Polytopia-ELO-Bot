"""Private confirmation workspace for guild suspension and resumption."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
from typing import Any

import discord

from modules import components_v2
from modules import operator_guild_command_capabilities as commands
from modules import operator_guild_console_views as console
from modules import operator_guild_lifecycle_workers as workers


logger = logging.getLogger('polybot.' + __name__)

Runner = Callable[
    [Any, workers.GuildLifecyclePreview, commands.GuildCommandCapabilityPlan, str],
    Awaitable[Any],
]


async def _private(interaction: Any, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def _items(values: tuple[str, ...]) -> str:
    return ', '.join(f'`{value}`' for value in values) or '*None*'


def _escape(value: Any) -> str:
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value))
    )


class GuildLifecycleConfirmationModal(discord.ui.Modal):
    def __init__(self, workspace: 'GuildLifecycleWorkspace'):
        self.workspace = workspace
        self.expected = workspace.confirmation
        super().__init__(
            title=(
                'Suspend guild'
                if workspace.preview.action == workers.SUSPEND
                else 'Resume guild'
            ),
            timeout=180.0,
        )
        self.confirmation = discord.ui.TextInput(
            placeholder=self.expected,
            required=True,
            min_length=len(self.expected),
            max_length=len(self.expected),
        )
        self.add_item(discord.ui.Label(
            text='Type the complete confirmation',
            description=(
                'The state transition commits first, then only this guild '
                'command tree is synchronized. Global sync is impossible.'
            ),
            component=self.confirmation,
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if str(self.confirmation.value) != self.expected:
            return await _private(interaction, f'Type `{self.expected}` exactly.')
        await self.workspace.commit(interaction, str(self.confirmation.value))


class GuildLifecycleWorkspace(components_v2.RequesterLayoutView):
    expired_message = (
        'This guild-lifecycle plan expired. Run the command again for fresh '
        'database and Discord evidence.'
    )

    def __init__(
        self,
        *,
        requester_id: int,
        preview: workers.GuildLifecyclePreview,
        command_plan: commands.GuildCommandCapabilityPlan,
        runner: Runner,
        back_runner: console.BackRunner | None = None,
        timeout: float = 600.0,
    ):
        super().__init__(requester_id=int(requester_id), timeout=timeout)
        self.preview = preview
        self.command_plan = command_plan
        self.runner = runner
        self.back_runner = back_runner
        self.busy = False
        self.terminal = False
        self.status = 'Review the exact lifecycle and Discord command-tree evidence.'
        self.rebuild()

    @property
    def page_count(self) -> int:
        return 1

    @property
    def confirmation(self) -> str:
        return self.preview.confirmation(self.command_plan.plan_digest)

    async def _confirm(self, interaction: Any) -> None:
        if not await self.authorize(interaction):
            return
        if self.is_finished() or self.terminal:
            return await _private(interaction, self.expired_message)
        if self.busy:
            return await _private(interaction, 'This lifecycle plan is already running.')
        await interaction.response.send_modal(GuildLifecycleConfirmationModal(self))

    async def _cancel(self, interaction: Any) -> None:
        if not await self.authorize(interaction):
            return
        self.terminal = True
        self.status = 'Cancelled. No lifecycle or command-tree change was made.'
        self.rebuild()
        await interaction.response.edit_message(view=self)
        if self.back_runner is None:
            self.stop()

    async def commit(self, interaction: Any, confirmation: str) -> None:
        if not await self.authorize(interaction):
            return
        if self.is_finished() or self.terminal:
            return await _private(interaction, self.expired_message)
        if self.busy:
            return await _private(interaction, 'This lifecycle plan is already running.')
        self.busy = True
        self.status = 'Revalidating and applying the exact lifecycle plan…'
        self.rebuild()
        await interaction.response.defer()
        await interaction.edit_original_response(view=self)
        try:
            result = await self.runner(
                interaction,
                self.preview,
                self.command_plan,
                confirmation,
            )
        except Exception as exc:
            known = isinstance(
                exc,
                (
                    workers.OperatorGuildLifecycleError,
                    commands.OperatorGuildCommandCapabilityError,
                ),
            )
            if not known:
                logger.exception(
                    'Unexpected guild-lifecycle coordinator failure for guild %s',
                    self.preview.guild_id,
                )
            self.busy = False
            self.terminal = isinstance(
                exc,
                (
                    workers.OperatorGuildLifecycleCommitted,
                    workers.OperatorGuildLifecycleCommandUnverified,
                    commands.OperatorGuildCommandCapabilityCommitted,
                ),
            ) or not known
            self.status = (
                str(exc) if known else
                'The operation stopped without a trustworthy terminal result. '
                'Inspect current state before retrying.'
            )
            self.rebuild()
            try:
                await interaction.edit_original_response(view=self)
            except Exception:
                logger.exception(
                    'Could not update guild-lifecycle failure panel for guild %s',
                    self.preview.guild_id,
                )
            try:
                await interaction.followup.send(self.status, ephemeral=True)
            except Exception:
                logger.exception(
                    'Could not publish guild-lifecycle failure fallback for guild %s',
                    self.preview.guild_id,
                )
            return
        self.busy = False
        self.terminal = True
        transition = getattr(result, 'transition', None)
        if transition is None:
            self.status = (
                f'Guild already `{self.preview.desired_state}`; its exact '
                'command tree was reconciled without a database write.'
            )
        else:
            self.status = (
                f'Guild is now `{transition.enrollment_state}` at generation '
                f'{transition.generation}; runtime policy and exact guild '
                'command tree are converged.'
            )
        self.rebuild()
        try:
            await interaction.edit_original_response(view=self)
        except Exception:
            logger.exception(
                'Could not update successful guild-lifecycle panel for guild %s',
                self.preview.guild_id,
            )
            try:
                await interaction.followup.send(self.status, ephemeral=True)
            except Exception:
                logger.exception(
                    'Could not publish guild-lifecycle success fallback for guild %s',
                    self.preview.guild_id,
                )
        if self.back_runner is None:
            self.stop()

    def rebuild(self) -> None:
        self.clear_items()
        preview = self.preview
        plan = self.command_plan
        transition = (
            f'`{preview.current_state}` → `{preview.desired_state}`; generation '
            f'`{preview.generation}` → `{preview.desired_generation}`'
            if preview.write_required else
            f'Already `{preview.desired_state}`; reconciliation only, no DB write'
        )
        warning = (
            'The bot remains in the guild but all command and listener dispatch '
            'fails closed. Configuration, drafts, revisions, and audits remain.'
            if preview.action == workers.SUSPEND else
            'Resume is allowed only after current role/channel references pass '
            'live validation.'
        )
        confirm = discord.ui.Button(
            label=(
                'Suspend exact guild'
                if preview.action == workers.SUSPEND else 'Resume exact guild'
            ),
            style=discord.ButtonStyle.danger,
            disabled=self.busy or self.terminal,
        )
        confirm.callback = self._confirm
        cancel = discord.ui.Button(
            label='Cancel',
            disabled=self.busy or self.terminal,
        )
        cancel.callback = self._cancel
        controls = [confirm, cancel]
        if self.back_runner is not None:
            controls.append(console.guild_list_back_button(
                self, self.back_runner, disabled=self.busy,
            ))
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(
                f'# {preview.action.title()} guild\n'
                f'**Target:** {_escape(preview.guild_name)} '
                f'(`{preview.guild_id}`)\n'
                f'**Lifecycle:** {transition}\n'
                f'**Active revision:** `{preview.revision}` · '
                f'`{preview.document_digest}`\n'
                f'**Command plan:** `{plan.plan_digest}`\n'
                f'**Confirmation:** `{self.confirmation}`\n\n'
                f'{warning}'
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(
                '## Exact target-guild command diff\n'
                f'**Create:** {_items(plan.creates)}\n'
                f'**Update:** {_items(plan.updates)}\n'
                f'**Remove:** {_items(plan.removals)}\n'
                f'**Unchanged:** {_items(plan.unchanged)}\n\n'
                '-# The remote global tree was read and is empty. No other '
                'guild is synchronized.'
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(f'**Status:** {_escape(self.status)}'),
            discord.ui.ActionRow(*controls),
            accent_colour=discord.Colour.orange(),
        ))


async def publish_private(interaction: Any, view: GuildLifecycleWorkspace):
    await interaction.edit_original_response(view=view)
    return view


__all__ = [
    'GuildLifecycleConfirmationModal',
    'GuildLifecycleWorkspace',
    'publish_private',
]
