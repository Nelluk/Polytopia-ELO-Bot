"""Requester-bound private confirmation for supervised bot restarts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging

import discord

from modules import operator_restart as service


logger = logging.getLogger('polybot.' + __name__)


async def _private(interaction: discord.Interaction, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


class ForceRestartModal(discord.ui.Modal, title='Force bot restart'):
    def __init__(self, view: 'RestartConfirmationView'):
        super().__init__(timeout=180.0)
        self.view = view
        self.confirmation = discord.ui.TextInput(
            label=f'Type {service.FORCE_CONFIRMATION}',
            placeholder=service.FORCE_CONFIRMATION,
            required=True,
            min_length=len(service.FORCE_CONFIRMATION),
            max_length=len(service.FORCE_CONFIRMATION),
        )
        self.add_item(self.confirmation)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self.view.authorize(interaction):
            return
        await self.view.run_restart(
            interaction,
            confirmation_text=str(self.confirmation.value),
        )


class RestartConfirmationView(discord.ui.LayoutView):
    expired_message = (
        'This restart confirmation expired. Run `/operator bot restart` again.'
    )

    def __init__(
        self,
        *,
        preview: service.RestartPreview,
        runner: Callable[[discord.Interaction, str | None], Awaitable[None]],
        timeout: float = 180.0,
    ):
        super().__init__(timeout=timeout)
        self.preview = preview
        self.requester_id = int(preview.requester_id)
        self.runner = runner
        self.message = None
        self.busy = False
        self.terminal = False
        self.status = (
            'Review the checkpoint and confirm the supervised restart.'
        )
        self.rebuild()

    async def authorize(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) == self.requester_id:
            return True
        await _private(
            interaction,
            'Only the operator who opened this restart confirmation can use it.',
        )
        return False

    async def _edit(self, interaction: discord.Interaction | None = None) -> None:
        if interaction is not None:
            try:
                await interaction.edit_original_response(view=self)
                return
            except discord.HTTPException:
                pass
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def run_restart(
        self,
        interaction: discord.Interaction,
        *,
        confirmation_text: str | None,
    ) -> None:
        if self.terminal or self.is_finished():
            return await _private(interaction, self.expired_message)
        if self.busy:
            return await _private(interaction, 'This restart is already running.')
        self.busy = True
        self.status = 'Final safety checks are running…'
        self.rebuild()
        await interaction.response.defer()
        await self._edit(interaction)
        try:
            await self.runner(interaction, confirmation_text)
        except service.RestartError as exc:
            self.busy = False
            self.status = str(exc)
            self.rebuild()
            await self._edit(interaction)
            return await interaction.followup.send(str(exc), ephemeral=True)
        except Exception:
            logger.exception('Unexpected supervised restart failure')
            self.busy = False
            self.status = (
                'Restart did not reach a trustworthy shutdown request. The '
                'failure was logged; inspect bot and service state before retrying.'
            )
            self.rebuild()
            await self._edit(interaction)
            return await interaction.followup.send(self.status, ephemeral=True)

        self.mark_accepted()

    def mark_accepted(self) -> None:
        self.terminal = True
        self.busy = False
        self.status = (
            'Restart accepted. The supervisor will start the reviewed checkout.'
        )
        self.rebuild()
        self.stop()

    async def _confirm(self, interaction: discord.Interaction) -> None:
        if not await self.authorize(interaction):
            return
        if self.preview.force:
            return await interaction.response.send_modal(ForceRestartModal(self))
        await self.run_restart(interaction, confirmation_text=None)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        if not await self.authorize(interaction):
            return
        self.terminal = True
        self.status = 'Restart cancelled. The bot is still running.'
        self.rebuild()
        self.stop()
        await interaction.response.edit_message(view=self)

    def rebuild(self) -> None:
        self.clear_items()
        preview = self.preview
        active = (
            '\n'.join(f'- {value}' for value in preview.activity.descriptions)
            if preview.activity.descriptions else '- None detected'
        )
        mode = 'OWNER FORCE' if preview.force else 'normal'
        warning = (
            '\n**Warning:** force mode will not wait for the active work listed '
            'above.'
            if preview.force and preview.activity.busy else ''
        )
        confirm = discord.ui.Button(
            label='Force restart' if preview.force else 'Restart bot',
            style=discord.ButtonStyle.danger,
            disabled=self.busy or self.terminal,
        )
        confirm.callback = self._confirm
        cancel = discord.ui.Button(
            label='Cancel',
            style=discord.ButtonStyle.secondary,
            disabled=self.busy or self.terminal,
        )
        cancel.callback = self._cancel
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(
                '# Supervised bot restart\n'
                f'**Mode:** `{mode}`\n'
                f'**Running checkpoint:** '
                f'`{preview.checkout.running_checkpoint}`\n'
                f'**Checkpoint to load:** '
                f'`{preview.checkout.desired_checkpoint}`\n'
                f'**Known active work:**\n{active}{warning}\n\n'
                f'-# {self.status}'
            ),
            discord.ui.ActionRow(confirm, cancel),
            accent_colour=discord.Colour.orange(),
        ))

    async def on_timeout(self) -> None:
        self.terminal = True
        self.status = self.expired_message
        self.rebuild()
        self.stop()
        await self._edit()
