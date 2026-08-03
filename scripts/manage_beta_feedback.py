#!/usr/bin/env python3
"""Read-only operator utility for the development beta feedback JSONL stream."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping

# Direct ``python scripts/manage_beta_feedback.py`` execution places the
# scripts directory, rather than the checkout, on sys.path.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from modules.beta_feedback import (
    FeedbackReadResult,
    FeedbackStorageError,
    feedback_paths,
    read_feedback_records,
)
from runtime_config import RuntimeConfigurationError, load_runtime_profile


REPORT_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]{20,}$')


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Read beta feedback from the development JSONL store.',
    )
    parser.add_argument(
        '--json',
        action='store_true',
        help='emit machine-readable JSON instead of concise text',
    )
    operations = parser.add_subparsers(dest='operation', required=True)
    list_parser = operations.add_parser('list', help='list recent reports')
    list_parser.add_argument('--limit', type=int, default=50)
    show_parser = operations.add_parser('show', help='show one report')
    show_parser.add_argument('--report-id', required=True)
    search_parser = operations.add_parser('search', help='search report text fields')
    search_parser.add_argument('query')
    search_parser.add_argument('--limit', type=int, default=50)
    return parser


def _selected_profile():
    if os.environ.get('POLYBOT_ENV', '').strip() != 'development':
        raise RuntimeConfigurationError(
            'Set POLYBOT_ENV=development; beta feedback is never read from production.'
        )
    return load_runtime_profile(create_directories=False)


def _record_dict(record: Mapping[str, Any]) -> dict[str, Any]:
    return dict(record)


def _compact(record: Mapping[str, Any]) -> str:
    return (
        f"{record['timestamp_utc']} {record['report_id']} "
        f"[{record['category']}/{record['source']}] "
        f"{record['summary']} ({record['requester_display_name']})"
    )


def _bounded_limit(value: int) -> int:
    if value < 1 or value > 1000:
        raise ValueError('--limit must be between 1 and 1000.')
    return value


def _result_payload(result: FeedbackReadResult) -> dict[str, Any]:
    return {
        'present': result.present,
        'records': [_record_dict(record) for record in result.records],
        'issues': [
            {
                'line': issue.line_number,
                'kind': issue.kind,
                'message': issue.message,
            }
            for issue in result.issues
        ],
    }


def _print_human(operation: str, result: FeedbackReadResult) -> None:
    if operation == 'show':
        if result.records:
            print(json.dumps(
                _record_dict(result.records[0]),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ))
        else:
            print('No matching beta feedback report.')
    elif result.records:
        for record in reversed(result.records):
            print(_compact(record))
    elif result.present:
        print('No matching beta feedback reports.')
    else:
        print('No beta feedback reports recorded.')

    if result.issues:
        print(
            f'Warning: ignored {len(result.issues)} malformed/truncated '
            'JSONL line(s).',
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        profile = _selected_profile()
        # This call is deliberately read-only and confirms the same path gate
        # used by the writer before attempting to open the JSONL file.
        feedback_paths(profile, create=False)
        if args.operation == 'list':
            limit = _bounded_limit(args.limit)
            result = read_feedback_records(profile, limit=limit)
        elif args.operation == 'show':
            if not REPORT_ID_PATTERN.fullmatch(args.report_id):
                raise ValueError('The report ID format is invalid.')
            result = read_feedback_records(profile, report_id=args.report_id)
        else:
            limit = _bounded_limit(args.limit)
            result = read_feedback_records(profile, query=args.query, limit=limit)
    except (RuntimeConfigurationError, FeedbackStorageError, ValueError) as exc:
        if args.json:
            print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        else:
            print(f'Error: {exc}', file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(_result_payload(result), ensure_ascii=False, sort_keys=True))
    else:
        _print_human(args.operation, result)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
