#!/usr/bin/env python3
"""Prepare the exact development-only Beta Lab House/Team/persona resources."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules import beta_lab_personas, beta_operations  # noqa: E402
from runtime_config import RuntimeConfigurationError, load_runtime_profile  # noqa: E402


CONFIRMATION = 'PREPARE-BETA-LAB-PERSONAS'
RECONCILE_CONFIRMATION = 'RECONCILE-BETA-LAB-PERSONAS'


def _profile():
    if os.environ.get('POLYBOT_ENV', '').strip() != 'development':
        raise RuntimeConfigurationError(
            'Set POLYBOT_ENV=development; persona setup never uses production.'
        )
    return load_runtime_profile(
        project_root=PROJECT_ROOT,
        environ=os.environ,
        create_directories=False,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true')
    operations = parser.add_subparsers(dest='operation', required=True)
    operations.add_parser('roles-status')
    role_setup = operations.add_parser('roles-setup')
    role_setup.add_argument('--confirm', required=True)
    role_reconcile = operations.add_parser('roles-reconcile')
    role_reconcile.add_argument('--confirm', required=True)
    operations.add_parser('database-status')
    database_seed = operations.add_parser('database-seed')
    database_seed.add_argument('--confirm', required=True)
    database_reconcile = operations.add_parser('database-reconcile')
    database_reconcile.add_argument('--confirm', required=True)
    return parser


def _role_value(status: beta_lab_personas.PersonaStatus) -> dict[str, object]:
    return {
        'ready': status.ready,
        'detail': status.detail,
        'team_role_id': status.team_role_id,
        'staff_role_id': status.staff_role_id,
    }


def _database_value(
    status: beta_lab_personas.PersonaDatabaseStatus,
) -> dict[str, object]:
    return {
        'ready': status.ready,
        'detail': status.detail,
        'team_id': status.team_id,
        'house_id': status.house_id,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        profile = _profile()
        if args.operation == 'roles-status':
            result = asyncio.run(beta_operations.send_control_request(
                profile,
                {'operation': 'beta-lab-persona-status'},
            ))
        elif args.operation == 'roles-setup':
            if args.confirm != CONFIRMATION:
                raise beta_lab_personas.BetaLabPersonaError(
                    f'Role setup requires --confirm {CONFIRMATION}.'
                )
            result = asyncio.run(beta_operations.send_control_request(
                profile,
                {
                    'operation': 'beta-lab-persona-setup',
                    'confirm': args.confirm,
                },
            ))
        elif args.operation == 'roles-reconcile':
            if args.confirm != RECONCILE_CONFIRMATION:
                raise beta_lab_personas.BetaLabPersonaError(
                    f'Role reconciliation requires --confirm '
                    f'{RECONCILE_CONFIRMATION}.'
                )
            result = asyncio.run(beta_operations.send_control_request(
                profile,
                {
                    'operation': 'beta-lab-persona-reconcile',
                    'confirm': args.confirm,
                },
            ))
        elif args.operation == 'database-status':
            result = _database_value(beta_lab_personas.database_status(profile))
        elif args.operation == 'database-seed':
            if args.confirm != CONFIRMATION:
                raise beta_lab_personas.BetaLabPersonaError(
                    f'Database seed requires --confirm {CONFIRMATION}.'
                )
            result = _database_value(beta_lab_personas.seed_database(profile))
        else:
            if args.confirm != RECONCILE_CONFIRMATION:
                raise beta_lab_personas.BetaLabPersonaError(
                    'Database reconciliation requires '
                    f'--confirm {RECONCILE_CONFIRMATION}.'
                )
            result = _database_value(
                beta_lab_personas.reconcile_pending_database(profile)
            )
        print(json.dumps(dict(result), sort_keys=True) if args.json else json.dumps(
            dict(result), sort_keys=True, indent=2,
        ))
        return 0
    except (
        RuntimeConfigurationError,
        beta_operations.BetaOperationsError,
        beta_lab_personas.BetaLabPersonaError,
    ) as exc:
        print(json.dumps({'error': str(exc)}) if args.json else f'Error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
