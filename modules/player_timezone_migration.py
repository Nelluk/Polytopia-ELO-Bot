"""Fail-closed additive migration planning for P6.2 timezone storage.

This module deliberately does not import ``modules.models``.  The application
model selects the new columns, so a deployment must add the columns before
starting code that contains the P6.2 model.  The standalone script uses this
module against an explicitly verified PostgreSQL target.
"""

from __future__ import annotations

from dataclasses import dataclass


TABLE_NAME = 'discordmember'
MINUTES_COLUMN = 'timezone_offset_minutes'
CLEARED_COLUMN = 'timezone_offset_cleared'
ADD_CONFIRMATION = 'P6.2-TIMEZONE-ADD'
ROLLBACK_CONFIRMATION = 'P6.2-TIMEZONE-ROLLBACK'


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
    """Require exact profile and PostgreSQL session identity."""

    if target.environment not in {'production', 'development'}:
        raise MigrationSafetyError(
            'Schema migration requires an explicit production or development '
            'runtime profile.'
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
    *,
    allow_development: bool = False,
) -> None:
    """Refuse a live apply unless the caller has separately opened its gate."""

    validate_target_identity(
        target,
        actual_database=target.database_name,
        actual_user=target.database_user,
    )
    if target.environment == 'development' and not allow_development:
        raise MigrationSafetyError(
            'P6.2 migration apply is not enabled for development databases '
            'by default; use the separately approved stopped-beta gate.'
        )
    if target.environment == 'development' and (
        target.database_name == 'polytopia_dev'
        or target.database_user == 'polybot_dev'
    ) and not allow_development:
        raise MigrationSafetyError(
            'Refusing to apply P6.2 to the polytopia_dev/polybot_dev target.'
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
    allow_development: bool = False,
) -> MigrationPlan:
    """Apply only the additive plan inside one transaction."""

    if confirmation != ADD_CONFIRMATION:
        raise MigrationSafetyError(
            f'Apply requires confirmation token {ADD_CONFIRMATION!r}.'
        )
    validate_apply_target(target, allow_development=allow_development)

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


def rollback_migration(
    connection,
    *,
    target: MigrationTarget,
    confirmation: str,
    owned_columns: tuple[str, ...],
    allow_development: bool = False,
) -> MigrationPlan:
    """Remove only explicitly owned additive columns, in reverse order."""

    if confirmation != ROLLBACK_CONFIRMATION:
        raise MigrationSafetyError(
            f'Rollback requires confirmation token {ROLLBACK_CONFIRMATION!r}.'
        )
    validate_apply_target(target, allow_development=allow_development)
    allowed = {MINUTES_COLUMN, CLEARED_COLUMN}
    if not owned_columns or any(column not in allowed for column in owned_columns):
        raise MigrationSafetyError(
            'Rollback requires explicit ownership of only P6.2 columns.'
        )
    statements = tuple(
        f'ALTER TABLE "{TABLE_NAME}" DROP COLUMN "{name}"'
        for name in reversed(tuple(dict.fromkeys(owned_columns)))
    )
    plan = MigrationPlan(
        table=TABLE_NAME,
        statements=statements,
        rollback_statements=(),
        added_columns=tuple(dict.fromkeys(owned_columns)),
    )
    try:
        with connection.cursor() as cursor:
            actual_database, actual_user = _session_identity(cursor)
            validate_target_identity(
                target,
                actual_database=actual_database,
                actual_user=actual_user,
            )
            for statement in plan.statements:
                cursor.execute(statement)
        connection.commit()
        return plan
    except Exception:
        connection.rollback()
        raise
