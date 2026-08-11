"""Development-only schema and persistence contract for guild-config drafts.

The active registry/revision/audit envelope remains owned by
``guild_configuration_storage``.  This module adds one independently
versioned, expiring draft row per already-enrolled guild.  Drafts never affect
runtime authority and create no protected lifecycle audit until a later,
separately reviewed activation unit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any

from modules import guild_configuration_storage as storage
from modules.guild_configuration_schema import (
    GuildConfigurationDocument,
    GuildConfigurationError,
    document_digest,
    document_to_mapping,
    validate_document,
)


DRAFT_TABLE = 'guild_configuration_draft'
DRAFT_SCHEMA_VERSION = 1
DRAFT_TTL_HOURS = 24
DRAFT_ADVISORY_LOCK_KEY = 0x50313036
ACTIVATION_SOURCE_KIND = 'owner_activation'
ACTIVATION_EVENT_TYPE = 'activation'
_HEX_DIGEST = re.compile(r'^[0-9a-f]{64}$')


class GuildConfigurationDraftStorageError(RuntimeError):
    """The draft schema, target, or stored draft is unsafe."""


@dataclass(frozen=True)
class DraftSchemaInventory:
    tables: tuple[str, ...]
    columns: tuple[tuple[str, str, str, str, str | None], ...]
    constraints: tuple[tuple[str, str, str], ...]

    @property
    def absent(self) -> bool:
        return not self.tables and not self.columns and not self.constraints


@dataclass(frozen=True)
class DraftSchemaPlan:
    schema_version: int
    statement_digest: str
    statements: tuple[str, ...] = field(repr=False)

    @property
    def confirmation(self) -> str:
        return f'P10.6B1 APPLY {self.statement_digest}'


@dataclass(frozen=True)
class DraftSchemaResult:
    schema_created: bool
    schema_version: int
    statement_digest: str


@dataclass(frozen=True)
class StoredGuildConfigurationDraft:
    guild_id: int
    draft_version: int
    base_revision: int
    base_generation: int
    document_digest: str
    actor: str
    created_at: str
    updated_at: str
    expires_at: str
    document: GuildConfigurationDocument = field(repr=False)


@dataclass(frozen=True)
class GuildConfigurationActivation:
    guild_id: int
    previous_revision: int
    previous_generation: int
    revision: int
    generation: int
    event_number: int
    document_digest: str
    source_digest: str
    actor: str
    document: GuildConfigurationDocument = field(repr=False)


EXPECTED_COLUMNS = (
    (DRAFT_TABLE, 'actor', 'text', 'NO', None),
    (DRAFT_TABLE, 'base_generation', 'int8', 'NO', None),
    (DRAFT_TABLE, 'base_revision', 'int8', 'NO', None),
    (DRAFT_TABLE, 'created_at', 'timestamptz', 'NO', None),
    (DRAFT_TABLE, 'document', 'jsonb', 'NO', None),
    (DRAFT_TABLE, 'document_digest', 'text', 'NO', None),
    (DRAFT_TABLE, 'draft_version', 'int8', 'NO', None),
    (DRAFT_TABLE, 'expires_at', 'timestamptz', 'NO', None),
    (DRAFT_TABLE, 'guild_id', 'int8', 'NO', None),
    (DRAFT_TABLE, 'schema_version', 'int4', 'NO', None),
    (DRAFT_TABLE, 'updated_at', 'timestamptz', 'NO', None),
)

EXPECTED_CONSTRAINTS = tuple(sorted({
    (DRAFT_TABLE, 'guild_config_draft_actor_ck', 'c'),
    (DRAFT_TABLE, 'guild_config_draft_base_fk', 'f'),
    (DRAFT_TABLE, 'guild_config_draft_digest_ck', 'c'),
    (DRAFT_TABLE, 'guild_config_draft_document_ck', 'c'),
    (DRAFT_TABLE, 'guild_config_draft_expiry_ck', 'c'),
    (DRAFT_TABLE, 'guild_config_draft_generation_ck', 'c'),
    (DRAFT_TABLE, 'guild_config_draft_guild_ck', 'c'),
    (DRAFT_TABLE, 'guild_config_draft_guild_fk', 'f'),
    (DRAFT_TABLE, 'guild_config_draft_pk', 'p'),
    (DRAFT_TABLE, 'guild_config_draft_schema_ck', 'c'),
    (DRAFT_TABLE, 'guild_config_draft_version_ck', 'c'),
}))


CREATE_DRAFT_SCHEMA_STATEMENTS = (
    f'''CREATE TABLE "{DRAFT_TABLE}" (
        guild_id BIGINT NOT NULL,
        draft_version BIGINT NOT NULL,
        base_revision BIGINT NOT NULL,
        base_generation BIGINT NOT NULL,
        schema_version INTEGER NOT NULL,
        document JSONB NOT NULL,
        document_digest TEXT NOT NULL,
        actor TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        CONSTRAINT guild_config_draft_pk PRIMARY KEY (guild_id),
        CONSTRAINT guild_config_draft_guild_ck CHECK (guild_id > 0),
        CONSTRAINT guild_config_draft_version_ck CHECK (draft_version > 0),
        CONSTRAINT guild_config_draft_generation_ck CHECK (base_generation > 0),
        CONSTRAINT guild_config_draft_schema_ck
            CHECK (schema_version = {DRAFT_SCHEMA_VERSION}),
        CONSTRAINT guild_config_draft_document_ck
            CHECK (jsonb_typeof(document) = 'object'),
        CONSTRAINT guild_config_draft_digest_ck
            CHECK (document_digest ~ '^[0-9a-f]{{64}}$'),
        CONSTRAINT guild_config_draft_actor_ck
            CHECK (char_length(actor) BETWEEN 1 AND 200),
        CONSTRAINT guild_config_draft_expiry_ck
            CHECK (expires_at >= created_at),
        CONSTRAINT guild_config_draft_guild_fk FOREIGN KEY (guild_id)
            REFERENCES "{storage.REGISTRY_TABLE}" (guild_id)
            ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
        CONSTRAINT guild_config_draft_base_fk
            FOREIGN KEY (guild_id, base_revision)
            REFERENCES "{storage.REVISION_TABLE}" (guild_id, revision_number)
            ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED
    )''',
)


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def _timestamp(value: Any, field_name: str) -> str:
    formatter = getattr(value, 'isoformat', None)
    if not callable(formatter):
        raise GuildConfigurationDraftStorageError(f'{field_name} is invalid.')
    rendered = formatter()
    if not isinstance(rendered, str) or not rendered:
        raise GuildConfigurationDraftStorageError(f'{field_name} is invalid.')
    return rendered


def _strict_positive(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GuildConfigurationDraftStorageError(f'{field_name} is invalid.')
    return value


def draft_schema_plan(target: storage.StorageTarget) -> DraftSchemaPlan:
    storage.validate_target(target)
    statements = tuple(CREATE_DRAFT_SCHEMA_STATEMENTS)
    return DraftSchemaPlan(
        schema_version=DRAFT_SCHEMA_VERSION,
        statement_digest=_canonical_digest({
            'schema_version': DRAFT_SCHEMA_VERSION,
            'statements': statements,
        }),
        statements=statements,
    )


def plan_to_mapping(plan: DraftSchemaPlan) -> dict[str, Any]:
    if not isinstance(plan, DraftSchemaPlan):
        raise GuildConfigurationDraftStorageError(
            'A validated draft-schema plan is required.'
        )
    return {
        'schema_version': plan.schema_version,
        'statement_digest': plan.statement_digest,
        'confirmation': plan.confirmation,
        'planned_schema_statements': list(plan.statements),
        'database_connected': False,
        'active_configuration_changed': False,
    }


def inspect_draft_schema(cursor: Any) -> DraftSchemaInventory:
    cursor.execute(
        'SELECT table_name FROM information_schema.tables '
        'WHERE table_schema = current_schema() AND table_name = %s '
        'ORDER BY table_name',
        (DRAFT_TABLE,),
    )
    tables = tuple(row[0] for row in cursor.fetchall())
    if not tables:
        return DraftSchemaInventory((), (), ())
    cursor.execute(
        'SELECT table_name, column_name, udt_name, is_nullable, column_default '
        'FROM information_schema.columns '
        'WHERE table_schema = current_schema() AND table_name = %s '
        'ORDER BY table_name, ordinal_position',
        (DRAFT_TABLE,),
    )
    columns = tuple(sorted(tuple(row) for row in cursor.fetchall()))
    cursor.execute(
        'SELECT source.relname, constraint_record.conname, '
        'constraint_record.contype '
        'FROM pg_constraint AS constraint_record '
        'JOIN pg_class AS source ON source.oid = constraint_record.conrelid '
        'JOIN pg_namespace AS namespace ON namespace.oid = source.relnamespace '
        'WHERE namespace.nspname = current_schema() '
        'AND source.relname = %s '
        "AND constraint_record.contype IN ('p', 'f', 'c', 'u') "
        'ORDER BY source.relname, constraint_record.conname',
        (DRAFT_TABLE,),
    )
    constraints = tuple(sorted(tuple(row) for row in cursor.fetchall()))
    return DraftSchemaInventory(tables, columns, constraints)


def validate_draft_schema(inventory: DraftSchemaInventory) -> bool:
    if not isinstance(inventory, DraftSchemaInventory):
        raise GuildConfigurationDraftStorageError(
            'A draft-schema inventory is required.'
        )
    if inventory.absent:
        return False
    if inventory.tables != (DRAFT_TABLE,):
        raise GuildConfigurationDraftStorageError(
            'Guild-configuration draft storage is partial or unexpected.'
        )
    if inventory.columns != tuple(sorted(EXPECTED_COLUMNS)):
        raise GuildConfigurationDraftStorageError(
            'Guild-configuration draft columns do not match schema version one.'
        )
    if inventory.constraints != EXPECTED_CONSTRAINTS:
        raise GuildConfigurationDraftStorageError(
            'Guild-configuration draft constraints do not match schema version one.'
        )
    return True


def _validate_live_connection(cursor: Any, target: storage.StorageTarget) -> None:
    cursor.execute('SELECT current_database(), current_user')
    actual_database, actual_user = cursor.fetchone()
    storage.validate_live_identity(
        target,
        actual_database=actual_database,
        actual_user=actual_user,
    )
    if not storage.validate_schema_inventory(
            storage.inspect_schema_inventory(cursor)):
        raise GuildConfigurationDraftStorageError(
            'The base guild-configuration storage is absent.'
        )


def apply_draft_schema(
    connection: Any,
    *,
    target: storage.StorageTarget,
    plan: DraftSchemaPlan,
    confirmation: str,
) -> DraftSchemaResult:
    expected = draft_schema_plan(target)
    if plan != expected or confirmation != expected.confirmation:
        raise GuildConfigurationDraftStorageError(
            f'Development apply requires exact confirmation '
            f'{expected.confirmation!r}.'
        )
    created = False
    try:
        with connection.cursor() as cursor:
            _validate_live_connection(cursor, target)
            cursor.execute('SHOW transaction_read_only')
            if str(cursor.fetchone()[0]).casefold() not in {'off', 'false'}:
                raise GuildConfigurationDraftStorageError(
                    'P10.6b1 apply requires a read-write transaction.'
                )
            cursor.execute(
                'SELECT pg_advisory_xact_lock(%s)',
                (DRAFT_ADVISORY_LOCK_KEY,),
            )
            inventory = inspect_draft_schema(cursor)
            if not validate_draft_schema(inventory):
                for statement in expected.statements:
                    cursor.execute(statement)
                created = True
            validate_draft_schema(inspect_draft_schema(cursor))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return DraftSchemaResult(
        schema_created=created,
        schema_version=expected.schema_version,
        statement_digest=expected.statement_digest,
    )


def verify_draft_schema(
    connection: Any,
    *,
    target: storage.StorageTarget,
) -> DraftSchemaResult:
    plan = draft_schema_plan(target)
    with connection.cursor() as cursor:
        _validate_live_connection(cursor, target)
        if not validate_draft_schema(inspect_draft_schema(cursor)):
            raise GuildConfigurationDraftStorageError(
                'Guild-configuration draft storage is absent.'
            )
    return DraftSchemaResult(
        schema_created=False,
        schema_version=plan.schema_version,
        statement_digest=plan.statement_digest,
    )


def _validate_document_row(
    *,
    guild_id: int,
    schema_version: Any,
    document_value: Any,
    stored_digest: Any,
) -> GuildConfigurationDocument:
    try:
        document = validate_document(document_value)
    except GuildConfigurationError as exc:
        raise GuildConfigurationDraftStorageError(
            f'Guild {guild_id} draft document is malformed.'
        ) from exc
    if (
            document.guild_id != guild_id
            or schema_version != document.schema_version
            or not isinstance(stored_digest, str)
            or not _HEX_DIGEST.fullmatch(stored_digest)
            or document_digest(document) != stored_digest
    ):
        raise GuildConfigurationDraftStorageError(
            f'Guild {guild_id} draft metadata is invalid.'
        )
    return document


def draft_from_row(row: Any) -> StoredGuildConfigurationDraft:
    if row is None or len(row) != 11:
        raise GuildConfigurationDraftStorageError('Draft row shape is invalid.')
    (
        guild_id, draft_version, base_revision, base_generation,
        schema_version, document_value, stored_digest, actor,
        created_at, updated_at, expires_at,
    ) = tuple(row)
    guild_id = _strict_positive(guild_id, 'Draft guild ID')
    draft_version = _strict_positive(draft_version, 'Draft version')
    base_revision = _strict_positive(base_revision, 'Draft base revision')
    base_generation = _strict_positive(base_generation, 'Draft base generation')
    if not isinstance(actor, str) or not actor or len(actor) > 200:
        raise GuildConfigurationDraftStorageError('Draft actor is invalid.')
    document = _validate_document_row(
        guild_id=guild_id,
        schema_version=schema_version,
        document_value=document_value,
        stored_digest=stored_digest,
    )
    return StoredGuildConfigurationDraft(
        guild_id=guild_id,
        draft_version=draft_version,
        base_revision=base_revision,
        base_generation=base_generation,
        document_digest=stored_digest,
        actor=actor,
        created_at=_timestamp(created_at, 'Draft creation timestamp'),
        updated_at=_timestamp(updated_at, 'Draft update timestamp'),
        expires_at=_timestamp(expires_at, 'Draft expiration timestamp'),
        document=document,
    )


def select_draft(
    cursor: Any,
    guild_id: int,
    *,
    active_only: bool = True,
    for_update: bool = False,
) -> StoredGuildConfigurationDraft | None:
    where = 'guild_id = %s'
    if active_only:
        where += ' AND expires_at > CURRENT_TIMESTAMP'
    suffix = ' FOR UPDATE' if for_update else ''
    cursor.execute(
        f'SELECT guild_id, draft_version, base_revision, base_generation, '
        f'schema_version, document, document_digest, actor, created_at, '
        f'updated_at, expires_at FROM "{DRAFT_TABLE}" WHERE {where}{suffix}',
        (guild_id,),
    )
    row = cursor.fetchone()
    return None if row is None else draft_from_row(row)


def select_active_configuration(
    cursor: Any,
    guild_id: int,
    *,
    for_update: bool,
) -> tuple[int, int, GuildConfigurationDocument, str]:
    suffix = ' FOR UPDATE OF registry' if for_update else ''
    cursor.execute(
        f'SELECT registry.enrollment_state, registry.active_revision, '
        f'registry.generation, revision.schema_version, revision.document, '
        f'revision.document_digest FROM "{storage.REGISTRY_TABLE}" AS registry '
        f'JOIN "{storage.REVISION_TABLE}" AS revision '
        'ON revision.guild_id = registry.guild_id '
        'AND revision.revision_number = registry.active_revision '
        f'WHERE registry.guild_id = %s{suffix}',
        (guild_id,),
    )
    row = cursor.fetchone()
    if row is None or len(row) != 6:
        raise GuildConfigurationDraftStorageError(
            'The current guild has no active configuration.'
        )
    state, revision, generation, schema_version, document_value, digest = row
    if state != 'active':
        raise GuildConfigurationDraftStorageError(
            'The current guild configuration is not active.'
        )
    revision = _strict_positive(revision, 'Active revision')
    generation = _strict_positive(generation, 'Active generation')
    document = _validate_document_row(
        guild_id=guild_id,
        schema_version=schema_version,
        document_value=document_value,
        stored_digest=digest,
    )
    return revision, generation, document, digest


def put_draft(
    cursor: Any,
    *,
    guild_id: int,
    base_revision: int,
    base_generation: int,
    document: GuildConfigurationDocument,
    actor: str,
) -> StoredGuildConfigurationDraft:
    digest = document_digest(document)
    payload = json.dumps(
        document_to_mapping(document),
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    )
    cursor.execute(
        f'INSERT INTO "{DRAFT_TABLE}" '
        '(guild_id, draft_version, base_revision, base_generation, '
        'schema_version, document, document_digest, actor, created_at, '
        'updated_at, expires_at) VALUES '
        '(%s, 1, %s, %s, %s, CAST(%s AS JSONB), %s, %s, CURRENT_TIMESTAMP, '
        f'CURRENT_TIMESTAMP, CURRENT_TIMESTAMP + INTERVAL \'{DRAFT_TTL_HOURS} hours\') '
        'ON CONFLICT (guild_id) DO UPDATE SET '
        f'draft_version = "{DRAFT_TABLE}".draft_version + 1, '
        'base_revision = EXCLUDED.base_revision, '
        'base_generation = EXCLUDED.base_generation, '
        'schema_version = EXCLUDED.schema_version, document = EXCLUDED.document, '
        'document_digest = EXCLUDED.document_digest, actor = EXCLUDED.actor, '
        'created_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP, '
        f'expires_at = CURRENT_TIMESTAMP + INTERVAL \'{DRAFT_TTL_HOURS} hours\' '
        'RETURNING guild_id, draft_version, base_revision, base_generation, '
        'schema_version, document, document_digest, actor, created_at, '
        'updated_at, expires_at',
        (
            guild_id,
            base_revision,
            base_generation,
            document.schema_version,
            payload,
            digest,
            actor,
        ),
    )
    return draft_from_row(cursor.fetchone())


def replace_draft(
    cursor: Any,
    *,
    guild_id: int,
    expected_version: int,
    expected_digest: str,
    base_revision: int,
    base_generation: int,
    document: GuildConfigurationDocument,
    actor: str,
) -> StoredGuildConfigurationDraft:
    digest = document_digest(document)
    payload = json.dumps(
        document_to_mapping(document),
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    )
    cursor.execute(
        f'UPDATE "{DRAFT_TABLE}" SET draft_version = draft_version + 1, '
        'schema_version = %s, document = CAST(%s AS JSONB), '
        'document_digest = %s, updated_at = CURRENT_TIMESTAMP, '
        f'expires_at = CURRENT_TIMESTAMP + INTERVAL \'{DRAFT_TTL_HOURS} hours\' '
        'WHERE guild_id = %s AND draft_version = %s AND document_digest = %s '
        'AND base_revision = %s AND base_generation = %s AND actor = %s '
        'AND expires_at > CURRENT_TIMESTAMP '
        'RETURNING guild_id, draft_version, base_revision, base_generation, '
        'schema_version, document, document_digest, actor, created_at, '
        'updated_at, expires_at',
        (
            document.schema_version,
            payload,
            digest,
            guild_id,
            expected_version,
            expected_digest,
            base_revision,
            base_generation,
            actor,
        ),
    )
    row = cursor.fetchone()
    if row is None:
        raise GuildConfigurationDraftStorageError(
            'The draft changed or expired; reopen it before editing.'
        )
    return draft_from_row(row)


def expire_draft(
    cursor: Any,
    *,
    guild_id: int,
    expected_version: int,
    expected_digest: str,
    actor: str,
) -> None:
    cursor.execute(
        f'UPDATE "{DRAFT_TABLE}" SET draft_version = draft_version + 1, '
        'updated_at = CURRENT_TIMESTAMP, expires_at = CURRENT_TIMESTAMP '
        'WHERE guild_id = %s AND draft_version = %s AND document_digest = %s '
        'AND actor = %s AND expires_at > CURRENT_TIMESTAMP',
        (guild_id, expected_version, expected_digest, actor),
    )
    if cursor.rowcount != 1:
        raise GuildConfigurationDraftStorageError(
            'The draft changed or expired; reopen it before discarding.'
        )


def activation_source_digest(
    *,
    draft: StoredGuildConfigurationDraft,
    actor: str,
) -> str:
    if not isinstance(draft, StoredGuildConfigurationDraft):
        raise GuildConfigurationDraftStorageError(
            'A validated stored draft is required for activation.'
        )
    if not isinstance(actor, str) or not actor or len(actor) > 200:
        raise GuildConfigurationDraftStorageError('Activation actor is invalid.')
    return _canonical_digest({
        'source_kind': ACTIVATION_SOURCE_KIND,
        'guild_id': draft.guild_id,
        'draft_version': draft.draft_version,
        'base_revision': draft.base_revision,
        'base_generation': draft.base_generation,
        'document_digest': draft.document_digest,
        'actor': actor,
    })


def activate_draft(
    cursor: Any,
    *,
    draft: StoredGuildConfigurationDraft,
    active_revision: int,
    active_generation: int,
    active_document_digest: str,
    actor: str,
    changed_paths: tuple[str, ...],
) -> GuildConfigurationActivation:
    """Create one immutable active revision/audit and consume its draft."""

    if not isinstance(draft, StoredGuildConfigurationDraft):
        raise GuildConfigurationDraftStorageError(
            'A validated stored draft is required for activation.'
        )
    active_revision = _strict_positive(active_revision, 'Active revision')
    active_generation = _strict_positive(active_generation, 'Active generation')
    if (
            draft.base_revision != active_revision
            or draft.base_generation != active_generation
            or not isinstance(active_document_digest, str)
            or not _HEX_DIGEST.fullmatch(active_document_digest)
    ):
        raise GuildConfigurationDraftStorageError(
            'The draft active base changed before activation.'
        )
    if (
            not isinstance(changed_paths, tuple)
            or not changed_paths
            or len(changed_paths) > 100
            or any(
                not isinstance(value, str)
                or not value
                or len(value) > 200
                for value in changed_paths
            )
    ):
        raise GuildConfigurationDraftStorageError(
            'Activation changed-path evidence is invalid.'
        )
    source_digest = activation_source_digest(draft=draft, actor=actor)
    payload = json.dumps(
        document_to_mapping(draft.document),
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    )
    cursor.execute(
        f'SELECT COALESCE(MAX(revision_number), 0) '
        f'FROM "{storage.REVISION_TABLE}" WHERE guild_id = %s',
        (draft.guild_id,),
    )
    revision = _strict_positive(cursor.fetchone()[0] + 1, 'Next revision')
    generation = active_generation + 1
    cursor.execute(
        f'SELECT COALESCE(MAX(event_number), 0) '
        f'FROM "{storage.AUDIT_TABLE}" WHERE guild_id = %s',
        (draft.guild_id,),
    )
    event_number = _strict_positive(
        cursor.fetchone()[0] + 1,
        'Next audit event',
    )
    cursor.execute(
        f'INSERT INTO "{storage.REVISION_TABLE}" '
        '(guild_id, revision_number, schema_version, document, '
        'document_digest, source_digest, parent_revision, source_kind, actor, '
        'created_at) VALUES (%s, %s, %s, CAST(%s AS JSONB), %s, %s, %s, %s, '
        '%s, CURRENT_TIMESTAMP)',
        (
            draft.guild_id,
            revision,
            draft.document.schema_version,
            payload,
            draft.document_digest,
            source_digest,
            active_revision,
            ACTIVATION_SOURCE_KIND,
            actor,
        ),
    )
    cursor.execute(
        f'UPDATE "{storage.REGISTRY_TABLE}" SET active_revision = %s, '
        'generation = %s, updated_at = CURRENT_TIMESTAMP '
        'WHERE guild_id = %s AND enrollment_state = %s '
        'AND active_revision = %s AND generation = %s',
        (
            revision,
            generation,
            draft.guild_id,
            'active',
            active_revision,
            active_generation,
        ),
    )
    if cursor.rowcount != 1:
        raise GuildConfigurationDraftStorageError(
            'The active configuration changed during activation.'
        )
    details = json.dumps({
        'draft_version': draft.draft_version,
        'base_revision': active_revision,
        'base_generation': active_generation,
        'previous_document_digest': active_document_digest,
        'source_digest': source_digest,
        'changed_paths': list(changed_paths),
    }, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    cursor.execute(
        f'INSERT INTO "{storage.AUDIT_TABLE}" '
        '(guild_id, event_number, event_type, revision_number, generation, '
        'document_digest, actor, details, created_at) VALUES '
        '(%s, %s, %s, %s, %s, %s, %s, CAST(%s AS JSONB), CURRENT_TIMESTAMP)',
        (
            draft.guild_id,
            event_number,
            ACTIVATION_EVENT_TYPE,
            revision,
            generation,
            draft.document_digest,
            actor,
            details,
        ),
    )
    expire_draft(
        cursor,
        guild_id=draft.guild_id,
        expected_version=draft.draft_version,
        expected_digest=draft.document_digest,
        actor=actor,
    )
    return GuildConfigurationActivation(
        guild_id=draft.guild_id,
        previous_revision=active_revision,
        previous_generation=active_generation,
        revision=revision,
        generation=generation,
        event_number=event_number,
        document_digest=draft.document_digest,
        source_digest=source_digest,
        actor=actor,
        document=draft.document,
    )


__all__ = [
    'ACTIVATION_EVENT_TYPE',
    'ACTIVATION_SOURCE_KIND',
    'CREATE_DRAFT_SCHEMA_STATEMENTS',
    'DRAFT_ADVISORY_LOCK_KEY',
    'DRAFT_SCHEMA_VERSION',
    'DRAFT_TABLE',
    'DRAFT_TTL_HOURS',
    'DraftSchemaInventory',
    'DraftSchemaPlan',
    'DraftSchemaResult',
    'EXPECTED_COLUMNS',
    'EXPECTED_CONSTRAINTS',
    'GuildConfigurationDraftStorageError',
    'GuildConfigurationActivation',
    'StoredGuildConfigurationDraft',
    'apply_draft_schema',
    'activate_draft',
    'activation_source_digest',
    'draft_from_row',
    'draft_schema_plan',
    'expire_draft',
    'inspect_draft_schema',
    'plan_to_mapping',
    'put_draft',
    'replace_draft',
    'select_active_configuration',
    'select_draft',
    'validate_draft_schema',
    'verify_draft_schema',
]
