"""Short-lived requester-authorized components for game tribe edits."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
import logging

import discord

from modules import game_tribe, game_workers


logger = logging.getLogger('polybot.' + __name__)


SelfCallback = Callable[
    [discord.Interaction, str, game_workers.GameTribeReadResult],
    Awaitable[game_workers.GameTribeMutationResult | None],
]
SingleCallback = Callable[
    [discord.Interaction, str, str, game_workers.GameTribeReadResult],
    Awaitable[game_workers.GameTribeMutationResult | None],
]
BulkPreviewCallback = Callable[
    [discord.Interaction, str, game_workers.GameTribeReadResult],
    Awaitable[game_workers.GameTribeBatchPreview | None],
]
BulkConfirmCallback = Callable[
    [discord.Interaction, game_workers.GameTribeBatchPreview],
    Awaitable[game_workers.GameTribeMutationResult | None],
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
    **kwargs,
) -> None:
    if acknowledged or _response_done(interaction):
        await interaction.followup.send(content, ephemeral=True, **kwargs)
    else:
        await interaction.response.send_message(
            content,
            ephemeral=True,
            **kwargs,
        )


async def _edit_message(message, **kwargs) -> None:
    if message is None:
        return
    try:
        await message.edit(**kwargs)
    except Exception:
        logger.debug(
            'Could not update a game-tribe component message',
            exc_info=True,
        )


class GameTribeEditModal(discord.ui.Modal, title='Edit one player tribe'):
    """Typed single-player editor over an immutable game snapshot."""

    player = discord.ui.TextInput(
        label='Player name or mention',
        placeholder='Use an in-game name or @mention',
        min_length=1,
        max_length=100,
        required=True,
    )
    tribe = discord.ui.TextInput(
        label='Tribe name or abbreviation',
        placeholder='Use None to clear the tribe',
        min_length=1,
        max_length=100,
        required=True,
    )

    def __init__(
        self,
        workspace: 'GameTribeWorkspaceView',
        snapshot: game_workers.GameTribeReadResult,
    ):
        super().__init__()
        self.workspace = workspace
        self.snapshot = snapshot
        self._submitted = False

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self._submitted:
            await _send_ephemeral(
                interaction,
                'This tribe edit was already submitted. Run `/game tribe '
                f'{self.snapshot.game_id}` again for a fresh workspace.',
            )
            return
        if self.workspace.is_finished():
            await _send_ephemeral(
                interaction,
                'This game-tribe workspace expired. Run `/game tribe '
                f'{self.snapshot.game_id}` again for a fresh workspace.',
            )
            return
        if interaction.user.id != self.workspace.requester_id:
            await _send_ephemeral(
                interaction,
                'Only the member who opened this game-tribe workspace can edit it.',
            )
            return
        if not self.workspace._busy:
            await _send_ephemeral(
                interaction,
                'This single-player tribe editor is no longer active. Run '
                f'`/game tribe {self.snapshot.game_id}` again.',
            )
            return

        self._submitted = True
        try:
            await interaction.response.defer(ephemeral=True)
            result = await self.workspace.on_single(
                interaction,
                str(self.player.value or ''),
                str(self.tribe.value or ''),
                self.snapshot,
            )
            if result is not None:
                await self.workspace.apply_result(result)
        except Exception:
            logger.exception('Unexpected game-tribe single edit failure')
            await interaction.followup.send(
                'The single-player tribe edit could not be completed. Run '
                f'`/game tribe {self.snapshot.game_id}` again.',
                ephemeral=True,
            )
        finally:
            self.workspace._release_action()


class GameTribeBulkModal(discord.ui.Modal, title='Bulk edit game tribes'):
    """Collect flat or line-based input, then require preview confirmation."""

    assignments = discord.ui.TextInput(
        label='Player/tribe pairs',
        placeholder='Player Tribe\nPlayer Tribe\n(or: Player Tribe Player Tribe)',
        style=discord.TextStyle.paragraph,
        min_length=1,
        max_length=1000,
        required=True,
    )

    def __init__(
        self,
        workspace: 'GameTribeWorkspaceView',
        snapshot: game_workers.GameTribeReadResult,
    ):
        super().__init__()
        self.workspace = workspace
        self.snapshot = snapshot
        self._submitted = False

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self._submitted:
            await _send_ephemeral(
                interaction,
                'This bulk tribe edit was already submitted. Run `/game tribe '
                f'{self.snapshot.game_id}` again for a fresh workspace.',
            )
            return
        if self.workspace.is_finished():
            await _send_ephemeral(
                interaction,
                'This game-tribe workspace expired. Run `/game tribe '
                f'{self.snapshot.game_id}` again for a fresh workspace.',
            )
            return
        if interaction.user.id != self.workspace.requester_id:
            await _send_ephemeral(
                interaction,
                'Only the member who opened this game-tribe workspace can edit it.',
            )
            return
        if not self.workspace._busy:
            await _send_ephemeral(
                interaction,
                'This bulk tribe editor is no longer active. Run `/game tribe '
                f'{self.snapshot.game_id}` again.',
            )
            return

        self._submitted = True
        try:
            await interaction.response.defer(ephemeral=True)
            preview = await self.workspace.on_bulk_preview(
                interaction,
                str(self.assignments.value or ''),
                self.snapshot,
            )
            if preview is None:
                self.workspace._release_action()
                return
            preview_view = GameTribeBulkPreviewView(
                self.workspace,
                preview,
            )
            self.workspace._previews.append(preview_view)
            preview_view.message = await interaction.followup.send(
                game_tribe.preview_message(preview),
                ephemeral=True,
                view=preview_view,
                wait=True,
            )
        except Exception:
            self.workspace._release_action()
            logger.exception('Could not prepare game-tribe bulk preview')
            if _response_done(interaction):
                await interaction.followup.send(
                    'The bulk tribe preview could not be prepared. Run '
                    f'`/game tribe {self.snapshot.game_id}` again.',
                    ephemeral=True,
                )


class GameTribeSelfSelectView(discord.ui.View):
    """Quick requester-only canonical tribe selection for self assignment."""

    def __init__(
        self,
        workspace: 'GameTribeWorkspaceView',
        snapshot: game_workers.GameTribeReadResult,
        *,
        timeout: float = 120.0,
    ):
        super().__init__(timeout=timeout)
        self.workspace = workspace
        self.snapshot = snapshot
        self.requester_id = workspace.requester_id
        self.message = None
        options = [
            discord.SelectOption(label='None — clear tribe', value='none')
        ]
        options.extend(
            discord.SelectOption(
                label=str(name)[:100],
                value=str(name),
                description=str(emoji)[:100] if emoji else None,
            )
            for name, emoji in snapshot.tribe_choices[:24]
        )
        self.select = discord.ui.Select(
            placeholder='Choose your tribe',
            min_values=1,
            max_values=1,
            options=options[:25],
            custom_id=f'game-tribe:{snapshot.game_id}:self-select',
        )
        self.cancel_button = discord.ui.Button(
            label='Cancel',
            style=discord.ButtonStyle.secondary,
            custom_id=f'game-tribe:{snapshot.game_id}:self-cancel',
        )
        self.select.callback = self._select_clicked
        self.cancel_button.callback = self._cancel_clicked
        self.add_item(self.select)
        self.add_item(self.cancel_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.is_finished() or self.workspace.is_finished():
            await _send_ephemeral(
                interaction,
                'This self-assignment control expired. Run `/game tribe '
                f'{self.snapshot.game_id}` again.',
            )
            return False
        if interaction.user.id != self.requester_id:
            await _send_ephemeral(
                interaction,
                'Only the member who opened this game-tribe workspace can use '
                'the self-assignment control.',
            )
            return False
        return True

    async def _disable(self) -> None:
        self.select.disabled = True
        self.cancel_button.disabled = True
        await _edit_message(self.message, view=self)

    async def _select_clicked(self, interaction: discord.Interaction) -> None:
        if not await self.interaction_check(interaction):
            return
        self.stop()
        try:
            await interaction.response.defer(ephemeral=True)
            await self._disable()
            result = await self.workspace.on_self(
                interaction,
                str(self.select.values[0]),
                self.snapshot,
            )
            if result is not None:
                await self.workspace.apply_result(result)
        except Exception:
            logger.exception('Unexpected game-tribe self-assignment failure')
            await interaction.followup.send(
                'Your tribe could not be updated. Run `/game tribe '
                f'{self.snapshot.game_id}` again.',
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
                'Self-assignment cancelled.',
                ephemeral=True,
            )
        finally:
            self.workspace._release_action()

    async def on_timeout(self) -> None:
        self.stop()
        await self._disable()
        self.workspace._release_action()


class GameTribeBulkPreviewView(discord.ui.View):
    """Requester-only Confirm/Cancel state for a parsed native bulk batch."""

    def __init__(
        self,
        workspace: 'GameTribeWorkspaceView',
        preview: game_workers.GameTribeBatchPreview,
        *,
        timeout: float = 120.0,
    ):
        super().__init__(timeout=timeout)
        self.workspace = workspace
        self.preview = preview
        self.requester_id = workspace.requester_id
        self.message = None
        self.confirm_button = discord.ui.Button(
            label='Confirm',
            style=discord.ButtonStyle.success,
            custom_id=f'game-tribe:{preview.game_id}:bulk-confirm',
        )
        self.cancel_button = discord.ui.Button(
            label='Cancel',
            style=discord.ButtonStyle.secondary,
            custom_id=f'game-tribe:{preview.game_id}:bulk-cancel',
        )
        self.confirm_button.callback = self._confirm_clicked
        self.cancel_button.callback = self._cancel_clicked
        self.add_item(self.confirm_button)
        self.add_item(self.cancel_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.is_finished() or self.workspace.is_finished():
            await _send_ephemeral(
                interaction,
                'This bulk tribe preview expired. Run `/game tribe '
                f'{self.preview.game_id}` again.',
            )
            return False
        if interaction.user.id != self.requester_id:
            await _send_ephemeral(
                interaction,
                'Only the member who opened this game-tribe workspace can '
                'confirm it.',
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
            result = await self.workspace.on_bulk_confirm(
                interaction,
                self.preview,
            )
            if result is not None:
                await self.workspace.apply_result(result)
        except Exception:
            logger.exception('Unexpected game-tribe bulk confirmation failure')
            await interaction.followup.send(
                'The bulk tribe edit could not be completed. Run `/game tribe '
                f'{self.preview.game_id}` again.',
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
                'Bulk tribe edit cancelled.',
                ephemeral=True,
            )
        finally:
            self.workspace._release_action()

    async def on_timeout(self) -> None:
        self.stop()
        await self._disable()
        self.workspace._release_action()


class GameTribeWorkspaceView(discord.ui.View):
    """Public mapping with three bounded requester-only edit paths."""

    def __init__(
        self,
        snapshot: game_workers.GameTribeReadResult,
        *,
        requester_id: int,
        on_self: SelfCallback,
        on_single: SingleCallback,
        on_bulk_preview: BulkPreviewCallback,
        on_bulk_confirm: BulkConfirmCallback,
        requester_actor: game_tribe.GameTribeActor | None = None,
        timeout: float = 300.0,
    ):
        super().__init__(timeout=timeout)
        self.snapshot = snapshot
        self.requester_id = int(requester_id)
        self.requester_actor = requester_actor
        self.on_self = on_self
        self.on_single = on_single
        self.on_bulk_preview = on_bulk_preview
        self.on_bulk_confirm = on_bulk_confirm
        self.message = None
        self._busy = False
        self._expired = False
        self._previews: list[GameTribeBulkPreviewView] = []

        self.self_button = discord.ui.Button(
            label='Set my tribe',
            style=discord.ButtonStyle.primary,
            custom_id=f'game-tribe:{snapshot.game_id}:self',
        )
        self.single_button = discord.ui.Button(
            label='Edit player',
            style=discord.ButtonStyle.secondary,
            custom_id=f'game-tribe:{snapshot.game_id}:single',
        )
        self.bulk_button = discord.ui.Button(
            label='Bulk edit',
            style=discord.ButtonStyle.secondary,
            custom_id=f'game-tribe:{snapshot.game_id}:bulk',
        )
        self.self_button.callback = self._self_clicked
        self.single_button.callback = self._single_clicked
        self.bulk_button.callback = self._bulk_clicked
        self.add_item(self.self_button)
        self.add_item(self.single_button)
        self.add_item(self.bulk_button)

    def _set_state(self) -> None:
        disabled = self._expired
        self.self_button.disabled = disabled
        self.single_button.disabled = disabled
        self.bulk_button.disabled = disabled

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
                'This game-tribe workspace expired. Run `/game tribe '
                f'{self.snapshot.game_id}` again for a fresh workspace.',
            )
            return False
        if interaction.user.id != self.requester_id:
            await _send_ephemeral(
                interaction,
                'Only the member who opened this game-tribe workspace can use '
                'its controls.',
            )
            return False
        return True

    async def _self_clicked(self, interaction: discord.Interaction) -> None:
        if not await self.interaction_check(interaction):
            return
        if not self._claim_action():
            await _send_ephemeral(
                interaction,
                'Another game-tribe action is already in progress. Try again '
                'shortly.',
            )
            return
        select_view = GameTribeSelfSelectView(self, self.snapshot)
        try:
            await interaction.response.defer(ephemeral=True)
            select_view.message = await interaction.followup.send(
                'Choose your tribe. The update will be applied immediately.',
                ephemeral=True,
                view=select_view,
                wait=True,
            )
        except Exception:
            self._release_action()
            logger.exception('Could not open game-tribe self selector')
            if _response_done(interaction):
                await interaction.followup.send(
                    'The self-assignment selector could not be opened. Run '
                    f'`/game tribe {self.snapshot.game_id}` again.',
                    ephemeral=True,
                )

    async def _single_clicked(self, interaction: discord.Interaction) -> None:
        if not await self.interaction_check(interaction):
            return
        if not self._claim_action():
            await _send_ephemeral(
                interaction,
                'Another game-tribe action is already in progress. Try again '
                'shortly.',
            )
            return
        modal = GameTribeEditModal(self, self.snapshot)
        try:
            await interaction.response.send_modal(modal)
        except Exception:
            self._release_action()
            logger.exception('Could not open game-tribe single editor')
            await _send_ephemeral(
                interaction,
                'The single-player tribe editor could not be opened. Run '
                f'`/game tribe {self.snapshot.game_id}` again.',
            )

    async def _bulk_clicked(self, interaction: discord.Interaction) -> None:
        if not await self.interaction_check(interaction):
            return
        if not self._claim_action():
            await _send_ephemeral(
                interaction,
                'Another game-tribe action is already in progress. Try again '
                'shortly.',
            )
            return
        modal = GameTribeBulkModal(self, self.snapshot)
        try:
            await interaction.response.send_modal(modal)
        except Exception:
            self._release_action()
            logger.exception('Could not open game-tribe bulk editor')
            await _send_ephemeral(
                interaction,
                'The bulk tribe editor could not be opened. Run `/game tribe '
                f'{self.snapshot.game_id}` again.',
            )

    async def apply_result(
        self,
        result: game_workers.GameTribeMutationResult,
    ) -> None:
        changed = {
            int(change.lineup_id): change for change in result.changes
        }
        players = tuple(
            replace(
                row,
                tribe_id=changed[row.lineup_id].tribe_id
                if row.lineup_id in changed else row.tribe_id,
                tribe_name=changed[row.lineup_id].tribe_name
                if row.lineup_id in changed else row.tribe_name,
                tribe_emoji=changed[row.lineup_id].tribe_emoji
                if row.lineup_id in changed else row.tribe_emoji,
            )
            for row in self.snapshot.players
        )
        # The result intentionally carries only primitive changed values. The
        # ID is not needed for rendering, but preserving the existing snapshot
        # row keeps the workspace a single immutable DTO boundary.
        self.snapshot = replace(
            self.snapshot,
            players=players,
            expected_snapshots=tuple(
                replace(
                    item,
                    tribe_name=(
                        changed[item.lineup_id].tribe_name
                        if item.lineup_id in changed else item.tribe_name
                    ),
                )
                for item in self.snapshot.expected_snapshots
            ),
            announcement_channel_id=result.announcement_channel_id,
            announcement_message_id=result.announcement_message_id,
        )
        self._set_state()
        if not self._expired and not self.is_finished():
            await _edit_message(
                self.message,
                content=game_tribe.workspace_message(
                    self.snapshot,
                    actor=self.requester_actor,
                ),
                view=self,
            )

    async def on_timeout(self) -> None:
        self._expired = True
        for preview in self._previews:
            if not preview.is_finished():
                preview.stop()
                await preview._disable()
        self._set_state()
        self.stop()
        await _edit_message(
            self.message,
            content=(
                f'{game_tribe.workspace_message(self.snapshot, actor=self.requester_actor)}\n\n'
                f'Tribe controls expired. Run `/game tribe '
                f'{self.snapshot.game_id}` again for a fresh workspace.'
            ),
            view=self,
        )
