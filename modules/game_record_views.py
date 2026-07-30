"""Interactive preview for the flexible ``/game record`` roster grammar."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import shlex

import discord


ACCENT_COLOUR = discord.Colour.from_rgb(83, 126, 231)


class RosterSyntaxError(ValueError):
    """Raised when a roster string cannot describe complete game sides."""


def parse_roster_string(roster: str) -> tuple[tuple[str, ...], ...]:
    """Parse the legacy ``player ... vs player ...`` grammar.

    A single player retains the prefix command's requester-versus-opponent
    shortcut. Multiple players require an explicit ``vs``/``versus`` side
    separator so an accidentally omitted separator cannot create one large
    side.
    """

    try:
        tokens = shlex.split(roster)
    except ValueError as exc:
        raise RosterSyntaxError(f'Invalid roster quoting: {exc}') from exc
    if not tokens:
        raise RosterSyntaxError('Enter at least one player.')
    if len(tokens) == 1:
        return ((tokens[0],),)

    sides: list[list[str]] = [[]]
    for token in tokens:
        if token.lower() in {'vs', 'versus'}:
            if not sides[-1]:
                raise RosterSyntaxError(
                    'Each `vs` separator must have players on both sides.'
                )
            sides.append([])
            continue
        sides[-1].append(token)

    if not sides[-1]:
        raise RosterSyntaxError(
            'Each `vs` separator must have players on both sides.'
        )
    if len(sides) < 2:
        raise RosterSyntaxError(
            'Separate sides with `vs`, for example '
            '`@Alice @Bob vs @Carol @Dave`.'
        )
    return tuple(tuple(side) for side in sides)


def roster_arguments(sides: tuple[tuple[str, ...], ...]) -> tuple[str, ...]:
    """Flatten parsed sides into the existing prefix callback grammar."""

    if len(sides) == 1:
        return sides[0]
    arguments: list[str] = []
    for index, side in enumerate(sides):
        if index:
            arguments.append('vs')
        arguments.extend(side)
    return tuple(arguments)


@dataclass(frozen=True)
class GameRecordPreview:
    """Primitive display state for a pending game-record request."""

    game_name: str
    roster: str
    ranked: bool
    sides: tuple[tuple[str, ...], ...]


class EditRosterModal(discord.ui.Modal):
    """Allow the requester to revise the free-form roster safely."""

    def __init__(self, view: 'GameRecordView'):
        super().__init__(title='Edit game roster', timeout=180.0)
        self.record_view = view
        self.roster = discord.ui.TextInput(
            default=view.preview.roster,
            placeholder='@Alice @Bob vs @Carol @Dave',
            style=discord.TextStyle.paragraph,
            min_length=1,
            max_length=4000,
        )
        self.add_item(discord.ui.Label(
            text='Players and sides',
            description='Separate sides with “vs”. Mentions are safest.',
            component=self.roster,
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        view = self.record_view
        if interaction.user.id != view.requester_id:
            await interaction.response.send_message(
                'Only the requester can edit this game draft.',
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        try:
            preview = await view.previewer(self.roster.value)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        view.preview = preview
        view.rebuild()
        await interaction.edit_original_response(view=view)


class GameRecordView(discord.ui.LayoutView):
    """Requester-only review gate before the game transaction is submitted."""

    def __init__(
        self,
        *,
        requester_id: int,
        preview: GameRecordPreview,
        previewer: Callable[[str], Awaitable[GameRecordPreview]],
        confirmer: Callable[[discord.Interaction, str], Awaitable[None]],
        timeout: float = 300.0,
    ):
        super().__init__(timeout=timeout)
        self.requester_id = requester_id
        self.preview = preview
        self.previewer = previewer
        self.confirmer = confirmer
        self.message: discord.Message | None = None
        self.status = 'Review the parsed sides before creating the game.'
        self.finished = False
        self.rebuild()

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            'Only the requester can control this game draft.',
            ephemeral=True,
        )
        return False

    def rebuild(self) -> None:
        self.clear_items()
        side_lines = '\n'.join(
            f'**Side {index}:** {", ".join(side)}'
            for index, side in enumerate(self.preview.sides, start=1)
        )
        ranked = 'Ranked' if self.preview.ranked else 'Unranked'

        confirm = discord.ui.Button(
            label='Confirm record',
            style=discord.ButtonStyle.success,
            disabled=self.finished,
        )
        confirm.callback = self._confirm
        edit = discord.ui.Button(
            label='Edit roster',
            style=discord.ButtonStyle.primary,
            disabled=self.finished,
        )
        edit.callback = self._edit
        cancel = discord.ui.Button(
            label='Cancel',
            style=discord.ButtonStyle.danger,
            disabled=self.finished,
        )
        cancel.callback = self._cancel

        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(
                '# Record game\n'
                f'**{discord.utils.escape_markdown(self.preview.game_name)}**'
                f' · {ranked}'
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(side_lines),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(f'-# {self.status}'),
            discord.ui.ActionRow(confirm, edit, cancel),
            accent_colour=ACCENT_COLOUR,
        ))

    async def _edit(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(EditRosterModal(self))

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self.finished = True
        self.status = 'Cancelled. No database or Discord changes were made.'
        self.rebuild()
        self.stop()
        await interaction.response.edit_message(view=self)

    async def _confirm(self, interaction: discord.Interaction) -> None:
        self.finished = True
        self.status = 'Creating the game…'
        self.rebuild()
        await interaction.response.edit_message(view=self)
        self.stop()
        await self.confirmer(interaction, self.preview.roster)

    async def on_timeout(self) -> None:
        self.finished = True
        self.status = 'This draft expired. Run `/game record` again.'
        self.rebuild()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
