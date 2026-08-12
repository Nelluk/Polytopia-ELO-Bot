"""Development-only storage for opt-in guild configuration delegation."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Sequence

from modules import guild_configuration_storage as storage


DELEGATION_TABLE = 'guild_configuration_delegation'
DELEGATION_SCHEMA_VERSION = 1
DELEGATION_ADVISORY_LOCK_KEY = 0x50313039
MAX_MANAGER_ROLES = 20
EVENT_TYPE = 'delegation_policy'


class GuildConfigurationDelegationStorageError(RuntimeError):
    """The delegation schema, policy, or mutation evidence is unsafe."""


@dataclass(frozen=True)
class DelegationSchemaInventory:
    tables: tuple[str, ...]
    columns: tuple[tuple[str, str, str, str, str | None], ...]
    constraints: tuple[tuple[str, str, str], ...]

    @property
    def absent(self) -> bool:
        return not self.tables and not self.columns and not self.constraints


@dataclass(frozen=True)
class DelegationSchemaPlan:
    schema_version: int
    statement_digest: str
    statements: tuple[str, ...] = field(repr=False)

    @property
    def confirmation(self) -> str:
        return f'P10.9 APPLY {self.statement_digest}'


@dataclass(frozen=True)
class DelegationSchemaResult:
    schema_created: bool
    schema_version: int
    statement_digest: str


@dataclass(frozen=True)
class GuildConfigurationDelegation:
    guild_id: int
    policy_version: int
    manager_role_ids: tuple[int, ...]
    allow_activation: bool
    actor: str
    created_at: str
    updated_at: str

    @property
    def enabled(self) -> bool:
        return bool(self.manager_role_ids)


EXPECTED_COLUMNS = (
    (DELEGATION_TABLE, 'actor', 'text', 'NO', None),
    (DELEGATION_TABLE, 'allow_activation', 'bool', 'NO', None),
    (DELEGATION_TABLE, 'created_at', 'timestamptz', 'NO', None),
    (DELEGATION_TABLE, 'guild_id', 'int8', 'NO', None),
    (DELEGATION_TABLE, 'manager_role_ids', 'jsonb', 'NO', None),
    (DELEGATION_TABLE, 'policy_version', 'int8', 'NO', None),
    (DELEGATION_TABLE, 'schema_version', 'int4', 'NO', None),
    (DELEGATION_TABLE, 'updated_at', 'timestamptz', 'NO', None),
)

EXPECTED_CONSTRAINTS = tuple(sorted({
    (DELEGATION_TABLE, 'guild_config_delegation_actor_ck', 'c'),
    (DELEGATION_TABLE, 'guild_config_delegation_guild_ck', 'c'),
    (DELEGATION_TABLE, 'guild_config_delegation_guild_fk', 'f'),
    (DELEGATION_TABLE, 'guild_config_delegation_pk', 'p'),
    (DELEGATION_TABLE, 'guild_config_delegation_roles_ck', 'c'),
    (DELEGATION_TABLE, 'guild_config_delegation_schema_ck', 'c'),
    (DELEGATION_TABLE, 'guild_config_delegation_version_ck', 'c'),
}))

CREATE_DELEGATION_SCHEMA_STATEMENTS = (
    f'''CREATE TABLE "{DELEGATION_TABLE}" (
        guild_id BIGINT NOT NULL,
        policy_version BIGINT NOT NULL,
        schema_version INTEGER NOT NULL,
        manager_role_ids JSONB NOT NULL,
        allow_activation BOOLEAN NOT NULL,
        actor TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        CONSTRAINT guild_config_delegation_pk PRIMARY KEY (guild_id),
        CONSTRAINT guild_config_delegation_guild_ck CHECK (guild_id > 0),
        CONSTRAINT guild_config_delegation_version_ck CHECK (policy_version > 0),
        CONSTRAINT guild_config_delegation_schema_ck
            CHECK (schema_version = {DELEGATION_SCHEMA_VERSION}),
        CONSTRAINT guild_config_delegation_roles_ck
            CHECK (jsonb_typeof(manager_role_ids) = 'array'),
        CONSTRAINT guild_config_delegation_actor_ck
            CHECK (char_length(actor) BETWEEN 1 AND 200),
        CONSTRAINT guild_config_delegation_guild_fk FOREIGN KEY (guild_id)
            REFERENCES "{storage.REGISTRY_TABLE}" (guild_id)
            ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED
    )''',
)


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def _strict_positive(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GuildConfigurationDelegationStorageError(f'{field_name} is invalid.')
    return value


def _actor(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 200:
        raise GuildConfigurationDelegationStorageError('Delegation actor is invalid.')
    return value


def _timestamp(value: Any, field_name: str) -> str:
    formatter = getattr(value, 'isoformat', None)
    if not callable(formatter):
        raise GuildConfigurationDelegationStorageError(f'{field_name} is invalid.')
    rendered = formatter()
    if not isinstance(rendered, str) or not rendered:
        raise GuildConfigurationDelegationStorageError(f'{field_name} is invalid.')
    return rendered


def normalize_manager_role_ids(values: Sequence[int]) -> tuple[int, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise GuildConfigurationDelegationStorageError(
            'Manager role IDs must be a bounded sequence.'
        )
    raw = tuple(values)
    if (
            len(raw) > MAX_MANAGER_ROLES
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
                for value in raw
            )
            or len(raw) != len(set(raw))
    ):
        raise GuildConfigurationDelegationStorageError(
            'Manager role IDs must be unique positive IDs within the policy limit.'
        )
    return tuple(sorted(raw))


def delegation_schema_plan(
    target: storage.StorageTarget,
) -> DelegationSchemaPlan:
    storage.validate_target(target)
    statements = tuple(CREATE_DELEGATION_SCHEMA_STATEMENTS)
    return DelegationSchemaPlan(
        schema_version=DELEGATION_SCHEMA_VERSION,
        statement_digest=_canonical_digest({
            'schema_version': DELEGATION_SCHEMA_VERSION,
            'statements': statements,
        }),
        statements=statements,
    )


def plan_to_mapping(plan: DelegationSchemaPlan) -> dict[str, Any]:
    if not isinstance(plan, DelegationSchemaPlan):
        raise GuildConfigurationDelegationStorageError(
            'A validated delegation-schema plan is required.'
        )
    return {
        'schema_version': plan.schema_version,
        'statement_digest': plan.statement_digest,
        'confirmation': plan.confirmation,
        'planned_schema_statements': list(plan.statements),
        'database_connected': False,
        'active_configuration_changed': False,
    }


def inspect_delegation_schema(cursor: Any) -> DelegationSchemaInventory:
    cursor.execute(
        'SELECT table_name FROM information_schema.tables '
        'WHERE table_schema = current_schema() AND table_name = %s '
        'ORDER BY table_name',
        (DELEGATION_TABLE,),
    )
    tables = tuple(row[0] for row in cursor.fetchall())
    if not tables:
        return DelegationSchemaInventory((), (), ())
    cursor.execute(
        'SELECT table_name, column_name, udt_name, is_nullable, column_default '
        'FROM information_schema.columns '
        'WHERE table_schema = current_schema() AND table_name = %s '
        'ORDER BY table_name, ordinal_position',
        (DELEGATION_TABLE,),
    )
    columns = tuple(sorted(tuple(row) for row in cursor.fetchall()))
    cursor.execute(
        'SELECT source.relname, constraint_record.conname, '
        'constraint_record.contype FROM pg_constraint AS constraint_record '
        'JOIN pg_class AS source ON source.oid = constraint_record.conrelid '
        'JOIN pg_namespace AS namespace ON namespace.oid = source.relnamespace '
        'WHERE namespace.nspname = current_schema() AND source.relname = %s '
        "AND constraint_record.contype IN ('p', 'f', 'c', 'u') "
        'ORDER BY source.relname, constraint_record.conname',
        (DELEGATION_TABLE,),
    )
    constraints = tuple(sorted(tuple(row) for row in cursor.fetchall()))
    return DelegationSchemaInventory(tables, columns, constraints)


def validate_delegation_schema(inventory: DelegationSchemaInventory) -> bool:
    if not isinstance(inventory, DelegationSchemaInventory):
        raise GuildConfigurationDelegationStorageError(
            'A delegation-schema inventory is required.'
        )
    if inventory.absent:
        return False
    if inventory.tables != (DELEGATION_TABLE,):
        raise GuildConfigurationDelegationStorageError(
            'Guild-configuration delegation storage is partial or unexpected.'
        )
    if inventory.columns != tuple(sorted(EXPECTED_COLUMNS)):
        raise GuildConfigurationDelegationStorageError(
            'Guild-configuration delegation columns do not match schema version one.'
        )
    if inventory.constraints != EXPECTED_CONSTRAINTS:
        raise GuildConfigurationDelegationStorageError(
            'Guild-configuration delegation constraints do not match schema version one.'
        )
    return True


def _validate_live_connection(cursor: Any, target: storage.StorageTarget) -> None:
    cursor.execute('SELECT current_database(), current_user')
    actual_database, actual_user = cursor.fetchone()
    storage.validate_live_identity(
        target, actual_database=actual_database, actual_user=actual_user,
    )
    if not storage.validate_schema_inventory(storage.inspect_schema_inventory(cursor)):
        raise GuildConfigurationDelegationStorageError(
            'The base guild-configuration storage is absent.'
        )


def apply_delegation_schema(
    connection: Any,
    *,
    target: storage.StorageTarget,
    plan: DelegationSchemaPlan,
    confirmation: str,
) -> DelegationSchemaResult:
    expected = delegation_schema_plan(target)
    if plan != expected or confirmation != expected.confirmation:
        raise GuildConfigurationDelegationStorageError(
            f'Development apply requires exact confirmation {expected.confirmation!r}.'
        )
    created = False
    try:
        with connection.cursor() as cursor:
            _validate_live_connection(cursor, target)
            cursor.execute('SHOW transaction_read_only')
            if str(cursor.fetchone()[0]).casefold() not in {'off', 'false'}:
                raise GuildConfigurationDelegationStorageError(
                    'P10.9 apply requires a read-write transaction.'
                )
            cursor.execute(
                'SELECT pg_advisory_xact_lock(%s)',
                (DELEGATION_ADVISORY_LOCK_KEY,),
            )
            inventory = inspect_delegation_schema(cursor)
            if not validate_delegation_schema(inventory):
                for statement in expected.statements:
                    cursor.execute(statement)
                created = True
            validate_delegation_schema(inspect_delegation_schema(cursor))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return DelegationSchemaResult(created, expected.schema_version, expected.statement_digest)


def verify_delegation_schema(
    connection: Any, *, target: storage.StorageTarget,
) -> DelegationSchemaResult:
    plan = delegation_schema_plan(target)
    with connection.cursor() as cursor:
        _validate_live_connection(cursor, target)
        if not validate_delegation_schema(inspect_delegation_schema(cursor)):
            raise GuildConfigurationDelegationStorageError(
                'Guild-configuration delegation storage is absent.'
            )
    return DelegationSchemaResult(False, plan.schema_version, plan.statement_digest)


def delegation_from_row(row: Any) -> GuildConfigurationDelegation:
    if row is None or len(row) != 8:
        raise GuildConfigurationDelegationStorageError(
            'Delegation row shape is invalid.'
        )
    guild_id, version, schema_version, roles, activation, actor, created, updated = row
    guild_id = _strict_positive(guild_id, 'Delegation guild ID')
    version = _strict_positive(version, 'Delegation policy version')
    if schema_version != DELEGATION_SCHEMA_VERSION or not isinstance(activation, bool):
        raise GuildConfigurationDelegationStorageError(
            f'Guild {guild_id} delegation metadata is invalid.'
        )
    try:
        role_ids = normalize_manager_role_ids(tuple(roles))
    except (TypeError, GuildConfigurationDelegationStorageError) as exc:
        raise GuildConfigurationDelegationStorageError(
            f'Guild {guild_id} delegation roles are invalid.'
        ) from exc
    return GuildConfigurationDelegation(
        guild_id=guild_id,
        policy_version=version,
        manager_role_ids=role_ids,
        allow_activation=activation,
        actor=_actor(actor),
        created_at=_timestamp(created, 'Delegation creation timestamp'),
        updated_at=_timestamp(updated, 'Delegation update timestamp'),
    )


def select_delegation(
    cursor: Any, guild_id: int, *, for_update: bool = False,
) -> GuildConfigurationDelegation | None:
    guild_id = _strict_positive(guild_id, 'Delegation guild ID')
    suffix = ' FOR UPDATE' if for_update else ''
    cursor.execute(
        f'SELECT guild_id, policy_version, schema_version, manager_role_ids, '
        f'allow_activation, actor, created_at, updated_at '
        f'FROM "{DELEGATION_TABLE}" WHERE guild_id = %s{suffix}',
        (guild_id,),
    )
    row = cursor.fetchone()
    return None if row is None else delegation_from_row(row)


def policy_digest(
    *, guild_id: int, expected_version: int | None,
    manager_role_ids: Sequence[int], allow_activation: bool,
) -> str:
    guild_id = _strict_positive(guild_id, 'Delegation guild ID')
    if expected_version is not None:
        _strict_positive(expected_version, 'Expected delegation policy version')
    roles = normalize_manager_role_ids(manager_role_ids)
    if not isinstance(allow_activation, bool):
        raise GuildConfigurationDelegationStorageError(
            'Delegated activation setting is invalid.'
        )
    if allow_activation and not roles:
        raise GuildConfigurationDelegationStorageError(
            'Delegated activation requires at least one manager role.'
        )
    return _canonical_digest({
        'guild_id': guild_id,
        'expected_version': expected_version,
        'manager_role_ids': roles,
        'allow_activation': allow_activation,
    })


def put_delegation(
    cursor: Any,
    *,
    guild_id: int,
    expected_version: int | None,
    manager_role_ids: Sequence[int],
    allow_activation: bool,
    actor: str,
) -> GuildConfigurationDelegation:
    guild_id = _strict_positive(guild_id, 'Delegation guild ID')
    roles = normalize_manager_role_ids(manager_role_ids)
    if not isinstance(allow_activation, bool):
        raise GuildConfigurationDelegationStorageError(
            'Delegated activation setting is invalid.'
        )
    if allow_activation and not roles:
        raise GuildConfigurationDelegationStorageError(
            'Delegated activation requires at least one manager role.'
        )
    actor = _actor(actor)
    cursor.execute(
        f'SELECT enrollment_state, active_revision, generation '
        f'FROM "{storage.REGISTRY_TABLE}" WHERE guild_id = %s FOR UPDATE',
        (guild_id,),
    )
    registry = cursor.fetchone()
    if registry is None or len(registry) != 3 or registry[0] != 'active':
        raise GuildConfigurationDelegationStorageError(
            'Delegation requires an active enrolled guild.'
        )
    active_revision = _strict_positive(registry[1], 'Active revision')
    generation = _strict_positive(registry[2], 'Active generation')
    current = select_delegation(cursor, guild_id, for_update=True)
    current_version = None if current is None else current.policy_version
    if current_version != expected_version:
        raise GuildConfigurationDelegationStorageError(
            'The delegation policy changed; reopen the workspace.'
        )
    if current is not None and (
            current.manager_role_ids == roles
            and current.allow_activation == allow_activation
    ):
        return current
    payload = json.dumps(list(roles), separators=(',', ':'))
    cursor.execute(
        f'INSERT INTO "{DELEGATION_TABLE}" '
        '(guild_id, policy_version, schema_version, manager_role_ids, '
        'allow_activation, actor, created_at, updated_at) VALUES '
        '(%s, 1, %s, CAST(%s AS JSONB), %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) '
        'ON CONFLICT (guild_id) DO UPDATE SET '
        f'policy_version = "{DELEGATION_TABLE}".policy_version + 1, '
        'manager_role_ids = EXCLUDED.manager_role_ids, '
        'allow_activation = EXCLUDED.allow_activation, actor = EXCLUDED.actor, '
        'updated_at = CURRENT_TIMESTAMP '
        'RETURNING guild_id, policy_version, schema_version, manager_role_ids, '
        'allow_activation, actor, created_at, updated_at',
        (guild_id, DELEGATION_SCHEMA_VERSION, payload, allow_activation, actor),
    )
    result = delegation_from_row(cursor.fetchone())
    cursor.execute(
        f'SELECT COALESCE(MAX(event_number), 0) '
        f'FROM "{storage.AUDIT_TABLE}" WHERE guild_id = %s',
        (guild_id,),
    )
    event_number = _strict_positive(cursor.fetchone()[0] + 1, 'Next audit event')
    details = json.dumps({
        'previous_policy_version': current_version,
        'previous_manager_role_ids': (
            [] if current is None else list(current.manager_role_ids)
        ),
        'previous_allow_activation': (
            False if current is None else current.allow_activation
        ),
        'policy_version': result.policy_version,
        'manager_role_ids': list(result.manager_role_ids),
        'allow_activation': result.allow_activation,
    }, sort_keys=True, separators=(',', ':'))
    cursor.execute(
        f'SELECT document_digest FROM "{storage.REVISION_TABLE}" '
        'WHERE guild_id = %s AND revision_number = %s',
        (guild_id, active_revision),
    )
    digest_row = cursor.fetchone()
    if digest_row is None or len(digest_row) != 1:
        raise GuildConfigurationDelegationStorageError(
            'The active revision disappeared during delegation update.'
        )
    cursor.execute(
        f'INSERT INTO "{storage.AUDIT_TABLE}" '
        '(guild_id, event_number, event_type, revision_number, generation, '
        'document_digest, actor, details, created_at) VALUES '
        '(%s, %s, %s, %s, %s, %s, %s, CAST(%s AS JSONB), CURRENT_TIMESTAMP)',
        (
            guild_id, event_number, EVENT_TYPE, active_revision, generation,
            digest_row[0], actor, details,
        ),
    )
    return result


__all__ = [
    'CREATE_DELEGATION_SCHEMA_STATEMENTS',
    'DELEGATION_ADVISORY_LOCK_KEY',
    'DELEGATION_SCHEMA_VERSION',
    'DELEGATION_TABLE',
    'DelegationSchemaInventory',
    'DelegationSchemaPlan',
    'DelegationSchemaResult',
    'EXPECTED_COLUMNS',
    'EXPECTED_CONSTRAINTS',
    'GuildConfigurationDelegation',
    'GuildConfigurationDelegationStorageError',
    'MAX_MANAGER_ROLES',
    'apply_delegation_schema',
    'delegation_from_row',
    'delegation_schema_plan',
    'inspect_delegation_schema',
    'normalize_manager_role_ids',
    'plan_to_mapping',
    'policy_digest',
    'put_delegation',
    'select_delegation',
    'validate_delegation_schema',
    'verify_delegation_schema',
]
