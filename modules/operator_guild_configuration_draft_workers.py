"""Bounded workers for owner-only guild-configuration drafts."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
import json
import re
from typing import Any, Mapping

import psycopg2

import settings
from modules import guild_configuration_draft_storage as drafts
from modules import guild_configuration_shadow as shadow
from modules import guild_configuration_storage as storage
from modules.guild_configuration_schema import (
    GuildConfigurationDocument,
    GuildConfigurationError,
    document_digest,
    validate_document,
)


SHOW = 'show'
RESET = 'reset'
REPLACE = 'replace'
DISCARD = 'discard'
VALIDATE = 'validate'
OPERATIONS = frozenset({SHOW, RESET, REPLACE, DISCARD, VALIDATE})
WRITE_OPERATIONS = frozenset({RESET, REPLACE, DISCARD})
_HEX_DIGEST = re.compile(r'^[0-9a-f]{64}$')


class OperatorGuildConfigurationDraftError(RuntimeError):
    """One safe owner draft operation could not complete."""


class OperatorGuildConfigurationDraftPermissionError(
    OperatorGuildConfigurationDraftError,
):
    """The requester is not the configured owner."""


class OperatorGuildConfigurationDraftUnavailable(
    OperatorGuildConfigurationDraftError,
):
    """The exact development draft store is unavailable."""


class OperatorGuildConfigurationDraftConflict(
    OperatorGuildConfigurationDraftError,
):
    """The draft or its active base changed."""


class OperatorGuildConfigurationDraftValidationError(
    OperatorGuildConfigurationDraftError,
):
    """A request, document, schema, or live reference is invalid."""


@dataclass(frozen=True)
class GuildConfigurationDraftRequest:
    operation: str
    requester_id: int
    guild_id: int
    target: storage.StorageTarget
    allowed_guild_ids: tuple[int, ...]
    runtime_revision: int
    runtime_generation: int
    runtime_document_digest: str
    database_password: str = field(repr=False)
    database_host: str | None = None
    database_port: int | None = None
    expected_draft_version: int | None = None
    expected_draft_digest: str | None = None
    replacement_document_json: str | None = field(default=None, repr=False)
    discord_snapshot_json: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class GuildConfigurationDraftValidation:
    base_revision_current: bool
    document_valid: bool
    live_references_valid: bool
    runtime_snapshot_current: bool


@dataclass(frozen=True)
class GuildConfigurationDraftResult:
    operation: str
    guild_id: int
    active_revision: int
    active_generation: int
    active_document_digest: str
    draft: drafts.StoredGuildConfigurationDraft | None
    validation: GuildConfigurationDraftValidation | None = None
    committed: bool = False


_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='polybot-operator-guild-draft',
)


def _strict_positive(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OperatorGuildConfigurationDraftValidationError(
            f'{field_name} is invalid.'
        )
    return value


def _validate_owner(requester_id: int) -> None:
    if int(requester_id) != int(settings.owner_id):
        raise OperatorGuildConfigurationDraftPermissionError(
            'Only the configured bot owner can manage guild configuration drafts.'
        )


def _validate_request(
    request: GuildConfigurationDraftRequest,
) -> GuildConfigurationDraftRequest:
    if not isinstance(request, GuildConfigurationDraftRequest):
        raise OperatorGuildConfigurationDraftValidationError(
            'A frozen guild-configuration draft request is required.'
        )
    _validate_owner(request.requester_id)
    if request.operation not in OPERATIONS:
        raise OperatorGuildConfigurationDraftValidationError(
            'The guild-configuration draft operation is invalid.'
        )
    _strict_positive(request.guild_id, 'Guild ID')
    try:
        storage.validate_target(request.target)
    except storage.GuildConfigurationStorageError as exc:
        raise OperatorGuildConfigurationDraftValidationError(
            'The guild-configuration runtime target is invalid.'
        ) from exc
    allowed = tuple(request.allowed_guild_ids)
    if (
            not allowed
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
                for value in allowed
            )
            or allowed != tuple(sorted(set(allowed)))
            or request.guild_id not in allowed
    ):
        raise OperatorGuildConfigurationDraftValidationError(
            'The current guild is outside the exact development allowlist.'
        )
    _strict_positive(request.runtime_revision, 'Running revision')
    _strict_positive(request.runtime_generation, 'Running generation')
    if not _HEX_DIGEST.fullmatch(request.runtime_document_digest):
        raise OperatorGuildConfigurationDraftValidationError(
            'The running document digest is invalid.'
        )
    if not request.database_password:
        raise OperatorGuildConfigurationDraftValidationError(
            'Development database authentication is unavailable.'
        )
    if request.operation == REPLACE:
        _strict_positive(request.expected_draft_version, 'Expected draft version')
        if not isinstance(request.expected_draft_digest, str) or not _HEX_DIGEST.fullmatch(
                request.expected_draft_digest):
            raise OperatorGuildConfigurationDraftValidationError(
                'The expected draft digest is invalid.'
            )
        if not request.replacement_document_json:
            raise OperatorGuildConfigurationDraftValidationError(
                'A complete replacement draft document is required.'
            )
    elif request.operation == DISCARD:
        _strict_positive(request.expected_draft_version, 'Expected draft version')
        if not isinstance(request.expected_draft_digest, str) or not _HEX_DIGEST.fullmatch(
                request.expected_draft_digest):
            raise OperatorGuildConfigurationDraftValidationError(
                'The expected draft digest is invalid.'
            )
        if request.replacement_document_json is not None:
            raise OperatorGuildConfigurationDraftValidationError(
                'Discard does not accept replacement content.'
            )
    elif any(value is not None for value in (
        request.expected_draft_version,
        request.expected_draft_digest,
        request.replacement_document_json,
    )):
        raise OperatorGuildConfigurationDraftValidationError(
            'Optimistic draft evidence is accepted only by edit or discard.'
        )
    if request.operation == VALIDATE:
        if not request.discord_snapshot_json:
            raise OperatorGuildConfigurationDraftValidationError(
                'Live Discord identity is required for draft validation.'
            )
    elif request.discord_snapshot_json is not None:
        raise OperatorGuildConfigurationDraftValidationError(
            'Live Discord identity is accepted only by draft validation.'
        )
    return request


def request_from_profile(
    *,
    profile: Any,
    requester_id: int,
    guild_id: int,
    operation: str,
    runtime_record: Any,
    expected_draft_version: int | None = None,
    expected_draft_digest: str | None = None,
    replacement_document: Mapping[str, Any] | None = None,
    discord_snapshot: Mapping[str, Any] | None = None,
) -> GuildConfigurationDraftRequest:
    if (
            getattr(profile, 'environment', None) != storage.DEVELOPMENT_ENVIRONMENT
            or getattr(profile, 'guild_configuration_source', None) != 'database'
    ):
        raise OperatorGuildConfigurationDraftValidationError(
            'Guild configuration drafts require development database authority.'
        )
    if runtime_record is None:
        raise OperatorGuildConfigurationDraftValidationError(
            'The running database guild configuration is not published.'
        )
    try:
        target = shadow.target_from_profile(profile)
    except shadow.GuildConfigurationShadowError as exc:
        raise OperatorGuildConfigurationDraftValidationError(
            'The guild-configuration runtime target is invalid.'
        ) from exc

    def freeze(value: Mapping[str, Any] | None) -> str | None:
        if value is None:
            return None
        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(',', ':'),
            )
        except (TypeError, ValueError) as exc:
            raise OperatorGuildConfigurationDraftValidationError(
                'Guild-configuration draft input could not be frozen.'
            ) from exc

    request = GuildConfigurationDraftRequest(
        operation=str(operation),
        requester_id=int(requester_id),
        guild_id=int(guild_id),
        target=target,
        allowed_guild_ids=tuple(sorted(int(value) for value in profile.allowed_guild_ids)),
        runtime_revision=int(runtime_record.revision),
        runtime_generation=int(runtime_record.generation),
        runtime_document_digest=str(runtime_record.document_digest),
        database_password=profile.database_password,
        database_host=profile.database_host,
        database_port=profile.database_port,
        expected_draft_version=expected_draft_version,
        expected_draft_digest=expected_draft_digest,
        replacement_document_json=freeze(replacement_document),
        discord_snapshot_json=freeze(discord_snapshot),
    )
    return _validate_request(request)


def _connect(request: GuildConfigurationDraftRequest):
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


def _replacement_document(
    request: GuildConfigurationDraftRequest,
) -> GuildConfigurationDocument | None:
    if request.replacement_document_json is None:
        return None
    try:
        value = json.loads(request.replacement_document_json)
        document = validate_document(value)
    except (json.JSONDecodeError, GuildConfigurationError) as exc:
        raise OperatorGuildConfigurationDraftValidationError(
            'The replacement draft document is invalid.'
        ) from exc
    if document.guild_id != request.guild_id:
        raise OperatorGuildConfigurationDraftValidationError(
            'The replacement draft belongs to another guild.'
        )
    return document


def _validate_schema(cursor: Any) -> None:
    try:
        if not storage.validate_schema_inventory(
                storage.inspect_schema_inventory(cursor)):
            raise storage.GuildConfigurationStorageError(
                'Base guild-configuration storage is absent.'
            )
        if not drafts.validate_draft_schema(drafts.inspect_draft_schema(cursor)):
            raise drafts.GuildConfigurationDraftStorageError(
                'Guild-configuration draft storage is absent.'
            )
    except (
        storage.GuildConfigurationStorageError,
        drafts.GuildConfigurationDraftStorageError,
    ) as exc:
        raise OperatorGuildConfigurationDraftValidationError(
            'The guild-configuration draft schema is absent or invalid.'
        ) from exc


def _active(
    cursor: Any,
    request: GuildConfigurationDraftRequest,
    *,
    for_update: bool,
) -> tuple[int, int, GuildConfigurationDocument, str]:
    try:
        value = drafts.select_active_configuration(
            cursor,
            request.guild_id,
            for_update=for_update,
        )
    except drafts.GuildConfigurationDraftStorageError as exc:
        raise OperatorGuildConfigurationDraftValidationError(str(exc)) from exc
    revision, generation, document, digest = value
    if (
            revision != request.runtime_revision
            or generation != request.runtime_generation
            or digest != request.runtime_document_digest
    ):
        raise OperatorGuildConfigurationDraftConflict(
            'The database active revision differs from the running immutable '
            'snapshot; restart reconciliation is required.'
        )
    return value


def _live_validate(
    request: GuildConfigurationDraftRequest,
    draft: drafts.StoredGuildConfigurationDraft,
) -> None:
    try:
        snapshot_value = json.loads(request.discord_snapshot_json or '')
        snapshots = storage.validate_discord_snapshot(
            snapshot_value,
            target=request.target,
            allowed_guild_ids=request.allowed_guild_ids,
        )
        storage.validate_document_references(
            draft.document,
            snapshots[draft.guild_id],
        )
    except (
        json.JSONDecodeError,
        KeyError,
        storage.GuildConfigurationStorageError,
    ) as exc:
        raise OperatorGuildConfigurationDraftValidationError(
            'The draft does not validate against current Discord roles and channels.'
        ) from exc


def execute_draft_operation(
    request: GuildConfigurationDraftRequest,
) -> GuildConfigurationDraftResult:
    request = _validate_request(request)
    replacement = _replacement_document(request)
    try:
        connection = _connect(request)
    except psycopg2.Error as exc:
        raise OperatorGuildConfigurationDraftUnavailable(
            'The development guild-configuration database is unavailable.'
        ) from exc
    committed = False
    try:
        readonly = request.operation not in WRITE_OPERATIONS
        connection.set_session(
            readonly=readonly,
            autocommit=False,
            isolation_level='REPEATABLE READ',
        )
        with connection.cursor() as cursor:
            cursor.execute('SHOW transaction_read_only')
            actual_readonly = str(cursor.fetchone()[0]).casefold() == 'on'
            if actual_readonly != readonly:
                raise OperatorGuildConfigurationDraftValidationError(
                    'The guild-configuration draft transaction mode is invalid.'
                )
            cursor.execute('SELECT current_database(), current_user')
            actual_database, actual_user = cursor.fetchone()
            try:
                storage.validate_live_identity(
                    request.target,
                    actual_database=actual_database,
                    actual_user=actual_user,
                )
            except storage.GuildConfigurationStorageError as exc:
                raise OperatorGuildConfigurationDraftValidationError(
                    'The guild-configuration database identity is invalid.'
                ) from exc
            _validate_schema(cursor)
            active_revision, active_generation, active_document, active_digest = (
                _active(cursor, request, for_update=not readonly)
            )
            actor = f'discord:{request.requester_id}'
            validation = None
            if request.operation == RESET:
                draft = drafts.put_draft(
                    cursor,
                    guild_id=request.guild_id,
                    base_revision=active_revision,
                    base_generation=active_generation,
                    document=active_document,
                    actor=actor,
                )
            else:
                try:
                    draft = drafts.select_draft(
                        cursor,
                        request.guild_id,
                        active_only=True,
                        for_update=not readonly,
                    )
                except drafts.GuildConfigurationDraftStorageError as exc:
                    raise OperatorGuildConfigurationDraftValidationError(
                        'The stored guild-configuration draft is invalid.'
                    ) from exc
                if request.operation in {REPLACE, DISCARD, VALIDATE} and draft is None:
                    raise OperatorGuildConfigurationDraftConflict(
                        'No current draft exists; create a fresh draft first.'
                    )
                if draft is not None and (
                        draft.base_revision != active_revision
                        or draft.base_generation != active_generation
                ):
                    raise OperatorGuildConfigurationDraftConflict(
                        'The draft is based on an older active revision; reset it '
                        'before continuing.'
                    )
                if request.operation == REPLACE:
                    assert replacement is not None
                    try:
                        draft = drafts.replace_draft(
                            cursor,
                            guild_id=request.guild_id,
                            expected_version=request.expected_draft_version,
                            expected_digest=request.expected_draft_digest,
                            base_revision=active_revision,
                            base_generation=active_generation,
                            document=replacement,
                            actor=actor,
                        )
                    except drafts.GuildConfigurationDraftStorageError as exc:
                        raise OperatorGuildConfigurationDraftConflict(str(exc)) from exc
                elif request.operation == DISCARD:
                    try:
                        drafts.expire_draft(
                            cursor,
                            guild_id=request.guild_id,
                            expected_version=request.expected_draft_version,
                            expected_digest=request.expected_draft_digest,
                            actor=actor,
                        )
                    except drafts.GuildConfigurationDraftStorageError as exc:
                        raise OperatorGuildConfigurationDraftConflict(str(exc)) from exc
                    draft = None
                elif request.operation == VALIDATE:
                    assert draft is not None
                    _live_validate(request, draft)
                    validation = GuildConfigurationDraftValidation(
                        base_revision_current=True,
                        document_valid=True,
                        live_references_valid=True,
                        runtime_snapshot_current=True,
                    )
            if request.operation in WRITE_OPERATIONS:
                connection.commit()
                committed = True
            return GuildConfigurationDraftResult(
                operation=request.operation,
                guild_id=request.guild_id,
                active_revision=active_revision,
                active_generation=active_generation,
                active_document_digest=active_digest,
                draft=draft,
                validation=validation,
                committed=committed,
            )
    except psycopg2.OperationalError as exc:
        raise OperatorGuildConfigurationDraftUnavailable(
            'The development guild-configuration draft operation was interrupted.'
        ) from exc
    except psycopg2.Error as exc:
        raise OperatorGuildConfigurationDraftValidationError(
            'The development guild-configuration draft transaction was invalid.'
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


async def run_draft_operation(
    request: GuildConfigurationDraftRequest,
) -> GuildConfigurationDraftResult:
    request = _validate_request(request)
    future = _executor.submit(execute_draft_operation, request)
    return await _drain_future(future)


__all__ = [
    'DISCARD',
    'GuildConfigurationDraftRequest',
    'GuildConfigurationDraftResult',
    'GuildConfigurationDraftValidation',
    'OPERATIONS',
    'OperatorGuildConfigurationDraftConflict',
    'OperatorGuildConfigurationDraftError',
    'OperatorGuildConfigurationDraftPermissionError',
    'OperatorGuildConfigurationDraftUnavailable',
    'OperatorGuildConfigurationDraftValidationError',
    'REPLACE',
    'RESET',
    'SHOW',
    'VALIDATE',
    'WRITE_OPERATIONS',
    'execute_draft_operation',
    'request_from_profile',
    'run_draft_operation',
]
