"""Private Components v2 confirmation for immutable configuration rollback."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import discord

from modules import components_v2
from modules import operator_guild_console_views as console
from modules import operator_guild_configuration_draft_workers as workers


Runner = Callable[..., Awaitable[workers.GuildConfigurationDraftResult]]


def _escape(value: Any) -> str:
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value or ''))
    )


async def _private(interaction: Any, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


class GuildConfigurationRollbackModal(discord.ui.Modal):
    def __init__(self, workspace: 'GuildConfigurationRollbackWorkspace'):
        self.workspace = workspace
        self.expected = workspace.preview.confirmation
        super().__init__(title='Rollback guild configuration', timeout=180.0)
        self.confirmation = discord.ui.TextInput(
            placeholder=self.expected,
            required=True,
            min_length=len(self.expected),
            max_length=len(self.expected),
        )
        self.add_item(discord.ui.Label(
            text='Type ROLLBACK, revision, and full digest',
            description=(
                'Creates a new immutable revision; history never moves backward.'
            ),
            component=self.confirmation,
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        workspace = self.workspace
        if str(self.confirmation.value) != self.expected:
            return await _private(
                interaction,
                f'Type `{self.expected}` exactly.',
            )
        if not await workspace.ready(interaction):
            return
        await workspace.commit(interaction, str(self.confirmation.value))


class GuildConfigurationRollbackWorkspace(components_v2.RequesterLayoutView):
    expired_message = (
        'This restore preview expired. Reopen **History** from '
        '`/operator guild list`.'
    )

    def __init__(
        self,
        *,
        requester_id: int,
        result: workers.GuildConfigurationDraftResult,
        runner: Runner,
        back_runner: console.BackRunner | None = None,
        timeout: float = 300.0,
    ):
        super().__init__(requester_id=int(requester_id), timeout=timeout)
        if result.rollback_preview is None:
            raise ValueError('A validated rollback preview is required.')
        self.result = result
        self.preview = result.rollback_preview
        self.runner = runner
        self.back_runner = back_runner
        self.busy = False
        self.terminal = False
        self.status = 'Review the exact source revision and changed fields.'
        self.rebuild()

    @property
    def page_count(self) -> int:
        return 1

    async def ready(self, interaction: Any) -> bool:
        if not await self.authorize(interaction):
            return False
        if self.terminal or self.is_finished():
            await _private(interaction, self.expired_message)
            return False
        if self.busy:
            await _private(interaction, 'This rollback is already running.')
            return False
        return True

    async def _confirm(self, interaction: Any) -> None:
        if not await self.ready(interaction):
            return
        await interaction.response.send_modal(
            GuildConfigurationRollbackModal(self)
        )

    async def _cancel(self, interaction: Any) -> None:
        if not await self.ready(interaction):
            return
        self.terminal = True
        self.status = 'Cancelled. Active configuration was unchanged.'
        self.rebuild()
        await interaction.response.edit_message(view=self)
        if self.back_runner is None:
            self.stop()

    async def commit(self, interaction: Any, confirmation_text: str) -> None:
        self.busy = True
        self.status = 'Revalidating and committing immutable rollback…'
        self.rebuild()
        await interaction.response.defer()
        await interaction.edit_original_response(view=self)
        try:
            result = await self.runner(
                interaction,
                workers.ROLLBACK_COMMIT,
                target_revision=self.preview.source_revision,
                expected_target_digest=self.preview.source_document_digest,
                expected_active_revision=self.preview.active_revision,
                expected_active_generation=self.preview.active_generation,
                expected_active_digest=self.preview.active_document_digest,
                confirmation_text=confirmation_text,
            )
        except workers.OperatorGuildConfigurationRollbackCommitted as exc:
            self.busy = False
            self.terminal = True
            self.status = str(exc)
            self.rebuild()
            try:
                await interaction.edit_original_response(view=self)
            except Exception:
                pass
            try:
                await interaction.followup.send(str(exc), ephemeral=True)
            except Exception:
                pass
            if self.back_runner is None:
                self.stop()
            return
        except workers.OperatorGuildConfigurationDraftError as exc:
            self.busy = False
            self.status = str(exc)
            self.rebuild()
            await interaction.edit_original_response(view=self)
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception:
            self.busy = False
            self.status = (
                'Rollback stopped without a trustworthy result. Reopen the '
                'preview and inspect logs before retrying.'
            )
            self.rebuild()
            await interaction.edit_original_response(view=self)
            await interaction.followup.send(self.status, ephemeral=True)
            return
        if result.rollback is None or not result.runtime_published:
            self.busy = False
            self.terminal = bool(result.committed)
            self.status = (
                f'Rollback r{result.active_revision}/g{result.active_generation} '
                'committed, but runtime publication could not be verified. '
                'Use `/operator bot restart` to reconcile.'
                if result.committed else
                'Rollback returned incomplete pre-commit evidence. Reopen the '
                'preview before retrying.'
            )
            self.rebuild()
            try:
                await interaction.edit_original_response(view=self)
            except Exception:
                if self.terminal:
                    try:
                        await interaction.followup.send(
                            self.status,
                            ephemeral=True,
                        )
                    except Exception:
                        pass
            if self.terminal:
                if self.back_runner is None:
                    self.stop()
            return
        self.result = result
        self.busy = False
        self.terminal = True
        self.status = (
            f'Rollback committed as revision {result.rollback.revision}, '
            f'generation {result.rollback.generation}; running settings were '
            'published.'
        )
        self.rebuild()
        try:
            await interaction.edit_original_response(view=self)
        except Exception:
            try:
                await interaction.followup.send(
                    f'Rollback committed as revision {result.rollback.revision}, '
                    f'generation {result.rollback.generation}; running settings '
                    'were published, but the confirmation panel could not be '
                    'updated.',
                    ephemeral=True,
                )
            except Exception:
                pass
        if self.back_runner is None:
            self.stop()

    def rebuild(self) -> None:
        self.clear_items()
        preview = self.preview
        changed = '\n'.join(
            f'- `{_escape(path)}`' for path in preview.changed_paths[:12]
        )
        if len(preview.changed_paths) > 12:
            changed += f'\n- …and {len(preview.changed_paths) - 12} more'
        children: list[Any] = [
            discord.ui.TextDisplay(
                '# Guild configuration rollback\n'
                f'**Current:** `r{preview.active_revision}` / '
                f'`g{preview.active_generation}`\n'
                f'**Restore document from:** `r{preview.source_revision}`\n'
                f'**Source digest:** `{preview.source_document_digest}`\n'
                f'**Changed fields:** `{len(preview.changed_paths)}`\n'
                f'{changed}\n\n'
                '-# Rollback creates a new monotonic revision and audit event. '
                'It never deletes or rewinds history.'
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(f'**Status:** {_escape(self.status)}'),
        ]
        confirm = discord.ui.Button(
            label=f'Rollback to r{preview.source_revision}',
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
        children.append(discord.ui.ActionRow(*controls))
        self.add_item(discord.ui.Container(
            *children,
            accent_colour=components_v2.DEFAULT_ACCENT,
        ))


async def publish_private(
    interaction: Any,
    view: GuildConfigurationRollbackWorkspace,
):
    message = await interaction.followup.send(
        view=view,
        ephemeral=True,
        wait=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    view.message = message
    return message


__all__ = [
    'GuildConfigurationRollbackModal',
    'GuildConfigurationRollbackWorkspace',
    'publish_private',
]
