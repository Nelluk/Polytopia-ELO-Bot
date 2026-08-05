"""Requester-controlled Components v2 presentation for squad discovery."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
import math

import discord

from modules import components_v2, squad_identity_views, squad_show_workers


PAGE_SIZE = squad_show_workers.SQUAD_SHOW_PAGE_SIZE
ACCENT_COLOUR = components_v2.DEFAULT_ACCENT
logger = logging.getLogger('polybot.' + __name__)


def _response_is_done(interaction: discord.Interaction) -> bool:
    response = getattr(interaction, 'response', None)
    value = getattr(response, 'is_done', False)
    return bool(value() if callable(value) else value)


def _safe_text(value: object) -> str:
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value))
    )


def _safe_game_text(value: object) -> str:
    """Keep the legacy game-summary formatting while preventing pings."""

    return discord.utils.escape_mentions(str(value))


def _member_text(member: squad_show_workers.SquadShowMember) -> str:
    name = _safe_text(member.name)
    return f'{member.team_emoji} **{name}**'.strip()


def _squad_name(card: squad_show_workers.SquadShowCard) -> str:
    return _safe_text(card.squad_name) if card.squad_name else 'Unnamed squad'


class SquadShowWorkspace(components_v2.RequesterLayoutView):
    """Public squad snapshot with requester-only cached navigation."""

    unauthorized_message = 'Only the requester can control this squad view.'

    def __init__(
        self,
        *,
        requester_id: int,
        result: squad_show_workers.SquadShowResult,
        member_loader: Callable[
            [tuple[int, ...]],
            Awaitable[squad_show_workers.SquadShowResult],
        ] | None = None,
        name_mutator: squad_identity_views.SquadNameMutationCallback | None = None,
        timeout: float = 300.0,
    ):
        super().__init__(requester_id=requester_id, timeout=timeout)
        self.result = result
        self.member_loader = member_loader
        self.name_mutator = name_mutator
        self.selected_squad_id = result.selected_squad_id
        self.message: discord.Message | None = None
        self._busy = False
        self.rebuild()

    @property
    def is_detail(self) -> bool:
        return self.selected_squad_id is not None

    @property
    def page_count(self) -> int:
        if self.is_detail:
            return 1
        return max(1, math.ceil(len(self.result.cards) / PAGE_SIZE))

    @property
    def current_cards(self) -> tuple[squad_show_workers.SquadShowCard, ...]:
        if self.is_detail:
            return ()
        start = self.page_index * PAGE_SIZE
        return self.result.cards[start:start + PAGE_SIZE]

    @property
    def selected_card(self) -> squad_show_workers.SquadShowCard | None:
        if self.selected_squad_id is None:
            return None
        return next(
            (
                card for card in self.result.cards
                if card.squad_id == int(self.selected_squad_id)
            ),
            None,
        )

    async def _private_error(
        self,
        interaction: discord.Interaction,
        message: str,
    ) -> None:
        if _response_is_done(interaction):
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def authorize(self, interaction: discord.Interaction) -> bool:
        if self.is_finished():
            await self._private_error(interaction, self.expired_message)
            return False
        return await super().authorize(interaction)

    def _claim_action(self) -> bool:
        if self.is_finished() or self._busy:
            return False
        self._busy = True
        return True

    def _release_action(self) -> None:
        self._busy = False

    async def _open_edit_name(self, interaction: discord.Interaction) -> None:
        if not await self.authorize(interaction):
            return
        card = self.selected_card
        if card is None or not card.can_edit_name:
            await self._private_error(
                interaction,
                'Only a member of this squad or server staff can edit its name.',
            )
            return
        if self.name_mutator is None:
            await self._private_error(
                interaction,
                'Squad-name editing is unavailable. Run `/squad show` again.',
            )
            return
        try:
            await interaction.response.send_modal(
                squad_identity_views.SquadNameEditModal(self, card)
            )
        except Exception:
            await self._private_error(
                interaction,
                'The squad-name editor could not be opened. Run `/squad show` '
                'again.',
            )

    async def apply_refreshed_result(
        self,
        result: squad_show_workers.SquadShowResult,
    ) -> None:
        """Replace the public dense card with a post-commit bounded reload."""

        selected_squad_id = self.selected_squad_id
        self.result = result
        self.selected_squad_id = (
            result.selected_squad_id
            if result.selected_squad_id is not None
            else selected_squad_id
        )
        self.page_index = 0
        self.rebuild()
        if self.message is not None:
            await self.message.edit(view=self)

    async def _publish_member_search(
        self,
        interaction: discord.Interaction,
        result: squad_show_workers.SquadShowResult,
    ) -> None:
        """Replace the public snapshot after one bounded member search."""

        previous_result = self.result
        previous_selected_squad_id = self.selected_squad_id
        previous_page_index = self.page_index
        try:
            self.result = result
            self.selected_squad_id = result.selected_squad_id
            self.page_index = 0
            self.rebuild()
            message = await interaction.edit_original_response(view=self)
        except Exception:
            self.result = previous_result
            self.selected_squad_id = previous_selected_squad_id
            self.page_index = previous_page_index
            self.rebuild()
            raise
        if message is not None:
            self.message = message

    async def _select_members(self, interaction: discord.Interaction) -> None:
        if not await self.authorize(interaction):
            return
        try:
            member_ids = tuple(
                int(getattr(value, 'id', value))
                for value in tuple(self.member_select.values or ())
            )
            if not (
                squad_show_workers.SQUAD_MEMBER_MIN
                <= len(member_ids)
                <= squad_show_workers.SQUAD_MEMBER_MAX
            ):
                raise squad_show_workers.SquadShowValidationError(
                    'Choose between one and three different Discord members.'
                )
            if len(set(member_ids)) != len(member_ids):
                raise squad_show_workers.SquadShowValidationError(
                    'Choose each Discord member only once.'
                )
            if any(member_id <= 0 for member_id in member_ids):
                raise squad_show_workers.SquadShowValidationError(
                    'Every selected Discord member must be valid.'
                )
        except (
            TypeError,
            ValueError,
            squad_show_workers.SquadShowValidationError,
        ):
            await self._private_error(
                interaction,
                'The member selection is invalid. Choose one to three guild '
                'members and try again.',
            )
            return

        if self.member_loader is None:
            await self._private_error(
                interaction,
                'Member search is unavailable. Run `/squad show` again.',
            )
            return

        # Component deferral defaults to deferred_message_update. Its original
        # response is the public workspace message, so success must edit that
        # message and must not try to create or delete a private placeholder.
        await interaction.response.defer()
        try:
            result = await self.member_loader(tuple(member_ids))
        except (
            squad_show_workers.SquadShowValidationError,
            squad_show_workers.SquadShowLookupError,
        ) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception:
            await interaction.followup.send(
                'Could not search squads for those members. Please run '
                '`/squad show` again.',
                ephemeral=True,
            )
            return

        if not result.cards:
            await interaction.followup.send(
                'No eligible squads matched those members.',
                ephemeral=True,
            )
            return

        previous_page_index = self.page_index
        previous_selected_squad_id = self.selected_squad_id
        try:
            await self._publish_member_search(interaction, result)
        except Exception as exc:
            logger.exception(
                'Squad member-search public refresh failed: %s; '
                'requester_id=%s member_ids=%s previous_page=%s '
                'previous_selected_squad_id=%s loaded_matches=%s',
                exc,
                self.requester_id,
                member_ids,
                previous_page_index,
                previous_selected_squad_id,
                min(len(result.cards), squad_show_workers.MAX_SQUAD_MATCHES),
            )
            await interaction.followup.send(
                'The squad workspace could not be refreshed. Please run '
                '`/squad show` again.',
                ephemeral=True,
            )

    async def _select_result(self, interaction: discord.Interaction) -> None:
        if not await self.authorize(interaction):
            return
        values = tuple(self.result_select.values or ())
        if len(values) != 1:
            await self._private_error(
                interaction,
                'Choose one of the displayed squads.',
            )
            return
        try:
            squad_id = int(values[0])
        except (TypeError, ValueError):
            await self._private_error(
                interaction,
                'Choose one of the displayed squads.',
            )
            return
        if squad_id not in {card.squad_id for card in self.result.cards}:
            await self._private_error(
                interaction,
                'That squad is no longer in this snapshot. Run `/squad show` '
                'again.',
            )
            return
        self.selected_squad_id = squad_id
        self.page_index = 0
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _back_to_results(self, interaction: discord.Interaction) -> None:
        if not await self.authorize(interaction):
            return
        if len(self.result.cards) <= 1:
            await self._private_error(
                interaction,
                'There is only one loaded squad in this snapshot.',
            )
            return
        self.selected_squad_id = None
        self.page_index = 0
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _move_page(
        self,
        interaction: discord.Interaction,
        page_index: int,
    ) -> None:
        if not await self.authorize(interaction):
            return
        if self.is_detail:
            await self._private_error(
                interaction,
                'This squad card has no additional result pages.',
            )
            return
        if page_index < 0 or page_index >= self.page_count:
            await self._private_error(
                interaction,
                f'Enter a page number from 1 to {self.page_count}.',
            )
            return
        self.page_index = page_index
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _previous_page(self, interaction: discord.Interaction) -> None:
        await self._move_page(
            interaction,
            max(0, self.page_index - 1),
        )

    async def _next_page(self, interaction: discord.Interaction) -> None:
        await self._move_page(
            interaction,
            min(self.page_count - 1, self.page_index + 1),
        )

    async def _open_page_modal(self, interaction: discord.Interaction) -> None:
        if not await self.authorize(interaction):
            return
        if self.is_detail:
            await self._private_error(
                interaction,
                'This squad card has no additional result pages.',
            )
            return
        await interaction.response.send_modal(
            components_v2.PageJumpModal(self)
        )

    def _card_body(self, card: squad_show_workers.SquadShowCard) -> str:
        member_lines = '\n'.join(
            f'- {_member_text(member)}'
            for member in card.members
        ) or '*No registered members found.*'
        rank = (
            f'#{card.leaderboard_rank} / {card.leaderboard_length}'
            if card.leaderboard_rank is not None
            else f'Unranked ({card.leaderboard_length} eligible squads)'
        )
        recent_lines = '\n\n'.join(
            f'**{_safe_game_text(game.headline)}**\n> '
            f'{_safe_game_text(game.summary)}'
            for game in card.recent_games[:squad_show_workers.RECENT_GAME_LIMIT]
        ) or '*No recent games found.*'
        recent_lines = recent_lines[:3800]
        return (
            f'## Squad #{card.squad_id} · {_squad_name(card)}\n'
            f'**Members**\n{member_lines}\n\n'
            f'**Current squad ELO:** `{card.elo}`\n'
            f'**Confirmed ranked record:** `{card.wins}W – {card.losses}L`\n'
            f'**Current leaderboard:** `{rank}`\n\n'
            f'**Most recent games**\n{recent_lines}'
        )[:4000]

    def _result_body(self) -> str:
        rows = []
        for card in self.current_cards:
            members = ' / '.join(
                _safe_text(member.name) for member in card.members
            )
            rows.append(
                f'**#{card.squad_id} · {_squad_name(card)}**\n'
                f'> {members or "No members"} · `{card.elo} ELO` · '
                f'`{card.wins}W – {card.losses}L`'
            )
        return '\n\n'.join(rows) or '*No eligible squads matched those members.*'

    def _result_options(self) -> list[discord.SelectOption]:
        options = []
        for card in self.current_cards[:25]:
            label = f'Squad #{card.squad_id} · {_squad_name(card)}'[:100]
            description = (
                f'{" / ".join(member.name for member in card.members)} · '
                f'{card.elo} ELO · {card.wins}W–{card.losses}L'
            )[:100]
            options.append(
                discord.SelectOption(
                    label=label,
                    value=str(card.squad_id),
                    description=description,
                )
            )
        return options

    def rebuild(self) -> None:
        self.clear_items()
        self.page_index = min(self.page_index, self.page_count - 1)
        self.result_select = None
        self.edit_name_button = None
        self.member_select = discord.ui.UserSelect(
            placeholder='Search squads by 1–3 members',
            min_values=squad_show_workers.SQUAD_MEMBER_MIN,
            max_values=squad_show_workers.SQUAD_MEMBER_MAX,
        )
        self.member_select.callback = self._select_members

        if self.is_detail:
            card = self.selected_card
            if card is None:
                self.selected_squad_id = None
                self.rebuild()
                return
            body = self._card_body(card)
            controls = [discord.ui.ActionRow(self.member_select)]
            if card.can_edit_name and self.name_mutator is not None:
                self.edit_name_button = discord.ui.Button(
                    label='Edit name',
                    style=discord.ButtonStyle.primary,
                    custom_id=f'squad-show:{int(card.squad_id)}:edit-name',
                )
                self.edit_name_button.callback = self._open_edit_name
                controls.append(discord.ui.ActionRow(self.edit_name_button))
            if len(self.result.cards) > 1:
                back = discord.ui.Button(
                    label='Back to matches',
                    style=discord.ButtonStyle.secondary,
                )
                back.callback = self._back_to_results
                controls.append(discord.ui.ActionRow(back))
            children = [
                discord.ui.TextDisplay('# 🛡️ Squad workspace'),
                discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
                discord.ui.TextDisplay(body),
                discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
                *controls,
                discord.ui.TextDisplay(
                    '-# This result is public. Controls belong to the '
                    'requester and expire; rerun `/squad show` afterward.'
                ),
            ]
        else:
            truncated = (
                f'{squad_show_workers.MAX_SQUAD_MATCHES}+ '
                f'(showing the first {squad_show_workers.MAX_SQUAD_MATCHES})'
                if self.result.truncated
                else str(self.result.total_matches)
            )
            result_select = discord.ui.Select(
                placeholder='Open a loaded squad card',
                options=self._result_options() or [
                    discord.SelectOption(
                        label='No matching squads',
                        value='none',
                        description='Run another member search.',
                    )
                ],
                disabled=not bool(self.current_cards),
            )
            result_select.callback = self._select_result
            self.result_select = result_select
            previous = discord.ui.Button(
                label='Previous',
                emoji='◀️',
                disabled=self.page_index == 0,
            )
            previous.callback = self._previous_page
            page = discord.ui.Button(
                label=f'Page {self.page_index + 1}/{self.page_count}',
                style=discord.ButtonStyle.primary,
                disabled=self.page_count <= 1,
            )
            page.callback = self._open_page_modal
            next_page = discord.ui.Button(
                label='Next',
                emoji='▶️',
                disabled=self.page_index == self.page_count - 1,
            )
            next_page.callback = self._next_page
            children = [
                discord.ui.TextDisplay('# 🔎 Squad search'),
                discord.ui.TextDisplay(
                    f'**Matches:** `{truncated}` · '
                    f'**Page:** `{self.page_index + 1}/{self.page_count}`'
                ),
                discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
                discord.ui.TextDisplay(self._result_body()),
                discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
                discord.ui.ActionRow(self.member_select),
                discord.ui.ActionRow(result_select),
                discord.ui.ActionRow(previous, page, next_page),
                discord.ui.TextDisplay(
                    '-# Results are public. Select a loaded squad for its '
                    'dense card; controls expire and can be rerun.'
                ),
            ]

        self.add_item(discord.ui.Container(
            *children,
            accent_colour=ACCENT_COLOUR,
        ))
