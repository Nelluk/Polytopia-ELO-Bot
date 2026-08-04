"""Read-only wider-beta readiness inventory and offline planning helpers.

The Discord inventory code is deliberately object-shape based so it can run
inside the already-authenticated beta without importing the database models.
The database inventory is an explicit CLI-only path and opens its own
read-only Peewee connection after the development identity gates pass.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
import re
import stat
from typing import Any, Callable

from modules.application_command_policy import (
    TOOLS_SUPPORT_IMPLEMENTED_ROOTS,
    TOOLS_SUPPORT_RESERVED_ROOTS,
)


READINESS_SCHEMA_VERSION = 1
DISCORD_INVENTORY_SCHEMA_VERSION = 1
DATABASE_INVENTORY_SCHEMA_VERSION = 1

BETA_GUILD_ID = 478571892832206869
BETA_APPLICATION_ID = 479029527553638401
BETA_DATABASE_NAME = 'polytopia_dev'
BETA_DATABASE_ROLE = 'polybot_dev'
BETA_PUBLIC_RELEASE_CHANNEL_ID = 481779940124000256
BETA_PUBLIC_RELEASE_CHANNEL_NAME = 'todo-and-changelog'
BETA_STAFFHELP_MIRROR_CHANNEL_ID = 480078679930830849
BETA_STAFFHELP_MIRROR_CHANNEL_NAME = 'admin-spam'
BETA_TESTER_ROLE_NAME = 'testers'
BETA_PINNED_TESTER_ROLE_ID = 480905534019731476

MAX_SNAPSHOT_BYTES = 256 * 1024
MAX_SNAPSHOT_DEPTH = 8
MAX_DISCORD_ROLES = 512
MAX_DISCORD_CHANNELS = 512
MAX_PERMISSION_OVERWRITES = 128
MAX_DATABASE_TEAMS = 256
MAX_DATABASE_HOUSES = 128
MAX_DATABASE_FIXTURE_ROWS = 128
MAX_MANIFEST_BYTES = 128 * 1024
MAX_MANIFEST_DEPTH = 8
MAX_PROPOSED_TEAMS = 20
MAX_PROPOSED_HOUSES = 20
MAX_PROPOSED_ROLE_BINDINGS = 64
MAX_FIXTURE_PLANS = 16
MAX_LIFECYCLE_STEPS = 16
MAX_CHECKLIST_ITEMS = 20
MAX_INVITATION_PREREQUISITES = 16
MAX_UNRESOLVED_DECISIONS = 16
MAX_TEXT_LENGTH = 500
MAX_SHORT_TEXT_LENGTH = 200

KNOWN_CAPABILITIES = frozenset({
    'core_user',
    'elo_maintenance',
    'team',
    'league',
    'house',
    'squad',
    'tools_support',
})

_CONTROL_CHARACTER = re.compile(r'[\r\n\x00-\x1f\x7f]')
_FORBIDDEN_SNAPSHOT_KEYS = frozenset({
    'token',
    'discord_token',
    'members',
    'member_list',
    'messages',
    'message_content',
    'staffhelp_body',
    'staffhelp_details',
    'attachments',
})


class ReadinessError(RuntimeError):
    """Base error for a refused or invalid readiness operation."""


class ReadinessInventoryError(ReadinessError):
    """A live or database inventory could not be made authoritative."""


class ReadinessManifestError(ReadinessError, ValueError):
    """A desired-state manifest is invalid or unsafe."""


class ReadinessPathError(ReadinessError):
    """A supplied offline snapshot or manifest path is unsafe."""


def _bounded_text(value: Any, field: str, maximum: int = MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str):
        raise ReadinessManifestError(f'{field} must be a string.')
    normalized = value.strip()
    if not normalized:
        raise ReadinessManifestError(f'{field} must not be empty.')
    if len(normalized) > maximum:
        raise ReadinessManifestError(
            f'{field} must be at most {maximum} characters.'
        )
    if _CONTROL_CHARACTER.search(normalized):
        raise ReadinessManifestError(f'{field} contains a control character.')
    if '@everyone' in normalized.casefold() or '@here' in normalized.casefold():
        raise ReadinessManifestError(f'{field} may not contain a broadcast mention.')
    return normalized


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ReadinessManifestError(f'{field} must be a positive integer.')
    return value


def _optional_positive_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, field)


def _assert_primitive_tree(value: Any, *, field: str, depth: int = 0) -> None:
    if depth > MAX_SNAPSHOT_DEPTH:
        raise ReadinessInventoryError(f'{field} exceeds the snapshot depth bound.')
    if value is None or isinstance(value, (str, int, bool)):
        return
    if isinstance(value, float):
        raise ReadinessInventoryError(f'{field} contains a non-JSON-safe number.')
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ReadinessInventoryError(f'{field} contains a non-string key.')
            _assert_primitive_tree(item, field=f'{field}.{key}', depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_primitive_tree(item, field=f'{field}[{index}]', depth=depth + 1)
        return
    raise ReadinessInventoryError(f'{field} contains a non-primitive value.')


def _json_size(value: Any) -> int:
    try:
        return len(json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8'))
    except (TypeError, ValueError) as exc:
        raise ReadinessInventoryError('Readiness data is not JSON-safe.') from exc


def _primitive(value: Any) -> Any:
    """Convert dates/enums used by fake or live objects to primitives."""

    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value
    isoformat = getattr(value, 'isoformat', None)
    if callable(isoformat):
        return str(isoformat())
    enum_value = getattr(value, 'value', None)
    if isinstance(enum_value, (str, int, bool)):
        return enum_value
    return str(value)


def _object_id(value: Any) -> int | None:
    raw = getattr(value, 'id', None)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _object_name(value: Any) -> str | None:
    raw = getattr(value, 'name', None)
    return str(raw) if raw is not None else None


def _channel_type(value: Any) -> str:
    raw = getattr(value, 'type', None)
    name = getattr(raw, 'name', None)
    if isinstance(name, str) and name:
        return name
    if isinstance(raw, str) and raw:
        return raw
    value = getattr(raw, 'value', None)
    if isinstance(value, int):
        return str(value)
    return type(value).__name__ if value is not None else 'unknown'


def _role_member_count(role: Any) -> int | None:
    try:
        members = getattr(role, 'members')
        return int(len(members))
    except (AttributeError, TypeError, ValueError):
        return None


def _permission_bits(value: Any) -> int | None:
    if value is None:
        return None
    raw = getattr(value, 'value', value)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _role_inventory_entry(role: Any) -> dict[str, Any]:
    permissions = _permission_bits(getattr(role, 'permissions', None))
    position = getattr(role, 'position', None)
    try:
        position = int(position)
    except (TypeError, ValueError):
        position = None
    is_default = getattr(role, 'is_default', None)
    if callable(is_default):
        try:
            is_default = bool(is_default())
        except Exception:
            is_default = None
    return {
        'id': _object_id(role),
        'name': _object_name(role),
        'managed': bool(getattr(role, 'managed', False)),
        'position': position,
        'member_count': _role_member_count(role),
        'permissions': permissions,
        'is_default': is_default if isinstance(is_default, bool) else None,
        'mentionable': bool(getattr(role, 'mentionable', False)),
        'hoist': bool(getattr(role, 'hoist', False)),
    }


def _iter_overwrites(channel: Any) -> tuple[tuple[Any, Any], ...]:
    raw = getattr(channel, 'overwrites', ())
    if callable(raw):
        raw = raw()
    if isinstance(raw, Mapping):
        return tuple(raw.items())
    try:
        return tuple(raw or ())
    except TypeError:
        return ()


def _overwrite_kind(target: Any) -> str:
    explicit = getattr(target, 'target_kind', None)
    if explicit in {'role', 'member'}:
        return explicit
    class_name = type(target).__name__.casefold()
    if 'role' in class_name:
        return 'role'
    return 'member'


def _overwrite_bits(overwrite: Any) -> tuple[int | None, int | None]:
    pair = getattr(overwrite, 'pair', None)
    if callable(pair):
        try:
            allow, deny = pair()
            return _permission_bits(allow), _permission_bits(deny)
        except Exception:
            pass
    if isinstance(overwrite, Mapping):
        return (
            _permission_bits(overwrite.get('allow')),
            _permission_bits(overwrite.get('deny')),
        )
    return (
        _permission_bits(getattr(overwrite, 'allow', None)),
        _permission_bits(getattr(overwrite, 'deny', None)),
    )


def _permission_overwrites(channel: Any) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    member_count = 0
    raw_entries = _iter_overwrites(channel)
    for target, overwrite in raw_entries[:MAX_PERMISSION_OVERWRITES]:
        kind = _overwrite_kind(target)
        allow, deny = _overwrite_bits(overwrite)
        if kind == 'role':
            entries.append({
                'target_kind': 'role',
                'target_id': _object_id(target),
                'target_name': _object_name(target),
                'allow': allow,
                'deny': deny,
            })
        else:
            # A member-specific overwrite is useful for permission auditing,
            # but its identity is intentionally omitted from the snapshot.
            member_count += 1
            entries.append({
                'target_kind': 'member',
                'target_id': None,
                'target_name': None,
                'allow': allow,
                'deny': deny,
            })
    entries.sort(key=lambda item: (
        item['target_kind'],
        item['target_id'] or 0,
        item['target_name'] or '',
        item['allow'] or 0,
        item['deny'] or 0,
    ))
    return {
        'entries': entries,
        'total_count': len(raw_entries),
        'member_overwrite_count': member_count + max(
            0, len(raw_entries) - MAX_PERMISSION_OVERWRITES
        ),
        'truncated': len(raw_entries) > MAX_PERMISSION_OVERWRITES,
    }


def _channel_inventory_entry(channel: Any, bot_member: Any) -> dict[str, Any]:
    category = getattr(channel, 'category', None)
    category_id = getattr(channel, 'category_id', None)
    if category_id is None:
        category_id = _object_id(category)
    try:
        category_id = int(category_id) if category_id is not None else None
    except (TypeError, ValueError):
        category_id = None
    position = getattr(channel, 'position', None)
    try:
        position = int(position)
    except (TypeError, ValueError):
        position = None
    bot_permissions = None
    permissions_for = getattr(channel, 'permissions_for', None)
    if callable(permissions_for) and bot_member is not None:
        try:
            bot_permissions = _permission_bits(permissions_for(bot_member))
        except Exception:
            bot_permissions = None
    return {
        'id': _object_id(channel),
        'name': _object_name(channel),
        'type': _channel_type(channel),
        'category_id': category_id,
        'category_name': _object_name(category),
        'position': position,
        'nsfw': bool(getattr(channel, 'nsfw', False)),
        'bot_permissions': bot_permissions,
        'permission_overwrites': _permission_overwrites(channel),
    }


def _sorted_objects(values: Iterable[Any]) -> tuple[Any, ...]:
    return tuple(sorted(
        tuple(values or ()),
        key=lambda item: (
            _object_id(item) or 0,
            _object_name(item) or '',
        ),
    ))


def _fixed_channel(
        guild: Any,
        channel_id: int,
        channel_name: str,
        label: str) -> dict[str, Any]:
    get_channel = getattr(guild, 'get_channel', None)
    channel = get_channel(channel_id) if callable(get_channel) else None
    if channel is None:
        raise ReadinessInventoryError(f'The fixed {label} channel is not cached.')
    channel_guild = getattr(channel, 'guild', None)
    if (
            _object_id(channel) != channel_id
            or _object_name(channel) != channel_name
            or _object_id(channel_guild) != BETA_GUILD_ID):
        raise ReadinessInventoryError(
            f'The fixed {label} channel does not match its pinned ID/name.'
        )
    if _channel_type(channel) == 'category':
        raise ReadinessInventoryError(f'The fixed {label} target is a category, not a channel.')
    category_id = getattr(channel, 'category_id', None)
    try:
        category_id = int(category_id) if category_id is not None else None
    except (TypeError, ValueError):
        category_id = None
    return {
        'id': channel_id,
        'name': channel_name,
        'type': _channel_type(channel),
        'category_id': category_id,
        'verified': True,
    }


def build_discord_inventory(
        *,
        bot: Any,
        profile: Any,
        pinned_tester_role_id: int,
        public_channel_id: int = BETA_PUBLIC_RELEASE_CHANNEL_ID,
        public_channel_name: str = BETA_PUBLIC_RELEASE_CHANNEL_NAME,
        staffhelp_channel_id: int = BETA_STAFFHELP_MIRROR_CHANNEL_ID,
        staffhelp_channel_name: str = BETA_STAFFHELP_MIRROR_CHANNEL_NAME,
        tester_role_name: str = BETA_TESTER_ROLE_NAME) -> dict[str, Any]:
    """Build a deterministic, privacy-bounded inventory from cached Discord data."""

    if (
            public_channel_id != BETA_PUBLIC_RELEASE_CHANNEL_ID
            or public_channel_name != BETA_PUBLIC_RELEASE_CHANNEL_NAME
            or staffhelp_channel_id != BETA_STAFFHELP_MIRROR_CHANNEL_ID
            or staffhelp_channel_name != BETA_STAFFHELP_MIRROR_CHANNEL_NAME
            or tester_role_name != BETA_TESTER_ROLE_NAME):
        raise ReadinessInventoryError('The readiness inventory fixed targets are immutable.')
    try:
        pinned_tester_role_id = int(pinned_tester_role_id)
    except (TypeError, ValueError) as exc:
        raise ReadinessInventoryError('The pinned testers role ID is invalid.') from exc
    if pinned_tester_role_id <= 0:
        raise ReadinessInventoryError('The pinned testers role ID is invalid.')

    user = getattr(bot, 'user', None)
    if _object_id(user) != BETA_APPLICATION_ID:
        raise ReadinessInventoryError('The authenticated bot ID is not the approved beta application.')
    is_ready = getattr(bot, 'is_ready', None)
    if callable(is_ready) and not is_ready():
        raise ReadinessInventoryError('The authenticated beta bot is not ready.')
    get_guild = getattr(bot, 'get_guild', None)
    guild = get_guild(BETA_GUILD_ID) if callable(get_guild) else None
    if guild is None or _object_id(guild) != BETA_GUILD_ID:
        raise ReadinessInventoryError('The approved development guild is not cached.')
    try:
        allowed = tuple(sorted(int(value) for value in getattr(profile, 'allowed_guild_ids', ())))
    except (TypeError, ValueError) as exc:
        raise ReadinessInventoryError('The development profile guild allowlist is invalid.') from exc
    if allowed != (BETA_GUILD_ID,):
        raise ReadinessInventoryError('The development profile does not allow exactly the approved guild.')

    roles = _sorted_objects(getattr(guild, 'roles', ()))
    tester_matches = tuple(
        role for role in roles if _object_name(role) == tester_role_name
    )
    if len(tester_matches) != 1:
        raise ReadinessInventoryError(
            'The testers role must resolve to exactly one cached role.'
        )
    live_tester_role_id = _object_id(tester_matches[0])
    if live_tester_role_id != int(pinned_tester_role_id):
        raise ReadinessInventoryError(
            'The live testers role ID does not match the pinned role ID.'
        )

    public = _fixed_channel(
        guild, public_channel_id, public_channel_name, 'public release'
    )
    private = _fixed_channel(
        guild, staffhelp_channel_id, staffhelp_channel_name, 'private staffhelp mirror'
    )

    bot_member = getattr(guild, 'me', None)
    if bot_member is None:
        get_member = getattr(guild, 'get_member', None)
        if callable(get_member):
            bot_member = get_member(BETA_APPLICATION_ID)
    categories = tuple(
        _channel_inventory_entry(channel, bot_member)
        for channel in _sorted_objects(getattr(guild, 'categories', ()))
    )
    channels = tuple(
        _channel_inventory_entry(channel, bot_member)
        for channel in _sorted_objects(getattr(guild, 'channels', ()))
        if _channel_type(channel) != 'category'
    )
    roles_total = len(roles)
    channels_total = len(channels)
    categories_total = len(categories)

    settings = getattr(profile, 'server_settings', None)
    assignments = getattr(settings, 'application_command_capabilities', {})
    current_capabilities: tuple[str, ...] = ()
    if isinstance(assignments, Mapping):
        raw = assignments.get(BETA_GUILD_ID, ())
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            current_capabilities = tuple(sorted({str(value) for value in raw}))

    result = {
        'schema_version': DISCORD_INVENTORY_SCHEMA_VERSION,
        'kind': 'discord_guild_inventory',
        'target': {
            'environment': str(getattr(profile, 'environment', '')),
            'guild_id': BETA_GUILD_ID,
            'application_id': BETA_APPLICATION_ID,
        },
        'bot': {
            'id': BETA_APPLICATION_ID,
            'name': _object_name(user),
            'ready': True,
        },
        'guild': {
            'id': BETA_GUILD_ID,
            'name': _object_name(guild),
            'member_count': _primitive(getattr(guild, 'member_count', None)),
        },
        'tester_role': {
            'name': tester_role_name,
            'live_id': live_tester_role_id,
            'pinned_id': int(pinned_tester_role_id),
            'match_count': len(tester_matches),
            'verified': True,
        },
        'fixed_channels': {
            'public_release': public,
            'staffhelp_mirror': private,
        },
        'capabilities': {
            'current': list(current_capabilities),
        },
        'roles': [
            _role_inventory_entry(role) for role in roles[:MAX_DISCORD_ROLES]
        ],
        'roles_total': roles_total,
        'roles_truncated': roles_total > MAX_DISCORD_ROLES,
        'categories': list(categories[:MAX_DISCORD_CHANNELS]),
        'categories_total': categories_total,
        'categories_truncated': categories_total > MAX_DISCORD_CHANNELS,
        'channels': list(channels[:MAX_DISCORD_CHANNELS]),
        'channels_total': channels_total,
        'channels_truncated': channels_total > MAX_DISCORD_CHANNELS,
        'privacy': {
            'member_lists_included': False,
            'message_content_included': False,
            'private_staffhelp_bodies_included': False,
            'tokens_included': False,
        },
    }
    _assert_primitive_tree(result, field='discord_inventory')
    if _json_size(result) > MAX_SNAPSHOT_BYTES:
        raise ReadinessInventoryError('The Discord readiness inventory exceeds its size bound.')
    return result


def validate_database_profile(profile: Any, guild_id: int = BETA_GUILD_ID) -> int:
    """Validate the complete development-only database identity before import/connect."""

    if (
            getattr(profile, 'environment', None) != 'development'
            or getattr(profile, 'database_name', None) != BETA_DATABASE_NAME
            or getattr(profile, 'database_user', None) != BETA_DATABASE_ROLE
            or bool(getattr(profile, 'background_tasks_enabled', True))
            or bool(getattr(profile, 'api_enabled', True))
            or bool(getattr(profile, 'bullet_enabled', True))):
        raise ReadinessInventoryError(
            'Database readiness inventory requires POLYBOT_ENV=development, '
            f'{BETA_DATABASE_NAME}, {BETA_DATABASE_ROLE}, and disabled integrations.'
        )
    try:
        normalized_guild_id = int(guild_id)
    except (TypeError, ValueError) as exc:
        raise ReadinessInventoryError('The readiness guild ID is invalid.') from exc
    try:
        allowed = tuple(sorted(int(value) for value in getattr(profile, 'allowed_guild_ids', ())))
    except (TypeError, ValueError) as exc:
        raise ReadinessInventoryError('The development profile guild allowlist is invalid.') from exc
    if allowed != (BETA_GUILD_ID,) or normalized_guild_id != BETA_GUILD_ID:
        raise ReadinessInventoryError(
            f'Database readiness inventory is restricted to guild {BETA_GUILD_ID}.'
        )
    return normalized_guild_id


def validate_live_database_identity(database_name: str, database_role: str) -> None:
    if database_name != BETA_DATABASE_NAME or database_role != BETA_DATABASE_ROLE:
        raise ReadinessInventoryError(
            'The live PostgreSQL session is not connected to the approved '
            f'{BETA_DATABASE_NAME} database as {BETA_DATABASE_ROLE}.'
        )


def _database_rows(database: Any, query: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
    cursor = database.execute_sql(query, tuple(params))
    rows = cursor.fetchall()
    return [tuple(row) for row in rows]


def _database_count(database: Any, query: str, params: Sequence[Any] = ()) -> int:
    rows = _database_rows(database, query, params)
    if len(rows) != 1:
        raise ReadinessInventoryError('A bounded database count returned an unexpected shape.')
    try:
        return int(rows[0][0])
    except (IndexError, TypeError, ValueError) as exc:
        raise ReadinessInventoryError('A database count was not an integer.') from exc


def _fixture_game(row: Sequence[Any]) -> dict[str, Any]:
    return {
        'id': int(row[0]),
        'name': str(row[1]) if row[1] is not None else None,
        'completed': bool(row[2]),
        'confirmed': bool(row[3]),
        'ranked': bool(row[4]),
        'pending': bool(row[5]),
        'expiration': _primitive(row[6]),
    }


def _range_or_empty(values: Sequence[int]) -> dict[str, Any]:
    normalized = sorted(int(value) for value in values)
    return {
        'count': len(normalized),
        'first': normalized[0] if normalized else None,
        'last': normalized[-1] if normalized else None,
        'ids': normalized,
    }


def _default_database_factory(profile: Any) -> Any:
    from playhouse.postgres_ext import PostgresqlExtDatabase

    connection_settings: dict[str, Any] = {
        'user': profile.database_user,
        'password': profile.database_password,
        'autoconnect': False,
    }
    if profile.database_host:
        connection_settings['host'] = profile.database_host
    if profile.database_port:
        connection_settings['port'] = profile.database_port
    return PostgresqlExtDatabase(profile.database_name, **connection_settings)


def read_development_database_inventory(
        *,
        profile: Any,
        guild_id: int = BETA_GUILD_ID,
        database_factory: Callable[[Any], Any] | None = None) -> dict[str, Any]:
    """Read bounded development DB planning data with a worker-local connection."""

    normalized_guild_id = validate_database_profile(profile, guild_id)
    try:
        database = (
            database_factory(profile)
            if database_factory is not None
            else _default_database_factory(profile)
        )
    except ReadinessError:
        raise
    except Exception as exc:
        raise ReadinessInventoryError(
            'The development database inventory could not create its connection.'
        ) from exc
    try:
        with database.connection_context():
            with database.atomic():
                # This is the first statement in the transaction.  It makes
                # an accidental write fail at PostgreSQL even if this helper
                # is later edited incorrectly.
                database.execute_sql('SET TRANSACTION READ ONLY')
                identity_rows = _database_rows(
                    database, 'SELECT current_database(), current_user'
                )
                if len(identity_rows) != 1 or len(identity_rows[0]) != 2:
                    raise ReadinessInventoryError('The live database identity query returned an unexpected shape.')
                validate_live_database_identity(
                    str(identity_rows[0][0]), str(identity_rows[0][1])
                )

                counts = {
                    'players': _database_count(
                        database,
                        'SELECT COUNT(*) FROM player WHERE guild_id = %s',
                        (normalized_guild_id,),
                    ),
                    'teams': _database_count(
                        database,
                        'SELECT COUNT(*) FROM team WHERE guild_id = %s',
                        (normalized_guild_id,),
                    ),
                    'houses': _database_count(database, 'SELECT COUNT(*) FROM house'),
                    'games': _database_count(
                        database,
                        'SELECT COUNT(*) FROM game WHERE guild_id = %s',
                        (normalized_guild_id,),
                    ),
                }
                team_rows = _database_rows(
                    database,
                    '''
                    SELECT t.id, t.name, t.guild_id, t.house_id,
                           t.is_hidden, t.is_archived, t.league_tier,
                           t.external_server, COUNT(p.id)
                    FROM team AS t
                    LEFT JOIN player AS p
                      ON p.team_id = t.id AND p.guild_id = %s
                    WHERE t.guild_id = %s
                    GROUP BY t.id, t.name, t.guild_id, t.house_id,
                             t.is_hidden, t.is_archived, t.league_tier,
                             t.external_server
                    ORDER BY t.id
                    LIMIT %s
                    ''',
                    (
                        normalized_guild_id,
                        normalized_guild_id,
                        MAX_DATABASE_TEAMS + 1,
                    ),
                )
                house_rows = _database_rows(
                    database,
                    'SELECT id, name FROM house ORDER BY id LIMIT %s',
                    (MAX_DATABASE_HOUSES + 1,),
                )
                fixture_rows = _database_rows(
                    database,
                    '''
                    SELECT id, name, is_completed, is_confirmed,
                           is_ranked, is_pending, expiration
                    FROM game
                    WHERE guild_id = %s AND notes = %s
                    ORDER BY id
                    LIMIT %s
                    ''',
                    (
                        normalized_guild_id,
                        'polybot-dev-beta-fixture:v1',
                        MAX_DATABASE_FIXTURE_ROWS + 1,
                    ),
                )
                leaderboard_player_rows = _database_rows(
                    database,
                    '''
                    SELECT p.id, dm.discord_id
                    FROM player AS p
                    JOIN discordmember AS dm ON dm.id = p.discord_member_id
                    WHERE p.guild_id = %s
                      AND dm.discord_id BETWEEN %s AND %s
                    ORDER BY dm.discord_id
                    LIMIT %s
                    ''',
                    (
                        normalized_guild_id,
                        9_000_000_000_100_000_001,
                        9_000_000_000_100_000_024,
                        MAX_DATABASE_FIXTURE_ROWS + 1,
                    ),
                )
                leaderboard_game_rows = _database_rows(
                    database,
                    '''
                    SELECT id
                    FROM game
                    WHERE guild_id = %s AND notes = %s
                    ORDER BY id
                    LIMIT %s
                    ''',
                    (
                        normalized_guild_id,
                        'polybot-dev-lb2-showcase:v1',
                        MAX_DATABASE_FIXTURE_ROWS + 1,
                    ),
                )

    except ReadinessError:
        raise
    except Exception as exc:
        raise ReadinessInventoryError(
            'The development database inventory failed closed.'
        ) from exc

    try:
        teams = [
            {
                'id': int(row[0]),
                'name': str(row[1]),
                'guild_id': int(row[2]),
                'house_id': int(row[3]) if row[3] is not None else None,
                'hidden': bool(row[4]),
                'archived': bool(row[5]),
                'league_tier': int(row[6]) if row[6] is not None else None,
                'external_server': int(row[7]) if row[7] is not None else None,
                'player_count': int(row[8]),
            }
            for row in team_rows[:MAX_DATABASE_TEAMS]
        ]
        houses = [
            {'id': int(row[0]), 'name': str(row[1])}
            for row in house_rows[:MAX_DATABASE_HOUSES]
        ]
        fixture_games = [
            _fixture_game(row) for row in fixture_rows[:MAX_DATABASE_FIXTURE_ROWS]
        ]
        bounded_leaderboard_players = leaderboard_player_rows[:MAX_DATABASE_FIXTURE_ROWS]
        bounded_leaderboard_games = leaderboard_game_rows[:MAX_DATABASE_FIXTURE_ROWS]
        leaderboard_player_ids = [int(row[0]) for row in bounded_leaderboard_players]
        leaderboard_discord_ids = [int(row[1]) for row in bounded_leaderboard_players]
        leaderboard_game_ids = [int(row[0]) for row in bounded_leaderboard_games]
    except Exception as exc:
        raise ReadinessInventoryError(
            'The development database inventory returned an invalid row shape.'
        ) from exc

    result = {
        'schema_version': DATABASE_INVENTORY_SCHEMA_VERSION,
        'kind': 'development_database_inventory',
        'target': {
            'environment': 'development',
            'guild_id': normalized_guild_id,
            'database': BETA_DATABASE_NAME,
            'database_role': BETA_DATABASE_ROLE,
        },
        'counts': counts,
        'teams': teams,
        'teams_total': counts['teams'],
        'teams_truncated': counts['teams'] > MAX_DATABASE_TEAMS,
        'houses': houses,
        'houses_total': counts['houses'],
        'houses_truncated': counts['houses'] > MAX_DATABASE_HOUSES,
        'role_binding_identifiers': {
            'team_roles': [
                {
                    'source_id': team['id'],
                    'role_name': team['name'],
                    'source': 'team.name',
                }
                for team in teams
            ],
            'house_roles': [
                {
                    'source_id': house['id'],
                    'role_name': house['name'],
                    'source': 'house.name',
                }
                for house in houses
            ],
            'role_ids_resolved': False,
        },
        'fixtures': {
            'beta_games': {
                'ownership_marker': 'polybot-dev-beta-fixture:v1',
                'count': len(fixture_games),
                'games': fixture_games,
                'truncated': len(fixture_rows) > MAX_DATABASE_FIXTURE_ROWS,
            },
            'leaderboard_showcase': {
                'ownership_marker': 'polybot-dev-lb2-showcase:v1',
                'players': _range_or_empty(leaderboard_discord_ids),
                'player_record_ids': _range_or_empty(leaderboard_player_ids),
                'games': _range_or_empty(leaderboard_game_ids),
                'truncated': (
                    len(leaderboard_player_rows) > MAX_DATABASE_FIXTURE_ROWS
                    or len(leaderboard_game_rows) > MAX_DATABASE_FIXTURE_ROWS
                ),
            },
        },
        'privacy': {
            'member_lists_included': False,
            'message_content_included': False,
            'game_notes_included': False,
            'tokens_included': False,
        },
    }
    _assert_primitive_tree(result, field='database_inventory')
    if _json_size(result) > MAX_SNAPSHOT_BYTES:
        raise ReadinessInventoryError('The database readiness inventory exceeds its size bound.')
    return result


def _validate_snapshot_key_safety(value: Any, *, field: str = 'snapshot') -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in _FORBIDDEN_SNAPSHOT_KEYS:
                raise ReadinessManifestError(
                    f'{field} contains a forbidden sensitive field: {key}.'
                )
            _validate_snapshot_key_safety(item, field=f'{field}.{key}')
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_snapshot_key_safety(item, field=f'{field}[{index}]')


def validate_inventory_snapshot(value: Any, *, kind: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ReadinessManifestError('An inventory snapshot must be a JSON object.')
    _assert_primitive_tree(value, field='inventory_snapshot')
    _validate_snapshot_key_safety(value)
    if _json_size(value) > MAX_SNAPSHOT_BYTES:
        raise ReadinessManifestError('The inventory snapshot exceeds its size bound.')
    if value.get('schema_version') not in {
        DISCORD_INVENTORY_SCHEMA_VERSION,
        DATABASE_INVENTORY_SCHEMA_VERSION,
    } or value.get('kind') != kind:
        raise ReadinessManifestError(f'Expected a {kind} snapshot with a supported schema.')
    return dict(value)


def _manifest_list(
        value: Any,
        field: str,
        maximum: int,
        *,
        item_validator: Callable[[Any, str], Any] | None = None) -> list[Any]:
    if not isinstance(value, list):
        raise ReadinessManifestError(f'{field} must be a JSON list.')
    if len(value) > maximum:
        raise ReadinessManifestError(f'{field} may contain at most {maximum} items.')
    output = []
    for index, item in enumerate(value):
        output.append(
            item_validator(item, f'{field}[{index}]')
            if item_validator is not None else item
        )
    return output


def _validate_text_item(value: Any, field: str) -> str:
    return _bounded_text(value, field, MAX_SHORT_TEXT_LENGTH)


def _validate_capability_list(value: Any, field: str) -> list[str]:
    values = _manifest_list(value, field, len(KNOWN_CAPABILITIES))
    result = []
    for index, item in enumerate(values):
        name = _bounded_text(item, f'{field}[{index}]', MAX_SHORT_TEXT_LENGTH)
        if name not in KNOWN_CAPABILITIES:
            raise ReadinessManifestError(f'{field}[{index}] names an unknown capability.')
        result.append(name)
    if len(result) != len(set(result)):
        raise ReadinessManifestError(f'{field} contains duplicate capabilities.')
    return sorted(result)


def _validate_proposed_team(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {'name', 'house_name', 'role_name'}:
        raise ReadinessManifestError(
            f'{field} must contain exactly name, house_name, and role_name.'
        )
    return {
        'name': _bounded_text(value['name'], f'{field}.name'),
        'house_name': (
            _bounded_text(value['house_name'], f'{field}.house_name')
            if value['house_name'] is not None else None
        ),
        'role_name': _bounded_text(value['role_name'], f'{field}.role_name'),
    }


def _validate_proposed_house(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {'name', 'role_name'}:
        raise ReadinessManifestError(
            f'{field} must contain exactly name and role_name.'
        )
    return {
        'name': _bounded_text(value['name'], f'{field}.name'),
        # A house role is deliberately optional: WB1.3b creates no house
        # roles, while retaining the field keeps the template extensible for
        # a later reviewed house-role decision.
        'role_name': (
            _bounded_text(value['role_name'], f'{field}.role_name')
            if value['role_name'] is not None else None
        ),
    }


def _validate_role_binding(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        'kind', 'source_id', 'role_name', 'role_id'
    }:
        raise ReadinessManifestError(
            f'{field} must contain exactly kind, source_id, role_name, and role_id.'
        )
    kind = _bounded_text(value['kind'], f'{field}.kind', 20)
    if kind not in {'team', 'house'}:
        raise ReadinessManifestError(f'{field}.kind must be team or house.')
    source_id = _optional_positive_int(
        value['source_id'], f'{field}.source_id'
    )
    role_id = _optional_positive_int(value['role_id'], f'{field}.role_id')
    if source_id is None and role_id is None:
        raise ReadinessManifestError(
            f'{field} must pin a Discord role ID before its database source ID exists.'
        )
    return {
        'kind': kind,
        # Proposed records do not have database IDs until the separate setup
        # transaction creates them.  Existing bindings retain their source
        # ID; a pending binding must still pin the Discord role ID.
        'source_id': source_id,
        'role_name': _bounded_text(value['role_name'], f'{field}.role_name'),
        'role_id': role_id,
    }


def _validate_fixture_plan(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        'family', 'ids', 'player_count'
    }:
        raise ReadinessManifestError(
            f'{field} must contain exactly family, ids, and player_count.'
        )
    family = _bounded_text(value['family'], f'{field}.family', 80)
    ids = value['ids']
    if not isinstance(ids, list) or len(ids) > MAX_DATABASE_FIXTURE_ROWS:
        raise ReadinessManifestError(f'{field}.ids must be a bounded list.')
    normalized = [_positive_int(item, f'{field}.ids[{index}]') for index, item in enumerate(ids)]
    if len(normalized) != len(set(normalized)):
        raise ReadinessManifestError(f'{field}.ids contains duplicates.')
    player_count = value['player_count']
    if player_count is not None:
        if isinstance(player_count, bool) or not isinstance(player_count, int) or player_count < 0:
            raise ReadinessManifestError(
                f'{field}.player_count must be null or a non-negative integer.'
            )
        if player_count > MAX_DATABASE_FIXTURE_ROWS:
            raise ReadinessManifestError(
                f'{field}.player_count exceeds the fixture bound.'
            )
    return {
        'family': family,
        'ids': sorted(normalized),
        'player_count': player_count,
    }


def _validate_optional_decision(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {'capability', 'decision', 'reason'}:
        raise ReadinessManifestError(
            f'{field} must contain exactly capability, decision, and reason.'
        )
    capability = _bounded_text(value['capability'], f'{field}.capability', 50)
    if capability not in KNOWN_CAPABILITIES:
        raise ReadinessManifestError(f'{field}.capability is unknown.')
    decision = _bounded_text(value['decision'], f'{field}.decision', 20)
    if decision not in {'include', 'exclude', 'unresolved'}:
        raise ReadinessManifestError(
            f'{field}.decision must be include, exclude, or unresolved.'
        )
    return {
        'capability': capability,
        'decision': decision,
        'reason': _bounded_text(value['reason'], f'{field}.reason'),
    }


def validate_readiness_manifest(value: Any) -> dict[str, Any]:
    """Validate and normalize a repository-backed desired-state manifest."""

    if not isinstance(value, Mapping):
        raise ReadinessManifestError('The readiness manifest must be a JSON object.')
    _assert_primitive_tree(value, field='readiness_manifest')
    if _json_size(value) > MAX_MANIFEST_BYTES:
        raise ReadinessManifestError('The readiness manifest exceeds its size bound.')
    expected = {
        'schema_version', 'target', 'discord', 'capabilities', 'database',
        'lifecycle', 'smoke',
    }
    if set(value) != expected:
        raise ReadinessManifestError(
            'The readiness manifest must contain exactly: '
            + ', '.join(sorted(expected))
        )
    if type(value['schema_version']) is not int or value['schema_version'] != READINESS_SCHEMA_VERSION:
        raise ReadinessManifestError('Unsupported readiness manifest schema version.')

    target = value['target']
    if not isinstance(target, Mapping) or set(target) != {
        'environment', 'guild_id', 'application_id', 'database', 'database_role'
    }:
        raise ReadinessManifestError('target has an invalid shape.')
    normalized_target = {
        'environment': _bounded_text(target['environment'], 'target.environment', 30),
        'guild_id': _positive_int(target['guild_id'], 'target.guild_id'),
        'application_id': _positive_int(target['application_id'], 'target.application_id'),
        'database': _bounded_text(target['database'], 'target.database', 80),
        'database_role': _bounded_text(target['database_role'], 'target.database_role', 80),
    }
    expected_target = {
        'environment': 'development',
        'guild_id': BETA_GUILD_ID,
        'application_id': BETA_APPLICATION_ID,
        'database': BETA_DATABASE_NAME,
        'database_role': BETA_DATABASE_ROLE,
    }
    if normalized_target != expected_target:
        raise ReadinessManifestError(
            'target must identify only the approved development guild, application, and database.'
        )

    discord_value = value['discord']
    if not isinstance(discord_value, Mapping) or set(discord_value) != {
        'tester_role', 'channels'
    }:
        raise ReadinessManifestError('discord has an invalid shape.')
    tester_role = discord_value['tester_role']
    if not isinstance(tester_role, Mapping) or set(tester_role) != {
        'name', 'expected_id', 'required'
    }:
        raise ReadinessManifestError('discord.tester_role has an invalid shape.')
    normalized_tester_role = {
        'name': _bounded_text(tester_role['name'], 'discord.tester_role.name'),
        'expected_id': _positive_int(tester_role['expected_id'], 'discord.tester_role.expected_id'),
        'required': tester_role['required'] if type(tester_role['required']) is bool else None,
    }
    if normalized_tester_role['required'] is None:
        raise ReadinessManifestError('discord.tester_role.required must be boolean.')
    if (
            normalized_tester_role['name'] != BETA_TESTER_ROLE_NAME
            or normalized_tester_role['expected_id'] != BETA_PINNED_TESTER_ROLE_ID
            or not normalized_tester_role['required']):
        raise ReadinessManifestError(
            'discord.tester_role must use the approved required pinned role.'
        )
    channels = discord_value['channels']
    if not isinstance(channels, Mapping) or set(channels) != {
        'public_release', 'staffhelp_mirror'
    }:
        raise ReadinessManifestError('discord.channels must contain the two fixed targets.')
    normalized_channels = {}
    for key, channel in channels.items():
        if not isinstance(channel, Mapping) or set(channel) != {'id', 'name', 'required'}:
            raise ReadinessManifestError(f'discord.channels.{key} has an invalid shape.')
        required = channel['required']
        if type(required) is not bool:
            raise ReadinessManifestError(f'discord.channels.{key}.required must be boolean.')
        normalized_channels[key] = {
            'id': _positive_int(channel['id'], f'discord.channels.{key}.id'),
            'name': _bounded_text(channel['name'], f'discord.channels.{key}.name'),
            'required': required,
        }
    expected_channels = {
        'public_release': {
            'id': BETA_PUBLIC_RELEASE_CHANNEL_ID,
            'name': BETA_PUBLIC_RELEASE_CHANNEL_NAME,
            'required': True,
        },
        'staffhelp_mirror': {
            'id': BETA_STAFFHELP_MIRROR_CHANNEL_ID,
            'name': BETA_STAFFHELP_MIRROR_CHANNEL_NAME,
            'required': True,
        },
    }
    if normalized_channels != expected_channels:
        raise ReadinessManifestError(
            'discord.channels must retain the two approved fixed targets.'
        )

    capabilities = value['capabilities']
    if not isinstance(capabilities, Mapping) or set(capabilities) != {
        'current', 'proposed', 'optional', 'unresolved'
    }:
        raise ReadinessManifestError('capabilities has an invalid shape.')
    optional = _manifest_list(
        capabilities['optional'], 'capabilities.optional', MAX_UNRESOLVED_DECISIONS,
        item_validator=_validate_optional_decision,
    )
    unresolved_capability_names = {
        item['capability'] for item in optional if item['decision'] == 'unresolved'
    }
    unresolved = _manifest_list(
        capabilities['unresolved'], 'capabilities.unresolved', MAX_UNRESOLVED_DECISIONS,
        item_validator=_validate_text_item,
    )
    normalized_capabilities = {
        'current': _validate_capability_list(capabilities['current'], 'capabilities.current'),
        'proposed': _validate_capability_list(capabilities['proposed'], 'capabilities.proposed'),
        'optional': optional,
        'unresolved': unresolved,
    }
    if unresolved_capability_names and not any(
            name in normalized_capabilities['unresolved']
            for name in unresolved_capability_names):
        normalized_capabilities['unresolved'].extend(
            f'Decide whether capability {name} should be proposed.'
            for name in sorted(unresolved_capability_names)
        )

    database_value = value['database']
    if not isinstance(database_value, Mapping) or set(database_value) != {
        'teams', 'houses', 'role_bindings', 'fixtures'
    }:
        raise ReadinessManifestError('database has an invalid shape.')
    normalized_database: dict[str, Any] = {}
    teams = database_value['teams']
    if not isinstance(teams, Mapping) or set(teams) != {'proposed', 'unresolved'}:
        raise ReadinessManifestError('database.teams has an invalid shape.')
    normalized_database['teams'] = {
        'proposed': _manifest_list(
            teams['proposed'], 'database.teams.proposed', MAX_PROPOSED_TEAMS,
            item_validator=_validate_proposed_team,
        ),
        'unresolved': _manifest_list(
            teams['unresolved'], 'database.teams.unresolved', MAX_UNRESOLVED_DECISIONS,
            item_validator=_validate_text_item,
        ),
    }
    houses = database_value['houses']
    if not isinstance(houses, Mapping) or set(houses) != {'proposed', 'unresolved'}:
        raise ReadinessManifestError('database.houses has an invalid shape.')
    normalized_database['houses'] = {
        'proposed': _manifest_list(
            houses['proposed'], 'database.houses.proposed', MAX_PROPOSED_HOUSES,
            item_validator=_validate_proposed_house,
        ),
        'unresolved': _manifest_list(
            houses['unresolved'], 'database.houses.unresolved', MAX_UNRESOLVED_DECISIONS,
            item_validator=_validate_text_item,
        ),
    }
    bindings = database_value['role_bindings']
    if not isinstance(bindings, Mapping) or set(bindings) != {'proposed', 'unresolved'}:
        raise ReadinessManifestError('database.role_bindings has an invalid shape.')
    normalized_database['role_bindings'] = {
        'proposed': _manifest_list(
            bindings['proposed'], 'database.role_bindings.proposed', MAX_PROPOSED_ROLE_BINDINGS,
            item_validator=_validate_role_binding,
        ),
        'unresolved': _manifest_list(
            bindings['unresolved'], 'database.role_bindings.unresolved', MAX_UNRESOLVED_DECISIONS,
            item_validator=_validate_text_item,
        ),
    }
    fixtures = database_value['fixtures']
    if not isinstance(fixtures, Mapping) or set(fixtures) != {
        'retain', 'cleanup', 'unresolved'
    }:
        raise ReadinessManifestError('database.fixtures has an invalid shape.')
    normalized_database['fixtures'] = {
        'retain': _manifest_list(
            fixtures['retain'], 'database.fixtures.retain', MAX_FIXTURE_PLANS,
            item_validator=_validate_fixture_plan,
        ),
        'cleanup': _manifest_list(
            fixtures['cleanup'], 'database.fixtures.cleanup', MAX_FIXTURE_PLANS,
            item_validator=_validate_fixture_plan,
        ),
        'unresolved': _manifest_list(
            fixtures['unresolved'], 'database.fixtures.unresolved', MAX_UNRESOLVED_DECISIONS,
            item_validator=_validate_text_item,
        ),
    }

    lifecycle = value['lifecycle']
    if not isinstance(lifecycle, Mapping) or set(lifecycle) != {'cleanup', 'rollback'}:
        raise ReadinessManifestError('lifecycle has an invalid shape.')
    normalized_lifecycle = {}
    for key in ('cleanup', 'rollback'):
        item = lifecycle[key]
        if not isinstance(item, Mapping) or set(item) != {'steps', 'unresolved'}:
            raise ReadinessManifestError(f'lifecycle.{key} has an invalid shape.')
        normalized_lifecycle[key] = {
            'steps': _manifest_list(
                item['steps'], f'lifecycle.{key}.steps', MAX_LIFECYCLE_STEPS,
                item_validator=_validate_text_item,
            ),
            'unresolved': _manifest_list(
                item['unresolved'], f'lifecycle.{key}.unresolved', MAX_UNRESOLVED_DECISIONS,
                item_validator=_validate_text_item,
            ),
        }

    smoke = value['smoke']
    if not isinstance(smoke, Mapping) or set(smoke) != {
        'checklist', 'invitation_prerequisites', 'tester_range', 'unresolved'
    }:
        raise ReadinessManifestError('smoke has an invalid shape.')
    tester_range = smoke['tester_range']
    if not isinstance(tester_range, Mapping) or set(tester_range) != {'minimum', 'maximum'}:
        raise ReadinessManifestError('smoke.tester_range has an invalid shape.')
    minimum = _positive_int(tester_range['minimum'], 'smoke.tester_range.minimum')
    maximum = _positive_int(tester_range['maximum'], 'smoke.tester_range.maximum')
    if minimum > maximum or minimum < 5 or maximum > 20:
        raise ReadinessManifestError('smoke.tester_range must be within 5–20 testers.')
    normalized_smoke = {
        'checklist': _manifest_list(
            smoke['checklist'], 'smoke.checklist', MAX_CHECKLIST_ITEMS,
            item_validator=_validate_text_item,
        ),
        'invitation_prerequisites': _manifest_list(
            smoke['invitation_prerequisites'],
            'smoke.invitation_prerequisites',
            MAX_INVITATION_PREREQUISITES,
            item_validator=_validate_text_item,
        ),
        'tester_range': {'minimum': minimum, 'maximum': maximum},
        'unresolved': _manifest_list(
            smoke['unresolved'], 'smoke.unresolved', MAX_UNRESOLVED_DECISIONS,
            item_validator=_validate_text_item,
        ),
    }

    normalized = {
        'schema_version': READINESS_SCHEMA_VERSION,
        'target': normalized_target,
        'discord': {
            'tester_role': normalized_tester_role,
            'channels': normalized_channels,
        },
        'capabilities': normalized_capabilities,
        'database': normalized_database,
        'lifecycle': normalized_lifecycle,
        'smoke': normalized_smoke,
    }
    _assert_primitive_tree(normalized, field='readiness_manifest')
    return normalized


def _snapshot_channel_map(snapshot: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    fixed_channels = snapshot.get('fixed_channels', {})
    if not isinstance(fixed_channels, Mapping):
        return {}
    return {
        key: value for key, value in fixed_channels.items()
        if isinstance(value, Mapping)
    }


def _snapshot_role_names(snapshot: Mapping[str, Any]) -> set[str]:
    roles = snapshot.get('roles', ())
    if not isinstance(roles, (list, tuple)):
        return set()
    return {
        str(item.get('name')) for item in roles
        if isinstance(item, Mapping) and isinstance(item.get('name'), str)
    }


def _fixture_ids(snapshot: Mapping[str, Any], family: str) -> set[int]:
    fixtures = snapshot.get('fixtures', {})
    if not isinstance(fixtures, Mapping):
        return set()
    entry = fixtures.get(family, {})
    if not isinstance(entry, Mapping):
        return set()
    if family == 'beta_games':
        games = entry.get('games', ())
        if not isinstance(games, (list, tuple)):
            return set()
        return {
            int(item['id']) for item in games
            if isinstance(item, Mapping) and isinstance(item.get('id'), int)
        }
    games = entry.get('games', {})
    if isinstance(games, Mapping):
        return {
            int(item) for item in games.get('ids', ()) if isinstance(item, int)
        }
    return set()


def plan_readiness(
        *,
        manifest: Mapping[str, Any],
        discord_inventory: Mapping[str, Any],
        database_inventory: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deterministic diff; this function has no apply path."""

    desired = validate_readiness_manifest(manifest)
    discord = validate_inventory_snapshot(
        discord_inventory, kind='discord_guild_inventory'
    )
    database = validate_inventory_snapshot(
        database_inventory, kind='development_database_inventory'
    )
    errors: list[str] = []
    unresolved: list[str] = []
    changes: list[str] = []

    expected_target = desired['target']
    discord_target = discord.get('target', {})
    if not isinstance(discord_target, Mapping):
        discord_target = {}
    database_target = database.get('target', {})
    if not isinstance(database_target, Mapping):
        database_target = {}
    actual_targets = {
        'environment': discord_target.get('environment'),
        'guild_id': discord_target.get('guild_id'),
        'application_id': discord_target.get('application_id'),
        'database': database_target.get('database'),
        'database_role': database_target.get('database_role'),
    }
    for key, expected_value in expected_target.items():
        if actual_targets.get(key) != expected_value:
            errors.append(
                f'target.{key}: expected {expected_value!r}, '
                f'found {actual_targets.get(key)!r}'
            )
    database_guild_id = database_target.get('guild_id')
    if database_guild_id != expected_target['guild_id']:
        errors.append(
            'target.guild_id: database inventory is for '
            f'{database_guild_id!r}, expected {expected_target["guild_id"]!r}'
        )

    role = discord.get('tester_role', {})
    if not isinstance(role, Mapping):
        role = {}
    if (
            role.get('live_id') != desired['discord']['tester_role']['expected_id']
            or role.get('pinned_id') != desired['discord']['tester_role']['expected_id']
            or role.get('name') != desired['discord']['tester_role']['name']
            or not role.get('verified', False)):
        errors.append('discord.tester_role does not match the desired unique pinned role.')

    channel_diff: dict[str, str] = {}
    actual_channels = _snapshot_channel_map(discord)
    for key, expected in desired['discord']['channels'].items():
        actual = actual_channels.get(key)
        if actual is None:
            channel_diff[key] = 'missing'
            errors.append(f'discord.channels.{key} is missing from the inventory.')
        elif actual.get('id') != expected['id'] or actual.get('name') != expected['name']:
            channel_diff[key] = 'mismatch'
            errors.append(f'discord.channels.{key} does not match its pinned ID/name.')
        else:
            channel_diff[key] = 'match'

    capabilities = discord.get('capabilities', {})
    if not isinstance(capabilities, Mapping):
        capabilities = {}
    raw_capabilities = capabilities.get('current', ())
    if not isinstance(raw_capabilities, (list, tuple)):
        raw_capabilities = ()
    actual_capabilities = {
        str(item) for item in raw_capabilities if isinstance(item, str)
    }
    declared_capabilities = set(desired['capabilities']['current'])
    if actual_capabilities != declared_capabilities:
        errors.append(
            'capabilities.current does not match the supplied Discord inventory.'
        )
    proposed_capabilities = set(desired['capabilities']['proposed'])
    capability_diff = {
        'add': sorted(proposed_capabilities - actual_capabilities),
        'remove': sorted(actual_capabilities - proposed_capabilities),
        'unchanged': sorted(actual_capabilities & proposed_capabilities),
        'root_review': {
            'tools_support': {
                'implemented_roots': list(TOOLS_SUPPORT_IMPLEMENTED_ROOTS),
                'reserved_unloaded_roots': list(TOOLS_SUPPORT_RESERVED_ROOTS),
            },
        },
    }
    changes.extend(
        f'capability add: {name}' for name in capability_diff['add']
    )
    changes.extend(
        f'capability remove: {name}' for name in capability_diff['remove']
    )
    unresolved.extend(desired['capabilities']['unresolved'])
    unresolved.extend(
        item['reason'] for item in desired['capabilities']['optional']
        if item['decision'] == 'unresolved'
    )

    database_teams = database.get('teams', ())
    if not isinstance(database_teams, (list, tuple)):
        database_teams = ()
    database_houses = database.get('houses', ())
    if not isinstance(database_houses, (list, tuple)):
        database_houses = ()
    db_team_names = {
        str(item.get('name')) for item in database_teams
        if isinstance(item, Mapping)
    }
    db_house_names = {
        str(item.get('name')) for item in database_houses
        if isinstance(item, Mapping)
    }
    proposed_teams = desired['database']['teams']['proposed']
    proposed_houses = desired['database']['houses']['proposed']
    missing_teams = sorted(
        item['name'] for item in proposed_teams if item['name'] not in db_team_names
    )
    missing_houses = sorted(
        item['name'] for item in proposed_houses if item['name'] not in db_house_names
    )
    changes.extend(f'proposed development team missing: {name}' for name in missing_teams)
    changes.extend(f'proposed development house missing: {name}' for name in missing_houses)
    role_names = _snapshot_role_names(discord)
    discord_roles = discord.get('roles', ())
    if not isinstance(discord_roles, (list, tuple)):
        discord_roles = ()
    role_ids_by_name = {
        str(item.get('name')): item.get('id')
        for item in discord_roles
        if isinstance(item, Mapping) and isinstance(item.get('name'), str)
    }
    unresolved.extend(desired['database']['teams']['unresolved'])
    unresolved.extend(desired['database']['houses']['unresolved'])
    unresolved.extend(desired['database']['role_bindings']['unresolved'])
    role_binding_diff = []
    for binding in desired['database']['role_bindings']['proposed']:
        status = 'match' if binding['role_name'] in role_names else 'missing'
        if (
                status == 'match'
                and binding['role_id'] is not None
                and role_ids_by_name.get(binding['role_name']) != binding['role_id']):
            status = 'id-mismatch'
        role_binding_diff.append({**binding, 'status': status})
        if status == 'missing':
            changes.append(
                f"{binding['kind']} role missing: {binding['role_name']}"
            )

    retention_diff = []
    for item in desired['database']['fixtures']['retain']:
        actual_ids = _fixture_ids(database, item['family'])
        expected_ids = set(item['ids'])
        missing = sorted(expected_ids - actual_ids)
        unexpected = sorted(actual_ids - expected_ids)
        actual_player_count = None
        if item['family'] == 'leaderboard_showcase':
            fixtures = database.get('fixtures', {})
            fixture_value = (
                fixtures.get(item['family'], {})
                if isinstance(fixtures, Mapping) else {}
            )
            if isinstance(fixture_value, Mapping):
                players_value = fixture_value.get('players', {})
                if isinstance(players_value, Mapping):
                    actual_player_count = players_value.get('count')
        player_count_mismatch = (
            item['player_count'] is not None
            and actual_player_count != item['player_count']
        )
        retention_diff.append({
            'family': item['family'],
            'expected_ids': sorted(expected_ids),
            'actual_ids': sorted(actual_ids),
            'missing': missing,
            'unexpected': unexpected,
            'expected_player_count': item['player_count'],
            'actual_player_count': actual_player_count,
            'player_count_mismatch': player_count_mismatch,
        })
        if missing or unexpected or player_count_mismatch:
            changes.append(f"fixture retention differs: {item['family']}")
    cleanup_plan = desired['database']['fixtures']['cleanup']
    if cleanup_plan:
        changes.extend(
            f"planned cleanup (not applied): {item['family']}"
            for item in cleanup_plan
        )
    unresolved.extend(desired['database']['fixtures']['unresolved'])
    unresolved.extend(desired['lifecycle']['cleanup']['unresolved'])
    unresolved.extend(desired['lifecycle']['rollback']['unresolved'])
    unresolved.extend(desired['smoke']['unresolved'])

    normalized_unresolved = sorted(set(unresolved))
    normalized_changes = sorted(set(changes))
    return {
        'schema_version': READINESS_SCHEMA_VERSION,
        'kind': 'beta_readiness_plan',
        'valid': not errors,
        'ready_for_review': not errors,
        'ready_for_live_apply': False,
        'ready_for_invitation': False,
        'errors': sorted(set(errors)),
        'unresolved': normalized_unresolved,
        'changes': normalized_changes,
        'diff': {
            'tester_role': 'match' if not any('tester_role' in error for error in errors) else 'mismatch',
            'channels': channel_diff,
            'capabilities': capability_diff,
            'teams': {
                'proposed': proposed_teams,
                'missing': missing_teams,
            },
            'houses': {
                'proposed': proposed_houses,
                'missing': missing_houses,
            },
            'role_bindings': role_binding_diff,
            'fixtures': {
                'retention': retention_diff,
                'cleanup_not_applied': cleanup_plan,
            },
            'cleanup_steps_not_applied': desired['lifecycle']['cleanup']['steps'],
            'rollback_steps_not_applied': desired['lifecycle']['rollback']['steps'],
            'smoke_checklist': desired['smoke']['checklist'],
            'invitation_prerequisites': desired['smoke']['invitation_prerequisites'],
        },
        'boundaries': {
            'discord_mutation_applied': False,
            'database_mutation_applied': False,
            'command_sync_applied': False,
            'tester_invitations_sent': False,
        },
    }


