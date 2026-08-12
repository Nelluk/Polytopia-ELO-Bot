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
    GitSnapshot,
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
    parser.add_argument(
        '--immutable-image-checkpoint',
        default='',
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--host-platform',
        choices=('darwin', 'linux'),
        default='',
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--host-uid',
        type=int,
        default=-1,
        help=argparse.SUPPRESS,
    )
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
        run_options = {}
        if args.immutable_image_checkpoint:
            image_checkpoint = os.environ.get(
                'POLYBOT_IMAGE_CHECKPOINT', ''
            ).strip()
            if (
                    os.environ.get('POLYBOT_DEPLOYMENT_CLI_INTERNAL') != '1'
                    or (PROJECT_ROOT / '.git').exists()
                    or args.immutable_image_checkpoint != image_checkpoint
                    or len(image_checkpoint) != 40
                    or any(
                        value not in '0123456789abcdef'
                        for value in image_checkpoint
                    )
                    or args.host_platform not in {'darwin', 'linux'}
                    or args.host_uid < 0):
                raise ContainerDoctorError(
                    'Immutable-image doctor invocation is invalid.'
                )
            run_options = {
                'git_probe': lambda _root: GitSnapshot(
                    checkpoint=image_checkpoint,
                    clean=True,
                ),
                'which': lambda name: (
                    '/usr/bin/docker' if name == 'docker' else None
                ),
                'host_platform': args.host_platform,
                'host_uid': args.host_uid,
            }
        report = run_doctor(PROJECT_ROOT, mode=args.mode, **run_options)
    except ContainerDoctorError as exc:
        print(f'Container deployment doctor refused: {exc}', file=sys.stderr)
        return 2
    print(report_json(report) if args.json else format_report(report))
    return 0 if report.ready else 2


if __name__ == '__main__':
    raise SystemExit(main())
