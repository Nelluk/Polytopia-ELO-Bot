"""Short-lived ordinary Discord components for the game-name workspace."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
import logging

import discord

from modules import game_name, game_workers


logger = logging.getLogger('polybot.' + __name__)


EditCallback = Callable[
    [discord.Interaction, str, game_workers.GameNameReadResult],
    Awaitable[game_workers.GameNameMutationResult | None],
]
ClearCallback = Callable[
    [discord.Interaction, game_workers.GameNameReadResult],
    Awaitable[game_workers.GameNameMutationResult | None],
]


def _response_done(interaction: discord.Interaction) -> bool:
    response = getattr(interaction, 'response', None)
    is_done = getattr(response, 'is_done', None)
    return bool(is_done()) if callable(is_done) else False


async def _send_ephemeral(
    interaction: discord.Interaction,
    content: str,
    *,
    acknowledged: bool = False,
) -> None:
    if acknowledged or _response_done(interaction):
        await interaction.followup.send(content, ephemeral=True)
    else:
        await interaction.response.send_message(content, ephemeral=True)


async def _edit_message(message, **kwargs) -> None:
    if message is None:
        return
    try:
        await message.edit(**kwargs)
    except Exception:
        logger.debug('Could not update a game-name component message', exc_info=True)


class GameNameEditModal(discord.ui.Modal, title='Edit game name'):
    """Edit one immutable snapshot using the model's 35-character boundary."""

    name = discord.ui.TextInput(
        label='Game name (35 characters max)',
        placeholder='Saved in the game model title-case format',
        min_length=1,
        max_length=game_name.GAME_NAME_MAX_LENGTH,
        required=True,
        )
    def __init__(
        self,
        workspace: 'GameNameWorkspaceView',
        snapshot: game_workers.GameNameReadResult,
    ):
        super().__init__()
        self.workspace = workspace
        self.snapshot = snapshot
        self._submitted = False
        self.name.default = snapshot.name or ''

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self._submitted:
            await _send_ephemeral(
                interaction,
                'This game-name edit was already submitted. Run `/game name` '
                'again for a fresh workspace.',
            )
            return
        if self.workspace.is_finished():
            await _send_ephemeral(
                interaction,
                'This game-name workspace expired. Run `/game name` again for a '
                'fresh workspace.',
            )
            return
        if interaction.user.id != self.workspace.requester_id:
            await _send_ephemeral(
                interaction,
                'Only the member who opened this game-name workspace can edit it.',
            )
            return
        if not self.workspace._claim_action():
            await _send_ephemeral(
                interaction,
                'Another game-name action is already in progress. Try again '
                'shortly.',
            )
            return

        self._submitted = True
        try:
            await interaction.response.defer(ephemeral=True)
            value = str(self.name.value or '')
            if not value:
                await interaction.followup.send(
                    'A game name is required. Use Clear name to remove the '
                    'current name.',
                    ephemeral=True,
                )
                return
            if len(value) > game_name.GAME_NAME_MAX_LENGTH:
                await interaction.followup.send(
                    'Game names must be 35 characters or fewer. The stored '
                    'value is also title-cased by the game model.',
                    ephemeral=True,
                )
                return
            result = await self.workspace.on_edit(
                interaction,
                value,
                self.snapshot,
            )
            if result is not None:
                await self.workspace.apply_result(result)
        except Exception:
            logger.exception('Unexpected game-name modal failure')
            await interaction.followup.send(
                'The game-name edit could not be completed. Run `/game name` '
                'again if the problem persists.',
                ephemeral=True,
            )
        finally:
            self.workspace._release_action()


