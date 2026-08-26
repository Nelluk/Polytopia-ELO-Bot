"""Read-only, model-free schema preflight for ordinary bot startup."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field

import psycopg2

from runtime_config import (
    SUPPORTED_ENVIRONMENTS,
    database_authentication_is_supported,
)

from modules.database_schema_contract import (
    REQUIRED_TABLES,
    WINNER_FOREIGN_KEY_SQL,
)
from modules import game_keep_active_migration, player_badges_migration


class StartupSchemaPreflightError(RuntimeError):
    """The configured database does not satisfy the startup schema contract."""


@dataclass(frozen=True)
class StartupSchemaPreflightRequest:
    environment: str
    database_name: str
    database_user: str
    database_password: str = field(repr=False)
    database_host: str | None = None
    database_port: int | None = None


@dataclass(frozen=True)
class StartupSchemaPreflightResult:
    database_name: str
    database_user: str
    verified_tables: tuple[str, ...]
    winner_foreign_key_verified: bool


_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='polybot-startup-schema-preflight',
)


def _validate_request(
    request: StartupSchemaPreflightRequest,
) -> StartupSchemaPreflightRequest:
    if not isinstance(request, StartupSchemaPreflightRequest):
        raise StartupSchemaPreflightError(
            'A frozen startup schema preflight request is required.'
        )
    if not request.database_name or not request.database_user:
        raise StartupSchemaPreflightError(
            'Startup schema preflight requires an explicit database and role.'
        )
    if request.environment not in SUPPORTED_ENVIRONMENTS:
        raise StartupSchemaPreflightError(
            'Startup schema preflight requires an explicit runtime environment.'
        )
    if not database_authentication_is_supported(
            environment=request.environment,
            database_password=request.database_password,
            database_host=request.database_host):
        raise StartupSchemaPreflightError(
            'Startup schema preflight requires a configured password except '
            'for production on the default local PostgreSQL socket.'
        )
    return request


def _connect(request: StartupSchemaPreflightRequest):
    connection_parameters = dict(
        dbname=request.database_name,
        user=request.database_user,
        host=request.database_host,
        port=request.database_port,
    )
    if request.database_password:
        connection_parameters['password'] = request.database_password
    return psycopg2.connect(**connection_parameters)


def inspect_startup_schema(
    request: StartupSchemaPreflightRequest,
) -> StartupSchemaPreflightResult:
    """Verify required tables and the deferred winner FK without writing."""

    request = _validate_request(request)
    connection = _connect(request)
    try:
        connection.set_session(readonly=True, autocommit=True)
        with connection.cursor() as cursor:
            cursor.execute('SHOW transaction_read_only')
            if str(cursor.fetchone()[0]).casefold() != 'on':
                raise StartupSchemaPreflightError(
                    'Startup schema preflight connection is not read-only.'
                )
            cursor.execute('SELECT current_database(), current_user')
            live_database, live_user = cursor.fetchone()
            if (
                live_database != request.database_name
                or live_user != request.database_user
            ):
                raise StartupSchemaPreflightError(
                    'Startup schema preflight database identity mismatch: '
                    f'expected {request.database_name!r}/{request.database_user!r}, '
                    f'received {live_database!r}/{live_user!r}.'
                )

            cursor.execute(
                'SELECT table_name FROM information_schema.tables '
                'WHERE table_schema = current_schema() '
                "AND table_type = 'BASE TABLE'"
            )
            actual_tables = frozenset(row[0] for row in cursor.fetchall())
            missing_tables = tuple(
                table for table in REQUIRED_TABLES
                if table not in actual_tables
            )
            if missing_tables:
                raise StartupSchemaPreflightError(
                    'Startup schema is incomplete; missing required tables: '
                    + ', '.join(missing_tables)
                    + '. Run only the separately reviewed schema/bootstrap '
                    'operation for this environment.'
                )

            try:
                table_exists, badge_column = (
                    player_badges_migration.schema_metadata(cursor)
                )
                badge_plan = player_badges_migration.plan_migration(
                    badge_column,
                    table_exists=table_exists,
                )
            except player_badges_migration.MigrationSafetyError as exc:
                raise StartupSchemaPreflightError(
                    'Startup schema has an incompatible player.badges column. '
                    'Run the separately reviewed badge migration verification.'
                ) from exc
            if not badge_plan.already_applied:
                raise StartupSchemaPreflightError(
                    'Startup schema is missing the required player.badges '
                    'column. Stop the writer and run only the separately '
                    'reviewed badge migration operation for this environment.'
                )

            try:
                table_exists, cleanup_column = (
                    game_keep_active_migration.schema_metadata(cursor)
                )
                cleanup_plan = game_keep_active_migration.plan_migration(
                    cleanup_column,
                    table_exists=table_exists,
                )
            except game_keep_active_migration.MigrationSafetyError as exc:
                raise StartupSchemaPreflightError(
                    'Startup schema has an incompatible game.cleanup_deferred_until '
                    'column. Run the separately reviewed keep-active migration.'
                ) from exc
            if not cleanup_plan.already_applied:
                raise StartupSchemaPreflightError(
                    'Startup schema is missing game.cleanup_deferred_until. '
                    'Stop the writer and run only the separately reviewed '
                    'keep-active migration operation.'
                )

            cursor.execute(WINNER_FOREIGN_KEY_SQL)
            winner_foreign_key_verified = bool(cursor.fetchone()[0])
            if not winner_foreign_key_verified:
                raise StartupSchemaPreflightError(
                    'Startup schema is missing the required '
                    'game.winner_id -> gameside.id foreign key. Run only the '
                    'separately reviewed schema/bootstrap operation for this '
                    'environment.'
                )
    finally:
        connection.close()

    return StartupSchemaPreflightResult(
        database_name=str(live_database),
        database_user=str(live_user),
        verified_tables=REQUIRED_TABLES,
        winner_foreign_key_verified=True,
    )


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
        except BaseException:
            pass
        raise cancellation
    return future.result()


async def run_startup_schema_preflight(
    request: StartupSchemaPreflightRequest,
) -> StartupSchemaPreflightResult:
    """Run the read-only preflight on its owned worker connection."""

    request = _validate_request(request)
    future = _executor.submit(inspect_startup_schema, request)
    return await _drain_future(future)
