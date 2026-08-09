"""Private native composer for a public Free Agent signup announcement."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging

import discord

from modules import components_v2, league_free_agents as service
from modules import league_free_agents_workers as workers


logger = logging.getLogger('polybot.' + __name__)


async def _private(interaction, content: str) -> None:
    response = interaction.response
    is_done = getattr(response, 'is_done', None)
    if callable(is_done) and is_done():
        await interaction.followup.send(content, ephemeral=True)
    else:
        await response.send_message(content, ephemeral=True)


async def _safe_edit_original(interaction, view) -> bool:
    try:
        await interaction.edit_original_response(view=view)
        return True
    except Exception:
        logger.exception('Could not refresh the private Free Agent draft')
        return False


class FreeAgentPostModal(discord.ui.Modal):
    """One optional long-form addition to the standard signup copy."""

    def __init__(self, view: 'FreeAgentPostView', generation: int):
        super().__init__(title='Free Agent signup announcement', timeout=300.0)
        self.view = view
        self.generation = int(generation)
        self._submitted = False
        self.message_input = discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=workers.MAX_ADDED_MESSAGE_LENGTH,
            default=view.added_message,
            placeholder='Optional dates, deadlines, or draft details',
        )
        self.add_item(discord.ui.Label(
            text='Additional announcement message',
            description='Optional; the standard signup instructions are added automatically.',
            component=self.message_input,
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self._submitted:
            return await _private(interaction, 'This modal was already submitted.')
        if interaction.user.id != self.view.requester_id:
            return await _private(
                interaction,
                'Only the requester can edit this announcement draft.',
            )
        if self.view.expired or self.view.committed or self.view.cancelled:
            return await _private(
                interaction,
                'This draft is no longer active. Run `/league free-agents post` again.',
            )
        if not self.view.is_current_generation(self.generation):
            self._submitted = True
            self.stop()
            return await _private(
                interaction,
                'This modal is stale because a newer editor was opened. Use the newest modal.',
            )
        try:
            added_message = service.normalize_added_message(self.message_input.value)
            # Validate the final Discord message before presenting Confirm.
            service.announcement_content(
                roles=self.view.roles,
                added_message=added_message,
                actor_mention=self.view.actor_mention,
            )
        except workers.FreeAgentPostError as exc:
            self._submitted = True
            self.stop()
            return await _private(interaction, str(exc))

        self._submitted = True
        self.stop()
        self.view.added_message = added_message
        self.view.status = 'Review this private preview, then Confirm once.'
        self.view.rebuild()
        if self.view.message is None:
            await interaction.response.send_message(view=self.view, ephemeral=True)
            try:
                self.view.message = await interaction.original_response()
            except Exception:
                logger.debug('Could not retain initial Free Agent draft message', exc_info=True)
        else:
            await interaction.response.edit_message(view=self.view)

    async def on_timeout(self) -> None:
        self.stop()
        self.view.invalidate_generation(self.generation)

    async def on_error(self, interaction, error, item) -> None:
        self.stop()
        self.view.invalidate_generation(self.generation)
        logger.error(
            'Free Agent post modal failed',
            exc_info=(type(error), error, getattr(error, '__traceback__', None)),
        )


class FreeAgentPostView(discord.ui.LayoutView):
    """Requester-bound preview with explicit edit/confirm/cancel controls."""

    def __init__(
        self,
        *,
        requester_id: int,
        actor_mention: str,
        channel,
        roles: service.FreeAgentRoleSnapshot,
        confirmer: Callable[[discord.Interaction, 'FreeAgentPostView'], Awaitable[service.FreeAgentPostResult]],
        timeout: float = 300.0,
    ):
        super().__init__(timeout=timeout)
        self.requester_id = int(requester_id)
        self.actor_mention = str(actor_mention)
        self.channel = channel
        self.roles = roles
        self.confirmer = confirmer
        self.added_message = ''
        self.status = 'Enter optional details, then review and confirm.'
        self.message = None
        self.expired = False
        self.committed = False
        self.cancelled = False
        self.busy = False
        self._generation = 0
        self.rebuild()

    def next_generation(self) -> int:
        self._generation += 1
        return self._generation

    def is_current_generation(self, generation: int) -> bool:
        return int(generation) == self._generation

    def invalidate_generation(self, generation: int) -> None:
        if self.is_current_generation(generation):
            self._generation += 1

    async def authorize(self, interaction) -> bool:
        if int(interaction.user.id) == self.requester_id:
            return True
        await _private(interaction, 'Only the requester can control this draft.')
        return False

    async def _ready(self, interaction) -> bool:
        if not await self.authorize(interaction):
            return False
        if self.expired or self.is_finished():
            await _private(
                interaction,
                'This draft expired. Run `/league free-agents post` again.',
            )
            return False
        if self.committed or self.cancelled:
            await _private(interaction, 'This draft is already finished.')
            return False
        return True

    async def _edit(self, interaction) -> None:
        if not await self._ready(interaction):
            return
        generation = self.next_generation()
        try:
            await interaction.response.send_modal(FreeAgentPostModal(self, generation))
        except Exception:
            self.invalidate_generation(generation)
            logger.exception('Could not open Free Agent announcement modal')
            await _private(
                interaction,
                'The message editor could not be opened. Try again.',
            )

    async def _cancel(self, interaction) -> None:
        if not await self._ready(interaction):
            return
        self.cancelled = True
        self.status = 'Draft cancelled. Nothing was posted or saved.'
        self.rebuild()
        await interaction.response.edit_message(view=self)
        self.stop()

    async def _confirm(self, interaction) -> None:
        if not await self._ready(interaction):
            return
        if self.busy:
            return await _private(
                interaction,
                'This announcement is already being posted.',
            )
        self.busy = True
        self.status = 'Posting the announcement and seeding reactions…'
        self.rebuild()
        await interaction.response.defer()
        try:
            result = await self.confirmer(interaction, self)
        except workers.FreeAgentPostError as exc:
            self.busy = False
            self.status = 'Posting failed safely. Review the message and retry if instructed.'
            self.rebuild()
            await _safe_edit_original(interaction, self)
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception:
            self.busy = False
            self.status = 'Posting failed unexpectedly. No retry should be attempted until staff checks the logs.'
            self.rebuild()
            logger.exception('Unexpected Free Agent post failure')
            await _safe_edit_original(interaction, self)
            await interaction.followup.send(
                'The signup post failed unexpectedly. Ask staff to check '
                'the bot logs before retrying.',
                ephemeral=True,
            )
            return

        self.committed = True
        self.busy = False
        self.status = f'Posted and activated: {result.message_link}'
        self.rebuild()
        updated = await _safe_edit_original(interaction, self)
        if not updated:
            await interaction.followup.send(
                f'The signup was posted and activated: {result.message_link}',
                ephemeral=True,
            )
        self.stop()

    def rebuild(self) -> None:
        self.clear_items()
        preview = service.preview_content(
            channel=self.channel,
            roles=self.roles,
            added_message=self.added_message,
            actor_mention=self.actor_mention,
        )
        children = [
            discord.ui.TextDisplay(preview),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(f'-# {self.status}'),
        ]
        if not self.committed and not self.cancelled and not self.expired:
            edit = discord.ui.Button(label='Edit message', disabled=self.busy)
            edit.callback = self._edit
            confirm = discord.ui.Button(
                label='Confirm post',
                style=discord.ButtonStyle.success,
                disabled=self.busy,
            )
            confirm.callback = self._confirm
            cancel = discord.ui.Button(
                label='Cancel',
                style=discord.ButtonStyle.danger,
                disabled=self.busy,
            )
            cancel.callback = self._cancel
            children.append(discord.ui.ActionRow(edit, confirm, cancel))
        self.add_item(discord.ui.Container(
            *children,
            accent_colour=components_v2.DEFAULT_ACCENT,
        ))

    async def on_timeout(self) -> None:
        self.expired = True
        self.status = 'Draft expired. Run `/league free-agents post` again.'
        self.rebuild()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


async def open_initial_modal(interaction, view: FreeAgentPostView) -> None:
    generation = view.next_generation()
    try:
        await interaction.response.send_modal(FreeAgentPostModal(view, generation))
    except Exception:
        view.invalidate_generation(generation)
        raise
