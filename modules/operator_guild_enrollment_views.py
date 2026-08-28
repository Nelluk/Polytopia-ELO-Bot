"""Private digest-bound confirmation for quarantined guild enrollment."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
from typing import Any

import discord

from modules import components_v2
from modules import guild_types
from modules import operator_guild_enrollment_workers as workers


Runner = Callable[..., Awaitable[workers.GuildEnrollmentResult]]
logger = logging.getLogger('polybot.' + __name__)


async def _private(interaction: Any, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


class GuildEnrollmentModal(discord.ui.Modal):
    def __init__(self, workspace: 'GuildEnrollmentWorkspace'):
        self.workspace = workspace
        self.expected = workspace.preview.confirmation
        super().__init__(
            title=(
                'Update guild'
                if workspace.preview.existing else 'Enroll guild'
            ),
            timeout=180.0,
        )
        self.confirmation = discord.ui.TextInput(
            placeholder=self.expected,
            required=True,
            min_length=len(self.expected),
            max_length=len(self.expected),
        )
        self.add_item(discord.ui.Label(
            text=(
                'Type UPDATE GUILD, guild ID, and full digest'
                if workspace.preview.existing
                else 'Type ENROLL, guild ID, and full digest'
            ),
            description=(
                'Creates a new immutable configuration revision.'
                if workspace.preview.existing
                else 'Creates the first immutable configuration revision.'
            ),
            component=self.confirmation,
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if str(self.confirmation.value) != self.expected:
            return await _private(interaction, f'Type `{self.expected}` exactly.')
        if not await self.workspace.ready(interaction):
            return
        await self.workspace.commit(interaction, str(self.confirmation.value))


class GuildEnrollmentWorkspace(components_v2.RequesterLayoutView):
    expired_message = (
        'This enrollment preview expired. Run `/operator guild enroll` again.'
    )

    def __init__(
        self,
        *,
        requester_id: int,
        result: workers.GuildEnrollmentResult,
        runner: Runner,
        timeout: float = 300.0,
    ):
        super().__init__(requester_id=int(requester_id), timeout=timeout)
        self.result = result
        self.preview = result.preview
        self.runner = runner
        self.busy = False
        self.terminal = False
        self.status = 'Review the exact target, server type, and ranking policy.'
        self.rebuild()

    @property
    def page_count(self) -> int:
        return 1

    async def ready(self, interaction: Any) -> bool:
        if not await self.authorize(interaction):
            return False
        if self.terminal or self.is_finished():
            await _private(interaction, self.expired_message)
            return False
        if self.busy:
            await _private(interaction, 'This enrollment is already running.')
            return False
        return True

    async def _confirm(self, interaction: Any) -> None:
        if await self.ready(interaction):
            await interaction.response.send_modal(GuildEnrollmentModal(self))

    async def _cancel(self, interaction: Any) -> None:
        if not await self.ready(interaction):
            return
        self.terminal = True
        self.status = (
            'Cancelled. Active configuration remains unchanged.'
            if self.preview.existing else
            'Cancelled. The target remains quarantined and unconfigured.'
        )
        self.rebuild()
        await interaction.response.edit_message(view=self)
        self.stop()

    async def _publish_terminal(self, interaction: Any) -> None:
        try:
            await interaction.edit_original_response(view=self)
        except Exception:
            logger.exception('Could not update committed guild-enrollment panel')
            try:
                await interaction.followup.send(self.status, ephemeral=True)
            except Exception:
                logger.exception(
                    'Could not send committed guild-enrollment reconciliation'
                )

    async def commit(self, interaction: Any, confirmation_text: str) -> None:
        self.busy = True
        self.status = (
            'Revalidating and committing a new configuration revision…'
            if self.preview.existing else
            'Revalidating and committing first configuration revision…'
        )
        self.rebuild()
        await interaction.response.defer()
        await interaction.edit_original_response(view=self)
        try:
            result = await self.runner(
                interaction,
                target_guild_id=self.preview.guild_id,
                template=self.preview.template,
                guild_type=self.preview.guild_type,
                include_in_global_leaderboard=(
                    self.preview.document.visibility.include_in_global_leaderboard
                ),
                operation=workers.COMMIT,
                expected_document_digest=self.preview.document_digest,
                confirmation_text=confirmation_text,
            )
        except workers.OperatorGuildEnrollmentCommitted as exc:
            self.busy = False
            self.terminal = True
            self.status = str(exc)
            self.rebuild()
            await self._publish_terminal(interaction)
            self.stop()
            return
        except workers.OperatorGuildEnrollmentError as exc:
            self.busy = False
            self.status = str(exc)
            self.rebuild()
            await interaction.edit_original_response(view=self)
            return
        except Exception:
            self.busy = False
            self.status = (
                'Enrollment stopped without a trustworthy result. Inspect logs '
                'before retrying.'
            )
            self.rebuild()
            await interaction.edit_original_response(view=self)
            return
        self.result = result
        self.busy = False
        self.terminal = True
        mutation = result.enrollment
        assert mutation is not None
        self.status = (
            f'{"Enrolled" if mutation.created else "Updated"} and published at '
            f'revision {mutation.revision} / generation {mutation.generation}. '
            'Discord commands were not synchronized; reopen this server from '
            '`/operator guild list` and choose **Repair commands** when ready.'
        )
        self.rebuild()
        await self._publish_terminal(interaction)
        self.stop()

    def rebuild(self) -> None:
        self.clear_items()
        preview = self.preview
        permissions = ', '.join(preview.bot_permissions)
        confirm = discord.ui.Button(
            label='Update guild' if preview.existing else 'Enroll guild',
            style=discord.ButtonStyle.danger,
            disabled=self.busy or self.terminal,
        )
        confirm.callback = self._confirm
        cancel = discord.ui.Button(
            label='Cancel',
            disabled=self.busy or self.terminal,
        )
        cancel.callback = self._cancel
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(
                f'# {"Update enrolled guild" if preview.existing else "Enroll quarantined guild"}\n'
                f'**Target:** {discord.utils.escape_markdown(preview.guild_name)} '
                f'(`{preview.guild_id}`)\n'
                f'**Server type:** {guild_types.TYPE_LABELS[preview.guild_type]}\n'
                '**Global leaderboard:** '
                f'{"Enabled" if preview.document.visibility.include_in_global_leaderboard else "Disabled"}\n'
                f'**Document digest:** `{preview.document_digest}`\n'
                f'**Observed bot permissions:** `{permissions}`\n\n'
                + (
                    '- Existing side-size, role, channel, and destination settings '
                    'are preserved.\n'
                    if preview.existing else
                    '- Everyone starts at ordinary user level 2; staff roles, '
                    'destinations, and channel restrictions are disabled.\n'
                )
                + '- Command groups are derived from the selected type. Squads '
                'remain available without persistent Teams.\n'
                '- This operation does not synchronize Discord commands.\n\n'
                f'-# {self.status}'
            ),
            discord.ui.ActionRow(confirm, cancel),
            accent_colour=discord.Colour.orange(),
        ))


async def publish_private(interaction: Any, view: GuildEnrollmentWorkspace):
    message = await interaction.followup.send(
        view=view,
        ephemeral=True,
        wait=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    view.message = message
    return message


__all__ = [
    'GuildEnrollmentModal',
    'GuildEnrollmentWorkspace',
    'publish_private',
]
