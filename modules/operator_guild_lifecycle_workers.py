"""Owner-only guild suspension and resumption workers.

Lifecycle transitions preserve the complete active revision and draft history.
The worker owns only PostgreSQL state and immutable post-commit runtime
snapshots; Discord command planning and application stay on the event loop.
"""

from __future__ import annotations

import asyncio
import copy
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
import json
import re
from typing import Any, Mapping, Sequence

import psycopg2

import settings
from runtime_config import database_authentication_is_supported
from modules import guild_configuration_draft_storage as drafts
from modules import guild_configuration_runtime as runtime
from modules import guild_configuration_shadow as shadow
from modules import guild_configuration_storage as storage
from modules.guild_configuration_schema import (
    GuildConfigurationDocument,
    GuildConfigurationError,
    document_digest,
    validate_document,
)


PREVIEW = 'preview'
COMMIT = 'commit'
OPERATIONS = frozenset({PREVIEW, COMMIT})
SUSPEND = 'suspend'
RESUME = 'resume'
ACTIONS = frozenset({SUSPEND, RESUME})
SUSPENDED = 'suspended'
ACTIVE = 'active'
EVENT_TYPES = {SUSPEND: 'suspension', RESUME: 'resumption'}
_HEX_DIGEST = re.compile(r'^[0-9a-f]{64}$')


class OperatorGuildLifecycleError(RuntimeError):
    """A guild lifecycle operation could not safely finish."""


class OperatorGuildLifecyclePermissionError(OperatorGuildLifecycleError):
    """The configured owner did not authorize the operation."""


class OperatorGuildLifecycleConflict(OperatorGuildLifecycleError):
    """Database or runtime evidence changed after preview."""


class OperatorGuildLifecycleValidationError(OperatorGuildLifecycleError):
    """The target, state, schema, or live Discord evidence is invalid."""


class OperatorGuildLifecycleUnavailable(OperatorGuildLifecycleError):
    """The exact runtime database operation was unavailable."""


class OperatorGuildLifecycleCommitted(OperatorGuildLifecycleError):
    """The lifecycle state committed but runtime publication is unverified."""

    def __init__(self, transition: 'GuildLifecycleTransition'):
        self.transition = transition
        super().__init__(
            f'Guild {transition.guild_id} {transition.action} committed at '
            f'generation {transition.generation}, but the running snapshot '
            'could not be reconciled. Restart the bot before retrying; do not '
            'repeat the database transition.'
        )


class OperatorGuildLifecycleCommandUnverified(OperatorGuildLifecycleError):
    """Lifecycle committed and published, but Discord convergence is unknown."""

    def __init__(self, transition: 'GuildLifecycleTransition', detail: str):
        self.transition = transition
        super().__init__(
            f'Guild {transition.guild_id} is committed and published as '
            f'`{transition.enrollment_state}` at generation '
            f'{transition.generation}, but its Discord command tree is not '
            f'verified ({detail}). Rerun `/operator guild '
            f'{transition.action}` for reconciliation without another '
            'database write.'
        )


@dataclass(frozen=True)
class RuntimeGuildEvidence:
    guild_id: int
    revision: int
    generation: int
    document_digest: str


@dataclass(frozen=True)
class GuildLifecycleRequest:
    operation: str
    action: str
    requester_id: int
    invoking_guild_id: int
    target_guild_id: int
    target_guild_name: str
    current_runtime: tuple[RuntimeGuildEvidence, ...]
    target: storage.StorageTarget
    database_password: str = field(repr=False)
    database_host: str | None = None
    database_port: int | None = None
    discord_snapshot_json: str = field(default='', repr=False)
    expected_state: str | None = None
    expected_revision: int | None = None
    expected_generation: int | None = None
    expected_document_digest: str | None = None
    command_plan_digest: str | None = None
    confirmation_text: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class GuildLifecyclePreview:
    action: str
    guild_id: int
    guild_name: str
    current_state: str
    desired_state: str
    revision: int
    generation: int
    desired_generation: int
    document_digest: str
    command_capabilities: tuple[str, ...]
    write_required: bool
    document: GuildConfigurationDocument = field(repr=False)

    def confirmation(self, command_plan_digest: str) -> str:
        verb = self.action.upper()
        mode = 'GUILD' if self.write_required else 'SYNC'
        return (
            f'{verb} {mode} {self.guild_id} {self.document_digest} '
            f'{command_plan_digest}'
        )


