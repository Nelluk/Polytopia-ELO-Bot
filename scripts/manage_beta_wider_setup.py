#!/usr/bin/env python3
"""Plan or apply the reviewed WB1.3b development DB setup.

``status`` and ``plan`` use a worker-local read-only Peewee connection.
``seed`` and ``cleanup`` are explicit, synchronous, exact-scope operations;
they require the development identity gate and a stopped durable beta writer.
This CLI never imports Discord, command registration, fixtures, or ELO code.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules import beta_readiness, beta_wider_setup  # noqa: E402
from runtime_config import RuntimeConfigurationError, load_runtime_profile  # noqa: E402


EXACT_CLEANUP_CONFIRMATION = 'WB1.3B-CLEANUP'
EXACT_RECONCILE_CONFIRMATION = 'WB1.3B-RECONCILE'


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Exact-scope reviewed WB1.3b development house/team setup.'
    )
    parser.add_argument('--json', action='store_true', help='emit compact JSON')
    parser.add_argument(
        '--manifest',
        default=beta_wider_setup.DEFAULT_MANIFEST,
        help='repository-relative reviewed WB1.3b manifest path',
    )
    parser.add_argument(
        '--guild-id',
        type=int,
        default=beta_readiness.BETA_GUILD_ID,
        help=argparse.SUPPRESS,
    )
    operations = parser.add_subparsers(dest='operation', required=True)
    operations.add_parser('status', help='read current exact-scope setup state')
    operations.add_parser('plan', help='read current state and produce a setup plan')
    operations.add_parser('seed', help='create only missing reviewed houses and teams')
    cleanup = operations.add_parser(
        'cleanup',
        help='remove only records proven owned by a prior reviewed seed',
    )
    cleanup.add_argument(
        '--confirm',
        required=True,
        help=f'exact confirmation token: {EXACT_CLEANUP_CONFIRMATION}',
    )
    reconcile = operations.add_parser(
        'reconcile-cleanup',
        help='remove stale cleanup evidence after a read-only absence check',
    )
    reconcile.add_argument(
        '--confirm',
        required=True,
        help=f'exact confirmation token: {EXACT_RECONCILE_CONFIRMATION}',
    )
    return parser


def _emit(value: Any, *, as_json: bool) -> None:
    if as_json:
        sys.stdout.write(json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(',', ':'),
        ))
    else:
        print(json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2))


def _selected_profile():
    if os.environ.get('POLYBOT_ENV', '').strip() != 'development':
        raise RuntimeConfigurationError(
            'Set POLYBOT_ENV=development; WB1.3b never uses a production profile.'
        )
    return load_runtime_profile(
        project_root=PROJECT_ROOT,
        environ=os.environ,
        create_directories=False,
    )


def _load_manifest(value: str) -> dict[str, Any]:
    raw = beta_readiness.load_json_path(
        PROJECT_ROOT,
        value,
        label='reviewed WB1.3b manifest',
        max_bytes=beta_readiness.MAX_MANIFEST_BYTES,
    )
    return beta_wider_setup.validate_reviewed_manifest(raw)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = _load_manifest(args.manifest)
        if args.operation in {'cleanup', 'reconcile-cleanup'}:
            expected_confirmation = (
                EXACT_CLEANUP_CONFIRMATION
                if args.operation == 'cleanup'
                else EXACT_RECONCILE_CONFIRMATION
            )
            if args.confirm != expected_confirmation:
                raise beta_wider_setup.WiderBetaSetupConfirmationError(
                    f'{args.operation} requires --confirm {expected_confirmation}.'
                )
        profile = _selected_profile()
        common = {
            'profile': profile,
            'manifest': manifest,
            'guild_id': args.guild_id,
        }
        if args.operation == 'status':
            result = beta_wider_setup.status_wider_beta_setup(**common)
        elif args.operation == 'plan':
            result = beta_wider_setup.plan_wider_beta_setup(**common)
        elif args.operation == 'seed':
            result = beta_wider_setup.seed_wider_beta_setup(**common)
        elif args.operation == 'cleanup':
            result = beta_wider_setup.cleanup_wider_beta_setup(
                **common,
                confirmed=True,
            )
        else:
            result = beta_wider_setup.reconcile_cleanup_evidence(
                **common,
                confirmed=True,
            )
        _emit(result, as_json=args.json)
        return 0
    except (
            RuntimeConfigurationError,
            beta_readiness.ReadinessError,
            beta_wider_setup.WiderBetaSetupError,
            ValueError,
    ) as exc:
        if args.json:
            print(json.dumps({'error': str(exc)}, ensure_ascii=True, sort_keys=True))
        else:
            print(f'Error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
