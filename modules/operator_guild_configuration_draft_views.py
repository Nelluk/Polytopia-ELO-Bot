"""Private Components v2 editor for one owner guild-configuration draft."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
import logging
from typing import Any

import discord

from modules import components_v2
from modules import operator_guild_configuration_drafts as service
from modules import operator_guild_configuration_draft_workers as workers
from modules.application_command_policy import DEFAULT_CAPABILITY_FAMILIES


Runner = Callable[..., Awaitable[workers.GuildConfigurationDraftResult]]
logger = logging.getLogger('polybot.' + __name__)
SECTION_LABELS = {
    service.SERVER_BASICS: 'Server Basics',
    service.ROLES: 'Roles',
    service.CHANNELS_AND_MESSAGES: 'Channels and Messages',
    service.CAPABILITIES: 'Command capabilities',
}
SECTION_DESCRIPTIONS = {
    service.SERVER_BASICS: (
        'Basic server identity and game structure. Set the displayed server '
        'name and legacy command prefix, control players per side, and review '
        'whether this server uses persistent named Teams or the global '
        'leaderboard. Squads are tracked automatically and do not require '
        'persistent Teams.'
    ),
    service.ROLES: (
        'Discord roles that determine PolyElo access. Ordinary user levels '
        'control game hosting and joining limits; helper and moderator roles '
        'grant staff access. Protected staff-role assignments are managed by '
        'the bot owner.'
    ),
    service.CHANNELS_AND_MESSAGES: (
        'Where commands may be used, where game channels may be created, and '
        'where PolyElo sends listings, announcements, staff help, logs, and '
        'other scheduled messages.'
    ),
    service.CAPABILITIES: (
        'The top-level Discord command groups registered for this server. '
        'Normal server settings derive these from server type and configured '
        'destinations; command registration remains a separate operation.'
    ),
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


def _option_description(value: str) -> str:
    """Return one readable Discord select-option description."""

    rendered = ' '.join(str(value).split())
    if len(rendered) <= 100:
        return rendered
    prefix = rendered[:97].rsplit(' ', 1)[0]
    return (prefix or rendered[:97]) + '…'


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
            description=_option_description(field.help_text),
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


class GuildConfigurationDraftWorkspace(components_v2.RequesterLayoutView):
    expired_message = (
        'This guild-configuration draft workspace expired. Run '
        '`/guild settings` again.'
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
        target_guild_name: str | None = None,
        target_guild_id: int | None = None,
        capabilities_only: bool = False,
        ordinary_only: bool = False,
        simple_owner: bool = False,
        timeout: float = 600.0,
    ):
        super().__init__(requester_id=int(requester_id), timeout=timeout)
        self.active_document = active_document
        self.result = result
        self.runner = runner
        self.role_names = dict(role_names)
        self.channel_names = dict(channel_names)
        self.target_guild_id = (
            int(result.guild_id) if target_guild_id is None else int(target_guild_id)
        )
        self.target_guild_name = str(
            target_guild_name
            or active_document.identity.display_name
            or self.target_guild_id
        )
        self.capabilities_only = bool(capabilities_only)
        self.ordinary_only = bool(ordinary_only)
        self.simple_owner = bool(simple_owner)
        self.expired_message = (
            'This command-capability workspace expired. Run '
            '`/operator guild list` and choose **Repair commands** again.'
            if self.capabilities_only else
            'This guild-settings workspace expired. Run `/guild settings` again.'
        )
        self.sections = (
            (service.CAPABILITIES,)
            if self.capabilities_only else service.SECTIONS
        )
        if self.ordinary_only:
            self.sections = service.ORDINARY_SECTIONS
        elif self.simple_owner:
            self.sections = tuple(
                section
                for section in service.SECTIONS
                if section != service.CAPABILITIES
            )
        self.section = self.sections[0]
        self.field_key: str | None = (
            service.fields_for_section(self.section)[0].key
            if self.capabilities_only else None
        )
        self.list_mode = 'add'
        self.busy = False
        self.terminal = False
        self.status = (
            'Choose settings to edit. Changes remain private until you save.'
            if self.simple_owner else
            'Create or edit an ordinary-settings draft, then validate it.'
            if self.ordinary_only and not result.activation_allowed
            else 'Create or edit a draft, validate it, then activate it.'
        )
        self.rebuild()

    @property
    def page_count(self) -> int:
        return 1

    @property
    def field(self) -> service.DraftField:
        if self.field_key is None:
            raise RuntimeError('No guild-configuration field is selected.')
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
                target_guild_id=self.target_guild_id,
            )
        except workers.OperatorGuildConfigurationDraftError as exc:
            self.busy = False
            if isinstance(
                    exc,
                    workers.OperatorGuildConfigurationActivationCommitted,
            ):
                self.terminal = True
                self.stop()
            self.status = str(exc)
            self.rebuild()
            try:
                await interaction.edit_original_response(view=self)
            except Exception:
                logger.exception(
                    'Could not update guild-settings failure panel for guild %s',
                    self.target_guild_id,
                )
            try:
                await interaction.followup.send(str(exc), ephemeral=True)
            except Exception:
                logger.exception(
                    'Could not publish guild-settings failure fallback for guild %s',
                    self.target_guild_id,
                )
            return None
        except Exception:
            self.busy = False
            self.terminal = operation == workers.ACTIVATE
            if self.terminal:
                self.stop()
            self.status = (
                'Save stopped without a trustworthy terminal result. Do not '
                'repeat it; reopen the settings view to inspect current truth.'
                if operation == workers.ACTIVATE else
                'The draft operation stopped without a trustworthy result. '
                'Reopen the workspace before retrying.'
            )
            self.rebuild()
            try:
                await interaction.edit_original_response(view=self)
            except Exception:
                logger.exception(
                    'Could not update uncertain guild-settings panel for guild %s',
                    self.target_guild_id,
                )
            try:
                await interaction.followup.send(self.status, ephemeral=True)
            except Exception:
                logger.exception(
                    'Could not publish uncertain guild-settings fallback for guild %s',
                    self.target_guild_id,
                )
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
        self.field_key = None
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
            await self.run_operation(interaction, workers.RESET)

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
        await self.run_operation(interaction, workers.DISCARD)

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
        await self.run_operation(interaction, workers.ACTIVATE)

    async def _save(self, interaction: Any) -> None:
        if not await self.ready(interaction):
            return
        draft = self.result.draft
        if draft is None:
            return await _private(interaction, 'There are no settings to save.')
        if not service.changed_paths(self.active_document, draft.document):
            return await _private(interaction, 'Make at least one change before saving.')
        if not self.result.activation_allowed:
            return await _private(
                interaction,
                'Only the bot owner can save these settings.',
            )
        await self.save(interaction)

    async def save(self, interaction: Any) -> None:
        result = await self._execute(interaction, workers.ACTIVATE)
        if result is None:
            return
        self.terminal = True
        self.status = (
            f'Saved and published settings for {self.target_guild_name}. '
            'The running bot is using the new configuration. Discord command '
            'registration remains a separate deployment operation.'
        )
        self.stop()
        self.rebuild()
        await interaction.edit_original_response(view=self)

    async def _cancel(self, interaction: Any) -> None:
        if not await self.ready(interaction):
            return
        if self.result.draft is not None:
            result = await self._execute(interaction, workers.DISCARD)
            if result is None:
                return
        self.terminal = True
        self.status = 'Editing cancelled. The running configuration was unchanged.'
        self.stop()
        self.rebuild()
        await interaction.edit_original_response(view=self)

    async def _refresh_draft(self, interaction: Any) -> None:
        if not await self.ready(interaction):
            return
        await self.run_operation(interaction, workers.SHOW)

    def _format_id(self, object_id: int, *, role: bool) -> str:
        names = self.role_names if role else self.channel_names
        name = names.get(int(object_id), 'unresolved')
        marker = f'<@&{object_id}>' if role else f'<#{object_id}>'
        return f'{marker} `{object_id}` ({_escape(name)})'

    def _format_field_value(self, field: service.DraftField, value: Any) -> str:
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

    def _format_value(self, value: Any) -> str:
        return self._format_field_value(self.field, value)

    def _compact_value(self, field: service.DraftField, value: Any) -> str:
        if value is None:
            return 'unset'
        if field.kind in {service.ROLE_LIST, service.OPTIONAL_ROLE}:
            values = (value,) if isinstance(value, int) else tuple(value)
            names = [self.role_names.get(int(item), str(item)) for item in values]
            rendered = ', '.join(_escape(item) for item in names) or 'none'
        elif field.kind in {
            service.CHANNEL_LIST,
            service.NULLABLE_CHANNEL_LIST,
            service.OPTIONAL_CHANNEL,
            service.CATEGORY_LIST,
        }:
            values = (value,) if isinstance(value, int) else tuple(value)
            names = [self.channel_names.get(int(item), str(item)) for item in values]
            rendered = ', '.join(_escape(item) for item in names) or 'none'
        elif field.kind == service.CAPABILITY_LIST:
            rendered = ', '.join(_escape(item) for item in value) or 'none'
        elif isinstance(value, bool):
            rendered = 'enabled' if value else 'disabled'
        else:
            rendered = _escape(value)
        return rendered if len(rendered) <= 120 else rendered[:117] + '…'

    def _readable_changes(self, changes: tuple[str, ...]) -> str:
        fields_by_path = {
            '.'.join(field.path): field
            for field in service.FIELDS
        }
        lines = []
        draft = self.result.draft
        if draft is None:
            return '*None*'
        for path in changes[:10]:
            field = fields_by_path.get(path)
            if field is None:
                lines.append(f'- {_escape(path)}')
                continue
            old = self._compact_value(
                field, service.field_value(self.active_document, field),
            )
            new = self._compact_value(
                field, service.field_value(draft.document, field),
            )
            lines.append(f'- **{_escape(field.label)}:** {old} → {new}')
        if len(changes) > 10:
            lines.append(f'- …and {len(changes) - 10} more')
        return '\n'.join(lines) or '*No unsaved changes.*'

    def _capabilities_changed(self) -> bool:
        draft = self.result.draft
        return bool(
            draft is not None
            and draft.document.command_capabilities
            != self.active_document.command_capabilities
        )

    def _draft_summary(self) -> str:
        draft = self.result.draft
        if self.simple_owner:
            if draft is None:
                return (
                    '# Edit guild settings\n'
                    f'**Server:** {_escape(self.target_guild_name)}\n\n'
                    + (
                        'The settings were saved.'
                        if self.terminal and self.result.activation is not None else
                        'There is no active editing session.'
                    )
                )
            changes = service.changed_paths(self.active_document, draft.document)
            return (
                '# Edit guild settings\n'
                f'**Server:** {_escape(self.target_guild_name)}\n'
                f'**Unsaved changes:** {len(changes)}\n'
                f'{self._readable_changes(changes)}'
            )
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
        if draft is not None and not self.terminal:
            section = discord.ui.Select(
                placeholder='Choose a settings section',
                options=[
                    discord.SelectOption(
                        label=SECTION_LABELS[value],
                        value=value,
                        default=value == self.section,
                    )
                    for value in self.sections
                ],
                disabled=self.busy,
            )
            section.callback = lambda interaction: self._select_section(interaction, section)
            fields = service.fields_for_section(
                self.section, ordinary_only=self.ordinary_only,
            )
            field_select = discord.ui.Select(
                placeholder='Choose one field to edit',
                options=[
                    discord.SelectOption(
                        label=value.label[:100],
                        value=value.key,
                        description=_option_description(value.help_text),
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
            if self.field_key is None:
                children.extend((
                    discord.ui.TextDisplay(
                        f'## {SECTION_LABELS[self.section]}\n'
                        f'{_escape(SECTION_DESCRIPTIONS[self.section])}\n\n'
                        'Choose a setting below to see its current value and '
                        'editing controls.'
                    ),
                    discord.ui.ActionRow(section),
                    discord.ui.ActionRow(field_select),
                ))
            else:
                current = service.field_value(draft.document, self.field)
                children.extend((
                    discord.ui.TextDisplay(
                        f'## {SECTION_LABELS[self.section]} · '
                        f'{_escape(self.field.label)}\n'
                        f'{_escape(self.field.help_text)}\n\n'
                        f'**Current value**\n{self._format_value(current)}'
                    ),
                    discord.ui.ActionRow(section),
                    discord.ui.ActionRow(field_select),
                ))
                self._add_value_controls(children, current)
        children.extend((
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(
                f'**Status:** {_escape(self.status)}\n'
                + (
                    '-# Save validates the complete configuration and publishes '
                    'settings immediately. Cancel discards only the private '
                    'editing session. Discord command registration is deployed '
                    'separately.'
                    if self.simple_owner else
                    '-# Activation publishes settings immediately. Discord '
                    'command registration is deployed separately.'
                )
                + (
                    '\n-# Cross-guild editing is capability-only because Discord '
                    'role/channel selectors belong to the invoking guild. Enable '
                    '`guild_admin`, then edit other settings from inside the target.'
                    if self.capabilities_only else ''
                )
                + (
                    '\n-# This delegated workspace exposes only ordinary '
                    'same-guild settings. Owner-only fields are neither shown '
                    'nor accepted by the worker.'
                    if self.ordinary_only else ''
                )
                + (
                    '\n-# The owner kept activation owner-only; a manager can '
                    'prepare and validate this draft for owner review.'
                    if self.ordinary_only and not self.result.activation_allowed
                    else ''
                )
            ),
        ))
        if self.simple_owner:
            changes = (
                service.changed_paths(self.active_document, draft.document)
                if draft is not None else ()
            )
            save = discord.ui.Button(
                label='Save changes',
                style=discord.ButtonStyle.success,
                disabled=(
                    self.busy
                    or self.terminal
                    or draft is None
                    or not changes
                    or not self.result.activation_allowed
                ),
            )
            save.callback = self._save
            cancel = discord.ui.Button(
                label='Cancel',
                style=discord.ButtonStyle.secondary,
                disabled=self.busy or self.terminal or draft is None,
            )
            cancel.callback = self._cancel
            children.append(discord.ui.ActionRow(save, cancel))
            self.add_item(discord.ui.Container(
                *children,
                accent_colour=components_v2.DEFAULT_ACCENT,
            ))
            return
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
                or not self.result.activation_allowed
            ),
        )
        activate.callback = self._activate
        controls.append(activate)
        refresh = discord.ui.Button(label='Refresh', disabled=self.busy)
        refresh.callback = self._refresh_draft
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
            capabilities = tuple(DEFAULT_CAPABILITY_FAMILIES)
            select = discord.ui.Select(
                placeholder='Replace command capabilities',
                min_values=0,
                max_values=len(capabilities),
                options=[
                    discord.SelectOption(
                        label=value.name,
                        value=value.name,
                        description=_option_description(value.description),
                        default=value.name in current,
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
    'DraftValueModal',
    'GuildConfigurationDraftWorkspace',
    'identity_maps',
    'publish_private',
]
