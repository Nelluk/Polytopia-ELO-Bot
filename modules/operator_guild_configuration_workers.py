"""Bounded read-only workers for the owner guild-configuration surface."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
import json
import re
from typing import Any, Mapping, Sequence

import psycopg2

import settings
from modules import guild_configuration_shadow as shadow
from modules import guild_configuration_storage as storage
from modules.guild_configuration_schema import (
    GuildConfigurationDocument,
    GuildConfigurationError,
    document_digest,
    validate_document,
)


LIST = 'list'
SETTINGS = 'settings'
VALIDATE = 'validate'
HISTORY = 'history'
OPERATIONS = frozenset({LIST, SETTINGS, VALIDATE, HISTORY})
MAX_REGISTRY_GUILDS = shadow.MAX_SHADOW_GUILDS
MAX_REVISIONS = 25
MAX_AUDITS = 50
_HEX_DIGEST = re.compile(r'^[0-9a-f]{64}$')


class OperatorGuildConfigurationError(RuntimeError):
    """One safe owner guild-configuration read could not complete."""


class OperatorGuildConfigurationPermissionError(OperatorGuildConfigurationError):
    """The requester is not the configured owner."""


class OperatorGuildConfigurationUnavailable(OperatorGuildConfigurationError):
    """The exact runtime database read is unavailable."""


class OperatorGuildConfigurationValidationError(OperatorGuildConfigurationError):
    """Stored, live, or runtime configuration evidence is invalid."""


@dataclass(frozen=True)
class GuildConfigurationReadRequest:
    operation: str
    requester_id: int
    guild_id: int
    target: storage.StorageTarget
    allowed_guild_ids: tuple[int, ...]
    runtime_revision: int | None
    runtime_generation: int | None
    runtime_document_digest: str | None
    database_password: str = field(repr=False)
    database_host: str | None = None
    database_port: int | None = None
    discord_snapshot_json: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class GuildConfigurationRecord:
    guild_id: int
    storage_schema_version: int
    enrollment_state: str
    active_revision: int | None
    generation: int
    updated_at: str
    document_digest: str | None
    source_digest: str | None
    document: GuildConfigurationDocument | None = field(repr=False)
    last_lifecycle_event: str | None = None
    last_lifecycle_actor: str | None = None
    last_lifecycle_at: str | None = None

    @property
    def display_name(self) -> str:
        if self.document is None:
            return f'Guild {self.guild_id}'
        return self.document.identity.display_name


@dataclass(frozen=True)
class GuildConfigurationRevisionSummary:
    revision_number: int
    parent_revision: int | None
    document_digest: str
    source_kind: str
    actor: str
    created_at: str


@dataclass(frozen=True)
class GuildConfigurationAuditSummary:
    event_number: int
    event_type: str
    revision_number: int | None
    generation: int
    document_digest: str | None
    actor: str
    created_at: str


@dataclass(frozen=True)
class GuildConfigurationValidationSummary:
    storage_schema_valid: bool
    database_identity_valid: bool
    active_document_valid: bool
    live_references_valid: bool
    running_snapshot_current: bool


@dataclass(frozen=True)
class GuildConfigurationReadResult:
    operation: str
    guild_id: int
    records: tuple[GuildConfigurationRecord, ...]
    selected: GuildConfigurationRecord | None = None
    revisions: tuple[GuildConfigurationRevisionSummary, ...] = ()
    audits: tuple[GuildConfigurationAuditSummary, ...] = ()
    revisions_truncated: bool = False
    audits_truncated: bool = False
    validation: GuildConfigurationValidationSummary | None = None


_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='polybot-operator-guild-config',
)


def _strict_positive(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OperatorGuildConfigurationValidationError(
            f'{field_name} is invalid.'
        )
    return value


def _timestamp(value: Any, field_name: str) -> str:
    formatter = getattr(value, 'isoformat', None)
    if not callable(formatter):
        raise OperatorGuildConfigurationValidationError(
            f'{field_name} is invalid.'
        )
    rendered = formatter()
    if not isinstance(rendered, str) or not rendered:
        raise OperatorGuildConfigurationValidationError(
            f'{field_name} is invalid.'
        )
    return rendered


def _validate_owner(requester_id: int) -> None:
    if int(requester_id) != int(settings.owner_id):
        raise OperatorGuildConfigurationPermissionError(
            'Only the configured bot owner can inspect guild configuration.'
        )


def _validate_request(
    request: GuildConfigurationReadRequest,
) -> GuildConfigurationReadRequest:
    if not isinstance(request, GuildConfigurationReadRequest):
        raise OperatorGuildConfigurationValidationError(
            'A frozen guild-configuration read request is required.'
        )
    _validate_owner(request.requester_id)
    if request.operation not in OPERATIONS:
        raise OperatorGuildConfigurationValidationError(
            'The guild-configuration read operation is invalid.'
        )
    _strict_positive(request.guild_id, 'Guild ID')
    try:
        storage.validate_target(request.target)
    except storage.GuildConfigurationStorageError as exc:
        raise OperatorGuildConfigurationValidationError(
            'The guild-configuration runtime target is invalid.'
        ) from exc
    allowed = tuple(request.allowed_guild_ids)
    if (
            not allowed
            or allowed != tuple(sorted(set(allowed)))
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
                for value in allowed
            )
            or (
                request.operation != HISTORY
                and request.guild_id not in allowed
            )
    ):
        raise OperatorGuildConfigurationValidationError(
            'The current guild is outside the exact runtime allowlist.'
        )
    runtime_values = (
        request.runtime_revision,
        request.runtime_generation,
        request.runtime_document_digest,
    )
    if request.operation in {SETTINGS, VALIDATE} and any(
            value is None for value in runtime_values):
        raise OperatorGuildConfigurationValidationError(
            'The running database guild configuration is not published.'
        )
    if any(value is not None for value in runtime_values):
        if any(value is None for value in runtime_values):
            raise OperatorGuildConfigurationValidationError(
                'The running guild evidence is incomplete.'
            )
        _strict_positive(request.runtime_revision, 'Running revision')
        _strict_positive(request.runtime_generation, 'Running generation')
        if not _HEX_DIGEST.fullmatch(request.runtime_document_digest):
            raise OperatorGuildConfigurationValidationError(
                'The running document digest is invalid.'
            )
    if not request.database_password:
        raise OperatorGuildConfigurationValidationError(
            'Development database authentication is unavailable.'
        )
    if request.operation == VALIDATE:
        if not request.discord_snapshot_json:
            raise OperatorGuildConfigurationValidationError(
                'Live Discord identity is required for validation.'
            )
    elif request.discord_snapshot_json is not None:
        raise OperatorGuildConfigurationValidationError(
            'Live Discord identity is accepted only by validation.'
        )
    return request


def request_from_profile(
    *,
    profile: Any,
    requester_id: int,
    guild_id: int,
    operation: str,
    runtime_record: Any,
    discord_snapshot: Mapping[str, Any] | None = None,
    runtime_guild_ids: Sequence[int] | None = None,
) -> GuildConfigurationReadRequest:
    """Freeze exact runtime identity before entering the worker."""

    if (
            getattr(profile, 'environment', None) not in {
                storage.DEVELOPMENT_ENVIRONMENT,
                storage.PRODUCTION_ENVIRONMENT,
            }
            or getattr(profile, 'guild_configuration_source', None) != 'database'
    ):
        raise OperatorGuildConfigurationValidationError(
            'Guild configuration inspection requires database authority.'
        )
    if runtime_record is None and operation in {SETTINGS, VALIDATE}:
        raise OperatorGuildConfigurationValidationError(
            'The running database guild configuration is not published.'
        )
    snapshot_json = None
    if discord_snapshot is not None:
        try:
            snapshot_json = json.dumps(
                discord_snapshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(',', ':'),
            )
        except (TypeError, ValueError) as exc:
            raise OperatorGuildConfigurationValidationError(
                'Live Discord identity could not be frozen.'
            ) from exc
    try:
        target = shadow.target_from_profile(profile)
    except shadow.GuildConfigurationShadowError as exc:
        raise OperatorGuildConfigurationValidationError(
            'The guild-configuration runtime target is invalid.'
        ) from exc
    allowed_source = (
        profile.allowed_guild_ids
        if runtime_guild_ids is None else runtime_guild_ids
    )
    request = GuildConfigurationReadRequest(
        operation=str(operation),
        requester_id=int(requester_id),
        guild_id=int(guild_id),
        target=target,
        allowed_guild_ids=tuple(sorted(int(value) for value in allowed_source)),
        runtime_revision=(
            None if runtime_record is None else int(runtime_record.revision)
        ),
        runtime_generation=(
            None if runtime_record is None else int(runtime_record.generation)
        ),
        runtime_document_digest=(
            None if runtime_record is None
            else str(runtime_record.document_digest)
        ),
        database_password=profile.database_password,
        database_host=profile.database_host,
        database_port=profile.database_port,
        discord_snapshot_json=snapshot_json,
    )
    return _validate_request(request)


def _connect(request: GuildConfigurationReadRequest):
    return psycopg2.connect(
        dbname=request.target.database_name,
        user=request.target.database_user,
        password=request.database_password,
        host=request.database_host,
        port=request.database_port,
        connect_timeout=shadow.CONNECT_TIMEOUT_SECONDS,
        options=(
            f'-c statement_timeout={shadow.STATEMENT_TIMEOUT_MILLISECONDS} '
            f'-c lock_timeout={shadow.LOCK_TIMEOUT_MILLISECONDS}'
        ),
    )


def _validated_document(
    *,
    guild_id: int,
    revision_number: int,
    schema_version: Any,
    document_value: Any,
    stored_digest: Any,
    source_digest: Any,
) -> GuildConfigurationDocument:
    try:
        document = validate_document(document_value)
    except GuildConfigurationError as exc:
        raise OperatorGuildConfigurationValidationError(
            f'Guild {guild_id} revision {revision_number} is malformed.'
        ) from exc
    if (
            document.guild_id != guild_id
            or schema_version != document.schema_version
            or not isinstance(stored_digest, str)
            or not _HEX_DIGEST.fullmatch(stored_digest)
            or document_digest(document) != stored_digest
            or not isinstance(source_digest, str)
            or not _HEX_DIGEST.fullmatch(source_digest)
    ):
        raise OperatorGuildConfigurationValidationError(
            f'Guild {guild_id} revision {revision_number} metadata is invalid.'
        )
    return document


def _registry_records(cursor: Any) -> tuple[GuildConfigurationRecord, ...]:
    cursor.execute(
        f'SELECT registry.guild_id, registry.storage_schema_version, '
        'registry.enrollment_state, registry.active_revision, '
        'registry.generation, registry.updated_at, revision.revision_number, '
        'revision.schema_version, revision.document, revision.document_digest, '
        'revision.source_digest '
        ', lifecycle.event_type, lifecycle.actor, lifecycle.created_at '
        f'FROM "{storage.REGISTRY_TABLE}" AS registry '
        f'LEFT JOIN "{storage.REVISION_TABLE}" AS revision '
        'ON revision.guild_id = registry.guild_id '
        'AND revision.revision_number = registry.active_revision '
        'LEFT JOIN LATERAL (SELECT event_type, actor, created_at FROM '
        f'"{storage.AUDIT_TABLE}" WHERE guild_id = registry.guild_id '
        "AND event_type IN ('suspension', 'resumption') "
        'ORDER BY event_number DESC LIMIT 1) AS lifecycle ON TRUE '
        'ORDER BY registry.guild_id LIMIT %s',
        (MAX_REGISTRY_GUILDS + 1,),
    )
    rows = tuple(tuple(row) for row in cursor.fetchall())
    if len(rows) > MAX_REGISTRY_GUILDS:
        raise OperatorGuildConfigurationValidationError(
            'The guild-configuration registry exceeds the reviewed bound.'
        )
    records = []
    seen: set[int] = set()
    for row in rows:
        if len(row) != 14:
            raise OperatorGuildConfigurationValidationError(
                'The guild-configuration registry row shape is invalid.'
            )
        (
            guild_id, storage_version, state, active_revision, generation,
            updated_at, revision_number, schema_version, document_value,
            stored_digest, source_digest,
            lifecycle_event, lifecycle_actor, lifecycle_at,
        ) = row
        guild_id = _strict_positive(guild_id, 'Stored guild ID')
        if guild_id in seen:
            raise OperatorGuildConfigurationValidationError(
                'The guild-configuration registry duplicates a guild.'
            )
        seen.add(guild_id)
        if storage_version != storage.STORAGE_SCHEMA_VERSION:
            raise OperatorGuildConfigurationValidationError(
                f'Guild {guild_id} uses an unsupported storage schema.'
            )
        if state not in {'pending', 'active', 'suspended', 'retired'}:
            raise OperatorGuildConfigurationValidationError(
                f'Guild {guild_id} has an invalid enrollment state.'
            )
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise OperatorGuildConfigurationValidationError(
                f'Guild {guild_id} has an invalid generation.'
            )
        document = None
        if active_revision is None:
            if any(value is not None for value in (
                revision_number, schema_version, document_value,
                stored_digest, source_digest,
            )):
                raise OperatorGuildConfigurationValidationError(
                    f'Guild {guild_id} has an invalid inactive revision join.'
                )
        else:
            active_revision = _strict_positive(active_revision, 'Active revision')
            if revision_number != active_revision:
                raise OperatorGuildConfigurationValidationError(
                    f'Guild {guild_id} active revision is missing.'
                )
            document = _validated_document(
                guild_id=guild_id,
                revision_number=active_revision,
                schema_version=schema_version,
                document_value=document_value,
                stored_digest=stored_digest,
                source_digest=source_digest,
            )
        if state == 'active' and (
                active_revision is None or document is None or generation <= 0
        ):
            raise OperatorGuildConfigurationValidationError(
                f'Guild {guild_id} has no valid active configuration.'
            )
        if lifecycle_event is None:
            if lifecycle_actor is not None or lifecycle_at is not None:
                raise OperatorGuildConfigurationValidationError(
                    f'Guild {guild_id} has incomplete lifecycle evidence.'
                )
            rendered_lifecycle_at = None
        else:
            if (
                    lifecycle_event not in {'suspension', 'resumption'}
                    or not isinstance(lifecycle_actor, str)
                    or not lifecycle_actor
            ):
                raise OperatorGuildConfigurationValidationError(
                    f'Guild {guild_id} has invalid lifecycle evidence.'
                )
            rendered_lifecycle_at = _timestamp(
                lifecycle_at,
                'Lifecycle timestamp',
            )
        records.append(GuildConfigurationRecord(
            guild_id=guild_id,
            storage_schema_version=storage_version,
            enrollment_state=state,
            active_revision=active_revision,
            generation=generation,
            updated_at=_timestamp(updated_at, 'Registry timestamp'),
            document_digest=stored_digest,
            source_digest=source_digest,
            last_lifecycle_event=lifecycle_event,
            last_lifecycle_actor=lifecycle_actor,
            last_lifecycle_at=rendered_lifecycle_at,
            document=document,
        ))
    return tuple(records)


def _revision_summaries(
    cursor: Any,
    guild_id: int,
) -> tuple[tuple[GuildConfigurationRevisionSummary, ...], bool]:
    cursor.execute(
        f'SELECT revision_number, schema_version, document, document_digest, '
        'source_digest, parent_revision, source_kind, actor, created_at '
        f'FROM "{storage.REVISION_TABLE}" WHERE guild_id = %s '
        'ORDER BY revision_number DESC LIMIT %s',
        (guild_id, MAX_REVISIONS + 1),
    )
    rows = tuple(tuple(row) for row in cursor.fetchall())
    truncated = len(rows) > MAX_REVISIONS
    rows = rows[:MAX_REVISIONS]
    values = []
    for row in rows:
        if len(row) != 9:
            raise OperatorGuildConfigurationValidationError(
                'A revision-history row has an invalid shape.'
            )
        (
            revision_number, schema_version, document_value, stored_digest,
            source_digest, parent_revision, source_kind, actor, created_at,
        ) = row
        revision_number = _strict_positive(revision_number, 'Revision number')
        _validated_document(
            guild_id=guild_id,
            revision_number=revision_number,
            schema_version=schema_version,
            document_value=document_value,
            stored_digest=stored_digest,
            source_digest=source_digest,
        )
        if parent_revision is not None:
            parent_revision = _strict_positive(parent_revision, 'Parent revision')
        if source_kind not in {'legacy_static_import', 'owner_activation', 'rollback'}:
            raise OperatorGuildConfigurationValidationError(
                f'Revision {revision_number} has an invalid source kind.'
            )
        if not isinstance(actor, str) or not actor:
            raise OperatorGuildConfigurationValidationError(
                f'Revision {revision_number} has an invalid actor.'
            )
        values.append(GuildConfigurationRevisionSummary(
            revision_number=revision_number,
            parent_revision=parent_revision,
            document_digest=stored_digest,
            source_kind=source_kind,
            actor=actor,
            created_at=_timestamp(created_at, 'Revision timestamp'),
        ))
    return tuple(values), truncated


def _audit_summaries(
    cursor: Any,
    guild_id: int,
) -> tuple[tuple[GuildConfigurationAuditSummary, ...], bool]:
    cursor.execute(
        f'SELECT event_number, event_type, revision_number, generation, '
        'document_digest, actor, details, created_at '
        f'FROM "{storage.AUDIT_TABLE}" WHERE guild_id = %s '
        'ORDER BY event_number DESC LIMIT %s',
        (guild_id, MAX_AUDITS + 1),
    )
    rows = tuple(tuple(row) for row in cursor.fetchall())
    truncated = len(rows) > MAX_AUDITS
    rows = rows[:MAX_AUDITS]
    values = []
    for row in rows:
        if len(row) != 8:
            raise OperatorGuildConfigurationValidationError(
                'An audit-history row has an invalid shape.'
            )
        (
            event_number, event_type, revision_number, generation,
            stored_digest, actor, details, created_at,
        ) = row
        event_number = _strict_positive(event_number, 'Audit event number')
        if not isinstance(event_type, str) or not event_type:
            raise OperatorGuildConfigurationValidationError(
                f'Audit event {event_number} has an invalid type.'
            )
        if revision_number is not None:
            revision_number = _strict_positive(revision_number, 'Audit revision')
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise OperatorGuildConfigurationValidationError(
                f'Audit event {event_number} has an invalid generation.'
            )
        if stored_digest is not None and (
                not isinstance(stored_digest, str)
                or not _HEX_DIGEST.fullmatch(stored_digest)
        ):
            raise OperatorGuildConfigurationValidationError(
                f'Audit event {event_number} has an invalid digest.'
            )
        if not isinstance(actor, str) or not actor or not isinstance(details, Mapping):
            raise OperatorGuildConfigurationValidationError(
                f'Audit event {event_number} has invalid attribution.'
            )
        values.append(GuildConfigurationAuditSummary(
            event_number=event_number,
            event_type=event_type,
            revision_number=revision_number,
            generation=generation,
            document_digest=stored_digest,
            actor=actor,
            created_at=_timestamp(created_at, 'Audit timestamp'),
        ))
    return tuple(values), truncated


def _selected_record(
    records: Sequence[GuildConfigurationRecord],
    guild_id: int,
    *,
    require_active: bool,
) -> GuildConfigurationRecord:
    selected = tuple(record for record in records if record.guild_id == guild_id)
    if len(selected) != 1:
        raise OperatorGuildConfigurationValidationError(
            'The current guild is not uniquely enrolled.'
        )
    record = selected[0]
    if record.document is None or (
            require_active and record.enrollment_state != 'active'
    ):
        raise OperatorGuildConfigurationValidationError(
            'The selected guild does not have the required configuration.'
        )
    return record


def _validate_runtime_match(
    request: GuildConfigurationReadRequest,
    selected: GuildConfigurationRecord,
) -> None:
    if (
            selected.active_revision != request.runtime_revision
            or selected.generation != request.runtime_generation
            or selected.document_digest != request.runtime_document_digest
    ):
        raise OperatorGuildConfigurationValidationError(
            'The database active revision differs from the running immutable snapshot; '
            'restart reconciliation is required.'
        )


def _validate_live_references(
    request: GuildConfigurationReadRequest,
    selected: GuildConfigurationRecord,
) -> None:
    try:
        snapshot_value = json.loads(request.discord_snapshot_json or '')
        snapshots = storage.validate_discord_snapshot(
            snapshot_value,
            target=request.target,
            allowed_guild_ids=request.allowed_guild_ids,
        )
        storage.validate_document_references(
            selected.document,
            snapshots[selected.guild_id],
        )
    except (json.JSONDecodeError, KeyError, storage.GuildConfigurationStorageError) as exc:
        raise OperatorGuildConfigurationValidationError(
            'The active configuration does not validate against current Discord '
            'roles and channels.'
        ) from exc


def inspect_guild_configuration(
    request: GuildConfigurationReadRequest,
) -> GuildConfigurationReadResult:
    """Perform one bounded read on one owned read-only connection."""

    request = _validate_request(request)
    try:
        connection = _connect(request)
    except psycopg2.Error as exc:
        raise OperatorGuildConfigurationUnavailable(
            'The guild-configuration database is unavailable.'
        ) from exc
    try:
        connection.set_session(
            readonly=True,
            autocommit=False,
            isolation_level='REPEATABLE READ',
        )
        with connection.cursor() as cursor:
            cursor.execute('SHOW transaction_read_only')
            if str(cursor.fetchone()[0]).casefold() != 'on':
                raise OperatorGuildConfigurationValidationError(
                    'The guild-configuration connection is not read-only.'
                )
            cursor.execute('SELECT current_database(), current_user')
            actual_database, actual_user = cursor.fetchone()
            try:
                storage.validate_live_identity(
                    request.target,
                    actual_database=actual_database,
                    actual_user=actual_user,
                )
                if not storage.validate_schema_inventory(
                        storage.inspect_schema_inventory(cursor)):
                    raise storage.GuildConfigurationStorageError(
                        'Guild configuration storage is absent.'
                    )
            except storage.GuildConfigurationStorageError as exc:
                raise OperatorGuildConfigurationValidationError(
                    'The guild-configuration database identity or schema is invalid.'
                ) from exc
            records = _registry_records(cursor)
            if request.operation == LIST:
                return GuildConfigurationReadResult(
                    operation=request.operation,
                    guild_id=request.guild_id,
                    records=records,
                )
            selected = _selected_record(
                records,
                request.guild_id,
                require_active=request.operation in {SETTINGS, VALIDATE},
            )
            if request.operation in {SETTINGS, VALIDATE}:
                _validate_runtime_match(request, selected)
            revisions = ()
            audits = ()
            revisions_truncated = False
            audits_truncated = False
            validation = None
            if request.operation == HISTORY:
                revisions, revisions_truncated = _revision_summaries(
                    cursor,
                    request.guild_id,
                )
                audits, audits_truncated = _audit_summaries(
                    cursor,
                    request.guild_id,
                )
            elif request.operation == VALIDATE:
                _validate_live_references(request, selected)
                validation = GuildConfigurationValidationSummary(
                    storage_schema_valid=True,
                    database_identity_valid=True,
                    active_document_valid=True,
                    live_references_valid=True,
                    running_snapshot_current=True,
                )
            return GuildConfigurationReadResult(
                operation=request.operation,
                guild_id=request.guild_id,
                records=records,
                selected=selected,
                revisions=revisions,
                audits=audits,
                revisions_truncated=revisions_truncated,
                audits_truncated=audits_truncated,
                validation=validation,
            )
    except psycopg2.OperationalError as exc:
        raise OperatorGuildConfigurationUnavailable(
            'The guild-configuration database read was interrupted.'
        ) from exc
    except psycopg2.Error as exc:
        raise OperatorGuildConfigurationValidationError(
            'The guild-configuration database read was invalid.'
        ) from exc
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()


async def _drain_future(future: Future):
    cancellation = None
    while not future.done():
        try:
            await asyncio.sleep(0.001)
        except asyncio.CancelledError as exc:
            cancellation = exc
    if cancellation is not None:
        try:
            future.result()
        except BaseException:
            pass
        raise cancellation
    return future.result()


async def run_read(
    request: GuildConfigurationReadRequest,
) -> GuildConfigurationReadResult:
    """Run one read off-loop and drain cancellation until ownership closes."""

    request = _validate_request(request)
    future = _executor.submit(inspect_guild_configuration, request)
    return await _drain_future(future)


__all__ = [
    'GuildConfigurationAuditSummary',
    'GuildConfigurationReadRequest',
    'GuildConfigurationReadResult',
    'GuildConfigurationRecord',
    'GuildConfigurationRevisionSummary',
    'GuildConfigurationValidationSummary',
    'HISTORY',
    'LIST',
    'MAX_AUDITS',
    'MAX_REGISTRY_GUILDS',
    'MAX_REVISIONS',
    'OPERATIONS',
    'OperatorGuildConfigurationError',
    'OperatorGuildConfigurationPermissionError',
    'OperatorGuildConfigurationUnavailable',
    'OperatorGuildConfigurationValidationError',
    'SETTINGS',
    'VALIDATE',
    'inspect_guild_configuration',
    'request_from_profile',
    'run_read',
]
