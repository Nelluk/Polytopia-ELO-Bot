#!/usr/bin/env python3
"""Plan, apply, or verify P10.6b1 development draft storage."""

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

from modules import guild_configuration_draft_storage as drafts  # noqa: E402
from modules import guild_configuration_storage as storage  # noqa: E402
from runtime_config import (  # noqa: E402
    RuntimeConfigurationError,
    load_runtime_profile,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='P10.6b1 development-only guild configuration draft schema.'
    )
    operations = parser.add_subparsers(dest='operation', required=True)
    operations.add_parser('plan', help='print the connection-free additive plan')
    apply = operations.add_parser(
        'apply',
        help='apply the exact additive draft-table transaction',
    )
    apply.add_argument('--confirm', required=True)
    operations.add_parser('verify', help='verify the exact draft-table schema')
    return parser


def _profile():
    if os.environ.get('POLYBOT_ENV') != storage.DEVELOPMENT_ENVIRONMENT:
        raise drafts.GuildConfigurationDraftStorageError(
            'Set exact POLYBOT_ENV=development; P10.6b1 never uses production.'
        )
    return load_runtime_profile(
        project_root=PROJECT_ROOT,
        environ=os.environ,
        create_directories=False,
    )


def _target(profile: Any) -> storage.StorageTarget:
    return storage.StorageTarget(
        environment=profile.environment,
        database_name=profile.database_name,
        database_user=profile.database_user,
        expected_application_id=profile.expected_bot_id,
        background_tasks_enabled=profile.background_tasks_enabled,
        api_enabled=profile.api_enabled,
        bullet_enabled=profile.bullet_enabled,
    )


def _connection(profile: Any, *, readonly: bool):
    import psycopg2

    connection = psycopg2.connect(
        dbname=profile.database_name,
        user=profile.database_user,
        password=profile.database_password,
        host=profile.database_host,
        port=profile.database_port,
    )
    if readonly:
        connection.set_session(readonly=True, autocommit=True)
    return connection


def _result_mapping(result: drafts.DraftSchemaResult) -> dict[str, Any]:
    return {
        'schema_created': result.schema_created,
        'schema_version': result.schema_version,
        'statement_digest': result.statement_digest,
        'active_configuration_changed': False,
    }


def _emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    connection = None
    try:
        profile = _profile()
        target = _target(profile)
        storage.validate_target(target)
        plan = drafts.draft_schema_plan(target)
        if args.operation == 'plan':
            _emit(drafts.plan_to_mapping(plan))
            return 0
        connection = _connection(
            profile,
            readonly=args.operation == 'verify',
        )
        if args.operation == 'apply':
            result = drafts.apply_draft_schema(
                connection,
                target=target,
                plan=plan,
                confirmation=args.confirm,
            )
        else:
            result = drafts.verify_draft_schema(
                connection,
                target=target,
            )
        _emit(_result_mapping(result))
        return 0
    except (
        RuntimeConfigurationError,
        storage.GuildConfigurationStorageError,
        drafts.GuildConfigurationDraftStorageError,
    ) as exc:
        print(f'P10.6b1 refused: {exc}', file=sys.stderr)
        return 2
    except Exception as exc:
        print(f'P10.6b1 operation failed: {exc}', file=sys.stderr)
        return 2
    finally:
        if connection is not None:
            connection.close()


if __name__ == '__main__':
    raise SystemExit(main())
