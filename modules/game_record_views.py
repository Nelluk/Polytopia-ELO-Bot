"""Interactive preview for the flexible ``/game record`` roster grammar."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
import shlex

import discord


ACCENT_COLOUR = discord.Colour.from_rgb(83, 126, 231)
logger = logging.getLogger('polybot.' + __name__)


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
class RosterMember:
    """Resolved primitive member state safe to retain in a short-lived view."""

    discord_id: int
    display_name: str


@dataclass(frozen=True)
class GameRecordPreview:
    """Primitive display state for a pending game-record request."""

    game_name: str
    roster: str
    ranked: bool
    sides: tuple[tuple[RosterMember, ...], ...]


class GameRecordView(discord.ui.LayoutView):
    """Requester-only review gate before the game transaction is submitted."""

    def __init__(
        self,
        *,
        requester_id: int,
        preview: GameRecordPreview,
        confirmer: Callable[[discord.Interaction, str], Awaitable[None]],
        timeout: float = 300.0,
    ):
        super().__init__(timeout=timeout)
        self.requester_id = requester_id
        self.preview = preview
        self.confirmer = confirmer
        self.message: discord.Message | None = None
        self.status = 'Review the parsed sides before creating the game.'
        self.finished = False
        self.editing = False
        self.selected_side = 0
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
            f'**Side {index}:** '
            f'{", ".join(member.display_name for member in side) or "*(empty)*"}'
            for index, side in enumerate(self.preview.sides, start=1)
        )
        ranked = 'Ranked' if self.preview.ranked else 'Unranked'

        controls = self._editing_controls() if self.editing else (
            self._review_controls()
        )
        children = [
            discord.ui.TextDisplay(
                '# Record game\n'
                f'**{discord.utils.escape_markdown(self.preview.game_name)}**'
                f' · {ranked}'
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(side_lines),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(f'-# {self.status}'),
            *controls,
        ]
        self.add_item(discord.ui.Container(
            *children,
            accent_colour=ACCENT_COLOUR,
        ))

    def _review_controls(self) -> tuple[discord.ui.ActionRow, ...]:
        confirm = discord.ui.Button(
            label='Confirm record',
            style=discord.ButtonStyle.success,
            disabled=(
                self.finished
                or any(not side for side in self.preview.sides)
            ),
        )
        confirm.callback = self._confirm
        edit = discord.ui.Button(
            label='Edit sides',
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
        return (discord.ui.ActionRow(confirm, edit, cancel),)

    def _editing_controls(self) -> tuple[discord.ui.ActionRow, ...]:
        side_options = []
        for index, side in enumerate(self.preview.sides):
            names = ', '.join(member.display_name for member in side)
            side_options.append(discord.SelectOption(
                label=f'Side {index + 1}',
                value=str(index),
                description=(names or 'No players selected')[:100],
                default=index == self.selected_side,
            ))
        side_select = discord.ui.Select(
            placeholder='Choose a side to edit',
            options=side_options,
        )
        side_select.callback = self._select_side

        selected_members = self.preview.sides[self.selected_side]
        member_select = discord.ui.UserSelect(
            placeholder=f'Replace players on side {self.selected_side + 1}',
            min_values=1,
            max_values=25,
            default_values=[
                discord.SelectDefaultValue(
                    id=member.discord_id,
                    type=discord.SelectDefaultValueType.user,
                )
                for member in selected_members
            ],
        )
        member_select.callback = self._replace_side
        self.side_select = side_select
        self.member_select = member_select

        add_side = discord.ui.Button(label='Add side')
        add_side.callback = self._add_side
        remove_side = discord.ui.Button(
            label='Remove side',
            style=discord.ButtonStyle.danger,
            disabled=len(self.preview.sides) <= 2,
        )
        remove_side.callback = self._remove_side
        done = discord.ui.Button(
            label='Done editing',
            style=discord.ButtonStyle.success,
        )
        done.callback = self._done_editing
        return (
            discord.ui.ActionRow(side_select),
            discord.ui.ActionRow(member_select),
            discord.ui.ActionRow(add_side, remove_side, done),
        )

    def _replace_sides(
        self,
        sides: list[list[RosterMember]],
    ) -> None:
        frozen_sides = tuple(tuple(side) for side in sides)
        roster = ' vs '.join(
            ' '.join(f'<@{member.discord_id}>' for member in side)
            for side in frozen_sides
        )
        self.preview = GameRecordPreview(
            game_name=self.preview.game_name,
            roster=roster,
            ranked=self.preview.ranked,
            sides=frozen_sides,
        )

    async def _edit(self, interaction: discord.Interaction) -> None:
        self.editing = True
        self.status = (
            'Choose a side, then use the Discord member selector to replace '
            'its players.'
        )
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _select_side(self, interaction: discord.Interaction) -> None:
        self.selected_side = int(self.side_select.values[0])
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _replace_side(self, interaction: discord.Interaction) -> None:
        sides = [list(side) for side in self.preview.sides]
        sides[self.selected_side] = [
            RosterMember(
                discord_id=member.id,
                display_name=discord.utils.escape_markdown(
                    member.display_name
                ),
            )
            for member in self.member_select.values
        ]
        self._replace_sides(sides)
        self.status = f'Updated side {self.selected_side + 1}.'
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _add_side(self, interaction: discord.Interaction) -> None:
        sides = [list(side) for side in self.preview.sides]
        sides.append([])
        self.selected_side = len(sides) - 1
        self._replace_sides(sides)
        self.status = 'Select at least one player for the new side.'
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _remove_side(self, interaction: discord.Interaction) -> None:
        if len(self.preview.sides) <= 2:
            await interaction.response.send_message(
                'A game must have at least two sides.',
                ephemeral=True,
            )
            return
        sides = [list(side) for side in self.preview.sides]
        sides.pop(self.selected_side)
        self.selected_side = min(self.selected_side, len(sides) - 1)
        self._replace_sides(sides)
        self.status = 'Removed that side.'
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _done_editing(self, interaction: discord.Interaction) -> None:
        if any(not side for side in self.preview.sides):
            await interaction.response.send_message(
                'Select at least one player for every side.',
                ephemeral=True,
            )
            return
        self.editing = False
        self.status = 'Review the updated sides before creating the game.'
        self.rebuild()
        await interaction.response.edit_message(view=self)

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
        try:
            await self.confirmer(interaction, self.preview.roster)
        except Exception:
            logger.exception('Unexpected error confirming game record')
            self.status = (
                'Game creation failed unexpectedly. No confirmation was '
                'recorded; run `/game record` again.'
            )
        else:
            self.status = (
                'Creation attempt finished. Review the bot response in this '
                'channel.'
            )
        self.rebuild()
        try:
            await interaction.edit_original_response(view=self)
        except discord.HTTPException:
            pass

    async def on_timeout(self) -> None:
        self.finished = True
        self.status = 'This draft expired. Run `/game record` again.'
        self.rebuild()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
