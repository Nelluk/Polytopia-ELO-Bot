"""Development-only startup comparison for guild configuration.

P10.4 uses the result as static-authority shadow health. P10.5 may consume an
exact matched result to build the database-authority runtime snapshot. This
module still performs no publication and ordinary setting reads never query
it.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
import re
from typing import Any, Mapping, Sequence

import psycopg2

from modules import guild_configuration_storage as storage
from modules.guild_configuration_schema import (
    GuildConfigurationDocument,
    GuildConfigurationError,
    document_digest,
    document_to_mapping,
    validate_document,
)


MAX_SHADOW_GUILDS = 100
CONNECT_TIMEOUT_SECONDS = 10
STATEMENT_TIMEOUT_MILLISECONDS = 15_000
LOCK_TIMEOUT_MILLISECONDS = 5_000
STATUS_MATCHED = 'matched'
STATUS_MISMATCH = 'mismatch'
STATUS_UNAVAILABLE = 'unavailable'
STATUS_MALFORMED = 'malformed'
_HEX_DIGEST = re.compile(r'^[0-9a-f]{64}$')


class GuildConfigurationShadowError(RuntimeError):
    """The shadow comparison could not produce trustworthy evidence."""


class GuildConfigurationShadowUnavailable(GuildConfigurationShadowError):
    """The read-only shadow database connection is unavailable."""


class GuildConfigurationShadowMalformed(GuildConfigurationShadowError):
    """The stored graph or runtime comparison input is malformed."""


@dataclass(frozen=True)
class ShadowReadRequest:
    target: storage.StorageTarget
    expected_imports: tuple[storage.GuildImport, ...]
    database_password: str = field(repr=False)
    database_host: str | None = None
    database_port: int | None = None


@dataclass(frozen=True)
class ActiveConfigurationReadRequest:
    target: storage.StorageTarget
    allowed_guild_ids: tuple[int, ...]
    database_password: str = field(repr=False)
    database_host: str | None = None
    database_port: int | None = None
    include_all_active: bool = False


@dataclass(frozen=True)
class StoredGuildConfiguration:
    guild_id: int
    storage_schema_version: int
    enrollment_state: str
    active_revision: int | None
    generation: int
    document: GuildConfigurationDocument | None
    document_digest: str | None
    source_digest: str | None
    parent_revision: int | None = None
    source_kind: str | None = None
    actor: str | None = None
    bootstrap_pending: bool = False


@dataclass(frozen=True)
class GuildConfigurationMismatch:
    guild_id: int
    paths: tuple[str, ...]


@dataclass(frozen=True)
class GuildConfigurationShadowResult:
    status: str
    expected_guild_ids: tuple[int, ...] = ()
    stored_guild_ids: tuple[int, ...] = ()
    matched_guild_ids: tuple[int, ...] = ()
    mismatches: tuple[GuildConfigurationMismatch, ...] = ()
    stored_configurations: tuple[StoredGuildConfiguration, ...] = field(
        default=(),
        repr=False,
    )
    safe_reason: str | None = None

    @property
    def promotion_ready(self) -> bool:
        return self.status == STATUS_MATCHED

    @property
    def bootstrap_pending_guild_ids(self) -> tuple[int, ...]:
        """Return pending state carried by the validated stored graph."""

        return tuple(
            value.guild_id
            for value in self.stored_configurations
            if value.bootstrap_pending
        )


_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='polybot-guild-config-shadow',
)


def target_from_profile(profile: Any) -> storage.StorageTarget:
    """Freeze and validate the exact development-only runtime target."""

    target = storage.StorageTarget(
        environment=profile.environment,
        database_name=profile.database_name,
        database_user=profile.database_user,
        expected_application_id=profile.expected_bot_id,
        background_tasks_enabled=profile.background_tasks_enabled,
        api_enabled=profile.api_enabled,
        bullet_enabled=profile.bullet_enabled,
    )
    try:
        return storage.validate_target(target)
    except storage.GuildConfigurationStorageError as exc:
        raise GuildConfigurationShadowMalformed(
            'runtime_target_invalid'
        ) from exc


def _object_id(value: Any, field_name: str) -> int:
    try:
        result = int(getattr(value, 'id'))
    except (AttributeError, TypeError, ValueError) as exc:
        raise GuildConfigurationShadowMalformed(
            f'{field_name}_identity_invalid'
        ) from exc
    if result <= 0:
        raise GuildConfigurationShadowMalformed(
            f'{field_name}_identity_invalid'
        )
    return result


def _object_name(value: Any, field_name: str) -> str:
    name = getattr(value, 'name', None)
    if not isinstance(name, str) or not name:
        raise GuildConfigurationShadowMalformed(f'{field_name}_name_invalid')
    return name


def _channel_type(channel: Any) -> str:
    raw = getattr(channel, 'type', None)
    name = getattr(raw, 'name', None)
    value = str(name if name else raw)
    if not value:
        raise GuildConfigurationShadowMalformed('channel_type_invalid')
    return value


def capture_discord_snapshot(
    *,
    profile: Any,
    guilds: Sequence[Any],
    guild_ids: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Capture a bounded, member-free snapshot from the ready Discord cache."""

    target = target_from_profile(profile)
    source_ids = (
        profile.allowed_guild_ids if guild_ids is None else guild_ids
    )
    allowed = tuple(sorted(int(value) for value in source_ids))
    if not allowed or len(allowed) > MAX_SHADOW_GUILDS:
        raise GuildConfigurationShadowMalformed('allowed_guild_inventory_invalid')
    if len(allowed) != len(set(allowed)) or any(value <= 0 for value in allowed):
        raise GuildConfigurationShadowMalformed('allowed_guild_inventory_invalid')
    if len(guilds) > MAX_SHADOW_GUILDS:
        raise GuildConfigurationShadowMalformed(
            'discord_guild_inventory_unbounded'
        )
    by_id: dict[int, Any] = {}
    for guild in guilds:
        guild_id = _object_id(guild, 'guild')
        if guild_id in allowed:
            if guild_id in by_id:
                raise GuildConfigurationShadowMalformed(
                    'discord_guild_inventory_duplicate'
                )
            by_id[guild_id] = guild
    if tuple(sorted(by_id)) != allowed:
        raise GuildConfigurationShadowMalformed(
            'discord_guild_inventory_incomplete'
        )

    values = []
    for guild_id in allowed:
        guild = by_id[guild_id]
        try:
            roles = tuple(sorted(
                tuple(getattr(guild, 'roles', ())),
                key=lambda item: _object_id(item, 'role'),
            ))
            channels = tuple(sorted(
                tuple(getattr(guild, 'channels', ())),
                key=lambda item: _object_id(item, 'channel'),
            ))
        except (TypeError, ValueError) as exc:
            raise GuildConfigurationShadowMalformed(
                'discord_object_inventory_invalid'
            ) from exc
        if (
            len(roles) > storage.MAX_SNAPSHOT_ROLES
            or len(channels) > storage.MAX_SNAPSHOT_CHANNELS
        ):
            raise GuildConfigurationShadowMalformed(
                'discord_object_inventory_unbounded'
            )
        role_values = []
        for role in roles:
            is_default = getattr(role, 'is_default', None)
            if not callable(is_default):
                raise GuildConfigurationShadowMalformed(
                    'role_default_identity_invalid'
                )
            role_values.append({
                'id': _object_id(role, 'role'),
                'name': _object_name(role, 'role'),
                'managed': bool(getattr(role, 'managed', False)),
                'is_default': bool(is_default()),
            })
        channel_values = []
        for channel in channels:
            category_id = getattr(channel, 'category_id', None)
            try:
                normalized_category_id = (
                    None if category_id is None else int(category_id)
                )
            except (TypeError, ValueError) as exc:
                raise GuildConfigurationShadowMalformed(
                    'channel_category_identity_invalid'
                ) from exc
            channel_values.append({
                'id': _object_id(channel, 'channel'),
                'name': _object_name(channel, 'channel'),
                'type': _channel_type(channel),
                'category_id': normalized_category_id,
            })
        values.append({
            'guild_id': guild_id,
            'guild_name': _object_name(guild, 'guild'),
            'roles': role_values,
            'channels': channel_values,
        })
    snapshot = {
        'schema_version': storage.SNAPSHOT_SCHEMA_VERSION,
        'kind': 'guild_configuration_discord_snapshot',
        'environment': target.environment,
        'application_id': target.expected_application_id,
        'guilds': values,
    }
    try:
        storage.validate_discord_snapshot(
            snapshot,
            target=target,
            allowed_guild_ids=allowed,
        )
    except storage.GuildConfigurationStorageError as exc:
        raise GuildConfigurationShadowMalformed(
            'discord_snapshot_invalid'
        ) from exc
    return snapshot