@dataclass(frozen=True)
class GuildLifecycleTransition:
    action: str
    guild_id: int
    guild_name: str
    previous_state: str
    enrollment_state: str
    revision: int
    generation: int
    event_number: int
    document_digest: str
    command_plan_digest: str
    actor: str


@dataclass(frozen=True)
class GuildLifecycleResult:
    operation: str
    preview: GuildLifecyclePreview
    transition: GuildLifecycleTransition | None = None
    runtime_snapshot: runtime.GuildConfigurationRuntimeSnapshot | None = field(
        default=None,
        repr=False,
    )


@dataclass(frozen=True)
class GuildLifecycleCompletion:
    preview: GuildLifecyclePreview
    transition: GuildLifecycleTransition | None
    command_apply: Any


_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='polybot-guild-lifecycle',
)


def _positive(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OperatorGuildLifecycleValidationError(f'{label} is invalid.')
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _HEX_DIGEST.fullmatch(value):
        raise OperatorGuildLifecycleValidationError(f'{label} is invalid.')
    return value


def _desired_state(action: str) -> str:
    return SUSPENDED if action == SUSPEND else ACTIVE


def _validate_request(request: GuildLifecycleRequest) -> GuildLifecycleRequest:
    if not isinstance(request, GuildLifecycleRequest):
        raise OperatorGuildLifecycleValidationError(
            'A frozen guild-lifecycle request is required.'
        )
    if int(request.requester_id) != int(settings.owner_id):
        raise OperatorGuildLifecyclePermissionError(
            'Only the configured bot owner can suspend or resume a guild.'
        )
    if request.operation not in OPERATIONS or request.action not in ACTIONS:
        raise OperatorGuildLifecycleValidationError(
            'The guild-lifecycle operation or action is invalid.'
        )
    _positive(request.invoking_guild_id, 'Invoking guild ID')
    _positive(request.target_guild_id, 'Target guild ID')
    if request.invoking_guild_id == request.target_guild_id:
        raise OperatorGuildLifecycleValidationError(
            'Run lifecycle controls from a different active guild so recovery '
            'authority remains available.'
        )
    if (
            not request.target_guild_name
            or request.target_guild_name != request.target_guild_name.strip()
            or len(request.target_guild_name) > 100
    ):
        raise OperatorGuildLifecycleValidationError(
            'The target guild name is invalid.'
        )
    current = request.current_runtime
    if not current or not all(isinstance(value, RuntimeGuildEvidence) for value in current):
        raise OperatorGuildLifecycleValidationError(
            'The running guild evidence is invalid.'
        )
    ids = tuple(value.guild_id for value in current)
    if ids != tuple(sorted(set(ids))) or request.invoking_guild_id not in ids:
        raise OperatorGuildLifecycleValidationError(
            'The running active-guild inventory is invalid.'
        )
    for value in current:
        _positive(value.guild_id, 'Running guild ID')
        _positive(value.revision, 'Running revision')
        _positive(value.generation, 'Running generation')
        _digest(value.document_digest, 'Running document digest')
    try:
        storage.validate_target(request.target)
    except storage.GuildConfigurationStorageError as exc:
        raise OperatorGuildLifecycleValidationError(
            'The runtime lifecycle target is invalid.'
        ) from exc
    if not database_authentication_is_supported(
            environment=request.target.environment,
            database_password=request.database_password,
            database_host=request.database_host,
    ) or not request.discord_snapshot_json:
        raise OperatorGuildLifecycleValidationError(
            'Lifecycle database or Discord identity is unavailable.'
        )
    if request.operation == COMMIT:
        if request.expected_state not in {ACTIVE, SUSPENDED}:
            raise OperatorGuildLifecycleValidationError(
                'The expected lifecycle state is invalid.'
            )
        _positive(request.expected_revision, 'Expected revision')
        _positive(request.expected_generation, 'Expected generation')
        _digest(request.expected_document_digest, 'Expected document digest')
        plan_digest = _digest(request.command_plan_digest, 'Command-plan digest')
        expected = (
            f'{request.action.upper()} GUILD {request.target_guild_id} '
            f'{request.expected_document_digest} {plan_digest}'
        )
        if request.confirmation_text != expected:
            raise OperatorGuildLifecycleValidationError(
                f'Lifecycle transition requires exact confirmation {expected!r}.'
            )
    elif any(value is not None for value in (
        request.expected_state,
        request.expected_revision,
        request.expected_generation,
        request.expected_document_digest,
        request.command_plan_digest,
        request.confirmation_text,
    )):
        raise OperatorGuildLifecycleValidationError(
            'Commit evidence is accepted only by lifecycle commit.'
        )
    return request


def request_from_profile(
    *,
    profile: Any,
    requester_id: int,
    invoking_guild_id: int,
    target_guild_id: int,
    target_guild_name: str,
    current_runtime_records: Sequence[Any],
    discord_snapshot: Mapping[str, Any],
    action: str,
    operation: str = PREVIEW,
    expected_state: str | None = None,
    expected_revision: int | None = None,
    expected_generation: int | None = None,
    expected_document_digest: str | None = None,
    command_plan_digest: str | None = None,
    confirmation_text: str | None = None,
) -> GuildLifecycleRequest:
    if (
            getattr(profile, 'environment', None) not in {
                storage.DEVELOPMENT_ENVIRONMENT,
                storage.PRODUCTION_ENVIRONMENT,
            }
            or getattr(profile, 'guild_configuration_source', None) != 'database'
    ):
        raise OperatorGuildLifecycleValidationError(
            'Guild lifecycle controls require database authority.'
        )
    try:
        target = shadow.target_from_profile(profile)
        current = tuple(sorted((
            RuntimeGuildEvidence(
                guild_id=int(record.guild_id),
                revision=int(record.revision),
                generation=int(record.generation),
                document_digest=str(record.document_digest),
            )
            for record in current_runtime_records
        ), key=lambda value: value.guild_id))
        snapshot_json = json.dumps(
            discord_snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        )
    except (TypeError, ValueError, shadow.GuildConfigurationShadowError) as exc:
        raise OperatorGuildLifecycleValidationError(
            'The guild-lifecycle evidence could not be frozen.'
        ) from exc
    return _validate_request(GuildLifecycleRequest(
        operation=str(operation),
        action=str(action),
        requester_id=int(requester_id),
        invoking_guild_id=int(invoking_guild_id),
        target_guild_id=int(target_guild_id),
        target_guild_name=str(target_guild_name),
        current_runtime=current,
        target=target,
        database_password=profile.database_password,
        database_host=profile.database_host,
        database_port=profile.database_port,
        discord_snapshot_json=snapshot_json,
        expected_state=expected_state,
        expected_revision=expected_revision,
        expected_generation=expected_generation,
        expected_document_digest=expected_document_digest,
        command_plan_digest=command_plan_digest,
        confirmation_text=confirmation_text,
    ))


def _connect(request: GuildLifecycleRequest):
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


def _validate_current_runtime(cursor: Any, request: GuildLifecycleRequest) -> None:
    cursor.execute(
        f'SELECT registry.guild_id, registry.active_revision, '
        f'registry.generation, revision.document_digest FROM '
        f'"{storage.REGISTRY_TABLE}" AS registry JOIN '
        f'"{storage.REVISION_TABLE}" AS revision ON '
        'revision.guild_id = registry.guild_id AND '
        'revision.revision_number = registry.active_revision '
        'WHERE registry.enrollment_state = %s ORDER BY registry.guild_id',
        (ACTIVE,),
    )
    rows = tuple(tuple(row) for row in cursor.fetchall())
    expected = tuple(
        (value.guild_id, value.revision, value.generation, value.document_digest)
        for value in request.current_runtime
    )
    if rows != expected:
        raise OperatorGuildLifecycleConflict(
            'The active database guild inventory differs from the running '
            'snapshot; restart reconciliation is required.'
        )


def _target_preview(
    cursor: Any,
    request: GuildLifecycleRequest,
    *,
    for_update: bool,
) -> GuildLifecyclePreview:
    cursor.execute(
        f'SELECT registry.enrollment_state, registry.active_revision, '
        f'registry.generation, revision.schema_version, revision.document, '
        f'revision.document_digest FROM "{storage.REGISTRY_TABLE}" AS registry '
        f'JOIN "{storage.REVISION_TABLE}" AS revision ON '
        'revision.guild_id = registry.guild_id AND '
        'revision.revision_number = registry.active_revision '
        'WHERE registry.guild_id = %s' + (' FOR UPDATE' if for_update else ''),
        (request.target_guild_id,),
    )
    row = cursor.fetchone()
    if row is None or len(row) != 6:
        raise OperatorGuildLifecycleValidationError(
            'The target guild has no resumable configuration revision.'
        )
    state, revision, generation, schema_version, document_value, stored_digest = row
    if state not in {ACTIVE, SUSPENDED}:
        raise OperatorGuildLifecycleValidationError(
            f'Guild lifecycle cannot change target state {state!r}.'
        )
    revision = _positive(revision, 'Target revision')
    generation = _positive(generation, 'Target generation')
    try:
        document = validate_document(document_value)
    except GuildConfigurationError as exc:
        raise OperatorGuildLifecycleValidationError(
            'The target active revision is malformed.'
        ) from exc
    if (
            schema_version != document.schema_version
            or document.guild_id != request.target_guild_id
            or document_digest(document) != stored_digest
    ):
        raise OperatorGuildLifecycleValidationError(
            'The target active revision metadata is invalid.'
        )
    desired = _desired_state(request.action)
    write_required = state != desired
    active_ids = {value.guild_id for value in request.current_runtime}
    if (state == ACTIVE) != (request.target_guild_id in active_ids):
        raise OperatorGuildLifecycleConflict(
            'The target lifecycle state differs from the running active inventory.'
        )
    if request.action == SUSPEND and state == ACTIVE and len(active_ids) < 2:
        raise OperatorGuildLifecycleValidationError(
            'The last active guild cannot be suspended because no trusted '
            'Discord control context would remain for resume.'
        )
    try:
        snapshot_value = json.loads(request.discord_snapshot_json)
        snapshots = storage.validate_discord_snapshot(
            snapshot_value,
            target=request.target,
            allowed_guild_ids=tuple(sorted(active_ids | {request.target_guild_id})),
        )
        if request.action == RESUME:
            storage.validate_document_references(
                document,
                snapshots[request.target_guild_id],
            )
    except (
        json.JSONDecodeError,
        KeyError,
        storage.GuildConfigurationStorageError,
    ) as exc:
        raise OperatorGuildLifecycleValidationError(
            'The target guild or its configured Discord references are invalid.'
        ) from exc
    preview = GuildLifecyclePreview(
        action=request.action,
        guild_id=request.target_guild_id,
        guild_name=request.target_guild_name,
        current_state=state,
        desired_state=desired,
        revision=revision,
        generation=generation,
        desired_generation=generation + (1 if write_required else 0),
        document_digest=stored_digest,
        command_capabilities=document.command_capabilities,
        write_required=write_required,
        document=document,
    )
    if request.operation == COMMIT and (
        not write_required
        or request.expected_state != state
        or request.expected_revision != revision
        or request.expected_generation != generation
        or request.expected_document_digest != stored_digest
    ):
        raise OperatorGuildLifecycleConflict(
            'The target lifecycle evidence changed after preview.'
        )
    return preview


def _transition(
    cursor: Any,
    request: GuildLifecycleRequest,
    preview: GuildLifecyclePreview,
) -> GuildLifecycleTransition:
    desired_generation = preview.generation + 1
    cursor.execute(
        f'UPDATE "{storage.REGISTRY_TABLE}" SET enrollment_state = %s, '
        'generation = %s, updated_at = CURRENT_TIMESTAMP '
        'WHERE guild_id = %s AND enrollment_state = %s '
        'AND active_revision = %s AND generation = %s',
        (
            preview.desired_state,
            desired_generation,
            preview.guild_id,
            preview.current_state,
            preview.revision,
            preview.generation,
        ),
    )
    if cursor.rowcount != 1:
        raise OperatorGuildLifecycleConflict(
            'The target lifecycle state changed before commit.'
        )
    cursor.execute(
        f'SELECT COALESCE(MAX(event_number), 0) + 1 FROM '
        f'"{storage.AUDIT_TABLE}" WHERE guild_id = %s',
        (preview.guild_id,),
    )
    event_number = _positive(cursor.fetchone()[0], 'Lifecycle event number')
    actor = f'discord:{request.requester_id}'
    details = json.dumps({
        'action': request.action,
        'previous_state': preview.current_state,
        'enrollment_state': preview.desired_state,
        'invoking_guild_id': request.invoking_guild_id,
        'target_guild_name': request.target_guild_name,
        'command_plan_digest': request.command_plan_digest,
        'application_commands_synchronized': False,
    }, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    cursor.execute(
        f'INSERT INTO "{storage.AUDIT_TABLE}" '
        '(guild_id, event_number, event_type, revision_number, generation, '
        'document_digest, actor, details, created_at) '
        'VALUES (%s, %s, %s, %s, %s, %s, %s, CAST(%s AS JSONB), '
        'CURRENT_TIMESTAMP)',
        (
            preview.guild_id,
            event_number,
            EVENT_TYPES[request.action],
            preview.revision,
            desired_generation,
            preview.document_digest,
            actor,
            details,
        ),
    )
    return GuildLifecycleTransition(
        action=request.action,
        guild_id=preview.guild_id,
        guild_name=preview.guild_name,
        previous_state=preview.current_state,
        enrollment_state=preview.desired_state,
        revision=preview.revision,
        generation=desired_generation,
        event_number=event_number,
        document_digest=preview.document_digest,
        command_plan_digest=str(request.command_plan_digest),
        actor=actor,
    )


def _post_commit_snapshot(
    request: GuildLifecycleRequest,
) -> runtime.GuildConfigurationRuntimeSnapshot:
    expected_active = {value.guild_id for value in request.current_runtime}
    if request.action == SUSPEND:
        expected_active.remove(request.target_guild_id)
    else:
        expected_active.add(request.target_guild_id)
    active = shadow.inspect_active_configuration(
        shadow.ActiveConfigurationReadRequest(
            target=request.target,
            allowed_guild_ids=tuple(sorted(expected_active)),
            database_password=request.database_password,
            database_host=request.database_host,
            database_port=request.database_port,
            include_all_active=True,
        )
    )
    active_ids = tuple(value.guild_id for value in active)
    if active_ids != tuple(sorted(expected_active)):
        raise OperatorGuildLifecycleValidationError(
            'The reloaded active guild inventory differs from the transition.'
        )
    snapshot_value = json.loads(request.discord_snapshot_json)
    filtered = copy.deepcopy(snapshot_value)
    filtered['guilds'] = [
        value for value in filtered['guilds']
        if value.get('guild_id') in expected_active
    ]
    return runtime.build_runtime_snapshot_from_stored(
        stored_configurations=active,
        discord_snapshot=filtered,
        allowed_guild_ids=active_ids,
        target=request.target,
    )


def execute_lifecycle(request: GuildLifecycleRequest) -> GuildLifecycleResult:
    request = _validate_request(request)
    try:
        connection = _connect(request)
    except psycopg2.Error as exc:
        raise OperatorGuildLifecycleUnavailable(
            'The guild-lifecycle database is unavailable.'
        ) from exc
    transition = None
    committed = False
    try:
        readonly = request.operation == PREVIEW
        connection.set_session(
            readonly=readonly,
            autocommit=False,
            isolation_level='REPEATABLE READ',
        )
        with connection.cursor() as cursor:
            cursor.execute('SHOW transaction_read_only')
            if (str(cursor.fetchone()[0]).casefold() == 'on') != readonly:
                raise OperatorGuildLifecycleValidationError(
                    'The lifecycle transaction mode is invalid.'
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
                        'Guild-configuration storage is absent.'
                    )
            except storage.GuildConfigurationStorageError as exc:
                raise OperatorGuildLifecycleValidationError(
                    'The guild-lifecycle storage or identity is invalid.'
                ) from exc
            if not readonly:
                cursor.execute(
                    'SELECT pg_advisory_xact_lock(%s)',
                    (drafts.DRAFT_ADVISORY_LOCK_KEY,),
                )
            _validate_current_runtime(cursor, request)
            preview = _target_preview(cursor, request, for_update=not readonly)
            if request.operation == COMMIT:
                transition = _transition(cursor, request, preview)
                connection.commit()
                committed = True
        runtime_snapshot = None
        if transition is not None:
            try:
                runtime_snapshot = _post_commit_snapshot(request)
            except Exception as exc:
                raise OperatorGuildLifecycleCommitted(transition) from exc
        return GuildLifecycleResult(
            operation=request.operation,
            preview=preview,
            transition=transition,
            runtime_snapshot=runtime_snapshot,
        )
    except psycopg2.OperationalError as exc:
        if committed and transition is not None:
            raise OperatorGuildLifecycleCommitted(transition) from exc
        raise OperatorGuildLifecycleUnavailable(
            'The guild-lifecycle operation was interrupted.'
        ) from exc
    except psycopg2.Error as exc:
        if committed and transition is not None:
            raise OperatorGuildLifecycleCommitted(transition) from exc
        raise OperatorGuildLifecycleValidationError(
            'The guild-lifecycle transaction was invalid.'
        ) from exc
    finally:
        if not committed:
            connection.rollback()
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
            result = future.result()
        except OperatorGuildLifecycleCommitted:
            raise
        except BaseException:
            raise cancellation
        if (
                isinstance(result, GuildLifecycleResult)
                and result.transition is not None
        ):
            raise OperatorGuildLifecycleCommitted(result.transition)
        raise cancellation
    return future.result()


async def run_lifecycle(
    request: GuildLifecycleRequest,
) -> GuildLifecycleResult:
    request = _validate_request(request)
    future = _executor.submit(execute_lifecycle, request)
    return await _drain_future(future)


__all__ = [
    'ACTIONS',
    'ACTIVE',
    'COMMIT',
    'GuildLifecyclePreview',
    'GuildLifecycleCompletion',
    'GuildLifecycleRequest',
    'GuildLifecycleResult',
    'GuildLifecycleTransition',
    'OperatorGuildLifecycleCommitted',
    'OperatorGuildLifecycleCommandUnverified',
    'OperatorGuildLifecycleConflict',
    'OperatorGuildLifecycleError',
    'OperatorGuildLifecyclePermissionError',
    'OperatorGuildLifecycleUnavailable',
    'OperatorGuildLifecycleValidationError',
    'PREVIEW',
    'RESUME',
    'RuntimeGuildEvidence',
    'SUSPEND',
    'SUSPENDED',
    'execute_lifecycle',
    'request_from_profile',
    'run_lifecycle',
]
