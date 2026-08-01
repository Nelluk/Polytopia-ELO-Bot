"""Ordinary message components for pending-game detail cards.

The detail card remains the established classic embed.  This module owns only
the short-lived component state and delegates every mutation to callbacks
provided by the games cog, which in turn uses the existing pending-game
services and workers.
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


class PendingGameCardView(discord.ui.View):
    """Shared public controls for one pending-game card.

    The controls are deliberately chosen from the last immutable snapshot,
    but every mutation click loads a new snapshot before invoking its callback.
    A callback returns ``True`` only after the existing application service has
    committed and published its public post-commit result.
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
        timeout: float = 300.0,
    ):
        super().__init__(timeout=timeout)
        self.snapshot = snapshot
        self.load_card = load_card
        self.on_join = on_join
        self.on_leave = on_leave
        self.on_start = on_start
        self.message: discord.Message | None = None
        self._busy = False
        self._side_selectors: list[PendingGameSideSelectView] = []
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

    def rebuild(self) -> None:
        """Build controls from public state without making authorization claims."""

        self.clear_items()
        self.join_button = None
        self.leave_button = None
        self.start_button = None
        self.refresh_button = None

        if not self.snapshot.is_pending:
            return

        if self.snapshot.status_label == 'Expired open game':
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

    async def _load_fresh(
        self,
        interaction: discord.Interaction,
        *,
        action: str,
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
            return None
        return payload

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
        if self.snapshot.is_pending:
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
    ) -> bool:
        payload = await self._load_fresh(interaction, action='post-commit refresh')
        if payload is None:
            return False
        return await self._refresh_from_payload(interaction, payload)

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
        for selector in self._side_selectors:
            selector.stop()
            selector.side_select.disabled = True
        for child in self.children:
            if isinstance(child, (discord.ui.Button, discord.ui.Select)):
                child.disabled = True
        self.stop()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.DiscordException:
                logger.debug(
                    'Could not disable expired pending game %s card controls',
                    self.game_id,
                    exc_info=True,
                )