def expected_bundle_from_runtime(
    *,
    profile: Any,
    guilds: Sequence[Any],
) -> storage.ImportBundle:
    """Materialize effective static settings from one live ready snapshot."""

    target = target_from_profile(profile)
    snapshot = capture_discord_snapshot(profile=profile, guilds=guilds)
    return expected_bundle_from_snapshot(
        profile=profile,
        discord_snapshot=snapshot,
    )


def expected_bundle_from_snapshot(
    *,
    profile: Any,
    discord_snapshot: Mapping[str, Any],
) -> storage.ImportBundle:
    """Materialize static semantics from one already captured live snapshot."""

    target = target_from_profile(profile)
    try:
        return storage.build_import_bundle(
            target=target,
            server_settings=profile.server_settings,
            allowed_guild_ids=profile.allowed_guild_ids,
            discord_snapshot=discord_snapshot,
        )
    except storage.GuildConfigurationStorageError as exc:
        raise GuildConfigurationShadowMalformed(
            'effective_static_configuration_invalid'
        ) from exc


def request_from_profile(
    *,
    profile: Any,
    expected_bundle: storage.ImportBundle,
) -> ShadowReadRequest:
    if not isinstance(expected_bundle, storage.ImportBundle):
        raise GuildConfigurationShadowMalformed('expected_bundle_invalid')
    target = target_from_profile(profile)
    request = ShadowReadRequest(
        target=target,
        expected_imports=expected_bundle.imports,
        database_password=profile.database_password,
        database_host=profile.database_host,
        database_port=profile.database_port,
    )
    return _validate_request(request)


