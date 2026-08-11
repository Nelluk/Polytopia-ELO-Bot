#!/usr/bin/env python3
"""Plan or explicitly install operator-backup release provenance.

The plan is read-only. Apply writes only the private ignored manifest consumed
by ``/operator database backup`` and requires the exact production identity,
clean reviewed checkpoint, deployed-script match, and acknowledgement token.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules import operator_backup


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Prepare production operator-backup release provenance.'
    )
    parser.add_argument(
        '--checkpoint',
        required=True,
        help='exact reviewed 40-character production release checkpoint',
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--validate', action='store_true')
    parser.add_argument(
        '--confirm',
        default='',
        help=(
            'production write acknowledgement; required value is '
            f'{operator_backup.RELEASE_MANIFEST_CONFIRMATION}'
        ),
    )
    return parser.parse_args(argv)


def main(argv=None, *, runtime=None) -> int:
    args = _parse_args(argv)
    if os.environ.get('POLYBOT_ENV') != operator_backup.PRODUCTION_ENVIRONMENT:
        print(
            'Release provenance refused: exact POLYBOT_ENV=production is '
            'required.',
            file=sys.stderr,
        )
        return 2
    frozen_runtime = runtime or operator_backup.capture_runtime()
    if (
        frozen_runtime.environment != operator_backup.PRODUCTION_ENVIRONMENT
        or frozen_runtime.database_name != operator_backup.PRODUCTION_DATABASE
        or frozen_runtime.project_root != operator_backup.PRODUCTION_ROOT
        or frozen_runtime.current_username != operator_backup.PRODUCTION_USER
    ):
        print(
            'Release provenance refused: the runtime is not the exact '
            'production identity.',
            file=sys.stderr,
        )
        return 2
    if args.validate:
        try:
            preflight = operator_backup.validate_runtime_sync(
                frozen_runtime.owner_id,
                frozen_runtime,
            )
        except operator_backup.BackupError as exc:
            print(f'Release provenance refused: {exc}', file=sys.stderr)
            return 2
        if preflight.release_checkpoint != args.checkpoint:
            print(
                'Release provenance refused: validation did not return the '
                'requested checkpoint.',
                file=sys.stderr,
            )
            return 2
        print(
            'Validated operator-backup release provenance for checkpoint '
            f'{preflight.release_checkpoint}.'
        )
        return 0

    try:
        manifest = operator_backup.build_release_manifest_sync(
            frozen_runtime,
            expected_checkpoint=args.checkpoint,
        )
    except operator_backup.BackupError as exc:
        print(f'Release provenance refused: {exc}', file=sys.stderr)
        return 2

    print(json.dumps(manifest.as_dict(), indent=2, sort_keys=True))
    if not args.apply:
        print('Plan only: no release manifest was written.')
        return 0
    if args.confirm != operator_backup.RELEASE_MANIFEST_CONFIRMATION:
        print(
            'Release provenance refused: exact apply acknowledgement is '
            'required.',
            file=sys.stderr,
        )
        return 2
    try:
        operator_backup.write_release_manifest_sync(frozen_runtime, manifest)
    except operator_backup.BackupError as exc:
        print(f'Release provenance refused: {exc}', file=sys.stderr)
        return 2
    print(f'Installed private release manifest: {frozen_runtime.release_manifest}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
