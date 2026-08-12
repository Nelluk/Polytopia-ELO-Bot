#!/usr/bin/env python3
"""Inspect, plan, and narrowly refresh the running development Beta Lab."""

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

from modules.beta_operations import (  # noqa: E402
    BetaOperationsError,
    assert_operator_context,
    send_control_request,
)
from runtime_config import RuntimeConfigurationError, load_runtime_profile  # noqa: E402


RESULTS_PACK = 'game-results'
REFRESH_CONFIRMATION = 'REFRESH-game-results'


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Operate the exact development Beta Lab through its running bot.',
    )
    parser.add_argument('--json', action='store_true')
    operations = parser.add_subparsers(dest='operation', required=True)
    operations.add_parser('status', help='read current pack readiness')
    operations.add_parser('plan', help='show bounded pack actions')
    refresh = operations.add_parser(
        'refresh',
        help='refresh one supported mutable pack through the ELO coordinator',
    )
    refresh.add_argument('--pack', choices=(RESULTS_PACK,), required=True)
    refresh.add_argument('--confirm', required=True)
    return parser


def _profile():
    if os.environ.get('POLYBOT_ENV', '').strip() != 'development':
        raise RuntimeConfigurationError(
            'Set POLYBOT_ENV=development; Beta Lab operations never use production.'
        )
    return load_runtime_profile(
        project_root=PROJECT_ROOT,
        environ=os.environ,
        create_directories=False,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        assert_operator_context(os.environ)
        profile = _profile()
        if args.operation == 'refresh':
            if args.confirm != REFRESH_CONFIRMATION:
                raise BetaLabCommandError(
                    'Refresh requires --confirm '
                    + REFRESH_CONFIRMATION
                )
            request = {
                'operation': 'beta-lab-refresh',
                'pack': args.pack,
                'confirm': args.confirm,
            }
        else:
            request = {'operation': f'beta-lab-{args.operation}'}
        result = asyncio.run(send_control_request(profile, request, timeout=60.0))
        if args.json:
            print(json.dumps(dict(result), ensure_ascii=False, sort_keys=True))
        else:
            print(json.dumps(dict(result), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (RuntimeConfigurationError, BetaOperationsError, BetaLabCommandError) as exc:
        if args.json:
            print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        else:
            print(f'Error: {exc}', file=sys.stderr)
        return 2


class BetaLabCommandError(ValueError):
    pass


if __name__ == '__main__':
    raise SystemExit(main())
