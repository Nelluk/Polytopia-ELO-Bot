"""Bounded workers for owner and delegated guild-configuration drafts."""

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
from modules import guild_configuration_delegation_storage as delegation
from modules import guild_configuration_runtime as runtime
from modules import guild_configuration_shadow as shadow
from modules import guild_configuration_storage as storage
from modules import guild_types
from modules.guild_configuration_schema import (
    GuildConfigurationDocument,
    GuildConfigurationError,
    document_digest,
    document_to_mapping,
    validate_document,
)


SHOW = 'show'
RESET = 'reset'
REPLACE = 'replace'
DISCARD = 'discard'
VALIDATE = 'validate'
ACTIVATE = 'activate'
ACTIVATE_COMMANDS = 'activate_commands'
ROLLBACK_PREVIEW = 'rollback_preview'
ROLLBACK_COMMIT = 'rollback_commit'
OPERATIONS = frozenset({
    SHOW, RESET, REPLACE, DISCARD, VALIDATE, ACTIVATE, ACTIVATE_COMMANDS,
    ROLLBACK_PREVIEW, ROLLBACK_COMMIT,
})
WRITE_OPERATIONS = frozenset({
    RESET, REPLACE, DISCARD, ACTIVATE, ACTIVATE_COMMANDS, ROLLBACK_COMMIT,
})
_HEX_DIGEST = re.compile(r'^[0-9a-f]{64}$')
ORDINARY_CHANGED_PATHS = frozenset({
    'identity.display_name',
    'identity.command_prefix',
    'teams.allow_uneven_teams',
    'teams.max_team_size',
    'channels.bot_channel_ids',
    'channels.strict_bot_channel_ids',
    'channels.newbie_message_channel_ids',
    'channels.match_challenge_channel_ids',
    'channels.game_category_ids',
    'channels.ranked_game_channel_id',
    'channels.unranked_game_channel_id',
    'channels.steam_game_channel_id',
    'channels.game_announce_channel_id',
    'channels.staff_help_channel_id',
})


class OperatorGuildConfigurationDraftError(RuntimeError):
    """One safe owner draft operation could not complete."""


class OperatorGuildConfigurationDraftPermissionError(
    OperatorGuildConfigurationDraftError,
):
    """The requester is not the configured owner."""


class OperatorGuildConfigurationDraftUnavailable(
    OperatorGuildConfigurationDraftError,
):
    """The exact runtime draft store is unavailable."""


class OperatorGuildConfigurationDraftConflict(
    OperatorGuildConfigurationDraftError,
):
    """The draft or its active base changed."""


class OperatorGuildConfigurationDraftValidationError(
    OperatorGuildConfigurationDraftError,
):
    """A request, document, schema, or live reference is invalid."""


class OperatorGuildConfigurationActivationCommitted(
    OperatorGuildConfigurationDraftError,
):
    """Activation committed, but the running immutable snapshot was not replaced."""

    def __init__(self, activation: drafts.GuildConfigurationActivation):
        self.activation = activation
        super().__init__(
            f'Configuration r{activation.revision}/g{activation.generation} '
            'committed, but runtime publication could not be verified. The '
            'database is authoritative; use `/operator bot restart` to reconcile.'
        )


class OperatorGuildConfigurationRollbackCommitted(
    OperatorGuildConfigurationDraftError,
):
    """Rollback committed, but the running immutable snapshot was not replaced."""

    def __init__(self, rollback: drafts.GuildConfigurationRollback):
        self.rollback = rollback
        super().__init__(
            f'Configuration rollback r{rollback.revision}/g{rollback.generation} '
            'committed, but runtime publication could not be verified. The '
            'database is authoritative; use `/operator bot restart` to reconcile.'
        )


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
    invoking_guild_id: int | None = None
    requester_role_ids: tuple[int, ...] = ()
    requester_is_guild_owner: bool = False
    expected_draft_version: int | None = None
    expected_draft_digest: str | None = None
    replacement_document_json: str | None = field(default=None, repr=False)
    discord_snapshot_json: str | None = field(default=None, repr=False)
    target_revision: int | None = None
    expected_target_digest: str | None = None
    expected_active_revision: int | None = None
    expected_active_generation: int | None = None
    expected_active_digest: str | None = None
    confirmation_text: str | None = field(default=None, repr=False)
    command_plan_digest: str | None = None


