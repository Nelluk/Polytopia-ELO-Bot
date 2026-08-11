"""Private Components v2 editor for one owner guild-configuration draft."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import discord

from modules import components_v2
from modules import operator_guild_configuration_drafts as service
from modules import operator_guild_configuration_draft_workers as workers
from modules.application_command_policy import DEFAULT_CAPABILITY_FAMILIES


Runner = Callable[..., Awaitable[workers.GuildConfigurationDraftResult]]
SECTION_LABELS = {
    service.IDENTITY: 'Identity',
    service.PERMISSIONS: 'Permissions',
    service.TEAMS: 'Teams & visibility',
    service.CHANNELS: 'Channel policy',
    service.DESTINATIONS: 'Destinations',
    service.CAPABILITIES: 'Command capabilities',
}
LIST_KINDS = {
    service.ROLE_LIST,
    service.CHANNEL_LIST,
    service.NULLABLE_CHANNEL_LIST,
    service.CATEGORY_LIST,
}


def _escape(value: Any) -> str:
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value or ''))
    )


def _object_id(value: Any) -> int:
    return int(getattr(value, 'id'))


async def _private(interaction: Any, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


class DraftValueModal(discord.ui.Modal):
    def __init__(self, workspace: 'GuildConfigurationDraftWorkspace'):
        self.workspace = workspace
        field = workspace.field
        super().__init__(title=f'Edit {field.label}'[:45], timeout=180.0)
        current = service.field_value(workspace.result.draft.document, field)
        self.value = discord.ui.TextInput(
            default=str(current),
            required=True,
            min_length=1,
            max_length=100,
        )
        self.add_item(discord.ui.Label(
            text=field.label[:45],
            description=(
                'Enter a whole number.'
                if field.kind == service.INTEGER else
                'Enter the complete replacement text.'
            ),
            component=self.value,
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        workspace = self.workspace
        if not await workspace.ready(interaction):
            return
        raw = str(self.value.value)
        value: Any = raw
        if workspace.field.kind == service.INTEGER:
            try:
                value = int(raw.strip())
            except ValueError:
                return await _private(interaction, 'Enter a whole number.')
        await workspace.apply_value(interaction, value)


class DraftConfirmationModal(discord.ui.Modal):
    def __init__(
        self,
        workspace: 'GuildConfigurationDraftWorkspace',
        *,
        operation: str,
    ):
        self.workspace = workspace
        self.operation = operation
        expected = 'RESET DRAFT' if operation == workers.RESET else 'DISCARD DRAFT'
        super().__init__(title=expected.title(), timeout=180.0)
        self.confirmation = discord.ui.TextInput(
            placeholder=expected,
            required=True,
            min_length=len(expected),
            max_length=len(expected),
        )
        self.add_item(discord.ui.Label(
            text=f'Type {expected}',
            description='This affects only the inactive draft.',
            component=self.confirmation,
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        expected = 'RESET DRAFT' if self.operation == workers.RESET else 'DISCARD DRAFT'
        if str(self.confirmation.value) != expected:
            return await _private(interaction, f'Type `{expected}` exactly.')
        if not await self.workspace.ready(interaction):
            return
        await self.workspace.run_operation(interaction, self.operation)


class DraftActivationModal(discord.ui.Modal):
    def __init__(self, workspace: 'GuildConfigurationDraftWorkspace'):
        self.workspace = workspace
        draft = workspace.result.draft
        assert draft is not None
        self.expected = f'ACTIVATE {draft.document_digest}'
        super().__init__(title='Activate guild configuration', timeout=180.0)
        self.confirmation = discord.ui.TextInput(
            placeholder=self.expected,
            required=True,
            min_length=len(self.expected),
            max_length=len(self.expected),
        )
        self.add_item(discord.ui.Label(
            text='Type ACTIVATE and the full draft digest',
            description=(
                'Commits one immutable revision and immediately replaces the '
                'running settings snapshot.'
            ),
            component=self.confirmation,
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if str(self.confirmation.value) != self.expected:
            return await _private(
                interaction,
                f'Type `{self.expected}` exactly.',
            )
        if not await self.workspace.ready(interaction):
            return
        await self.workspace.run_operation(interaction, workers.ACTIVATE)


class GuildConfigurationDraftWorkspace(components_v2.RequesterLayoutView):
    expired_message = (
        'This guild-configuration draft workspace expired. Run '
        '`/operator guild edit` again.'
    )

    def __init__(
        self,
        *,
        requester_id: int,
        active_document: Any,
        result: workers.GuildConfigurationDraftResult,
        runner: Runner,
        role_names: Mapping[int, str],
        channel_names: Mapping[int, str],
        timeout: float = 600.0,
    ):
        super().__init__(requester_id=int(requester_id), timeout=timeout)
        self.active_document = active_document
        self.result = result
        self.runner = runner
        self.role_names = dict(role_names)
        self.channel_names = dict(channel_names)
        self.section = service.IDENTITY
        self.field_key = service.fields_for_section(self.section)[0].key
        self.list_mode = 'add'
        self.busy = False
        self.status = 'Create or edit a draft, validate it, then activate it.'
        self.rebuild()

    @property
    def page_count(self) -> int:
        return 1

    @property
    def field(self) -> service.DraftField:
        return service.FIELD_BY_KEY[self.field_key]

    async def ready(self, interaction: Any) -> bool:
        if not await self.authorize(interaction):
            return False
        if self.is_finished():
            await _private(interaction, self.expired_message)
            return False
        if self.busy:
            await _private(interaction, 'This draft operation is already running.')
            return False
        return True

    async def _execute(
        self,
        interaction: Any,
        operation: str,
        *,
        replacement_document: Any = None,
    ) -> workers.GuildConfigurationDraftResult | None:
        self.busy = True
        self.status = f'{operation.title()} in progress…'
        self.rebuild()
        await interaction.response.defer()
        await interaction.edit_original_response(view=self)
        draft = self.result.draft
        uses_optimistic_evidence = operation in {
            workers.REPLACE,
            workers.DISCARD,
            workers.ACTIVATE,
        }
        try:
            result = await self.runner(
                interaction,
                operation,
                expected_draft_version=(
                    draft.draft_version
                    if draft is not None and uses_optimistic_evidence else None
                ),
                expected_draft_digest=(
                    draft.document_digest
                    if draft is not None and uses_optimistic_evidence else None
                ),
                replacement_document=replacement_document,
            )
        except workers.OperatorGuildConfigurationDraftError as exc:
            self.busy = False
            self.status = str(exc)
            self.rebuild()
            await interaction.edit_original_response(view=self)
            await interaction.followup.send(str(exc), ephemeral=True)
            return None
        except Exception:
            self.busy = False
            self.status = (
                'The draft operation stopped without a trustworthy result. '
                'Reopen the workspace before retrying.'
            )
            self.rebuild()
            await interaction.edit_original_response(view=self)
            await interaction.followup.send(self.status, ephemeral=True)
            return None
        self.result = result
        if result.activation is not None:
            self.active_document = result.activation.document
        self.busy = False
        return result

    async def run_operation(self, interaction: Any, operation: str) -> None:
        result = await self._execute(interaction, operation)
        if result is None:
            return
        messages = {
            workers.RESET: 'Fresh draft copied from the running active revision.',
            workers.DISCARD: 'Draft discarded. Active configuration was unchanged.',
            workers.VALIDATE: 'Draft validation passed against current Discord state.',
            workers.ACTIVATE: (
                f'Activated revision {result.active_revision}, generation '
                f'{result.active_generation}; running settings were published.'
            ),
            workers.SHOW: 'Draft refreshed.',
        }
        self.status = messages.get(operation, 'Draft operation complete.')
        self.rebuild()
        await interaction.edit_original_response(view=self)

    async def apply_value(self, interaction: Any, value: Any) -> None:
        draft = self.result.draft
        if draft is None:
            return await _private(interaction, 'Create a draft before editing.')
        try:
            replacement = service.replace_field(draft.document, self.field, value)
        except service.GuildConfigurationDraftEditError as exc:
            return await _private(interaction, str(exc))
        result = await self._execute(
            interaction,
            workers.REPLACE,
            replacement_document=replacement,
        )
        if result is None:
            return
        self.status = f'Updated {self.field.label}. Active configuration is unchanged.'
        self.rebuild()
        await interaction.edit_original_response(view=self)

    async def _select_section(self, interaction: Any, select: Any) -> None:
        if not await self.ready(interaction):
            return
        self.section = str(select.values[0])
        self.field_key = service.fields_for_section(self.section)[0].key
        self.list_mode = 'add'
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _select_field(self, interaction: Any, select: Any) -> None:
        if not await self.ready(interaction):
            return
        self.field_key = str(select.values[0])
        self.list_mode = 'add'
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _select_boolean(self, interaction: Any, select: Any) -> None:
        if not await self.ready(interaction):
            return
        await self.apply_value(interaction, select.values[0] == 'true')

    async def _select_capabilities(self, interaction: Any, select: Any) -> None:
        if not await self.ready(interaction):
            return
        await self.apply_value(interaction, tuple(select.values))

    async def _select_list_mode(self, interaction: Any, select: Any) -> None:
        if not await self.ready(interaction):
            return
        self.list_mode = str(select.values[0])
        if self.list_mode in {'clear', 'inherit'}:
            value = None if self.list_mode == 'inherit' else ()
            await self.apply_value(interaction, value)
            return
        self.rebuild()
        await interaction.response.edit_message(view=self)

    async def _select_role(self, interaction: Any, select: Any) -> None:
        if not await self.ready(interaction):
            return
        draft = self.result.draft
        if draft is None:
            return await _private(interaction, 'Create a draft before editing.')
        object_id = _object_id(select.values[0])
        try:
            if self.field.kind == service.OPTIONAL_ROLE:
                replacement = service.replace_field(
                    draft.document,
                    self.field,
                    object_id,
                )
            elif self.list_mode == 'remove':
                replacement = service.remove_id(
                    draft.document,
                    self.field,
                    object_id,
                )
            else:
                replacement = service.add_id(
                    draft.document,
                    self.field,
                    object_id,
                )
        except service.GuildConfigurationDraftEditError as exc:
            return await _private(interaction, str(exc))
        await self._apply_replacement(interaction, replacement)

    async def _select_channel(self, interaction: Any, select: Any) -> None:
        if not await self.ready(interaction):
            return
        draft = self.result.draft
        if draft is None:
            return await _private(interaction, 'Create a draft before editing.')
        object_id = _object_id(select.values[0])
        try:
            if self.field.kind == service.OPTIONAL_CHANNEL:
                replacement = service.replace_field(
                    draft.document,
                    self.field,
                    object_id,
                )
            elif self.list_mode == 'remove':
                replacement = service.remove_id(
                    draft.document,
                    self.field,
                    object_id,
                )
            else:
                replacement = service.add_id(
                    draft.document,
                    self.field,
                    object_id,
                )
        except service.GuildConfigurationDraftEditError as exc:
            return await _private(interaction, str(exc))
        await self._apply_replacement(interaction, replacement)

    async def _apply_replacement(self, interaction: Any, replacement: Any) -> None:
        result = await self._execute(
            interaction,
            workers.REPLACE,
            replacement_document=replacement,
        )
        if result is None:
            return
        self.status = f'Updated {self.field.label}. Active configuration is unchanged.'
        self.rebuild()
        await interaction.edit_original_response(view=self)

    async def _edit_text(self, interaction: Any) -> None:
        if not await self.ready(interaction):
            return
        await interaction.response.send_modal(DraftValueModal(self))

    async def _clear_optional(self, interaction: Any) -> None:
        if not await self.ready(interaction):
            return
        await self.apply_value(interaction, None)

    async def _reset(self, interaction: Any) -> None:
        if not await self.ready(interaction):
            return
        if self.result.draft is None:
            await self.run_operation(interaction, workers.RESET)
        else:
            await interaction.response.send_modal(DraftConfirmationModal(
                self,
                operation=workers.RESET,
            ))

    async def _validate(self, interaction: Any) -> None:
        if not await self.ready(interaction):
            return
        if self.result.draft is None:
            return await _private(interaction, 'Create a draft before validating.')
        await self.run_operation(interaction, workers.VALIDATE)

    async def _discard(self, interaction: Any) -> None:
        if not await self.ready(interaction):
            return
        if self.result.draft is None:
            return await _private(interaction, 'There is no current draft to discard.')
        await interaction.response.send_modal(DraftConfirmationModal(
            self,
            operation=workers.DISCARD,
        ))

    async def _activate(self, interaction: Any) -> None:
        if not await self.ready(interaction):
            return
        if self.result.draft is None:
            return await _private(interaction, 'There is no current draft to activate.')
        if self.result.validation is None:
            return await _private(
                interaction,
                'Validate the current draft immediately before activation.',
            )
        await interaction.response.send_modal(DraftActivationModal(self))

    async def _refresh(self, interaction: Any) -> None:
        if not await self.ready(interaction):
            return
        await self.run_operation(interaction, workers.SHOW)

    def _format_id(self, object_id: int, *, role: bool) -> str:
        names = self.role_names if role else self.channel_names
        name = names.get(int(object_id), 'unresolved')
        marker = f'<@&{object_id}>' if role else f'<#{object_id}>'
        return f'{marker} `{object_id}` ({_escape(name)})'

    def _format_value(self, value: Any) -> str:
        field = self.field
        if value is None:
            return '*Inherit / unset*'
        if field.kind in {service.ROLE_LIST, service.OPTIONAL_ROLE}:
            values = (value,) if isinstance(value, int) else tuple(value)
            return '\n'.join(self._format_id(item, role=True) for item in values) or '*None*'
        if field.kind in {
            service.CHANNEL_LIST,
            service.NULLABLE_CHANNEL_LIST,
            service.OPTIONAL_CHANNEL,
            service.CATEGORY_LIST,
        }:
            values = (value,) if isinstance(value, int) else tuple(value)
            return '\n'.join(self._format_id(item, role=False) for item in values) or '*None*'
        if field.kind == service.CAPABILITY_LIST:
            return ', '.join(f'`{_escape(item)}`' for item in value) or '*None*'
        if isinstance(value, bool):
            return 'Enabled' if value else 'Disabled'
        return f'`{_escape(value)}`'

    def _draft_summary(self) -> str:
        draft = self.result.draft
        if draft is None:
            return (
                '# Guild configuration draft\n'
                f'**Active:** revision `{self.result.active_revision}` · '
                f'generation `{self.result.active_generation}`\n'
                '**Draft:** none\n\n'
                'Create a private draft copied from the active configuration. '
                'It will expire after 24 hours and cannot affect the running bot.'
            )
        changes = service.changed_paths(self.active_document, draft.document)
        lines = '\n'.join(f'- `{_escape(path)}`' for path in changes[:10])
        if len(changes) > 10:
            lines += f'\n- …and {len(changes) - 10} more'
        return (
            '# Guild configuration draft\n'
            f'**Active:** revision `{self.result.active_revision}` · '
            f'generation `{self.result.active_generation}`\n'
            f'**Draft:** version `{draft.draft_version}` · base '
            f'`r{draft.base_revision}/g{draft.base_generation}` · expires '
            f'`{_escape(draft.expires_at)}`\n'
            f'**Digest:** `{draft.document_digest}`\n'
            f'**Changed fields:** `{len(changes)}`\n'
            f'{lines or "*No changes from active configuration.*"}'
        )

    def rebuild(self) -> None:
        self.clear_items()
        draft = self.result.draft
        children: list[Any] = [
            discord.ui.TextDisplay(self._draft_summary()),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
        ]
        if draft is not None:
            section = discord.ui.Select(
                placeholder='Choose a settings section',
                options=[
                    discord.SelectOption(
                        label=SECTION_LABELS[value],
                        value=value,
                        default=value == self.section,
                    )
                    for value in service.SECTIONS
                ],
                disabled=self.busy,
            )
            section.callback = lambda interaction: self._select_section(interaction, section)
            fields = service.fields_for_section(self.section)
            field_select = discord.ui.Select(
                placeholder='Choose one field to edit',
                options=[
                    discord.SelectOption(
                        label=value.label[:100],
                        value=value.key,
                        default=value.key == self.field_key,
                    )
                    for value in fields
                ],
                disabled=self.busy,
            )
            field_select.callback = lambda interaction: self._select_field(
                interaction,
                field_select,
            )
            current = service.field_value(draft.document, self.field)
            children.extend((
                discord.ui.TextDisplay(
                    f'## {SECTION_LABELS[self.section]} · {_escape(self.field.label)}\n'
                    f'{self._format_value(current)}'
                ),
                discord.ui.ActionRow(section),
                discord.ui.ActionRow(field_select),
            ))
            self._add_value_controls(children, current)
        children.extend((
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(
                f'**Status:** {_escape(self.status)}\n'
                '-# Activation publishes ordinary settings immediately. Command '
                'capability changes remain blocked and commands are never synchronized here.'
            ),
        ))
        controls = []
        reset = discord.ui.Button(
            label='Create draft' if draft is None else 'Reset from active',
            style=discord.ButtonStyle.primary,
            disabled=self.busy,
        )
        reset.callback = self._reset
        controls.append(reset)
        validate = discord.ui.Button(
            label='Validate',
            style=discord.ButtonStyle.success,
            disabled=self.busy or draft is None,
        )
        validate.callback = self._validate
        controls.append(validate)
        activate = discord.ui.Button(
            label='Activate',
            style=discord.ButtonStyle.danger,
            disabled=(
                self.busy
                or draft is None
                or self.result.validation is None
                or not service.changed_paths(self.active_document, draft.document)
            ),
        )
        activate.callback = self._activate
        controls.append(activate)
        refresh = discord.ui.Button(label='Refresh', disabled=self.busy)
        refresh.callback = self._refresh
        controls.append(refresh)
        discard = discord.ui.Button(
            label='Discard',
            style=discord.ButtonStyle.danger,
            disabled=self.busy or draft is None,
        )
        discard.callback = self._discard
        controls.append(discard)
        children.append(discord.ui.ActionRow(*controls))
        self.add_item(discord.ui.Container(
            *children,
            accent_colour=components_v2.DEFAULT_ACCENT,
        ))

    def _add_value_controls(self, children: list[Any], current: Any) -> None:
        field = self.field
        if field.kind in {service.OPTIONAL_ROLE, service.OPTIONAL_CHANNEL}:
            clear = discord.ui.Button(label='Clear field', disabled=self.busy)
            clear.callback = self._clear_optional
            children.append(discord.ui.ActionRow(clear))
        if field.kind in {service.TEXT, service.INTEGER}:
            edit = discord.ui.Button(
                label=f'Edit {field.label}'[:80],
                style=discord.ButtonStyle.secondary,
                disabled=self.busy,
            )
            edit.callback = self._edit_text
            children.append(discord.ui.ActionRow(edit))
            return
        if field.kind == service.BOOLEAN:
            select = discord.ui.Select(
                placeholder='Choose enabled or disabled',
                options=[
                    discord.SelectOption(label='Enabled', value='true', default=current is True),
                    discord.SelectOption(label='Disabled', value='false', default=current is False),
                ],
                disabled=self.busy,
            )
            select.callback = lambda interaction: self._select_boolean(interaction, select)
            children.append(discord.ui.ActionRow(select))
            return
        if field.kind == service.CAPABILITY_LIST:
            capabilities = tuple(value.name for value in DEFAULT_CAPABILITY_FAMILIES)
            select = discord.ui.Select(
                placeholder='Replace command capabilities',
                min_values=0,
                max_values=len(capabilities),
                options=[
                    discord.SelectOption(
                        label=value,
                        value=value,
                        default=value in current,
                    )
                    for value in capabilities
                ],
                disabled=self.busy,
            )
            select.callback = lambda interaction: self._select_capabilities(
                interaction,
                select,
            )
            children.append(discord.ui.ActionRow(select))
            return
        if field.kind in LIST_KINDS:
            options = [
                discord.SelectOption(label='Add selected object', value='add', default=self.list_mode == 'add'),
                discord.SelectOption(label='Remove selected object', value='remove', default=self.list_mode == 'remove'),
                discord.SelectOption(label='Clear list', value='clear'),
            ]
            if field.kind == service.NULLABLE_CHANNEL_LIST:
                options.append(discord.SelectOption(label='Use inherited/unrestricted behavior', value='inherit'))
            mode = discord.ui.Select(
                placeholder='Choose list action',
                options=options,
                disabled=self.busy,
            )
            mode.callback = lambda interaction: self._select_list_mode(interaction, mode)
            children.append(discord.ui.ActionRow(mode))
        if field.kind in {service.ROLE_LIST, service.OPTIONAL_ROLE}:
            select = discord.ui.RoleSelect(
                placeholder='Choose one exact role',
                min_values=1,
                max_values=1,
                disabled=self.busy,
            )
            select.callback = lambda interaction: self._select_role(interaction, select)
            children.append(discord.ui.ActionRow(select))
            return
        channel_types = None
        if field.kind == service.CATEGORY_LIST:
            channel_types = [discord.ChannelType.category]
        select = discord.ui.ChannelSelect(
            placeholder='Choose one exact channel or category',
            min_values=1,
            max_values=1,
            channel_types=channel_types,
            disabled=self.busy,
        )
        select.callback = lambda interaction: self._select_channel(interaction, select)
        children.append(discord.ui.ActionRow(select))


async def publish_private(interaction: Any, view: GuildConfigurationDraftWorkspace):
    message = await interaction.followup.send(
        view=view,
        ephemeral=True,
        wait=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    view.message = message
    return message


def identity_maps(guild: Any) -> tuple[dict[int, str], dict[int, str]]:
    roles = {
        int(role.id): str(role.name)
        for role in tuple(getattr(guild, 'roles', ()))
    }
    channels = {
        int(channel.id): str(channel.name)
        for channel in tuple(getattr(guild, 'channels', ()))
    }
    return roles, channels


__all__ = [
    'DraftActivationModal',
    'DraftConfirmationModal',
    'DraftValueModal',
    'GuildConfigurationDraftWorkspace',
    'identity_maps',
    'publish_private',
]
