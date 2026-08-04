#!/usr/bin/env python3
"""Inspect and plan the development wider-beta readiness state.

``discord-inventory`` uses the already-authenticated beta's protected local
socket.  ``database-inventory`` opens a separate read-only development
database connection.  ``validate`` and ``plan`` are offline and never apply a
Discord, command, filesystem, or database change.
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

from modules import beta_readiness  # noqa: E402
from modules.beta_operations import send_control_request  # noqa: E402
from runtime_config import RuntimeConfigurationError, load_runtime_profile  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Read-only development wider-beta readiness inventory and planning.'
    )
    parser.add_argument('--json', action='store_true', help='emit compact JSON')
    operations = parser.add_subparsers(dest='operation', required=True)
    operations.add_parser(
        'discord-inventory',
        help='request the bounded inventory from the authenticated beta socket',
    )
    database = operations.add_parser(
        'database-inventory',
        help='read the bounded development database inventory',
    )
    database.add_argument('--guild-id', type=int, default=beta_readiness.BETA_GUILD_ID)
    validate = operations.add_parser(
        'validate',
        help='validate one repository-backed desired-state manifest',
    )
    validate.add_argument('--manifest', required=True)
    plan = operations.add_parser(
        'plan',
        help='compare a manifest with supplied Discord and database snapshots',
    )
    plan.add_argument('--manifest', required=True)
    plan.add_argument('--discord-inventory', required=True)
    plan.add_argument('--database-inventory', required=True)
    return parser


def _emit(value: Any, *, as_json: bool) -> None:
    if as_json:
        # Keep the documented snapshot file byte size equal to the JSON
        # payload bound; a trailing print newline would make an exact-bound
        # inventory unloadable by the planner.
        sys.stdout.write(json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(',', ':'),
        ))
        return
    if isinstance(value, dict):
        print(json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2))
    else:
        print(value)


def _selected_profile():
    if os.environ.get('POLYBOT_ENV', '').strip() != 'development':
        raise RuntimeConfigurationError(
            'Set POLYBOT_ENV=development; readiness database/socket operations never use production.'
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
        label='readiness manifest',
        max_bytes=beta_readiness.MAX_MANIFEST_BYTES,
    )
    return beta_readiness.validate_readiness_manifest(raw)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.operation == 'discord-inventory':
            profile = _selected_profile()
            result = asyncio.run(send_control_request(
                profile,
                {'operation': 'readiness-inventory'},
            ))
            beta_readiness.validate_inventory_snapshot(
                result, kind='discord_guild_inventory'
            )
            _emit(dict(result), as_json=args.json)
            return 0

        if args.operation == 'database-inventory':
            profile = _selected_profile()
            result = beta_readiness.read_development_database_inventory(
                profile=profile,
                guild_id=args.guild_id,
            )
            _emit(result, as_json=args.json)
            return 0

        if args.operation == 'validate':
            manifest = _load_manifest(args.manifest)
            _emit({
                'schema_version': manifest['schema_version'],
                'status': 'valid',
                'target': manifest['target'],
                'unresolved': (
                    manifest['capabilities']['unresolved']
                    + manifest['database']['teams']['unresolved']
                    + manifest['database']['houses']['unresolved']
                    + manifest['database']['role_bindings']['unresolved']
                    + manifest['database']['fixtures']['unresolved']
                    + manifest['lifecycle']['cleanup']['unresolved']
                    + manifest['lifecycle']['rollback']['unresolved']
                    + manifest['smoke']['unresolved']
                ),
            }, as_json=args.json)
            return 0

        manifest = _load_manifest(args.manifest)
        discord_snapshot = beta_readiness.load_json_path(
            PROJECT_ROOT,
            args.discord_inventory,
            label='Discord inventory snapshot',
            max_bytes=beta_readiness.MAX_SNAPSHOT_BYTES,
        )
        database_snapshot = beta_readiness.load_json_path(
            PROJECT_ROOT,
            args.database_inventory,
            label='database inventory snapshot',
            max_bytes=beta_readiness.MAX_SNAPSHOT_BYTES,
        )
        result = beta_readiness.plan_readiness(
            manifest=manifest,
            discord_inventory=discord_snapshot,
            database_inventory=database_snapshot,
        )
        _emit(result, as_json=args.json)
        return 0
    except (
            RuntimeConfigurationError,
            beta_readiness.ReadinessError,
            ValueError,
    ) as exc:
        if args.json:
            print(json.dumps({'error': str(exc)}, ensure_ascii=True, sort_keys=True))
        else:
            print(f'Error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
