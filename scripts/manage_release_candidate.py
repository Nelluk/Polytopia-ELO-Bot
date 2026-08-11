#!/usr/bin/env python3
"""Inspect or strictly validate one non-secret M7/R-002 evidence record."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules import release_candidate  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Validate one exact modernization release-candidate record.',
    )
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--manifest', required=True)
    parser.add_argument(
        'operation', choices=('inspect', 'validate', 'require-ready'),
    )
    return parser


def _path(value: str) -> Path:
    candidate = Path(value)
    if (
            candidate.is_absolute()
            or len(candidate.parts) != 2
            or candidate.parts[0] != 'release-candidate-manifests'
            or candidate.suffix != '.json'
            or any(part in {'.', '..'} for part in candidate.parts)):
        raise release_candidate.ReleaseCandidateError(
            '--manifest must name one JSON file in release-candidate-manifests.'
        )
    selected = PROJECT_ROOT / candidate
    manifest_root = (PROJECT_ROOT / 'release-candidate-manifests').resolve()
    if selected.is_symlink() or selected.resolve().parent != manifest_root:
        raise release_candidate.ReleaseCandidateError(
            'The release-candidate manifest path is not a direct regular file.'
        )
    return selected


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = release_candidate.load(_path(args.manifest))
        if args.operation != 'inspect':
            release_candidate.verify_repository(manifest, PROJECT_ROOT)
        result = release_candidate.summary(manifest)
        if args.operation == 'require-ready' and result['blockers']:
            if args.json:
                print(json.dumps(result, sort_keys=True))
            else:
                print('Release candidate is blocked:', file=sys.stderr)
                for blocker in result['blockers']:
                    print(f'- {blocker}', file=sys.stderr)
            return 3
        if args.json:
            print(json.dumps(result, sort_keys=True))
        else:
            print(f"release_id: {result['release_id']}")
            print(f"candidate_sha: {result['candidate_sha']}")
            print(f"rollback_sha: {result['rollback_sha']}")
            print(f"ready: {result['ready']}")
            for name, status in result['gates'].items():
                print(f'{name}: {status}')
        return 0
    except release_candidate.ReleaseCandidateError as exc:
        if args.json:
            print(json.dumps({'error': str(exc)}, sort_keys=True))
        else:
            print(f'Error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
