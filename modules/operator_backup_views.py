"""Private requester-bound confirmation for production backup execution."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging

import discord

from modules import operator_backup


logger = logging.getLogger('polybot.' + __name__)


class BackupConfirmationView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        requester_id: int,
        runner: Callable[
            [discord.Interaction],
            Awaitable[operator_backup.BackupResult],
        ],
        timeout: float = 300.0,
    ):
        super().__init__(timeout=timeout)
        self.requester_id = int(requester_id)
        self.runner = runner
        self.message = None
        self.busy = False
        self.finished = False
        self.status = (
            'The production identity and fixed host backup wrapper are ready. '
            'Confirm only when an exceptional manual recovery point is needed.'
        )
        self.rebuild()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) == self.requester_id:
            return True
        await interaction.response.send_message(
            'Only the requesting bot owner can control this backup preview.',
            ephemeral=True,
        )
        return False

    def rebuild(self) -> None:
        self.clear_items()
        run = discord.ui.Button(
            label='Run backup',
            style=discord.ButtonStyle.danger,
            disabled=self.busy or self.finished,
        )
        run.callback = self._run
        cancel = discord.ui.Button(
            label='Cancel',
            style=discord.ButtonStyle.secondary,
            disabled=self.busy or self.finished,
        )
        cancel.callback = self._cancel
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(
                '# Production backup\n'
                'Routine recovery points remain managed by the '
                'three-times-daily host schedule. This control runs the same '
                'reviewed production database, public GameLog, local-image, '
                'and DuckDB reporting workflow immediately.\n\n'
                f'-# {self.status}'
            ),
            discord.ui.ActionRow(run, cancel),
            accent_colour=discord.Colour.orange(),
        ))

    async def _edit(self) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass

    async def _run(self, interaction: discord.Interaction) -> None:
        if self.busy or self.finished:
            return await interaction.response.send_message(
                'This backup preview is already being handled.', ephemeral=True
            )
        self.busy = True
        self.status = 'Backup running; do not start another host backup.'
        self.rebuild()
        # The component interaction supplies a fresh 15-minute token. Stop the
        # five-minute preview timer as soon as execution owns the panel so it
        # cannot advertise an expiry/retry while the child remains active.
        self.stop()
        await interaction.response.defer()
        try:
            await interaction.edit_original_response(view=self)
        except discord.HTTPException:
            logger.warning(
                'Could not publish operator backup running state; continuing '
                'with the accepted single-flight operation.',
                exc_info=True,
            )
        try:
            result = await self.runner(interaction)
        except operator_backup.BackupConflictError as exc:
            return await self._finish(interaction, str(exc))
        except operator_backup.BackupError as exc:
            return await self._finish(interaction, str(exc))
        except asyncio.CancelledError:
            await self._finish(
                interaction,
                'The backup was interrupted and its process group was '
                'stopped. Inspect host logs and artifacts before retrying.',
            )
            raise
        except Exception:
            logger.exception('Unexpected operator backup view failure')
            return await self._finish(
                interaction,
                'The backup ended without a trustworthy result. Inspect host '
                'logs and artifacts before retrying.',
            )
        await self._finish(interaction, operator_backup.format_result(result))

    async def _finish(
        self,
        interaction: discord.Interaction,
        status: str,
    ) -> None:
        """Replace the private panel with exactly one terminal result."""

        self.finished = True
        self.busy = False
        self.status = status
        self.rebuild()
        self.stop()
        await interaction.edit_original_response(view=self)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self.finished = True
        self.status = 'Backup cancelled. No process was started.'
        self.rebuild()
        self.stop()
        await interaction.response.edit_message(view=self)

    async def on_timeout(self) -> None:
        if self.busy or self.finished:
            return
        self.finished = True
        self.status = 'This backup preview expired. Run the command again.'
        self.rebuild()
        self.stop()
        await self._edit()
