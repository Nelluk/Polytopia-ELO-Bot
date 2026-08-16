#!/usr/bin/env python3
"""Plan, apply, or verify the development PolyChampions historical mirror."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
sys.dont_write_bytecode = True

from modules import beta_operations  # noqa: E402
from modules.historical_mirror import (  # noqa: E402
    HistoricalMirrorError,
    HistoricalMirrorReconciliationRequired,
    PARKING_GUILD_ID,
    SOURCE_GUILD_ID,
    TARGET_GUILD_ID,
    apply_database,
    plan_database,
    verification_plan,
    verify_database,
)
from runtime_config import RuntimeConfigurationError, load_runtime_profile  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Development-only PolyChampions historical guild mirror.'
    )
    commands = parser.add_subparsers(dest='operation', required=True)
    commands.add_parser('plan', help='Read and print a digest-bound plan.')
    apply = commands.add_parser('apply', help='Apply the exact reviewed plan.')
    apply.add_argument('--confirm', required=True, help='Exact confirmation from plan.')
    verify = commands.add_parser('verify', help='Read-only invariant verification.')
    verify.add_argument('--confirm', required=True,
                        help='Exact plan confirmation carrying pre-state counts.')
    return parser


def _profile():
    if os.environ.get('POLYBOT_ENV', '').strip() != 'development':
        raise HistoricalMirrorError('POLYBOT_ENV must be exactly development.')
    return load_runtime_profile(
        project_root=PROJECT_ROOT, environ=os.environ, create_directories=False,
    )


def _checkpoint() -> str:
    return beta_operations.current_checkpoint(PROJECT_ROOT)


def _print_plan(plan) -> None:
    print('Historical PolyChampions mirror plan')
    print(f'source guild: {SOURCE_GUILD_ID}')
    print(f'target guild: {TARGET_GUILD_ID}')
    print(f'parking guild: {PARKING_GUILD_ID}')
    print('direct tables: ' + ', '.join(plan.present_tables))
    print('source counts: ' + (', '.join(f'{k}={v}' for k, v in plan.source_counts) or '(none)'))
    print('target counts to park: ' + (', '.join(f'{k}={v}' for k, v in plan.target_counts) or '(none)'))
    print('parking counts: ' + (', '.join(f'{k}={v}' for k, v in plan.parking_counts) or '(none)'))
    print('scrub candidates: ' + (', '.join(f'{k}={v}' for k, v in plan.scrub_counts) or '(none)'))
    print('writes: park target direct rows, remap source direct rows, scrub target Discord references, delete broadcasts and API applications')
    print('configuration topology: ' + plan.configuration.status)
    if plan.configuration.status == 'not_ready':
        print('configuration note: restore all five guild_configuration_* tables before apply')
    print(f'plan digest: {plan.digest}')
    print(f'confirmation: {plan.confirmation}')


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        profile = _profile()
        checkpoint = _checkpoint()
        if args.operation == 'plan':
            plan = plan_database(profile, checkpoint=checkpoint)
            _print_plan(plan)
            print('Plan only; no write was attempted.')
            return 0
        if args.operation == 'apply':
            result = apply_database(
                profile, args.confirm, checkpoint=checkpoint,
            )
            print(f'Historical mirror committed and verified ({result.verification}).')
            return 0
        plan = verification_plan(
            profile, args.confirm, checkpoint=checkpoint,
        )
        verify_database(profile, plan)
        print('Historical mirror verification passed.')
        if plan.configuration.status == 'not_ready':
            print('configuration: not ready (restore the five development tables before startup)')
        return 0
    except HistoricalMirrorReconciliationRequired as exc:
        print(f'Historical mirror committed; reconciliation required: {exc}', file=sys.stderr)
        return 3
    except (HistoricalMirrorError, RuntimeConfigurationError,
            beta_operations.BetaOperationsError) as exc:
        print(f'Historical mirror refused: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
