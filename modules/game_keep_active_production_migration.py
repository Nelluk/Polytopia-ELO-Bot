"""Fail-closed production policy for the game keep-active column."""

from __future__ import annotations

from dataclasses import dataclass

from modules import game_keep_active_migration as schema

MigrationSafetyError = schema.MigrationSafetyError
MigrationPlan = schema.MigrationPlan
ColumnState = schema.ColumnState
PRODUCTION_ENVIRONMENT = 'production'
PRODUCTION_DATABASE = 'polytopia2'
PRODUCTION_APPLY_CONFIRMATION = 'P5.17-PRODUCTION-GAME-KEEP-ACTIVE-APPLY'


@dataclass(frozen=True)
class MigrationPolicy:
    environment: str = PRODUCTION_ENVIRONMENT
    database_name: str = PRODUCTION_DATABASE
    apply_confirmation: str = PRODUCTION_APPLY_CONFIRMATION


@dataclass(frozen=True)
class MigrationTarget:
    environment: str
    database_name: str
    database_user: str


PRODUCTION_POLICY = MigrationPolicy()


def validate_target(target, *, policy=PRODUCTION_POLICY):
    if target.environment != policy.environment or target.database_name != policy.database_name:
        raise MigrationSafetyError('Production migration target is not exact.')
    if not target.database_user.strip():
        raise MigrationSafetyError('Production role must be explicit.')


def validate_live_identity(target, *, actual_database, actual_user, policy=PRODUCTION_POLICY):
    validate_target(target, policy=policy)
    if (actual_database, actual_user) != (target.database_name, target.database_user):
        raise MigrationSafetyError('Live production database identity mismatch.')


def validate_apply_confirmation(value, *, policy=PRODUCTION_POLICY):
    if value != policy.apply_confirmation:
        raise MigrationSafetyError('Production apply acknowledgement mismatch.')


def plan_migration(column, *, table_exists=True):
    return schema.plan_migration(column, table_exists=table_exists)


def verify_migration(connection, *, target, policy=PRODUCTION_POLICY):
    validate_target(target, policy=policy)
    try:
        with connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION READ ONLY')
            cursor.execute('SHOW transaction_read_only')
            readonly = cursor.fetchone()
            if not readonly or str(readonly[0]).casefold() != 'on':
                raise MigrationSafetyError(
                    'Production verification connection is not transaction read-only.'
                )
            cursor.execute('SELECT current_database(), current_user')
            identity = cursor.fetchone()
            validate_live_identity(target, actual_database=identity[0], actual_user=identity[1], policy=policy)
            table_exists, column = schema.schema_metadata(cursor)
            plan = plan_migration(column, table_exists=table_exists)
        connection.rollback()
        return plan
    except Exception:
        connection.rollback()
        raise


def apply_migration(connection, *, target, policy=PRODUCTION_POLICY, confirmation):
    validate_target(target, policy=policy)
    validate_apply_confirmation(confirmation, policy=policy)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '5s'")
            cursor.execute('SELECT current_database(), current_user')
            identity = cursor.fetchone()
            validate_live_identity(target, actual_database=identity[0], actual_user=identity[1], policy=policy)
            table_exists, column = schema.schema_metadata(cursor)
            plan = plan_migration(column, table_exists=table_exists)
            for statement in plan.statements:
                cursor.execute(statement)
            table_exists, column = schema.schema_metadata(cursor)
            if not plan_migration(column, table_exists=table_exists).already_applied:
                raise MigrationSafetyError('Post-DDL verification failed.')
        connection.commit()
        return plan
    except Exception:
        connection.rollback()
        raise
