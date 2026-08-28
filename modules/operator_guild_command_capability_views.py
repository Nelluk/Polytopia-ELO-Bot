"""Private confirmation panel for one type-derived guild command sync."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
from typing import Any

import discord

from modules import components_v2
from modules import operator_guild_command_capabilities as service


logger = logging.getLogger('polybot.' + __name__)

Runner = Callable[
    [Any, service.GuildCommandCapabilityPlan, str],
    Awaitable[service.GuildCommandCapabilityCompletion],
]


def _escape(value: Any) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(value)))


def _items(values: tuple[str, ...]) -> str:
    return ', '.join(f'`{_escape(value)}`' for value in values) or '*None*'


async def _private(interaction: Any, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


class GuildCommandCapabilityConfirmationModal(discord.ui.Modal):
    def __init__(self, workspace: 'GuildCommandCapabilityWorkspace'):
        self.workspace = workspace
        self.expected = workspace.plan.confirmation
        title = (
            'Activate and synchronize commands'
            if workspace.plan.mode == service.ACTIVATE
            else 'Synchronize guild commands'
        )
        super().__init__(title=title[:45], timeout=180.0)
        self.confirmation = discord.ui.TextInput(
            placeholder=self.expected,
            required=True,
            min_length=len(self.expected),
            max_length=len(self.expected),
        )
        self.add_item(discord.ui.Label(
            text='Type the complete confirmation',
            description=(
                'This may commit one configuration revision, then synchronizes '
                'only the displayed guild. It never synchronizes globally.'
            ),
            component=self.confirmation,
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if str(self.confirmation.value) != self.expected:
            return await _private(interaction, f'Type `{self.expected}` exactly.')
        await self.workspace.commit(interaction, str(self.confirmation.value))


class GuildCommandCapabilityWorkspace(components_v2.RequesterLayoutView):
    expired_message = (
        'This server-command plan expired. Run `/operator guild sync` '
        'again for fresh database and Discord evidence.'
    )

    def __init__(
        self,
        *,
        requester_id: int,
        guild_name: str,
        plan: service.GuildCommandCapabilityPlan,
        runner: Runner,
        timeout: float = 600.0,
    ):
        super().__init__(requester_id=int(requester_id), timeout=timeout)
        self.guild_name = str(guild_name)
        self.plan = plan
        self.runner = runner
        self.busy = False
        self.terminal = False
        self.status = (
            'Review both immutable configuration and live Discord command '
            'evidence before confirming.'
        )
        self.rebuild()

    @property
    def page_count(self) -> int:
        return 1

    async def _confirm(self, interaction: Any) -> None:
        if not await self.authorize(interaction):
            return
        if self.is_finished() or self.terminal:
            return await _private(interaction, self.expired_message)
        if self.busy:
            return await _private(interaction, 'This plan is already running.')
        await interaction.response.send_modal(
            GuildCommandCapabilityConfirmationModal(self)
        )

    async def commit(self, interaction: Any, confirmation: str) -> None:
        if not await self.authorize(interaction):
            return
        if self.is_finished() or self.terminal:
            return await _private(interaction, self.expired_message)
        if self.busy:
            return await _private(interaction, 'This plan is already running.')
        self.busy = True
        self.status = (
            'Revalidating the database and remote tree; applying the exact '
            'guild-only plan…'
        )
        self.rebuild()
        await interaction.response.defer()
        await interaction.edit_original_response(view=self)
        try:
            result = await self.runner(interaction, self.plan, confirmation)
        except service.OperatorGuildCommandCapabilityError as exc:
            self.busy = False
            self.terminal = isinstance(
                exc, service.OperatorGuildCommandCapabilityCommitted
            )
            self.status = str(exc)
            self.rebuild()
            try:
                await interaction.edit_original_response(view=self)
            except Exception:
                logger.exception(
                    'Could not update command-capability failure panel for guild %s',
                    self.plan.guild_id,
                )
            try:
                await interaction.followup.send(str(exc), ephemeral=True)
            except Exception:
                logger.exception(
                    'Could not publish command-capability failure fallback for guild %s',
                    self.plan.guild_id,
                )
            return
        except Exception:
            logger.exception(
                'Unexpected command-capability coordinator failure for guild %s',
                self.plan.guild_id,
            )
            self.busy = False
            self.terminal = True
            self.status = (
                'The operation stopped without a trustworthy terminal result. '
                'Do not repeat a database change; reopen `/operator guild sync` '
                'to inspect current truth.'
            )
            self.rebuild()
            try:
                await interaction.edit_original_response(view=self)
            except Exception:
                logger.exception(
                    'Could not update uncertain command-capability panel for guild %s',
                    self.plan.guild_id,
                )
            try:
                await interaction.followup.send(self.status, ephemeral=True)
            except Exception:
                logger.exception(
                    'Could not publish uncertain command-capability fallback for guild %s',
                    self.plan.guild_id,
                )
            return
        self.busy = False
        self.terminal = True
        if result.committed_revision is None:
            self.status = (
                f'Guild command tree reconciled with {len(result.apply.roots)} '
                'registered roots; no database revision was written.'
            )
        else:
            self.status = (
                f'Activated revision {result.committed_revision}, generation '
                f'{result.committed_generation}; the fail-closed runtime policy '
                'and exact guild command tree are converged.'
            )
        self.rebuild()
        try:
            await interaction.edit_original_response(view=self)
        except Exception:
            logger.exception(
                'Could not update successful command-capability panel for guild %s',
                self.plan.guild_id,
            )
            await interaction.followup.send(self.status, ephemeral=True)

    def rebuild(self) -> None:
        self.clear_items()
        plan = self.plan
        mode = (
            'Activate draft and synchronize'
            if plan.mode == service.ACTIVATE
            else 'Reconcile active policy only'
        )
        children: list[Any] = [
            discord.ui.TextDisplay(
                '# Server command-sync plan\n'
                f'**Target:** {_escape(self.guild_name)} (`{plan.guild_id}`)\n'
                f'**Mode:** {mode}\n'
                f'**Active evidence:** `r{plan.active_revision}/g'
                f'{plan.active_generation}` · `{plan.active_document_digest}`\n'
                + (
                    f'**Draft:** version `{plan.draft_version}` · '
                    f'`{plan.draft_document_digest}`\n'
                    if plan.mode == service.ACTIVATE else ''
                )
                + f'**Plan digest:** `{plan.plan_digest}`\n'
                + f'**Confirmation:** `{plan.confirmation}`'
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(
                '## Command changes\n'
                f'**Create:** {_items(plan.creates)}\n'
                f'**Update:** {_items(plan.updates)}\n'
                f'**Remove:** {_items(plan.removals)}\n'
                f'**Unchanged:** {_items(plan.unchanged)}\n\n'
                '-# The remote global tree was read and is empty. Apply is '
                'hard-coded to this one guild.'
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(f'**Status:** {_escape(self.status)}'),
        ]
        confirm = discord.ui.Button(
            label=(
                'Activate + sync exact guild'
                if plan.mode == service.ACTIVATE
                else 'Sync exact guild'
            ),
            style=discord.ButtonStyle.danger,
            disabled=self.busy or self.terminal,
        )
        confirm.callback = self._confirm
        children.append(discord.ui.ActionRow(confirm))
        self.add_item(discord.ui.Container(
            *children,
            accent_colour=components_v2.DEFAULT_ACCENT,
        ))


async def publish_private(interaction: Any, view: GuildCommandCapabilityWorkspace) -> None:
    if not interaction.response.is_done():
        await interaction.response.send_message(view=view, ephemeral=True)
    else:
        await interaction.edit_original_response(view=view)


__all__ = [
    'GuildCommandCapabilityConfirmationModal',
    'GuildCommandCapabilityWorkspace',
    'publish_private',
]
