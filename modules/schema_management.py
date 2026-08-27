"""Configured-target schema bootstrap and additive upgrade management."""

from __future__ import annotations

from dataclasses import dataclass, field
import importlib

from runtime_config import (
    SUPPORTED_ENVIRONMENTS,
    database_authentication_is_supported,
)
from modules.database_schema_contract import REQUIRED_TABLES, WINNER_FOREIGN_KEY_SQL
from modules.development_schema_bootstrap import MODEL_NAMES
from modules.startup_schema_preflight import (
    StartupSchemaPreflightRequest,
    StartupSchemaPreflightResult,
    inspect_startup_schema,
)
from modules import (
    game_keep_active_migration,
    player_badges_migration,
    player_timezone_migration,
)


# Serializes this tool's DDL transactions. Operators must still stop every bot
# writer before applying an upgrade.
SCHEMA_ADVISORY_LOCK_KEY = 0x506F6C795363686D  # ASCII-ish ``PolySchm``.


class SchemaManagementError(RuntimeError):
    """The configured target, live identity, or schema is unsafe."""


@dataclass(frozen=True)
class SchemaTarget:
    environment: str
    database_name: str
    database_user: str
    database_password: str = field(repr=False)
    database_host: str | None = None
    database_port: int | None = None


@dataclass(frozen=True)
class SchemaPlan:
    database_name: str
    database_user: str
    operations: tuple[str, ...]

    @property
    def already_current(self) -> bool:
        return not self.operations


def target_from_profile(profile) -> SchemaTarget:
    return SchemaTarget(
        environment=profile.environment,
        database_name=profile.database_name,
        database_user=profile.database_user,
        database_password=profile.database_password,
        database_host=profile.database_host,
        database_port=profile.database_port,
    )


def confirmation_token(target: SchemaTarget) -> str:
    return (
        f'APPLY {target.environment.upper()} SCHEMA TO '
        f'{target.database_name} AS {target.database_user}'
    )


def _validate_target(target: SchemaTarget) -> SchemaTarget:
    if not isinstance(target, SchemaTarget):
        raise SchemaManagementError('A frozen configured schema target is required.')
    if target.environment not in SUPPORTED_ENVIRONMENTS:
        raise SchemaManagementError('Schema management requires an explicit environment.')
    if not target.database_name or not target.database_user:
        raise SchemaManagementError('Schema management requires a database and role.')
    if not database_authentication_is_supported(
        environment=target.environment,
        database_password=target.database_password,
        database_host=target.database_host,
    ):
        raise SchemaManagementError(
            'Schema management requires configured authentication; passwordless '
            'access is supported only for production on the local socket.'
        )
    return target


def _validate_live_identity(target: SchemaTarget, database: str, user: str) -> None:
    if (database, user) != (target.database_name, target.database_user):
        raise SchemaManagementError(
            'Live database identity mismatch: expected '
            f'{target.database_name!r}/{target.database_user!r}, received '
            f'{database!r}/{user!r}.'
        )


def _connect(target: SchemaTarget):
    import psycopg2

    values = {
        'dbname': target.database_name,
        'user': target.database_user,
        'host': target.database_host,
        'port': target.database_port,
    }
    if target.database_password:
        values['password'] = target.database_password
    return psycopg2.connect(**values)


