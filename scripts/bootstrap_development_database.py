#!/usr/bin/env python3
"""Plan or explicitly apply the development database schema bootstrap."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime_config import load_runtime_profile  # noqa: E402
from modules.development_schema_bootstrap import (  # noqa: E402
    DevelopmentSchemaBootstrapError,
    DevelopmentSchemaBootstrapTarget,
    bootstrap_development_schema,
    confirmation_token,
)
from modules import beta_database_writer_lock  # noqa: E402


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            'Plan or explicitly create missing tables and the deferred winner '
            'foreign key in a configured development database.'
        )
    )
    parser.add_argument(
        '--apply',
        action='store_true',
        help='Perform the explicitly confirmed development-only DDL.',
    )
    parser.add_argument(
        '--confirm',
        default='',
        help='Exact confirmation token printed by the plan.',
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    profile = load_runtime_profile(create_directories=False)
    target = DevelopmentSchemaBootstrapTarget(
        environment=profile.environment,
        database_name=profile.database_name,
        database_user=profile.database_user,
        database_password=profile.database_password,
        database_host=profile.database_host,
        database_port=profile.database_port,
    )
    if target.environment != 'development':
        raise DevelopmentSchemaBootstrapError(
            'This schema bootstrap tool requires POLYBOT_ENV=development.'
        )

    token = confirmation_token(target)
    print('Development schema bootstrap plan')
    print(f'database: {target.database_name}')
    print(f'role: {target.database_user}')
    print('writes: missing model tables and game.winner_id foreign key only')
    print(f'confirmation: {token}')
    if not args.apply:
        print('Plan only; no database connection or DDL was attempted.')
        return 0

    writer_lock = None
    fresh_database_lock = None
    try:
        writer_lock = beta_database_writer_lock.BetaDatabaseWriterLock(profile)
        writer_lock.acquire()
    except beta_database_writer_lock.BetaDatabaseWriterLockError:
        # A genuinely fresh database has no fence table yet. The reviewed
        # bootstrap transaction creates it alongside the application schema.
        # Any nonfresh database must install/verify the fence separately and
        # therefore cannot use this exception path.
        import psycopg2

        probe = psycopg2.connect(
            dbname=profile.database_name,
            user=profile.database_user,
            password=profile.database_password,
            host=profile.database_host,
            port=profile.database_port,
            connect_timeout=10,
        )
        try:
            probe.autocommit = True
            with probe.cursor() as cursor:
                cursor.execute(
                    'SELECT current_database(), current_user, '
                    'pg_try_advisory_lock(%s)',
                    (
                        beta_database_writer_lock
                        .DATABASE_WRITER_ADVISORY_LOCK_KEY,
                    ),
                )
                identity = cursor.fetchone()
                if identity != (
                    profile.database_name,
                    profile.database_user,
                    True,
                ):
                    raise DevelopmentSchemaBootstrapError(
                        'Fresh development bootstrap could not acquire the '
                        'database-scoped writer lock.'
                    )
                cursor.execute(
                    'SELECT count(*) FROM information_schema.tables '
                    "WHERE table_schema = current_schema() "
                    "AND table_type = 'BASE TABLE'"
                )
                relation_count = int(cursor.fetchone()[0])
            if relation_count != 0:
                raise DevelopmentSchemaBootstrapError(
                    'A nonfresh database must install and acquire the '
                    'development writer fence before schema changes.'
                )
        except BaseException:
            probe.close()
            raise
        fresh_database_lock = probe
    try:
        result = bootstrap_development_schema(
            target,
            confirmation=args.confirm,
        )
    finally:
        if writer_lock is not None:
            writer_lock.release()
        if fresh_database_lock is not None:
            try:
                with fresh_database_lock.cursor() as cursor:
                    cursor.execute(
                        'SELECT pg_advisory_unlock(%s)',
                        (
                            beta_database_writer_lock
                            .DATABASE_WRITER_ADVISORY_LOCK_KEY,
                        ),
                    )
            finally:
                fresh_database_lock.close()
    print(
        'Development schema bootstrap committed and verified: '
        f'{len(result.verified_tables)} tables, winner foreign key present.'
    )
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except DevelopmentSchemaBootstrapError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        raise SystemExit(2)
