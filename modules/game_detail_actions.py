"""Ordinary message components for native game-detail cards.

The detail card remains the established classic embed.  This module owns only
the short-lived component state and delegates every mutation to callbacks
provided by the games cog, which in turn uses the existing game services and
workers.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging

import discord

from modules import game_detail_views, game_detail_workers


logger = logging.getLogger('polybot.' + __name__)


@dataclass(frozen=True)
class PendingGameCardPayload:
    """Immutable card data returned by one bounded detail read."""

    snapshot: game_detail_workers.GameDetailSnapshot
    rendered: game_detail_views.ClassicGameDetailRender


CardLoader = Callable[[discord.Interaction], Awaitable[PendingGameCardPayload]]
JoinAction = Callable[[discord.Interaction, str | None], Awaitable[bool]]
LeaveAction = Callable[[discord.Interaction], Awaitable[bool]]
StartAction = Callable[[discord.Interaction, str], Awaitable[bool]]
DeletePrepareAction = Callable[[discord.Interaction], Awaitable[bool]]
DeleteAction = Callable[[discord.Interaction], Awaitable[bool]]
WinAction = Callable[[discord.Interaction, int, str], Awaitable[bool]]


def winner_action_eligible(
    snapshot: game_detail_workers.GameDetailSnapshot,
) -> bool:
    """Return whether the public card may advertise a first result claim."""

    return bool(
        snapshot.status_label == 'Incomplete'
        and not snapshot.is_pending
        and not snapshot.is_completed
        and not snapshot.is_confirmed
        and not snapshot.win_claimed_ts
        and snapshot.winner_side_id is None
        and not snapshot.cross_guild
        and snapshot.sides
    )


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


async def _reject_unwired_delete(*_args) -> bool:
    """Fail closed if a caller constructs a card without the service hooks."""

    return False


def _side_options(
    snapshot: game_detail_workers.GameDetailSnapshot,
) -> tuple[discord.SelectOption, ...]:
    options = []
    for side in snapshot.sides:
        occupied = len(side.lineups)
        if occupied >= side.capacity:
            continue
        label = f'Side {side.position}'
        if side.sidename:
            label += f' — {side.sidename}'
        description = f'{occupied}/{side.capacity} players'
        options.append(discord.SelectOption(
            label=label[:100],
            value=str(side.position),
            description=description[:100],
        ))
    return tuple(options)


def _winner_side_options(
    snapshot: game_detail_workers.GameDetailSnapshot,
) -> tuple[discord.SelectOption, ...]:
    """Build stable-ID winner choices from one immutable in-progress snapshot."""

    options = []
    for side in snapshot.sides:
        side_name = str(side.sidename or side.name or '').strip()
        label = f'Side {side.position}'
        if side_name:
            label += f' — {side_name}'
        roster = ', '.join(
            lineup.player_name
            for lineup in side.lineups
            if lineup.player_name
        )
        description = f'{len(side.lineups)}/{side.capacity} players'
        if roster:
            description += f': {roster}'
        options.append(discord.SelectOption(
            label=label[:100],
            value=str(side.side_id),
            description=description[:100],
        ))
    return tuple(options)


class PendingGameStartModal(discord.ui.Modal, title='Start pending game'):
    """Collect the exact Polytopia game name for an existing start service."""

    game_name = discord.ui.TextInput(
        label='Polytopia game name',
        placeholder='Exact name shown in Polytopia',
        min_length=1,
        max_length=100,
        required=True,
    )

    def __init__(self, view: 'PendingGameCardView'):
        super().__init__()
        self.card_view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.card_view.run_start(
            interaction,
            str(self.game_name.value or '').strip(),
        )


class PendingGameSideSelectView(discord.ui.View):
    """Requester-only ephemeral side selection for an ambiguous join."""

    def __init__(
        self,
        card_view: 'PendingGameCardView',
        *,
        requester_id: int,
        options: tuple[discord.SelectOption, ...],
        timeout: float = 120.0,
    ):
        super().__init__(timeout=timeout)
        self.card_view = card_view
        self.requester_id = requester_id
        self.message: discord.Message | None = None
        self.side_select = discord.ui.Select(
            placeholder='Choose a side',
            options=list(options),
            min_values=1,
            max_values=1,
        )
        self.side_select.callback = self._select_side
        self.add_item(self.side_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await _send_ephemeral(
            interaction,
            'Only the member who requested side selection can use it.',
        )
        return False

    async def _select_side(self, interaction: discord.Interaction) -> None:
        if self.is_finished():
            await _send_ephemeral(
                interaction,
                'This side selector expired. Press Join on the game card again.',
            )
            return
        self.side_select.disabled = True
        self.stop()
        await interaction.response.defer(ephemeral=True)
        await self.card_view.run_join(
            interaction,
            side_arg=self.side_select.values[0],
            acknowledged=True,
        )
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.DiscordException:
                logger.debug(
                    'Could not disable the completed pending-game side selector',
                    exc_info=True,
                )

    async def on_timeout(self) -> None:
        self.side_select.disabled = True
        self.stop()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.DiscordException:
                logger.debug(
                    'Could not disable the expired pending-game side selector',
                    exc_info=True,
                )


class PendingGameDeleteConfirmationView(discord.ui.View):
    """Requester-only ephemeral confirmation for a public Delete button."""

    def __init__(
        self,
        card_view: 'PendingGameCardView',
        *,
        requester_id: int,
        timeout: float = 120.0,
    ):
        super().__init__(timeout=timeout)
        self.card_view = card_view
        self.requester_id = requester_id
        self.message: discord.Message | None = None
        self.confirm_button = discord.ui.Button(
            label='Confirm delete',
            style=discord.ButtonStyle.danger,
            custom_id=f'pending-game:{card_view.game_id}:delete-confirm',
        )
        self.cancel_button = discord.ui.Button(
            label='Cancel',
            style=discord.ButtonStyle.secondary,
            custom_id=f'pending-game:{card_view.game_id}:delete-cancel',
        )
        self.confirm_button.callback = self._confirm_clicked
        self.cancel_button.callback = self._cancel_clicked
        self.add_item(self.confirm_button)
        self.add_item(self.cancel_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.is_finished():
            await _send_ephemeral(
                interaction,
                'This deletion confirmation expired. Press Delete on the game '
                'card again.',
            )
            return False
        if interaction.user.id != self.requester_id:
            await _send_ephemeral(
                interaction,
                'Only the member who requested deletion can confirm it.',
            )
            return False
        return True

    async def _edit_disabled(self) -> None:
        self.confirm_button.disabled = True
        self.cancel_button.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.DiscordException:
                logger.debug(
                    'Could not disable pending-game deletion confirmation',
                    exc_info=True,
                )

    async def _confirm_clicked(self, interaction: discord.Interaction) -> None:
        if self.is_finished():
            await _send_ephemeral(
                interaction,
                'This deletion confirmation expired. Press Delete on the game '
                'card again.',
            )
            return
        self.stop()
        await interaction.response.defer(ephemeral=True)
        await self._edit_disabled()
        try:
            await self.card_view.run_delete(
                interaction,
                acknowledged=True,
            )
        finally:
            if self.card_view._busy and not self.card_view.is_finished():
                self.card_view._release_delete_claim()

    async def _cancel_clicked(self, interaction: discord.Interaction) -> None:
        if self.is_finished():
            await _send_ephemeral(
                interaction,
                'This deletion confirmation expired. Press Delete on the game '
                'card again.',
            )
            return
        self.stop()
        await interaction.response.defer(ephemeral=True)
        await self._edit_disabled()
        await interaction.followup.send(
            'Game deletion cancelled.',
            ephemeral=True,
        )
        self.card_view._release_delete_claim()

    async def on_timeout(self) -> None:
        self.stop()
        await self._edit_disabled()
        self.card_view._release_delete_claim()


class InProgressWinnerSideSelectView(discord.ui.View):
    """Requester-only ephemeral selector for an in-progress result claim."""

    def __init__(
        self,
        card_view: 'PendingGameCardView',
        *,
        requester_id: int,
        options: tuple[discord.SelectOption, ...],
        timeout: float = 120.0,
    ):
        super().__init__(timeout=timeout)
        self.card_view = card_view
        self.requester_id = requester_id
        self.message: discord.Message | None = None
        self.side_select = discord.ui.Select(
            placeholder='Choose the winning side',
            options=list(options),
            min_values=1,
            max_values=1,
        )
        self.side_select.callback = self._select_side
        self.add_item(self.side_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.is_finished() or self.card_view.is_finished():
            await _send_ephemeral(
                interaction,
                'This winner selector expired. Run `/game show` again for a '
                'fresh card.',
            )
            return False
        if interaction.user.id != self.requester_id:
            await _send_ephemeral(
                interaction,
                'Only the member who requested winner selection can use it.',
            )
            return False
        return True

    async def _edit_disabled(self) -> None:
        self.side_select.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.DiscordException:
                logger.debug(
                    'Could not disable the completed in-progress winner selector',
                    exc_info=True,
                )

    async def _select_side(self, interaction: discord.Interaction) -> None:
        if self.is_finished() or self.card_view.is_finished():
            await _send_ephemeral(
                interaction,
                'This winner selector expired. Run `/game show` again for a '
                'fresh card.',
            )
            return
        if interaction.user.id != self.requester_id:
            await _send_ephemeral(
                interaction,
                'Only the member who requested winner selection can use it.',
            )
            return

        try:
            side_id = int(self.side_select.values[0])
        except (IndexError, TypeError, ValueError):
            await _send_ephemeral(
                interaction,
                'That winner selection is invalid. Run `/game show` again.',
            )
            return

        selected_option = next(
            (
                option for option in self.side_select.options
                if option.value == str(side_id)
            ),
            None,
        )
        if selected_option is None:
            await _send_ephemeral(
                interaction,
                'That winner selection is invalid. Run `/game show` again.',
            )
            return

        self.side_select.disabled = True
        self.stop()
        # Acknowledge before editing or creating the next ephemeral step.
        await interaction.response.defer(ephemeral=True)
        await self._edit_disabled()

        confirmation = DeclareWinnerConfirmationView(
            self.card_view,
            requester_id=self.requester_id,
            winning_side_id=side_id,
            winner_label=selected_option.label,
        )
        self.card_view._winner_confirmations.append(confirmation)
        confirmation.message = await interaction.followup.send(
            f'Confirm declaring **{selected_option.label}** the winner of '
            f'game `{self.card_view.game_id}`. The result may finalize ELO.',
            ephemeral=True,
            view=confirmation,
            wait=True,
        )

    async def on_timeout(self) -> None:
        self.stop()
        await self._edit_disabled()


class DeclareWinnerConfirmationView(discord.ui.View):
    """Requester-only confirmation before a result mutation can be submitted."""

    def __init__(
        self,
        card_view: 'PendingGameCardView',
        *,
        requester_id: int,
        winning_side_id: int,
        winner_label: str,
        timeout: float = 120.0,
    ):
        super().__init__(timeout=timeout)
        self.card_view = card_view
        self.requester_id = requester_id
        self.winning_side_id = int(winning_side_id)
        self.winner_label = str(winner_label)
        self.message: discord.Message | None = None
        self.confirm_button = discord.ui.Button(
            label='Confirm winner',
            style=discord.ButtonStyle.success,
            custom_id=f'in-progress-game:{card_view.game_id}:winner-confirm',
        )
        self.cancel_button = discord.ui.Button(
            label='Cancel',
            style=discord.ButtonStyle.secondary,
            custom_id=f'in-progress-game:{card_view.game_id}:winner-cancel',
        )
        self.confirm_button.callback = self._confirm_clicked
        self.cancel_button.callback = self._cancel_clicked
        self.add_item(self.confirm_button)
        self.add_item(self.cancel_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.is_finished() or self.card_view.is_finished():
            await _send_ephemeral(
                interaction,
                'This winner confirmation expired. Run `/game show` again for '
                'a fresh card.',
            )
            return False
        if interaction.user.id != self.requester_id:
            await _send_ephemeral(
                interaction,
                'Only the member who requested winner confirmation can use it.',
            )
            return False
        return True

    async def _edit_disabled(self) -> None:
        self.confirm_button.disabled = True
        self.cancel_button.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.DiscordException:
                logger.debug(
                    'Could not disable in-progress winner confirmation',
                    exc_info=True,
                )

    async def _confirm_clicked(self, interaction: discord.Interaction) -> None:
        if self.is_finished() or self.card_view.is_finished():
            await _send_ephemeral(
                interaction,
                'This winner confirmation expired. Run `/game show` again for '
                'a fresh card.',
            )
            return
        if interaction.user.id != self.requester_id:
            await _send_ephemeral(
                interaction,
                'Only the member who requested winner confirmation can use it.',
            )
            return
        self.stop()
        # Defer before the confirmation edit and before the mutation service.
        await interaction.response.defer(ephemeral=True)
        await self._edit_disabled()
        await self.card_view.run_winner(
            interaction,
            winning_side_id=self.winning_side_id,
            winner_label=self.winner_label,
            acknowledged=True,
        )

    async def _cancel_clicked(self, interaction: discord.Interaction) -> None:
        if self.is_finished() or self.card_view.is_finished():
            await _send_ephemeral(
                interaction,
                'This winner confirmation expired. Run `/game show` again for '
                'a fresh card.',
            )
            return
        if interaction.user.id != self.requester_id:
            await _send_ephemeral(
                interaction,
                'Only the member who requested winner confirmation can use it.',
            )
            return
        self.stop()
        await interaction.response.defer(ephemeral=True)
        await self._edit_disabled()
        await interaction.followup.send(
            'Winner declaration cancelled.',
            ephemeral=True,
        )

    async def on_timeout(self) -> None:
        self.stop()
        await self._edit_disabled()


class PendingGameCardView(discord.ui.View):
    """Shared public controls for one native game-detail card.

    The controls are deliberately chosen from the last immutable snapshot,
    but every mutation click loads a new snapshot before invoking its callback.
    A callback returns ``True`` only after the existing application service has
    committed and fully published its public post-commit result.  A committed
    but reconciliation-required outcome is deliberately not a card refresh
    success.
    """

    expired_message = (
        'This game-card interaction has expired. Run `/game show` again for a '
        'fresh card.'
    )
    busy_message = 'Another action is already being processed for this game.'
    card_refresh_failure = (
        'The game action completed, but the card could not be refreshed. '
        'Run `/game show` again for the current state.'
    )

    def __init__(
        self,
        *,
        snapshot: game_detail_workers.GameDetailSnapshot,
        load_card: CardLoader,
        on_join: JoinAction,
        on_leave: LeaveAction,
        on_start: StartAction,
        on_delete_prepare: DeletePrepareAction | None = None,
        on_delete: DeleteAction | None = None,
        on_winner: WinAction | None = None,
        timeout: float = 300.0,
    ):
        super().__init__(timeout=timeout)
        self.snapshot = snapshot
        self.load_card = load_card
        self.on_join = on_join
        self.on_leave = on_leave
        self.on_start = on_start
        self.on_delete_prepare = on_delete_prepare or _reject_unwired_delete
        self.on_delete = on_delete or _reject_unwired_delete
        self.on_winner = on_winner or self._reject_unwired_winner
        self.message: discord.Message | None = None
        self._busy = False
        self._side_selectors: list[PendingGameSideSelectView] = []
        self._delete_confirmations: list[PendingGameDeleteConfirmationView] = []
        self._winner_selectors: list[InProgressWinnerSideSelectView] = []
        self._winner_confirmations: list[DeclareWinnerConfirmationView] = []
        self.rebuild()

    @property
    def game_id(self) -> int:
        return self.snapshot.game_id

    def _add_button(
        self,
        *,
        label: str,
        custom_id: str,
        style: discord.ButtonStyle,
        callback,
    ) -> discord.ui.Button:
        button = discord.ui.Button(
            label=label,
            custom_id=custom_id,
            style=style,
        )
        button.callback = callback
        self.add_item(button)
        return button

    @staticmethod
    async def _reject_unwired_winner(*_args) -> bool:
        """Fail closed if a caller constructs a winner card without a service."""

        return False

    def rebuild(self) -> None:
        """Build controls from public state without making authorization claims."""

        self.clear_items()
        self.join_button = None
        self.leave_button = None
        self.start_button = None
        self.delete_button = None
        self.declare_winner_button = None
        self.refresh_button = None

        if winner_action_eligible(self.snapshot):
            self.declare_winner_button = self._add_button(
                label='Declare Winner',
                custom_id=f'in-progress-game:{self.game_id}:winner',
                style=discord.ButtonStyle.success,
                callback=self._winner_clicked,
            )
            self.refresh_button = self._add_button(
                label='Refresh',
                custom_id=f'in-progress-game:{self.game_id}:refresh',
                style=discord.ButtonStyle.secondary,
                callback=self._refresh_clicked,
            )
            return

        if not self.snapshot.is_pending:
            return

        if self.snapshot.status_label == 'Expired open game':
            self.delete_button = self._add_button(
                label='Delete',
                custom_id=f'pending-game:{self.game_id}:delete',
                style=discord.ButtonStyle.danger,
                callback=self._delete_clicked,
            )
            self.refresh_button = self._add_button(
                label='Refresh',
                custom_id=f'pending-game:{self.game_id}:refresh',
                style=discord.ButtonStyle.secondary,
                callback=self._refresh_clicked,
            )
            return

        if self.snapshot.pending_full:
            self.leave_button = self._add_button(
                label='Leave',
                custom_id=f'pending-game:{self.game_id}:leave',
                style=discord.ButtonStyle.danger,
                callback=self._leave_clicked,
            )
            self.start_button = self._add_button(
                label='Start',
                custom_id=f'pending-game:{self.game_id}:start',
                style=discord.ButtonStyle.success,
                callback=self._start_clicked,
            )
        elif self.snapshot.pending_join_available:
            self.join_button = self._add_button(
                label='Join',
                custom_id=f'pending-game:{self.game_id}:join',
                style=discord.ButtonStyle.success,
                callback=self._join_clicked,
            )
            self.leave_button = self._add_button(
                label='Leave',
                custom_id=f'pending-game:{self.game_id}:leave',
                style=discord.ButtonStyle.danger,
                callback=self._leave_clicked,
            )

        self.delete_button = self._add_button(
            label='Delete',
            custom_id=f'pending-game:{self.game_id}:delete',
            style=discord.ButtonStyle.danger,
            callback=self._delete_clicked,
        )

        self.refresh_button = self._add_button(
            label='Refresh',
            custom_id=f'pending-game:{self.game_id}:refresh',
            style=discord.ButtonStyle.secondary,
            callback=self._refresh_clicked,
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Keep controls public; service callbacks remain authoritative."""

        if self.is_finished():
            await _send_ephemeral(interaction, self.expired_message)
            return False
        if self._busy:
            await _send_ephemeral(interaction, self.busy_message)
            return False
        return True

    async def _claim(self, interaction: discord.Interaction) -> bool:
        if self.is_finished():
            await _send_ephemeral(interaction, self.expired_message)
            return False
        if self._busy:
            await _send_ephemeral(interaction, self.busy_message)
            return False
        self._busy = True
        return True

    def _release_delete_claim(self) -> None:
        """Release a pending confirmation's card claim after cancel/expiry."""

        if not self.is_finished():
            self._busy = False

    def _remove_controls_after_delete(self) -> None:
        """Make a committed deletion unable to be submitted again."""

        self.clear_items()
        self.stop()

    async def _disable_card_after_delete(self, interaction) -> None:
        self._remove_controls_after_delete()
        message = self.message or getattr(interaction, 'message', None)
        if message is None:
            return
        try:
            await message.edit(view=None)
        except discord.DiscordException:
            logger.debug(
                'Could not remove controls from deleted pending game %s card',
                self.game_id,
                exc_info=True,
            )

    async def _load_fresh(
        self,
        interaction: discord.Interaction,
        *,
        action: str,
        retire_on_failure: bool = False,
    ) -> PendingGameCardPayload | None:
        try:
            payload = await self.load_card(interaction)
        except Exception:
            logger.exception(
                'Could not reload pending game %s before card %s action',
                self.game_id,
                action,
            )
            await _send_ephemeral(
                interaction,
                'The current game state could not be loaded. Run `/game show` '
                'again and try once more.',
                acknowledged=True,
            )
            if retire_on_failure:
                await self._retire_after_failed_reload(interaction)
            return None
        return payload

    async def _retire_after_failed_reload(self, interaction) -> None:
        """Fail closed when the source row is stale, deleted, or unreadable."""

        self.clear_items()
        self.stop()
        message = self.message or getattr(interaction, 'message', None)
        if message is None:
            return
        try:
            await message.edit(view=None)
        except discord.DiscordException:
            logger.debug(
                'Could not remove stale game %s card controls',
                self.game_id,
                exc_info=True,
            )

    @staticmethod
    def _is_active_pending(snapshot) -> bool:
        return bool(
            snapshot.is_pending
            and snapshot.status_label != 'Expired open game'
        )

    async def _refresh_from_payload(
        self,
        interaction: discord.Interaction,
        payload: PendingGameCardPayload,
    ) -> bool:
        self.snapshot = payload.snapshot
        if (
            (self.snapshot.is_pending or winner_action_eligible(self.snapshot))
            and not self.is_finished()
        ):
            self.rebuild()
            view = self
        else:
            self.clear_items()
            self.stop()
            view = None

        message = self.message or getattr(interaction, 'message', None)
        if message is None:
            await _send_ephemeral(
                interaction,
                self.card_refresh_failure,
                acknowledged=True,
            )
            return False

        try:
            kwargs = game_detail_views.classic_edit_kwargs(
                message,
                payload.rendered,
                view=view,
            )
            await message.edit(**kwargs)
        except Exception:
            logger.exception(
                'Could not refresh pending game %s card after action',
                self.game_id,
            )
            await _send_ephemeral(
                interaction,
                self.card_refresh_failure,
                acknowledged=True,
            )
            return False
        return True

    async def _refresh_after_action(
        self,
        interaction: discord.Interaction,
        *,
        retire_on_failure: bool = False,
    ) -> bool:
        payload = await self._load_fresh(
            interaction,
            action='post-commit refresh',
            retire_on_failure=retire_on_failure,
        )
        if payload is None:
            return False
        return await self._refresh_from_payload(interaction, payload)

    async def _winner_clicked(self, interaction: discord.Interaction) -> None:
        """Open a fresh, requester-only side selector for a public click."""

        if not await self._claim(interaction):
            return
        try:
            await interaction.response.defer(ephemeral=True)
            payload = await self._load_fresh(
                interaction,
                action='winner selection',
                retire_on_failure=True,
            )
            if payload is None:
                return
            if not winner_action_eligible(payload.snapshot):
                await _send_ephemeral(
                    interaction,
                    'This game is no longer eligible for a new winner claim. '
                    'Refresh the card or run `/game show` again.',
                    acknowledged=True,
                )
                await self._refresh_from_payload(interaction, payload)
                return

            options = _winner_side_options(payload.snapshot)
            if not options:
                await _send_ephemeral(
                    interaction,
                    'No valid sides were available for this winner claim. '
                    'Run `/game show` again.',
                    acknowledged=True,
                )
                await self._refresh_from_payload(interaction, payload)
                return

            selector = InProgressWinnerSideSelectView(
                self,
                requester_id=interaction.user.id,
                options=options,
            )
            self._winner_selectors.append(selector)
            selector.message = await interaction.followup.send(
                'Choose the winning side. You will confirm the result before '
                'anything is changed.',
                ephemeral=True,
                view=selector,
                wait=True,
            )
        except Exception:
            logger.exception(
                'Unexpected in-progress game %s winner selector failure',
                self.game_id,
            )
            await _send_ephemeral(
                interaction,
                'The winner selector could not be opened. No public game '
                'change was made.',
                acknowledged=True,
            )
        finally:
            # A selector is requester-scoped and ephemeral.  The public card
            # remains usable by another eligible member while this selector
            # is waiting for its owner.
            self._busy = False

    async def run_winner(
        self,
        interaction: discord.Interaction,
        *,
        winning_side_id: int,
        winner_label: str,
        acknowledged: bool = False,
    ) -> bool:
        """Revalidate a selected side and invoke the shared win service."""

        if not await self._claim(interaction):
            return False
        try:
            if not acknowledged:
                await interaction.response.defer(ephemeral=True)
            payload = await self._load_fresh(
                interaction,
                action='winner claim',
                retire_on_failure=True,
            )
            if payload is None:
                return False
            snapshot = payload.snapshot
            if not winner_action_eligible(snapshot):
                await _send_ephemeral(
                    interaction,
                    'This game is no longer eligible for a new winner claim. '
                    'Run `/game show` again for the current state.',
                    acknowledged=True,
                )
                await self._refresh_from_payload(interaction, payload)
                return False
            if int(winning_side_id) not in {
                int(side.side_id) for side in snapshot.sides
            }:
                await _send_ephemeral(
                    interaction,
                    'That winner side is no longer part of this game. Run '
                    '`/game show` again and try once more.',
                    acknowledged=True,
                )
                await self._refresh_from_payload(interaction, payload)
                return False

            if not await self.on_winner(
                interaction,
                int(winning_side_id),
                str(winner_label),
            ):
                return False
            await self._refresh_after_action(
                interaction,
                retire_on_failure=True,
            )
            return True
        except Exception:
            logger.exception(
                'Unexpected in-progress game %s winner action failure',
                self.game_id,
            )
            await _send_ephemeral(
                interaction,
                'The winner could not be recorded. No public game change was '
                'made.',
                acknowledged=True,
            )
            return False
        finally:
            self._busy = False

    async def _join_clicked(self, interaction: discord.Interaction) -> None:
        await self.run_join(interaction)

    async def run_join(
        self,
        interaction: discord.Interaction,
        *,
        side_arg: str | None = None,
        acknowledged: bool = False,
    ) -> None:
        if not await self._claim(interaction):
            return
        try:
            if not acknowledged:
                await interaction.response.defer(ephemeral=True)
            payload = await self._load_fresh(interaction, action='join')
            if payload is None:
                return
            snapshot = payload.snapshot
            if not self._is_active_pending(snapshot):
                await _send_ephemeral(
                    interaction,
                    'This game is no longer open for joining. Refresh the card '
                    'or run `/game show` again.',
                    acknowledged=True,
                )
                return
            if snapshot.pending_full:
                await _send_ephemeral(
                    interaction,
                    'This game is full and is no longer open for joining.',
                    acknowledged=True,
                )
                return

            if side_arg is None:
                options = _side_options(snapshot)
                if len(options) > 1:
                    selector = PendingGameSideSelectView(
                        self,
                        requester_id=interaction.user.id,
                        options=options,
                    )
                    self._side_selectors.append(selector)
                    selector.message = await interaction.followup.send(
                        'Choose a side for this join. Your selection will be '
                        'checked again before the game changes.',
                        ephemeral=True,
                        view=selector,
                        wait=True,
                    )
                    return

            if not await self.on_join(interaction, side_arg):
                return
            await self._refresh_after_action(interaction)
        except Exception:
            logger.exception('Unexpected pending game %s join action failure', self.game_id)
            await _send_ephemeral(
                interaction,
                'The game could not be joined. No public game change was made.',
                acknowledged=True,
            )
        finally:
            self._busy = False

    async def _leave_clicked(self, interaction: discord.Interaction) -> None:
        if not await self._claim(interaction):
            return
        try:
            await interaction.response.defer(ephemeral=True)
            payload = await self._load_fresh(interaction, action='leave')
            if payload is None:
                return
            if not self._is_active_pending(payload.snapshot):
                await _send_ephemeral(
                    interaction,
                    'This game is no longer pending. Refresh the card or run '
                    '`/game show` again.',
                    acknowledged=True,
                )
                return
            if not await self.on_leave(interaction):
                return
            await self._refresh_after_action(interaction)
        except Exception:
            logger.exception('Unexpected pending game %s leave action failure', self.game_id)
            await _send_ephemeral(
                interaction,
                'The game could not be changed. No public game change was made.',
                acknowledged=True,
            )
        finally:
            self._busy = False

    async def _start_clicked(self, interaction: discord.Interaction) -> None:
        if self.is_finished():
            await _send_ephemeral(interaction, self.expired_message)
            return
        await interaction.response.send_modal(PendingGameStartModal(self))

    async def _delete_clicked(self, interaction: discord.Interaction) -> None:
        if not await self._claim(interaction):
            return
        confirmation_sent = False
        try:
            await interaction.response.defer(ephemeral=True)
            payload = await self._load_fresh(interaction, action='delete')
            if payload is None:
                return
            if not payload.snapshot.is_pending:
                await _send_ephemeral(
                    interaction,
                    'This game is no longer pending. Refresh the card or run '
                    '`/game show` again.',
                    acknowledged=True,
                )
                return
            if not await self.on_delete_prepare(interaction):
                return
            if self.is_finished():
                await _send_ephemeral(
                    interaction,
                    self.expired_message,
                    acknowledged=True,
                )
                return

            confirmation = PendingGameDeleteConfirmationView(
                self,
                requester_id=interaction.user.id,
            )
            self._delete_confirmations.append(confirmation)
            confirmation.message = await interaction.followup.send(
                f'Are you sure you want to permanently delete pending game '
                f'`{self.game_id}`? This cannot be undone.',
                ephemeral=True,
                view=confirmation,
                wait=True,
            )
            confirmation_sent = True
        except Exception:
            logger.exception(
                'Unexpected pending game %s delete confirmation failure',
                self.game_id,
            )
            await _send_ephemeral(
                interaction,
                'The deletion confirmation could not be opened. No public '
                'game change was made.',
                acknowledged=True,
            )
        finally:
            if not confirmation_sent:
                self._busy = False

    async def run_delete(
        self,
        interaction: discord.Interaction,
        *,
        acknowledged: bool = False,
    ) -> bool:
        """Run the shared deletion service after confirmation."""

        if self.is_finished():
            await _send_ephemeral(interaction, self.expired_message, acknowledged=acknowledged)
            self._release_delete_claim()
            return False
        try:
            if not acknowledged:
                await interaction.response.defer(ephemeral=True)
            if not await self.on_delete(interaction):
                return False
            await self._disable_card_after_delete(interaction)
            return True
        except Exception:
            logger.exception(
                'Unexpected pending game %s delete action failure',
                self.game_id,
            )
            await _send_ephemeral(
                interaction,
                'The game could not be deleted. No public game change was '
                'made.',
                acknowledged=True,
            )
            return False
        finally:
            self._busy = False

    async def run_start(
        self,
        interaction: discord.Interaction,
        name: str,
    ) -> None:
        if not await self._claim(interaction):
            return
        try:
            await interaction.response.defer(ephemeral=True)
            payload = await self._load_fresh(interaction, action='start')
            if payload is None:
                return
            if not self._is_active_pending(payload.snapshot):
                await _send_ephemeral(
                    interaction,
                    'This game is no longer pending. Refresh the card or run '
                    '`/game show` again.',
                    acknowledged=True,
                )
                return
            if not payload.snapshot.pending_full:
                await _send_ephemeral(
                    interaction,
                    'This game is not full. All players must join before it '
                    'can be started.',
                    acknowledged=True,
                )
                return
            if not await self.on_start(interaction, name):
                return
            await self._refresh_after_action(interaction)
        except Exception:
            logger.exception('Unexpected pending game %s start action failure', self.game_id)
            await _send_ephemeral(
                interaction,
                'The game could not be started. No public game change was made.',
                acknowledged=True,
            )
        finally:
            self._busy = False

    async def _refresh_clicked(self, interaction: discord.Interaction) -> None:
        if not await self._claim(interaction):
            return
        try:
            await interaction.response.defer(ephemeral=True)
            payload = await self._load_fresh(interaction, action='refresh')
            if payload is not None:
                await self._refresh_from_payload(interaction, payload)
        except Exception:
            logger.exception('Unexpected pending game %s refresh failure', self.game_id)
            await _send_ephemeral(
                interaction,
                'The game card could not be refreshed. Run `/game show` again.',
                acknowledged=True,
            )
        finally:
            self._busy = False

    async def on_timeout(self) -> None:
        # Mark the public view finished before awaiting any child-message
        # edits.  A concurrent click cannot slip through while timeout
        # reconciliation is yielding to Discord.
        self.stop()
        for selector in self._side_selectors:
            selector.stop()
            selector.side_select.disabled = True
        for selector in self._winner_selectors:
            selector.stop()
            await selector._edit_disabled()
        for confirmation in self._delete_confirmations:
            confirmation.stop()
            confirmation.confirm_button.disabled = True
            confirmation.cancel_button.disabled = True
            await confirmation._edit_disabled()
        for confirmation in self._winner_confirmations:
            confirmation.stop()
            await confirmation._edit_disabled()
        for child in self.children:
            if isinstance(child, (discord.ui.Button, discord.ui.Select)):
                child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.DiscordException:
                logger.debug(
                    'Could not disable expired pending game %s card controls',
                    self.game_id,
                    exc_info=True,
                )
