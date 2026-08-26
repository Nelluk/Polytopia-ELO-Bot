"""Model-free additive migration plan for Game.cleanup_deferred_until."""

from __future__ import annotations

from dataclasses import dataclass

TABLE_NAME = 'game'
COLUMN_NAME = 'cleanup_deferred_until'
DEVELOPMENT_ENVIRONMENT = 'development'
DEVELOPMENT_DATABASE = 'polytopia_dev'
DEVELOPMENT_ROLE = 'polybot_dev'
DEVELOPMENT_APPLY_CONFIRMATION = 'P5.17-DEVELOPMENT-GAME-KEEP-ACTIVE-APPLY'
DDL = (
    'ALTER TABLE "public"."game" '
    'ADD COLUMN "cleanup_deferred_until" DATE NULL'
)


class MigrationSafetyError(RuntimeError):
    pass


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
    def already_applied(self):
        return not self.statements


def column_matches_contract(column: ColumnState) -> bool:
    return (
        column.data_type.casefold() == 'date'
        and column.udt_name.casefold() == 'date'
        and column.is_nullable.upper() == 'YES'
        and column.column_default is None
    )


def plan_migration(column: ColumnState | None, *, table_exists=True):
    if not table_exists:
        raise MigrationSafetyError('public.game does not exist; refusing migration.')
    if column is None:
        return MigrationPlan((DDL,))
    if not column_matches_contract(column):
        raise MigrationSafetyError(
            'public.game.cleanup_deferred_until has an incompatible schema.'
        )
    return MigrationPlan(())


def validate_target_identity(target, *, actual_database, actual_user):
    if (
        target.environment,
        target.database_name,
        target.database_user,
    ) != (DEVELOPMENT_ENVIRONMENT, DEVELOPMENT_DATABASE, DEVELOPMENT_ROLE):
        raise MigrationSafetyError(
            'P5.17 development apply requires development / polytopia_dev / '
            'polybot_dev.'
        )
    if (actual_database, actual_user) != (DEVELOPMENT_DATABASE, DEVELOPMENT_ROLE):
        raise MigrationSafetyError('Live database identity does not match the reviewed target.')


def validate_apply_target(target):
    validate_target_identity(
        target,
        actual_database=target.database_name,
        actual_user=target.database_user,
    )


def validate_apply_confirmation(value):
    if value != DEVELOPMENT_APPLY_CONFIRMATION:
        raise MigrationSafetyError(
            f'Apply requires exact confirmation {DEVELOPMENT_APPLY_CONFIRMATION!r}.'
        )


def schema_metadata(cursor):
    cursor.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = %s)",
        (TABLE_NAME,),
    )
    if not cursor.fetchone()[0]:
        return False, None
    cursor.execute(
        'SELECT data_type, udt_name, is_nullable, column_default '
        'FROM information_schema.columns '
        "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
        (TABLE_NAME, COLUMN_NAME),
    )
    row = cursor.fetchone()
    return True, ColumnState(*row) if row else None


def inspect_migration(connection, *, target):
    with connection.cursor() as cursor:
        cursor.execute('SELECT current_database(), current_user')
        identity = cursor.fetchone()
        validate_target_identity(
            target, actual_database=identity[0], actual_user=identity[1],
        )
        table_exists, column = schema_metadata(cursor)
    return plan_migration(column, table_exists=table_exists)


def apply_migration(connection, *, target, confirmation):
    validate_apply_target(target)
    validate_apply_confirmation(confirmation)
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT current_database(), current_user')
            identity = cursor.fetchone()
            validate_target_identity(target, actual_database=identity[0], actual_user=identity[1])
            table_exists, column = schema_metadata(cursor)
            plan = plan_migration(column, table_exists=table_exists)
            for statement in plan.statements:
                cursor.execute(statement)
            table_exists, column = schema_metadata(cursor)
            if not plan_migration(column, table_exists=table_exists).already_applied:
                raise MigrationSafetyError('Post-DDL verification failed.')
        connection.commit()
        return plan
    except Exception:
        connection.rollback()
        raise
