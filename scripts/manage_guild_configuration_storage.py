#!/usr/bin/env python3
"""Capture, plan, stage, apply, or verify static guild configuration storage.

``snapshot`` performs bounded read-only Discord HTTP requests. ``plan`` is
offline and connection-free. Production ``stage`` is an online, dormant write
that is allowed only while the bound runtime selector remains ``static``.
Production ``apply`` retains its maintenance acknowledgement. Both write paths
require a distinct digest-bound confirmation and their external approval.
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
from modules import beta_database_writer_lock  # noqa: E402


MAX_SNAPSHOT_BYTES = 512 * 1024
PRODUCTION_MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
DEFAULT_SNAPSHOT = (
    'logs/development/guild-configuration/discord-snapshot.json'
)
PRODUCTION_DEFAULT_SNAPSHOT = (
    'logs/production/guild-configuration/discord-snapshot.json'
)
PRODUCTION_GUILD_COUNT = 49
POLYCHAMPIONS_GUILD_ID = 447883341463814144
PCPLUS_GUILD_ID = 1289762588346814495
PRODUCTION_GLOBAL_LEADERBOARD_GUILD_IDS = frozenset({
    283436219780825088,
    POLYCHAMPIONS_GUILD_ID,
    814317488418193478,
    PCPLUS_GUILD_ID,
})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Guild configuration snapshot and storage tooling.'
    )
    operations = parser.add_subparsers(dest='operation', required=True)
    snapshot = operations.add_parser(
        'snapshot', help='capture bounded role/channel identity through Discord HTTP'
    )
    snapshot.add_argument('--output')
    for name, help_text in (
        ('plan', 'build an offline import plan from a captured snapshot'),
        (
            'stage',
            'create and import dormant production storage while authority is static',
        ),
        ('apply', 'apply the exact additive schema/import transaction'),
        ('verify', 'read and verify the exact stored schema/import'),
    ):
        operation = operations.add_parser(name, help=help_text)
        operation.add_argument('--snapshot')
        if name == 'plan':
            operation.add_argument(
                '--output',
                help='write the exact plan as a private JSON file',
            )
        if name in {'stage', 'apply'}:
            operation.add_argument('--confirm', required=True)
        if name == 'apply':
            operation.add_argument(
                '--production-maintenance',
                action='store_true',
                help=(
                    'acknowledge that the separately approved production '
                    'backup and zero-writer maintenance checks are complete'
                ),
            )
    return parser


def _profile():
    environment = os.environ.get('POLYBOT_ENV')
    if environment not in {
            storage.DEVELOPMENT_ENVIRONMENT,
            storage.PRODUCTION_ENVIRONMENT,
    }:
        raise storage.GuildConfigurationStorageError(
            'Set exact POLYBOT_ENV=development or POLYBOT_ENV=production.'
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


def _snapshot_directory(environment: str) -> Path:
    if environment not in {
            storage.DEVELOPMENT_ENVIRONMENT,
            storage.PRODUCTION_ENVIRONMENT,
    }:
        raise storage.GuildConfigurationStorageError(
            'Snapshot environment is not supported.'
        )
    return Path('logs') / environment / 'guild-configuration'


def _default_snapshot(environment: str) -> str:
    return (
        PRODUCTION_DEFAULT_SNAPSHOT
        if environment == storage.PRODUCTION_ENVIRONMENT
        else DEFAULT_SNAPSHOT
    )


def _max_snapshot_bytes(environment: str) -> int:
    _snapshot_directory(environment)
    return (
        PRODUCTION_MAX_SNAPSHOT_BYTES
        if environment == storage.PRODUCTION_ENVIRONMENT
        else MAX_SNAPSHOT_BYTES
    )


def _safe_relative_path(
    value: str,
    *,
    must_exist: bool,
    environment: str = storage.DEVELOPMENT_ENVIRONMENT,
) -> Path:
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
        candidate.relative_to(_snapshot_directory(environment))
    except ValueError as exc:
        raise storage.GuildConfigurationStorageError(
            f'Snapshot path must remain in logs/{environment}/guild-configuration.'
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


def _write_snapshot(
    path_value: str,
    value: dict[str, Any],
    *,
    environment: str = storage.DEVELOPMENT_ENVIRONMENT,
) -> Path:
    path = _safe_relative_path(
        path_value,
        must_exist=False,
        environment=environment,
    )
    payload = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8') + b'\n'
    if len(payload) > _max_snapshot_bytes(environment):
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


def _load_snapshot(
    path_value: str,
    *,
    environment: str = storage.DEVELOPMENT_ENVIRONMENT,
) -> dict[str, Any]:
    path = _safe_relative_path(
        path_value,
        must_exist=True,
        environment=environment,
    )
    payload = path.read_bytes()
    if len(payload) > _max_snapshot_bytes(environment):
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


def _production_guild_types(profile: Any) -> dict[int, str] | None:
    if profile.environment != storage.PRODUCTION_ENVIRONMENT:
        return None
    allowed = tuple(sorted(int(value) for value in profile.allowed_guild_ids))
    if (
            len(allowed) != PRODUCTION_GUILD_COUNT
            or POLYCHAMPIONS_GUILD_ID not in allowed
            or PCPLUS_GUILD_ID not in allowed
    ):
        raise storage.GuildConfigurationStorageError(
            'Production guild inventory differs from the reviewed 49-guild '
            'migration inventory.'
        )
    values = {guild_id: 'standard' for guild_id in allowed}
    values[POLYCHAMPIONS_GUILD_ID] = 'league'
    values[PCPLUS_GUILD_ID] = 'team'
    return values


def _bundle(profile: Any, target: storage.StorageTarget, snapshot_path: str):
    snapshot = _load_snapshot(
        snapshot_path,
        environment=profile.environment,
    )
    bundle = storage.build_import_bundle(
        target=target,
        server_settings=profile.server_settings,
        allowed_guild_ids=profile.allowed_guild_ids,
        discord_snapshot=snapshot,
        guild_type_overrides=_production_guild_types(profile),
    )
    if profile.environment == storage.PRODUCTION_ENVIRONMENT:
        _validate_production_bundle(bundle)
    return bundle


def _validate_production_bundle(
    bundle: storage.ImportBundle,
) -> dict[str, Any]:
    imports = tuple(bundle.imports)
    by_id = {value.guild_id: value.document for value in imports}
    if len(imports) != PRODUCTION_GUILD_COUNT or len(by_id) != len(imports):
        raise storage.GuildConfigurationStorageError(
            'Production import bundle is not the exact 49-guild inventory.'
        )
    team_ids = {
        guild_id for guild_id, document in by_id.items()
        if document.teams.allow_teams
    }
    league_ids = {
        guild_id for guild_id, document in by_id.items()
        if document.teams.require_teams
    }
    global_ids = {
        guild_id for guild_id, document in by_id.items()
        if document.visibility.include_in_global_leaderboard
    }
    if team_ids != {POLYCHAMPIONS_GUILD_ID, PCPLUS_GUILD_ID}:
        raise storage.GuildConfigurationStorageError(
            'Production import must enable persistent Teams only for '
            'PolyChampions and PCPLUS.'
        )
    if league_ids != {POLYCHAMPIONS_GUILD_ID}:
        raise storage.GuildConfigurationStorageError(
            'Production import must require Teams only for PolyChampions.'
        )
    if global_ids != PRODUCTION_GLOBAL_LEADERBOARD_GUILD_IDS:
        raise storage.GuildConfigurationStorageError(
            'Production global-leaderboard inventory differs from the '
            'reviewed four guilds.'
        )
    for guild_id, document in by_id.items():
        capabilities = set(document.command_capabilities)
        required = {'core_user', 'guild_admin', 'squad'}
        if not required.issubset(capabilities):
            raise storage.GuildConfigurationStorageError(
                f'Guild {guild_id} is missing a standard command capability.'
            )
        expected_team = guild_id in team_ids
        if ('team' in capabilities) != expected_team:
            raise storage.GuildConfigurationStorageError(
                f'Guild {guild_id} Team capability differs from its type.'
            )
        expected_league = guild_id == POLYCHAMPIONS_GUILD_ID
        if ({'league', 'house'} <= capabilities) != expected_league:
            raise storage.GuildConfigurationStorageError(
                f'Guild {guild_id} league capabilities differ from its type.'
            )
    return {
        'guild_count': len(imports),
        'standard_guild_count': len(imports) - len(team_ids),
        'team_guild_ids': sorted(team_ids - league_ids),
        'league_guild_ids': sorted(league_ids),
        'global_leaderboard_guild_ids': sorted(global_ids),
    }


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
    writer_lock = None
    try:
        profile = _profile()
        target = _target(profile)
        storage.validate_target(target)
        if args.operation == 'stage':
            if profile.environment != storage.PRODUCTION_ENVIRONMENT:
                raise storage.GuildConfigurationStorageError(
                    'Online static staging is supported only for production.'
                )
            if profile.guild_configuration_source != 'static':
                raise storage.GuildConfigurationStorageError(
                    'Online staging requires guild_configuration_source=static.'
                )
        if (
                args.operation == 'apply'
                and profile.environment == storage.PRODUCTION_ENVIRONMENT
                and not args.production_maintenance
        ):
            raise storage.GuildConfigurationStorageError(
                'Production apply requires --production-maintenance after '
                'separate approval, backup, and zero-writer verification.'
            )
        snapshot_path = (
            getattr(args, 'snapshot', None)
            or _default_snapshot(profile.environment)
        )
        if args.operation == 'snapshot':
            snapshot_path = args.output or _default_snapshot(profile.environment)
            snapshot = asyncio.run(_capture_snapshot(profile))
            storage.validate_discord_snapshot(
                snapshot,
                target=target,
                allowed_guild_ids=profile.allowed_guild_ids,
            )
            path = _write_snapshot(
                snapshot_path,
                snapshot,
                environment=profile.environment,
            )
            _emit({
                'status': 'captured',
                'path': str(path.relative_to(PROJECT_ROOT)),
                'guild_ids': list(profile.allowed_guild_ids),
                'database_connected': False,
                'discord_mutated': False,
            })
            return 0

        bundle = _bundle(profile, target, snapshot_path)
        if args.operation == 'plan':
            value = storage.bundle_to_mapping(bundle, target=target)
            if profile.environment == storage.PRODUCTION_ENVIRONMENT:
                value['production_migration_summary'] = (
                    _validate_production_bundle(bundle)
                )
            if args.output:
                path = _write_snapshot(
                    args.output,
                    value,
                    environment=profile.environment,
                )
                _emit({
                    'status': 'planned',
                    'path': str(path.relative_to(PROJECT_ROOT)),
                    'bundle_digest': bundle.bundle_digest,
                    'confirmation': value['confirmation'],
                    'online_static_staging_confirmation': value.get(
                        'online_static_staging_confirmation'
                    ),
                    'production_migration_summary': value.get(
                        'production_migration_summary'
                    ),
                    'database_connected': False,
                    'discord_connected': False,
                })
            else:
                _emit(value)
            return 0
        if args.operation in {'stage', 'apply'}:
            if profile.environment == storage.DEVELOPMENT_ENVIRONMENT:
                writer_lock = beta_database_writer_lock.BetaDatabaseWriterLock(
                    profile
                )
                writer_lock.acquire()
            production_mode = None
            if profile.environment == storage.PRODUCTION_ENVIRONMENT:
                production_mode = (
                    storage.PRODUCTION_MODE_ONLINE_STATIC_STAGE
                    if args.operation == 'stage'
                    else storage.PRODUCTION_MODE_MAINTENANCE
                )
            connection = _connection(profile, readonly=False)
            result = storage.apply_storage(
                connection,
                target=target,
                bundle=bundle,
                confirmation=args.confirm,
                production_mode=production_mode,
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
    except (
        RuntimeConfigurationError,
        storage.GuildConfigurationStorageError,
        beta_database_writer_lock.BetaDatabaseWriterLockError,
    ) as exc:
        print(f'Guild configuration operation refused: {exc}', file=sys.stderr)
        return 2
    except Exception as exc:
        print(f'Guild configuration operation failed: {exc}', file=sys.stderr)
        return 2
    finally:
        if connection is not None:
            connection.close()
        if writer_lock is not None:
            writer_lock.release()


if __name__ == '__main__':
    raise SystemExit(main())
