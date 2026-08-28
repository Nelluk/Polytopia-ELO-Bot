"""Pure guild-type policy derived from protected Team settings.

Guild type is an owner-facing abstraction, not a second stored source of
truth.  The existing ``allow_teams`` and ``require_teams`` keys remain the
authoritative representation while command capabilities are materialized for
the existing guild-command deployment machinery.
"""

from __future__ import annotations

from typing import Any

from modules.guild_configuration_schema import (
    GuildConfigurationDocument,
    document_to_mapping,
    validate_document,
)


STANDARD = 'standard'
TEAM = 'team'
LEAGUE = 'league'
GUILD_TYPES = (STANDARD, TEAM, LEAGUE)

TYPE_LABELS = {
    STANDARD: 'Standard',
    TEAM: 'Team',
    LEAGUE: 'League',
}

_BASE_CAPABILITIES = ('core_user', 'squad', 'guild_admin')
_TYPE_CAPABILITIES = {
    STANDARD: _BASE_CAPABILITIES,
    TEAM: (*_BASE_CAPABILITIES, 'team'),
    LEAGUE: (*_BASE_CAPABILITIES, 'team', 'league', 'house'),
}
_OPERATIONAL_OVERLAYS = frozenset({'elo_maintenance', 'operator'})


class GuildTypeError(ValueError):
    """A guild type or type-derived configuration is invalid."""


def normalize_guild_type(value: Any) -> str:
    if not isinstance(value, str):
        raise GuildTypeError('Guild type must be Standard, Team, or League.')
    normalized = value.strip().casefold()
    if normalized not in GUILD_TYPES:
        raise GuildTypeError('Guild type must be Standard, Team, or League.')
    return normalized


def guild_type_for_document(document: GuildConfigurationDocument) -> str:
    if not isinstance(document, GuildConfigurationDocument):
        raise GuildTypeError('A validated guild configuration is required.')
    if document.teams.require_teams:
        return LEAGUE
    if document.teams.allow_teams:
        return TEAM
    return STANDARD


def label_for_document(document: GuildConfigurationDocument) -> str:
    return TYPE_LABELS[guild_type_for_document(document)]


def capabilities_for_type(
    guild_type: str,
    *,
    staff_help_enabled: bool,
    existing_capabilities: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Return the materialized capabilities for a type and stable overlays."""

    normalized = normalize_guild_type(guild_type)
    values = list(_TYPE_CAPABILITIES[normalized])
    values.extend(
        capability
        for capability in existing_capabilities
        if capability in _OPERATIONAL_OVERLAYS
    )
    if staff_help_enabled:
        values.append('tools_support')
    # Match application-command policy's deterministic lexical order.
    return tuple(sorted(set(values)))


def apply_guild_type(
    document: GuildConfigurationDocument,
    guild_type: str,
    *,
    include_in_global_leaderboard: bool | None = None,
) -> GuildConfigurationDocument:
    """Return a validated document with one derived type configuration."""

    if not isinstance(document, GuildConfigurationDocument):
        raise GuildTypeError('A validated guild configuration is required.')
    normalized = normalize_guild_type(guild_type)
    mapping = document_to_mapping(document)
    mapping['teams']['allow_teams'] = normalized in {TEAM, LEAGUE}
    mapping['teams']['require_teams'] = normalized == LEAGUE
    if include_in_global_leaderboard is not None:
        if not isinstance(include_in_global_leaderboard, bool):
            raise GuildTypeError(
                'Global leaderboard participation must be enabled or disabled.'
            )
        mapping['visibility']['include_in_global_leaderboard'] = (
            include_in_global_leaderboard
        )
    mapping['command_capabilities'] = list(capabilities_for_type(
        normalized,
        staff_help_enabled=(
            document.channels.staff_help_channel_id is not None
        ),
        existing_capabilities=document.command_capabilities,
    ))
    return validate_document(mapping)


__all__ = [
    'GUILD_TYPES',
    'GuildTypeError',
    'LEAGUE',
    'STANDARD',
    'TEAM',
    'TYPE_LABELS',
    'apply_guild_type',
    'capabilities_for_type',
    'guild_type_for_document',
    'label_for_document',
    'normalize_guild_type',
]