def active_request_from_profile(profile: Any) -> ActiveConfigurationReadRequest:
    """Freeze a direct active-graph read for selected database authority."""

    raw_allowed = tuple(profile.allowed_guild_ids)
    if any(
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        for value in raw_allowed
    ):
        raise GuildConfigurationShadowMalformed(
            'allowed_guild_inventory_invalid'
        )
    request = ActiveConfigurationReadRequest(
        target=target_from_profile(profile),
        allowed_guild_ids=tuple(sorted(raw_allowed)),
        database_password=profile.database_password,
        database_host=profile.database_host,
        database_port=profile.database_port,
        include_all_active=True,
    )
    return _validate_active_request(request)


def _validate_active_request(
    request: ActiveConfigurationReadRequest,
) -> ActiveConfigurationReadRequest:
    if not isinstance(request, ActiveConfigurationReadRequest):
        raise GuildConfigurationShadowMalformed('active_request_invalid')
    try:
        storage.validate_target(request.target)
    except storage.GuildConfigurationStorageError as exc:
        raise GuildConfigurationShadowMalformed('runtime_target_invalid') from exc
    allowed = request.allowed_guild_ids
    if (
            not allowed
            or len(allowed) > MAX_SHADOW_GUILDS
            or allowed != tuple(sorted(set(allowed)))
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
                for value in allowed
            )
    ):
        raise GuildConfigurationShadowMalformed(
            'allowed_guild_inventory_invalid'
        )
    if not request.database_password:
        raise GuildConfigurationShadowMalformed(
            'database_authentication_missing'
        )
    if not isinstance(request.include_all_active, bool):
        raise GuildConfigurationShadowMalformed('active_request_invalid')
    return request


def _validate_request(request: ShadowReadRequest) -> ShadowReadRequest:
    if not isinstance(request, ShadowReadRequest):
        raise GuildConfigurationShadowMalformed('request_invalid')
    try:
        storage.validate_target(request.target)
    except storage.GuildConfigurationStorageError as exc:
        raise GuildConfigurationShadowMalformed('runtime_target_invalid') from exc
    if not request.database_password:
        raise GuildConfigurationShadowMalformed('database_authentication_missing')
    if not request.expected_imports or len(request.expected_imports) > MAX_SHADOW_GUILDS:
        raise GuildConfigurationShadowMalformed('expected_guild_inventory_invalid')
    ids = tuple(value.guild_id for value in request.expected_imports)
    if ids != tuple(sorted(ids)) or len(ids) != len(set(ids)):
        raise GuildConfigurationShadowMalformed('expected_guild_inventory_invalid')
    for value in request.expected_imports:
        if not isinstance(value, storage.GuildImport):
            raise GuildConfigurationShadowMalformed('expected_document_invalid')
        if document_digest(value.document) != value.document_digest:
            raise GuildConfigurationShadowMalformed('expected_document_digest_invalid')
    return request


def _connect(request: ShadowReadRequest):
    return psycopg2.connect(
        dbname=request.target.database_name,
        user=request.target.database_user,
        password=request.database_password,
        host=request.database_host,
        port=request.database_port,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        options=(
            f'-c statement_timeout={STATEMENT_TIMEOUT_MILLISECONDS} '
            f'-c lock_timeout={LOCK_TIMEOUT_MILLISECONDS}'
        ),
    )


