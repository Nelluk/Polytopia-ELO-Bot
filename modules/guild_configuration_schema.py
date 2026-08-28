"""Pure typed contract for future dynamic guild configuration.

This module is deliberately offline: it imports no runtime settings, Discord,
or Peewee state.  It validates complete schema-versioned documents and can
materialize the current inherited Python-dictionary shape when supplied an
explicit role-name resolution snapshot.  It does not select configuration
authority or read/write a database.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping, Sequence

from modules.application_command_policy import (
    ApplicationCommandPolicyError,
    build_capability_policy,
)


SCHEMA_VERSION = 1
MAX_DISPLAY_NAME_LENGTH = 100
MAX_PREFIX_LENGTH = 5
MAX_TEAM_SIZE = 16
MAX_ROLE_IDS = 50
MAX_CHANNEL_IDS = 100
MAX_CATEGORY_IDS = 50

LEGACY_DEFAULT_KEYS = frozenset({
    'helper_roles',
    'mod_roles',
    'user_roles_level_4',
    'user_roles_level_3',
    'user_roles_level_2',
    'user_roles_level_1',
    'inactive_role',
    'display_name',
    'require_teams',
    'allow_teams',
    'allow_uneven_teams',
    'max_team_size',
    'command_prefix',
    'include_in_global_lb',
    'match_challenge_channel',
    'bot_channels_private',
    'bot_channels_strict',
    'bot_channels',
    'newbie_message_channels',
    'match_challenge_channels',
    'ranked_game_channel',
    'unranked_game_channel',
    'steam_game_channel',
    'log_channel',
    'game_announce_channel',
    'staff_help_channel',
    'game_channel_categories',
})
OBSOLETE_LEGACY_KEYS = frozenset({'match_challenge_channel'})
MIGRATED_LEGACY_KEYS = LEGACY_DEFAULT_KEYS - OBSOLETE_LEGACY_KEYS


class GuildConfigurationError(ValueError):
    """A guild configuration document is incomplete, ambiguous, or unsafe."""


def _strict_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise GuildConfigurationError(f'{field} must be a boolean.')
    return value


def _positive_int(value: Any, field: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GuildConfigurationError(f'{field} must be a positive integer.')
    if maximum is not None and value > maximum:
        raise GuildConfigurationError(f'{field} must be at most {maximum}.')
    return value


def _bounded_string(value: Any, field: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise GuildConfigurationError(f'{field} must be a string.')
    if not value or value != value.strip() or len(value) > maximum:
        raise GuildConfigurationError(
            f'{field} must be nonempty, trimmed, and at most {maximum} characters.'
        )
    if not value.isprintable():
        raise GuildConfigurationError(f'{field} must contain printable characters.')
    return value


def _id_tuple(
    value: Any,
    field: str,
    *,
    maximum: int,
) -> tuple[int, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise GuildConfigurationError(f'{field} must be a sequence of IDs.')
    if len(value) > maximum:
        raise GuildConfigurationError(
            f'{field} may contain at most {maximum} IDs.'
        )
    normalized = tuple(
        _positive_int(item, f'{field} entry')
        for item in value
    )
    if len(normalized) != len(set(normalized)):
        raise GuildConfigurationError(f'{field} contains duplicate IDs.')
    return normalized


def _optional_id_tuple(
    value: Any,
    field: str,
    *,
    maximum: int,
) -> tuple[int, ...] | None:
    if value is None:
        return None
    return _id_tuple(value, field, maximum=maximum)


def _optional_id(value: Any, field: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, field)


def _exact_mapping(
    value: Any,
    expected: frozenset[str],
    field: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GuildConfigurationError(f'{field} must be an object.')
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected, key=str)
        detail = []
        if missing:
            detail.append('missing ' + ', '.join(missing))
        if unknown:
            detail.append('unknown ' + ', '.join(str(item) for item in unknown))
        raise GuildConfigurationError(
            f'{field} must contain exactly the reviewed fields'
            + (': ' + '; '.join(detail) if detail else '.')
        )
    return value


def _json_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise GuildConfigurationError(f'{field} must be a JSON list.')
    return value


def _optional_json_list(value: Any, field: str) -> list[Any] | None:
    if value is None:
        return None
    return _json_list(value, field)


@dataclass(frozen=True)
class GuildIdentity:
    display_name: str
    command_prefix: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            'display_name',
            _bounded_string(
                self.display_name,
                'identity.display_name',
                maximum=MAX_DISPLAY_NAME_LENGTH,
            ),
        )
        prefix = _bounded_string(
            self.command_prefix,
            'identity.command_prefix',
            maximum=MAX_PREFIX_LENGTH,
        )
        if any(character.isspace() for character in prefix):
            raise GuildConfigurationError(
                'identity.command_prefix must not contain whitespace.'
            )
        object.__setattr__(self, 'command_prefix', prefix)


@dataclass(frozen=True)
class GuildPermissionPolicy:
    helper_role_ids: tuple[int, ...]
    mod_role_ids: tuple[int, ...]
    user_role_ids_level_1: tuple[int, ...]
    user_role_ids_level_2: tuple[int, ...]
    user_role_ids_level_3: tuple[int, ...]
    user_role_ids_level_4: tuple[int, ...]
    inactive_role_id: int | None

    def __post_init__(self) -> None:
        for field in (
            'helper_role_ids',
            'mod_role_ids',
            'user_role_ids_level_1',
            'user_role_ids_level_2',
            'user_role_ids_level_3',
            'user_role_ids_level_4',
        ):
            object.__setattr__(
                self,
                field,
                _id_tuple(getattr(self, field), f'permissions.{field}', maximum=MAX_ROLE_IDS),
            )
        object.__setattr__(
            self,
            'inactive_role_id',
            _optional_id(self.inactive_role_id, 'permissions.inactive_role_id'),
        )


@dataclass(frozen=True)
class GuildTeamPolicy:
    require_teams: bool
    allow_teams: bool
    allow_uneven_teams: bool
    max_team_size: int

    def __post_init__(self) -> None:
        for field in ('require_teams', 'allow_teams', 'allow_uneven_teams'):
            object.__setattr__(
                self,
                field,
                _strict_bool(getattr(self, field), f'teams.{field}'),
            )
        object.__setattr__(
            self,
            'max_team_size',
            _positive_int(
                self.max_team_size,
                'teams.max_team_size',
                maximum=MAX_TEAM_SIZE,
            ),
        )
        if self.require_teams and not self.allow_teams:
            raise GuildConfigurationError(
                'teams.require_teams cannot be true when teams.allow_teams is false.'
            )


@dataclass(frozen=True)
class GuildVisibilityPolicy:
    include_in_global_leaderboard: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            'include_in_global_leaderboard',
            _strict_bool(
                self.include_in_global_leaderboard,
                'visibility.include_in_global_leaderboard',
            ),
        )


@dataclass(frozen=True)
class GuildChannelPolicy:
    bot_channel_ids: tuple[int, ...] | None
    strict_bot_channel_ids: tuple[int, ...] | None
    private_bot_channel_ids: tuple[int, ...]
    newbie_message_channel_ids: tuple[int, ...]
    match_challenge_channel_ids: tuple[int, ...]
    ranked_game_channel_id: int | None
    unranked_game_channel_id: int | None
    steam_game_channel_id: int | None
    log_channel_id: int | None
    game_announce_channel_id: int | None
    staff_help_channel_id: int | None
    game_category_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        for field in ('bot_channel_ids', 'strict_bot_channel_ids'):
            object.__setattr__(
                self,
                field,
                _optional_id_tuple(
                    getattr(self, field),
                    f'channels.{field}',
                    maximum=MAX_CHANNEL_IDS,
                ),
            )
        for field in (
            'private_bot_channel_ids',
            'newbie_message_channel_ids',
            'match_challenge_channel_ids',
        ):
            object.__setattr__(
                self,
                field,
                _id_tuple(
                    getattr(self, field),
                    f'channels.{field}',
                    maximum=MAX_CHANNEL_IDS,
                ),
            )
        for field in (
            'ranked_game_channel_id',
            'unranked_game_channel_id',
            'steam_game_channel_id',
            'log_channel_id',
            'game_announce_channel_id',
            'staff_help_channel_id',
        ):
            object.__setattr__(
                self,
                field,
                _optional_id(getattr(self, field), f'channels.{field}'),
            )
        object.__setattr__(
            self,
            'game_category_ids',
            _id_tuple(
                self.game_category_ids,
                'channels.game_category_ids',
                maximum=MAX_CATEGORY_IDS,
            ),
        )


@dataclass(frozen=True)
class GuildConfigurationDocument:
    schema_version: int
    guild_id: int
    identity: GuildIdentity
    permissions: GuildPermissionPolicy
    teams: GuildTeamPolicy
    visibility: GuildVisibilityPolicy
    channels: GuildChannelPolicy
    command_capabilities: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != SCHEMA_VERSION
        ):
            raise GuildConfigurationError(
                f'Unsupported guild configuration schema version: {self.schema_version!r}.'
            )
        object.__setattr__(self, 'guild_id', _positive_int(self.guild_id, 'guild_id'))
        for field, expected in (
            ('identity', GuildIdentity),
            ('permissions', GuildPermissionPolicy),
            ('teams', GuildTeamPolicy),
            ('visibility', GuildVisibilityPolicy),
            ('channels', GuildChannelPolicy),
        ):
            if not isinstance(getattr(self, field), expected):
                raise GuildConfigurationError(
                    f'{field} must be a validated {expected.__name__}.'
                )
        if self.guild_id in self.permissions.helper_role_ids:
            raise GuildConfigurationError(
                'permissions.helper_role_ids must not contain the @everyone role.'
            )
        if self.guild_id in self.permissions.mod_role_ids:
            raise GuildConfigurationError(
                'permissions.mod_role_ids must not contain the @everyone role.'
            )
        if self.permissions.inactive_role_id == self.guild_id:
            raise GuildConfigurationError(
                'permissions.inactive_role_id must not be the @everyone role.'
            )
        if isinstance(self.command_capabilities, (str, bytes)) or not isinstance(
            self.command_capabilities,
            Sequence,
        ):
            raise GuildConfigurationError(
                'command_capabilities must be a sequence of capability names.'
            )
        try:
            policy = build_capability_policy(
                {self.guild_id: tuple(self.command_capabilities)},
                (self.guild_id,),
            )
        except ApplicationCommandPolicyError as exc:
            raise GuildConfigurationError(str(exc)) from exc
        capabilities = policy.capabilities_for_guild(self.guild_id)
        object.__setattr__(self, 'command_capabilities', capabilities)
        if (
            'tools_support' in capabilities
            and self.channels.staff_help_channel_id is None
        ):
            raise GuildConfigurationError(
                'tools_support requires channels.staff_help_channel_id.'
            )


_TOP_LEVEL_FIELDS = frozenset({
    'schema_version',
    'guild_id',
    'identity',
    'permissions',
    'teams',
    'visibility',
    'channels',
    'command_capabilities',
})
_IDENTITY_FIELDS = frozenset({'display_name', 'command_prefix'})
_PERMISSION_FIELDS = frozenset({
    'helper_role_ids',
    'mod_role_ids',
    'user_role_ids_level_1',
    'user_role_ids_level_2',
    'user_role_ids_level_3',
    'user_role_ids_level_4',
    'inactive_role_id',
})
_TEAM_FIELDS = frozenset({
    'require_teams',
    'allow_teams',
    'allow_uneven_teams',
    'max_team_size',
})
_VISIBILITY_FIELDS = frozenset({'include_in_global_leaderboard'})
_CHANNEL_FIELDS = frozenset({
    'bot_channel_ids',
    'strict_bot_channel_ids',
    'private_bot_channel_ids',
    'newbie_message_channel_ids',
    'match_challenge_channel_ids',
    'ranked_game_channel_id',
    'unranked_game_channel_id',
    'steam_game_channel_id',
    'log_channel_id',
    'game_announce_channel_id',
    'staff_help_channel_id',
    'game_category_ids',
})


def validate_document(value: Mapping[str, Any]) -> GuildConfigurationDocument:
    """Validate one complete JSON-shaped document and return frozen values."""

    root = _exact_mapping(value, _TOP_LEVEL_FIELDS, 'document')
    identity = _exact_mapping(root['identity'], _IDENTITY_FIELDS, 'identity')
    permissions = _exact_mapping(
        root['permissions'],
        _PERMISSION_FIELDS,
        'permissions',
    )
    teams = _exact_mapping(root['teams'], _TEAM_FIELDS, 'teams')
    visibility = _exact_mapping(
        root['visibility'],
        _VISIBILITY_FIELDS,
        'visibility',
    )
    channels = _exact_mapping(root['channels'], _CHANNEL_FIELDS, 'channels')
    return GuildConfigurationDocument(
        schema_version=root['schema_version'],
        guild_id=root['guild_id'],
        identity=GuildIdentity(
            display_name=identity['display_name'],
            command_prefix=identity['command_prefix'],
        ),
        permissions=GuildPermissionPolicy(
            helper_role_ids=tuple(_json_list(
                permissions['helper_role_ids'], 'permissions.helper_role_ids'
            )),
            mod_role_ids=tuple(_json_list(
                permissions['mod_role_ids'], 'permissions.mod_role_ids'
            )),
            user_role_ids_level_1=tuple(_json_list(
                permissions['user_role_ids_level_1'],
                'permissions.user_role_ids_level_1',
            )),
            user_role_ids_level_2=tuple(_json_list(
                permissions['user_role_ids_level_2'],
                'permissions.user_role_ids_level_2',
            )),
            user_role_ids_level_3=tuple(_json_list(
                permissions['user_role_ids_level_3'],
                'permissions.user_role_ids_level_3',
            )),
            user_role_ids_level_4=tuple(_json_list(
                permissions['user_role_ids_level_4'],
                'permissions.user_role_ids_level_4',
            )),
            inactive_role_id=permissions['inactive_role_id'],
        ),
        teams=GuildTeamPolicy(
            require_teams=teams['require_teams'],
            allow_teams=teams['allow_teams'],
            allow_uneven_teams=teams['allow_uneven_teams'],
            max_team_size=teams['max_team_size'],
        ),
        visibility=GuildVisibilityPolicy(
            include_in_global_leaderboard=visibility[
                'include_in_global_leaderboard'
            ],
        ),
        channels=GuildChannelPolicy(
            bot_channel_ids=(
                None
                if channels['bot_channel_ids'] is None
                else tuple(_optional_json_list(
                    channels['bot_channel_ids'], 'channels.bot_channel_ids'
                ))
            ),
            strict_bot_channel_ids=(
                None
                if channels['strict_bot_channel_ids'] is None
                else tuple(_optional_json_list(
                    channels['strict_bot_channel_ids'],
                    'channels.strict_bot_channel_ids',
                ))
            ),
            private_bot_channel_ids=tuple(_json_list(
                channels['private_bot_channel_ids'],
                'channels.private_bot_channel_ids',
            )),
            newbie_message_channel_ids=tuple(_json_list(
                channels['newbie_message_channel_ids'],
                'channels.newbie_message_channel_ids',
            )),
            match_challenge_channel_ids=tuple(_json_list(
                channels['match_challenge_channel_ids'],
                'channels.match_challenge_channel_ids',
            )),
            ranked_game_channel_id=channels['ranked_game_channel_id'],
            unranked_game_channel_id=channels['unranked_game_channel_id'],
            steam_game_channel_id=channels['steam_game_channel_id'],
            log_channel_id=channels['log_channel_id'],
            game_announce_channel_id=channels['game_announce_channel_id'],
            staff_help_channel_id=channels['staff_help_channel_id'],
            game_category_ids=tuple(_json_list(
                channels['game_category_ids'], 'channels.game_category_ids'
            )),
        ),
        command_capabilities=tuple(_json_list(
            root['command_capabilities'], 'command_capabilities'
        )),
    )


def document_to_mapping(document: GuildConfigurationDocument) -> dict[str, Any]:
    """Return the canonical complete JSON-shaped representation."""

    if not isinstance(document, GuildConfigurationDocument):
        raise GuildConfigurationError(
            'document must be a validated GuildConfigurationDocument.'
        )
    return {
        'schema_version': document.schema_version,
        'guild_id': document.guild_id,
        'identity': {
            'display_name': document.identity.display_name,
            'command_prefix': document.identity.command_prefix,
        },
        'permissions': {
            'helper_role_ids': list(document.permissions.helper_role_ids),
            'mod_role_ids': list(document.permissions.mod_role_ids),
            'user_role_ids_level_1': list(
                document.permissions.user_role_ids_level_1
            ),
            'user_role_ids_level_2': list(
                document.permissions.user_role_ids_level_2
            ),
            'user_role_ids_level_3': list(
                document.permissions.user_role_ids_level_3
            ),
            'user_role_ids_level_4': list(
                document.permissions.user_role_ids_level_4
            ),
            'inactive_role_id': document.permissions.inactive_role_id,
        },
        'teams': {
            'require_teams': document.teams.require_teams,
            'allow_teams': document.teams.allow_teams,
            'allow_uneven_teams': document.teams.allow_uneven_teams,
            'max_team_size': document.teams.max_team_size,
        },
        'visibility': {
            'include_in_global_leaderboard': (
                document.visibility.include_in_global_leaderboard
            ),
        },
        'channels': {
            'bot_channel_ids': (
                None
                if document.channels.bot_channel_ids is None
                else list(document.channels.bot_channel_ids)
            ),
            'strict_bot_channel_ids': (
                None
                if document.channels.strict_bot_channel_ids is None
                else list(document.channels.strict_bot_channel_ids)
            ),
            'private_bot_channel_ids': list(
                document.channels.private_bot_channel_ids
            ),
            'newbie_message_channel_ids': list(
                document.channels.newbie_message_channel_ids
            ),
            'match_challenge_channel_ids': list(
                document.channels.match_challenge_channel_ids
            ),
            'ranked_game_channel_id': document.channels.ranked_game_channel_id,
            'unranked_game_channel_id': (
                document.channels.unranked_game_channel_id
            ),
            'steam_game_channel_id': document.channels.steam_game_channel_id,
            'log_channel_id': document.channels.log_channel_id,
            'game_announce_channel_id': (
                document.channels.game_announce_channel_id
            ),
            'staff_help_channel_id': document.channels.staff_help_channel_id,
            'game_category_ids': list(document.channels.game_category_ids),
        },
        'command_capabilities': list(document.command_capabilities),
    }


def canonical_document_json(document: GuildConfigurationDocument) -> str:
    """Serialize deterministically while preserving semantic list order."""

    return json.dumps(
        document_to_mapping(document),
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    )


def document_digest(document: GuildConfigurationDocument) -> str:
    """Bind confirmation/revision evidence to the complete guild document."""

    return hashlib.sha256(
        canonical_document_json(document).encode('utf-8')
    ).hexdigest()


def _legacy_role_names(value: Any, field: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise GuildConfigurationError(f'{field} must be a sequence of role names.')
    if len(value) > MAX_ROLE_IDS:
        raise GuildConfigurationError(
            f'{field} may contain at most {MAX_ROLE_IDS} role names.'
        )
    names = tuple(value)
    if any(not isinstance(item, str) or not item for item in names):
        raise GuildConfigurationError(f'{field} contains an invalid role name.')
    if len(names) != len(set(names)):
        raise GuildConfigurationError(f'{field} contains duplicate role names.')
    return names


def _resolve_legacy_role(
    name: str,
    *,
    guild_id: int,
    role_ids_by_name: Mapping[str, Any],
    field: str,
) -> int:
    if name == '@everyone':
        return guild_id
    if name not in role_ids_by_name:
        raise GuildConfigurationError(
            f'{field} role {name!r} does not resolve in guild {guild_id}.'
        )
    raw = role_ids_by_name[name]
    if isinstance(raw, bool):
        candidates: tuple[Any, ...] = (raw,)
    elif isinstance(raw, int):
        candidates = (raw,)
    elif isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise GuildConfigurationError(
            f'{field} role {name!r} has an invalid resolution snapshot.'
        )
    else:
        candidates = tuple(raw)
    if len(candidates) != 1:
        raise GuildConfigurationError(
            f'{field} role {name!r} must resolve to exactly one role ID.'
        )
    return _positive_int(candidates[0], f'{field} role {name!r}')


def _resolve_legacy_roles(
    value: Any,
    *,
    guild_id: int,
    role_ids_by_name: Mapping[str, Any],
    field: str,
    allow_multiple_matches: bool = False,
) -> list[int]:
    names = _legacy_role_names(value, field)
    resolved = []
    for name in names:
        if not allow_multiple_matches or name == '@everyone':
            resolved.append(_resolve_legacy_role(
                name,
                guild_id=guild_id,
                role_ids_by_name=role_ids_by_name,
                field=field,
            ))
            continue
        raw = role_ids_by_name.get(name)
        candidates = (
            tuple(raw)
            if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes))
            else ()
        )
        if not candidates:
            raise GuildConfigurationError(
                f'{field} role {name!r} does not resolve in guild {guild_id}.'
            )
        resolved.extend(
            _positive_int(candidate, f'{field} role {name!r}')
            for candidate in candidates
        )
    if len(resolved) != len(set(resolved)):
        raise GuildConfigurationError(
            f'{field} resolves multiple names to the same role ID.'
        )
    return resolved


def _legacy_id_list(value: Any, field: str) -> list[int] | None:
    if value is None:
        return None
    return list(_id_tuple(value, field, maximum=MAX_CHANNEL_IDS))


def materialize_legacy_document(
    *,
    guild_id: int,
    defaults: Mapping[str, Any],
    overrides: Mapping[str, Any],
    role_ids_by_name: Mapping[str, Any],
    command_capabilities: Sequence[str] = (),
    allow_multiple_role_matches: bool = False,
) -> GuildConfigurationDocument:
    """Convert one complete legacy default+override snapshot without I/O.

    Role resolution is supplied explicitly by a future Discord-side plan step;
    this function never imports Discord or looks up live objects itself.
    """

    normalized_guild_id = _positive_int(guild_id, 'guild_id')
    default_values = _exact_mapping(defaults, LEGACY_DEFAULT_KEYS, 'legacy defaults')
    if not isinstance(overrides, Mapping):
        raise GuildConfigurationError('legacy overrides must be an object.')
    unknown_overrides = set(overrides) - LEGACY_DEFAULT_KEYS
    if unknown_overrides:
        raise GuildConfigurationError(
            'legacy overrides contain unknown fields: '
            + ', '.join(sorted(str(item) for item in unknown_overrides))
        )
    if not isinstance(role_ids_by_name, Mapping):
        raise GuildConfigurationError('role_ids_by_name must be an object.')
    if isinstance(command_capabilities, (str, bytes)) or not isinstance(
        command_capabilities,
        Sequence,
    ):
        raise GuildConfigurationError(
            'command_capabilities must be a sequence of capability names.'
        )
    if not isinstance(allow_multiple_role_matches, bool):
        raise GuildConfigurationError(
            'allow_multiple_role_matches must be enabled or disabled.'
        )
    effective = dict(default_values)
    effective.update(overrides)
    if effective['match_challenge_channel'] is not None:
        raise GuildConfigurationError(
            'obsolete match_challenge_channel must be cleared before migration.'
        )

    inactive_name = effective['inactive_role']
    if inactive_name is not None and (
        not isinstance(inactive_name, str) or not inactive_name
    ):
        raise GuildConfigurationError(
            'legacy inactive_role must be a role name or null.'
        )
    inactive_role_id = (
        None
        if inactive_name is None
        else _resolve_legacy_role(
            inactive_name,
            guild_id=normalized_guild_id,
            role_ids_by_name=role_ids_by_name,
            field='inactive_role',
        )
    )
    return validate_document({
        'schema_version': SCHEMA_VERSION,
        'guild_id': normalized_guild_id,
        'identity': {
            'display_name': effective['display_name'],
            'command_prefix': effective['command_prefix'],
        },
        'permissions': {
            'helper_role_ids': _resolve_legacy_roles(
                effective['helper_roles'],
                guild_id=normalized_guild_id,
                role_ids_by_name=role_ids_by_name,
                field='helper_roles',
                allow_multiple_matches=allow_multiple_role_matches,
            ),
            'mod_role_ids': _resolve_legacy_roles(
                effective['mod_roles'],
                guild_id=normalized_guild_id,
                role_ids_by_name=role_ids_by_name,
                field='mod_roles',
                allow_multiple_matches=allow_multiple_role_matches,
            ),
            'user_role_ids_level_1': _resolve_legacy_roles(
                effective['user_roles_level_1'],
                guild_id=normalized_guild_id,
                role_ids_by_name=role_ids_by_name,
                field='user_roles_level_1',
                allow_multiple_matches=allow_multiple_role_matches,
            ),
            'user_role_ids_level_2': _resolve_legacy_roles(
                effective['user_roles_level_2'],
                guild_id=normalized_guild_id,
                role_ids_by_name=role_ids_by_name,
                field='user_roles_level_2',
                allow_multiple_matches=allow_multiple_role_matches,
            ),
            'user_role_ids_level_3': _resolve_legacy_roles(
                effective['user_roles_level_3'],
                guild_id=normalized_guild_id,
                role_ids_by_name=role_ids_by_name,
                field='user_roles_level_3',
                allow_multiple_matches=allow_multiple_role_matches,
            ),
            'user_role_ids_level_4': _resolve_legacy_roles(
                effective['user_roles_level_4'],
                guild_id=normalized_guild_id,
                role_ids_by_name=role_ids_by_name,
                field='user_roles_level_4',
                allow_multiple_matches=allow_multiple_role_matches,
            ),
            'inactive_role_id': inactive_role_id,
        },
        'teams': {
            'require_teams': effective['require_teams'],
            'allow_teams': effective['allow_teams'],
            'allow_uneven_teams': effective['allow_uneven_teams'],
            'max_team_size': effective['max_team_size'],
        },
        'visibility': {
            'include_in_global_leaderboard': effective['include_in_global_lb'],
        },
        'channels': {
            'bot_channel_ids': _legacy_id_list(
                effective['bot_channels'], 'bot_channels'
            ),
            'strict_bot_channel_ids': _legacy_id_list(
                effective['bot_channels_strict'], 'bot_channels_strict'
            ),
            'private_bot_channel_ids': _legacy_id_list(
                effective['bot_channels_private'], 'bot_channels_private'
            ),
            'newbie_message_channel_ids': _legacy_id_list(
                effective['newbie_message_channels'], 'newbie_message_channels'
            ),
            'match_challenge_channel_ids': _legacy_id_list(
                effective['match_challenge_channels'], 'match_challenge_channels'
            ),
            'ranked_game_channel_id': effective['ranked_game_channel'],
            'unranked_game_channel_id': effective['unranked_game_channel'],
            'steam_game_channel_id': effective['steam_game_channel'],
            'log_channel_id': effective['log_channel'],
            'game_announce_channel_id': effective['game_announce_channel'],
            'staff_help_channel_id': effective['staff_help_channel'],
            'game_category_ids': _legacy_id_list(
                effective['game_channel_categories'], 'game_channel_categories'
            ),
        },
        'command_capabilities': list(command_capabilities),
    })
