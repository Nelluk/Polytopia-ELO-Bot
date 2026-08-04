"""Fail-closed additive migration planning for P6.2 timezone storage.

This module deliberately does not import ``modules.models``.  The application
model selects the new columns, so a deployment must add the columns before
starting code that contains the P6.2 model.  The standalone script uses this
module only against the explicitly verified development PostgreSQL target;
production apply/rollback is outside this unit.
"""

from __future__ import annotations

from dataclasses import dataclass


TABLE_NAME = 'discordmember'
MINUTES_COLUMN = 'timezone_offset_minutes'
CLEARED_COLUMN = 'timezone_offset_cleared'
DEVELOPMENT_ENVIRONMENT = 'development'
DEVELOPMENT_DATABASE = 'polytopia_dev'
DEVELOPMENT_ROLE = 'polybot_dev'
DEVELOPMENT_APPLY_CONFIRMATION = 'P6.2-DEVELOPMENT-TIMEZONE-APPLY'


class MigrationSafetyError(RuntimeError):
    """The target or current schema is not safe for this migration."""


@dataclass(frozen=True)
class MigrationTarget:
    environment: str
    database_name: str
    database_user: str


@dataclass(frozen=True)
class ColumnState:
    name: str
    data_type: str
    is_nullable: str
    column_default: str | None = None


@dataclass(frozen=True)
class MigrationPlan:
    table: str
    statements: tuple[str, ...]
    rollback_statements: tuple[str, ...]
    added_columns: tuple[str, ...]

    @property
    def already_applied(self) -> bool:
        return not self.statements


_EXPECTED = {
    MINUTES_COLUMN: ColumnState(
        name=MINUTES_COLUMN,
        data_type='smallint',
        is_nullable='YES',
    ),
    CLEARED_COLUMN: ColumnState(
        name=CLEARED_COLUMN,
        data_type='boolean',
        is_nullable='NO',
    ),
}


def validate_target_identity(
    target: MigrationTarget,
    *,
    actual_database: str,
    actual_user: str,
) -> None:
    """Require the fixed development profile and PostgreSQL identity."""

    if target.environment != DEVELOPMENT_ENVIRONMENT:
        raise MigrationSafetyError(
            'P6.2 migration is development-only; production schema work is '
            'deferred to the separately reviewed P9 unit.'
        )
    if target.database_name != DEVELOPMENT_DATABASE:
        raise MigrationSafetyError(
            f'P6.2 migration requires database {DEVELOPMENT_DATABASE!r}; '
            f'configured target was {target.database_name!r}.'
        )
    if target.database_user != DEVELOPMENT_ROLE:
        raise MigrationSafetyError(
            f'P6.2 migration requires role {DEVELOPMENT_ROLE!r}; '
            f'configured target was {target.database_user!r}.'
        )
    if actual_database != target.database_name:
        raise MigrationSafetyError(
            f'Connected to database {actual_database!r}, expected '
            f'{target.database_name!r}.'
        )
    if actual_user != target.database_user:
        raise MigrationSafetyError(
            f'Connected as role {actual_user!r}, expected '
            f'{target.database_user!r}.'
        )


def validate_apply_target(
    target: MigrationTarget,
) -> None:
    """Refuse every live target outside the fixed development profile."""

    validate_target_identity(
        target,
        actual_database=target.database_name,
        actual_user=target.database_user,
    )


def validate_apply_confirmation(confirmation: str) -> None:
    """Require the explicit acknowledgement for development DDL."""

    if confirmation != DEVELOPMENT_APPLY_CONFIRMATION:
        raise MigrationSafetyError(
            'Development apply requires confirmation token '
            f'{DEVELOPMENT_APPLY_CONFIRMATION!r}.'
        )


def _column_from_value(name: str, value) -> ColumnState:
    if isinstance(value, ColumnState):
        return value
    if not isinstance(value, dict):
        raise MigrationSafetyError(
            f'Column metadata for {name!r} is not a mapping.'
        )
    return ColumnState(
        name=name,
        data_type=str(value.get('data_type', '')),
        is_nullable=str(value.get('is_nullable', '')),
        column_default=(
            str(value['column_default'])
            if value.get('column_default') is not None
            else None
        ),
    )


def plan_migration(
    existing_columns: dict[str, ColumnState | dict],
    *,
    table_exists: bool = True,
) -> MigrationPlan:
    """Build an idempotent, additive plan without executing DDL."""

    if not table_exists:
        raise MigrationSafetyError(
            'The public.discordmember table was not found; refusing migration.'
        )

    statements = []
    added = []
    for name, expected in _EXPECTED.items():
        actual_value = existing_columns.get(name)
        if actual_value is not None:
            actual = _column_from_value(name, actual_value)
            if (
                actual.data_type.lower() != expected.data_type
                or actual.is_nullable.upper() != expected.is_nullable
            ):
                raise MigrationSafetyError(
                    f'Existing {TABLE_NAME}.{name} has unexpected '
                    f'type/nullability ({actual.data_type}, '
                    f'{actual.is_nullable}); refusing to alter it.'
                )
            continue

        if name == MINUTES_COLUMN:
            statement = (
                f'ALTER TABLE "{TABLE_NAME}" ADD COLUMN "{name}" '
                'SMALLINT NULL'
            )
        else:
            statement = (
                f'ALTER TABLE "{TABLE_NAME}" ADD COLUMN "{name}" '
                'BOOLEAN NOT NULL DEFAULT FALSE'
            )
        statements.append(statement)
        added.append(name)

    rollback_statements = tuple(
        f'ALTER TABLE "{TABLE_NAME}" DROP COLUMN "{name}"'
        for name in reversed(added)
    )
    return MigrationPlan(
        table=TABLE_NAME,
        statements=tuple(statements),
        rollback_statements=rollback_statements,
        added_columns=tuple(added),
    )


def schema_metadata(cursor) -> tuple[bool, dict[str, ColumnState]]:
    """Read only the bounded table/column metadata needed for planning."""

    cursor.execute(
        "SELECT EXISTS ("
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = %s)",
        (TABLE_NAME,),
    )
    table_exists = bool(cursor.fetchone()[0])
    if not table_exists:
        return False, {}
    cursor.execute(
        'SELECT column_name, data_type, is_nullable, column_default '
        'FROM information_schema.columns '
        "WHERE table_schema = 'public' AND table_name = %s "
        'AND column_name IN (%s, %s)',
        (TABLE_NAME, MINUTES_COLUMN, CLEARED_COLUMN),
    )
    columns = {
        row[0]: ColumnState(
            name=row[0],
            data_type=row[1],
            is_nullable=row[2],
            column_default=row[3],
        )
        for row in cursor.fetchall()
    }
    return True, columns


def _session_identity(cursor) -> tuple[str, str]:
    cursor.execute('SELECT current_database(), current_user')
    return tuple(cursor.fetchone())


def apply_migration(
    connection,
    *,
    target: MigrationTarget,
    confirmation: str,
) -> MigrationPlan:
    """Apply only the additive plan to the fixed development target."""

    validate_apply_confirmation(confirmation)
    validate_apply_target(target)

    try:
        with connection.cursor() as cursor:
            actual_database, actual_user = _session_identity(cursor)
            validate_target_identity(
                target,
                actual_database=actual_database,
                actual_user=actual_user,
            )
            table_exists, columns = schema_metadata(cursor)
            plan = plan_migration(columns, table_exists=table_exists)
            for statement in plan.statements:
                cursor.execute(statement)
        connection.commit()
        return plan
    except Exception:
        connection.rollback()
        raise
