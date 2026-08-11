"""Fail-closed production tooling for the additive timezone columns.

This module is model-free and never opens a connection itself.  The CLI owns
connection creation only after validating the fixed production policy.  Tests
may supply an explicit non-production policy to exercise the same transaction
logic against an already-migrated development schema.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping

from modules import player_timezone_migration as schema


PRODUCTION_ENVIRONMENT = 'production'
PRODUCTION_DATABASE = 'polytopia2'
PRODUCTION_APPLY_CONFIRMATION = 'P9-B1-PRODUCTION-TIMEZONE-APPLY'
_TABLE_SQL = '"public"."discordmember"'

MigrationSafetyError = schema.MigrationSafetyError


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


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    table: str
    statements: tuple[str, ...]
    added_columns: tuple[str, ...]

    @property
    def already_applied(self) -> bool:
        return not self.statements


PRODUCTION_POLICY = MigrationPolicy(
    environment=PRODUCTION_ENVIRONMENT,
    database_name=PRODUCTION_DATABASE,
    apply_confirmation=PRODUCTION_APPLY_CONFIRMATION,
)


def validate_target(target: MigrationTarget, *, policy: MigrationPolicy) -> None:
    """Require the exact configured target selected by a fixed policy."""

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
        actual_user: str) -> None:
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
        policy: MigrationPolicy) -> None:
    if confirmation != policy.apply_confirmation:
        raise MigrationSafetyError(
            'Production apply requires confirmation token '
            f'{policy.apply_confirmation!r}.'
        )


def _is_false_default(value: str | None) -> bool:
    if value is None:
        return False
    normalized = re.sub(r'\s+', '', str(value).casefold())
    while normalized.startswith('(') and normalized.endswith(')'):
        normalized = normalized[1:-1]
    return normalized in ('false', 'false::boolean')


def _column_state(
        name: str,
        value: schema.ColumnState | dict) -> schema.ColumnState:
    if isinstance(value, schema.ColumnState):
        return value
    if not isinstance(value, dict):
        raise MigrationSafetyError(
            f'Column metadata for {name!r} is not a mapping.'
        )
    return schema.ColumnState(
        name=name,
        data_type=str(value.get('data_type', '')),
        is_nullable=str(value.get('is_nullable', '')),
        column_default=(
            str(value['column_default'])
            if value.get('column_default') is not None
            else None
        ),
    )


def _validate_defaults(
        columns: Mapping[str, schema.ColumnState | dict]) -> None:
    minutes = columns.get(schema.MINUTES_COLUMN)
    if minutes is not None:
        minutes_state = _column_state(schema.MINUTES_COLUMN, minutes)
        if minutes_state.column_default is not None:
            raise MigrationSafetyError(
                f'Existing {schema.TABLE_NAME}.{schema.MINUTES_COLUMN} has an '
                'unexpected default; refusing to alter it.'
            )
    cleared = columns.get(schema.CLEARED_COLUMN)
    if cleared is not None:
        cleared_state = _column_state(schema.CLEARED_COLUMN, cleared)
        if not _is_false_default(cleared_state.column_default):
            raise MigrationSafetyError(
                f'Existing {schema.TABLE_NAME}.{schema.CLEARED_COLUMN} does '
                'not have the reviewed FALSE default; refusing to alter it.'
            )


def plan_migration(
        existing_columns: Mapping[str, schema.ColumnState | dict],
        *,
        table_exists: bool = True) -> MigrationPlan:
    """Return only the reviewed additive production plan."""

    _validate_defaults(existing_columns)
    shared_plan = schema.plan_migration(
        dict(existing_columns),
        table_exists=table_exists,
    )
    statements = []
    for name in shared_plan.added_columns:
        if name == schema.MINUTES_COLUMN:
            definition = 'SMALLINT NULL'
        elif name == schema.CLEARED_COLUMN:
            definition = 'BOOLEAN NOT NULL DEFAULT FALSE'
        else:  # pragma: no cover - shared planning has a fixed reviewed set.
            raise MigrationSafetyError(
                f'Unreviewed timezone migration column {name!r}.'
            )
        statements.append(
            f'ALTER TABLE {_TABLE_SQL} ADD COLUMN "{name}" {definition}'
        )
    return MigrationPlan(
        table=shared_plan.table,
        statements=tuple(statements),
        added_columns=shared_plan.added_columns,
    )


def _session_identity(cursor) -> tuple[str, str]:
    cursor.execute('SELECT current_database(), current_user')
    return tuple(cursor.fetchone())


def _read_plan(cursor) -> MigrationPlan:
    table_exists, columns = schema.schema_metadata(cursor)
    return plan_migration(columns, table_exists=table_exists)


def verify_migration(
        connection,
        *,
        target: MigrationTarget,
        policy: MigrationPolicy) -> MigrationPlan:
    """Inspect identity/schema in an explicitly read-only transaction."""

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
        confirmation: str) -> MigrationPlan:
    """Atomically apply and verify only the reviewed additive statements."""

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
                    'Post-DDL verification still reports missing reviewed '
                    'columns; refusing to commit.'
                )
        connection.commit()
        return plan
    except Exception:
        connection.rollback()
        raise
