#!/usr/bin/env python3
"""Validate and explicitly deliver reviewed development-beta releases.

``init``, ``prepare``, and ``validate`` are offline.  ``deliver`` and
``resolve-tester-role`` send one local request to the already-authenticated
beta process over its protected Unix socket; this utility never creates a
Discord client of its own.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules.beta_operations import (  # noqa: E402
    BetaOperationsError,
    assert_clean_checkout,
    current_checkpoint,
    init_release_draft,
    load_prepared_release_manifest,
    prepare_release_manifest,
    send_control_request,
)
from runtime_config import RuntimeConfigurationError, load_runtime_profile  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Guarded development-beta release and tester-role operations.',
    )
    parser.add_argument('--json', action='store_true', help='emit JSON output')
    operations = parser.add_subparsers(dest='operation', required=True)
    init = operations.add_parser(
        'init',
        help='copy the tracked schema template into a private release draft',
    )
    init.add_argument('--release-id', required=True)
    prepare = operations.add_parser(
        'prepare',
        help='inject the clean checkout HEAD into an ignored operational manifest',
    )
    prepare.add_argument('--manifest', required=True)
    for name, help_text in (
            ('validate', 'validate a prepared manifest against the current checkout'),
            ('deliver', 'deliver one explicit prepared release through the running beta'),
    ):
        command = operations.add_parser(name, help=help_text)
        command.add_argument('--manifest', required=True)
    operations.add_parser(
        'resolve-tester-role',
        help='resolve and persist the exact testers role through the running beta',
    )
    operations.add_parser('status', help='read local release idempotency state')
    return parser


def _selected_profile():
    if os.environ.get('POLYBOT_ENV', '').strip() != 'development':
        raise RuntimeConfigurationError(
            'Set POLYBOT_ENV=development; release operations never use production.'
        )
    return load_runtime_profile(
        project_root=PROJECT_ROOT,
        environ=os.environ,
        create_directories=False,
    )


def _manifest_path(value: str) -> Path:
    candidate = Path(value)
    if (
            not value
            or candidate.is_absolute()
            or any(part in {'.', '..'} for part in candidate.parts)
            or candidate.suffix != '.json'
            or candidate.name != candidate.parts[-1]):
        raise ValueError('--manifest must name one direct operational manifest file.')
    return candidate


def _emit(value: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    elif isinstance(value, dict):
        for key, item in value.items():
            print(f'{key}: {item}')
    else:
        print(value)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        profile = _selected_profile()
        if args.operation == 'init':
            assert_clean_checkout(PROJECT_ROOT)
            checkpoint = current_checkpoint(PROJECT_ROOT)
            draft_path = init_release_draft(profile, args.release_id)
            _emit({
                'status': 'initialized',
                'release_id': draft_path.stem,
                'checkpoint': checkpoint,
                'draft_path': str(draft_path),
            }, as_json=args.json)
            return 0
        if args.operation in {'prepare', 'validate', 'deliver'}:
            assert_clean_checkout(PROJECT_ROOT)
            checkpoint = current_checkpoint(PROJECT_ROOT)
            manifest_path = _manifest_path(args.manifest)
            if args.operation == 'prepare':
                prepared = prepare_release_manifest(
                    profile,
                    manifest_path,
                    current_checkpoint=checkpoint,
                )
                _emit({
                    'status': prepared.status,
                    'release_id': prepared.manifest.release_id,
                    'expected_checkpoint': prepared.manifest.expected_checkpoint,
                    'fingerprint': prepared.fingerprint,
                    'draft_path': str(prepared.draft_path),
                    'prepared_path': str(prepared.prepared_path),
                }, as_json=args.json)
                return 0
            manifest = load_prepared_release_manifest(
                profile,
                manifest_path,
                current_checkpoint=checkpoint,
            )
            if args.operation == 'validate':
                _emit({
                    'status': 'valid',
                    'release_id': manifest.release_id,
                    'expected_checkpoint': manifest.expected_checkpoint,
                    'ping_testers': manifest.ping_testers,
                }, as_json=args.json)
                return 0
            result = asyncio.run(send_control_request(
                profile,
                {'operation': 'deliver', 'manifest': manifest.as_dict()},
            ))
            _emit(dict(result), as_json=args.json)
            return 0
        if args.operation == 'resolve-tester-role':
            result = asyncio.run(send_control_request(
                profile,
                {'operation': 'resolve-tester-role'},
            ))
            _emit(dict(result), as_json=args.json)
            return 0
        result = asyncio.run(send_control_request(
            profile,
            {'operation': 'status'},
        ))
        _emit(dict(result), as_json=args.json)
        return 0
    except (RuntimeConfigurationError, BetaOperationsError, ValueError) as exc:
        if args.json:
            print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        else:
            print(f'Error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
