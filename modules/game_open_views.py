"""Requester-controlled preview for native pending-game creation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import asyncio
import logging

import discord


logger = logging.getLogger('polybot.' + __name__)
ACCENT_COLOUR = discord.Colour.from_rgb(83, 126, 231)


@dataclass(frozen=True)
class OpenGameDraft:
    size: tuple[int, ...]
    ranked: bool = True
    expiration_hours: int = 24
    notes: str = ''

    @property
    def size_string(self) -> str:
        return 'v'.join(str(size) for size in self.size)


class OpenGameNotesModal(discord.ui.Modal, title='Open-game notes'):
    notes = discord.ui.TextInput(
        label='Notes',
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=150,
    )

    def __init__(self, view: 'OpenGameView'):
        super().__init__()
        self.open_game_view = view
        self.notes.default = view.draft.notes

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.open_game_view.requester_id:
            await interaction.response.send_message(
                'Only the requester can control this open-game draft.',
                ephemeral=True,
            )
            return
        self.open_game_view.set_notes(str(self.notes.value or ''))
        await interaction.response.edit_message(view=self.open_game_view)


class OpenGameView(discord.ui.LayoutView):
    """Short-lived requester-only draft before the shared worker runs."""

    def __init__(
        self,
        *,
        requester_id: int,
        draft: OpenGameDraft,
        confirmer: Callable[
            [discord.Interaction, OpenGameDraft], Awaitable[None]
        ],
        timeout: float = 300.0,
    ):
        super().__init__(timeout=timeout)
        self.requester_id = requester_id
        self.draft = draft
        self.confirmer = confirmer
        self.message: discord.Message | None = None
        self.status = 'Review the draft and confirm when it is ready.'
        self.finished = False
        self.rebuild()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                'Only the requester can control this open-game draft.',
                ephemeral=True,
            )
            return False
        if self.finished:
            await interaction.response.send_message(
                'This draft is no longer active. Run `/game open` again.',
                ephemeral=True,
            )
            return False
        return True

    def rebuild(self) -> None:
        self.clear_items()
        ranked = 'Ranked' if self.draft.ranked else 'Unranked'
        notes = (
            discord.utils.escape_mentions(self.draft.notes)
            if self.draft.notes
            else '*(none)*'
        )
        expiration_options = [1, 24, 48, 96, 168]
        if self.draft.expiration_hours not in expiration_options:
            expiration_options.append(self.draft.expiration_hours)
            expiration_options.sort()
        expiration_select = discord.ui.Select(
            placeholder='Set expiration',
            options=[
                discord.SelectOption(
                    label=f'{hours} hours',
                    value=str(hours),
                    default=hours == self.draft.expiration_hours,
                )
                for hours in expiration_options
            ],
            disabled=self.finished,
        )
        expiration_select.callback = self._set_expiration

        ranked_button = discord.ui.Button(
            label=f'Switch to {"unranked" if self.draft.ranked else "ranked"}',
            style=discord.ButtonStyle.secondary,
            disabled=self.finished,
        )
        ranked_button.callback = self._toggle_ranked
        notes_button = discord.ui.Button(
            label='Edit notes',
            style=discord.ButtonStyle.primary,
            disabled=self.finished,
        )
        notes_button.callback = self._edit_notes
        confirm = discord.ui.Button(
            label='Confirm open game',
            style=discord.ButtonStyle.success,
            disabled=self.finished,
        )
        confirm.callback = self._confirm
        cancel = discord.ui.Button(
            label='Cancel',
            style=discord.ButtonStyle.danger,
            disabled=self.finished,
        )
        cancel.callback = self._cancel

        self.expiration_select = expiration_select
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(
                '# Open game\n'
                f'**Size:** {self.draft.size_string}\n'
                f'**State:** {ranked}\n'
                f'**Expiration:** {self.draft.expiration_hours} hours\n'
                f'**Notes:** {notes}'
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(f'-# {self.status}'),
            discord.ui.ActionRow(ranked_button, notes_button),
            discord.ui.ActionRow(expiration_select),
            discord.ui.ActionRow(confirm, cancel),
            accent_colour=ACCENT_COLOUR,
        ))

    def set_notes(self, notes: str) -> None:
        self.draft = OpenGameDraft(
            size=self.draft.size,
            ranked=self.draft.ranked,
            expiration_hours=self.draft.expiration_hours,
            notes=notes[:150].strip(),
        )
        self.status = 'Notes updated. Review the draft before confirming.'
        self.rebuild()

    async def _toggle_ranked(self, interaction: discord.Interaction) -> None:
        self.draft = OpenGameDraft(
            size=self.draft.size,
            ranked=not self.draft.ranked,
            expiration_hours=self.draft.expiration_hours,
            notes=self.draft.notes,
        )
        self.status = 'Ranked state updated. Review the draft before confirming.'
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _set_expiration(self, interaction: discord.Interaction) -> None:
        self.draft = OpenGameDraft(
            size=self.draft.size,
            ranked=self.draft.ranked,
            expiration_hours=int(self.expiration_select.values[0]),
            notes=self.draft.notes,
        )
        self.status = 'Expiration updated. Review the draft before confirming.'
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _edit_notes(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(OpenGameNotesModal(self))

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self.finished = True
        self.status = (
            'Cancelled. No database or Discord changes were made. Run '
            '`/game open` again when ready.'
        )
        self.rebuild()
        self.stop()
        await interaction.response.edit_message(view=self)

    async def _confirm(self, interaction: discord.Interaction) -> None:
        if self.finished:
            await interaction.response.send_message(
                'This draft is no longer active. Run `/game open` again.',
                ephemeral=True,
            )
            return

        self.finished = True
        self.status = 'Creating the open game…'
        self.stop()
        await interaction.response.defer(ephemeral=True)

        try:
            await self.confirmer(interaction, self.draft)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Unexpected error confirming open game')
            self.status = (
                'Creation failed unexpectedly. No confirmation was recorded; '
                'run `/game open` again.'
            )
        else:
            self.status = (
                'Creation attempt finished. Run `/game open` again for another '
                'game.'
            )

        self.rebuild()
        try:
            await interaction.edit_original_response(view=self)
        except discord.DiscordException:
            logger.debug('Could not update finished open-game draft', exc_info=True)

    async def on_timeout(self) -> None:
        self.finished = True
        self.status = 'This draft expired. Run `/game open` again.'
        self.rebuild()
        self.stop()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.DiscordException:
                logger.debug('Could not disable expired open-game draft', exc_info=True)
