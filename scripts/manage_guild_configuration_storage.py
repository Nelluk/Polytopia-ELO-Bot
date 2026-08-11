#!/usr/bin/env python3
"""Capture, plan, apply, or verify P10.3 development guild configuration.

``snapshot`` performs bounded read-only Discord HTTP requests. ``plan`` is
offline and connection-free. ``apply`` and ``verify`` are fixed to the
development profile and PostgreSQL identity; apply additionally requires the
digest-bound confirmation printed by plan and a separately stopped beta.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules import guild_configuration_storage as storage  # noqa: E402
from runtime_config import (  # noqa: E402
    RuntimeConfigurationError,
    load_runtime_profile,
)


MAX_SNAPSHOT_BYTES = 512 * 1024
DEFAULT_SNAPSHOT = (
    'logs/development/guild-configuration/discord-snapshot.json'
)
SNAPSHOT_DIRECTORY = Path('logs/development/guild-configuration')


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='P10.3 development-only guild configuration storage tooling.'
    )
    operations = parser.add_subparsers(dest='operation', required=True)
    snapshot = operations.add_parser(
        'snapshot', help='capture bounded role/channel identity through Discord HTTP'
    )
    snapshot.add_argument('--output', default=DEFAULT_SNAPSHOT)
    for name, help_text in (
        ('plan', 'build an offline import plan from a captured snapshot'),
        ('apply', 'apply the exact additive schema/import transaction'),
        ('verify', 'read and verify the exact stored schema/import'),
    ):
        operation = operations.add_parser(name, help=help_text)
        operation.add_argument('--snapshot', default=DEFAULT_SNAPSHOT)
        if name == 'apply':
            operation.add_argument('--confirm', required=True)
    return parser


def _profile():
    if os.environ.get('POLYBOT_ENV') != storage.DEVELOPMENT_ENVIRONMENT:
        raise storage.GuildConfigurationStorageError(
            'Set exact POLYBOT_ENV=development; P10.3 never uses production.'
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


def _safe_relative_path(value: str, *, must_exist: bool) -> Path:
    if (
        not isinstance(value, str)
        or not value
        or '\x00' in value
        or Path(value).is_absolute()
        or Path(value).suffix != '.json'
    ):
        raise storage.GuildConfigurationStorageError(
            'Snapshot path must be one relative .json file inside the checkout.'
        )
    candidate = Path(value)
    if any(part in {'', '.', '..'} for part in candidate.parts):
        raise storage.GuildConfigurationStorageError('Snapshot path is unsafe.')
    try:
        candidate.relative_to(SNAPSHOT_DIRECTORY)
    except ValueError as exc:
        raise storage.GuildConfigurationStorageError(
            'Snapshot path must remain in logs/development/guild-configuration.'
        ) from exc
    path = (PROJECT_ROOT / candidate).resolve(strict=False)
    try:
        path.relative_to(PROJECT_ROOT.resolve())
    except ValueError as exc:
        raise storage.GuildConfigurationStorageError(
            'Snapshot path must remain inside the checkout.'
        ) from exc
    current = PROJECT_ROOT
    for part in candidate.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise storage.GuildConfigurationStorageError(
                'Snapshot path may not traverse a symlink.'
            )
    if must_exist and not path.is_file():
        raise storage.GuildConfigurationStorageError(
            f'Snapshot does not exist: {candidate}'
        )
    return path


def _write_snapshot(path_value: str, value: dict[str, Any]) -> Path:
    path = _safe_relative_path(path_value, must_exist=False)
    payload = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8') + b'\n'
    if len(payload) > MAX_SNAPSHOT_BYTES:
        raise storage.GuildConfigurationStorageError(
            'Captured Discord snapshot exceeds its byte bound.'
        )
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f'.{path.name}.tmp-{os.getpid()}')
    descriptor = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0),
            0o600,
        )
        with os.fdopen(descriptor, 'wb', closefd=True) as output:
            descriptor = None
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return path


def _load_snapshot(path_value: str) -> dict[str, Any]:
    path = _safe_relative_path(path_value, must_exist=True)
    payload = path.read_bytes()
    if len(payload) > MAX_SNAPSHOT_BYTES:
        raise storage.GuildConfigurationStorageError('Snapshot exceeds its byte bound.')
    try:
        value = json.loads(payload.decode('utf-8'))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise storage.GuildConfigurationStorageError(
            'Snapshot is not valid UTF-8 JSON.'
        ) from exc
    if not isinstance(value, dict):
        raise storage.GuildConfigurationStorageError('Snapshot must contain an object.')
    return value


def _object_id(value: Any) -> int:
    return int(getattr(value, 'id'))


def _object_name(value: Any) -> str:
    return str(getattr(value, 'name', ''))


def _channel_type(channel: Any) -> str:
    raw = getattr(channel, 'type', None)
    name = getattr(raw, 'name', None)
    return str(name if name else raw)


async def _capture_snapshot(profile: Any) -> dict[str, Any]:
    import discord

    client = discord.Client(intents=discord.Intents.none())
    await client.login(profile.discord_token)
    try:
        if _object_id(client.user) != profile.expected_bot_id:
            raise storage.GuildConfigurationStorageError(
                'Discord authenticated a different application identity.'
            )
        guild_values = []
        for guild_id in profile.allowed_guild_ids:
            guild = await client.fetch_guild(guild_id)
            roles = sorted(await guild.fetch_roles(), key=_object_id)
            channels = sorted(await guild.fetch_channels(), key=_object_id)
            guild_values.append({
                'guild_id': guild_id,
                'guild_name': _object_name(guild),
                'roles': [
                    {
                        'id': _object_id(role),
                        'name': _object_name(role),
                        'managed': bool(getattr(role, 'managed', False)),
                        'is_default': bool(role.is_default()),
                    }
                    for role in roles
                ],
                'channels': [
                    {
                        'id': _object_id(channel),
                        'name': _object_name(channel),
                        'type': _channel_type(channel),
                        'category_id': (
                            None
                            if getattr(channel, 'category_id', None) is None
                            else int(channel.category_id)
                        ),
                    }
                    for channel in channels
                ],
            })
        return {
            'schema_version': storage.SNAPSHOT_SCHEMA_VERSION,
            'kind': 'guild_configuration_discord_snapshot',
            'environment': profile.environment,
            'application_id': profile.expected_bot_id,
            'guilds': guild_values,
        }
    finally:
        await client.close()


def _bundle(profile: Any, target: storage.StorageTarget, snapshot_path: str):
    snapshot = _load_snapshot(snapshot_path)
    return storage.build_import_bundle(
        target=target,
        server_settings=profile.server_settings,
        allowed_guild_ids=profile.allowed_guild_ids,
        discord_snapshot=snapshot,
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


def _result_mapping(result: storage.StorageResult) -> dict[str, Any]:
    return {
        'schema_created': result.schema_created,
        'imported_guild_ids': list(result.imported_guild_ids),
        'unchanged_guild_ids': list(result.unchanged_guild_ids),
        'verified_guild_ids': list(result.verified_guild_ids),
        'bundle_digest': result.bundle_digest,
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
        if args.operation == 'snapshot':
            snapshot = asyncio.run(_capture_snapshot(profile))
            storage.validate_discord_snapshot(
                snapshot,
                target=target,
                allowed_guild_ids=profile.allowed_guild_ids,
            )
            path = _write_snapshot(args.output, snapshot)
            _emit({
                'status': 'captured',
                'path': str(path.relative_to(PROJECT_ROOT)),
                'guild_ids': list(profile.allowed_guild_ids),
                'database_connected': False,
                'discord_mutated': False,
            })
            return 0

        bundle = _bundle(profile, target, args.snapshot)
        if args.operation == 'plan':
            _emit(storage.bundle_to_mapping(bundle))
            return 0
        if args.operation == 'apply':
            connection = _connection(profile, readonly=False)
            result = storage.apply_storage(
                connection,
                target=target,
                bundle=bundle,
                confirmation=args.confirm,
            )
        else:
            connection = _connection(profile, readonly=True)
            result = storage.verify_storage(
                connection,
                target=target,
                bundle=bundle,
            )
        _emit(_result_mapping(result))
        return 0
    except (RuntimeConfigurationError, storage.GuildConfigurationStorageError) as exc:
        print(f'P10.3 refused: {exc}', file=sys.stderr)
        return 2
    except Exception as exc:
        print(f'P10.3 operation failed: {exc}', file=sys.stderr)
        return 2
    finally:
        if connection is not None:
            connection.close()


if __name__ == '__main__':
    raise SystemExit(main())