def inspect_active_configuration(
    request: ActiveConfigurationReadRequest,
) -> tuple[StoredGuildConfiguration, ...]:
    """Load the complete active graph without comparing legacy static input."""

    request = _validate_active_request(request)
    try:
        connection = _connect(request)
    except psycopg2.Error as exc:
        raise GuildConfigurationShadowUnavailable(
            'database_connection_unavailable'
        ) from exc
    try:
        connection.set_session(readonly=True, autocommit=True)
        with connection.cursor() as cursor:
            cursor.execute('SHOW transaction_read_only')
            if str(cursor.fetchone()[0]).casefold() != 'on':
                raise GuildConfigurationShadowMalformed(
                    'connection_not_read_only'
                )
            cursor.execute('SELECT current_database(), current_user')
            live_database, live_user = cursor.fetchone()
            try:
                storage.validate_live_identity(
                    request.target,
                    actual_database=live_database,
                    actual_user=live_user,
                )
                if not storage.validate_schema_inventory(
                    storage.inspect_schema_inventory(cursor)
                ):
                    raise GuildConfigurationShadowMalformed(
                        'storage_schema_missing'
                    )
            except storage.GuildConfigurationStorageError as exc:
                raise GuildConfigurationShadowMalformed(
                    'storage_or_identity_invalid'
                ) from exc
            stored = _stored_values(_load_rows(cursor))
        if request.include_all_active:
            active = tuple(
                value for value in stored
                if value.enrollment_state == 'active'
            )
            active_ids = tuple(value.guild_id for value in active)
            if not set(request.allowed_guild_ids).issubset(active_ids):
                raise GuildConfigurationShadowMalformed(
                    'stored_guild_inventory_incomplete'
                )
            if any(
                value.active_revision is None
                or value.document is None
                or value.document_digest is None
                for value in active
            ):
                raise GuildConfigurationShadowMalformed(
                    'stored_active_graph_invalid'
                )
            return active
        if tuple(value.guild_id for value in stored) != request.allowed_guild_ids:
            raise GuildConfigurationShadowMalformed(
                'stored_guild_inventory_incomplete'
            )
        if any(
            value.enrollment_state != 'active'
            or value.active_revision is None
            or value.document is None
            or value.document_digest is None
            for value in stored
        ):
            raise GuildConfigurationShadowMalformed(
                'stored_active_graph_invalid'
            )
        return stored
    except psycopg2.OperationalError as exc:
        raise GuildConfigurationShadowUnavailable(
            'database_read_unavailable'
        ) from exc
    except psycopg2.Error as exc:
        raise GuildConfigurationShadowMalformed(
            'database_read_invalid'
        ) from exc
    except GuildConfigurationShadowError:
        raise
    except Exception as exc:
        raise GuildConfigurationShadowMalformed(
            'active_read_invalid'
        ) from exc
    finally:
        connection.close()


def _load_rows(cursor: Any) -> tuple[tuple[Any, ...], ...]:
    cursor.execute(
        f'SELECT registry.guild_id, registry.storage_schema_version, '
        'registry.enrollment_state, registry.active_revision, '
        'registry.generation, revision.revision_number, '
        'revision.schema_version, revision.document, '
        'revision.document_digest, revision.source_digest, '
        'revision.parent_revision, revision.source_kind, revision.actor, '
        'jsonb_build_object( '
        f"'total_count', (SELECT COUNT(*) FROM \"{storage.AUDIT_TABLE}\" "
        'WHERE guild_id = registry.guild_id), '
        "'max_event_number', COALESCE((SELECT MAX(event_number) "
        f'FROM "{storage.AUDIT_TABLE}" WHERE guild_id = registry.guild_id), 0), '
        "'relevant_count', (SELECT COUNT(*) FROM "
        f'"{storage.AUDIT_TABLE}" WHERE guild_id = registry.guild_id '
        "AND (event_number = 1 OR event_type = '"
        f"{storage.FIRST_GUILD_BOOTSTRAP_EVENT_TYPE}"
        "')), "
        "'relevant', COALESCE((SELECT jsonb_agg( "
        'jsonb_build_array(relevant.event_number, relevant.event_type, '
        'relevant.revision_number, relevant.generation, '
        'relevant.document_digest, relevant.actor, relevant.details) '
        'ORDER BY relevant.event_number) '
        'FROM (SELECT event_number, event_type, revision_number, generation, '
        'document_digest, actor, details '
        f'FROM "{storage.AUDIT_TABLE}" '
        'WHERE guild_id = registry.guild_id '
        "AND (event_number = 1 OR event_type = '"
        f"{storage.FIRST_GUILD_BOOTSTRAP_EVENT_TYPE}"
        "') ORDER BY event_number "
        f'LIMIT {storage.FIRST_GUILD_BOOTSTRAP_MAX_RELEVANT_AUDITS}) AS relevant), '
        "'[]'::jsonb)"
        ') AS bootstrap_audit_evidence '
        f'FROM "{storage.REGISTRY_TABLE}" AS registry '
        f'LEFT JOIN "{storage.REVISION_TABLE}" AS revision '
        'ON revision.guild_id = registry.guild_id '
        'AND revision.revision_number = registry.active_revision '
        'ORDER BY registry.guild_id LIMIT %s',
        (MAX_SHADOW_GUILDS + 1,),
    )
    rows = tuple(tuple(row) for row in cursor.fetchall())
    if len(rows) > MAX_SHADOW_GUILDS:
        raise GuildConfigurationShadowMalformed('stored_guild_inventory_unbounded')
    return rows


