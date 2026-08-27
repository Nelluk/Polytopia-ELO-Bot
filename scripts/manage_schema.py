#!/usr/bin/env python3
"""Plan, verify, or explicitly apply the configured PolyBot schema."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime_config import load_runtime_profile  # noqa: E402
from modules.schema_management import (  # noqa: E402
    SchemaManagementError,
    apply_schema,
    confirmation_token,
    inspect_schema,
    target_from_profile,
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--verify', action='store_true')
    mode.add_argument('--apply', action='store_true')
    parser.add_argument('--confirm', default='')
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    profile = load_runtime_profile(create_directories=False)
    target = target_from_profile(profile)
    token = confirmation_token(target)
    if args.apply and args.confirm != token:
        raise SchemaManagementError(
            f'Schema apply confirmation mismatch; expected {token!r}.'
        )

    plan = inspect_schema(target)
    print('Configured schema plan')
    print(f'environment: {target.environment}')
    print(f'database: {plan.database_name}')
    print(f'role: {plan.database_user}')
    if plan.operations:
        print('required operations:')
        for operation in plan.operations:
            print(f'  - {operation}')
    else:
        print('required operations: none (schema is current)')

    if args.verify:
        return 1 if plan.operations else 0
    if not args.apply:
        print(f'confirmation: {token}')
        print('Plan only; the database was inspected read-only and no DDL ran.')
        return 0
    if not plan.operations:
        print('No DDL was necessary.')
        return 0

    result = apply_schema(target, confirmation=args.confirm)
    print(
        'Schema committed and verified: '
        f'{len(result.verified_tables)} tables and required constraints/columns.'
    )
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SchemaManagementError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        raise SystemExit(2)
