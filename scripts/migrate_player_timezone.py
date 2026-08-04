#!/usr/bin/env python3
"""Plan or apply the additive P6.2 timezone schema migration.

The default mode is offline and prints the exact additive/rollback SQL.  Live
apply and rollback require explicit confirmation, an exact production runtime
profile, and PostgreSQL session identity verification.  This script never
runs as bot startup work.
"""

from __future__ import annotations

import argparse
import sys

from modules import player_timezone_migration as migration


def _offline_plan() -> migration.MigrationPlan:
    return migration.plan_migration({}, table_exists=True)


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Plan or explicitly apply the P6.2 timezone migration.'
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--rollback', action='store_true')
    parser.add_argument('--confirm', default='')
    parser.add_argument(
        '--owned-column',
        action='append',
        dest='owned_columns',
        default=[],
        help='P6.2 column previously recorded as owned; repeat as needed.',
    )
    return parser.parse_args(argv)


def _print_plan(plan: migration.MigrationPlan) -> None:
    print(f'table: public.{plan.table}')
    print('apply statements:')
    if plan.statements:
        for statement in plan.statements:
            print(f'  {statement};')
    else:
        print('  (already applied)')
    print('rollback statements:')
    if plan.rollback_statements:
        for statement in plan.rollback_statements:
            print(f'  {statement};')
    else:
        print('  (none)')


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
    if not args.apply and not args.rollback:
        _print_plan(_offline_plan())
        print(
            'No database connection or DDL was performed. Live operations '
            'require the stopped-beta deployment gate.'
        )
        return 0

    from runtime_config import get_runtime_profile

    profile = get_runtime_profile()
    target = migration.MigrationTarget(
        environment=profile.environment,
        database_name=profile.database_name,
        database_user=profile.database_user,
    )
    connection = None
    try:
        connection = _live_connection(profile)
        if args.apply:
            plan = migration.apply_migration(
                connection,
                target=target,
                confirmation=args.confirm,
            )
        else:
            plan = migration.rollback_migration(
                connection,
                target=target,
                confirmation=args.confirm,
                owned_columns=tuple(args.owned_columns),
            )
    except migration.MigrationSafetyError as exc:
        print(f'Migration refused: {exc}', file=sys.stderr)
        return 2
    finally:
        if connection is not None:
            connection.close()

    _print_plan(plan)
    print('Migration transaction committed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
