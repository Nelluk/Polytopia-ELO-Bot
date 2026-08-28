"""Persistence and legacy import planning for guild configuration.

This module owns the P10.3 additive PostgreSQL storage contract plus its exact
initial import.  It is not a runtime settings service.  P10.4 reuses its pure
contract and read-only schema inventory only during one startup shadow check;
ordinary guild-setting reads never import or call it.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from modules.application_command_policy import policy_from_server_settings
from modules.guild_configuration_schema import (
    GuildConfigurationDocument,
    GuildConfigurationError,
    document_digest,
    document_to_mapping,
    materialize_legacy_document,
    validate_document,
)


STORAGE_SCHEMA_VERSION = 1
SNAPSHOT_SCHEMA_VERSION = 1
IMPORT_SCHEMA_VERSION = 1
DEVELOPMENT_ENVIRONMENT = 'development'
DEVELOPMENT_DATABASE = 'polytopia_dev'
DEVELOPMENT_ROLE = 'polybot_dev'
DEVELOPMENT_BETA_APPLICATION_ID = 479029527553638401
DEVELOPMENT_BETA_GUILD_ID = 478571892832206869
DEVELOPMENT_STAFF_HELP_CHANNEL_ID = 480078679930830849
PRODUCTION_ENVIRONMENT = 'production'
PRODUCTION_DATABASE = 'polytopia2'
PRODUCTION_ROLE = 'polyelo'
PRODUCTION_APPLICATION_ID = 484067640302764042
IMPORT_ACTOR = 'p10.3-development-static-import'
PRODUCTION_IMPORT_ACTOR = 'production-static-import'
IMPORT_SOURCE_KIND = 'legacy_static_import'
IMPORT_EVENT_TYPE = 'initial_import'
# P11.5C reads this existing immutable audit evidence; it deliberately does
# not add a schema column or infer trust from the document's permissions.
FIRST_GUILD_BOOTSTRAP_EVENT_TYPE = 'first_guild_bootstrap'
FIRST_GUILD_BOOTSTRAP_ACTOR = 'container:p11.5b-first-guild-bootstrap'
FIRST_GUILD_BOOTSTRAP_TEMPLATE = 'operator-only'
FIRST_GUILD_BOOTSTRAP_MAX_RELEVANT_AUDITS = 4
ADVISORY_LOCK_KEY = 0x50313033
MAX_SNAPSHOT_ROLES = 250
MAX_SNAPSHOT_CHANNELS = 500

REGISTRY_TABLE = 'guild_configuration_registry'
REVISION_TABLE = 'guild_configuration_revision'
AUDIT_TABLE = 'guild_configuration_audit'
STORAGE_TABLES = (AUDIT_TABLE, REGISTRY_TABLE, REVISION_TABLE)


class GuildConfigurationStorageError(RuntimeError):
    """The schema, target, snapshot, or import state is unsafe."""


@dataclass(frozen=True)
class StorageTarget:
    environment: str
    database_name: str
    database_user: str
    expected_application_id: int
    background_tasks_enabled: bool
    api_enabled: bool
    bullet_enabled: bool


@dataclass(frozen=True)
class GuildImport:
    guild_id: int
    document: GuildConfigurationDocument
    document_digest: str
    source_digest: str


@dataclass(frozen=True)
class ImportBundle:
    schema_version: int
    storage_schema_version: int
    imports: tuple[GuildImport, ...]
    bundle_digest: str

    @property
    def confirmation(self) -> str:
        return f'P10.3 APPLY {self.bundle_digest}'


def confirmation_for_target(
    bundle: ImportBundle,
    target: StorageTarget,
) -> str:
    validate_target(target)
    if target.environment == PRODUCTION_ENVIRONMENT:
        return f'PRODUCTION GUILD CONFIGURATION APPLY {bundle.bundle_digest}'
    return bundle.confirmation


def import_actor_for_target(target: StorageTarget) -> str:
    validate_target(target)
    return (
        PRODUCTION_IMPORT_ACTOR
        if target.environment == PRODUCTION_ENVIRONMENT
        else IMPORT_ACTOR
    )


@dataclass(frozen=True)
class SchemaInventory:
    tables: tuple[str, ...]
    columns: tuple[tuple[str, str, str, str, str | None], ...]
    constraints: tuple[tuple[str, str, str], ...]

    @property
    def absent(self) -> bool:
        return not self.tables and not self.columns and not self.constraints


@dataclass(frozen=True)
class StorageResult:
    schema_created: bool
    imported_guild_ids: tuple[int, ...]
    unchanged_guild_ids: tuple[int, ...]
    verified_guild_ids: tuple[int, ...]
    bundle_digest: str


_HEX_DIGEST = re.compile(r'^[0-9a-f]{64}$')


EXPECTED_COLUMNS = tuple(sorted(
    (*value, None)
    for value in {
        (REGISTRY_TABLE, 'guild_id', 'int8', 'NO'),
        (REGISTRY_TABLE, 'storage_schema_version', 'int2', 'NO'),
        (REGISTRY_TABLE, 'enrollment_state', 'text', 'NO'),
        (REGISTRY_TABLE, 'active_revision', 'int8', 'YES'),
        (REGISTRY_TABLE, 'generation', 'int8', 'NO'),
        (REGISTRY_TABLE, 'created_at', 'timestamptz', 'NO'),
        (REGISTRY_TABLE, 'updated_at', 'timestamptz', 'NO'),
        (REVISION_TABLE, 'guild_id', 'int8', 'NO'),
        (REVISION_TABLE, 'revision_number', 'int8', 'NO'),
        (REVISION_TABLE, 'schema_version', 'int4', 'NO'),
        (REVISION_TABLE, 'document', 'jsonb', 'NO'),
        (REVISION_TABLE, 'document_digest', 'text', 'NO'),
        (REVISION_TABLE, 'source_digest', 'text', 'NO'),
        (REVISION_TABLE, 'parent_revision', 'int8', 'YES'),
        (REVISION_TABLE, 'source_kind', 'text', 'NO'),
        (REVISION_TABLE, 'actor', 'text', 'NO'),
        (REVISION_TABLE, 'created_at', 'timestamptz', 'NO'),
        (AUDIT_TABLE, 'guild_id', 'int8', 'NO'),
        (AUDIT_TABLE, 'event_number', 'int8', 'NO'),
        (AUDIT_TABLE, 'event_type', 'text', 'NO'),
        (AUDIT_TABLE, 'revision_number', 'int8', 'YES'),
        (AUDIT_TABLE, 'generation', 'int8', 'NO'),
        (AUDIT_TABLE, 'document_digest', 'text', 'YES'),
        (AUDIT_TABLE, 'actor', 'text', 'NO'),
        (AUDIT_TABLE, 'details', 'jsonb', 'NO'),
        (AUDIT_TABLE, 'created_at', 'timestamptz', 'NO'),
    }
))

EXPECTED_CONSTRAINTS = tuple(sorted({
    (REGISTRY_TABLE, 'guild_config_registry_pk', 'p'),
    (REGISTRY_TABLE, 'guild_config_registry_guild_ck', 'c'),
    (REGISTRY_TABLE, 'guild_config_registry_schema_ck', 'c'),
    (REGISTRY_TABLE, 'guild_config_registry_state_ck', 'c'),
    (REGISTRY_TABLE, 'guild_config_registry_generation_ck', 'c'),
    (REGISTRY_TABLE, 'guild_config_registry_active_fk', 'f'),
    (REVISION_TABLE, 'guild_config_revision_pk', 'p'),
    (REVISION_TABLE, 'guild_config_revision_guild_fk', 'f'),
    (REVISION_TABLE, 'guild_config_revision_number_ck', 'c'),
    (REVISION_TABLE, 'guild_config_revision_schema_ck', 'c'),
    (REVISION_TABLE, 'guild_config_revision_document_ck', 'c'),
    (REVISION_TABLE, 'guild_config_revision_digest_ck', 'c'),
    (REVISION_TABLE, 'guild_config_revision_source_digest_ck', 'c'),
    (REVISION_TABLE, 'guild_config_revision_parent_fk', 'f'),
    (REVISION_TABLE, 'guild_config_revision_source_ck', 'c'),
    (REVISION_TABLE, 'guild_config_revision_actor_ck', 'c'),
    (AUDIT_TABLE, 'guild_config_audit_pk', 'p'),
    (AUDIT_TABLE, 'guild_config_audit_guild_fk', 'f'),
    (AUDIT_TABLE, 'guild_config_audit_revision_fk', 'f'),
    (AUDIT_TABLE, 'guild_config_audit_event_ck', 'c'),
    (AUDIT_TABLE, 'guild_config_audit_generation_ck', 'c'),
    (AUDIT_TABLE, 'guild_config_audit_digest_ck', 'c'),
    (AUDIT_TABLE, 'guild_config_audit_actor_ck', 'c'),
    (AUDIT_TABLE, 'guild_config_audit_details_ck', 'c'),
}))


CREATE_SCHEMA_STATEMENTS = (
    f'''CREATE TABLE "{REGISTRY_TABLE}" (
        guild_id BIGINT NOT NULL,
        storage_schema_version SMALLINT NOT NULL,
        enrollment_state TEXT NOT NULL,
        active_revision BIGINT NULL,
        generation BIGINT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        CONSTRAINT guild_config_registry_pk PRIMARY KEY (guild_id),
        CONSTRAINT guild_config_registry_guild_ck CHECK (guild_id > 0),
        CONSTRAINT guild_config_registry_schema_ck
            CHECK (storage_schema_version = 1),
        CONSTRAINT guild_config_registry_state_ck CHECK (
            enrollment_state IN ('pending', 'active', 'suspended', 'retired')
        ),
        CONSTRAINT guild_config_registry_generation_ck CHECK (generation >= 0)
    )''',
    f'''CREATE TABLE "{REVISION_TABLE}" (
        guild_id BIGINT NOT NULL,
        revision_number BIGINT NOT NULL,
        schema_version INTEGER NOT NULL,
        document JSONB NOT NULL,
        document_digest TEXT NOT NULL,
        source_digest TEXT NOT NULL,
        parent_revision BIGINT NULL,
        source_kind TEXT NOT NULL,
        actor TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        CONSTRAINT guild_config_revision_pk
            PRIMARY KEY (guild_id, revision_number),
        CONSTRAINT guild_config_revision_guild_fk FOREIGN KEY (guild_id)
            REFERENCES "{REGISTRY_TABLE}" (guild_id)
            ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
        CONSTRAINT guild_config_revision_number_ck CHECK (revision_number > 0),
        CONSTRAINT guild_config_revision_schema_ck CHECK (schema_version > 0),
        CONSTRAINT guild_config_revision_document_ck
            CHECK (jsonb_typeof(document) = 'object'),
        CONSTRAINT guild_config_revision_digest_ck
            CHECK (document_digest ~ '^[0-9a-f]{{64}}$'),
        CONSTRAINT guild_config_revision_source_digest_ck
            CHECK (source_digest ~ '^[0-9a-f]{{64}}$'),
        CONSTRAINT guild_config_revision_parent_fk
            FOREIGN KEY (guild_id, parent_revision)
            REFERENCES "{REVISION_TABLE}" (guild_id, revision_number)
            ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
        CONSTRAINT guild_config_revision_source_ck CHECK (
            source_kind IN ('legacy_static_import', 'owner_activation', 'rollback')
        ),
        CONSTRAINT guild_config_revision_actor_ck
            CHECK (char_length(actor) BETWEEN 1 AND 200)
    )''',
    f'''CREATE TABLE "{AUDIT_TABLE}" (
        guild_id BIGINT NOT NULL,
        event_number BIGINT NOT NULL,
        event_type TEXT NOT NULL,
        revision_number BIGINT NULL,
        generation BIGINT NOT NULL,
        document_digest TEXT NULL,
        actor TEXT NOT NULL,
        details JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        CONSTRAINT guild_config_audit_pk PRIMARY KEY (guild_id, event_number),
        CONSTRAINT guild_config_audit_guild_fk FOREIGN KEY (guild_id)
            REFERENCES "{REGISTRY_TABLE}" (guild_id)
            ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
        CONSTRAINT guild_config_audit_revision_fk
            FOREIGN KEY (guild_id, revision_number)
            REFERENCES "{REVISION_TABLE}" (guild_id, revision_number)
            ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
        CONSTRAINT guild_config_audit_event_ck
            CHECK (char_length(event_type) BETWEEN 1 AND 80),
        CONSTRAINT guild_config_audit_generation_ck CHECK (generation >= 0),
        CONSTRAINT guild_config_audit_digest_ck CHECK (
            document_digest IS NULL OR document_digest ~ '^[0-9a-f]{{64}}$'
        ),
        CONSTRAINT guild_config_audit_actor_ck
            CHECK (char_length(actor) BETWEEN 1 AND 200),
        CONSTRAINT guild_config_audit_details_ck
            CHECK (jsonb_typeof(details) = 'object')
    )''',
    f'''ALTER TABLE "{REGISTRY_TABLE}"
        ADD CONSTRAINT guild_config_registry_active_fk
        FOREIGN KEY (guild_id, active_revision)
        REFERENCES "{REVISION_TABLE}" (guild_id, revision_number)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED''',
)


def validate_target(target: StorageTarget) -> StorageTarget:
    if not isinstance(target, StorageTarget):
        raise GuildConfigurationStorageError('A frozen storage target is required.')
    development = StorageTarget(
        environment=DEVELOPMENT_ENVIRONMENT,
        database_name=DEVELOPMENT_DATABASE,
        database_user=DEVELOPMENT_ROLE,
        expected_application_id=DEVELOPMENT_BETA_APPLICATION_ID,
        background_tasks_enabled=False,
        api_enabled=False,
        bullet_enabled=False,
    )
    production = StorageTarget(
        environment=PRODUCTION_ENVIRONMENT,
        database_name=PRODUCTION_DATABASE,
        database_user=PRODUCTION_ROLE,
        expected_application_id=PRODUCTION_APPLICATION_ID,
        background_tasks_enabled=True,
        api_enabled=False,
        bullet_enabled=True,
    )
    if target not in {development, production}:
        raise GuildConfigurationStorageError(
            'Guild-configuration storage requires an exact reviewed '
            'development or production runtime target.'
        )
    return target


def validate_live_identity(
    target: StorageTarget,
    *,
    actual_database: Any,
    actual_user: Any,
) -> None:
    validate_target(target)
    if actual_database != target.database_name or actual_user != target.database_user:
        raise GuildConfigurationStorageError(
            'Database identity mismatch: expected '
            f'{target.database_name!r}/{target.database_user!r}, received '
            f'{actual_database!r}/{actual_user!r}.'
        )


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def _strict_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GuildConfigurationStorageError(f'{field} must be a positive integer.')
    return value


def _exact_mapping(
    value: Any,
    fields: frozenset[str],
    field: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GuildConfigurationStorageError(f'{field} must be an object.')
    actual = set(value)
    if actual != fields:
        missing = sorted(fields - actual)
        unknown = sorted(actual - fields, key=str)
        raise GuildConfigurationStorageError(
            f'{field} shape mismatch; missing={missing!r} unknown={unknown!r}.'
        )
    return value


_SNAPSHOT_FIELDS = frozenset({
    'schema_version', 'kind', 'environment', 'application_id', 'guilds',
})
_GUILD_SNAPSHOT_FIELDS = frozenset({
    'guild_id', 'guild_name', 'roles', 'channels',
})
_ROLE_FIELDS = frozenset({'id', 'name', 'managed', 'is_default'})
_CHANNEL_FIELDS = frozenset({'id', 'name', 'type', 'category_id'})


def validate_discord_snapshot(
    value: Mapping[str, Any],
    *,
    target: StorageTarget,
    allowed_guild_ids: Sequence[int],
) -> dict[int, dict[str, Any]]:
    """Validate one complete privacy-bounded live Discord snapshot."""

    validate_target(target)
    root = _exact_mapping(value, _SNAPSHOT_FIELDS, 'Discord snapshot')
    if root['schema_version'] != SNAPSHOT_SCHEMA_VERSION:
        raise GuildConfigurationStorageError('Unsupported Discord snapshot version.')
    if root['kind'] != 'guild_configuration_discord_snapshot':
        raise GuildConfigurationStorageError('Unexpected Discord snapshot kind.')
    if root['environment'] != target.environment:
        raise GuildConfigurationStorageError(
            'Discord snapshot environment does not match the runtime target.'
        )
    if root['application_id'] != target.expected_application_id:
        raise GuildConfigurationStorageError('Discord snapshot application mismatch.')
    guilds = root['guilds']
    if not isinstance(guilds, list):
        raise GuildConfigurationStorageError('Discord snapshot guilds must be a list.')
    allowed = tuple(sorted({
        _strict_int(value, 'allowed guild ID')
        for value in allowed_guild_ids
    }))
    if not allowed:
        raise GuildConfigurationStorageError('At least one allowed guild is required.')
    by_guild: dict[int, dict[str, Any]] = {}
    for raw_guild in guilds:
        guild = _exact_mapping(raw_guild, _GUILD_SNAPSHOT_FIELDS, 'guild snapshot')
        guild_id = _strict_int(guild['guild_id'], 'guild snapshot ID')
        if guild_id in by_guild:
            raise GuildConfigurationStorageError('Discord snapshot duplicates a guild.')
        if not isinstance(guild['guild_name'], str) or not guild['guild_name']:
            raise GuildConfigurationStorageError('Guild snapshot name is invalid.')
        roles = guild['roles']
        channels = guild['channels']
        if not isinstance(roles, list) or len(roles) > MAX_SNAPSHOT_ROLES:
            raise GuildConfigurationStorageError('Guild snapshot roles are invalid or unbounded.')
        if not isinstance(channels, list) or len(channels) > MAX_SNAPSHOT_CHANNELS:
            raise GuildConfigurationStorageError(
                'Guild snapshot channels are invalid or unbounded.'
            )

        role_rows = []
        role_ids = set()
        default_roles = []
        for raw_role in roles:
            role = _exact_mapping(raw_role, _ROLE_FIELDS, 'role snapshot')
            role_id = _strict_int(role['id'], 'role ID')
            if role_id in role_ids:
                raise GuildConfigurationStorageError('Discord snapshot duplicates a role ID.')
            if not isinstance(role['name'], str) or not role['name']:
                raise GuildConfigurationStorageError('Role snapshot name is invalid.')
            if not isinstance(role['managed'], bool) or not isinstance(role['is_default'], bool):
                raise GuildConfigurationStorageError('Role snapshot flags must be booleans.')
            role_ids.add(role_id)
            role_rows.append(dict(role))
            if role['is_default']:
                default_roles.append(role_id)
        if default_roles != [guild_id]:
            raise GuildConfigurationStorageError(
                'The exact @everyone role must be the snapshot guild ID.'
            )

        channel_rows = []
        channel_ids = set()
        for raw_channel in channels:
            channel = _exact_mapping(raw_channel, _CHANNEL_FIELDS, 'channel snapshot')
            channel_id = _strict_int(channel['id'], 'channel ID')
            if channel_id in channel_ids:
                raise GuildConfigurationStorageError('Discord snapshot duplicates a channel ID.')
            if not isinstance(channel['name'], str) or not channel['name']:
                raise GuildConfigurationStorageError('Channel snapshot name is invalid.')
            if not isinstance(channel['type'], str) or not channel['type']:
                raise GuildConfigurationStorageError('Channel snapshot type is invalid.')
            category_id = channel['category_id']
            if category_id is not None:
                _strict_int(category_id, 'channel category ID')
            channel_ids.add(channel_id)
            channel_rows.append(dict(channel))
        by_guild[guild_id] = {
            'guild_name': guild['guild_name'],
            'roles': tuple(role_rows),
            'channels': tuple(channel_rows),
        }
    if tuple(sorted(by_guild)) != allowed:
        raise GuildConfigurationStorageError(
            'Discord snapshot guild set does not match the runtime allowlist.'
        )
    return by_guild


def _effective_legacy_values(
    defaults: Mapping[str, Any],
    overrides: Mapping[str, Any],
) -> dict[str, Any]:
    values = dict(defaults)
    values.update(overrides)
    return values


def _role_resolution(snapshot: Mapping[str, Any]) -> dict[str, list[int]]:
    values: dict[str, list[int]] = {}
    for role in snapshot['roles']:
        if role['is_default']:
            continue
        values.setdefault(role['name'], []).append(role['id'])
    return {name: sorted(ids) for name, ids in values.items()}


def _channel_maps(snapshot: Mapping[str, Any]) -> tuple[dict[int, str], set[int]]:
    channel_types = {row['id']: row['type'] for row in snapshot['channels']}
    category_ids = {
        channel_id for channel_id, kind in channel_types.items()
        if kind == 'category'
    }
    return channel_types, category_ids


def _validate_document_references(
    document: GuildConfigurationDocument,
    snapshot: Mapping[str, Any],
) -> None:
    role_ids = {row['id'] for row in snapshot['roles']}
    managed_role_ids = {
        row['id'] for row in snapshot['roles'] if row['managed']
    }
    permission_ids = (
        document.permissions.helper_role_ids
        + document.permissions.mod_role_ids
        + document.permissions.user_role_ids_level_1
        + document.permissions.user_role_ids_level_2
        + document.permissions.user_role_ids_level_3
        + document.permissions.user_role_ids_level_4
        + (() if document.permissions.inactive_role_id is None else (
            document.permissions.inactive_role_id,
        ))
    )
    missing_roles = sorted(set(permission_ids) - role_ids)
    if missing_roles:
        raise GuildConfigurationStorageError(
            'Configuration references role IDs absent from the exact guild: '
            + ', '.join(str(value) for value in missing_roles)
        )
    unsafe_managed_roles = sorted(set(permission_ids) & managed_role_ids)
    if unsafe_managed_roles:
        raise GuildConfigurationStorageError(
            'Configuration permission roles must not be managed integration roles: '
            + ', '.join(str(value) for value in unsafe_managed_roles)
        )

    channel_types, category_ids = _channel_maps(snapshot)
    channels = document.channels
    ordinary_ids = set(
        (channels.bot_channel_ids or ())
        + (channels.strict_bot_channel_ids or ())
        + channels.private_bot_channel_ids
        + channels.newbie_message_channel_ids
        + channels.match_challenge_channel_ids
        + tuple(
            value for value in (
                channels.ranked_game_channel_id,
                channels.unranked_game_channel_id,
                channels.steam_game_channel_id,
                channels.log_channel_id,
                channels.game_announce_channel_id,
                channels.staff_help_channel_id,
            ) if value is not None
        )
    )
    missing_channels = sorted(ordinary_ids - set(channel_types))
    if missing_channels:
        raise GuildConfigurationStorageError(
            'Configuration references channel IDs absent from the exact guild: '
            + ', '.join(str(value) for value in missing_channels)
        )
    wrong_channels = sorted(value for value in ordinary_ids if value in category_ids)
    if wrong_channels:
        raise GuildConfigurationStorageError(
            'Configuration uses category IDs where channels are required: '
            + ', '.join(str(value) for value in wrong_channels)
        )
    missing_categories = sorted(set(channels.game_category_ids) - category_ids)
    if missing_categories:
        raise GuildConfigurationStorageError(
            'Configuration references missing or non-category IDs: '
            + ', '.join(str(value) for value in missing_categories)
        )


def validate_document_references(
    document: GuildConfigurationDocument,
    snapshot: Mapping[str, Any],
) -> None:
    """Validate one stored document against one exact live guild snapshot."""

    if not isinstance(document, GuildConfigurationDocument):
        raise GuildConfigurationStorageError(
            'A validated guild configuration document is required.'
        )
    if not isinstance(snapshot, Mapping):
        raise GuildConfigurationStorageError(
            'A validated guild Discord snapshot is required.'
        )
    _validate_document_references(document, snapshot)


def build_import_bundle(
    *,
    target: StorageTarget,
    server_settings: Any,
    allowed_guild_ids: Sequence[int],
    discord_snapshot: Mapping[str, Any],
    guild_type_overrides: Mapping[int, str] | None = None,
) -> ImportBundle:
    """Materialize every explicit static guild without database I/O."""

    validate_target(target)
    allowed = tuple(sorted({
        _strict_int(value, 'allowed guild ID')
        for value in allowed_guild_ids
    }))
    snapshots = validate_discord_snapshot(
        discord_snapshot,
        target=target,
        allowed_guild_ids=allowed,
    )
    server_list = getattr(server_settings, 'server_list', None)
    if not isinstance(server_list, Mapping) or 'default' not in server_list:
        raise GuildConfigurationStorageError('Server settings have no default mapping.')
    explicit_ids = tuple(sorted(
        value for value in server_list if isinstance(value, int)
    ))
    if explicit_ids != allowed or set(server_list) != {'default', *allowed}:
        raise GuildConfigurationStorageError(
            'Server-settings guild inventory does not match the allowlist exactly.'
        )
    normalized_types: dict[int, str] | None = None
    if guild_type_overrides is not None:
        from modules import guild_types

        if not isinstance(guild_type_overrides, Mapping):
            raise GuildConfigurationStorageError(
                'Guild-type overrides must be an exact guild-ID mapping.'
            )
        if tuple(sorted(guild_type_overrides)) != allowed:
            raise GuildConfigurationStorageError(
                'Guild-type overrides do not match the allowlist exactly.'
            )
        try:
            normalized_types = {
                guild_id: guild_types.normalize_guild_type(
                    guild_type_overrides[guild_id]
                )
                for guild_id in allowed
            }
        except guild_types.GuildTypeError as exc:
            raise GuildConfigurationStorageError(str(exc)) from exc
    policy = policy_from_server_settings(server_settings, allowed)
    defaults = server_list['default']
    imports = []
    for guild_id in allowed:
        overrides = server_list[guild_id]
        if not isinstance(defaults, Mapping) or not isinstance(overrides, Mapping):
            raise GuildConfigurationStorageError('Legacy guild settings must be mappings.')
        capabilities = policy.capabilities_for_guild(guild_id)
        effective = _effective_legacy_values(defaults, overrides)
        route_override = None
        if (
            guild_id == DEVELOPMENT_BETA_GUILD_ID
            and 'tools_support' in capabilities
            and effective.get('staff_help_channel') is None
        ):
            route_override = DEVELOPMENT_STAFF_HELP_CHANNEL_ID
            overrides = dict(overrides)
            overrides['staff_help_channel'] = route_override
        try:
            document = materialize_legacy_document(
                guild_id=guild_id,
                defaults=defaults,
                overrides=overrides,
                role_ids_by_name=_role_resolution(snapshots[guild_id]),
                command_capabilities=capabilities,
            )
        except GuildConfigurationError as exc:
            raise GuildConfigurationStorageError(str(exc)) from exc
        if normalized_types is not None:
            try:
                document = guild_types.apply_guild_type(
                    document,
                    normalized_types[guild_id],
                )
            except guild_types.GuildTypeError as exc:
                raise GuildConfigurationStorageError(str(exc)) from exc
        _validate_document_references(document, snapshots[guild_id])
        source_payload = {
            'guild_id': guild_id,
            'defaults': defaults,
            'overrides': server_list[guild_id],
            'command_capabilities': list(capabilities),
            'resolved_role_ids_by_name': {
                name: _role_resolution(snapshots[guild_id])[name]
                for name in sorted({
                    *effective['helper_roles'],
                    *effective['mod_roles'],
                    *effective['user_roles_level_1'],
                    *effective['user_roles_level_2'],
                    *effective['user_roles_level_3'],
                    *effective['user_roles_level_4'],
                    *(() if effective['inactive_role'] is None else (
                        effective['inactive_role'],
                    )),
                } - {'@everyone'})
            },
            'effective_staff_help_channel_override': route_override,
        }
        if normalized_types is not None:
            source_payload['migration_guild_type'] = normalized_types[guild_id]
        imports.append(GuildImport(
            guild_id=guild_id,
            document=document,
            document_digest=document_digest(document),
            source_digest=_canonical_digest(source_payload),
        ))
    bundle_payload = {
        'schema_version': IMPORT_SCHEMA_VERSION,
        'storage_schema_version': STORAGE_SCHEMA_VERSION,
        'imports': [
            {
                'guild_id': value.guild_id,
                'document_digest': value.document_digest,
                'source_digest': value.source_digest,
            }
            for value in imports
        ],
    }
    return ImportBundle(
        schema_version=IMPORT_SCHEMA_VERSION,
        storage_schema_version=STORAGE_SCHEMA_VERSION,
        imports=tuple(imports),
        bundle_digest=_canonical_digest(bundle_payload),
    )


def bundle_to_mapping(
    bundle: ImportBundle,
    *,
    target: StorageTarget | None = None,
) -> dict[str, Any]:
    if not isinstance(bundle, ImportBundle):
        raise GuildConfigurationStorageError('A validated import bundle is required.')
    statements = list(CREATE_SCHEMA_STATEMENTS)
    confirmation = bundle.confirmation
    if target is not None:
        validate_target(target)
        confirmation = confirmation_for_target(bundle, target)
        if target.environment == PRODUCTION_ENVIRONMENT:
            from modules import guild_configuration_delegation_storage as delegation
            from modules import guild_configuration_draft_storage as drafts

            statements.extend(drafts.CREATE_DRAFT_SCHEMA_STATEMENTS)
            statements.extend(delegation.CREATE_DELEGATION_SCHEMA_STATEMENTS)
    return {
        'schema_version': bundle.schema_version,
        'storage_schema_version': bundle.storage_schema_version,
        'bundle_digest': bundle.bundle_digest,
        'confirmation': confirmation,
        'guilds': [
            {
                'guild_id': value.guild_id,
                'document_digest': value.document_digest,
                'source_digest': value.source_digest,
                'document': document_to_mapping(value.document),
            }
            for value in bundle.imports
        ],
        'planned_schema_statements': statements,
    }


def bundle_from_mapping(
    value: Mapping[str, Any],
    *,
    target: StorageTarget,
) -> ImportBundle:
    """Validate and reconstruct one exact emitted import plan."""

    validate_target(target)
    if not isinstance(value, Mapping):
        raise GuildConfigurationStorageError('Import plan must be an object.')
    root = dict(value)
    # The production planner adds a human-readable derivative summary. It is
    # not part of the digest-bound import contract.
    root.pop('production_migration_summary', None)
    expected_fields = {
        'schema_version', 'storage_schema_version', 'bundle_digest',
        'confirmation', 'guilds', 'planned_schema_statements',
    }
    if set(root) != expected_fields:
        raise GuildConfigurationStorageError(
            'Import plan shape does not match the storage contract.'
        )
    raw_guilds = root['guilds']
    if not isinstance(raw_guilds, list) or not raw_guilds:
        raise GuildConfigurationStorageError(
            'Import plan must contain at least one guild.'
        )
    imports: list[GuildImport] = []
    for raw in raw_guilds:
        if not isinstance(raw, Mapping) or set(raw) != {
                'guild_id', 'document_digest', 'source_digest', 'document'}:
            raise GuildConfigurationStorageError(
                'Import plan guild shape is invalid.'
            )
        guild_id = _strict_int(raw['guild_id'], 'import plan guild ID')
        if (
                not isinstance(raw['document_digest'], str)
                or not _HEX_DIGEST.fullmatch(raw['document_digest'])
                or not isinstance(raw['source_digest'], str)
                or not _HEX_DIGEST.fullmatch(raw['source_digest'])
        ):
            raise GuildConfigurationStorageError(
                f'Guild {guild_id} import plan digest is invalid.'
            )
        try:
            document = validate_document(raw['document'])
        except GuildConfigurationError as exc:
            raise GuildConfigurationStorageError(
                f'Guild {guild_id} import plan document is invalid.'
            ) from exc
        if (
                document.guild_id != guild_id
                or document_digest(document) != raw['document_digest']
        ):
            raise GuildConfigurationStorageError(
                f'Guild {guild_id} import plan document digest differs.'
            )
        imports.append(GuildImport(
            guild_id=guild_id,
            document=document,
            document_digest=raw['document_digest'],
            source_digest=raw['source_digest'],
        ))
    guild_ids = tuple(item.guild_id for item in imports)
    if guild_ids != tuple(sorted(set(guild_ids))):
        raise GuildConfigurationStorageError(
            'Import plan guild inventory is not unique and sorted.'
        )
    payload = {
        'schema_version': IMPORT_SCHEMA_VERSION,
        'storage_schema_version': STORAGE_SCHEMA_VERSION,
        'imports': [
            {
                'guild_id': item.guild_id,
                'document_digest': item.document_digest,
                'source_digest': item.source_digest,
            }
            for item in imports
        ],
    }
    bundle = ImportBundle(
        schema_version=IMPORT_SCHEMA_VERSION,
        storage_schema_version=STORAGE_SCHEMA_VERSION,
        imports=tuple(imports),
        bundle_digest=_canonical_digest(payload),
    )
    if root != bundle_to_mapping(bundle, target=target):
        raise GuildConfigurationStorageError(
            'Import plan differs from its exact digest-bound bundle.'
        )
    return bundle


def _schema_inventory(cursor: Any) -> SchemaInventory:
    cursor.execute(
        'SELECT table_name FROM information_schema.tables '
        'WHERE table_schema = current_schema() AND table_name = ANY(%s) '
        'ORDER BY table_name',
        (list(STORAGE_TABLES),),
    )
    tables = tuple(row[0] for row in cursor.fetchall())
    if not tables:
        return SchemaInventory((), (), ())
    cursor.execute(
        'SELECT table_name, column_name, udt_name, is_nullable, column_default '
        'FROM information_schema.columns '
        'WHERE table_schema = current_schema() AND table_name = ANY(%s) '
        'ORDER BY table_name, ordinal_position',
        (list(STORAGE_TABLES),),
    )
    columns = tuple(sorted(tuple(row) for row in cursor.fetchall()))
    cursor.execute(
        'SELECT source.relname, constraint_record.conname, '
        'constraint_record.contype '
        'FROM pg_constraint AS constraint_record '
        'JOIN pg_class AS source ON source.oid = constraint_record.conrelid '
        'JOIN pg_namespace AS namespace ON namespace.oid = source.relnamespace '
        'WHERE namespace.nspname = current_schema() '
        'AND source.relname = ANY(%s) '
        "AND constraint_record.contype IN ('p', 'f', 'c', 'u') "
        'ORDER BY source.relname, constraint_record.conname',
        (list(STORAGE_TABLES),),
    )
    constraints = tuple(sorted(tuple(row) for row in cursor.fetchall()))
    return SchemaInventory(tables, columns, constraints)


def inspect_schema_inventory(cursor: Any) -> SchemaInventory:
    """Return the exact storage inventory for read-only sibling services."""

    return _schema_inventory(cursor)


def validate_schema_inventory(inventory: SchemaInventory) -> bool:
    """Return False when absent; reject every partial or drifted schema."""

    if not isinstance(inventory, SchemaInventory):
        raise GuildConfigurationStorageError('A schema inventory is required.')
    if inventory.absent:
        return False
    if inventory.tables != tuple(sorted(STORAGE_TABLES)):
        raise GuildConfigurationStorageError(
            'Guild configuration storage tables are partial or unexpected.'
        )
    if inventory.columns != EXPECTED_COLUMNS:
        raise GuildConfigurationStorageError(
            'Guild configuration storage columns do not match schema version one.'
        )
    if inventory.constraints != EXPECTED_CONSTRAINTS:
        raise GuildConfigurationStorageError(
            'Guild configuration storage constraints do not match schema version one.'
        )
    return True


def _session_identity(cursor: Any) -> tuple[Any, Any]:
    cursor.execute('SELECT current_database(), current_user')
    return tuple(cursor.fetchone())


def _registry_rows(cursor: Any) -> tuple[tuple[Any, ...], ...]:
    cursor.execute(
        f'SELECT guild_id, storage_schema_version, enrollment_state, '
        f'active_revision, generation FROM "{REGISTRY_TABLE}" ORDER BY guild_id'
    )
    return tuple(tuple(row) for row in cursor.fetchall())


def _revision_row(cursor: Any, guild_id: int, revision: int) -> tuple[Any, ...] | None:
    cursor.execute(
        f'SELECT schema_version, document, document_digest, source_digest, '
        f'parent_revision, source_kind, actor FROM "{REVISION_TABLE}" '
        'WHERE guild_id = %s AND revision_number = %s',
        (guild_id, revision),
    )
    row = cursor.fetchone()
    return None if row is None else tuple(row)


def _audit_rows(cursor: Any, guild_id: int) -> tuple[tuple[Any, ...], ...]:
    cursor.execute(
        f'SELECT event_number, event_type, revision_number, generation, '
        f'document_digest, actor, details FROM "{AUDIT_TABLE}" '
        'WHERE guild_id = %s ORDER BY event_number',
        (guild_id,),
    )
    return tuple(tuple(row) for row in cursor.fetchall())


def _verify_cursor(
    cursor: Any,
    bundle: ImportBundle,
    *,
    expected_actor: str = IMPORT_ACTOR,
) -> tuple[int, ...]:
    validate_schema_inventory(_schema_inventory(cursor))
    expected_ids = tuple(value.guild_id for value in bundle.imports)
    registry = _registry_rows(cursor)
    if tuple(row[0] for row in registry) != expected_ids:
        raise GuildConfigurationStorageError(
            'Stored guild registry does not match the exact import bundle.'
        )
    by_id = {value.guild_id: value for value in bundle.imports}
    for row in registry:
        guild_id, storage_version, state, active_revision, generation = row
        if (storage_version, state, active_revision, generation) != (
            STORAGE_SCHEMA_VERSION, 'active', 1, 1,
        ):
            raise GuildConfigurationStorageError(
                f'Guild {guild_id} registry state is not the exact initial import.'
            )
        expected = by_id[guild_id]
        revision = _revision_row(cursor, guild_id, 1)
        if revision is None:
            raise GuildConfigurationStorageError(f'Guild {guild_id} revision 1 is missing.')
        (
            schema_version, document_value, stored_digest, source_digest,
            parent_revision, source_kind, actor,
        ) = revision
        try:
            document = validate_document(document_value)
        except GuildConfigurationError as exc:
            raise GuildConfigurationStorageError(
                f'Guild {guild_id} stored document is invalid: {exc}'
            ) from exc
        if (
            schema_version != expected.document.schema_version
            or stored_digest != expected.document_digest
            or document_digest(document) != expected.document_digest
            or document != expected.document
            or source_digest != expected.source_digest
            or parent_revision is not None
            or source_kind != IMPORT_SOURCE_KIND
            or actor != expected_actor
        ):
            raise GuildConfigurationStorageError(
                f'Guild {guild_id} revision 1 differs from the exact import bundle.'
            )
        audits = _audit_rows(cursor, guild_id)
        if audits != ((
            1,
            IMPORT_EVENT_TYPE,
            1,
            1,
            expected.document_digest,
            expected_actor,
            {'source_digest': expected.source_digest},
        ),):
            raise GuildConfigurationStorageError(
                f'Guild {guild_id} initial import audit is missing or changed.'
            )
    return expected_ids


def verify_storage(
    connection: Any,
    *,
    target: StorageTarget,
    bundle: ImportBundle,
) -> StorageResult:
    validate_target(target)
    with connection.cursor() as cursor:
        actual_database, actual_user = _session_identity(cursor)
        validate_live_identity(
            target,
            actual_database=actual_database,
            actual_user=actual_user,
        )
        if target.environment == PRODUCTION_ENVIRONMENT:
            _validate_production_auxiliary_schema(cursor)
            verified = _verify_cursor(
                cursor,
                bundle,
                expected_actor=import_actor_for_target(target),
            )
        else:
            verified = _verify_cursor(cursor, bundle)
    return StorageResult(False, (), verified, verified, bundle.bundle_digest)


def _insert_import(
    cursor: Any,
    value: GuildImport,
    *,
    actor: str = IMPORT_ACTOR,
) -> None:
    document_json = json.dumps(
        document_to_mapping(value.document),
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    )
    cursor.execute(
        f'INSERT INTO "{REGISTRY_TABLE}" '
        '(guild_id, storage_schema_version, enrollment_state, active_revision, '
        'generation, created_at, updated_at) '
        "VALUES (%s, %s, 'active', NULL, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (value.guild_id, STORAGE_SCHEMA_VERSION),
    )
    cursor.execute(
        f'INSERT INTO "{REVISION_TABLE}" '
        '(guild_id, revision_number, schema_version, document, document_digest, '
        'source_digest, parent_revision, source_kind, actor, created_at) '
        'VALUES (%s, 1, %s, CAST(%s AS JSONB), %s, %s, NULL, %s, %s, '
        'CURRENT_TIMESTAMP)',
        (
            value.guild_id,
            value.document.schema_version,
            document_json,
            value.document_digest,
            value.source_digest,
            IMPORT_SOURCE_KIND,
            actor,
        ),
    )
    cursor.execute(
        f'UPDATE "{REGISTRY_TABLE}" SET active_revision = 1, generation = 1, '
        'updated_at = CURRENT_TIMESTAMP WHERE guild_id = %s',
        (value.guild_id,),
    )
    cursor.execute(
        f'INSERT INTO "{AUDIT_TABLE}" '
        '(guild_id, event_number, event_type, revision_number, generation, '
        'document_digest, actor, details, created_at) '
        'VALUES (%s, 1, %s, 1, 1, %s, %s, CAST(%s AS JSONB), CURRENT_TIMESTAMP)',
        (
            value.guild_id,
            IMPORT_EVENT_TYPE,
            value.document_digest,
            actor,
            json.dumps({'source_digest': value.source_digest}, sort_keys=True),
        ),
    )


def _validate_production_auxiliary_schema(cursor: Any) -> None:
    from modules import guild_configuration_delegation_storage as delegation
    from modules import guild_configuration_draft_storage as drafts

    if not drafts.validate_draft_schema(drafts.inspect_draft_schema(cursor)):
        raise GuildConfigurationStorageError(
            'Production guild-configuration draft storage is absent.'
        )
    if not delegation.validate_delegation_schema(
            delegation.inspect_delegation_schema(cursor)):
        raise GuildConfigurationStorageError(
            'Production guild-configuration delegation storage is absent.'
        )


def _ensure_production_auxiliary_schema(cursor: Any) -> bool:
    from modules import guild_configuration_delegation_storage as delegation
    from modules import guild_configuration_draft_storage as drafts

    created = False
    draft_inventory = drafts.inspect_draft_schema(cursor)
    if not drafts.validate_draft_schema(draft_inventory):
        for statement in drafts.CREATE_DRAFT_SCHEMA_STATEMENTS:
            cursor.execute(statement)
        created = True
    delegation_inventory = delegation.inspect_delegation_schema(cursor)
    if not delegation.validate_delegation_schema(delegation_inventory):
        for statement in delegation.CREATE_DELEGATION_SCHEMA_STATEMENTS:
            cursor.execute(statement)
        created = True
    _validate_production_auxiliary_schema(cursor)
    return created


def apply_storage(
    connection: Any,
    *,
    target: StorageTarget,
    bundle: ImportBundle,
    confirmation: str,
) -> StorageResult:
    """Create/import atomically; exact repeats verify as no-ops."""

    validate_target(target)
    if not isinstance(bundle, ImportBundle) or not _HEX_DIGEST.fullmatch(
        bundle.bundle_digest
    ):
        raise GuildConfigurationStorageError('A validated import bundle is required.')
    expected_confirmation = confirmation_for_target(bundle, target)
    if confirmation != expected_confirmation:
        raise GuildConfigurationStorageError(
            f'Apply requires exact confirmation {expected_confirmation!r}.'
        )
    imported: list[int] = []
    unchanged: list[int] = []
    schema_created = False
    try:
        with connection.cursor() as cursor:
            actual_database, actual_user = _session_identity(cursor)
            validate_live_identity(
                target,
                actual_database=actual_database,
                actual_user=actual_user,
            )
            cursor.execute('SHOW transaction_read_only')
            if str(cursor.fetchone()[0]).casefold() not in {'off', 'false'}:
                raise GuildConfigurationStorageError(
                    'P10.3 apply requires a read-write transaction.'
                )
            cursor.execute('SELECT pg_advisory_xact_lock(%s)', (ADVISORY_LOCK_KEY,))
            inventory = _schema_inventory(cursor)
            if not validate_schema_inventory(inventory):
                for statement in CREATE_SCHEMA_STATEMENTS:
                    cursor.execute(statement)
                schema_created = True
                validate_schema_inventory(_schema_inventory(cursor))
            if target.environment == PRODUCTION_ENVIRONMENT:
                schema_created = (
                    _ensure_production_auxiliary_schema(cursor)
                    or schema_created
                )

            existing_rows = _registry_rows(cursor)
            existing_ids = tuple(row[0] for row in existing_rows)
            expected_ids = tuple(value.guild_id for value in bundle.imports)
            unexpected = sorted(set(existing_ids) - set(expected_ids))
            if unexpected:
                raise GuildConfigurationStorageError(
                    'Registry contains guilds outside the exact import bundle: '
                    + ', '.join(str(value) for value in unexpected)
                )
            for value in bundle.imports:
                if value.guild_id in existing_ids:
                    unchanged.append(value.guild_id)
                else:
                    if target.environment == PRODUCTION_ENVIRONMENT:
                        _insert_import(
                            cursor,
                            value,
                            actor=import_actor_for_target(target),
                        )
                    else:
                        _insert_import(cursor, value)
                    imported.append(value.guild_id)
            if target.environment == PRODUCTION_ENVIRONMENT:
                verified = _verify_cursor(
                    cursor,
                    bundle,
                    expected_actor=import_actor_for_target(target),
                )
            else:
                verified = _verify_cursor(cursor, bundle)
        connection.commit()
        return StorageResult(
            schema_created,
            tuple(imported),
            tuple(unchanged),
            verified,
            bundle.bundle_digest,
        )
    except Exception:
        connection.rollback()
        raise


__all__ = [
    'ADVISORY_LOCK_KEY',
    'AUDIT_TABLE',
    'CREATE_SCHEMA_STATEMENTS',
    'DEVELOPMENT_BETA_APPLICATION_ID',
    'DEVELOPMENT_BETA_GUILD_ID',
    'DEVELOPMENT_DATABASE',
    'DEVELOPMENT_ENVIRONMENT',
    'DEVELOPMENT_ROLE',
    'DEVELOPMENT_STAFF_HELP_CHANNEL_ID',
    'EXPECTED_COLUMNS',
    'EXPECTED_CONSTRAINTS',
    'GuildConfigurationStorageError',
    'GuildImport',
    'IMPORT_ACTOR',
    'ImportBundle',
    'PRODUCTION_APPLICATION_ID',
    'PRODUCTION_DATABASE',
    'PRODUCTION_ENVIRONMENT',
    'PRODUCTION_IMPORT_ACTOR',
    'PRODUCTION_ROLE',
    'REGISTRY_TABLE',
    'REVISION_TABLE',
    'SNAPSHOT_SCHEMA_VERSION',
    'STORAGE_SCHEMA_VERSION',
    'STORAGE_TABLES',
    'SchemaInventory',
    'StorageResult',
    'StorageTarget',
    'apply_storage',
    'build_import_bundle',
    'bundle_from_mapping',
    'bundle_to_mapping',
    'confirmation_for_target',
    'import_actor_for_target',
    'inspect_schema_inventory',
    'validate_discord_snapshot',
    'validate_document_references',
    'validate_live_identity',
    'validate_schema_inventory',
    'validate_target',
    'verify_storage',
]
