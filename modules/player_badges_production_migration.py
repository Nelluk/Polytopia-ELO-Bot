"""Fail-closed production tooling for the additive player badges column.

This module is model-free and never opens a connection itself. The CLI owns
connection creation only after validating the fixed production policy. The
shared schema module remains development-gated for its own CLI; this wrapper
reuses only its exact column metadata and additive plan contract.
"""

from __future__ import annotations

from dataclasses import dataclass

from modules import player_badges_migration as schema


PRODUCTION_ENVIRONMENT = 'production'
PRODUCTION_DATABASE = 'polytopia2'
PRODUCTION_APPLY_CONFIRMATION = 'P12.1-PRODUCTION-PLAYER-BADGES-APPLY'

MigrationSafetyError = schema.MigrationSafetyError
MigrationPlan = schema.MigrationPlan


@dataclass(frozen=True, slots=True)
class MigrationPolicy:
    environment: str
    database_name: str
    apply_confirmation: str


@dataclass(frozen=True, slots=True)
class MigrationTarget:
    environment: str
    database_name: str
    database_user: str


PRODUCTION_POLICY = MigrationPolicy(
    environment=PRODUCTION_ENVIRONMENT,
    database_name=PRODUCTION_DATABASE,
    apply_confirmation=PRODUCTION_APPLY_CONFIRMATION,
)


def validate_target(target: MigrationTarget, *, policy: MigrationPolicy) -> None:
    if target.environment != policy.environment:
        raise MigrationSafetyError(
            f'Migration requires environment {policy.environment!r}; '
            f'configured environment was {target.environment!r}.'
        )
    if target.database_name != policy.database_name:
        raise MigrationSafetyError(
            f'Migration requires database {policy.database_name!r}; '
            f'configured database was {target.database_name!r}.'
        )
    if not target.database_user.strip():
        raise MigrationSafetyError(
            'The configured database role is empty; refusing migration.'
        )


def validate_live_identity(
    target: MigrationTarget,
    *,
    policy: MigrationPolicy,
    actual_database: str,
    actual_user: str,
) -> None:
    validate_target(target, policy=policy)
    if actual_database != target.database_name:
        raise MigrationSafetyError(
            f'Connected to database {actual_database!r}, expected configured '
            f'database {target.database_name!r}.'
        )
    if actual_user != target.database_user:
        raise MigrationSafetyError(
            f'Connected as role {actual_user!r}, expected configured role '
            f'{target.database_user!r}.'
        )


def validate_apply_confirmation(
    confirmation: str,
    *,
    policy: MigrationPolicy,
) -> None:
    if confirmation != policy.apply_confirmation:
        raise MigrationSafetyError(
            'Production apply requires confirmation token '
            f'{policy.apply_confirmation!r}.'
        )


def plan_migration(
    column: schema.ColumnState | None,
    *,
    table_exists: bool = True,
) -> MigrationPlan:
    """Return only the shared, reviewed additive badges plan."""

    return schema.plan_migration(column, table_exists=table_exists)


def _session_identity(cursor) -> tuple[str, str]:
    cursor.execute('SELECT current_database(), current_user')
    return tuple(cursor.fetchone())


def _read_plan(cursor) -> MigrationPlan:
    table_exists, column = schema.schema_metadata(cursor)
    return plan_migration(column, table_exists=table_exists)


def verify_migration(
    connection,
    *,
    target: MigrationTarget,
    policy: MigrationPolicy,
) -> MigrationPlan:
    """Inspect identity and schema in an explicitly read-only transaction."""

    validate_target(target, policy=policy)
    try:
        with connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION READ ONLY')
            actual_database, actual_user = _session_identity(cursor)
            validate_live_identity(
                target,
                policy=policy,
                actual_database=actual_database,
                actual_user=actual_user,
            )
            plan = _read_plan(cursor)
        connection.rollback()
        return plan
    except Exception:
        connection.rollback()
        raise


def apply_migration(
    connection,
    *,
    target: MigrationTarget,
    policy: MigrationPolicy,
    confirmation: str,
) -> MigrationPlan:
    """Atomically apply and exactly verify the reviewed additive column."""

    validate_target(target, policy=policy)
    validate_apply_confirmation(confirmation, policy=policy)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '5s'")
            actual_database, actual_user = _session_identity(cursor)
            validate_live_identity(
                target,
                policy=policy,
                actual_database=actual_database,
                actual_user=actual_user,
            )
            plan = _read_plan(cursor)
            for statement in plan.statements:
                cursor.execute(statement)
            verification = _read_plan(cursor)
            if not verification.already_applied:
                raise MigrationSafetyError(
                    'Post-DDL verification still reports the reviewed badges '
                    'column missing; refusing to commit.'
                )
        connection.commit()
        return plan
    except Exception:
        connection.rollback()
        raise
