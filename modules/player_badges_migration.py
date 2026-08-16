"""Model-free plan, verify, and apply support for P12.1 player badges."""

from __future__ import annotations

from dataclasses import dataclass


TABLE_NAME = 'player'
COLUMN_NAME = 'badges'
DEVELOPMENT_ENVIRONMENT = 'development'
DEVELOPMENT_DATABASE = 'polytopia_dev'
DEVELOPMENT_ROLE = 'polybot_dev'
DEVELOPMENT_APPLY_CONFIRMATION = 'P12.1-DEVELOPMENT-PLAYER-BADGES-APPLY'
DDL = (
    'ALTER TABLE "public"."player" '
    'ADD COLUMN "badges" TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[]'
)


class MigrationSafetyError(RuntimeError):
    """The target identity or schema differs from the reviewed contract."""


@dataclass(frozen=True)
class MigrationTarget:
    environment: str
    database_name: str
    database_user: str


@dataclass(frozen=True)
class ColumnState:
    data_type: str
    udt_name: str
    is_nullable: str
    column_default: str | None


@dataclass(frozen=True)
class MigrationPlan:
    statements: tuple[str, ...]

    @property
    def already_applied(self) -> bool:
        return not self.statements


def validate_target_identity(
    target: MigrationTarget,
    *,
    actual_database: str,
    actual_user: str,
) -> None:
    expected = (
        DEVELOPMENT_ENVIRONMENT,
        DEVELOPMENT_DATABASE,
        DEVELOPMENT_ROLE,
    )
    configured = (
        target.environment,
        target.database_name,
        target.database_user,
    )
    if configured != expected:
        raise MigrationSafetyError(
            'P12.1 apply requires the exact development / polytopia_dev / '
            'polybot_dev target.'
        )
    if (actual_database, actual_user) != (
        DEVELOPMENT_DATABASE,
        DEVELOPMENT_ROLE,
    ):
        raise MigrationSafetyError(
            'The live PostgreSQL database or role does not match the '
            'reviewed development target.'
        )


def validate_apply_target(target: MigrationTarget) -> None:
    validate_target_identity(
        target,
        actual_database=target.database_name,
        actual_user=target.database_user,
    )


def validate_apply_confirmation(value: str) -> None:
    if value != DEVELOPMENT_APPLY_CONFIRMATION:
        raise MigrationSafetyError(
            f'Apply requires exact confirmation '
            f'{DEVELOPMENT_APPLY_CONFIRMATION!r}.'
        )


def _normalized_default(value: str | None) -> str:
    return ''.join(str(value or '').lower().split())


def column_matches_contract(column: ColumnState) -> bool:
    default = _normalized_default(column.column_default)
    return (
        column.data_type.lower() == 'array'
        and column.udt_name.lower() == '_text'
        and column.is_nullable.upper() == 'NO'
        and default in {
            "array[]::text[]",
            "'{}'::text[]",
        }
    )


def plan_migration(
    column: ColumnState | None,
    *,
    table_exists: bool = True,
) -> MigrationPlan:
    if not table_exists:
        raise MigrationSafetyError(
            'public.player does not exist; refusing the migration.'
        )
    if column is None:
        return MigrationPlan(statements=(DDL,))
    if not column_matches_contract(column):
        raise MigrationSafetyError(
            'public.player.badges exists but its PostgreSQL type, element '
            'type, nullability, or default differs from the reviewed contract.'
        )
    return MigrationPlan(statements=())


def schema_metadata(cursor) -> tuple[bool, ColumnState | None]:
    cursor.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = %s)",
        (TABLE_NAME,),
    )
    if not bool(cursor.fetchone()[0]):
        return False, None
    cursor.execute(
        'SELECT data_type, udt_name, is_nullable, column_default '
        'FROM information_schema.columns '
        "WHERE table_schema = 'public' AND table_name = %s "
        'AND column_name = %s',
        (TABLE_NAME, COLUMN_NAME),
    )
    row = cursor.fetchone()
    if row is None:
        return True, None
    return True, ColumnState(*row)


def _session_identity(cursor) -> tuple[str, str]:
    cursor.execute('SELECT current_database(), current_user')
    return tuple(cursor.fetchone())


def inspect_migration(connection, *, target: MigrationTarget) -> MigrationPlan:
    """Read and verify the live identity/schema without executing DDL."""

    with connection.cursor() as cursor:
        database, user = _session_identity(cursor)
        validate_target_identity(
            target,
            actual_database=database,
            actual_user=user,
        )
        table_exists, column = schema_metadata(cursor)
        return plan_migration(column, table_exists=table_exists)


def apply_migration(
    connection,
    *,
    target: MigrationTarget,
    confirmation: str,
) -> MigrationPlan:
    """Apply and exactly verify the additive DDL in one transaction."""

    validate_apply_target(target)
    validate_apply_confirmation(confirmation)
    try:
        plan = inspect_migration(connection, target=target)
        with connection.cursor() as cursor:
            for statement in plan.statements:
                cursor.execute(statement)
            table_exists, column = schema_metadata(cursor)
            verified = plan_migration(column, table_exists=table_exists)
            if not verified.already_applied:
                raise MigrationSafetyError(
                    'Post-apply verification did not find the reviewed column.'
                )
        connection.commit()
        return plan
    except Exception:
        connection.rollback()
        raise
