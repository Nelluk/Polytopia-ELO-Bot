"""Requester-bound Components v2 composer for ``/game ping``."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging

import discord

from modules import game_ping, game_ping_workers as workers


logger = logging.getLogger('polybot.' + __name__)


async def _private(interaction, content: str) -> None:
    response = getattr(interaction, 'response', None)
    is_done = getattr(response, 'is_done', None)
    if callable(is_done) and is_done():
        await interaction.followup.send(content, ephemeral=True)
    else:
        await interaction.response.send_message(content, ephemeral=True)


async def _edit_message(message, **kwargs) -> None:
    if message is None:
        return
    try:
        await message.edit(**kwargs)
    except Exception:
        logger.debug('Could not update the game-ping private draft', exc_info=True)


def _preview_chunks(content: str) -> tuple[str, ...]:
    # TextDisplay has a substantially higher practical ceiling than an
    # ordinary message, but keeping each display bounded avoids a giant v2
    # component and leaves room for the controls in the container.
    return game_ping.split_message_chunks(content, max_length=3_800) or ('',)


class GamePingComposeModal(discord.ui.Modal, title='Compose game ping'):
    """Three bounded long-form sections plus one native FileUpload field."""

    section_one = discord.ui.Label(
        text='Message section 1',
        description='Up to 4,000 characters; sections are joined in order.',
        component=discord.ui.TextInput(
            custom_id='game-ping-message-1',
            style=discord.TextStyle.paragraph,
            max_length=workers.MAX_TEXT_SECTION_LENGTH,
            required=False,
            placeholder='First part of the notification',
        ),
    )
    section_two = discord.ui.Label(
        text='Message section 2',
        description='Optional; blank sections are omitted.',
        component=discord.ui.TextInput(
            custom_id='game-ping-message-2',
            style=discord.TextStyle.paragraph,
            max_length=workers.MAX_TEXT_SECTION_LENGTH,
            required=False,
            placeholder='Optional second part',
        ),
    )
    section_three = discord.ui.Label(
        text='Message section 3',
        description='Optional; total section input is at most 12,000 characters.',
        component=discord.ui.TextInput(
            custom_id='game-ping-message-3',
            style=discord.TextStyle.paragraph,
            max_length=workers.MAX_TEXT_SECTION_LENGTH,
            required=False,
            placeholder='Optional third part',
        ),
    )
    attachments = discord.ui.Label(
        text='Attachments (optional)',
        description='Up to 10 Discord uploads; bodies are not stored or downloaded.',
        component=discord.ui.FileUpload(
            custom_id='game-ping-attachments',
            required=False,
            max_values=workers.MAX_ATTACHMENTS,
        ),
    )

    def __init__(
        self,
        view: 'GamePingComposerView',
        generation: int | None = None,
    ):
        super().__init__(timeout=300)
        self.view = view
        self.generation = int(
            view.current_modal_generation
            if generation is None
            else generation
        )
        self._submitted = False
        existing = view.draft.sections if view.draft is not None else ()
        values = tuple(existing) + ('', '', '')
        self.section_one.component.default = values[0]
        self.section_two.component.default = values[1]
        self.section_three.component.default = values[2]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self._submitted:
            await _private(
                interaction,
                'This composer submission was already handled. Use Edit on the '
                'private draft if you need to change it.',
            )
            return
        if not await self.view.authorize(interaction):
            return
        if self.view.is_finished() or self.view.expired:
            await _private(
                interaction,
                'This game-ping draft expired. Run `/game ping` again.',
            )
            return
        if not self.view.is_current_modal(self.generation):
            self._submitted = True
            self.stop()
            await _private(
                interaction,
                'This compose modal is stale because a newer draft modal was '
                'opened. Use the newest modal or choose Edit again.',
            )
            return

        sections = (
            str(getattr(self.section_one.component, 'value', '') or ''),
            str(getattr(self.section_two.component, 'value', '') or ''),
            str(getattr(self.section_three.component, 'value', '') or ''),
        )
        uploaded = getattr(self.attachments.component, 'values', ()) or ()
        try:
            if uploaded:
                frozen_attachments = game_ping.capture_attachments(tuple(uploaded))
            elif self.view.draft is not None:
                # FileUpload has no native "keep existing values" state when a
                # modal is reopened. Preserve the frozen metadata unless the
                # user supplies a replacement set explicitly.
                frozen_attachments = self.view.draft.attachments
            else:
                frozen_attachments = ()
            draft = game_ping.build_draft(sections, frozen_attachments)
        except workers.GamePingValidationError as exc:
            self._submitted = True
            self.stop()
            await _private(interaction, str(exc))
            return

        self._submitted = True
        try:
            await interaction.response.defer(ephemeral=True)
            if not self.view.is_current_modal(self.generation):
                self.stop()
                await _private(
                    interaction,
                    'This compose modal became stale while it was being '
                    'submitted. Use the newest modal or choose Edit again.',
                )
                return
            if self.view.is_finished() or self.view.expired:
                self.stop()
                await _private(
                    interaction,
                    'This game-ping draft expired. Run `/game ping` again.',
                )
                return
            self.view.draft = draft
            self.view.status = (
                'Draft updated. Review the private preview, then Confirm once '
                'to deliver it.'
            )
            self.view.rebuild()
            await _edit_message(self.view.message, view=self.view)
            await interaction.followup.send(
                'Private draft updated. Check the preview before confirming.',
                ephemeral=True,
            )
        except Exception:
            logger.exception('Could not update a game-ping draft from its modal')
            await _private(
                interaction,
                'The private draft could not be updated. Run `/game ping` again '
                'if the problem persists.',
            )

    async def on_timeout(self) -> None:
        """Invalidate this generation if it is still the current modal."""

        self.stop()
        self.view.invalidate_modal_generation(self.generation)

    async def on_error(self, interaction, error, item) -> None:
        """Release the modal lease if Discord dispatches an uncaught error."""

        self.stop()
        self.view.invalidate_modal_generation(self.generation)
        logger.error(
            'Game-ping compose modal failed during Discord dispatch',
            exc_info=(type(error), error, getattr(error, '__traceback__', None)),
        )


class GamePingComposerView(discord.ui.LayoutView):
    """One requester-bound, expiry-safe, single-flight notification draft."""

    def __init__(
        self,
        *,
        requester: workers.MemberSnapshot,
        target: workers.MemberSnapshot,
        result: workers.GamePingLoadResult,
        channel_facts: workers.ChannelFacts,
        selected_game_id: int | None,
        target_loader: Callable[
            [discord.Interaction, workers.MemberSnapshot],
            Awaitable[tuple[workers.GamePingLoadResult, workers.ChannelFacts]],
        ],
        confirmer: Callable[
            [discord.Interaction, 'GamePingComposerView'],
            Awaitable[game_ping.DeliveryResult],
        ],
        timeout: float = 300.0,
    ):
        super().__init__(timeout=timeout)
        self.requester = requester
        self.target = target
        self.result = result
        self.channel_facts = channel_facts
        self.requester_id = int(requester.discord_id)
        self.guild_id = int(result.guild_id)
        self.channel_id = int(channel_facts.channel_id)
        self.selected_game_id = selected_game_id
        self.scope = 'single'
        self.target_loader = target_loader
        self.confirmer = confirmer
        self.message = None
        self.draft: game_ping.GamePingDraft | None = None
        self.status = 'Compose a message, then review and confirm delivery.'
        self.expired = False
        self.committed = False
        self._busy = False
        self._modal_generation = 0
        self._confirmations = 0
        self.rebuild()

    @property
    def current_modal_generation(self) -> int:
        return self._modal_generation

    def next_modal_generation(self) -> int:
        self._modal_generation += 1
        return self._modal_generation

    def is_current_modal(self, generation: int) -> bool:
        return int(generation) == self._modal_generation

    def invalidate_modal_generation(self, generation: int) -> None:
        if self.is_current_modal(generation):
            self._modal_generation += 1

    def _target_select_allowed(self) -> bool:
        return self.requester.level > 3 or self.requester.is_staff

    async def authorize(self, interaction: discord.Interaction) -> bool:
        if int(getattr(getattr(interaction, 'user', None), 'id', 0)) != self.requester_id:
            await _private(
                interaction,
                'Only the member who opened this game-ping draft can use its controls.',
            )
            return False
        interaction_guild_id = getattr(interaction, 'guild_id', None)
        if interaction_guild_id is None:
            interaction_guild_id = getattr(getattr(interaction, 'guild', None), 'id', None)
        interaction_channel_id = getattr(interaction, 'channel_id', None)
        if interaction_channel_id is None:
            interaction_channel_id = getattr(getattr(interaction, 'channel', None), 'id', None)
        if int(interaction_guild_id or 0) != self.guild_id or int(interaction_channel_id or 0) != self.channel_id:
            await _private(
                interaction,
                'This game-ping draft is limited to its original server channel.',
            )
            return False
        if self.expired or self.is_finished():
            await _private(
                interaction,
                'This game-ping draft expired. Run `/game ping` again.',
            )
            return False
        return True

    def _claim(self) -> bool:
        if self.expired or self.committed or self.is_finished() or self._busy:
            return False
        self._busy = True
        return True

    def _release(self) -> None:
        self._busy = False

    def _selected_game_exists(self) -> bool:
        return self.selected_game_id in {
            game.game_id for game in self.result.games
        }

    def _scope_text(self) -> str:
        if self.scope == 'all':
            return 'all incomplete games'
        if self.selected_game_id is None:
            return 'one selected game'
        return f'game {self.selected_game_id}'

    def _controls(self):
        scope_options = (
            discord.SelectOption(
                label='One game',
                value='single',
                description='Notify participants in one loaded incomplete game.',
                default=self.scope == 'single',
            ),
            discord.SelectOption(
                label='All incomplete games',
                value='all',
                description=(
                    'Notify all loaded incomplete games for the target; bounded.'
                ),
                default=self.scope == 'all',
            ),
        )
        scope_select = discord.ui.Select(
            placeholder='Choose notification scope',
            options=scope_options,
            custom_id=f'game-ping:{self.requester_id}:scope',
            disabled=self.committed or self.expired or self._busy,
        )
        scope_select.callback = self._scope_changed
        self.scope_select = scope_select

        game_options = []
        for game in self.result.games[:workers.MAX_GAME_CHOICES]:
            title = f'Game {game.game_id}'
            if game.name:
                title += f' · {game_ping._safe_name(game.name, fallback="game")}'
            game_options.append(discord.SelectOption(
                label=title[:100],
                value=str(game.game_id),
                description=f'{len(game.participants)} resolved participant(s)'[:100],
                default=(
                    self.scope == 'single'
                    and game.game_id == self.selected_game_id
                ),
            ))
        if not game_options:
            game_options = [discord.SelectOption(
                label='No eligible incomplete games loaded',
                value='none',
                description='Run /game ping again after the game is available.',
            )]
        game_select = discord.ui.Select(
            placeholder='Choose one loaded game',
            options=game_options,
            custom_id=f'game-ping:{self.requester_id}:game',
            disabled=(
                self.scope == 'all'
                or self.committed
                or self.expired
                or self._busy
                or not bool(self.result.games)
            ),
        )
        game_select.callback = self._game_changed
        self.game_select = game_select

        rows = [discord.ui.ActionRow(scope_select), discord.ui.ActionRow(game_select)]
        if self._target_select_allowed():
            target_select = discord.ui.UserSelect(
                placeholder='Act for another player (optional)',
                min_values=1,
                max_values=1,
                custom_id=f'game-ping:{self.requester_id}:target',
                default_values=[discord.SelectDefaultValue(
                    id=self.target.discord_id,
                    type=discord.SelectDefaultValueType.user,
                )],
                disabled=self.committed or self.expired or self._busy,
            )
            target_select.callback = self._target_changed
            self.target_select = target_select
            rows.append(discord.ui.ActionRow(target_select))
        else:
            self.target_select = None

        compose = discord.ui.Button(
            label='Edit' if self.draft is not None else 'Compose',
            style=discord.ButtonStyle.primary,
            custom_id=f'game-ping:{self.requester_id}:compose',
            disabled=self.committed or self.expired or self._busy,
        )
        compose.callback = self._compose_clicked
        confirm = discord.ui.Button(
            label='Confirm',
            style=discord.ButtonStyle.success,
            custom_id=f'game-ping:{self.requester_id}:confirm',
            disabled=(
                self.committed
                or self.expired
                or self._busy
                or self.draft is None
                or (self.scope == 'single' and not self._selected_game_exists())
                or (self.scope == 'all' and not self.result.all_scope_allowed)
            ),
        )
        confirm.callback = self._confirm_clicked
        cancel = discord.ui.Button(
            label='Cancel',
            style=discord.ButtonStyle.danger,
            custom_id=f'game-ping:{self.requester_id}:cancel',
            disabled=self.committed or self.expired or self._busy,
        )
        cancel.callback = self._cancel_clicked
        self.compose_button = compose
        self.confirm_button = confirm
        self.cancel_button = cancel
        rows.append(discord.ui.ActionRow(compose, confirm, cancel))
        return rows

    def rebuild(self) -> None:
        self.clear_items()
        preview = game_ping.preview_message(
            self.result,
            requester=self.requester,
            target=self.target,
            scope=self.scope,
            selected_game_id=self.selected_game_id,
            draft=self.draft,
            channel_facts=self.channel_facts,
        )
        children = [
            discord.ui.TextDisplay(chunk)
            for chunk in _preview_chunks(preview)
        ]
        children.extend(self._controls())
        children.append(discord.ui.TextDisplay(f'-# {self.status}'))
        self.add_item(discord.ui.Container(*children))

    async def _refresh(self, interaction) -> None:
        self.rebuild()
        if self.message is not None:
            await _edit_message(self.message, view=self)
        elif hasattr(interaction, 'edit_original_response'):
            await interaction.edit_original_response(view=self)

    async def _scope_changed(self, interaction: discord.Interaction) -> None:
        if not await self.authorize(interaction):
            return
        value = str(self.scope_select.values[0])
        if value == 'all' and not self.result.all_scope_allowed:
            return await _private(
                interaction,
                'This target cannot use the all-incomplete-games scope. Choose '
                'one game or ask a staff member for help.',
            )
        self.scope = value
        self.status = f'Scope changed to {self._scope_text()}.'
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _game_changed(self, interaction: discord.Interaction) -> None:
        if not await self.authorize(interaction):
            return
        try:
            game_id = int(self.game_select.values[0])
        except (TypeError, ValueError):
            return await _private(interaction, 'Choose one of the loaded games.')
        if game_id not in {game.game_id for game in self.result.games}:
            return await _private(interaction, 'That game is not in this bounded draft.')
        self.scope = 'single'
        self.selected_game_id = game_id
        self.status = f'Selected game {game_id}. Compose a message to continue.'
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _target_changed(self, interaction: discord.Interaction) -> None:
        if not await self.authorize(interaction):
            return
        values = tuple(getattr(self.target_select, 'values', ()) or ())
        if len(values) != 1:
            return await _private(interaction, 'Select exactly one target member.')
        selected = values[0]
        guild_id = getattr(getattr(interaction, 'guild', None), 'id', self.guild_id)
        target = game_ping.target_snapshot(selected, int(guild_id))
        if not self._claim():
            return await _private(
                interaction,
                'Another game-ping action is already in progress. Try again shortly.',
            )
        previous = (self.target, self.result, self.channel_facts, self.selected_game_id, self.scope)
        try:
            await interaction.response.defer(ephemeral=True)
            loaded, facts = await self.target_loader(interaction, target)
            self.target = target
            self.result = loaded
            self.channel_facts = facts
            loaded_ids = {game.game_id for game in loaded.games}
            self.selected_game_id = (
                previous[3]
                if previous[3] in loaded_ids
                else loaded.inferred_game_id
                or (loaded.games[0].game_id if loaded.games else None)
            )
            self.scope = 'single'
            self.draft = None
            self.status = f'Target changed to {target.description}.'
            await self._refresh(interaction)
            await interaction.followup.send(
                'Target changed. The previous message draft was cleared; compose '
                'a fresh notification and review the destinations.',
                ephemeral=True,
            )
        except workers.GamePingValidationError as exc:
            self.target, self.result, self.channel_facts, self.selected_game_id, self.scope = previous
            await _private(interaction, str(exc))
        except Exception:
            self.target, self.result, self.channel_facts, self.selected_game_id, self.scope = previous
            logger.exception('Could not reload game-ping target selection')
            await _private(
                interaction,
                'The target games could not be loaded. The prior private draft '
                'selection was restored.',
            )
        finally:
            self._release()

    async def _compose_clicked(self, interaction: discord.Interaction) -> None:
        if not await self.authorize(interaction):
            return
        if self.scope == 'single' and not self._selected_game_exists():
            return await _private(interaction, 'Choose one loaded game first.')
        if self.scope == 'all' and not self.result.all_scope_allowed:
            return await _private(interaction, 'The all-games scope is not permitted for this target.')
        generation = self.next_modal_generation()
        modal = GamePingComposeModal(self, generation)
        try:
            await interaction.response.send_modal(modal)
        except Exception:
            self.invalidate_modal_generation(generation)
            logger.exception('Could not open the game-ping compose modal')
            await _private(interaction, 'The composer could not be opened. Run `/game ping` again.')

    async def _confirm_clicked(self, interaction: discord.Interaction) -> None:
        if not await self.authorize(interaction):
            return
        if self.draft is None:
            return await _private(interaction, 'Compose a message or attach a file before confirming.')
        if not self._claim():
            return await _private(
                interaction,
                'Another game-ping action is already in progress. Try again shortly.',
            )
        self._confirmations += 1
        self.status = 'Confirming privately; no notification is sent until the database commit succeeds.'
        self.rebuild()
        try:
            await interaction.response.defer(ephemeral=True)
            await _edit_message(self.message, view=self)
            result = await self.confirmer(interaction, self)
        except workers.GamePingValidationError as exc:
            # The exact draft and selection remain intact and the buttons are
            # restored, so a pre-commit failure is safely retryable.
            self.status = f'Not committed: {exc}'
            self._release()
            self.rebuild()
            await _edit_message(self.message, view=self)
            await _private(interaction, str(exc))
            return
        except Exception:
            logger.exception('Unexpected game-ping confirmation failure')
            self.status = 'Not committed: the notification failed before commit; the exact draft is still available.'
            self._release()
            self.rebuild()
            await _edit_message(self.message, view=self)
            await _private(
                interaction,
                'The notification was not committed. The exact draft was '
                'restored; fix the issue or try Confirm again.',
            )
            return

        self.committed = True
        self.status = (
            'Committed and terminal. Public delivery/reconciliation completed '
            'or was recorded below; do not retry.'
        )
        if result.failures:
            self.status += f' {len(result.failures)} destination(s) failed after commit.'
        self.stop()
        self._release()
        self.rebuild()
        await _edit_message(self.message, view=self)

    async def _cancel_clicked(self, interaction: discord.Interaction) -> None:
        if not await self.authorize(interaction):
            return
        if not self._claim():
            return await _private(interaction, 'Another game-ping action is already in progress.')
        try:
            await interaction.response.defer(ephemeral=True)
            self.expired = True
            self.status = 'Cancelled. No database audit or Discord notification was created.'
            self.stop()
            self.rebuild()
            await _edit_message(self.message, view=self)
        finally:
            self._release()

    async def on_timeout(self) -> None:
        # A timeout callback can race a worker-backed Confirm.  Leave the
        # current state alone until that single-flight operation reports its
        # committed/not-committed result; otherwise a committed notification
        # could be presented as merely expired while delivery is still being
        # reconciled.
        if self.committed or self._busy:
            return
        self.expired = True
        self.status = 'Expired. No notification was sent; run `/game ping` again.'
        self.stop()
        self.rebuild()
        await _edit_message(self.message, view=self)
