#!/usr/bin/env python3
"""Plan, verify, or explicitly apply the production timezone migration.

The default plan is connection-free.  Verify is read-only.  Apply is the only
DDL path and requires the exact production profile, database, configured/live
role identity, and acknowledgement token.  There is intentionally no rollback
mode: code/config rollback leaves these additive columns in place.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules import player_timezone_production_migration as migration


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Plan, verify, or apply the production timezone migration.'
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--verify', action='store_true')
    mode.add_argument('--apply', action='store_true')
    parser.add_argument(
        '--confirm',
        default='',
        help=(
            'production apply acknowledgement; required value is '
            f'{migration.PRODUCTION_APPLY_CONFIRMATION}'
        ),
    )
    return parser.parse_args(argv)


def _offline_plan() -> migration.MigrationPlan:
    return migration.plan_migration({}, table_exists=True)


def _print_plan(plan: migration.MigrationPlan, *, label: str) -> None:
    print(f'table: public.{plan.table}')
    print(f'{label}:')
    if plan.statements:
        for statement in plan.statements:
            print(f'  {statement};')
    else:
        print('  (already applied)')
    print(
        'rollback disposition: retain the additive columns; roll back code '
        'and configuration instead'
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


def _target_from_profile(profile) -> migration.MigrationTarget:
    return migration.MigrationTarget(
        environment=profile.environment,
        database_name=profile.database_name,
        database_user=profile.database_user,
    )


def main(argv=None) -> int:
    args = _parse_args(argv)
    if not args.verify and not args.apply:
        _print_plan(_offline_plan(), label='planned apply statements (not executed)')
        print('No runtime configuration was loaded and no database connection or DDL was performed.')
        return 0

    if os.environ.get('POLYBOT_ENV') != migration.PRODUCTION_ENVIRONMENT:
        print(
            'Migration refused: live modes require exact '
            'POLYBOT_ENV=production.',
            file=sys.stderr,
        )
        return 2
    if args.apply:
        try:
            migration.validate_apply_confirmation(
                args.confirm,
                policy=migration.PRODUCTION_POLICY,
            )
        except migration.MigrationSafetyError as exc:
            print(f'Migration refused: {exc}', file=sys.stderr)
            return 2

    from runtime_config import RuntimeConfigurationError, load_runtime_profile

    connection = None
    try:
        profile = load_runtime_profile(
            project_root=PROJECT_ROOT,
            environ=os.environ,
            create_directories=False,
        )
        target = _target_from_profile(profile)
        migration.validate_target(target, policy=migration.PRODUCTION_POLICY)
        connection = _live_connection(profile)
        if args.verify:
            plan = migration.verify_migration(
                connection,
                target=target,
                policy=migration.PRODUCTION_POLICY,
            )
            _print_plan(plan, label='missing apply statements (not executed)')
            if plan.already_applied:
                print('Production timezone schema verification passed read-only.')
                return 0
            print(
                'Production timezone schema verification is incomplete; no '
                'DDL was performed.',
                file=sys.stderr,
            )
            return 1

        plan = migration.apply_migration(
            connection,
            target=target,
            policy=migration.PRODUCTION_POLICY,
            confirmation=args.confirm,
        )
        _print_plan(plan, label='apply statements (executed)')
        print('Production timezone migration transaction committed and verified.')
        return 0
    except (migration.MigrationSafetyError, RuntimeConfigurationError) as exc:
        print(f'Migration refused: {exc}', file=sys.stderr)
        return 2
    except Exception as exc:
        print(
            f'Migration failed: {type(exc).__name__}: {exc}',
            file=sys.stderr,
        )
        return 2
    finally:
        if connection is not None:
            connection.close()


if __name__ == '__main__':
    raise SystemExit(main())
