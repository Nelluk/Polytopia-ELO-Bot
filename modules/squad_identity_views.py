"""Requester-only modal presentation for squad identity edits."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging

import discord

from modules import squad_identity, squad_identity_workers, squad_show_workers


logger = logging.getLogger('polybot.' + __name__)


SquadNameMutationCallback = Callable[
    [
        discord.Interaction,
        squad_show_workers.SquadShowCard,
        str | None,
        bool,
    ],
    Awaitable[None],
]


def _response_is_done(interaction: discord.Interaction) -> bool:
    response = getattr(interaction, 'response', None)
    value = getattr(response, 'is_done', False)
    return bool(value() if callable(value) else value)


async def _send_private(
    interaction: discord.Interaction,
    content: str,
) -> None:
    if _response_is_done(interaction):
        await interaction.followup.send(content, ephemeral=True)
    else:
        await interaction.response.send_message(content, ephemeral=True)


class SquadNameEditModal(discord.ui.Modal, title='Edit squad name'):
    """Edit or explicitly clear the name from one immutable card snapshot."""

    def __init__(
        self,
        workspace,
        card: squad_identity_workers.SquadShowCard,
    ):
        super().__init__()
        self.workspace = workspace
        self.card = card
        self._submitted = False
        self.name_input = discord.ui.TextInput(
            placeholder='Leave blank only when clearing the name',
            required=False,
            max_length=squad_identity.MAX_SQUAD_NAME_LENGTH,
        )
        self.clear_input = discord.ui.Checkbox(
            custom_id=f'squad-name:{int(card.squad_id)}:clear',
            default=False,
        )
        # Aliases keep the modal easy to exercise in offline tests while the
        # labels make the checkbox's explicit clear meaning visible in Discord.
        self.name = self.name_input
        self.clear = self.clear_input
        self.name_input.default = card.squad_name or ''
        self.add_item(discord.ui.Label(
            text='Squad name',
            description=(
                f'Whitespace is normalized; maximum '
                f'{squad_identity.MAX_SQUAD_NAME_LENGTH} characters.'
            ),
            component=self.name_input,
        ))
        self.add_item(discord.ui.Label(
            text='Clear current name',
            description='Select this explicitly to remove the saved name.',
            component=self.clear_input,
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self._submitted:
            await _send_private(
                interaction,
                'This squad-name edit was already submitted. Run `/squad show` '
                'again for a fresh workspace.',
            )
            return
        if self.workspace.is_finished():
            await _send_private(
                interaction,
                'This squad-name editor expired. Run `/squad show` again for a '
                'fresh workspace.',
            )
            return
        if int(interaction.user.id) != int(self.workspace.requester_id):
            await _send_private(
                interaction,
                'Only the member who opened this squad card can edit its name.',
            )
            return
        if not self.workspace._claim_action():
            await _send_private(
                interaction,
                'Another squad-name action is already in progress. Try again '
                'shortly.',
            )
            return

        self._submitted = True
        try:
            await interaction.response.defer(ephemeral=True)
            raw_name = str(self.name_input.value or '')
            clear = bool(self.clear_input.value)
            # An empty optional input is not an implicit clear.  Only the
            # explicit checkbox turns omission into a clear operation.  The
            # unchanged prefilled snapshot is likewise not a newly supplied
            # name when the requester selects that checkbox.
            if clear and raw_name == str(self.card.squad_name or ''):
                name = None
            else:
                name = raw_name if raw_name != '' else None
            try:
                squad_identity.validate_input(name, clear)
            except squad_identity_workers.SquadNameValidationError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            callback = self.workspace.name_mutator
            if callback is None:
                await interaction.followup.send(
                    'Squad-name editing is unavailable. Run `/squad show` '
                    'again for a fresh workspace.',
                    ephemeral=True,
                )
                return
            await callback(interaction, self.card, name, clear)
        except Exception:
            logger.exception('Unexpected squad-name modal failure')
            if _response_is_done(interaction):
                await interaction.followup.send(
                    'The squad-name edit could not be completed. Run `/squad '
                    'show` again if the problem persists.',
                    ephemeral=True,
                )
        finally:
            self.workspace._release_action()
