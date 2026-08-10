"""Private inventory and typed confirmation for orphan player deletion."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import discord

from modules import operator_player_deletion_workers as workers


ACCENT = discord.Colour.red()


def _bounded(lines: list[str], *, limit: int = 3500) -> str:
    text = '\n'.join(lines) or '- None'
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + '\n- … (truncated)'


class PlayerDeletionConfirmationModal(
    discord.ui.Modal,
    title='Confirm orphan player deletion',
):
    confirmation = discord.ui.TextInput(
        label='Type the exact confirmation shown',
        placeholder='DELETE 123456789012345678',
        required=True,
        max_length=32,
    )

    def __init__(self, parent: 'PlayerDeletionPreviewView'):
        super().__init__(timeout=180.0)
        self.parent_view = parent
        self.confirmation.placeholder = f'DELETE {parent.preview.target_id}'

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.parent_view.submit_confirmation(
            interaction,
            str(self.confirmation.value),
        )


class PlayerDeletionPreviewView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        requester_id: int,
        preview: workers.PlayerDeletionPreview,
        confirmer: Callable[
            [discord.Interaction, workers.PlayerDeletionPreview, str],
            Awaitable[None],
        ],
        timeout: float = 300.0,
    ):
        super().__init__(timeout=timeout)
        self.requester_id = int(requester_id)
        self.preview = preview
        self.confirmer = confirmer
        self.message = None
        self.busy = False
        self.finished = False
        self.status = (
            f'To delete, press Delete and type `DELETE {preview.target_id}`.'
        )
        self.rebuild()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) == self.requester_id:
            return True
        await interaction.response.send_message(
            'Only the requesting bot owner can control this preview.',
            ephemeral=True,
        )
        return False

    def rebuild(self) -> None:
        self.clear_items()
        player_lines = []
        for row in self.preview.players:
            attributes = []
            if row.team_id is not None:
                attributes.append(f'team={row.team_id}')
            if row.nick:
                attributes.append(f'nick={row.nick}')
            if row.rating_summary:
                attributes.append(','.join(row.rating_summary))
            if row.trophies_present:
                attributes.append('trophies')
            if row.is_banned:
                attributes.append('banned')
            attribute_text = '; '.join(attributes) or 'default metadata'
            player_lines.append(
                f'- guild `{row.guild_id}` / Player `{row.player_id}`: '
                f'{discord.utils.escape_markdown(row.name)}; {attribute_text}; '
                f'S{row.squad_memberships}/P{row.house_preferences}/'
                f'L{row.lineups}/H{row.hosted_games}/B{row.bid_references}'
            )
        blocker_lines = [f'- {value}' for value in self.preview.blockers]
        warning_lines = [f'- {value}' for value in self.preview.warnings]
        metadata = ', '.join(self.preview.account_metadata) or 'none'
        ratings = ', '.join(self.preview.global_rating_summary) or 'all defaults'

        delete = discord.ui.Button(
            label='Delete identity',
            style=discord.ButtonStyle.danger,
            disabled=self.busy or self.finished or bool(self.preview.blockers),
        )
        delete.callback = self._open_confirmation
        cancel = discord.ui.Button(
            label='Cancel',
            style=discord.ButtonStyle.secondary,
            disabled=self.busy or self.finished,
        )
        cancel.callback = self._cancel
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(
                '# Orphan player deletion preview\n'
                f'**Stored identity:** '
                f'{discord.utils.escape_markdown(self.preview.target_name)} '
                f'`{self.preview.target_id}`\n'
                f'**Account metadata:** {metadata}\n'
                f'**Global rating history:** {ratings}\n'
                f'**Explicit deletion graph:** {self.preview.player_count} '
                f'Player row(s), {self.preview.squad_membership_count} squad '
                f'membership(s), {self.preview.house_preference_count} House '
                'preference(s).'
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(
                '**Per-guild inventory**\n' + _bounded(player_lines)
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(
                '**Blocking references**\n' + _bounded(blocker_lines)
            ),
            discord.ui.TextDisplay(
                '**Deletion warnings**\n' + _bounded(warning_lines)
            ),
            discord.ui.TextDisplay(
                '**Privacy boundary:** this removes only the reviewed database '
                'identity graph. Audit/support records, sheets, logs, and '
                'backups require the separate privacy runbook.\n'
                f'-# {self.status}'
            ),
            discord.ui.ActionRow(delete, cancel),
            accent_colour=ACCENT,
        ))

    async def _edit(self) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass

    async def _open_confirmation(self, interaction: discord.Interaction) -> None:
        if self.busy or self.finished:
            return await interaction.response.send_message(
                'This deletion preview is already being handled.',
                ephemeral=True,
            )
        await interaction.response.send_modal(
            PlayerDeletionConfirmationModal(self)
        )

    async def submit_confirmation(
        self,
        interaction: discord.Interaction,
        confirmation_text: str,
    ) -> None:
        if int(interaction.user.id) != self.requester_id:
            return await interaction.response.send_message(
                'Only the requesting bot owner can confirm this deletion.',
                ephemeral=True,
            )
        if self.busy or self.finished:
            return await interaction.response.send_message(
                'This deletion preview is already being handled.',
                ephemeral=True,
            )
        expected = f'DELETE {self.preview.target_id}'
        if confirmation_text != expected:
            return await interaction.response.send_message(
                f'Type exactly `{expected}`. No database changes were made.',
                ephemeral=True,
            )

        self.busy = True
        self.status = 'Revalidating and committing the deletion…'
        self.rebuild()
        await interaction.response.defer(ephemeral=True)
        await self._edit()
        try:
            await self.confirmer(
                interaction,
                self.preview,
                confirmation_text,
            )
        except workers.PlayerDeletionError as exc:
            self.busy = False
            self.status = str(exc)
            self.rebuild()
            await self._edit()
            return await interaction.followup.send(str(exc), ephemeral=True)
        except Exception:
            self.busy = False
            self.status = (
                'Deletion failed before a confirmed result. Run the command '
                'again; if the database committed, reconcile before retrying.'
            )
            self.rebuild()
            await self._edit()
            return await interaction.followup.send(self.status, ephemeral=True)

        self.finished = True
        self.busy = False
        self.status = 'Deletion committed. This preview is now closed.'
        self.rebuild()
        self.stop()
        await self._edit()

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self.finished = True
        self.status = 'Deletion cancelled. No database changes were made.'
        self.rebuild()
        self.stop()
        await interaction.response.edit_message(view=self)

    async def on_timeout(self) -> None:
        self.finished = True
        self.status = 'This preview expired. Run the command again.'
        self.rebuild()
        self.stop()
        await self._edit()
