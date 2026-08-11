"""Private Components v2 preview for development fixture mutations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging

import discord

from modules import operator_beta_fixtures as service
from modules import operator_beta_fixtures_workers as workers


logger = logging.getLogger('polybot.' + __name__)


def _safe_name(value: str) -> str:
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value))
    )


class BetaFixturePreviewView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        requester_id: int,
        preview: workers.BetaFixturePreview,
        confirmer: Callable[
            [discord.Interaction, workers.BetaFixturePreview],
            Awaitable[workers.BetaFixtureResult],
        ],
        timeout: float = 300.0,
    ):
        super().__init__(timeout=timeout)
        self.requester_id = int(requester_id)
        self.preview = preview
        self.confirmer = confirmer
        self.message = None
        self.busy = False
        self.finished = False
        self.status = 'Review the exact participants and owned game IDs.'
        self.rebuild()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) == self.requester_id:
            return True
        await interaction.response.send_message(
            'Only the requesting bot owner can control this fixture preview.',
            ephemeral=True,
        )
        return False

    def rebuild(self) -> None:
        self.clear_items()
        operation = self.preview.operation
        action = (
            'Prepare fixtures'
            if operation == workers.PREPARE
            else 'Reset fixtures'
        )
        style = (
            discord.ButtonStyle.primary
            if operation == workers.PREPARE
            else discord.ButtonStyle.danger
        )
        confirm = discord.ui.Button(
            label=action,
            style=style,
            disabled=self.busy or self.finished or not self.preview.can_commit,
        )
        confirm.callback = self._confirm
        cancel = discord.ui.Button(
            label='Cancel',
            style=discord.ButtonStyle.secondary,
            disabled=self.busy or self.finished,
        )
        cancel.callback = self._cancel
        title = (
            'Prepare beta result fixtures'
            if operation == workers.PREPARE
            else 'Reset beta result fixtures'
        )
        warning = (
            'This creates three exactly marked development games.'
            if operation == workers.PREPARE
            else (
                'This deletes only the reviewed, exactly marked owned games, '
                'reverses their ELO effects, and creates a fresh fixed bundle.'
            )
        )
        participant_text = ', '.join(
            f'**{_safe_name(item.display_name)}** '
            f'(`{item.user_id}`)'
            for item in self.preview.participants
        ) or ', '.join(f'`{value}`' for value in self.preview.user_ids)
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(
                f'# {title}\n'
                + service.readiness_markdown(self.preview.snapshot)
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(
                f'**Participants after commit:** '
                + participant_text
                + f'\n{warning}\n-# {self.status}'
            ),
            discord.ui.ActionRow(confirm, cancel),
            accent_colour=(
                discord.Colour.blurple()
                if operation == workers.PREPARE
                else discord.Colour.orange()
            ),
        ))

    async def _edit(self) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass

    async def _confirm(self, interaction: discord.Interaction) -> None:
        if self.busy or self.finished:
            return await interaction.response.send_message(
                'This fixture preview is already being handled.',
                ephemeral=True,
            )
        self.busy = True
        self.status = 'Revalidating and committing the owned fixture bundle…'
        self.rebuild()
        await interaction.response.defer(ephemeral=True)
        await self._edit()
        try:
            result = await self.confirmer(interaction, self.preview)
        except workers.BetaFixtureError as exc:
            self.busy = False
            self.status = str(exc)
            self.rebuild()
            await self._edit()
            return await interaction.followup.send(str(exc), ephemeral=True)
        except Exception:
            logger.exception('Unexpected confirmed beta fixture failure')
            self.busy = False
            self.status = (
                'The fixture operation failed before a confirmed result. '
                'Run `/whattotest` to reconcile current owned state before '
                'retrying.'
            )
            self.rebuild()
            await self._edit()
            return await interaction.followup.send(self.status, ephemeral=True)
        self.busy = False
        self.finished = True
        self.status = service.completion_markdown(
            result,
            participants=self.preview.participants,
        )
        self.rebuild()
        self.stop()
        await self._edit()
        await interaction.followup.send(
            service.completion_markdown(
                result,
                participants=self.preview.participants,
            ),
            ephemeral=True,
        )

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self.finished = True
        self.status = 'Cancelled. No database changes were made.'
        self.rebuild()
        self.stop()
        await interaction.response.edit_message(view=self)

    async def on_timeout(self) -> None:
        self.finished = True
        self.status = 'Expired. Run the command again for a fresh preview.'
        self.rebuild()
        self.stop()
        await self._edit()
