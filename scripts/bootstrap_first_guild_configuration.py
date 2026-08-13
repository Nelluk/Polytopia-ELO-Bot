#!/usr/bin/env python3
"""Capture, plan, or apply the first development guild configuration."""

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

from modules import guild_configuration_bootstrap as bootstrap  # noqa: E402
from modules import guild_configuration_storage as storage  # noqa: E402
from runtime_config import RuntimeConfigurationError, load_runtime_profile  # noqa: E402
from modules import beta_database_writer_lock  # noqa: E402
from scripts import manage_guild_configuration_storage as snapshots  # noqa: E402


DEFAULT_SNAPSHOT = snapshots.DEFAULT_SNAPSHOT


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='P11.5B first trusted-guild bootstrap for a fresh database.'
    )
    operations = parser.add_subparsers(dest='operation', required=True)
    for name, help_text in (
        ('snapshot', 'read the exact configured guild through Discord'),
        ('plan', 'print the database-free exact bootstrap plan'),
        ('apply', 'apply the exact fresh-database bootstrap transaction'),
    ):
        operation = operations.add_parser(name, help=help_text)
        operation.add_argument('--guild-id', required=True, type=int)
        operation.add_argument('--snapshot', default=DEFAULT_SNAPSHOT)
        if name == 'apply':
            operation.add_argument('--confirm', required=True)
    return parser


def _profile():
    if os.environ.get('POLYBOT_ENV') != storage.DEVELOPMENT_ENVIRONMENT:
        raise bootstrap.FirstGuildBootstrapError(
            'Set exact POLYBOT_ENV=development; P11.5B never uses production.'
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


def _require_single_guild(profile: Any, guild_id: int) -> None:
    if tuple(profile.allowed_guild_ids) != (guild_id,):
        raise bootstrap.FirstGuildBootstrapError(
            'The supplied guild ID must be the sole configured development guild.'
        )


def _plan(profile: Any, target: storage.StorageTarget, path: str):
    value = snapshots._load_snapshot(path)
    return bootstrap.build_first_guild_plan(
        target=target,
        allowed_guild_ids=profile.allowed_guild_ids,
        discord_snapshot=value,
    )


def _connection(profile: Any):
    import psycopg2

    connection = psycopg2.connect(
        dbname=profile.database_name,
        user=profile.database_user,
        password=profile.database_password,
        host=profile.database_host,
        port=profile.database_port,
        connect_timeout=10,
        options='-c statement_timeout=30000 -c lock_timeout=5000',
    )
    connection.set_session(
        readonly=False,
        autocommit=False,
        isolation_level='SERIALIZABLE',
    )
    return connection


def _print_plan(plan: bootstrap.FirstGuildBootstrapPlan) -> None:
    value = bootstrap.plan_to_mapping(plan)
    print('P11.5B first trusted-guild bootstrap plan')
    print(f'guild: {value["guild_id"]} ({value["guild_name"]})')
    print('database: polytopia_dev')
    print('role: polybot_dev')
    print('requires: complete relation-empty application schema')
    print('creates: base, draft, and delegation guild-configuration storage')
    print('activates: revision 1, generation 1, operator-only capability')
    print('Discord writes: none')
    print('application-command synchronization: disabled')
    print(f'document_sha256: {value["document_digest"]}')
    print(f'plan_sha256: {value["plan_digest"]}')
    print(f'confirmation: {value["confirmation"]}')


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    connection = None
    writer_lock = None
    try:
        profile = _profile()
        _require_single_guild(profile, args.guild_id)
        target = _target(profile)
        storage.validate_target(target)
        if args.operation == 'snapshot':
            value = asyncio.run(snapshots._capture_snapshot(profile))
            storage.validate_discord_snapshot(
                value,
                target=target,
                allowed_guild_ids=profile.allowed_guild_ids,
            )
            path = snapshots._write_snapshot(args.snapshot, value)
            print(
                'Captured one read-only Discord guild snapshot: '
                f'{path.relative_to(PROJECT_ROOT)}'
            )
            print('Discord writes: none')
            return 0

        plan = _plan(profile, target, args.snapshot)
        _print_plan(plan)
        if args.operation == 'plan':
            print('Plan only; no database connection or write was attempted.')
            return 0
        writer_lock = beta_database_writer_lock.BetaDatabaseWriterLock(profile)
        writer_lock.acquire()
        connection = _connection(profile)
        result = bootstrap.apply_first_guild_bootstrap(
            connection,
            target=target,
            plan=plan,
            confirmation=args.confirm,
        )
        print(
            'First trusted guild committed and verified: '
            f'{result.guild_id}; revision={result.revision}; '
            f'generation={result.generation}; '
            f'document_sha256={result.document_digest}'
        )
        print('No Discord application commands were synchronized.')
        return 0
    except (
        RuntimeConfigurationError,
        storage.GuildConfigurationStorageError,
        bootstrap.FirstGuildBootstrapError,
        beta_database_writer_lock.BetaDatabaseWriterLockError,
    ) as exc:
        print(f'P11.5B refused: {exc}', file=sys.stderr)
        return 2
    except Exception as exc:
        print(f'P11.5B operation failed: {exc}', file=sys.stderr)
        return 2
    finally:
        if connection is not None:
            connection.close()
        if writer_lock is not None:
            writer_lock.release()


if __name__ == '__main__':
    raise SystemExit(main())
