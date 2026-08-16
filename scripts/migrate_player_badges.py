#!/usr/bin/env python3
"""Plan, verify, or explicitly apply the P12.1 development migration."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules import player_badges_migration as migration


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--verify', action='store_true')
    mode.add_argument('--apply', action='store_true')
    parser.add_argument('--confirm', default='')
    return parser.parse_args(argv)


def _target(profile):
    return migration.MigrationTarget(
        environment=profile.environment,
        database_name=profile.database_name,
        database_user=profile.database_user,
    )


def _connect(profile):
    import psycopg2

    return psycopg2.connect(
        dbname=profile.database_name,
        user=profile.database_user,
        password=profile.database_password,
        host=profile.database_host,
        port=profile.database_port,
    )


def _print_plan(plan, *, applied=False):
    print('table: public.player')
    print('column: badges TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[]')
    if plan.already_applied:
        print('status: already applied and exactly verified')
    else:
        print('status: applied and verified' if applied else 'status: apply required')
        for statement in plan.statements:
            print(f'  {statement};')
    print('rollback: leave the harmless additive column in place')


def main(argv=None) -> int:
    args = _parse_args(argv)
    if not args.verify and not args.apply:
        _print_plan(migration.plan_migration(None))
        print('No database connection or DDL was performed.')
        return 0
    if os.environ.get('POLYBOT_ENV') != migration.DEVELOPMENT_ENVIRONMENT:
        print('Migration refused: POLYBOT_ENV must be development.', file=sys.stderr)
        return 2

    from runtime_config import get_runtime_profile

    profile = get_runtime_profile()
    target = _target(profile)
    connection = None
    writer_lock = None
    try:
        migration.validate_apply_target(target)
        if args.apply:
            migration.validate_apply_confirmation(args.confirm)
            from modules import beta_database_writer_lock

            writer_lock = beta_database_writer_lock.BetaDatabaseWriterLock(profile)
            writer_lock.acquire()
        connection = _connect(profile)
        if args.verify:
            connection.set_session(readonly=True, autocommit=True)
        plan = (
            migration.apply_migration(
                connection,
                target=target,
                confirmation=args.confirm,
            )
            if args.apply
            else migration.inspect_migration(connection, target=target)
        )
    except Exception as exc:
        print(f'Migration refused: {exc}', file=sys.stderr)
        return 2
    finally:
        if connection is not None:
            connection.close()
        if writer_lock is not None:
            writer_lock.release()
    _print_plan(plan, applied=args.apply)
    if args.verify and not plan.already_applied:
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