def _audit_event(raw: Any) -> tuple[int, str, int | None, int, str | None, str, Mapping[str, Any]]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 7:
        raise GuildConfigurationShadowMalformed('bootstrap_audit_event_shape_invalid')
    (
        event_number, event_type, revision_number, generation,
        document_digest_value, actor, details,
    ) = raw
    if (
        isinstance(event_number, bool)
        or not isinstance(event_number, int)
        or event_number <= 0
        or not isinstance(event_type, str)
        or not event_type
        or (
            revision_number is not None
            and (
                isinstance(revision_number, bool)
                or not isinstance(revision_number, int)
                or revision_number <= 0
            )
        )
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 0
        or (
            document_digest_value is not None
            and (
                not isinstance(document_digest_value, str)
                or not _HEX_DIGEST.fullmatch(document_digest_value)
            )
        )
        or not isinstance(actor, str)
        or not actor
        or not isinstance(details, Mapping)
    ):
        raise GuildConfigurationShadowMalformed('bootstrap_audit_event_invalid')
    return (
        event_number, event_type, revision_number, generation,
        document_digest_value, actor, details,
    )


def _bootstrap_audit_summary(
    value: Any,
) -> tuple[int, int, tuple[tuple[int, str, int | None, int, str | None, str, Mapping[str, Any]], ...]]:
    """Validate the bounded audit evidence returned by the storage query."""

    if not isinstance(value, Mapping) or set(value) != {
        'total_count', 'max_event_number', 'relevant_count', 'relevant',
    }:
        raise GuildConfigurationShadowMalformed('bootstrap_audit_summary_invalid')
    total_count = value['total_count']
    max_event_number = value['max_event_number']
    relevant_count = value['relevant_count']
    if any(
        isinstance(item, bool) or not isinstance(item, int) or item < 0
        for item in (total_count, max_event_number, relevant_count)
    ):
        raise GuildConfigurationShadowMalformed('bootstrap_audit_summary_invalid')
    raw_relevant = value['relevant']
    if not isinstance(raw_relevant, list):
        raise GuildConfigurationShadowMalformed('bootstrap_audit_summary_invalid')
    if (
        relevant_count != len(raw_relevant)
        or relevant_count > storage.FIRST_GUILD_BOOTSTRAP_MAX_RELEVANT_AUDITS
        or (total_count == 0 and (max_event_number != 0 or relevant_count != 0))
        or (total_count > 0 and max_event_number <= 0)
        or relevant_count > total_count
    ):
        raise GuildConfigurationShadowMalformed('bootstrap_audit_summary_invalid')
    events = tuple(_audit_event(item) for item in raw_relevant)
    if total_count > 0 and not any(item[0] == 1 for item in events):
        raise GuildConfigurationShadowMalformed('bootstrap_audit_first_event_missing')
    return total_count, max_event_number, events


def _validate_bootstrap_event(
    event: tuple[int, str, int | None, int, str | None, str, Mapping[str, Any]],
) -> None:
    """Validate the immutable shape of a first-bootstrap audit event."""

    if (
        event[0] != 1
        or event[2] != 1
        or event[3] != 1
        or event[4] is None
        or event[5] != storage.FIRST_GUILD_BOOTSTRAP_ACTOR
    ):
        raise GuildConfigurationShadowMalformed(
            'bootstrap_audit_current_revision_mismatch'
        )
    details = event[6]
    if (
        set(details) != {
            'template',
            'guild_name',
            'source_digest',
            'application_commands_synchronized',
        }
        or details.get('template') != storage.FIRST_GUILD_BOOTSTRAP_TEMPLATE
        or not isinstance(details.get('guild_name'), str)
        or not details.get('guild_name')
        or not isinstance(details.get('source_digest'), str)
        or not _HEX_DIGEST.fullmatch(details['source_digest'])
        or details.get('application_commands_synchronized') is not False
    ):
        raise GuildConfigurationShadowMalformed(
            'bootstrap_audit_details_mismatch'
        )


