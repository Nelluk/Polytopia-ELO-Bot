"""Owner-only quarantined guild enrollment workers.

Unknown guilds remain Discord-cache observations only.  This worker creates
the first authoritative registry/revision/audit graph only after a digest-
bound owner confirmation, then reloads a complete immutable runtime snapshot.
It never synchronizes Discord application commands.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

import psycopg2

import settings
from modules import guild_configuration_draft_storage as drafts
from modules import guild_configuration_runtime as runtime
from modules import guild_configuration_shadow as shadow
from modules import guild_configuration_storage as storage
from modules import guild_types
from modules.guild_configuration_schema import (
    GuildConfigurationDocument,
    document_digest,
    document_to_mapping,
    validate_document,
)


PREVIEW = 'preview'
COMMIT = 'commit'
OPERATIONS = frozenset({PREVIEW, COMMIT})
BASIC_PREFIX_TEMPLATE = 'basic_prefix_v1'
TEMPLATES = frozenset({BASIC_PREFIX_TEMPLATE})
ENROLLMENT_EVENT_TYPE = 'enrollment'
REQUIRED_BOT_PERMISSIONS = frozenset({
    'view_channel',
    'send_messages',
    'read_message_history',
})
_HEX_DIGEST = re.compile(r'^[0-9a-f]{64}$')


class OperatorGuildEnrollmentError(RuntimeError):
    """A quarantined enrollment could not safely complete."""


class OperatorGuildEnrollmentPermissionError(OperatorGuildEnrollmentError):
    """The request did not retain configured-owner authority."""


class OperatorGuildEnrollmentConflict(OperatorGuildEnrollmentError):
    """The target or running graph changed after preview."""


class OperatorGuildEnrollmentValidationError(OperatorGuildEnrollmentError):
    """The target, template, schema, or live Discord evidence is invalid."""


class OperatorGuildEnrollmentUnavailable(OperatorGuildEnrollmentError):
    """The exact runtime database operation was unavailable."""


class OperatorGuildEnrollmentCommitted(OperatorGuildEnrollmentError):
    """Enrollment committed but runtime publication needs reconciliation."""

    def __init__(self, enrollment: 'GuildEnrollment'):
        self.enrollment = enrollment
        action = 'enrollment' if enrollment.created else 'configuration update'
        super().__init__(
            f'Guild {enrollment.guild_id} {action} committed as revision '
            f'{enrollment.revision}, generation {enrollment.generation}, but '
            'the running snapshot could not be reconciled. Restart the bot; '
            'do not repeat the operation.'
        )


@dataclass(frozen=True)
class RuntimeGuildEvidence:
    guild_id: int
    revision: int
    generation: int
    document_digest: str


@dataclass(frozen=True)
class GuildEnrollmentRequest:
    operation: str
    requester_id: int
    invoking_guild_id: int
    target_guild_id: int
    target_guild_name: str
    template: str
    guild_type: str
    include_in_global_leaderboard: bool | None
    bot_permissions: tuple[str, ...]
    current_runtime: tuple[RuntimeGuildEvidence, ...]
    forbidden_guild_ids: tuple[int, ...]
    target: storage.StorageTarget
    database_password: str = field(repr=False)
    database_host: str | None = None
    database_port: int | None = None
    discord_snapshot_json: str = field(default='', repr=False)
    target_current_document_json: str | None = field(default=None, repr=False)
    expected_document_digest: str | None = None
    confirmation_text: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class GuildEnrollmentPreview:
    guild_id: int
    guild_name: str
    template: str
    guild_type: str
    existing: bool
    document_digest: str
    bot_permissions: tuple[str, ...]
    document: GuildConfigurationDocument = field(repr=False)
    previous_document_digest: str | None = None

    @property
    def confirmation(self) -> str:
        action = 'UPDATE GUILD' if self.existing else 'ENROLL'
        return f'{action} {self.guild_id} {self.document_digest}'


@dataclass(frozen=True)
class GuildEnrollment:
    guild_id: int
    guild_name: str
    template: str
    revision: int
    generation: int
    event_number: int
    document_digest: str
    actor: str
    created: bool
    document: GuildConfigurationDocument = field(repr=False)


@dataclass(frozen=True)
class GuildEnrollmentResult:
    operation: str
    preview: GuildEnrollmentPreview
    enrollment: GuildEnrollment | None = None
    runtime_snapshot: runtime.GuildConfigurationRuntimeSnapshot | None = field(
        default=None,
        repr=False,
    )


_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='polybot-guild-enrollment',
)


def _positive(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OperatorGuildEnrollmentValidationError(
            f'{field_name} is invalid.'
        )
    return value


def basic_prefix_document(
    *,
    guild_id: int,
    guild_name: str,
    guild_type: str = guild_types.STANDARD,
    include_in_global_leaderboard: bool = False,
) -> GuildConfigurationDocument:
    """Build the one reviewed, usable, least-authority onboarding template."""

    document = validate_document({
        'schema_version': 1,
        'guild_id': _positive(guild_id, 'Target guild ID'),
        'identity': {
            'display_name': str(guild_name).strip()[:100],
            'command_prefix': '$',
        },
        'permissions': {
            'helper_role_ids': [],
            'mod_role_ids': [],
            'user_role_ids_level_1': [],
            'user_role_ids_level_2': [guild_id],
            'user_role_ids_level_3': [],
            'user_role_ids_level_4': [],
            'inactive_role_id': None,
        },
        'teams': {
            'require_teams': False,
            'allow_teams': False,
            'allow_uneven_teams': False,
            'max_team_size': 2,
        },
        'visibility': {
            'include_in_global_leaderboard': include_in_global_leaderboard,
        },
        'channels': {
            'bot_channel_ids': None,
            'strict_bot_channel_ids': None,
            'private_bot_channel_ids': [],
            'newbie_message_channel_ids': [],
            'match_challenge_channel_ids': [],
            'ranked_game_channel_id': None,
            'unranked_game_channel_id': None,
            'steam_game_channel_id': None,
            'log_channel_id': None,
            'game_announce_channel_id': None,
            'staff_help_channel_id': None,
            'game_category_ids': [],
        },
        'command_capabilities': [],
    })
    return guild_types.apply_guild_type(
        document,
        guild_type,
        include_in_global_leaderboard=include_in_global_leaderboard,
    )


def _source_digest(template: str, document: GuildConfigurationDocument) -> str:
    payload = json.dumps(
        {
            'template': template,
            'schema_version': document.schema_version,
            'document_digest': document_digest(document),
        },
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def _preview(request: GuildEnrollmentRequest) -> GuildEnrollmentPreview:
    existing = request.target_current_document_json is not None
    if existing:
        try:
            current_document = validate_document(json.loads(
                request.target_current_document_json
            ))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise OperatorGuildEnrollmentValidationError(
                'The current target configuration is invalid.'
            ) from exc
        document = guild_types.apply_guild_type(
            current_document,
            request.guild_type,
            include_in_global_leaderboard=(
                request.include_in_global_leaderboard
            ),
        )
        previous_digest = document_digest(current_document)
    else:
        document = basic_prefix_document(
            guild_id=request.target_guild_id,
            guild_name=request.target_guild_name,
            guild_type=request.guild_type,
            include_in_global_leaderboard=bool(
                request.include_in_global_leaderboard
            ),
        )
        previous_digest = None
    return GuildEnrollmentPreview(
        guild_id=request.target_guild_id,
        guild_name=request.target_guild_name,
        template=request.template,
        guild_type=request.guild_type,
        existing=existing,
        document_digest=document_digest(document),
        bot_permissions=request.bot_permissions,
        document=document,
        previous_document_digest=previous_digest,
    )


def _validate_request(request: GuildEnrollmentRequest) -> GuildEnrollmentRequest:
    if not isinstance(request, GuildEnrollmentRequest):
        raise OperatorGuildEnrollmentValidationError(
            'A frozen guild-enrollment request is required.'
        )
    if int(request.requester_id) != int(settings.owner_id):
        raise OperatorGuildEnrollmentPermissionError(
            'Only the configured bot owner can enroll or reconfigure a guild.'
        )
    if request.operation not in OPERATIONS or request.template not in TEMPLATES:
        raise OperatorGuildEnrollmentValidationError(
            'The guild-enrollment operation or template is invalid.'
        )
    try:
        guild_type = guild_types.normalize_guild_type(request.guild_type)
    except guild_types.GuildTypeError as exc:
        raise OperatorGuildEnrollmentValidationError(str(exc)) from exc
    if guild_type != request.guild_type:
        raise OperatorGuildEnrollmentValidationError(
            'The guild type is not normalized.'
        )
    if (
        request.include_in_global_leaderboard is not None
        and not isinstance(request.include_in_global_leaderboard, bool)
    ):
        raise OperatorGuildEnrollmentValidationError(
            'Global leaderboard participation must be enabled or disabled.'
        )
    _positive(request.invoking_guild_id, 'Invoking guild ID')
    _positive(request.target_guild_id, 'Target guild ID')
    if (
        not request.target_guild_name
        or request.target_guild_name != request.target_guild_name.strip()
        or len(request.target_guild_name) > 100
    ):
        raise OperatorGuildEnrollmentValidationError(
            'The target guild name is invalid.'
        )
    current = request.current_runtime
    if not all(isinstance(value, RuntimeGuildEvidence) for value in current):
        raise OperatorGuildEnrollmentValidationError(
            'The running guild evidence is invalid.'
        )
    ids = tuple(value.guild_id for value in current)
    existing = request.target_current_document_json is not None
    if (
        not current
        or ids != tuple(sorted(set(ids)))
        or request.invoking_guild_id not in ids
        or (request.target_guild_id in ids) != existing
    ):
        raise OperatorGuildEnrollmentValidationError(
            'The running guild inventory is invalid for this guild operation.'
        )
    if not existing and request.target_guild_id == request.invoking_guild_id:
        raise OperatorGuildEnrollmentValidationError(
            'The current active guild cannot be enrolled again.'
        )
    if not existing and request.target_guild_id in request.forbidden_guild_ids:
        raise OperatorGuildEnrollmentValidationError(
            'A protected guild cannot be enrolled from this runtime.'
        )
    for value in current:
        _positive(value.guild_id, 'Running guild ID')
        _positive(value.revision, 'Running revision')
        _positive(value.generation, 'Running generation')
        if not _HEX_DIGEST.fullmatch(value.document_digest):
            raise OperatorGuildEnrollmentValidationError(
                'A running document digest is invalid.'
            )
    forbidden = request.forbidden_guild_ids
    if (
        any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
            for value in forbidden
        )
        or forbidden != tuple(sorted(set(forbidden)))
    ):
        raise OperatorGuildEnrollmentValidationError(
            'The forbidden guild inventory is invalid.'
        )
    if (
        any(
            not isinstance(value, str) or not value
            for value in request.bot_permissions
        )
        or tuple(sorted(set(request.bot_permissions))) != request.bot_permissions
    ):
        raise OperatorGuildEnrollmentValidationError(
            'The target-guild bot permission evidence is invalid.'
        )
    missing_permissions = tuple(sorted(
        REQUIRED_BOT_PERMISSIONS.difference(request.bot_permissions)
    ))
    if missing_permissions:
        labels = ', '.join(
            value.replace('_', ' ').title() for value in missing_permissions
        )
        raise OperatorGuildEnrollmentValidationError(
            f'The bot is missing required target-guild permissions: {labels}.'
        )
    try:
        storage.validate_target(request.target)
    except storage.GuildConfigurationStorageError as exc:
        raise OperatorGuildEnrollmentValidationError(
            'The guild-enrollment target is invalid.'
        ) from exc
    if not request.database_password or not request.discord_snapshot_json:
        raise OperatorGuildEnrollmentValidationError(
            'Enrollment database or Discord identity is unavailable.'
        )
    preview = _preview(request)
    if preview.document.guild_id != request.target_guild_id:
        raise OperatorGuildEnrollmentValidationError(
            'The target configuration belongs to a different guild.'
        )
    if existing:
        target_evidence = next(
            value for value in current
            if value.guild_id == request.target_guild_id
        )
        if preview.previous_document_digest != target_evidence.document_digest:
            raise OperatorGuildEnrollmentConflict(
                'The target configuration differs from the running snapshot.'
            )
        if preview.document_digest == preview.previous_document_digest:
            raise OperatorGuildEnrollmentValidationError(
                'This guild already has the selected type and leaderboard setting.'
            )
    expected_ids = ids if existing else (*ids, request.target_guild_id)
    try:
        snapshot_value = json.loads(request.discord_snapshot_json)
        snapshots = storage.validate_discord_snapshot(
            snapshot_value,
            target=request.target,
            allowed_guild_ids=expected_ids,
        )
        storage.validate_document_references(
            preview.document,
            snapshots[request.target_guild_id],
        )
    except (
        json.JSONDecodeError,
        KeyError,
        storage.GuildConfigurationStorageError,
    ) as exc:
        raise OperatorGuildEnrollmentValidationError(
            'The target guild roles or channels are invalid.'
        ) from exc
    if request.operation == COMMIT:
        if request.expected_document_digest != preview.document_digest:
            raise OperatorGuildEnrollmentConflict(
                'The enrollment document changed after preview.'
            )
        if request.confirmation_text != preview.confirmation:
            raise OperatorGuildEnrollmentValidationError(
                f'Confirmation must exactly match {preview.confirmation!r}.'
            )
    elif (
        request.expected_document_digest is not None
        or request.confirmation_text is not None
    ):
        raise OperatorGuildEnrollmentValidationError(
            'Confirmation evidence is accepted only by enrollment commit.'
        )
    return request


def _connect(request: GuildEnrollmentRequest):
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


def _validate_current_runtime(cursor: Any, request: GuildEnrollmentRequest) -> None:
    cursor.execute(
        f'SELECT registry.guild_id, registry.active_revision, '
        f'registry.generation, revision.document_digest FROM '
        f'"{storage.REGISTRY_TABLE}" AS registry JOIN '
        f'"{storage.REVISION_TABLE}" AS revision ON '
        'revision.guild_id = registry.guild_id AND '
        'revision.revision_number = registry.active_revision '
        'WHERE registry.enrollment_state = %s ORDER BY registry.guild_id',
        ('active',),
    )
    rows = tuple(tuple(row) for row in cursor.fetchall())
    expected = tuple(
        (
            value.guild_id,
            value.revision,
            value.generation,
            value.document_digest,
        )
        for value in request.current_runtime
    )
    if rows != expected:
        raise OperatorGuildEnrollmentConflict(
            'The active database guild inventory differs from the running '
            'snapshot; restart reconciliation is required.'
        )


def request_from_profile(
    *,
    profile: Any,
    requester_id: int,
    invoking_guild_id: int,
    target_guild_id: int,
    target_guild_name: str,
    template: str,
    guild_type: str,
    include_in_global_leaderboard: bool | None,
    bot_permissions: Sequence[str],
    current_runtime_records: Sequence[Any],
    forbidden_guild_ids: Sequence[int],
    discord_snapshot: Mapping[str, Any],
    operation: str = PREVIEW,
    expected_document_digest: str | None = None,
    confirmation_text: str | None = None,
) -> GuildEnrollmentRequest:
    if (
        getattr(profile, 'environment', None) not in {
            storage.DEVELOPMENT_ENVIRONMENT,
            storage.PRODUCTION_ENVIRONMENT,
        }
        or getattr(profile, 'guild_configuration_source', None) != 'database'
    ):
        raise OperatorGuildEnrollmentValidationError(
            'Guild enrollment requires database authority.'
        )
    try:
        target = shadow.target_from_profile(profile)
        snapshot_json = json.dumps(
            discord_snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        )
        records = tuple(current_runtime_records)
        current = tuple(sorted((
            RuntimeGuildEvidence(
                guild_id=int(record.guild_id),
                revision=int(record.revision),
                generation=int(record.generation),
                document_digest=str(record.document_digest),
            )
            for record in records
        ), key=lambda value: value.guild_id))
        target_record = next(
            (
                record for record in records
                if int(record.guild_id) == int(target_guild_id)
            ),
            None,
        )
        target_current_document_json = (
            None
            if target_record is None
            else json.dumps(
                document_to_mapping(target_record.document),
                ensure_ascii=False,
                sort_keys=True,
                separators=(',', ':'),
            )
        )
    except (
        AttributeError,
        TypeError,
        ValueError,
        shadow.GuildConfigurationShadowError,
    ) as exc:
        raise OperatorGuildEnrollmentValidationError(
            'The running guild-enrollment evidence could not be frozen.'
        ) from exc
    return _validate_request(GuildEnrollmentRequest(
        operation=str(operation),
        requester_id=int(requester_id),
        invoking_guild_id=int(invoking_guild_id),
        target_guild_id=int(target_guild_id),
        target_guild_name=str(target_guild_name),
        template=str(template),
        guild_type=guild_types.normalize_guild_type(guild_type),
        include_in_global_leaderboard=include_in_global_leaderboard,
        bot_permissions=tuple(sorted(str(value) for value in bot_permissions)),
        current_runtime=current,
        forbidden_guild_ids=tuple(sorted(
            int(value) for value in forbidden_guild_ids
        )),
        target=target,
        database_password=profile.database_password,
        database_host=profile.database_host,
        database_port=profile.database_port,
        discord_snapshot_json=snapshot_json,
        target_current_document_json=target_current_document_json,
        expected_document_digest=expected_document_digest,
        confirmation_text=confirmation_text,
    ))


def _target_absent(cursor: Any, guild_id: int) -> None:
    cursor.execute(
        f'SELECT enrollment_state FROM "{storage.REGISTRY_TABLE}" '
        'WHERE guild_id = %s',
        (guild_id,),
    )
    if cursor.fetchone() is not None:
        raise OperatorGuildEnrollmentConflict(
            'The target guild already has an enrollment record.'
        )


def _changed_paths(
    current: GuildConfigurationDocument,
    desired: GuildConfigurationDocument,
) -> tuple[str, ...]:
    def difference(expected: Any, candidate: Any, prefix: str = '') -> list[str]:
        if isinstance(expected, Mapping) and isinstance(candidate, Mapping):
            paths: list[str] = []
            for key in sorted(set(expected) | set(candidate), key=str):
                path = f'{prefix}.{key}' if prefix else str(key)
                if key not in expected or key not in candidate:
                    paths.append(path)
                else:
                    paths.extend(difference(expected[key], candidate[key], path))
            return paths
        return [] if expected == candidate else [prefix]

    return tuple(difference(
        document_to_mapping(current),
        document_to_mapping(desired),
    ))


def _insert_enrollment(
    cursor: Any,
    request: GuildEnrollmentRequest,
    preview: GuildEnrollmentPreview,
) -> GuildEnrollment:
    actor = f'discord:{request.requester_id}'
    source_digest = _source_digest(request.template, preview.document)
    document_json = json.dumps(
        document_to_mapping(preview.document),
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    )
    cursor.execute(
        f'INSERT INTO "{storage.REGISTRY_TABLE}" '
        '(guild_id, storage_schema_version, enrollment_state, '
        'active_revision, generation, created_at, updated_at) '
        "VALUES (%s, %s, 'active', NULL, 0, CURRENT_TIMESTAMP, "
        'CURRENT_TIMESTAMP)',
        (preview.guild_id, storage.STORAGE_SCHEMA_VERSION),
    )
    cursor.execute(
        f'INSERT INTO "{storage.REVISION_TABLE}" '
        '(guild_id, revision_number, schema_version, document, '
        'document_digest, source_digest, parent_revision, source_kind, '
        'actor, created_at) VALUES '
        '(%s, 1, %s, CAST(%s AS JSONB), %s, %s, NULL, %s, %s, '
        'CURRENT_TIMESTAMP)',
        (
            preview.guild_id,
            preview.document.schema_version,
            document_json,
            preview.document_digest,
            source_digest,
            drafts.ACTIVATION_SOURCE_KIND,
            actor,
        ),
    )
    cursor.execute(
        f'UPDATE "{storage.REGISTRY_TABLE}" SET active_revision = 1, '
        'generation = 1, updated_at = CURRENT_TIMESTAMP '
        'WHERE guild_id = %s AND active_revision IS NULL AND generation = 0',
        (preview.guild_id,),
    )
    if cursor.rowcount != 1:
        raise OperatorGuildEnrollmentConflict(
            'The target enrollment state changed before activation.'
        )
    details = json.dumps({
        'template': request.template,
        'invoking_guild_id': request.invoking_guild_id,
        'target_guild_name': request.target_guild_name,
        'source_digest': source_digest,
        'application_commands_synchronized': False,
    }, sort_keys=True)
    cursor.execute(
        f'INSERT INTO "{storage.AUDIT_TABLE}" '
        '(guild_id, event_number, event_type, revision_number, generation, '
        'document_digest, actor, details, created_at) '
        'VALUES (%s, 1, %s, 1, 1, %s, %s, CAST(%s AS JSONB), '
        'CURRENT_TIMESTAMP)',
        (
            preview.guild_id,
            ENROLLMENT_EVENT_TYPE,
            preview.document_digest,
            actor,
            details,
        ),
    )
    return GuildEnrollment(
        guild_id=preview.guild_id,
        guild_name=preview.guild_name,
        template=preview.template,
        revision=1,
        generation=1,
        event_number=1,
        document_digest=preview.document_digest,
        actor=actor,
        created=True,
        document=preview.document,
    )


def _update_enrollment(
    cursor: Any,
    request: GuildEnrollmentRequest,
    preview: GuildEnrollmentPreview,
) -> GuildEnrollment:
    evidence = next(
        value for value in request.current_runtime
        if value.guild_id == preview.guild_id
    )
    try:
        current_document, current_digest = drafts.select_revision(
            cursor,
            preview.guild_id,
            evidence.revision,
        )
        if current_digest != evidence.document_digest:
            raise OperatorGuildEnrollmentConflict(
                'The active target revision changed before the update.'
            )
        existing_draft = drafts.select_draft(
            cursor,
            preview.guild_id,
            active_only=True,
            for_update=True,
        )
        if existing_draft is not None:
            raise OperatorGuildEnrollmentConflict(
                'This guild has an unfinished settings draft. Save or cancel it '
                'before changing the guild type.'
            )
        actor = f'discord:{request.requester_id}'
        draft = drafts.put_draft(
            cursor,
            guild_id=preview.guild_id,
            base_revision=evidence.revision,
            base_generation=evidence.generation,
            document=preview.document,
            actor=actor,
        )
        activation = drafts.activate_draft(
            cursor,
            draft=draft,
            active_revision=evidence.revision,
            active_generation=evidence.generation,
            active_document_digest=evidence.document_digest,
            actor=actor,
            changed_paths=_changed_paths(current_document, preview.document),
        )
    except drafts.GuildConfigurationDraftStorageError as exc:
        raise OperatorGuildEnrollmentConflict(str(exc)) from exc
    return GuildEnrollment(
        guild_id=preview.guild_id,
        guild_name=preview.guild_name,
        template=preview.template,
        revision=activation.revision,
        generation=activation.generation,
        event_number=activation.event_number,
        document_digest=activation.document_digest,
        actor=activation.actor,
        created=False,
        document=activation.document,
    )


def _post_commit_snapshot(
    request: GuildEnrollmentRequest,
) -> runtime.GuildConfigurationRuntimeSnapshot:
    active = shadow.inspect_active_configuration(
        shadow.ActiveConfigurationReadRequest(
            target=request.target,
            allowed_guild_ids=tuple(
                value.guild_id for value in request.current_runtime
            ),
            database_password=request.database_password,
            database_host=request.database_host,
            database_port=request.database_port,
            include_all_active=True,
        )
    )
    active_ids = tuple(value.guild_id for value in active)
    snapshot_value = json.loads(request.discord_snapshot_json)
    return runtime.build_runtime_snapshot_from_stored(
        stored_configurations=active,
        discord_snapshot=snapshot_value,
        allowed_guild_ids=active_ids,
        target=request.target,
    )


def execute_enrollment(
    request: GuildEnrollmentRequest,
) -> GuildEnrollmentResult:
    request = _validate_request(request)
    preview = _preview(request)
    try:
        connection = _connect(request)
    except psycopg2.Error as exc:
        raise OperatorGuildEnrollmentUnavailable(
            'The guild-configuration database is unavailable.'
        ) from exc
    enrollment = None
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
                raise OperatorGuildEnrollmentValidationError(
                    'The enrollment transaction mode is invalid.'
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
                raise OperatorGuildEnrollmentValidationError(
                    'The guild-configuration storage or identity is invalid.'
                ) from exc
            if not readonly:
                cursor.execute(
                    'SELECT pg_advisory_xact_lock(%s)',
                    (drafts.DRAFT_ADVISORY_LOCK_KEY,),
                )
            _validate_current_runtime(cursor, request)
            if not preview.existing:
                _target_absent(cursor, request.target_guild_id)
            if request.operation == COMMIT:
                enrollment = (
                    _update_enrollment(cursor, request, preview)
                    if preview.existing
                    else _insert_enrollment(cursor, request, preview)
                )
                connection.commit()
                committed = True
        runtime_snapshot = None
        if enrollment is not None:
            try:
                runtime_snapshot = _post_commit_snapshot(request)
                published = runtime_snapshot.guilds.get(enrollment.guild_id)
                if published is None or (
                    published.revision,
                    published.generation,
                    published.document_digest,
                ) != (
                    enrollment.revision,
                    enrollment.generation,
                    enrollment.document_digest,
                ):
                    raise OperatorGuildEnrollmentValidationError(
                        'The committed enrollment was absent from the reloaded graph.'
                    )
            except Exception as exc:
                raise OperatorGuildEnrollmentCommitted(enrollment) from exc
        return GuildEnrollmentResult(
            operation=request.operation,
            preview=preview,
            enrollment=enrollment,
            runtime_snapshot=runtime_snapshot,
        )
    except psycopg2.OperationalError as exc:
        if committed and enrollment is not None:
            raise OperatorGuildEnrollmentCommitted(enrollment) from exc
        raise OperatorGuildEnrollmentUnavailable(
            'The guild enrollment was interrupted.'
        ) from exc
    except psycopg2.Error as exc:
        if committed and enrollment is not None:
            raise OperatorGuildEnrollmentCommitted(enrollment) from exc
        raise OperatorGuildEnrollmentValidationError(
            'The guild-enrollment transaction was invalid.'
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
        except OperatorGuildEnrollmentCommitted:
            raise
        except BaseException:
            raise cancellation
        if (
            isinstance(result, GuildEnrollmentResult)
            and result.enrollment is not None
        ):
            raise OperatorGuildEnrollmentCommitted(result.enrollment)
        raise cancellation
    return future.result()


async def run_enrollment(
    request: GuildEnrollmentRequest,
) -> GuildEnrollmentResult:
    request = _validate_request(request)
    future = _executor.submit(execute_enrollment, request)
    return await _drain_future(future)


__all__ = [
    'BASIC_PREFIX_TEMPLATE',
    'COMMIT',
    'ENROLLMENT_EVENT_TYPE',
    'GuildEnrollment',
    'GuildEnrollmentPreview',
    'GuildEnrollmentRequest',
    'GuildEnrollmentResult',
    'OperatorGuildEnrollmentCommitted',
    'OperatorGuildEnrollmentConflict',
    'OperatorGuildEnrollmentError',
    'OperatorGuildEnrollmentPermissionError',
    'OperatorGuildEnrollmentUnavailable',
    'OperatorGuildEnrollmentValidationError',
    'PREVIEW',
    'REQUIRED_BOT_PERMISSIONS',
    'RuntimeGuildEvidence',
    'basic_prefix_document',
    'execute_enrollment',
    'request_from_profile',
    'run_enrollment',
]
