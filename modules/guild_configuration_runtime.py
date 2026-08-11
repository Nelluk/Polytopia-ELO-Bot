"""Immutable hot-path guild configuration published from a matched DB read."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from modules.application_command_policy import (
    CapabilityPolicy,
    build_capability_policy,
)
from modules.guild_configuration_schema import GuildConfigurationDocument
from modules.guild_configuration_shadow import (
    GuildConfigurationShadowResult,
    STATUS_MATCHED,
    StoredGuildConfiguration,
)


ROLE_SETTING_FIELDS = MappingProxyType({
    'helper_roles': 'helper_role_ids',
    'mod_roles': 'mod_role_ids',
    'user_roles_level_1': 'user_role_ids_level_1',
    'user_roles_level_2': 'user_role_ids_level_2',
    'user_roles_level_3': 'user_role_ids_level_3',
    'user_roles_level_4': 'user_role_ids_level_4',
    'inactive_role': 'inactive_role_id',
})


class GuildConfigurationRuntimeError(RuntimeError):
    """A matched stored graph cannot safely become runtime authority."""


@dataclass(frozen=True)
class RuntimeGuildConfiguration:
    guild_id: int
    revision: int
    generation: int
    document_digest: str
    document: GuildConfigurationDocument
    legacy_settings: Mapping[str, Any]
    role_ids: Mapping[str, tuple[int, ...]]
    role_names: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class GuildConfigurationRuntimeSnapshot:
    source: str
    guilds: Mapping[int, RuntimeGuildConfiguration]
    legacy_config: Mapping[int, Mapping[str, Any]]
    command_policy: CapabilityPolicy


def _role_identity_by_guild(
        discord_snapshot: Mapping[str, Any],
) -> Mapping[int, Mapping[int, str]]:
    raw_guilds = discord_snapshot.get('guilds')
    if not isinstance(raw_guilds, list):
        raise GuildConfigurationRuntimeError('discord_snapshot_invalid')
    by_guild: dict[int, Mapping[int, str]] = {}
    for raw_guild in raw_guilds:
        if not isinstance(raw_guild, Mapping):
            raise GuildConfigurationRuntimeError('discord_snapshot_invalid')
        guild_id = raw_guild.get('guild_id')
        roles = raw_guild.get('roles')
        if (
                isinstance(guild_id, bool)
                or not isinstance(guild_id, int)
                or guild_id <= 0
                or guild_id in by_guild
                or not isinstance(roles, list)
        ):
            raise GuildConfigurationRuntimeError('discord_snapshot_invalid')
        role_names: dict[int, str] = {}
        for role in roles:
            if not isinstance(role, Mapping):
                raise GuildConfigurationRuntimeError('discord_snapshot_invalid')
            role_id = role.get('id')
            role_name = role.get('name')
            if (
                    isinstance(role_id, bool)
                    or not isinstance(role_id, int)
                    or role_id <= 0
                    or role_id in role_names
                    or not isinstance(role_name, str)
                    or not role_name
            ):
                raise GuildConfigurationRuntimeError('discord_snapshot_invalid')
            role_names[role_id] = role_name
        by_guild[guild_id] = MappingProxyType(role_names)
    return MappingProxyType(by_guild)


def _role_values(
        document: GuildConfigurationDocument,
        role_identity: Mapping[int, str],
) -> tuple[Mapping[str, tuple[int, ...]], Mapping[str, tuple[str, ...]]]:
    ids: dict[str, tuple[int, ...]] = {}
    names: dict[str, tuple[str, ...]] = {}
    for setting_name, field_name in ROLE_SETTING_FIELDS.items():
        value = getattr(document.permissions, field_name)
        role_ids = () if value is None else (
            (int(value),) if isinstance(value, int) else tuple(int(item) for item in value)
        )
        try:
            role_names = tuple(role_identity[role_id] for role_id in role_ids)
        except KeyError as exc:
            raise GuildConfigurationRuntimeError(
                'configured_role_unavailable'
            ) from exc
        ids[setting_name] = role_ids
        names[setting_name] = role_names
    return MappingProxyType(ids), MappingProxyType(names)


def _legacy_settings(
        document: GuildConfigurationDocument,
        role_names: Mapping[str, tuple[str, ...]],
) -> Mapping[str, Any]:
    channels = document.channels
    values = {
        'display_name': document.identity.display_name,
        'command_prefix': document.identity.command_prefix,
        'helper_roles': role_names['helper_roles'],
        'mod_roles': role_names['mod_roles'],
        'user_roles_level_1': role_names['user_roles_level_1'],
        'user_roles_level_2': role_names['user_roles_level_2'],
        'user_roles_level_3': role_names['user_roles_level_3'],
        'user_roles_level_4': role_names['user_roles_level_4'],
        'inactive_role': (
            role_names['inactive_role'][0]
            if role_names['inactive_role']
            else None
        ),
        'require_teams': document.teams.require_teams,
        'allow_teams': document.teams.allow_teams,
        'allow_uneven_teams': document.teams.allow_uneven_teams,
        'max_team_size': document.teams.max_team_size,
        'include_in_global_lb': (
            document.visibility.include_in_global_leaderboard
        ),
        'bot_channels': channels.bot_channel_ids,
        'bot_channels_strict': channels.strict_bot_channel_ids,
        'bot_channels_private': channels.private_bot_channel_ids,
        'newbie_message_channels': channels.newbie_message_channel_ids,
        'match_challenge_channels': channels.match_challenge_channel_ids,
        'ranked_game_channel': channels.ranked_game_channel_id,
        'unranked_game_channel': channels.unranked_game_channel_id,
        'steam_game_channel': channels.steam_game_channel_id,
        'log_channel': channels.log_channel_id,
        'game_announce_channel': channels.game_announce_channel_id,
        'staff_help_channel': channels.staff_help_channel_id,
        'game_channel_categories': channels.game_category_ids,
    }
    return MappingProxyType(values)


def build_runtime_snapshot(
        *,
        result: GuildConfigurationShadowResult,
        discord_snapshot: Mapping[str, Any],
        allowed_guild_ids: Sequence[int],
) -> GuildConfigurationRuntimeSnapshot:
    """Convert one exact current-process match to the immutable hot-path view."""

    allowed = tuple(sorted(int(value) for value in allowed_guild_ids))
    if (
            result.status != STATUS_MATCHED
            or not result.promotion_ready
            or result.expected_guild_ids != allowed
            or result.stored_guild_ids != allowed
            or result.matched_guild_ids != allowed
    ):
        raise GuildConfigurationRuntimeError('shadow_result_not_promotion_ready')
    stored_by_id = {
        value.guild_id: value for value in result.stored_configurations
    }
    if tuple(sorted(stored_by_id)) != allowed:
        raise GuildConfigurationRuntimeError('stored_inventory_incomplete')
    role_identity = _role_identity_by_guild(discord_snapshot)
    if tuple(sorted(role_identity)) != allowed:
        raise GuildConfigurationRuntimeError('discord_inventory_incomplete')

    runtime_values: dict[int, RuntimeGuildConfiguration] = {}
    legacy_values: dict[int, Mapping[str, Any]] = {}
    capabilities: dict[int, tuple[str, ...]] = {}
    for guild_id in allowed:
        stored: StoredGuildConfiguration = stored_by_id[guild_id]
        if (
                stored.enrollment_state != 'active'
                or stored.active_revision is None
                or stored.generation <= 0
                or stored.document is None
                or stored.document_digest is None
        ):
            raise GuildConfigurationRuntimeError('stored_active_graph_invalid')
        role_ids, role_names = _role_values(
            stored.document,
            role_identity[guild_id],
        )
        legacy = _legacy_settings(stored.document, role_names)
        runtime = RuntimeGuildConfiguration(
            guild_id=guild_id,
            revision=stored.active_revision,
            generation=stored.generation,
            document_digest=stored.document_digest,
            document=stored.document,
            legacy_settings=legacy,
            role_ids=role_ids,
            role_names=role_names,
        )
        runtime_values[guild_id] = runtime
        legacy_values[guild_id] = legacy
        capabilities[guild_id] = stored.document.command_capabilities

    return GuildConfigurationRuntimeSnapshot(
        source='database',
        guilds=MappingProxyType(runtime_values),
        legacy_config=MappingProxyType(legacy_values),
        command_policy=build_capability_policy(capabilities, allowed),
    )


__all__ = [
    'GuildConfigurationRuntimeError',
    'GuildConfigurationRuntimeSnapshot',
    'ROLE_SETTING_FIELDS',
    'RuntimeGuildConfiguration',
    'build_runtime_snapshot',
]