def _derive_bootstrap_pending(
    *,
    guild_id: int,
    state: str,
    active_revision: int | None,
    generation: int,
    document: GuildConfigurationDocument | None,
    stored_digest: str | None,
    source_digest: str | None,
    parent_revision: int | None,
    source_kind: str | None,
    actor: str | None,
    audit_evidence: Any,
) -> bool:
    """Derive pending only from the exact persisted first-bootstrap graph."""

    total_count, max_event_number, events = _bootstrap_audit_summary(
        audit_evidence
    )
    if (
        state == 'active'
        and active_revision == 1
        and generation == 1
        and total_count != 1
    ):
        raise GuildConfigurationShadowMalformed(
            'bootstrap_audit_current_revision_evidence_invalid'
        )
    bootstrap_events = tuple(
        event for event in events
        if event[1] == storage.FIRST_GUILD_BOOTSTRAP_EVENT_TYPE
    )
    if not bootstrap_events:
        if (
            state == 'active'
            and active_revision == 1
            and generation == 1
            and source_kind == 'owner_activation'
            and actor == storage.FIRST_GUILD_BOOTSTRAP_ACTOR
        ):
            raise GuildConfigurationShadowMalformed(
                'bootstrap_audit_evidence_missing'
            )
        return False
    if len(bootstrap_events) != 1:
        raise GuildConfigurationShadowMalformed(
            'bootstrap_audit_evidence_conflicting'
        )
    event = bootstrap_events[0]
    if event[0] != 1:
        raise GuildConfigurationShadowMalformed(
            'bootstrap_audit_event_number_invalid'
        )
    _validate_bootstrap_event(event)
    # A later revision, suspension, or resume retains the historical event but
    # is no longer the current first-bootstrap graph.
    if not (
        state == 'active'
        and active_revision == 1
        and generation == 1
    ):
        return False
    if (
        document is None
        or stored_digest is None
        or source_digest is None
        or parent_revision is not None
        or source_kind != 'owner_activation'
        or actor != storage.FIRST_GUILD_BOOTSTRAP_ACTOR
        or max_event_number != 1
        or total_count != 1
        or event[2] != active_revision
        or event[3] != generation
        or event[4] != stored_digest
        or event[5] != storage.FIRST_GUILD_BOOTSTRAP_ACTOR
    ):
        raise GuildConfigurationShadowMalformed(
            'bootstrap_audit_current_revision_mismatch'
        )
    expected_details = {
        'template': storage.FIRST_GUILD_BOOTSTRAP_TEMPLATE,
        'guild_name': document.identity.display_name,
        'source_digest': source_digest,
        'application_commands_synchronized': False,
    }
    if event[6] != expected_details:
        raise GuildConfigurationShadowMalformed(
            'bootstrap_audit_details_mismatch'
        )
    if event[4] != document_digest(document) or document.guild_id != guild_id:
        raise GuildConfigurationShadowMalformed(
            'bootstrap_audit_document_mismatch'
        )
    return True