def safe_read_path(
        root: Path,
        value: str | Path,
        *,
        label: str,
        expected_suffix: str = '.json') -> Path:
    """Resolve one repository-backed direct path without traversal/symlinks."""

    raw = str(value)
    candidate = Path(raw)
    if (
            not raw
            or candidate.is_absolute()
            or any(part in {'.', '..'} for part in candidate.parts)
            or '\x00' in raw
            or candidate.suffix != expected_suffix):
        raise ReadinessPathError(
            f'{label} must be one relative {expected_suffix} file inside the repository.'
        )
    root = Path(root).resolve()
    path = (root / candidate).resolve(strict=False)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ReadinessPathError(f'{label} must remain inside the repository.') from exc
    current = root
    for part in candidate.parts:
        current = current / part
        info = current.lstat() if current.exists() else None
        if info is not None and stat.S_ISLNK(info.st_mode):
            raise ReadinessPathError(f'{label} may not traverse a symlink.')
    if not path.is_file():
        raise ReadinessPathError(f'{label} does not exist: {candidate}')
    return path


def load_json_path(
        root: Path,
        value: str | Path,
        *,
        label: str,
        max_bytes: int) -> dict[str, Any]:
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ReadinessPathError('The JSON byte bound must be a positive integer.')
    path = safe_read_path(root, value, label=label)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ReadinessPathError(f'Could not read {label}.') from exc
    if len(payload) > max_bytes:
        raise ReadinessPathError(f'{label} exceeds its size bound.')
    try:
        value = json.loads(payload.decode('utf-8'))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReadinessPathError(f'{label} is not valid UTF-8 JSON.') from exc
    if not isinstance(value, dict):
        raise ReadinessPathError(f'{label} must contain a JSON object.')
    return value


__all__ = [
    'BETA_APPLICATION_ID',
    'BETA_DATABASE_NAME',
    'BETA_DATABASE_ROLE',
    'BETA_GUILD_ID',
    'BETA_PINNED_TESTER_ROLE_ID',
    'BETA_PUBLIC_RELEASE_CHANNEL_ID',
    'BETA_PUBLIC_RELEASE_CHANNEL_NAME',
    'BETA_STAFFHELP_MIRROR_CHANNEL_ID',
    'BETA_STAFFHELP_MIRROR_CHANNEL_NAME',
    'BETA_TESTER_ROLE_NAME',
    'DATABASE_INVENTORY_SCHEMA_VERSION',
    'DISCORD_INVENTORY_SCHEMA_VERSION',
    'MAX_SNAPSHOT_BYTES',
    'ReadinessError',
    'ReadinessInventoryError',
    'ReadinessManifestError',
    'ReadinessPathError',
    'build_discord_inventory',
    'load_json_path',
    'plan_readiness',
    'read_development_database_inventory',
    'safe_read_path',
    'validate_database_profile',
    'validate_inventory_snapshot',
    'validate_live_database_identity',
    'validate_readiness_manifest',
]
