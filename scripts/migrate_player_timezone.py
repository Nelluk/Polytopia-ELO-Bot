#!/usr/bin/env python3
"""Plan or explicitly apply the development P6.2 timezone schema migration.

The default mode is offline and prints the exact additive/rollback SQL.  Live
apply requires explicit ``POLYBOT_ENV=development``, the exact
``polytopia_dev``/``polybot_dev`` profile, an acknowledgement token, and
PostgreSQL session identity verification.  Rollback is intentionally offline
review SQL only; this script never runs DDL at bot startup.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules import player_timezone_migration as migration


def _offline_plan() -> migration.MigrationPlan:
    return migration.plan_migration({}, table_exists=True)


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            'Plan or explicitly apply the development-only P6.2 timezone '
            'migration.'
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--rollback', action='store_true')
    parser.add_argument(
        '--confirm',
        default='',
        help=(
            'development apply acknowledgement; required value is '
            f'{migration.DEVELOPMENT_APPLY_CONFIRMATION}'
        ),
    )
    return parser.parse_args(argv)


def _print_statements(label: str, statements: tuple[str, ...], empty: str) -> None:
    print(f'{label}:')
    if statements:
        for statement in statements:
            print(f'  {statement};')
    else:
        print(f'  ({empty})')


def _print_plan(
    plan: migration.MigrationPlan,
    *,
    apply_executed: bool = False,
    rollback_only: bool = False,
) -> None:
    print(f'table: public.{plan.table}')
    if rollback_only:
        _print_statements(
            'reviewed rollback statements (not executed)',
            plan.rollback_statements,
            'none in this plan',
        )
    else:
        _print_statements(
            (
                'apply statements (executed)'
                if apply_executed
                else 'planned apply statements (not executed)'
            ),
            plan.statements,
            'already applied',
        )
        _print_statements(
            'reviewed rollback statements (not executed)',
            plan.rollback_statements,
            'none in this plan',
        )


def _live_connection(profile):
    import psycopg2

    return psycopg2.connect(
        dbname=profile.database_name,
        user=profile.database_user,
        password=profile.database_password,
        host=profile.database_host,
        port=profile.database_port,
    )


def main(argv=None) -> int:
    args = _parse_args(argv)
    if args.rollback:
        _print_plan(_offline_plan(), rollback_only=True)
        print(
            'Rollback is review-only in P6.2: no database connection or DDL '
            'was performed.'
        )
        return 0
    if not args.apply:
        _print_plan(_offline_plan())
        print(
            'No database connection or DDL was performed. Live operations '
            'require the stopped-beta development gate.'
        )
        return 0

    if os.environ.get('POLYBOT_ENV') != migration.DEVELOPMENT_ENVIRONMENT:
        print(
            'Migration refused: set explicit POLYBOT_ENV=development; '
            'P6.2 does not support production schema operations.',
            file=sys.stderr,
        )
        return 2

    from runtime_config import get_runtime_profile
    from modules import beta_database_writer_lock

    profile = get_runtime_profile()
    target = migration.MigrationTarget(
        environment=profile.environment,
        database_name=profile.database_name,
        database_user=profile.database_user,
    )
    connection = None
    writer_lock = None
    try:
        # Validate the fixed profile before opening a PostgreSQL connection.
        migration.validate_apply_target(target)
        migration.validate_apply_confirmation(args.confirm)
        writer_lock = beta_database_writer_lock.BetaDatabaseWriterLock(profile)
        writer_lock.acquire()
        connection = _live_connection(profile)
        plan = migration.apply_migration(
            connection,
            target=target,
            confirmation=args.confirm,
        )
    except (
        migration.MigrationSafetyError,
        beta_database_writer_lock.BetaDatabaseWriterLockError,
    ) as exc:
        print(f'Migration refused: {exc}', file=sys.stderr)
        return 2
    finally:
        if connection is not None:
            connection.close()
        if writer_lock is not None:
            writer_lock.release()

    _print_plan(plan, apply_executed=True)
    print('Development migration transaction committed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