@dataclass(frozen=True)
class GuildConfigurationDraftValidation:
    base_revision_current: bool
    document_valid: bool
    live_references_valid: bool
    runtime_snapshot_current: bool


@dataclass(frozen=True)
class GuildConfigurationRollbackPreview:
    guild_id: int
    active_revision: int
    active_generation: int
    active_document_digest: str
    source_revision: int
    source_document_digest: str
    changed_paths: tuple[str, ...]
    source_document: GuildConfigurationDocument = field(repr=False)

    @property
    def confirmation(self) -> str:
        return f'ROLLBACK {self.source_revision} {self.source_document_digest}'


@dataclass(frozen=True)
class GuildConfigurationDraftResult:
    operation: str
    guild_id: int
    active_revision: int
    active_generation: int
    active_document_digest: str
    draft: drafts.StoredGuildConfigurationDraft | None
    validation: GuildConfigurationDraftValidation | None = None
    activation: drafts.GuildConfigurationActivation | None = None
    rollback_preview: GuildConfigurationRollbackPreview | None = None
    rollback: drafts.GuildConfigurationRollback | None = None
    runtime_snapshot: runtime.GuildConfigurationRuntimeSnapshot | None = field(
        default=None,
        repr=False,
    )
    runtime_published: bool = False
    committed: bool = False
    delegated: bool = False
    activation_allowed: bool = True


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