class GameNameClearConfirmationView(discord.ui.View):
    """Requester-only confirmation for the elevated destructive clear action."""

    def __init__(
        self,
        workspace: 'GameNameWorkspaceView',
        snapshot: game_workers.GameNameReadResult,
        *,
        timeout: float = 120.0,
    ):
        super().__init__(timeout=timeout)
        self.workspace = workspace
        self.snapshot = snapshot
        self.requester_id = workspace.requester_id
        self.message = None
        self.confirm_button = discord.ui.Button(
            label='Confirm clear',
            style=discord.ButtonStyle.danger,
            custom_id=f'game-name:{snapshot.game_id}:clear-confirm',
        )
        self.cancel_button = discord.ui.Button(
            label='Cancel',
            style=discord.ButtonStyle.secondary,
            custom_id=f'game-name:{snapshot.game_id}:clear-cancel',
        )
        self.confirm_button.callback = self._confirm_clicked
        self.cancel_button.callback = self._cancel_clicked
        self.add_item(self.confirm_button)
        self.add_item(self.cancel_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.is_finished() or self.workspace.is_finished():
            await _send_ephemeral(
                interaction,
                'This clear confirmation expired. Press Clear name again or '
                'run `/game name` for a fresh workspace.',
            )
            return False
        if interaction.user.id != self.requester_id:
            await _send_ephemeral(
                interaction,
                'Only the member who opened this game-name workspace can confirm '
                'it.',
            )
            return False
        return True

    async def _disable(self) -> None:
        self.confirm_button.disabled = True
        self.cancel_button.disabled = True
        await _edit_message(self.message, view=self)

    async def _confirm_clicked(self, interaction: discord.Interaction) -> None:
        if not await self.interaction_check(interaction):
            return
        self.stop()
        try:
            await interaction.response.defer(ephemeral=True)
            await self._disable()
            result = await self.workspace.on_clear(
                interaction,
                self.snapshot,
            )
            if result is not None:
                await self.workspace.apply_result(result)
        except Exception:
            logger.exception('Unexpected game-name clear confirmation failure')
            await interaction.followup.send(
                'The game-name clear could not be completed. Run `/game name` '
                'again if the problem persists.',
                ephemeral=True,
            )
        finally:
            self.workspace._release_action()

    async def _cancel_clicked(self, interaction: discord.Interaction) -> None:
        if not await self.interaction_check(interaction):
            return
        self.stop()
        try:
            await interaction.response.defer(ephemeral=True)
            await self._disable()
            await interaction.followup.send(
                'Game-name clear cancelled.',
                ephemeral=True,
            )
        finally:
            self.workspace._release_action()

    async def on_timeout(self) -> None:
        self.stop()
        await self._disable()
        self.workspace._release_action()


class GameNameWorkspaceView(discord.ui.View):
    """A small public workspace over the established dense game presentation."""

    def __init__(
        self,
        snapshot: game_workers.GameNameReadResult,
        *,
        requester_id: int,
        on_edit: EditCallback,
        on_clear: ClearCallback,
        requester_actor: game_name.GameNameActor | None = None,
        timeout: float = 300.0,
    ):
        super().__init__(timeout=timeout)
        self.snapshot = snapshot
        self.requester_id = int(requester_id)
        self.requester_actor = requester_actor
        self.on_edit = on_edit
        self.on_clear = on_clear
        self.message = None
        self._busy = False
        self._expired = False
        self._confirmations: list[GameNameClearConfirmationView] = []

        self.edit_button = discord.ui.Button(
            label='Edit name',
            style=discord.ButtonStyle.primary,
            custom_id=f'game-name:{snapshot.game_id}:edit',
        )
        self.clear_button = discord.ui.Button(
            label='Clear name',
            style=discord.ButtonStyle.danger,
            custom_id=f'game-name:{snapshot.game_id}:clear',
        )
        self.edit_button.callback = self._edit_clicked
        self.clear_button.callback = self._clear_clicked
        self.add_item(self.edit_button)
        self.add_item(self.clear_button)
        self._set_state()

    @property
    def current_name(self) -> str | None:
        return self.snapshot.name

    def _set_state(self) -> None:
        disabled = bool(self._expired or self.snapshot.is_pending)
        self.edit_button.disabled = disabled
        self.clear_button.disabled = disabled

    def _claim_action(self) -> bool:
        if self._expired or self.is_finished() or self._busy:
            return False
        self._busy = True
        return True

    def _release_action(self) -> None:
        self._busy = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self._expired or self.is_finished():
            await _send_ephemeral(
                interaction,
                'This game-name workspace expired. Run `/game name` again for a '
                'fresh workspace.',
            )
            return False
        if interaction.user.id != self.requester_id:
            await _send_ephemeral(
                interaction,
                'Only the member who opened this game-name workspace can use its '
                'controls.',
            )
            return False
        if self.snapshot.is_pending:
            await _send_ephemeral(
                interaction,
                'This game has not started yet; its name cannot be edited.',
            )
            return False
        return True

    async def _edit_clicked(self, interaction: discord.Interaction) -> None:
        if not await self.interaction_check(interaction):
            return
        if not self._claim_action():
            await _send_ephemeral(
                interaction,
                'Another game-name action is already in progress. Try again '
                'shortly.',
            )
            return
        modal = GameNameEditModal(self, self.snapshot)
        try:
            await interaction.response.send_modal(modal)
        except Exception:
            logger.exception('Could not open game-name edit modal')
            await _send_ephemeral(
                interaction,
                'The game-name editor could not be opened. Run `/game name` again.',
            )
        finally:
            self._release_action()

    async def _clear_clicked(self, interaction: discord.Interaction) -> None:
        if not await self.interaction_check(interaction):
            return
        if not self._claim_action():
            await _send_ephemeral(
                interaction,
                'Another game-name action is already in progress. Try again '
                'shortly.',
            )
            return
        confirmation = GameNameClearConfirmationView(self, self.snapshot)
        self._confirmations.append(confirmation)
        try:
            await interaction.response.defer(ephemeral=True)
            confirmation.message = await interaction.followup.send(
                f'Clear the current tracked name for game `{self.snapshot.game_id}`? '
                'This requires explicit confirmation and elevated permission.',
                ephemeral=True,
                view=confirmation,
                wait=True,
            )
        except Exception:
            self._release_action()
            logger.exception('Could not open game-name clear confirmation')
            if _response_done(interaction):
                await interaction.followup.send(
                    'The clear confirmation could not be opened. Run `/game name` '
                    'again.',
                    ephemeral=True,
                )

    async def apply_result(
        self,
        result: game_workers.GameNameMutationResult,
    ) -> None:
        """Reflect a committed result in the public workspace if still live."""

        self.snapshot = replace(
            self.snapshot,
            name=result.name,
            is_pending=result.is_pending,
            is_completed=result.is_completed,
            announcement_channel_id=result.announcement_channel_id,
            announcement_message_id=result.announcement_message_id,
        )
        self._set_state()
        if not self._expired and not self.is_finished():
            await _edit_message(
                self.message,
                content=game_name.workspace_message(
                    result.game_id,
                    result.name,
                    actor=self.requester_actor,
                ),
                view=self,
            )

    async def on_timeout(self) -> None:
        self._expired = True
        for confirmation in self._confirmations:
            if not confirmation.is_finished():
                confirmation.stop()
                await confirmation._disable()
        self._set_state()
        self.stop()
        expired_message = game_name.workspace_message(
            self.snapshot.game_id,
            self.snapshot.name,
            actor=self.requester_actor,
        )
        await _edit_message(
            self.message,
            content=(
                f'{expired_message}\n\n'
                f'Name controls expired. Run `/game name {self.snapshot.game_id}` '
                'again for a fresh workspace.'
            ),
            view=self,
        )