def _stored_values(rows: Sequence[Sequence[Any]]) -> tuple[StoredGuildConfiguration, ...]:
    values = []
    seen: set[int] = set()
    for raw in rows:
        if len(raw) not in {10, 14}:
            raise GuildConfigurationShadowMalformed('stored_row_shape_invalid')
        (
            guild_id, storage_version, state, active_revision, generation,
            revision_number, schema_version, document_value, stored_digest,
            source_digest,
        ) = raw[:10]
        if len(raw) == 14:
            parent_revision, source_kind, actor, audit_evidence = raw[10:]
        else:
            parent_revision = source_kind = actor = audit_evidence = None
        if (
            isinstance(guild_id, bool)
            or not isinstance(guild_id, int)
            or guild_id <= 0
            or guild_id in seen
        ):
            raise GuildConfigurationShadowMalformed('stored_guild_identity_invalid')
        seen.add(guild_id)
        if storage_version != storage.STORAGE_SCHEMA_VERSION:
            raise GuildConfigurationShadowMalformed('stored_schema_version_invalid')
        if state not in {'pending', 'active', 'suspended', 'retired'}:
            raise GuildConfigurationShadowMalformed('stored_enrollment_state_invalid')
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise GuildConfigurationShadowMalformed('stored_generation_invalid')
        document = None
        if active_revision is None:
            if any(value is not None for value in (
                revision_number, schema_version, document_value,
                stored_digest, source_digest,
            )):
                raise GuildConfigurationShadowMalformed('inactive_revision_join_invalid')
        else:
            if (
                isinstance(active_revision, bool)
                or not isinstance(active_revision, int)
                or active_revision <= 0
                or revision_number != active_revision
            ):
                raise GuildConfigurationShadowMalformed('active_revision_invalid')
            try:
                document = validate_document(document_value)
            except GuildConfigurationError as exc:
                raise GuildConfigurationShadowMalformed(
                    'stored_document_invalid'
                ) from exc
            if (
                schema_version != document.schema_version
                or document.guild_id != guild_id
                or document_digest(document) != stored_digest
                or not isinstance(source_digest, str)
                or not _HEX_DIGEST.fullmatch(source_digest)
            ):
                raise GuildConfigurationShadowMalformed(
                    'stored_document_metadata_invalid'
                )
            if len(raw) == 14:
                if (
                    parent_revision is not None
                    and (
                        isinstance(parent_revision, bool)
                        or not isinstance(parent_revision, int)
                        or parent_revision <= 0
                    )
                ):
                    raise GuildConfigurationShadowMalformed(
                        'stored_revision_parent_invalid'
                    )
                if not isinstance(source_kind, str) or not source_kind:
                    raise GuildConfigurationShadowMalformed(
                        'stored_revision_source_invalid'
                    )
                if not isinstance(actor, str) or not actor:
                    raise GuildConfigurationShadowMalformed(
                        'stored_revision_actor_invalid'
                    )
        if state == 'active' and (active_revision is None or document is None):
            raise GuildConfigurationShadowMalformed('active_document_missing')
        if state == 'active' and generation <= 0:
            raise GuildConfigurationShadowMalformed('active_generation_invalid')
        bootstrap_pending = False
        if len(raw) == 14:
            bootstrap_pending = _derive_bootstrap_pending(
                guild_id=guild_id,
                state=state,
                active_revision=active_revision,
                generation=generation,
                document=document,
                stored_digest=stored_digest,
                source_digest=source_digest,
                parent_revision=parent_revision,
                source_kind=source_kind,
                actor=actor,
                audit_evidence=audit_evidence,
            )
        values.append(StoredGuildConfiguration(
            guild_id=guild_id,
            storage_schema_version=storage_version,
            enrollment_state=state,
            active_revision=active_revision,
            generation=generation,
            document=document,
            document_digest=stored_digest,
            source_digest=source_digest,
            parent_revision=parent_revision,
            source_kind=source_kind,
            actor=actor,
            bootstrap_pending=bootstrap_pending,
        ))
    return tuple(values)


def _difference_paths(expected: Any, stored: Any, prefix: str = '') -> tuple[str, ...]:
    paths: list[str] = []
    if isinstance(expected, Mapping) and isinstance(stored, Mapping):
        for key in sorted(set(expected) | set(stored), key=str):
            path = f'{prefix}.{key}' if prefix else str(key)
            if key not in expected or key not in stored:
                paths.append(path)
            else:
                paths.extend(_difference_paths(expected[key], stored[key], path))
    elif isinstance(expected, list) and isinstance(stored, list):
        if expected != stored:
            paths.append(prefix)
    elif expected != stored:
        paths.append(prefix)
    return tuple(paths)


def _compare(
    expected_imports: Sequence[storage.GuildImport],
    stored_values: Sequence[StoredGuildConfiguration],
) -> GuildConfigurationShadowResult:
    expected_by_id = {value.guild_id: value for value in expected_imports}
    stored_by_id = {value.guild_id: value for value in stored_values}
    expected_ids = tuple(sorted(expected_by_id))
    stored_ids = tuple(sorted(stored_by_id))
    matched = []
    mismatches = []
    for guild_id in sorted(set(expected_ids) | set(stored_ids)):
        paths = []
        expected = expected_by_id.get(guild_id)
        stored = stored_by_id.get(guild_id)
        if expected is None:
            paths.append('guild.unexpected_in_storage')
        elif stored is None:
            paths.append('guild.missing_from_storage')
        else:
            if stored.enrollment_state != 'active':
                paths.append('registry.enrollment_state')
            if stored.document is None:
                paths.append('revision.active_document')
            else:
                paths.extend(_difference_paths(
                    document_to_mapping(expected.document),
                    document_to_mapping(stored.document),
                ))
            if not paths:
                matched.append(guild_id)
        if paths:
            mismatches.append(GuildConfigurationMismatch(
                guild_id=guild_id,
                paths=tuple(paths),
            ))
    status = STATUS_MATCHED if not mismatches else STATUS_MISMATCH
    return GuildConfigurationShadowResult(
        status=status,
        expected_guild_ids=expected_ids,
        stored_guild_ids=stored_ids,
        matched_guild_ids=tuple(matched),
        mismatches=tuple(mismatches),
        stored_configurations=tuple(stored_values),
    )


