"""Explicit development-only owner for initial PolyBot schema creation."""

from __future__ import annotations

from dataclasses import dataclass, field
import importlib

from modules.database_schema_contract import (
    REQUIRED_TABLES,
    WINNER_FOREIGN_KEY_SQL,
)
from modules import development_writer_fence
from modules.startup_schema_preflight import (
    StartupSchemaPreflightRequest,
    StartupSchemaPreflightResult,
    inspect_startup_schema,
)


class DevelopmentSchemaBootstrapError(RuntimeError):
    """The explicit development bootstrap request is invalid or unsafe."""


@dataclass(frozen=True)
class DevelopmentSchemaBootstrapTarget:
    environment: str
    database_name: str
    database_user: str
    database_password: str = field(repr=False)
    database_host: str | None
    database_port: int | None


MODEL_NAMES = (
    'Configuration',
    'House',
    'Team',
    'DiscordMember',
    'Game',
    'Player',
    'Tribe',
    'Squad',
    'GameSide',
    'SquadMember',
    'Lineup',
    'GameLog',
    'TeamServerBroadcastMessage',
    'ApiApplication',
    'Auction',
    'Bid',
    'PlayerHousePreference',
)


def confirmation_token(target: DevelopmentSchemaBootstrapTarget) -> str:
    return (
        'BOOTSTRAP DEVELOPMENT DATABASE '
        f'{target.database_name} AS {target.database_user}'
    )


def _validate_target(
    target: DevelopmentSchemaBootstrapTarget,
) -> DevelopmentSchemaBootstrapTarget:
    if not isinstance(target, DevelopmentSchemaBootstrapTarget):
        raise DevelopmentSchemaBootstrapError(
            'A frozen development schema bootstrap target is required.'
        )
    if target.environment != 'development':
        raise DevelopmentSchemaBootstrapError(
            'Schema bootstrap is development-only.'
        )
    if not target.database_name or not target.database_user:
        raise DevelopmentSchemaBootstrapError(
            'Schema bootstrap requires an explicit database and role.'
        )
    if not target.database_password:
        raise DevelopmentSchemaBootstrapError(
            'Schema bootstrap requires explicit database authentication.'
        )
    return target


def _model_inventory(models):
    model_classes = tuple(getattr(models, name) for name in MODEL_NAMES)
    actual_tables = tuple(sorted(
        model._meta.table_name for model in model_classes
    ))
    if actual_tables != REQUIRED_TABLES:
        raise DevelopmentSchemaBootstrapError(
            'Development schema bootstrap inventory does not match the '
            'model-free startup contract.'
        )
    return model_classes


def bootstrap_development_schema(
    target: DevelopmentSchemaBootstrapTarget,
    *,
    confirmation: str,
) -> StartupSchemaPreflightResult:
    """Create missing development tables/FK only after exact confirmation."""

    target = _validate_target(target)
    expected_confirmation = confirmation_token(target)
    if confirmation != expected_confirmation:
        raise DevelopmentSchemaBootstrapError(
            'Development schema bootstrap confirmation mismatch; expected '
            f'{expected_confirmation!r}.'
        )

    models = importlib.import_module('modules.models')
    model_classes = _model_inventory(models)
    with models.db.connection_context():
        live_database, live_user = models.db.execute_sql(
            'SELECT current_database(), current_user'
        ).fetchone()
        if (
            live_database != target.database_name
            or live_user != target.database_user
        ):
            raise DevelopmentSchemaBootstrapError(
                'Development schema bootstrap database identity mismatch: '
                f'expected {target.database_name!r}/{target.database_user!r}, '
                f'received {live_database!r}/{live_user!r}.'
            )
        with models.db.atomic():
            models.db.create_tables(model_classes, safe=True)
            models.db.execute_sql(development_writer_fence.CREATE_TABLE_SQL)
            models.db.execute_sql(
                development_writer_fence.INSERT_ROW_SQL,
                (
                    development_writer_fence.DATABASE_WRITER_ADVISORY_LOCK_KEY,
                    development_writer_fence.FENCE_SCHEMA_VERSION,
                ),
            )
            winner_fk_exists = bool(
                models.db.execute_sql(WINNER_FOREIGN_KEY_SQL).fetchone()[0]
            )
            if not winner_fk_exists:
                models.Game._schema.create_foreign_key(models.Game.winner)

    return inspect_startup_schema(StartupSchemaPreflightRequest(
        database_name=target.database_name,
        database_user=target.database_user,
        database_password=target.database_password,
        database_host=target.database_host,
        database_port=target.database_port,
    ))
