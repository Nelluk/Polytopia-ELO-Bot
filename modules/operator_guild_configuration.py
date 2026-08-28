"""Private Discord adapter for owner guild-configuration inspection."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
from typing import Any, Iterable

import discord

import settings
from modules import guild_configuration_shadow as shadow
from modules import operator_guild_configuration_workers as workers


OVERVIEW = 'overview'
PERMISSIONS = 'permissions'
TEAMS = 'teams'
CHANNELS = 'channels'
DESTINATIONS = 'destinations'
CAPABILITIES = 'capabilities'
SETTINGS_SECTIONS = frozenset({
    OVERVIEW,
    PERMISSIONS,
    TEAMS,
    CHANNELS,
    DESTINATIONS,
    CAPABILITIES,
})
SECTION_TITLES = {
    TEAMS: 'Sides & persistent Teams',
}
MAX_LISTED_GUILDS = 20
MAX_LISTED_HISTORY = 10
SettingsEditCallback = Callable[[Any], Awaitable[Any]]


logger = logging.getLogger('polybot.' + __name__)


def access_error(interaction: Any) -> str | None:
    guild_id = getattr(interaction, 'guild_id', None)
    if guild_id is None:
        return 'This command can only be used in a server.'
    if int(interaction.user.id) != int(settings.owner_id):
        return 'Only the configured bot owner can inspect guild configuration.'
    profile = settings.runtime_profile
    if (
            profile.environment != 'development'
            or profile.guild_configuration_source != 'database'
    ):
        return (
            'Guild configuration inspection requires development database '
            'authority.'
        )
    if not settings.guild_configuration_ready():
        return 'The running database guild configuration is not published.'
    if settings.database_guild_configuration(int(guild_id)) is None:
        return 'This server is not active in the running configuration snapshot.'
    return None


def build_request(
    *,
    bot: Any,
    interaction: Any,
    operation: str,
) -> workers.GuildConfigurationReadRequest:
    guild_id = int(interaction.guild_id)
    runtime_guild_ids = settings.database_guild_ids()
    snapshot = None
    if operation == workers.VALIDATE:
        snapshot = shadow.capture_discord_snapshot(
            profile=settings.runtime_profile,
            guilds=tuple(bot.guilds),
            guild_ids=runtime_guild_ids,
        )
    return workers.request_from_profile(
        profile=settings.runtime_profile,
        requester_id=int(interaction.user.id),
        guild_id=guild_id,
        operation=operation,
        runtime_record=settings.database_guild_configuration(guild_id),
        discord_snapshot=snapshot,
        runtime_guild_ids=runtime_guild_ids,
    )


def _safe(value: Any) -> str:
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value))
    )


def _inline(value: Any) -> str:
    return f'`{str(value).replace("`", "ˋ")}`'


def _trim(value: str, limit: int = 1024) -> str:
    if len(value) <= limit:
        return value
    return value[:limit - 24].rstrip() + '\n… additional values omitted'


def _bool(value: bool) -> str:
    return 'Yes' if value else 'No'


def _role(role_id: int, guild_id: int) -> str:
    if role_id == guild_id:
        return f'@everyone ({_inline(role_id)})'
    return f'<@&{role_id}> ({_inline(role_id)})'


def _channel(channel_id: int) -> str:
    return f'<#{channel_id}> ({_inline(channel_id)})'


def _ids(
    values: Iterable[int] | None,
    *,
    formatter,
    none_label: str = 'None',
) -> str:
    if values is None:
        return 'All channels'
    values = tuple(values)
    if not values:
        return none_label
    return _trim('\n'.join(formatter(int(value)) for value in values))


def _base_embed(result: workers.GuildConfigurationReadResult) -> discord.Embed:
    embed = discord.Embed(color=discord.Color.blurple())
    embed.set_footer(text='Read-only • no configuration revision was created')
    return embed


def _registry_embed(
    result: workers.GuildConfigurationReadResult,
) -> discord.Embed:
    embed = _base_embed(result)
    embed.title = 'Guild configuration registry'
    if not result.records:
        embed.description = 'No guilds are enrolled.'
        return embed
    lines = []
    state_labels = {
        'active': '🟢 active',
        'suspended': '⏸️ suspended',
        'pending': '🟡 pending',
        'retired': '⛔ retired',
    }
    for record in result.records[:MAX_LISTED_GUILDS]:
        revision = record.active_revision if record.active_revision is not None else '—'
        line = (
            f'**{_safe(record.display_name)}** ({_inline(record.guild_id)}) — '
            f'{state_labels[record.enrollment_state]} • revision `{revision}` • '
            f'generation `{record.generation}`'
        )
        if record.last_lifecycle_event is not None:
            line += (
                f'\n-# Last lifecycle: `{record.last_lifecycle_event}` by '
                f'`{_safe(record.last_lifecycle_actor)}` at '
                f'`{_safe(record.last_lifecycle_at)}`'
            )
        lines.append(line)
    if len(result.records) > MAX_LISTED_GUILDS:
        lines.append(
            f'… {len(result.records) - MAX_LISTED_GUILDS} additional guild(s) omitted.'
        )
    embed.description = _trim('\n'.join(lines), limit=4096)
    return embed


def _record_header(
    embed: discord.Embed,
    record: workers.GuildConfigurationRecord,
) -> None:
    digest = record.document_digest or '—'
    embed.add_field(
        name='Active record',
        value=(
            f'State: `{record.enrollment_state}`\n'
            f'Revision: `{record.active_revision}`\n'
            f'Generation: `{record.generation}`\n'
            f'Digest: `{digest[:16]}`'
        ),
        inline=True,
    )


def _settings_embed(
    result: workers.GuildConfigurationReadResult,
    section: str,
) -> discord.Embed:
    if section not in SETTINGS_SECTIONS:
        raise ValueError('Unknown guild-configuration settings section.')
    record = result.selected
    if record is None or record.document is None:
        raise ValueError('The settings result has no active document.')
    document = record.document
    embed = _base_embed(result)
    embed.title = (
        f'{record.display_name} — {SECTION_TITLES.get(section, section.title())}'
    )
    embed.description = (
        f'Active settings for guild {_inline(record.guild_id)}. '
        'Use **Edit settings** below to make changes.'
    )
    if section == OVERVIEW:
        embed.add_field(
            name='Identity',
            value=(
                f'Guild ID: {_inline(record.guild_id)}\n'
                f'Display name: **{_safe(document.identity.display_name)}**\n'
                f'Command prefix: {_inline(document.identity.command_prefix)}\n'
                f'Global leaderboard: '
                f'**{_bool(document.visibility.include_in_global_leaderboard)}**'
            ),
            inline=False,
        )
    elif section == PERMISSIONS:
        policy = document.permissions
        fields = (
            ('Helper roles', policy.helper_role_ids),
            ('Mod roles', policy.mod_role_ids),
            ('User level 1', policy.user_role_ids_level_1),
            ('User level 2', policy.user_role_ids_level_2),
            ('User level 3', policy.user_role_ids_level_3),
            ('User level 4', policy.user_role_ids_level_4),
            (
                'Inactive role',
                () if policy.inactive_role_id is None else (policy.inactive_role_id,),
            ),
        )
        for name, values in fields:
            embed.add_field(
                name=name,
                value=_ids(
                    values,
                    formatter=lambda value: _role(value, record.guild_id),
                ),
                inline=False,
            )
    elif section == TEAMS:
        policy = document.teams
        embed.add_field(
            name='Side and persistent Team policy',
            value=(
                f'Require persistent Teams: **{_bool(policy.require_teams)}**\n'
                f'Allow persistent Teams: **{_bool(policy.allow_teams)}**\n'
                'Allow unequal side sizes: '
                f'**{_bool(policy.allow_uneven_teams)}**\n'
                f'Maximum players per side: `{policy.max_team_size}`'
            ),
            inline=False,
        )
    elif section == CHANNELS:
        policy = document.channels
        fields = (
            ('Bot channels', policy.bot_channel_ids),
            ('Strict bot channels', policy.strict_bot_channel_ids),
            ('Private bot channels', policy.private_bot_channel_ids),
            ('Newbie-message channels', policy.newbie_message_channel_ids),
            ('Match-challenge channels', policy.match_challenge_channel_ids),
            ('Game categories', policy.game_category_ids),
        )
        for name, values in fields:
            embed.add_field(
                name=name,
                value=_ids(values, formatter=_channel),
                inline=False,
            )
    elif section == DESTINATIONS:
        policy = document.channels
        fields = (
            ('Ranked games', policy.ranked_game_channel_id),
            ('Unranked games', policy.unranked_game_channel_id),
            ('Steam games', policy.steam_game_channel_id),
            ('Game log', policy.log_channel_id),
            ('Game announcements', policy.game_announce_channel_id),
            ('Staff help', policy.staff_help_channel_id),
        )
        for name, value in fields:
            embed.add_field(
                name=name,
                value='None' if value is None else _channel(value),
                inline=True,
            )
    else:
        embed.add_field(
            name='Command capabilities',
            value=_trim(
                '\n'.join(f'• `{_safe(value)}`' for value in document.command_capabilities)
                or 'None'
            ),
            inline=False,
        )
        embed.add_field(
            name='Deployment boundary',
            value=(
                'Capability changes do not synchronize Discord commands. '
                'Registration remains a separate guild-scoped operation.'
            ),
            inline=False,
        )
    return embed


def _validation_embed(
    result: workers.GuildConfigurationReadResult,
) -> discord.Embed:
    record = result.selected
    validation = result.validation
    if record is None or validation is None:
        raise ValueError('The validation result is incomplete.')
    if not all((
            validation.storage_schema_valid,
            validation.database_identity_valid,
            validation.active_document_valid,
            validation.live_references_valid,
            validation.running_snapshot_current,
    )):
        raise ValueError('A failed validation cannot render as passed.')
    embed = discord.Embed(
        title=f'{_safe(record.display_name)} — validation passed',
        color=discord.Color.green(),
        description=(
            '✅ Exact development database and role\n'
            '✅ Exact P10 storage schema\n'
            '✅ Active document schema and digest\n'
            '✅ Current Discord role/channel references\n'
            '✅ Running immutable revision, generation, and digest'
        ),
    )
    _record_header(embed, record)
    embed.set_footer(text='Read-only • validation changed nothing')
    return embed


def _history_embed(
    result: workers.GuildConfigurationReadResult,
) -> discord.Embed:
    record = result.selected
    if record is None:
        raise ValueError('The history result has no active record.')
    embed = _base_embed(result)
    embed.title = f'{record.display_name} — configuration history'
    revision_lines = []
    for value in result.revisions[:MAX_LISTED_HISTORY]:
        active = ' **active**' if value.revision_number == record.active_revision else ''
        parent = '—' if value.parent_revision is None else str(value.parent_revision)
        revision_lines.append(
            f'`r{value.revision_number}`{active} • `{value.source_kind}` • '
            f'parent `{parent}` • `{value.document_digest[:12]}`\n'
            f'by {_safe(value.actor)} • {_inline(value.created_at)}'
        )
    audit_lines = []
    for value in result.audits[:MAX_LISTED_HISTORY]:
        revision = '—' if value.revision_number is None else value.revision_number
        audit_lines.append(
            f'`e{value.event_number}` • `{_safe(value.event_type)}` • '
            f'revision `{revision}` • generation `{value.generation}`\n'
            f'by {_safe(value.actor)} • {_inline(value.created_at)}'
        )
    embed.add_field(
        name=(
            f'Revisions ({len(result.revisions)}'
            f'{"+" if result.revisions_truncated else ""})'
        ),
        value=_trim('\n\n'.join(revision_lines) or 'None'),
        inline=False,
    )
    embed.add_field(
        name=(
            f'Audit events ({len(result.audits)}'
            f'{"+" if result.audits_truncated else ""})'
        ),
        value=_trim('\n\n'.join(audit_lines) or 'None'),
        inline=False,
    )
    return embed


def result_embed(
    result: workers.GuildConfigurationReadResult,
    *,
    section: str = OVERVIEW,
) -> discord.Embed:
    if result.operation == workers.LIST:
        return _registry_embed(result)
    if result.operation == workers.SETTINGS:
        return _settings_embed(result, section)
    if result.operation == workers.VALIDATE:
        return _validation_embed(result)
    if result.operation == workers.HISTORY:
        return _history_embed(result)
    raise ValueError('Unknown guild-configuration result operation.')


class GuildConfigurationSettingsView(discord.ui.View):
    """Small owner-only bridge from inspection to the settings editor."""

    def __init__(
        self,
        *,
        requester_id: int,
        guild_id: int,
        edit_callback: SettingsEditCallback,
        timeout: float = 600.0,
    ):
        super().__init__(timeout=timeout)
        self.requester_id = int(requester_id)
        self.guild_id = int(guild_id)
        self.edit_callback = edit_callback
        self.message: discord.Message | None = None
        edit = discord.ui.Button(
            label='Edit settings',
            style=discord.ButtonStyle.primary,
        )
        edit.callback = self._edit
        self.add_item(edit)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if (
                int(interaction.user.id) == self.requester_id
                and int(interaction.guild_id or 0) == self.guild_id
        ):
            return True
        await interaction.response.send_message(
            'Only the owner who opened this settings view can use it.',
            ephemeral=True,
        )
        return False

    async def _edit(self, interaction: discord.Interaction) -> None:
        await self.edit_callback(interaction)

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def publish_private(
    interaction: Any,
    result: workers.GuildConfigurationReadResult,
    *,
    section: str = OVERVIEW,
    requester_id: int | None = None,
    edit_callback: SettingsEditCallback | None = None,
) -> discord.Message:
    view = None
    if edit_callback is not None:
        if result.operation != workers.SETTINGS or result.selected is None:
            raise ValueError('Edit controls require one selected settings result.')
        if requester_id is None:
            raise ValueError('Edit controls require the original requester.')
        view = GuildConfigurationSettingsView(
            requester_id=requester_id,
            guild_id=result.selected.guild_id,
            edit_callback=edit_callback,
        )
    message = await interaction.followup.send(
        embed=result_embed(result, section=section),
        view=view,
        ephemeral=True,
        wait=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    if view is not None:
        view.message = message
    return message


__all__ = [
    'CAPABILITIES',
    'CHANNELS',
    'DESTINATIONS',
    'GuildConfigurationSettingsView',
    'OVERVIEW',
    'PERMISSIONS',
    'SETTINGS_SECTIONS',
    'TEAMS',
    'access_error',
    'build_request',
    'publish_private',
    'result_embed',
]
