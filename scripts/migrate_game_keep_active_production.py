#!/usr/bin/env python3
"""Plan, verify, or explicitly apply the production keep-active column."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules import game_keep_active_production_migration as migration


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--verify', action='store_true')
    mode.add_argument('--apply', action='store_true')
    parser.add_argument('--confirm', default='')
    args = parser.parse_args(argv)
    print('column: public.game.cleanup_deferred_until DATE NULL')
    if not args.verify and not args.apply:
        print('status: apply required')
        print('No database connection or DDL was performed.')
        return 0
    if os.environ.get('POLYBOT_ENV') != migration.PRODUCTION_ENVIRONMENT:
        print('Migration refused: POLYBOT_ENV must be production.', file=sys.stderr)
        return 2
    from runtime_config import get_runtime_profile
    profile = get_runtime_profile()
    target = migration.MigrationTarget(profile.environment, profile.database_name, profile.database_user)
    connection = None
    try:
        migration.validate_target(target)
        if args.apply:
            migration.validate_apply_confirmation(
                args.confirm, policy=migration.PRODUCTION_POLICY,
            )
        import psycopg2
        connection = psycopg2.connect(
            dbname=profile.database_name, user=profile.database_user,
            password=profile.database_password, host=profile.database_host,
            port=profile.database_port,
        )
        plan = (
            migration.apply_migration(connection, target=target, confirmation=args.confirm)
            if args.apply else migration.verify_migration(connection, target=target)
        )
        print('status: already applied and exactly verified' if plan.already_applied else 'status: apply required')
        return 0 if args.apply or plan.already_applied else 1
    except Exception as exc:
        print(f'Migration refused: {exc}', file=sys.stderr)
        return 2
    finally:
        if connection is not None:
            connection.close()


if __name__ == '__main__':
    raise SystemExit(main())