def _validate_request(
    request: GuildConfigurationDraftRequest,
) -> GuildConfigurationDraftRequest:
    if not isinstance(request, GuildConfigurationDraftRequest):
        raise OperatorGuildConfigurationDraftValidationError(
            'A frozen guild-configuration draft request is required.'
        )
    if request.operation not in OPERATIONS:
        raise OperatorGuildConfigurationDraftValidationError(
            'The guild-configuration draft operation is invalid.'
        )
    _strict_positive(request.guild_id, 'Guild ID')
    owner = int(request.requester_id) == int(settings.owner_id)
    if request.invoking_guild_id is not None:
        _strict_positive(request.invoking_guild_id, 'Invoking guild ID')
    roles = tuple(request.requester_role_ids)
    if not isinstance(request.requester_is_guild_owner, bool):
        raise OperatorGuildConfigurationDraftValidationError(
            'The requester guild-owner evidence is invalid.'
        )
    if (
            any(
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
                for value in roles
            )
            or roles != tuple(sorted(set(roles)))
    ):
        raise OperatorGuildConfigurationDraftValidationError(
            'The requester role snapshot is invalid.'
        )
    if not owner and (
            request.invoking_guild_id != request.guild_id
            or (not roles and not request.requester_is_guild_owner)
            or request.operation in {
                ACTIVATE_COMMANDS, ROLLBACK_PREVIEW, ROLLBACK_COMMIT,
            }
    ):
        raise OperatorGuildConfigurationDraftPermissionError(
            'Only the configured bot owner can use this configuration operation.'
        )
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
            'The current guild is outside the exact runtime allowlist.'
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
    if request.operation not in {ROLLBACK_PREVIEW, ROLLBACK_COMMIT} and any(
            value is not None for value in (
                request.target_revision,
                request.expected_target_digest,
                request.expected_active_revision,
                request.expected_active_generation,
                request.expected_active_digest,
            )
    ):
        raise OperatorGuildConfigurationDraftValidationError(
            'Rollback evidence is accepted only by rollback operations.'
        )
    if (
            request.operation not in {
                ROLLBACK_PREVIEW, ROLLBACK_COMMIT, ACTIVATE_COMMANDS,
            }
            and request.confirmation_text is not None
    ):
        raise OperatorGuildConfigurationDraftValidationError(
            'Confirmation text is not accepted by this operation.'
        )
    if (
            request.operation != ACTIVATE_COMMANDS
            and request.command_plan_digest is not None
    ):
        raise OperatorGuildConfigurationDraftValidationError(
            'Command-plan evidence is accepted only by coordinated activation.'
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
    elif request.operation in {DISCARD, ACTIVATE, ACTIVATE_COMMANDS}:
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
        if request.operation == ACTIVATE_COMMANDS:
            if (
                    not isinstance(request.command_plan_digest, str)
                    or not _HEX_DIGEST.fullmatch(request.command_plan_digest)
            ):
                raise OperatorGuildConfigurationDraftValidationError(
                    'A full command-plan digest is required.'
                )
            expected = (
                f'ACTIVATE COMMANDS {request.expected_draft_digest} '
                f'{request.command_plan_digest}'
            )
            if request.confirmation_text != expected:
                raise OperatorGuildConfigurationDraftValidationError(
                    f'Command-capability activation requires exact confirmation '
                    f'{expected!r}.'
                )
    elif request.operation in {ROLLBACK_PREVIEW, ROLLBACK_COMMIT}:
        _strict_positive(request.target_revision, 'Rollback source revision')
        if request.operation == ROLLBACK_PREVIEW:
            if any(value is not None for value in (
                request.expected_target_digest,
                request.expected_active_revision,
                request.expected_active_generation,
                request.expected_active_digest,
                request.confirmation_text,
            )):
                raise OperatorGuildConfigurationDraftValidationError(
                    'Rollback preview does not accept commit evidence.'
                )
        else:
            _strict_positive(
                request.expected_active_revision,
                'Expected active revision',
            )
            _strict_positive(
                request.expected_active_generation,
                'Expected active generation',
            )
            if (
                    not isinstance(request.expected_target_digest, str)
                    or not _HEX_DIGEST.fullmatch(request.expected_target_digest)
                    or not isinstance(request.expected_active_digest, str)
                    or not _HEX_DIGEST.fullmatch(request.expected_active_digest)
            ):
                raise OperatorGuildConfigurationDraftValidationError(
                    'The expected rollback evidence is invalid.'
                )
            if (
                    request.expected_active_revision != request.runtime_revision
                    or request.expected_active_generation != request.runtime_generation
                    or request.expected_active_digest
                    != request.runtime_document_digest
            ):
                raise OperatorGuildConfigurationDraftConflict(
                    'The active configuration changed after rollback preview; '
                    'open a fresh preview.'
                )
            expected = (
                f'ROLLBACK {request.target_revision} '
                f'{request.expected_target_digest}'
            )
            if request.confirmation_text != expected:
                raise OperatorGuildConfigurationDraftValidationError(
                    f'Rollback requires exact confirmation {expected!r}.'
                )
        if any(value is not None for value in (
            request.expected_draft_version,
            request.expected_draft_digest,
            request.replacement_document_json,
        )):
            raise OperatorGuildConfigurationDraftValidationError(
                'Rollback does not accept draft evidence.'
            )
    elif any(value is not None for value in (
        request.expected_draft_version,
        request.expected_draft_digest,
        request.replacement_document_json,
        request.target_revision,
        request.expected_target_digest,
        request.expected_active_revision,
        request.expected_active_generation,
        request.expected_active_digest,
        request.confirmation_text,
        request.command_plan_digest,
    )):
        raise OperatorGuildConfigurationDraftValidationError(
            'Optimistic draft evidence is accepted only by edit, discard, or '
            'activation.'
        )
    if request.operation in {
        VALIDATE, ACTIVATE, ACTIVATE_COMMANDS, ROLLBACK_PREVIEW, ROLLBACK_COMMIT,
    }:
        if not request.discord_snapshot_json:
            raise OperatorGuildConfigurationDraftValidationError(
                'Live Discord identity is required for draft validation.'
            )
    elif request.discord_snapshot_json is not None:
        raise OperatorGuildConfigurationDraftValidationError(
            'Live Discord identity is accepted only by validation or activation.'
        )
    return request


def request_from_profile(
    *,
    profile: Any,
    requester_id: int,
    guild_id: int,
    operation: str,
    runtime_record: Any,
    invoking_guild_id: int | None = None,
    requester_role_ids: Sequence[int] = (),
    requester_is_guild_owner: bool = False,
    expected_draft_version: int | None = None,
    expected_draft_digest: str | None = None,
    replacement_document: Mapping[str, Any] | None = None,
    discord_snapshot: Mapping[str, Any] | None = None,
    target_revision: int | None = None,
    expected_target_digest: str | None = None,
    expected_active_revision: int | None = None,
    expected_active_generation: int | None = None,
    expected_active_digest: str | None = None,
    confirmation_text: str | None = None,
    command_plan_digest: str | None = None,
    runtime_guild_ids: Sequence[int] | None = None,
) -> GuildConfigurationDraftRequest:
    if (
            getattr(profile, 'environment', None) not in {
                storage.DEVELOPMENT_ENVIRONMENT,
                storage.PRODUCTION_ENVIRONMENT,
            }
            or getattr(profile, 'guild_configuration_source', None) != 'database'
    ):
        raise OperatorGuildConfigurationDraftValidationError(
            'Guild configuration drafts require database authority.'
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

    allowed_source = (
        profile.allowed_guild_ids
        if runtime_guild_ids is None else runtime_guild_ids
    )
    request = GuildConfigurationDraftRequest(
        operation=str(operation),
        requester_id=int(requester_id),
        guild_id=int(guild_id),
        target=target,
        allowed_guild_ids=tuple(sorted(int(value) for value in allowed_source)),
        runtime_revision=int(runtime_record.revision),
        runtime_generation=int(runtime_record.generation),
        runtime_document_digest=str(runtime_record.document_digest),
        invoking_guild_id=(
            None if invoking_guild_id is None else int(invoking_guild_id)
        ),
        requester_role_ids=tuple(sorted(int(value) for value in requester_role_ids)),
        requester_is_guild_owner=requester_is_guild_owner,
        database_password=profile.database_password,
        database_host=profile.database_host,
        database_port=profile.database_port,
        expected_draft_version=expected_draft_version,
        expected_draft_digest=expected_draft_digest,
        replacement_document_json=freeze(replacement_document),
        discord_snapshot_json=freeze(discord_snapshot),
        target_revision=target_revision,
        expected_target_digest=expected_target_digest,
        expected_active_revision=expected_active_revision,
        expected_active_generation=expected_active_generation,
        expected_active_digest=expected_active_digest,
        confirmation_text=confirmation_text,
        command_plan_digest=command_plan_digest,
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
        if not delegation.validate_delegation_schema(
                delegation.inspect_delegation_schema(cursor)):
            raise delegation.GuildConfigurationDelegationStorageError(
                'Guild-configuration delegation storage is absent.'
            )
    except (
        storage.GuildConfigurationStorageError,
        drafts.GuildConfigurationDraftStorageError,
        delegation.GuildConfigurationDelegationStorageError,
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


def _live_validate_document(
    request: GuildConfigurationDraftRequest,
    document: GuildConfigurationDocument,
) -> None:
    try:
        snapshot_value = json.loads(request.discord_snapshot_json or '')
        snapshots = storage.validate_discord_snapshot(
            snapshot_value,
            target=request.target,
            allowed_guild_ids=request.allowed_guild_ids,
        )
        storage.validate_document_references(
            document,
            snapshots[document.guild_id],
        )
    except (
        json.JSONDecodeError,
        KeyError,
        storage.GuildConfigurationStorageError,
    ) as exc:
        raise OperatorGuildConfigurationDraftValidationError(
            'The draft does not validate against current Discord roles and channels.'
        ) from exc


def _live_validate(
    request: GuildConfigurationDraftRequest,
    draft: drafts.StoredGuildConfigurationDraft,
) -> None:
    _live_validate_document(request, draft.document)


def _changed_paths(
    active: GuildConfigurationDocument,
    candidate: GuildConfigurationDocument,
) -> tuple[str, ...]:
    def difference(expected: Any, observed: Any, prefix: str = '') -> list[str]:
        if isinstance(expected, Mapping) and isinstance(observed, Mapping):
            paths: list[str] = []
            for key in sorted(set(expected) | set(observed), key=str):
                path = f'{prefix}.{key}' if prefix else str(key)
                if key not in expected or key not in observed:
                    paths.append(path)
                else:
                    paths.extend(difference(expected[key], observed[key], path))
            return paths
        return [prefix] if expected != observed else []

    return tuple(difference(
        document_to_mapping(active),
        document_to_mapping(candidate),
    ))


def _delegated_authority(
    cursor: Any,
    request: GuildConfigurationDraftRequest,
) -> tuple[bool, bool]:
    if int(request.requester_id) == int(settings.owner_id):
        return False, True
    if request.requester_is_guild_owner:
        return True, True
    try:
        policy = delegation.select_delegation(
            cursor, request.guild_id, for_update=False,
        )
    except delegation.GuildConfigurationDelegationStorageError as exc:
        raise OperatorGuildConfigurationDraftPermissionError(
            'Configuration delegation could not be verified.'
        ) from exc
    if (
            policy is None
            or not policy.enabled
            or not set(request.requester_role_ids).intersection(
                policy.manager_role_ids
            )
    ):
        raise OperatorGuildConfigurationDraftPermissionError(
            'You do not currently hold a configured guild-manager role.'
        )
    return True, policy.allow_activation


def _require_ordinary(
    active: GuildConfigurationDocument,
    candidate: GuildConfigurationDocument,
) -> None:
    forbidden = [
        path for path in _changed_paths(active, candidate)
        if path not in ORDINARY_CHANGED_PATHS
    ]
    if 'command_capabilities' in forbidden:
        expected = guild_types.capabilities_for_type(
            guild_types.guild_type_for_document(active),
            staff_help_enabled=(
                candidate.channels.staff_help_channel_id is not None
            ),
            existing_capabilities=active.command_capabilities,
        )
        if candidate.command_capabilities == expected:
            forbidden.remove('command_capabilities')
    if forbidden:
        raise OperatorGuildConfigurationDraftPermissionError(
            'This draft includes owner-only configuration. Ask the bot owner '
            'to finish or reset it before delegated editing.'
        )


def _post_commit_runtime_snapshot(
    request: GuildConfigurationDraftRequest,
) -> runtime.GuildConfigurationRuntimeSnapshot:
    try:
        discord_snapshot = json.loads(request.discord_snapshot_json or '')
        active = shadow.inspect_active_configuration(
            shadow.ActiveConfigurationReadRequest(
                target=request.target,
                allowed_guild_ids=request.allowed_guild_ids,
                database_password=request.database_password,
                database_host=request.database_host,
                database_port=request.database_port,
                include_all_active=True,
            )
        )
        active_ids = tuple(value.guild_id for value in active)
        return runtime.build_runtime_snapshot_from_stored(
            stored_configurations=active,
            discord_snapshot=discord_snapshot,
            allowed_guild_ids=active_ids,
            target=request.target,
        )
    except (
        json.JSONDecodeError,
        shadow.GuildConfigurationShadowError,
        runtime.GuildConfigurationRuntimeError,
    ) as exc:
        raise OperatorGuildConfigurationDraftValidationError(
            'The committed active graph could not be loaded for publication.'
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
            'The guild-configuration database is unavailable.'
        ) from exc
    committed = False
    activation = None
    rollback = None
    rollback_preview = None
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
            delegated, activation_allowed = _delegated_authority(
                cursor, request,
            )
            actor = f'discord:{request.requester_id}'
            validation = None
            if request.operation in {ROLLBACK_PREVIEW, ROLLBACK_COMMIT}:
                assert request.target_revision is not None
                if request.target_revision >= active_revision:
                    raise OperatorGuildConfigurationDraftValidationError(
                        'Rollback requires an earlier same-guild revision.'
                    )
                try:
                    source_document, source_digest = drafts.select_revision(
                        cursor,
                        request.guild_id,
                        request.target_revision,
                    )
                except drafts.GuildConfigurationDraftStorageError as exc:
                    raise OperatorGuildConfigurationDraftValidationError(
                        str(exc)
                    ) from exc
                if (
                        request.operation == ROLLBACK_COMMIT
                        and source_digest != request.expected_target_digest
                ):
                    raise OperatorGuildConfigurationDraftConflict(
                        'The selected rollback revision digest changed.'
                    )
                _live_validate_document(request, source_document)
                if (
                        source_document.command_capabilities
                        != active_document.command_capabilities
                ):
                    raise OperatorGuildConfigurationDraftValidationError(
                        'This historical revision has different command '
                        'capabilities and cannot be rolled back until command '
                        'deployment is coordinated.'
                    )
                changed_paths = _changed_paths(active_document, source_document)
                if not changed_paths:
                    raise OperatorGuildConfigurationDraftValidationError(
                        'The selected historical document is identical to the '
                        'active configuration; there is nothing to roll back.'
                    )
                rollback_preview = GuildConfigurationRollbackPreview(
                    guild_id=request.guild_id,
                    active_revision=active_revision,
                    active_generation=active_generation,
                    active_document_digest=active_digest,
                    source_revision=request.target_revision,
                    source_document_digest=source_digest,
                    changed_paths=changed_paths,
                    source_document=source_document,
                )
                if request.operation == ROLLBACK_COMMIT:
                    try:
                        rollback = drafts.rollback_to_revision(
                            cursor,
                            guild_id=request.guild_id,
                            active_revision=active_revision,
                            active_generation=active_generation,
                            active_document_digest=active_digest,
                            source_revision=request.target_revision,
                            source_document=source_document,
                            source_document_digest=source_digest,
                            actor=actor,
                            changed_paths=changed_paths,
                        )
                    except drafts.GuildConfigurationDraftStorageError as exc:
                        raise OperatorGuildConfigurationDraftConflict(
                            str(exc)
                        ) from exc
            elif request.operation == RESET:
                if delegated:
                    existing = drafts.select_draft(
                        cursor,
                        request.guild_id,
                        active_only=True,
                        for_update=True,
                    )
                    if existing is not None:
                        _require_ordinary(active_document, existing.document)
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
                if request.operation in {
                    REPLACE, DISCARD, VALIDATE, ACTIVATE, ACTIVATE_COMMANDS,
                } and draft is None:
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
                if delegated and draft is not None:
                    _require_ordinary(active_document, draft.document)
                if request.operation == REPLACE:
                    assert replacement is not None
                    if delegated:
                        _require_ordinary(active_document, replacement)
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
                elif request.operation in {ACTIVATE, ACTIVATE_COMMANDS}:
                    assert draft is not None
                    if delegated and not activation_allowed:
                        raise OperatorGuildConfigurationDraftPermissionError(
                            'The owner delegated editing and validation, but kept '
                            'activation owner-only.'
                        )
                    _live_validate(request, draft)
                    capabilities_changed = (
                        draft.document.command_capabilities
                        != active_document.command_capabilities
                    )
                    if request.operation == ACTIVATE_COMMANDS and not capabilities_changed:
                        raise OperatorGuildConfigurationDraftValidationError(
                            'The draft does not change command capabilities; use '
                            'ordinary activation.'
                        )
                    changed_paths = _changed_paths(
                        active_document,
                        draft.document,
                    )
                    if not changed_paths:
                        raise OperatorGuildConfigurationDraftValidationError(
                            'The draft is identical to the active configuration; '
                            'there is nothing to activate.'
                        )
                    try:
                        activation = drafts.activate_draft(
                            cursor,
                            draft=draft,
                            active_revision=active_revision,
                            active_generation=active_generation,
                            active_document_digest=active_digest,
                            actor=actor,
                            changed_paths=changed_paths,
                            command_plan_digest=request.command_plan_digest,
                        )
                    except drafts.GuildConfigurationDraftStorageError as exc:
                        raise OperatorGuildConfigurationDraftConflict(str(exc)) from exc
                    draft = None
            if request.operation in WRITE_OPERATIONS:
                connection.commit()
                committed = True
            runtime_snapshot = None
            committed_change = activation or rollback
            if committed_change is not None:
                try:
                    runtime_snapshot = _post_commit_runtime_snapshot(request)
                    published = runtime_snapshot.guilds[request.guild_id]
                    if (
                            published.revision != committed_change.revision
                            or published.generation != committed_change.generation
                            or published.document_digest != committed_change.document_digest
                    ):
                        raise OperatorGuildConfigurationDraftValidationError(
                            'The committed revision was not present in the reloaded graph.'
                        )
                except Exception as exc:
                    if rollback is not None:
                        raise OperatorGuildConfigurationRollbackCommitted(
                            rollback
                        ) from exc
                    assert activation is not None
                    raise OperatorGuildConfigurationActivationCommitted(activation) from exc
            return GuildConfigurationDraftResult(
                operation=request.operation,
                guild_id=request.guild_id,
                active_revision=(
                    committed_change.revision
                    if committed_change is not None else active_revision
                ),
                active_generation=(
                    committed_change.generation
                    if committed_change is not None else active_generation
                ),
                active_document_digest=(
                    committed_change.document_digest
                    if committed_change is not None else active_digest
                ),
                draft=(None if request.operation in {
                    ROLLBACK_PREVIEW, ROLLBACK_COMMIT,
                } else draft),
                validation=validation,
                activation=activation,
                rollback_preview=rollback_preview,
                rollback=rollback,
                runtime_snapshot=runtime_snapshot,
                committed=committed,
                delegated=delegated,
                activation_allowed=activation_allowed,
            )
    except psycopg2.OperationalError as exc:
        raise OperatorGuildConfigurationDraftUnavailable(
            'The guild-configuration draft operation was interrupted.'
        ) from exc
    except psycopg2.Error as exc:
        raise OperatorGuildConfigurationDraftValidationError(
            'The guild-configuration draft transaction was invalid.'
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
            result = future.result()
        except (
            OperatorGuildConfigurationActivationCommitted,
            OperatorGuildConfigurationRollbackCommitted,
        ):
            raise
        except BaseException:
            raise cancellation
        if (
                isinstance(result, GuildConfigurationDraftResult)
                and result.rollback is not None
        ):
            raise OperatorGuildConfigurationRollbackCommitted(result.rollback)
        if (
                isinstance(result, GuildConfigurationDraftResult)
                and result.activation is not None
        ):
            raise OperatorGuildConfigurationActivationCommitted(result.activation)
        raise cancellation
    return future.result()


async def run_draft_operation(
    request: GuildConfigurationDraftRequest,
) -> GuildConfigurationDraftResult:
    request = _validate_request(request)
    future = _executor.submit(execute_draft_operation, request)
    return await _drain_future(future)


__all__ = [
    'ACTIVATE',
    'ACTIVATE_COMMANDS',
    'DISCARD',
    'GuildConfigurationDraftRequest',
    'GuildConfigurationDraftResult',
    'GuildConfigurationDraftValidation',
    'GuildConfigurationRollbackPreview',
    'OPERATIONS',
    'OperatorGuildConfigurationDraftConflict',
    'OperatorGuildConfigurationDraftError',
    'OperatorGuildConfigurationDraftPermissionError',
    'OperatorGuildConfigurationDraftUnavailable',
    'OperatorGuildConfigurationDraftValidationError',
    'OperatorGuildConfigurationActivationCommitted',
    'OperatorGuildConfigurationRollbackCommitted',
    'REPLACE',
    'RESET',
    'ROLLBACK_COMMIT',
    'ROLLBACK_PREVIEW',
    'SHOW',
    'VALIDATE',
    'WRITE_OPERATIONS',
    'execute_draft_operation',
    'request_from_profile',
    'run_draft_operation',
]
