"""Discord-facing pure helpers for owner guild-configuration drafts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import settings
from modules import guild_configuration_shadow as shadow
from modules import operator_guild_configuration_draft_workers as workers
from modules.guild_configuration_schema import (
    GuildConfigurationDocument,
    GuildConfigurationError,
    document_to_mapping,
    validate_document,
)


IDENTITY = 'identity'
PERMISSIONS = 'permissions'
TEAMS = 'teams'
CHANNELS = 'channels'
DESTINATIONS = 'destinations'
CAPABILITIES = 'capabilities'
SECTIONS = (
    IDENTITY,
    PERMISSIONS,
    TEAMS,
    CHANNELS,
    DESTINATIONS,
    CAPABILITIES,
)

TEXT = 'text'
BOOLEAN = 'boolean'
INTEGER = 'integer'
ROLE_LIST = 'role_list'
OPTIONAL_ROLE = 'optional_role'
CHANNEL_LIST = 'channel_list'
NULLABLE_CHANNEL_LIST = 'nullable_channel_list'
OPTIONAL_CHANNEL = 'optional_channel'
CATEGORY_LIST = 'category_list'
CAPABILITY_LIST = 'capability_list'


class GuildConfigurationDraftEditError(ValueError):
    """A typed draft edit cannot produce a valid complete document."""


@dataclass(frozen=True)
class DraftField:
    key: str
    label: str
    section: str
    path: tuple[str, ...]
    kind: str


FIELDS = (
    DraftField('display_name', 'Display name', IDENTITY, ('identity', 'display_name'), TEXT),
    DraftField('command_prefix', 'Command prefix', IDENTITY, ('identity', 'command_prefix'), TEXT),
    DraftField('helper_roles', 'Helper roles', PERMISSIONS, ('permissions', 'helper_role_ids'), ROLE_LIST),
    DraftField('mod_roles', 'Moderator roles', PERMISSIONS, ('permissions', 'mod_role_ids'), ROLE_LIST),
    DraftField('user_level_1_roles', 'User level 1 roles', PERMISSIONS, ('permissions', 'user_role_ids_level_1'), ROLE_LIST),
    DraftField('user_level_2_roles', 'User level 2 roles', PERMISSIONS, ('permissions', 'user_role_ids_level_2'), ROLE_LIST),
    DraftField('user_level_3_roles', 'User level 3 roles', PERMISSIONS, ('permissions', 'user_role_ids_level_3'), ROLE_LIST),
    DraftField('user_level_4_roles', 'User level 4 roles', PERMISSIONS, ('permissions', 'user_role_ids_level_4'), ROLE_LIST),
    DraftField('inactive_role', 'Inactive role', PERMISSIONS, ('permissions', 'inactive_role_id'), OPTIONAL_ROLE),
    DraftField('require_teams', 'Require persistent Teams', TEAMS, ('teams', 'require_teams'), BOOLEAN),
    DraftField('allow_teams', 'Allow persistent Teams', TEAMS, ('teams', 'allow_teams'), BOOLEAN),
    DraftField('allow_uneven_teams', 'Allow unequal side sizes', TEAMS, ('teams', 'allow_uneven_teams'), BOOLEAN),
    DraftField('max_team_size', 'Maximum players per side', TEAMS, ('teams', 'max_team_size'), INTEGER),
    DraftField('global_leaderboard', 'Include in global leaderboard', TEAMS, ('visibility', 'include_in_global_leaderboard'), BOOLEAN),
    DraftField('bot_channels', 'Bot channels', CHANNELS, ('channels', 'bot_channel_ids'), NULLABLE_CHANNEL_LIST),
    DraftField('strict_bot_channels', 'Strict bot channels', CHANNELS, ('channels', 'strict_bot_channel_ids'), NULLABLE_CHANNEL_LIST),
    DraftField('private_bot_channels', 'Private bot channels', CHANNELS, ('channels', 'private_bot_channel_ids'), CHANNEL_LIST),
    DraftField('newbie_channels', 'Newbie message channels', CHANNELS, ('channels', 'newbie_message_channel_ids'), CHANNEL_LIST),
    DraftField('challenge_channels', 'Match challenge channels', CHANNELS, ('channels', 'match_challenge_channel_ids'), CHANNEL_LIST),
    DraftField('game_categories', 'Game channel categories', CHANNELS, ('channels', 'game_category_ids'), CATEGORY_LIST),
    DraftField('ranked_game_channel', 'Ranked game channel', DESTINATIONS, ('channels', 'ranked_game_channel_id'), OPTIONAL_CHANNEL),
    DraftField('unranked_game_channel', 'Unranked game channel', DESTINATIONS, ('channels', 'unranked_game_channel_id'), OPTIONAL_CHANNEL),
    DraftField('steam_game_channel', 'Steam game channel', DESTINATIONS, ('channels', 'steam_game_channel_id'), OPTIONAL_CHANNEL),
    DraftField('log_channel', 'Log channel', DESTINATIONS, ('channels', 'log_channel_id'), OPTIONAL_CHANNEL),
    DraftField('game_announce_channel', 'Game announcement channel', DESTINATIONS, ('channels', 'game_announce_channel_id'), OPTIONAL_CHANNEL),
    DraftField('staff_help_channel', 'Staff-help channel', DESTINATIONS, ('channels', 'staff_help_channel_id'), OPTIONAL_CHANNEL),
    DraftField('command_capabilities', 'Command capabilities', CAPABILITIES, ('command_capabilities',), CAPABILITY_LIST),
)
FIELD_BY_KEY = {value.key: value for value in FIELDS}
ORDINARY_FIELD_KEYS = frozenset({
    'display_name', 'command_prefix',
    'allow_uneven_teams', 'max_team_size',
    'bot_channels', 'strict_bot_channels', 'newbie_channels',
    'challenge_channels', 'game_categories', 'ranked_game_channel',
    'unranked_game_channel', 'steam_game_channel', 'game_announce_channel',
})
ORDINARY_FIELDS = tuple(value for value in FIELDS if value.key in ORDINARY_FIELD_KEYS)
ORDINARY_SECTIONS = tuple(
    value for value in SECTIONS
    if any(field.section == value for field in ORDINARY_FIELDS)
)


def fields_for_section(
    section: str, *, ordinary_only: bool = False,
) -> tuple[DraftField, ...]:
    if section not in SECTIONS:
        raise GuildConfigurationDraftEditError('Unknown draft section.')
    source = ORDINARY_FIELDS if ordinary_only else FIELDS
    return tuple(value for value in source if value.section == section)


def field_value(document: GuildConfigurationDocument, field: DraftField) -> Any:
    value: Any = document_to_mapping(document)
    for part in field.path:
        value = value[part]
    if isinstance(value, list):
        return tuple(value)
    return value


def replace_field(
    document: GuildConfigurationDocument,
    field: DraftField,
    value: Any,
) -> GuildConfigurationDocument:
    if not isinstance(field, DraftField) or field.key not in FIELD_BY_KEY:
        raise GuildConfigurationDraftEditError('Unknown draft field.')
    mapping = document_to_mapping(document)
    target: Any = mapping
    for part in field.path[:-1]:
        target = target[part]
    if field.kind in {ROLE_LIST, CHANNEL_LIST, CATEGORY_LIST, CAPABILITY_LIST}:
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise GuildConfigurationDraftEditError(
                f'{field.label} requires an ordered value list.'
            )
        normalized: Any = list(value)
    elif field.kind == NULLABLE_CHANNEL_LIST:
        if value is None:
            normalized = None
        elif isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise GuildConfigurationDraftEditError(
                f'{field.label} requires an ordered channel list or Inherit.'
            )
        else:
            normalized = list(value)
    else:
        normalized = value
    target[field.path[-1]] = normalized
    try:
        return validate_document(mapping)
    except GuildConfigurationError as exc:
        raise GuildConfigurationDraftEditError(str(exc)) from exc


def add_id(
    document: GuildConfigurationDocument,
    field: DraftField,
    object_id: int,
) -> GuildConfigurationDocument:
    current = field_value(document, field)
    if current is None:
        current = ()
    values = tuple(current)
    if object_id in values:
        raise GuildConfigurationDraftEditError(
            f'{field.label} already contains that object.'
        )
    return replace_field(document, field, (*values, int(object_id)))


def remove_id(
    document: GuildConfigurationDocument,
    field: DraftField,
    object_id: int,
) -> GuildConfigurationDocument:
    current = field_value(document, field)
    if current is None or object_id not in current:
        raise GuildConfigurationDraftEditError(
            f'{field.label} does not contain that object.'
        )
    return replace_field(
        document,
        field,
        tuple(value for value in current if value != object_id),
    )


def changed_paths(
    active: GuildConfigurationDocument,
    draft: GuildConfigurationDocument,
) -> tuple[str, ...]:
    def difference(expected: Any, candidate: Any, prefix: str = '') -> list[str]:
        if isinstance(expected, Mapping) and isinstance(candidate, Mapping):
            paths = []
            for key in sorted(set(expected) | set(candidate), key=str):
                path = f'{prefix}.{key}' if prefix else str(key)
                if key not in expected or key not in candidate:
                    paths.append(path)
                else:
                    paths.extend(difference(expected[key], candidate[key], path))
            return paths
        if expected != candidate:
            return [prefix]
        return []

    return tuple(difference(
        document_to_mapping(active),
        document_to_mapping(draft),
    ))


def access_error(interaction: Any) -> str | None:
    if getattr(interaction, 'guild_id', None) is None:
        return 'This command can only be used in a server.'
    if int(interaction.user.id) != int(settings.owner_id):
        return 'Only the configured bot owner can manage guild configuration drafts.'
    profile = settings.runtime_profile
    if (
            profile.environment != 'development'
            or profile.guild_configuration_source != 'database'
    ):
        return 'Guild configuration drafts require development database authority.'
    if not settings.guild_configuration_ready():
        return 'The running database guild configuration is not published.'
    if settings.database_guild_configuration(int(interaction.guild_id)) is None:
        return 'This server is not active in the running configuration snapshot.'
    return None


def delegated_access_error(interaction: Any) -> str | None:
    if getattr(interaction, 'guild_id', None) is None:
        return 'This command can only be used in a server.'
    profile = settings.runtime_profile
    if (
            profile.environment != 'development'
            or profile.guild_configuration_source != 'database'
    ):
        return 'Guild configuration editing requires development database authority.'
    if not settings.guild_configuration_ready():
        return 'The running database guild configuration is not published.'
    if settings.database_guild_configuration(int(interaction.guild_id)) is None:
        return 'This server is not active in the running configuration snapshot.'
    return None


def requester_role_ids(interaction: Any) -> tuple[int, ...]:
    values = set()
    for role in tuple(getattr(interaction.user, 'roles', ())):
        role_id = int(role.id)
        is_default = getattr(role, 'is_default', None)
        if (
                role_id <= 0
                or bool(getattr(role, 'managed', False))
                or (callable(is_default) and bool(is_default()))
        ):
            continue
        values.add(role_id)
    return tuple(sorted(values))


def build_request(
    *,
    bot: Any,
    interaction: Any,
    operation: str,
    target_guild_id: int | None = None,
    expected_draft_version: int | None = None,
    expected_draft_digest: str | None = None,
    replacement_document: GuildConfigurationDocument | None = None,
    command_plan_digest: str | None = None,
    confirmation_text: str | None = None,
) -> workers.GuildConfigurationDraftRequest:
    guild_id = int(
        interaction.guild_id if target_guild_id is None else target_guild_id
    )
    runtime_guild_ids = settings.database_guild_ids()
    snapshot = None
    if operation in {
        workers.VALIDATE, workers.ACTIVATE, workers.ACTIVATE_COMMANDS,
    }:
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
        invoking_guild_id=int(interaction.guild_id),
        requester_role_ids=requester_role_ids(interaction),
        expected_draft_version=expected_draft_version,
        expected_draft_digest=expected_draft_digest,
        replacement_document=(
            None
            if replacement_document is None
            else document_to_mapping(replacement_document)
        ),
        discord_snapshot=snapshot,
        command_plan_digest=command_plan_digest,
        confirmation_text=confirmation_text,
        runtime_guild_ids=runtime_guild_ids,
    )


def build_rollback_request(
    *,
    bot: Any,
    interaction: Any,
    operation: str,
    target_revision: int,
    target_guild_id: int | None = None,
    expected_target_digest: str | None = None,
    expected_active_revision: int | None = None,
    expected_active_generation: int | None = None,
    expected_active_digest: str | None = None,
    confirmation_text: str | None = None,
) -> workers.GuildConfigurationDraftRequest:
    if operation not in {workers.ROLLBACK_PREVIEW, workers.ROLLBACK_COMMIT}:
        raise GuildConfigurationDraftEditError(
            'Unknown guild-configuration rollback operation.'
        )
    guild_id = int(
        interaction.guild_id if target_guild_id is None else target_guild_id
    )
    runtime_guild_ids = settings.database_guild_ids()
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
        invoking_guild_id=int(interaction.guild_id),
        requester_role_ids=requester_role_ids(interaction),
        discord_snapshot=snapshot,
        target_revision=int(target_revision),
        expected_target_digest=expected_target_digest,
        expected_active_revision=expected_active_revision,
        expected_active_generation=expected_active_generation,
        expected_active_digest=expected_active_digest,
        confirmation_text=confirmation_text,
        runtime_guild_ids=runtime_guild_ids,
    )


__all__ = [
    'BOOLEAN',
    'CAPABILITIES',
    'CAPABILITY_LIST',
    'CATEGORY_LIST',
    'CHANNELS',
    'CHANNEL_LIST',
    'DESTINATIONS',
    'DraftField',
    'FIELD_BY_KEY',
    'FIELDS',
    'GuildConfigurationDraftEditError',
    'IDENTITY',
    'INTEGER',
    'NULLABLE_CHANNEL_LIST',
    'OPTIONAL_CHANNEL',
    'OPTIONAL_ROLE',
    'ORDINARY_FIELDS',
    'ORDINARY_FIELD_KEYS',
    'ORDINARY_SECTIONS',
    'PERMISSIONS',
    'ROLE_LIST',
    'SECTIONS',
    'TEAMS',
    'TEXT',
    'access_error',
    'delegated_access_error',
    'add_id',
    'build_request',
    'build_rollback_request',
    'changed_paths',
    'field_value',
    'fields_for_section',
    'remove_id',
    'replace_field',
    'requester_role_ids',
]
