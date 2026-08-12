#!/usr/bin/env python3
"""Read-only development container deployment preflight."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
sys.dont_write_bytecode = True

from modules.container_deployment_doctor import (  # noqa: E402
    ContainerDoctorError,
    MODES,
    format_report,
    report_json,
    run_doctor,
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            'Read-only preflight for the reviewed development container '
            'contract. Never invokes Docker or connects to external systems.'
        )
    )
    parser.add_argument('--mode', choices=MODES, required=True)
    parser.add_argument('--json', action='store_true', help='emit machine JSON')
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if os.environ.get('POLYBOT_ENV') != 'development':
        print(
            'Container deployment doctor refused: POLYBOT_ENV must be '
            'exactly development.',
            file=sys.stderr,
        )
        return 2
    try:
        report = run_doctor(PROJECT_ROOT, mode=args.mode)
    except ContainerDoctorError as exc:
        print(f'Container deployment doctor refused: {exc}', file=sys.stderr)
        return 2
    print(report_json(report) if args.json else format_report(report))
    return 0 if report.ready else 2


if __name__ == '__main__':
    raise SystemExit(main())
