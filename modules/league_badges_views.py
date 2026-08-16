"""Requester-bound Components v2 badge selector and confirmation draft."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging

import discord

from modules import components_v2, league_badges, league_badges_workers as workers


logger = logging.getLogger('polybot.' + __name__)


class BadgeDraftWorkspace(components_v2.RequesterLayoutView):
    unauthorized_message = 'Only the Mod who opened this badge draft can use it.'

    def __init__(
        self,
        *,
        requester_id: int,
        guild_id: int,
        draft: league_badges.BadgeDraft,
        runner: Callable[[discord.Interaction, tuple[int, ...]], Awaitable[workers.BadgeMutationResult]],
        timeout: float = 300.0,
    ):
        super().__init__(requester_id=int(requester_id), timeout=timeout)
        self.guild_id = int(guild_id)
        self.draft = draft
        self.runner = runner
        self.recipient_ids: tuple[int, ...] = ()
        self.recipient_labels: tuple[str, ...] = ()
        self.status = 'Select 1–25 registered members.'
        self.terminal = False
        self.running = False
        self.rebuild()

    @property
    def page_count(self) -> int:
        return 1

    async def _private(self, interaction, content: str):
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)

    async def _ready(self, interaction) -> bool:
        if not await self.authorize(interaction):
            return False
        if self.terminal or self.is_finished():
            await self._private(
                interaction,
                'This badge draft expired. Rerun `/league badge '
                f'{self.draft.operation}`.',
            )
            return False
        if int(getattr(interaction, 'guild_id', 0) or 0) != self.guild_id:
            await self._private(interaction, 'This draft belongs to another guild.')
            return False
        error = league_badges.access_error(interaction.user, self.guild_id)
        if error:
            await self._private(interaction, error)
            return False
        return True

    async def _select(self, interaction) -> None:
        if not await self._ready(interaction):
            return
        members = tuple(self.member_select.values)
        ids = tuple(int(member.id) for member in members)
        if not 1 <= len(ids) <= workers.MAX_TARGETS or len(set(ids)) != len(ids):
            await self._private(interaction, 'Select between 1 and 25 unique members.')
            return
        self.recipient_ids = ids
        self.recipient_labels = tuple(
            league_badges.safe_text(
                getattr(member, 'display_name', None)
                or getattr(member, 'name', None)
                or f'user-{member.id}'
            )
            for member in members
        )
        self.status = 'Review the exact badge and recipients, then confirm.'
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _cancel(self, interaction) -> None:
        if not await self._ready(interaction):
            return
        self.terminal = True
        self.status = 'Cancelled. No badge was changed.'
        self.rebuild()
        await interaction.response.edit_message(view=self)
        self.stop()

    async def _confirm(self, interaction) -> None:
        if not await self._ready(interaction):
            return
        if self.running:
            await self._private(interaction, 'This badge draft is already running.')
            return
        if not self.recipient_ids:
            await self._private(interaction, 'Select at least one member first.')
            return
        self.running = True
        self.status = 'Committing one atomic badge transaction…'
        self.rebuild()
        await interaction.response.defer(ephemeral=True)
        try:
            result = await self.runner(interaction, self.recipient_ids)
        except workers.BadgeValidationError as exc:
            self.running = False
            invalid = (
                '\nInvalid recipients: '
                + ' '.join(f'<@{value}>' for value in exc.invalid_recipient_ids)
                if exc.invalid_recipient_ids else ''
            )
            self.status = 'No badge was changed.'
            self.rebuild()
            await interaction.edit_original_response(view=self)
            await interaction.followup.send(
                f'{exc}{invalid}', ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        except workers.BadgeError as exc:
            self.running = False
            self.status = 'No badge was changed.'
            self.rebuild()
            await interaction.edit_original_response(view=self)
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except league_badges.BadgePublicationError as exc:
            self.running = False
            self.terminal = True
            self.status = f'Reconciliation required: {exc}'
            self.rebuild()
            await interaction.edit_original_response(view=self)
            self.stop()
            return
        except Exception:
            logger.exception('Badge transaction failed before a result was returned')
            self.running = False
            self.status = (
                'No successful change is being claimed. An operator should '
                'inspect the database log before retrying.'
            )
            self.rebuild()
            await interaction.edit_original_response(view=self)
            return

        self.running = False
        self.terminal = True
        self.status = (
            f'Complete: {result.changed_count} changed; '
            f'{result.unchanged_count} unchanged; public result posted.'
        )
        self.rebuild()
        await interaction.edit_original_response(view=self)
        self.stop()

    def rebuild(self) -> None:
        self.clear_items()
        recipients = (
            '\n'.join(
                f'- <@{discord_id}> — {label}'
                for discord_id, label in zip(
                    self.recipient_ids,
                    self.recipient_labels,
                    strict=True,
                )
            )
            if self.recipient_ids else '*No recipients selected yet.*'
        )
        operation = self.draft.operation.title()
        children = [
            discord.ui.TextDisplay(
                f'# {operation} player badge\n'
                f'**Exact badge:** {league_badges.safe_badge(self.draft.badge)}\n'
                f'**Recipients ({len(self.recipient_ids)}):**\n{recipients}\n\n'
                f'**Status:** {league_badges.safe_text(self.status)}'
            ),
        ]
        if not self.terminal:
            self.member_select = discord.ui.UserSelect(
                placeholder='Choose 1–25 members',
                min_values=1,
                max_values=25,
                disabled=self.running,
            )
            self.member_select.callback = self._select
            children.append(discord.ui.ActionRow(self.member_select))
            confirm = discord.ui.Button(
                label=f'Confirm {self.draft.operation}',
                style=discord.ButtonStyle.danger,
                disabled=self.running or not self.recipient_ids,
            )
            confirm.callback = self._confirm
            cancel = discord.ui.Button(
                label='Cancel',
                style=discord.ButtonStyle.secondary,
                disabled=self.running,
            )
            cancel.callback = self._cancel
            children.append(discord.ui.ActionRow(confirm, cancel))
        children.append(discord.ui.TextDisplay(
            '-# This private draft is requester-bound. On timeout, rerun '
            f'`/league badge {self.draft.operation}`.'
        ))
        self.add_item(discord.ui.Container(
            *children,
            accent_colour=components_v2.DEFAULT_ACCENT,
        ))

    async def on_timeout(self) -> None:
        self.terminal = True
        self.status = (
            'Expired. No new submission is accepted; rerun '
            f'`/league badge {self.draft.operation}`.'
        )
        self.rebuild()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def publish_private(interaction, view: BadgeDraftWorkspace):
    message = await interaction.followup.send(
        view=view,
        ephemeral=True,
        wait=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    view.message = message
    return message
