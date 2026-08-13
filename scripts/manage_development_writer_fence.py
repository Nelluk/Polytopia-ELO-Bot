#!/usr/bin/env python3
"""Plan, install, or verify the development writer-fence schema."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import psycopg2


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules import development_writer_fence as fence  # noqa: E402
from runtime_config import (  # noqa: E402
    RuntimeConfigurationError,
    load_runtime_profile,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_subparsers(dest='operation', required=True)
    operations.add_parser('plan')
    apply = operations.add_parser('apply')
    apply.add_argument('--confirm', required=True)
    operations.add_parser('verify')
    return parser


def _target(profile) -> fence.WriterFenceTarget:
    return fence.WriterFenceTarget(
        environment=profile.environment,
        database_name=profile.database_name,
        database_user=profile.database_user,
        database_password=profile.database_password,
        database_host=profile.database_host,
        database_port=profile.database_port,
    )


def _connection(profile):
    return psycopg2.connect(
        dbname=profile.database_name,
        user=profile.database_user,
        password=profile.database_password,
        host=profile.database_host,
        port=profile.database_port,
        connect_timeout=10,
        application_name='polybot-development-writer-fence-schema',
        options='-c statement_timeout=30000 -c lock_timeout=5000',
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        profile = load_runtime_profile(
            project_root=PROJECT_ROOT,
            create_directories=False,
        )
        target = fence.validate_target(_target(profile))
        token = fence.confirmation_token(target)
        if args.operation == 'plan':
            print('Development writer-fence schema plan')
            print(f'database: {target.database_name}')
            print(f'role: {target.database_user}')
            print(f'table: {fence.FENCE_TABLE}')
            print(f'confirmation: {token}')
            print('Plan only; no database connection or DDL was attempted.')
            return 0
        connection = _connection(profile)
        try:
            if args.operation == 'apply':
                fence.apply_schema(
                    connection,
                    target,
                    confirmation=args.confirm,
                )
                print('Development writer-fence schema installed and verified.')
            else:
                connection.set_session(readonly=True, autocommit=True)
                fence.verify_schema(connection, target)
                print('Development writer-fence schema verified.')
        finally:
            connection.close()
        return 0
    except (
        RuntimeConfigurationError,
        fence.DevelopmentWriterFenceError,
    ) as exc:
        print(f'Writer-fence operation refused: {exc}', file=sys.stderr)
        return 2
    except Exception:
        print(
            'Writer-fence operation refused: database operation failed.',
            file=sys.stderr,
        )
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
