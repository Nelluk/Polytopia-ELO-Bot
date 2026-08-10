"""Private preview/confirmation view for player migration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import discord

from modules import operator_player_migration_workers as workers


ACCENT = discord.Colour.orange()


class PlayerMigrationPreviewView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        requester_id: int,
        preview: workers.PlayerMigrationPreview,
        confirmer: Callable[[discord.Interaction, workers.PlayerMigrationPreview], Awaitable[None]],
        timeout: float = 300.0,
    ):
        super().__init__(timeout=timeout)
        self.requester_id = int(requester_id)
        self.preview = preview
        self.confirmer = confirmer
        self.message = None
        self.busy = False
        self.finished = False
        self.status = 'Review the complete migration graph before confirming.'
        self.rebuild()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) == self.requester_id:
            return True
        await interaction.response.send_message(
            'Only the requesting superuser can control this preview.',
            ephemeral=True,
        )
        return False

    def rebuild(self) -> None:
        self.clear_items()
        guild_lines = []
        for row in self.preview.guilds:
            guild_lines.append(
                f'- `{row.guild_id}`: {row.disposition}; destination deps '
                f'G{row.incomplete_games}/L{row.lineups}/H{row.hosted_games}/'
                f'S{row.squad_memberships}/'
                f'P{row.house_preferences}/B{row.bids}'
            )
        metadata = ', '.join(self.preview.destination_metadata) or 'none'
        blockers = (
            '\n'.join(f'- {value}' for value in self.preview.blockers)
            if self.preview.blockers else '- None'
        )
        confirm = discord.ui.Button(
            label='Confirm migration',
            style=discord.ButtonStyle.danger,
            disabled=self.busy or self.finished or bool(self.preview.blockers),
        )
        confirm.callback = self._confirm
        cancel = discord.ui.Button(
            label='Cancel',
            style=discord.ButtonStyle.secondary,
            disabled=self.busy or self.finished,
        )
        cancel.callback = self._cancel
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(
                '# Player migration preview\n'
                f'**Source:** {discord.utils.escape_markdown(self.preview.source_name)} '
                f'`{self.preview.source_id}`\n'
                f'**Destination:** {discord.utils.escape_markdown(self.preview.destination_name)} '
                f'`{self.preview.destination_id}`\n'
                f'**Existing destination identity:** {self.preview.destination_exists}\n'
                f'**Destination completed games:** {self.preview.destination_completed_games}\n'
                f'**Destination metadata that will not be merged:** {metadata}\n'
                '**Retained:** source account identity/rating history; only '
                'the destination Discord ID and current Discord name replace it.'
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(
                '**Per-server graph**\n' + ('\n'.join(guild_lines) or '- None')
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay('**Blocking conflicts**\n' + blockers),
            discord.ui.TextDisplay(f'-# {self.status}'),
            discord.ui.ActionRow(confirm, cancel),
            accent_colour=ACCENT,
        ))

    async def _confirm(self, interaction: discord.Interaction) -> None:
        if self.busy or self.finished:
            return await interaction.response.send_message(
                'This migration is already being handled.', ephemeral=True
            )
        self.busy = True
        self.status = 'Revalidating and committing the migration…'
        self.rebuild()
        await interaction.response.edit_message(view=self)
        try:
            await self.confirmer(interaction, self.preview)
        except workers.PlayerMigrationError as exc:
            self.busy = False
            self.status = str(exc)
            self.rebuild()
            return await interaction.edit_original_response(view=self)
        except Exception:
            self.busy = False
            self.status = (
                'Migration failed before a confirmed result. Run the command '
                'again; if the database committed, reconcile before retrying.'
            )
            self.rebuild()
            return await interaction.edit_original_response(view=self)
        self.finished = True
        self.busy = False
        self.stop()

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self.finished = True
        self.status = 'Migration cancelled. No database changes were made.'
        self.rebuild()
        self.stop()
        await interaction.response.edit_message(view=self)

    async def on_timeout(self) -> None:
        self.finished = True
        self.status = 'This preview expired. Run the command again.'
        self.rebuild()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