def inspect_shadow_configuration(
    request: ShadowReadRequest,
) -> GuildConfigurationShadowResult:
    """Load and compare one immutable snapshot on an owned connection."""

    request = _validate_request(request)
    try:
        connection = _connect(request)
    except psycopg2.Error as exc:
        raise GuildConfigurationShadowUnavailable('database_connection_unavailable') from exc
    try:
        connection.set_session(readonly=True, autocommit=True)
        with connection.cursor() as cursor:
            cursor.execute('SHOW transaction_read_only')
            if str(cursor.fetchone()[0]).casefold() != 'on':
                raise GuildConfigurationShadowMalformed('connection_not_read_only')
            cursor.execute('SELECT current_database(), current_user')
            live_database, live_user = cursor.fetchone()
            try:
                storage.validate_live_identity(
                    request.target,
                    actual_database=live_database,
                    actual_user=live_user,
                )
                if not storage.validate_schema_inventory(
                    storage.inspect_schema_inventory(cursor)
                ):
                    raise GuildConfigurationShadowMalformed('storage_schema_missing')
            except storage.GuildConfigurationStorageError as exc:
                raise GuildConfigurationShadowMalformed(
                    'storage_or_identity_invalid'
                ) from exc
            stored = _stored_values(_load_rows(cursor))
        return _compare(request.expected_imports, stored)
    except psycopg2.OperationalError as exc:
        raise GuildConfigurationShadowUnavailable(
            'database_read_unavailable'
        ) from exc
    except psycopg2.Error as exc:
        raise GuildConfigurationShadowMalformed('database_read_invalid') from exc
    except GuildConfigurationShadowError:
        raise
    except Exception as exc:
        raise GuildConfigurationShadowMalformed('shadow_read_invalid') from exc
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


async def run_shadow_comparison(
    request: ShadowReadRequest,
) -> GuildConfigurationShadowResult:
    """Run the read-only comparison off-loop and drain it on cancellation."""

    request = _validate_request(request)
    future = _executor.submit(inspect_shadow_configuration, request)
    return await _drain_future(future)


async def run_active_configuration(
    request: ActiveConfigurationReadRequest,
) -> tuple[StoredGuildConfiguration, ...]:
    """Load direct database authority off-loop and drain worker ownership."""

    request = _validate_active_request(request)
    future = _executor.submit(inspect_active_configuration, request)
    return await _drain_future(future)


def failure_result(status: str, safe_reason: str) -> GuildConfigurationShadowResult:
    if status not in {STATUS_UNAVAILABLE, STATUS_MALFORMED}:
        raise ValueError('Unsupported guild configuration shadow failure status.')
    return GuildConfigurationShadowResult(status=status, safe_reason=safe_reason)


__all__ = [
    'ActiveConfigurationReadRequest',
    'GuildConfigurationMismatch',
    'GuildConfigurationShadowError',
    'GuildConfigurationShadowMalformed',
    'GuildConfigurationShadowResult',
    'GuildConfigurationShadowUnavailable',
    'MAX_SHADOW_GUILDS',
    'CONNECT_TIMEOUT_SECONDS',
    'LOCK_TIMEOUT_MILLISECONDS',
    'STATEMENT_TIMEOUT_MILLISECONDS',
    'STATUS_MALFORMED',
    'STATUS_MATCHED',
    'STATUS_MISMATCH',
    'STATUS_UNAVAILABLE',
    'ShadowReadRequest',
    'StoredGuildConfiguration',
    'capture_discord_snapshot',
    'active_request_from_profile',
    'expected_bundle_from_runtime',
    'expected_bundle_from_snapshot',
    'failure_result',
    'inspect_shadow_configuration',
    'inspect_active_configuration',
    'request_from_profile',
    'run_shadow_comparison',
    'run_active_configuration',
    'target_from_profile',
]
