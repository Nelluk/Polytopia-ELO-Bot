"""Private preview and delivery workspace for the one-time owner notice."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import discord

from modules import components_v2
from modules import operator_guild_console_views as console
from modules import operator_guild_owner_notices as notices


TestRunner = Callable[[Any, str], Awaitable[None]]
DeliveryRunner = Callable[
    [Any, notices.OwnerNoticePlan],
    Awaitable[notices.OwnerNoticeDeliveryResult],
]


class GuildOwnerNoticeWorkspace(components_v2.RequesterLayoutView):
    expired_message = (
        'This owner-notice preview expired. Reopen it from '
        '`/operator guild list`.'
    )

    def __init__(
        self,
        *,
        requester_id: int,
        plan: notices.OwnerNoticePlan,
        completed_owner_ids: Sequence[int],
        test_runner: TestRunner,
        delivery_runner: DeliveryRunner,
        back_runner: console.BackRunner,
        timeout: float = 600.0,
    ):
        super().__init__(requester_id=int(requester_id), timeout=timeout)
        self.plan = plan
        plan_owner_ids = {value.owner_id for value in plan.notices}
        self.completed_owner_ids = {
            int(value) for value in completed_owner_ids
            if int(value) in plan_owner_ids
        }
        self.test_runner = test_runner
        self.delivery_runner = delivery_runner
        self.back_runner = back_runner
        self.busy = False
        self.armed = False
        self.delivery_attempted = False
        self.delivery_result: notices.OwnerNoticeDeliveryResult | None = None
        self.status = (
            'Review the exact messages. No guild owner has been contacted by '
            'opening this preview.'
        )
        self.rebuild()

    @property
    def previews(self) -> tuple[tuple[notices.OwnerNotice, int, str], ...]:
        return tuple(
            (notice, index, message)
            for notice in self.plan.notices
            for index, message in enumerate(notice.messages)
        )

    @property
    def page_count(self) -> int:
        return max(1, len(self.previews))

    @property
    def pending_owner_count(self) -> int:
        return sum(
            notice.owner_id not in self.completed_owner_ids
            for notice in self.plan.notices
        )

    async def ready(self, interaction: Any) -> bool:
        if not await self.authorize(interaction):
            return False
        if self.is_finished():
            await interaction.response.send_message(
                self.expired_message, ephemeral=True,
            )
            return False
        if self.busy:
            await interaction.response.send_message(
                'An owner-notice operation is already running.', ephemeral=True,
            )
            return False
        return True

    async def _previous(self, interaction: Any) -> None:
        if not await self.ready(interaction):
            return
        self.page_index -= 1
        self.armed = False
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _next(self, interaction: Any) -> None:
        if not await self.ready(interaction):
            return
        self.page_index += 1
        self.armed = False
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _send_test(self, interaction: Any) -> None:
        if not await self.ready(interaction):
            return
        preview = self.previews[self.page_index]
        self.busy = True
        self.status = 'Sending this exact preview only to you…'
        self.rebuild()
        await interaction.response.defer()
        await interaction.edit_original_response(view=self)
        try:
            await self.test_runner(interaction, preview[2])
            self.status = (
                'Test delivered only to you. No guild owner was contacted.'
            )
        except Exception:
            self.status = (
                'The test DM failed. No guild owner was contacted.'
            )
        self.busy = False
        self.rebuild()
        await interaction.edit_original_response(view=self)

    async def _send_all(self, interaction: Any) -> None:
        if not await self.ready(interaction):
            return
        if not self.armed:
            self.armed = True
            self.status = (
                'Final review: the red button now sends the previewed campaign '
                'to every pending owner.'
            )
            self.rebuild()
            await interaction.response.edit_message(view=self)
            return
        self.busy = True
        self.delivery_attempted = True
        self.status = 'Revalidating the plan and delivering owner DMs…'
        self.rebuild()
        await interaction.response.defer()
        await interaction.edit_original_response(view=self)
        try:
            result = await self.delivery_runner(interaction, self.plan)
            self.delivery_result = result
            self.completed_owner_ids.update(
                value.owner_id
                for value in result.statuses
                if value.state in {'sent', 'already_sent'}
            )
            self.status = (
                f'Delivery finished: {result.sent_count} sent, '
                f'{result.skipped_count} already sent, '
                f'{result.failed_count} failed.'
            )
        except notices.GuildOwnerNoticeError as exc:
            self.status = str(exc)
        except Exception:
            self.status = (
                'Owner delivery stopped without a trustworthy result. Review '
                'the delivery receipts before retrying.'
            )
        self.busy = False
        self.armed = False
        self.rebuild()
        await interaction.edit_original_response(view=self)

    def rebuild(self) -> None:
        self.clear_items()
        previews = self.previews
        if previews:
            notice, message_index, message = previews[self.page_index]
            guild_names = ', '.join(value.guild_name for value in notice.guilds)
            if len(guild_names) > 500:
                guild_names = guild_names[:499].rstrip() + '…'
            preview = (
                f'## Exact DM preview\n'
                f'**Recipient:** {notice.owner_name} (`{notice.owner_id}`)\n'
                f'**Server(s):** {guild_names}\n'
                f'**Message:** `{message_index + 1}/{len(notice.messages)}`\n\n'
                f'{message}'
            )
        else:
            preview = '## Exact DM preview\n*No recipients are available.*'
        summary = (
            '# Guild-owner update\n'
            f'**Campaign:** `{self.plan.campaign_id}`\n'
            f'**Active servers:** `{self.plan.guild_count}` · '
            f'**Unique owners:** `{self.plan.recipient_count}` · '
            f'**Message parts:** `{self.plan.message_count}`\n'
            f'**Servers with validation findings:** '
            f'`{self.plan.issue_guild_count}` · '
            f'**Owners already sent:** `{len(self.completed_owner_ids)}`\n'
            f'**Plan:** `{self.plan.plan_digest[:12]}`'
        )
        children: list[Any] = [
            discord.ui.TextDisplay(summary),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(preview),
        ]
        previous = discord.ui.Button(
            label='Previous',
            disabled=self.busy or self.page_index == 0,
        )
        previous.callback = self._previous
        page = discord.ui.Button(
            label=f'Preview {self.page_index + 1}/{self.page_count}',
            disabled=True,
        )
        next_page = discord.ui.Button(
            label='Next',
            disabled=self.busy or self.page_index >= self.page_count - 1,
        )
        next_page.callback = self._next
        children.append(discord.ui.ActionRow(previous, page, next_page))
        test = discord.ui.Button(
            label='DM this preview to me',
            disabled=self.busy or not previews,
        )
        test.callback = self._send_test
        send = discord.ui.Button(
            label=(
                f'Send to {self.pending_owner_count} owners'
                if self.armed else 'Review sending'
            ),
            style=(
                discord.ButtonStyle.danger
                if self.armed else discord.ButtonStyle.primary
            ),
            disabled=(
                self.busy
                or self.pending_owner_count == 0
                or self.delivery_attempted
            ),
        )
        send.callback = self._send_all
        children.append(discord.ui.ActionRow(test, send))
        if self.delivery_result is not None and self.delivery_result.failed_count:
            failures = '\n'.join(
                f'- `{value.owner_id}` — {value.detail}'
                for value in self.delivery_result.statuses
                if value.state == 'failed'
            )
            children.append(discord.ui.TextDisplay(
                f'## Manual follow-up required\n{failures}'
            ))
        children.append(discord.ui.ActionRow(
            console.guild_list_back_button(
                self, self.back_runner, disabled=self.busy,
            )
        ))
        children.extend((
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(
                f'**Status:** {discord.utils.escape_markdown(self.status)}\n'
                '-# Test sends go only to the operator. Owner delivery '
                'revalidates the complete plan and never falls back to a '
                'public server message.'
            ),
        ))
        self.add_item(discord.ui.Container(
            *children, accent_colour=components_v2.DEFAULT_ACCENT,
        ))


__all__ = ['GuildOwnerNoticeWorkspace']