def inspect_schema(target: SchemaTarget, *, connect=_connect) -> SchemaPlan:
    """Return an exact read-only plan for a fresh or existing database."""

    target = _validate_target(target)
    connection = connect(target)
    try:
        connection.set_session(readonly=True, autocommit=True)
        with connection.cursor() as cursor:
            cursor.execute('SHOW transaction_read_only')
            row = cursor.fetchone()
            if not row or str(row[0]).casefold() != 'on':
                raise SchemaManagementError('Schema inspection is not read-only.')
            cursor.execute('SELECT current_database(), current_user')
            database, user = cursor.fetchone()
            _validate_live_identity(target, database, user)
            cursor.execute(
                'SELECT table_name FROM information_schema.tables '
                "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
            )
            actual_tables = frozenset(value[0] for value in cursor.fetchall())
            missing_tables = tuple(
                name for name in REQUIRED_TABLES if name not in actual_tables
            )
            operations = [
                f'create missing table public.{name}' for name in missing_tables
            ]

            if 'discordmember' in actual_tables:
                exists, columns = player_timezone_migration.schema_metadata(cursor)
                operations.extend(
                    player_timezone_migration.plan_migration(
                        columns, table_exists=exists
                    ).statements
                )
            if 'player' in actual_tables:
                exists, column = player_badges_migration.schema_metadata(cursor)
                operations.extend(
                    player_badges_migration.plan_migration(
                        column, table_exists=exists
                    ).statements
                )
            if 'game' in actual_tables:
                exists, column = game_keep_active_migration.schema_metadata(cursor)
                operations.extend(
                    game_keep_active_migration.plan_migration(
                        column, table_exists=exists
                    ).statements
                )

            if {'game', 'gameside'} <= actual_tables:
                cursor.execute(WINNER_FOREIGN_KEY_SQL)
                if not bool(cursor.fetchone()[0]):
                    operations.append('create game.winner_id -> gameside.id foreign key')
            elif 'game' in missing_tables or 'gameside' in missing_tables:
                operations.append('create game.winner_id -> gameside.id foreign key')
        return SchemaPlan(str(database), str(user), tuple(operations))
    except (
        game_keep_active_migration.MigrationSafetyError,
        player_badges_migration.MigrationSafetyError,
        player_timezone_migration.MigrationSafetyError,
    ) as exc:
        raise SchemaManagementError(str(exc)) from exc
    finally:
        connection.close()


def _model_inventory(models):
    model_classes = tuple(getattr(models, name) for name in MODEL_NAMES)
    actual_tables = tuple(sorted(model._meta.table_name for model in model_classes))
    if actual_tables != REQUIRED_TABLES:
        raise SchemaManagementError(
            'Schema model inventory does not match the startup contract.'
        )
    return model_classes


def apply_schema(
    target: SchemaTarget,
    *,
    confirmation: str,
) -> StartupSchemaPreflightResult:
    """Atomically create missing tables and apply known additive upgrades."""

    target = _validate_target(target)
    expected = confirmation_token(target)
    if confirmation != expected:
        raise SchemaManagementError(
            f'Schema apply confirmation mismatch; expected {expected!r}.'
        )

    models = importlib.import_module('modules.models')
    model_classes = _model_inventory(models)
    try:
        with models.db.connection_context():
            database, user = models.db.execute_sql(
                'SELECT current_database(), current_user'
            ).fetchone()
            _validate_live_identity(target, database, user)
            with models.db.atomic():
                models.db.execute_sql(
                    'SELECT pg_advisory_xact_lock(%s)',
                    (SCHEMA_ADVISORY_LOCK_KEY,),
                )
                models.db.create_tables(model_classes, safe=True)
                with models.db.cursor() as cursor:
                    exists, columns = player_timezone_migration.schema_metadata(cursor)
                    timezone_plan = player_timezone_migration.plan_migration(
                        columns, table_exists=exists
                    )
                    for statement in timezone_plan.statements:
                        cursor.execute(statement)

                    exists, column = player_badges_migration.schema_metadata(cursor)
                    badge_plan = player_badges_migration.plan_migration(
                        column, table_exists=exists
                    )
                    for statement in badge_plan.statements:
                        cursor.execute(statement)

                    exists, column = game_keep_active_migration.schema_metadata(cursor)
                    keep_active_plan = game_keep_active_migration.plan_migration(
                        column, table_exists=exists
                    )
                    for statement in keep_active_plan.statements:
                        cursor.execute(statement)

                winner_exists = bool(
                    models.db.execute_sql(WINNER_FOREIGN_KEY_SQL).fetchone()[0]
                )
                if not winner_exists:
                    models.Game._schema.create_foreign_key(models.Game.winner)
    except (
        game_keep_active_migration.MigrationSafetyError,
        player_badges_migration.MigrationSafetyError,
        player_timezone_migration.MigrationSafetyError,
    ) as exc:
        raise SchemaManagementError(str(exc)) from exc

    return inspect_startup_schema(StartupSchemaPreflightRequest(
        environment=target.environment,
        database_name=target.database_name,
        database_user=target.database_user,
        database_password=target.database_password,
        database_host=target.database_host,
        database_port=target.database_port,
    ))
