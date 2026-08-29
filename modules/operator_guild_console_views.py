"""Owner-only Components v2 console for enrolled guild management."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import math
from typing import Any

import discord

from modules import components_v2
from modules import guild_types
from modules import operator_guild_configuration_workers as workers


PAGE_SIZE = 15
VALIDATE = 'validate'
HISTORY = 'history'
SUSPEND = 'suspend'
RESUME = 'resume'
MANAGERS = 'managers'
COMMANDS = 'commands'

ActionRunner = Callable[[Any, str, int], Awaitable[None]]
RollbackRunner = Callable[[Any, int, int], Awaitable[None]]
BackRunner = Callable[[Any], Awaitable[None]]
NoticeRunner = Callable[[Any], Awaitable[None]]


def _escape(value: Any) -> str:
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value or ''))
    )


def _trim(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit - 1].rstrip() + '…'


def guild_list_back_button(
    view: components_v2.RequesterLayoutView,
    runner: BackRunner,
    *,
    disabled: bool = False,
) -> discord.ui.Button:
    """Build a requester-bound button that reloads the owner guild console."""

    button = discord.ui.Button(
        label='Back to server list',
        disabled=disabled,
    )

    async def callback(interaction: Any) -> None:
        if not await view.authorize(interaction):
            return
        if bool(getattr(view, 'busy', False)):
            return await interaction.response.send_message(
                'Wait for the current server action to finish.', ephemeral=True,
            )
        await runner(interaction)

    button.callback = callback
    return button


class GuildRegistryConsole(components_v2.RequesterLayoutView):
    """Select one enrolled guild, then open a target-bound workflow."""

    expired_message = (
        'This server-management console expired. Run `/operator guild list` '
        'again for current data.'
    )

    def __init__(
        self,
        *,
        requester_id: int,
        result: workers.GuildConfigurationReadResult,
        runner: ActionRunner,
        notice_runner: NoticeRunner | None = None,
        timeout: float = 600.0,
    ):
        super().__init__(requester_id=int(requester_id), timeout=timeout)
        if result.operation != workers.LIST:
            raise ValueError('The guild console requires a registry result.')
        self.result = result
        self.records = tuple(result.records)
        self.runner = runner
        self.notice_runner = notice_runner
        self.selected_guild_id: int | None = None
        self.busy = False
        self.status = 'Select a server to inspect or manage.'
        self.rebuild()

    @property
    def page_count(self) -> int:
        return max(1, math.ceil(len(self.records) / PAGE_SIZE))

    @property
    def selected(self) -> workers.GuildConfigurationRecord | None:
        return next(
            (
                record for record in self.records
                if record.guild_id == self.selected_guild_id
            ),
            None,
        )

    @property
    def page_records(self) -> tuple[workers.GuildConfigurationRecord, ...]:
        start = self.page_index * PAGE_SIZE
        return self.records[start:start + PAGE_SIZE]

    async def _select(self, interaction: Any, select: discord.ui.Select) -> None:
        if not await self.authorize(interaction):
            return
        self.selected_guild_id = int(select.values[0])
        self.status = 'Choose an action for the selected server.'
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _previous(self, interaction: Any) -> None:
        if not await self.authorize(interaction):
            return
        self.page_index -= 1
        self.selected_guild_id = None
        self.status = 'Select a server to inspect or manage.'
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _next(self, interaction: Any) -> None:
        if not await self.authorize(interaction):
            return
        self.page_index += 1
        self.selected_guild_id = None
        self.status = 'Select a server to inspect or manage.'
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _action(self, interaction: Any, action: str) -> None:
        if not await self.authorize(interaction):
            return
        selected = self.selected
        if selected is None:
            return await interaction.response.send_message(
                'Select an enrolled server first.', ephemeral=True,
            )
        if self.busy:
            return await interaction.response.send_message(
                'Another server action is already opening.', ephemeral=True,
            )
        self.busy = True
        try:
            await self.runner(interaction, action, selected.guild_id)
        finally:
            self.busy = False

    async def _owner_notices(self, interaction: Any) -> None:
        if not await self.authorize(interaction):
            return
        if self.notice_runner is None:
            return await interaction.response.send_message(
                'The owner-update workflow is unavailable.', ephemeral=True,
            )
        if self.busy:
            return await interaction.response.send_message(
                'Another server action is already opening.', ephemeral=True,
            )
        self.busy = True
        try:
            await self.notice_runner(interaction)
        finally:
            self.busy = False

    def _button(
        self,
        label: str,
        action: str,
        *,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        disabled: bool = False,
    ) -> discord.ui.Button:
        button = discord.ui.Button(
            label=label,
            style=style,
            disabled=self.busy or disabled,
        )
        button.callback = lambda interaction: self._action(interaction, action)
        return button

    def rebuild(self) -> None:
        self.clear_items()
        records = self.page_records
        state_icons = {
            'active': '🟢',
            'suspended': '⏸️',
            'pending': '🟡',
            'retired': '⛔',
        }
        lines = []
        for record in records:
            guild_type = (
                guild_types.label_for_document(record.document)
                if record.document is not None else 'Unconfigured'
            )
            marker = '▶️' if record.guild_id == self.selected_guild_id else '•'
            lines.append(
                f'{marker} {state_icons[record.enrollment_state]} '
                f'**{_escape(record.display_name)}** · {guild_type} · '
                f'`{record.guild_id}`'
            )
        if not lines:
            lines.append('*No enrolled servers.*')

        selected = self.selected
        selected_text = ''
        if selected is not None:
            revision = selected.active_revision or '—'
            selected_text = (
                '\n\n## Selected server\n'
                f'**{_escape(selected.display_name)}** (`{selected.guild_id}`)\n'
                f'State: **{selected.enrollment_state}** · '
                f'Revision: `{revision}` · Generation: `{selected.generation}`'
            )

        children: list[Any] = [
            discord.ui.TextDisplay(
                '# PolyElo server management\n'
                f'Page `{self.page_index + 1}/{self.page_count}` · '
                f'`{len(self.records)}` enrolled servers\n\n'
                + '\n'.join(lines)
                + selected_text
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
        ]

        if records:
            options = []
            for record in records:
                guild_type = (
                    guild_types.label_for_document(record.document)
                    if record.document is not None else 'Unconfigured'
                )
                options.append(discord.SelectOption(
                    label=_trim(record.display_name, 100),
                    value=str(record.guild_id),
                    description=_trim(
                        f'{record.enrollment_state.title()} · {guild_type} · '
                        f'{record.guild_id}',
                        100,
                    ),
                    default=record.guild_id == self.selected_guild_id,
                ))
            select = discord.ui.Select(
                placeholder='Select a server from this page',
                options=options,
                min_values=1,
                max_values=1,
                disabled=self.busy,
            )
            select.callback = lambda interaction: self._select(interaction, select)
            children.append(discord.ui.ActionRow(select))

        previous = discord.ui.Button(
            label='Previous', disabled=self.busy or self.page_index == 0,
        )
        previous.callback = self._previous
        page = discord.ui.Button(
            label=f'Page {self.page_index + 1}/{self.page_count}', disabled=True,
        )
        next_page = discord.ui.Button(
            label='Next',
            disabled=self.busy or self.page_index >= self.page_count - 1,
        )
        next_page.callback = self._next
        owner_notices = discord.ui.Button(
            label='Owner notices',
            disabled=self.busy or self.notice_runner is None,
        )
        owner_notices.callback = self._owner_notices
        children.append(discord.ui.ActionRow(
            previous, page, next_page, owner_notices,
        ))

        active = selected is not None and selected.enrollment_state == 'active'
        suspended = (
            selected is not None and selected.enrollment_state == 'suspended'
        )
        children.append(discord.ui.ActionRow(
            self._button('Validate', VALIDATE, disabled=not active),
            self._button(
                'History', HISTORY,
                disabled=selected is None or selected.active_revision is None,
            ),
            self._button(
                'Resume' if suspended else 'Suspend',
                RESUME if suspended else SUSPEND,
                style=discord.ButtonStyle.danger,
                disabled=not (active or suspended),
            ),
            self._button('Managers', MANAGERS, disabled=not active),
            self._button('Repair commands', COMMANDS, disabled=not active),
        ))
        children.extend((
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(
                f'**Status:** {_escape(self.status)}\n'
                '-# Actions stay in this panel. Use **Back to server list** '
                'to return; changes still require a target-bound confirmation.'
            ),
        ))
        self.add_item(discord.ui.Container(
            *children,
            accent_colour=components_v2.DEFAULT_ACCENT,
        ))


class GuildHistoryWorkspace(components_v2.RequesterLayoutView):
    """Target-bound history browser with rollback launched from one revision."""

    expired_message = (
        'This configuration history expired. Reopen it from '
        '`/operator guild list`.'
    )

    def __init__(
        self,
        *,
        requester_id: int,
        result: workers.GuildConfigurationReadResult,
        rollback_runner: RollbackRunner,
        back_runner: BackRunner | None = None,
        timeout: float = 600.0,
    ):
        super().__init__(requester_id=int(requester_id), timeout=timeout)
        if result.operation != workers.HISTORY or result.selected is None:
            raise ValueError('The history workspace requires one selected guild.')
        self.result = result
        self.rollback_runner = rollback_runner
        self.back_runner = back_runner
        self.selected_revision: int | None = None
        self.busy = False
        self.rebuild()

    @property
    def page_count(self) -> int:
        return 1

    async def _select_revision(
        self,
        interaction: Any,
        select: discord.ui.Select,
    ) -> None:
        if not await self.authorize(interaction):
            return
        self.selected_revision = int(select.values[0])
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _rollback(self, interaction: Any) -> None:
        if not await self.authorize(interaction):
            return
        if self.selected_revision is None:
            return await interaction.response.send_message(
                'Select an earlier revision first.', ephemeral=True,
            )
        self.busy = True
        try:
            await self.rollback_runner(
                interaction,
                self.result.guild_id,
                self.selected_revision,
            )
        finally:
            self.busy = False

    def rebuild(self) -> None:
        self.clear_items()
        selected = self.result.selected
        assert selected is not None
        revisions = []
        for revision in self.result.revisions[:10]:
            active = ' **active**' if (
                revision.revision_number == selected.active_revision
            ) else ''
            revisions.append(
                f'- `r{revision.revision_number}`{active} · '
                f'{_escape(revision.source_kind)} · '
                f'`{revision.created_at}`'
            )
        audits = [
            f'- `e{audit.event_number}` · {_escape(audit.event_type)} · '
            f'`{audit.created_at}`'
            for audit in self.result.audits[:8]
        ]
        children: list[Any] = [
            discord.ui.TextDisplay(
                '# Configuration history\n'
                f'**Server:** {_escape(selected.display_name)} '
                f'(`{selected.guild_id}`)\n'
                f'**State:** {selected.enrollment_state} · '
                f'**Active revision:** `r{selected.active_revision}`\n\n'
                '## Revisions\n'
                + ('\n'.join(revisions) or '*None*')
                + '\n\n## Recent audit events\n'
                + ('\n'.join(audits) or '*None*')
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
        ]
        earlier = tuple(
            revision for revision in self.result.revisions
            if revision.revision_number != selected.active_revision
            and revision.revision_number < int(selected.active_revision or 0)
        )
        if earlier and selected.enrollment_state == 'active':
            options = [
                discord.SelectOption(
                    label=f'Restore document from revision {value.revision_number}',
                    value=str(value.revision_number),
                    description=_trim(
                        f'{value.source_kind} · {value.created_at}', 100,
                    ),
                    default=value.revision_number == self.selected_revision,
                )
                for value in earlier[:25]
            ]
            select = discord.ui.Select(
                placeholder='Choose an earlier revision',
                options=options,
                disabled=self.busy,
            )
            select.callback = lambda interaction: self._select_revision(
                interaction, select,
            )
            rollback = discord.ui.Button(
                label=(
                    f'Preview restore r{self.selected_revision}'
                    if self.selected_revision is not None else
                    'Preview restore'
                ),
                style=discord.ButtonStyle.danger,
                disabled=self.busy or self.selected_revision is None,
            )
            rollback.callback = self._rollback
            children.extend((
                discord.ui.ActionRow(select),
                discord.ui.ActionRow(rollback),
            ))
        else:
            reason = (
                'Resume this server before restoring configuration.'
                if selected.enrollment_state != 'active' else
                'No earlier revision is available to restore.'
            )
            children.append(discord.ui.TextDisplay(f'-# {reason}'))
        if self.back_runner is not None:
            children.append(discord.ui.ActionRow(
                guild_list_back_button(
                    self, self.back_runner, disabled=self.busy,
                )
            ))
        children.append(discord.ui.TextDisplay(
            '-# Restore always creates a new revision; history is never erased '
            'or rewound.'
        ))
        self.add_item(discord.ui.Container(
            *children,
            accent_colour=components_v2.DEFAULT_ACCENT,
        ))


class GuildValidationWorkspace(components_v2.RequesterLayoutView):
    """Read-only validation result with a route back to the guild console."""

    def __init__(
        self,
        *,
        requester_id: int,
        result: workers.GuildConfigurationReadResult,
        back_runner: BackRunner,
        timeout: float = 600.0,
    ):
        super().__init__(requester_id=int(requester_id), timeout=timeout)
        if (
                result.operation != workers.VALIDATE
                or result.selected is None
                or result.validation is None
        ):
            raise ValueError('A complete validation result is required.')
        validation = result.validation
        if not all((
                validation.storage_schema_valid,
                validation.database_identity_valid,
                validation.active_document_valid,
                validation.live_references_valid,
                validation.running_snapshot_current,
        )):
            raise ValueError('A failed validation cannot render as passed.')
        self.result = result
        self.back_runner = back_runner
        self.rebuild()

    @property
    def page_count(self) -> int:
        return 1

    def rebuild(self) -> None:
        self.clear_items()
        selected = self.result.selected
        assert selected is not None
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(
                '# Configuration validation passed\n'
                f'**Server:** {_escape(selected.display_name)} '
                f'(`{selected.guild_id}`)\n'
                f'**Revision:** `r{selected.active_revision}` · '
                f'**Generation:** `g{selected.generation}`\n\n'
                '✅ Exact runtime database and role\n'
                '✅ Exact configuration storage schema\n'
                '✅ Active document schema and digest\n'
                '✅ Current Discord role/channel references\n'
                '✅ Running revision, generation, and digest\n\n'
                '-# Read-only; validation changed nothing.'
            ),
            discord.ui.ActionRow(
                guild_list_back_button(self, self.back_runner)
            ),
            accent_colour=discord.Colour.green(),
        ))


async def publish_private(interaction: Any, view: GuildRegistryConsole):
    message = await interaction.followup.send(
        view=view,
        ephemeral=True,
        wait=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    view.message = message
    return message


async def publish_history(interaction: Any, view: GuildHistoryWorkspace):
    message = await interaction.followup.send(
        view=view,
        ephemeral=True,
        wait=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    view.message = message
    return message


async def replace_private(
    interaction: Any,
    view: components_v2.RequesterLayoutView,
):
    message = await interaction.edit_original_response(
        content=None,
        embed=None,
        view=view,
    )
    view.message = message
    return message


__all__ = [
    'COMMANDS',
    'GuildHistoryWorkspace',
    'GuildRegistryConsole',
    'GuildValidationWorkspace',
    'HISTORY',
    'MANAGERS',
    'PAGE_SIZE',
    'RESUME',
    'SUSPEND',
    'VALIDATE',
    'publish_private',
    'publish_history',
    'replace_private',
    'guild_list_back_button',
]
