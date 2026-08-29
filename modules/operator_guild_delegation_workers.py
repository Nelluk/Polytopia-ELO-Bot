"""Bounded owner workers for guild configuration delegation policy."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
import re
from typing import Any

import psycopg2

import settings
from runtime_config import database_authentication_is_supported
from modules import guild_configuration_delegation_storage as delegation
from modules import guild_configuration_shadow as shadow
from modules import guild_configuration_storage as storage


SHOW = 'show'
APPLY = 'apply'
OPERATIONS = frozenset({SHOW, APPLY})
_HEX_DIGEST = re.compile(r'^[0-9a-f]{64}$')


class OperatorGuildDelegationError(RuntimeError):
    """A delegation operation could not complete safely."""


class OperatorGuildDelegationPermissionError(OperatorGuildDelegationError):
    """The requester is not the configured bot owner."""


class OperatorGuildDelegationValidationError(OperatorGuildDelegationError):
    """The request, schema, policy, or Discord role evidence is invalid."""


class OperatorGuildDelegationUnavailable(OperatorGuildDelegationError):
    """The exact runtime database is unavailable."""


@dataclass(frozen=True)
class DiscordRoleEvidence:
    role_id: int
    managed: bool
    everyone: bool


@dataclass(frozen=True)
class GuildDelegationRequest:
    operation: str
    requester_id: int
    guild_id: int
    target: storage.StorageTarget
    allowed_guild_ids: tuple[int, ...]
    role_evidence: tuple[DiscordRoleEvidence, ...]
    database_password: str = field(repr=False)
    database_host: str | None = None
    database_port: int | None = None
    expected_policy_version: int | None = None
    manager_role_ids: tuple[int, ...] | None = None
    allow_activation: bool | None = None
    expected_plan_digest: str | None = None
    confirmation_text: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class GuildDelegationResult:
    operation: str
    guild_id: int
    policy: delegation.GuildConfigurationDelegation | None
    plan_digest: str
    committed: bool = False

    @property
    def confirmation(self) -> str:
        return f'DELEGATE {self.guild_id} {self.plan_digest}'


_executor = ThreadPoolExecutor(
    max_workers=1, thread_name_prefix='polybot-operator-guild-delegation',
)


def _validate_request(request: GuildDelegationRequest) -> GuildDelegationRequest:
    if not isinstance(request, GuildDelegationRequest):
        raise OperatorGuildDelegationValidationError(
            'A frozen guild delegation request is required.'
        )
    if int(request.requester_id) != int(settings.owner_id):
        raise OperatorGuildDelegationPermissionError(
            'Only the configured bot owner can grant configuration delegation.'
        )
    if request.operation not in OPERATIONS:
        raise OperatorGuildDelegationValidationError(
            'The guild delegation operation is invalid.'
        )
    try:
        storage.validate_target(request.target)
    except storage.GuildConfigurationStorageError as exc:
        raise OperatorGuildDelegationValidationError(
            'The guild delegation database target is invalid.'
        ) from exc
    if (
            isinstance(request.guild_id, bool)
            or request.guild_id <= 0
            or request.guild_id not in request.allowed_guild_ids
    ):
        raise OperatorGuildDelegationValidationError(
            'Delegation requires an active guild.'
        )
    evidence_ids = tuple(value.role_id for value in request.role_evidence)
    if (
            any(
                not isinstance(value, DiscordRoleEvidence)
                or isinstance(value.role_id, bool)
                or value.role_id <= 0
                or not isinstance(value.managed, bool)
                or not isinstance(value.everyone, bool)
                for value in request.role_evidence
            )
            or evidence_ids != tuple(sorted(set(evidence_ids)))
    ):
        raise OperatorGuildDelegationValidationError(
            'The current Discord role snapshot is invalid.'
        )
    if not database_authentication_is_supported(
            environment=request.target.environment,
            database_password=request.database_password,
            database_host=request.database_host,
    ):
        raise OperatorGuildDelegationValidationError(
            'Database authentication is unavailable.'
        )
    if request.operation == SHOW:
        if any(value is not None for value in (
            request.expected_policy_version,
            request.manager_role_ids,
            request.allow_activation,
            request.expected_plan_digest,
            request.confirmation_text,
        )):
            raise OperatorGuildDelegationValidationError(
                'Delegation inspection does not accept mutation evidence.'
            )
    else:
        if request.manager_role_ids is None or request.allow_activation is None:
            raise OperatorGuildDelegationValidationError(
                'A complete replacement delegation policy is required.'
            )
        try:
            roles = delegation.normalize_manager_role_ids(request.manager_role_ids)
            digest = delegation.policy_digest(
                guild_id=request.guild_id,
                expected_version=request.expected_policy_version,
                manager_role_ids=roles,
                allow_activation=request.allow_activation,
            )
        except delegation.GuildConfigurationDelegationStorageError as exc:
            raise OperatorGuildDelegationValidationError(str(exc)) from exc
        by_id = {value.role_id: value for value in request.role_evidence}
        invalid = tuple(
            role_id for role_id in roles
            if role_id not in by_id
            or by_id[role_id].managed
            or by_id[role_id].everyone
        )
        if invalid:
            raise OperatorGuildDelegationValidationError(
                'Manager roles must still exist in this guild and cannot be '
                '`@everyone` or Discord-managed roles.'
            )
        if (
                request.expected_plan_digest != digest
                or request.confirmation_text
                != f'DELEGATE {request.guild_id} {digest}'
        ):
            raise OperatorGuildDelegationValidationError(
                'Delegation apply requires the exact current plan confirmation.'
            )
    return request


def request_from_profile(
    *,
    profile: Any,
    requester_id: int,
    guild_id: int,
    operation: str,
    role_evidence: tuple[DiscordRoleEvidence, ...],
    runtime_guild_ids: tuple[int, ...],
    expected_policy_version: int | None = None,
    manager_role_ids: tuple[int, ...] | None = None,
    allow_activation: bool | None = None,
    expected_plan_digest: str | None = None,
    confirmation_text: str | None = None,
) -> GuildDelegationRequest:
    if (
            getattr(profile, 'environment', None) not in {
                storage.DEVELOPMENT_ENVIRONMENT,
                storage.PRODUCTION_ENVIRONMENT,
            }
            or getattr(profile, 'guild_configuration_source', None) != 'database'
    ):
        raise OperatorGuildDelegationValidationError(
            'Guild delegation requires database authority.'
        )
    try:
        target = shadow.target_from_profile(profile)
    except shadow.GuildConfigurationShadowError as exc:
        raise OperatorGuildDelegationValidationError(
            'The guild delegation database target is invalid.'
        ) from exc
    return _validate_request(GuildDelegationRequest(
        operation=str(operation),
        requester_id=int(requester_id),
        guild_id=int(guild_id),
        target=target,
        allowed_guild_ids=tuple(sorted(int(value) for value in runtime_guild_ids)),
        role_evidence=tuple(sorted(role_evidence, key=lambda value: value.role_id)),
        database_password=profile.database_password,
        database_host=profile.database_host,
        database_port=profile.database_port,
        expected_policy_version=expected_policy_version,
        manager_role_ids=manager_role_ids,
        allow_activation=allow_activation,
        expected_plan_digest=expected_plan_digest,
        confirmation_text=confirmation_text,
    ))


def _connect(request: GuildDelegationRequest):
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


def execute_delegation(request: GuildDelegationRequest) -> GuildDelegationResult:
    request = _validate_request(request)
    try:
        connection = _connect(request)
    except psycopg2.Error as exc:
        raise OperatorGuildDelegationUnavailable(
            'The guild delegation database is unavailable.'
        ) from exc
    try:
        readonly = request.operation == SHOW
        connection.set_session(
            readonly=readonly, autocommit=False, isolation_level='REPEATABLE READ',
        )
        with connection.cursor() as cursor:
            cursor.execute('SHOW transaction_read_only')
            actual_readonly = str(cursor.fetchone()[0]).casefold() == 'on'
            if actual_readonly != readonly:
                raise OperatorGuildDelegationValidationError(
                    'The guild delegation transaction mode is invalid.'
                )
            delegation._validate_live_connection(cursor, request.target)
            if not delegation.validate_delegation_schema(
                    delegation.inspect_delegation_schema(cursor)):
                raise OperatorGuildDelegationValidationError(
                    'Guild delegation storage is absent.'
                )
            current = delegation.select_delegation(
                cursor, request.guild_id, for_update=not readonly,
            )
            if request.operation == SHOW:
                roles = () if current is None else current.manager_role_ids
                activation = False if current is None else current.allow_activation
                digest = delegation.policy_digest(
                    guild_id=request.guild_id,
                    expected_version=(
                        None if current is None else current.policy_version
                    ),
                    manager_role_ids=roles,
                    allow_activation=activation,
                )
                return GuildDelegationResult(SHOW, request.guild_id, current, digest)
            roles = delegation.normalize_manager_role_ids(
                request.manager_role_ids or (),
            )
            digest = delegation.policy_digest(
                guild_id=request.guild_id,
                expected_version=request.expected_policy_version,
                manager_role_ids=roles,
                allow_activation=bool(request.allow_activation),
            )
            if digest != request.expected_plan_digest:
                raise OperatorGuildDelegationValidationError(
                    'The delegation plan evidence is invalid.'
                )
            if current is not None and (
                    current.manager_role_ids == roles
                    and current.allow_activation == request.allow_activation
            ):
                raise OperatorGuildDelegationValidationError(
                    'The delegation policy is unchanged; no write is needed.'
                )
            try:
                policy = delegation.put_delegation(
                    cursor,
                    guild_id=request.guild_id,
                    expected_version=request.expected_policy_version,
                    manager_role_ids=roles,
                    allow_activation=bool(request.allow_activation),
                    actor=f'discord:{request.requester_id}',
                )
            except delegation.GuildConfigurationDelegationStorageError as exc:
                raise OperatorGuildDelegationValidationError(str(exc)) from exc
        connection.commit()
        return GuildDelegationResult(APPLY, request.guild_id, policy, digest, True)
    except psycopg2.OperationalError as exc:
        raise OperatorGuildDelegationUnavailable(
            'The guild delegation operation was interrupted.'
        ) from exc
    except (
        storage.GuildConfigurationStorageError,
        delegation.GuildConfigurationDelegationStorageError,
    ) as exc:
        raise OperatorGuildDelegationValidationError(str(exc)) from exc
    except psycopg2.Error as exc:
        raise OperatorGuildDelegationValidationError(
            'The guild delegation transaction was invalid.'
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
        except Exception:
            pass
        raise cancellation
    return future.result()


async def run_delegation(
    request: GuildDelegationRequest,
) -> GuildDelegationResult:
    future = _executor.submit(execute_delegation, request)
    return await _drain_future(future)


__all__ = [
    'APPLY', 'SHOW', 'DiscordRoleEvidence', 'GuildDelegationRequest',
    'GuildDelegationResult', 'OperatorGuildDelegationError',
    'OperatorGuildDelegationPermissionError',
    'OperatorGuildDelegationUnavailable',
    'OperatorGuildDelegationValidationError', 'execute_delegation',
    'request_from_profile', 'run_delegation',
]
