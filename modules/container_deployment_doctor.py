"""Read-only preflight for the development container deployment contract."""

from __future__ import annotations

import ast
import configparser
from dataclasses import dataclass
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tomllib
from typing import Callable, Mapping


MODES = ('bundled', 'external')
PASS = 'pass'
WARN = 'warn'
BLOCK = 'block'
_CHECKPOINT = re.compile(r'^[0-9a-f]{40}$')
_DEVELOPMENT_DATABASE = re.compile(
    r'(^|[_-])(dev|development|test|testing|sandbox)([_-]|$)',
    re.IGNORECASE,
)
_PLACEHOLDER_ID = 123456789012345678
_ALLOWED_ENV_KEYS = frozenset({
    'POLYBOT_BOT_IMAGE',
    'POLYBOT_PYTHON_IMAGE',
    'POLYBOT_UV_IMAGE',
    'POLYBOT_POSTGRES_IMAGE',
    'POLYBOT_SOURCE_CHECKPOINT',
    'POLYBOT_RUNTIME_UID',
    'POLYBOT_RUNTIME_GID',
    'POLYBOT_BOT_MEMORY_LIMIT',
    'POLYBOT_BOT_CPU_LIMIT',
    'POLYBOT_POSTGRES_MEMORY_LIMIT',
    'POLYBOT_POSTGRES_CPU_LIMIT',
    'POLYBOT_RECOVERY_UID',
    'POLYBOT_RECOVERY_GID',
})


class ContainerDoctorError(RuntimeError):
    """One deployment input or repository contract could not be inspected."""


@dataclass(frozen=True, slots=True)
class Finding:
    key: str
    status: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            'key': self.key,
            'status': self.status,
            'message': self.message,
        }


@dataclass(frozen=True, slots=True)
class GitSnapshot:
    checkpoint: str
    clean: bool


@dataclass(frozen=True, slots=True)
class DoctorReport:
    mode: str
    checkpoint: str
    findings: tuple[Finding, ...]
    commands: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not any(item.status == BLOCK for item in self.findings)

    def as_dict(self) -> dict[str, object]:
        return {
            'mode': self.mode,
            'checkpoint': self.checkpoint,
            'ready': self.ready,
            'findings': [item.as_dict() for item in self.findings],
            'commands': list(self.commands),
        }


def _finding(key: str, status: str, message: str) -> Finding:
    if status not in {PASS, WARN, BLOCK}:
        raise ValueError(f'Unknown doctor status: {status}')
    return Finding(key=key, status=status, message=message)


def _git_snapshot(project_root: Path) -> GitSnapshot:
    def run(*arguments: str) -> str:
        try:
            result = subprocess.run(
                ('git', *arguments),
                cwd=project_root,
                check=False,
                capture_output=True,
                env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'},
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ContainerDoctorError(
                f'Could not inspect Git checkpoint: {type(exc).__name__}.'
            ) from exc
        if result.returncode != 0:
            raise ContainerDoctorError(
                'Could not inspect Git checkpoint; run the doctor from one '
                'valid repository checkout.'
            )
        return result.stdout.strip()

    checkpoint = run('rev-parse', '--verify', 'HEAD')
    if not _CHECKPOINT.fullmatch(checkpoint):
        raise ContainerDoctorError('Git HEAD is not one exact lowercase SHA-1.')
    status_output = run('status', '--porcelain', '--untracked-files=all')
    return GitSnapshot(checkpoint=checkpoint, clean=not status_output)


def _read_toml(path: Path) -> Mapping[str, object]:
    try:
        if path.stat().st_size > 262_144:
            raise ContainerDoctorError('Container contract exceeds 256 KiB.')
        with path.open('rb') as source:
            value = tomllib.load(source)
    except ContainerDoctorError:
        raise
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ContainerDoctorError(
            f'Could not read container contract {path}: {type(exc).__name__}.'
        ) from exc
    required_strings = (
        'environment',
        'python_image',
        'uv_image',
        'postgres_image',
        'restart_supervisor_environment',
        'beta_control_environment',
        'beta_startup_sync_environment',
        'beta_checkpoint_environment',
        'image_checkpoint_environment',
        'source_checkpoint_environment',
        'bot_uid_environment',
        'bot_gid_environment',
        'bot_stop_signal',
        'database_name',
        'database_user',
        'bundled_database_host',
        'backup_directory',
        'backup_archive_prefix',
        'restore_database_name',
        'restore_database_host',
        'import_database_name',
        'import_database_host',
        'import_archive_prefix',
        'verified_archive_receipt_suffix',
    )
    required_integers = (
        'contract_version',
        'postgres_major',
        'restart_exit_status',
        'bot_stop_grace_seconds',
        'database_port',
        'beta_application_id',
        'beta_guild_id',
    )
    invalid = [
        key for key in required_strings
        if not isinstance(value.get(key), str) or not value[key]
    ]
    invalid.extend(
        key for key in required_integers
        if type(value.get(key)) is not int or int(value[key]) <= 0
    )
    policy = value.get('runtime_policy')
    expected_policy_keys = {
        'background_tasks_enabled',
        'api_enabled',
        'bullet_enabled',
        'startup_schema_changes',
        'startup_fixture_changes',
        'startup_discord_sync',
        'global_discord_sync',
    }
    if (
            value.get('contract_version') != 6
            or value.get('environment') != 'development'
            or not isinstance(policy, dict)
            or set(policy) != expected_policy_keys
            or any(setting is not False for setting in policy.values())):
        invalid.append('contract-policy')
    for key in ('required_bind_files', 'required_secret_files', 'persistent_volumes'):
        items = value.get(key)
        if (
                not isinstance(items, list)
                or not items
                or any(not isinstance(item, str) or not item for item in items)):
            invalid.append(key)
    exact_lists = {
        'required_bind_files': [
            'deploy/container/config.development.ini',
            'deploy/container/server_settings_dev.py',
        ],
        'required_secret_files': [
            'deploy/container/secrets/postgres-admin-password.txt',
            'deploy/container/secrets/polybot-database-password.txt',
        ],
        'persistent_volumes': [
            'postgres_data',
            'postgres_restore_data',
            'polybot_images',
            'polybot_logs',
        ],
    }
    for key, expected in exact_lists.items():
        if value.get(key) != expected:
            invalid.append(key)
    exact_scalars = {
        'postgres_major': 18,
        'bot_uid_environment': 'POLYBOT_RUNTIME_UID',
        'bot_gid_environment': 'POLYBOT_RUNTIME_GID',
        'restart_exit_status': 75,
        'restart_supervisor_environment': 'POLYBOT_RESTART_SUPERVISOR=compose',
        'beta_control_environment': 'POLYBOT_BETA_CONTROL=enabled',
        'beta_startup_sync_environment': 'POLYBOT_BETA_STARTUP_SYNC=disabled',
        'beta_checkpoint_environment': 'POLYBOT_BETA_CHECKPOINT',
        'beta_application_id': 479029527553638401,
        'beta_guild_id': 478571892832206869,
        'image_checkpoint_environment': 'POLYBOT_IMAGE_CHECKPOINT',
        'source_checkpoint_environment': 'POLYBOT_SOURCE_CHECKPOINT',
        'bot_stop_signal': 'SIGINT',
        'bot_stop_grace_seconds': 45,
        'database_name': 'polytopia_dev',
        'database_user': 'polybot_dev',
        'bundled_database_host': 'postgres',
        'database_port': 5432,
        'backup_directory': 'deploy/container/backups',
        'backup_archive_prefix': 'polybot-polytopia_dev',
        'restore_database_name': 'polytopia_restore_verify',
        'restore_database_host': 'restore-postgres',
        'import_database_name': 'polytopia_dev',
        'import_database_host': 'postgres',
        'import_archive_prefix': 'polybot-polytopia_dev',
        'verified_archive_receipt_suffix': '.verified',
    }
    for key, expected in exact_scalars.items():
        if value.get(key) != expected:
            invalid.append(key)
    expected_import_counts = {
        'guild_games': 71,
        'houses': 4,
        'guild_players': 44,
        'guild_teams': 15,
        'beta_fixture_games': 3,
        'showcase_games': 48,
        'showcase_players': 24,
    }
    if value.get('legacy_import_expected_counts') != expected_import_counts:
        invalid.append('legacy_import_expected_counts')
    if not str(value.get('postgres_image', '')).startswith('postgres:18.'):
        invalid.append('postgres_image')
    if invalid:
        raise ContainerDoctorError(
            'Container contract is incomplete or unsafe: '
            + ', '.join(sorted(set(invalid)))
        )
    return value


def _parse_env_file(path: Path) -> dict[str, str]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ContainerDoctorError(
            f'Required Compose environment file is missing: {path}'
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ContainerDoctorError(
            f'Compose environment must be one regular non-symlink file: {path}'
        )
    if metadata.st_size > 65_536:
        raise ContainerDoctorError(
            f'Compose environment exceeds the 64 KiB inspection limit: {path}'
        )
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except (OSError, UnicodeError) as exc:
        raise ContainerDoctorError(
            f'Could not read {path}: {type(exc).__name__}.'
        ) from exc
    values: dict[str, str] = {}
    for line_number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            raise ContainerDoctorError(
                f'{path} line {line_number} must be KEY=VALUE.'
            )
        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip()
        if not key or key in values:
            raise ContainerDoctorError(
                f'{path} line {line_number} has an empty or duplicate key.'
            )
        if key not in _ALLOWED_ENV_KEYS:
            raise ContainerDoctorError(
                f'{path} contains unsupported key {key!r}.'
            )
        if not value or value[0:1] in {'"', "'"} or '#' in value:
            raise ContainerDoctorError(
                f'{path} key {key!r} must use one plain nonempty value.'
            )
        values[key] = value
    return values


def _regular_private_file(
    path: Path,
    label: str,
    *,
    max_bytes: int = 1_048_576,
) -> tuple[Finding, str | None]:
    try:
        metadata = path.lstat()
    except OSError:
        return _finding(label, BLOCK, f'Required file is missing: {path}'), None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        return _finding(
            label,
            BLOCK,
            f'Required path must be one regular non-symlink file: {path}',
        ), None
    if metadata.st_size > max_bytes:
        return _finding(
            label,
            BLOCK,
            f'Sensitive file exceeds its bounded inspection limit: {path}',
        ), None
    permissions = stat.S_IMODE(metadata.st_mode)
    if permissions & 0o077:
        return _finding(
            label,
            BLOCK,
            f'Sensitive file must not grant group/other access: {path}',
        ), None
    try:
        value = path.read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        return _finding(
            label,
            BLOCK,
            f'Sensitive file must be readable UTF-8 text: {path}',
        ), None
    return _finding(label, PASS, f'Private regular file is present: {path}'), value


def _secret_value(path: Path, label: str) -> tuple[Finding, str | None]:
    finding, raw = _regular_private_file(path, label, max_bytes=16_384)
    if raw is None:
        return finding, None
    if '\x00' in raw or not raw or raw.endswith('\n\n'):
        return _finding(
            label,
            BLOCK,
            f'Secret must contain exactly one nonempty text line: {path}',
        ), None
    value = raw[:-1] if raw.endswith('\n') else raw
    if not value or '\n' in value or '\r' in value:
        return _finding(
            label,
            BLOCK,
            f'Secret must contain exactly one nonempty text line: {path}',
        ), None
    if len(value) < 16:
        return _finding(
            label,
            BLOCK,
            f'Secret must contain at least 16 characters: {path}',
        ), None
    return finding, value


def _backup_directory(
    path: Path,
    *,
    uid: int,
    gid: int,
    host_platform: str,
    host_uid: int,
) -> Finding:
    try:
        metadata = path.lstat()
    except OSError:
        return _finding(
            'backup-directory',
            BLOCK,
            f'Required off-volume backup directory is missing: {path}',
        )
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        return _finding(
            'backup-directory',
            BLOCK,
            f'Backup path must be one non-symlink directory: {path}',
        )
    permissions = stat.S_IMODE(metadata.st_mode)
    if host_platform == 'darwin':
        safe = metadata.st_uid == host_uid and permissions == 0o700
        success = (
            'Off-volume backup directory is owned by the invoking macOS user '
            'with mode 0700; the recovery job still requires a live Docker '
            'Desktop bind probe.'
        )
        failure = (
            'Off-volume backup directory on macOS must be owned by the '
            'invoking host user with mode 0700.'
        )
    else:
        safe = (
            metadata.st_uid == uid
            and metadata.st_gid == gid
            and permissions == 0o700
        )
        success = (
            'Off-volume backup directory has the exact recovery owner and '
            f'mode 0700: {path}'
        )
        failure = (
            'Off-volume backup directory must match the configured recovery '
            f'UID/GID and mode 0700: {path}'
        )
    return _finding(
        'backup-directory',
        BLOCK if not safe else (WARN if host_platform == 'darwin' else PASS),
        success if safe else failure,
    )


def _runtime_bind_ownership(
    paths: tuple[Path, ...],
    *,
    uid: int,
    gid: int,
    host_platform: str,
    host_uid: int,
) -> Finding:
    try:
        if host_platform == 'darwin':
            mismatched = [
                path for path in paths if path.lstat().st_uid != host_uid
            ]
        else:
            mismatched = [
                path for path in paths
                if path.lstat().st_uid != uid or path.lstat().st_gid != gid
            ]
    except OSError:
        mismatched = list(paths)
    if host_platform == 'darwin':
        success = (
            'Private bot configuration is owned by the invoking macOS user; '
            'confirm that Docker Desktop presents a mode-0600 bind to the '
            'configured non-root identity before starting the bot.'
        )
        failure = (
            'Private bot configuration files on macOS must be owned by the '
            'invoking host user.'
        )
    else:
        success = (
            'Private bot configuration ownership matches the configured '
            'non-root container UID/GID.'
        )
        failure = (
            'Private bot configuration files must share the configured '
            'non-root container UID/GID.'
        )
    return _finding(
        'runtime-bind-ownership',
        BLOCK if mismatched else (WARN if host_platform == 'darwin' else PASS),
        success if not mismatched else failure,
    )


def _read_config(path: Path) -> tuple[Finding, configparser.SectionProxy | None]:
    finding, raw = _regular_private_file(path, 'runtime-config')
    if raw is None:
        return finding, None
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(raw)
    except configparser.Error:
        return _finding(
            'runtime-config',
            BLOCK,
            f'Development runtime config is not valid INI: {path}',
        ), None
    return finding, parser['DEFAULT']


def _bool(defaults: configparser.SectionProxy, key: str) -> bool | None:
    try:
        return defaults.getboolean(key)
    except ValueError:
        return None


def _validate_config(
    defaults: configparser.SectionProxy,
    *,
    mode: str,
    contract: Mapping[str, object],
) -> list[Finding]:
    findings: list[Finding] = []
    expected = {
        'psql_db': str(contract['database_name']),
        'psql_user': str(contract['database_user']),
        'psql_port': str(contract['database_port']),
        'guild_configuration_source': 'database',
        'background_tasks_enabled': 'false',
        'api_enabled': 'false',
        'bullet_enabled': 'false',
        'image_root': 'data/development/images',
        'log_root': 'logs/development',
    }
    mismatches = [
        key for key, value in expected.items()
        if defaults.get(key, '').strip().lower() != value.lower()
    ]
    host = defaults.get('psql_host', '').strip()
    if mode == 'bundled':
        if host != str(contract['bundled_database_host']):
            mismatches.append('psql_host')
    elif not host or host in {'localhost', '127.0.0.1', 'postgres'}:
        mismatches.append('psql_host')
    for key in ('background_tasks_enabled', 'api_enabled', 'bullet_enabled'):
        if _bool(defaults, key) is not False and key not in mismatches:
            mismatches.append(key)
    if mismatches:
        findings.append(_finding(
            'development-profile',
            BLOCK,
            'Development profile has unsafe or mode-incompatible settings: '
            + ', '.join(sorted(set(mismatches))),
        ))
    else:
        findings.append(_finding(
            'development-profile',
            PASS,
            f'Development database/profile identity matches {mode} mode.',
        ))

    database = defaults.get('psql_db', '').strip()
    password = defaults.get('psql_password', '').strip()
    token = defaults.get('discord_key', '').strip()
    production_database = defaults.get('production_database_name', '').strip()
    required_values = {
        'discord_key': token,
        'expected_bot_id': defaults.get('expected_bot_id', '').strip(),
        'owner_id': defaults.get('owner_id', '').strip(),
        'psql_password': password,
        'production_database_name': production_database,
        'production_bot_id': defaults.get('production_bot_id', '').strip(),
        'production_guild_ids': defaults.get('production_guild_ids', '').strip(),
    }
    unsafe_values = [
        key for key, value in required_values.items()
        if not value or value.upper().startswith(('YOUR_', 'REPLACE_', 'CHANGEME'))
    ]
    if not _DEVELOPMENT_DATABASE.search(database):
        unsafe_values.append('psql_db')
    if database and database == production_database:
        unsafe_values.append('production_database_name')
    for key in ('expected_bot_id', 'owner_id', 'production_bot_id'):
        try:
            numeric = int(required_values[key])
        except ValueError:
            numeric = 0
        if numeric <= 0 or numeric == _PLACEHOLDER_ID:
            unsafe_values.append(key)
    if required_values['expected_bot_id'] == required_values['production_bot_id']:
        unsafe_values.append('expected_bot_id')
    try:
        production_guild_ids = tuple(
            int(value.strip())
            for value in required_values['production_guild_ids'].split(',')
            if value.strip()
        )
    except ValueError:
        production_guild_ids = ()
    if not production_guild_ids or any(value <= 0 for value in production_guild_ids):
        unsafe_values.append('production_guild_ids')
    if unsafe_values:
        findings.append(_finding(
            'identity-denylists',
            BLOCK,
            'Runtime identity, credentials, or production denylist remains '
            'missing/placeholder: ' + ', '.join(sorted(set(unsafe_values))),
        ))
    else:
        findings.append(_finding(
            'identity-denylists',
            PASS,
            'Development credentials are non-placeholder and production '
            'identity denylists are populated.',
        ))
    return findings


def _validate_server_settings(path: Path) -> Finding:
    finding, raw = _regular_private_file(path, 'server-settings')
    if raw is None:
        return finding
    try:
        tree = ast.parse(raw, filename=str(path))
    except SyntaxError:
        return _finding(
            'server-settings', BLOCK,
            f'Development server settings are not valid Python syntax: {path}',
        )
    assignments = {
        target.id: node.value
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (
            node.targets if isinstance(node, ast.Assign) else (node.target,)
        )
        if isinstance(target, ast.Name)
    }
    required = {
        'server_shortcut_ids',
        'application_command_capabilities',
        'server_list',
    }
    placeholders = any(
        isinstance(node, ast.Constant) and node.value == _PLACEHOLDER_ID
        for node in ast.walk(tree)
    )
    dictionaries = all(
        isinstance(assignments.get(name), ast.Dict)
        for name in required
    )
    if not dictionaries or placeholders:
        return _finding(
            'server-settings',
            BLOCK,
            'Development server settings retain placeholders or omit required '
            'top-level policy dictionaries.',
        )
    return _finding(
        'server-settings',
        PASS,
        'Development server settings parse without executing and contain the '
        'required non-placeholder policy dictionaries.',
    )


def _validate_assets(
    project_root: Path,
    *,
    mode: str,
    contract: Mapping[str, object],
) -> Finding:
    assets = project_root / 'deploy/container'
    compose_name = (
        'compose.development.yaml'
        if mode == 'bundled'
        else 'compose.development.external-db.yaml'
    )
    try:
        dockerfile = (assets / 'Dockerfile').read_text(encoding='utf-8')
        compose = (assets / compose_name).read_text(encoding='utf-8')
        backup_script = (
            (assets / 'backup-development-database.sh').read_text(encoding='utf-8')
            if mode == 'bundled' else ''
        )
        restore_script = (
            (assets / 'restore-development-database.sh').read_text(encoding='utf-8')
            if mode == 'bundled' else ''
        )
        import_script = (
            (assets / 'import-development-database.sh').read_text(encoding='utf-8')
            if mode == 'bundled' else ''
        )
    except (OSError, UnicodeError):
        return _finding(
            'repository-assets', BLOCK,
            'Dockerfile, selected Compose definition, or recovery asset is '
            'missing/unreadable.',
        )
    required_dockerfile = (
        f"ARG PYTHON_IMAGE={contract['python_image']}",
        f"ARG UV_IMAGE={contract['uv_image']}",
        'ARG POLYBOT_RUNTIME_UID=10001',
        'ARG POLYBOT_RUNTIME_GID=10001',
        'POLYBOT_IMAGE_CHECKPOINT=${POLYBOT_SOURCE_CHECKPOINT}',
        'COPY --chown=${POLYBOT_RUNTIME_UID}:${POLYBOT_RUNTIME_GID} . .',
        'USER ${POLYBOT_RUNTIME_UID}:${POLYBOT_RUNTIME_GID}',
        f"STOPSIGNAL {contract['bot_stop_signal']}",
        '["python", "bot.py", "--skip_tasks"]',
    )
    required_compose = (
        str(contract['python_image']),
        str(contract['uv_image']),
        'POLYBOT_SOURCE_CHECKPOINT:',
        'POLYBOT_RUNTIME_UID: ${POLYBOT_RUNTIME_UID:?',
        'POLYBOT_RUNTIME_GID: ${POLYBOT_RUNTIME_GID:?',
        'POLYBOT_RESTART_SUPERVISOR: compose',
        'POLYBOT_BETA_CONTROL: enabled',
        'POLYBOT_BETA_STARTUP_SYNC: disabled',
        'POLYBOT_BETA_CHECKPOINT: ${POLYBOT_SOURCE_CHECKPOINT:?',
        f'POLYBOT_BETA_APPLICATION_ID: "{contract["beta_application_id"]}"',
        f'POLYBOT_BETA_GUILD_ID: "{contract["beta_guild_id"]}"',
        f'POLYBOT_BETA_DATABASE: {contract["database_name"]}',
        f'POLYBOT_BETA_DATABASE_ROLE: {contract["database_user"]}',
        'read_only: true',
        'user: "${POLYBOT_RUNTIME_UID}:${POLYBOT_RUNTIME_GID}"',
        f'stop_signal: {contract["bot_stop_signal"]}',
        f'stop_grace_period: {contract["bot_stop_grace_seconds"]}s',
        'restart: "on-failure:5"',
    )
    missing = [value for value in required_dockerfile if value not in dockerfile]
    missing.extend(value for value in required_compose if value not in compose)
    if mode == 'bundled':
        for value in (
            str(contract['postgres_image']),
            'condition: service_healthy',
            'database-provision:',
            'profiles: ["tools"]',
            'postgres_data:/var/lib/postgresql',
            'database-backup:',
            'database-restore-drill:',
            'database-import:',
            'profiles: ["recovery"]',
            './backups:/backups',
            'restore-postgres:',
            'postgres_restore_data:/var/lib/postgresql',
        ):
            if value not in compose:
                missing.append(value)
        required_recovery_script = (
            'POLYBOT_ENV must be development.',
            str(contract['backup_archive_prefix']),
            str(contract['restore_database_name']),
            'pg_restore --list',
        )
        joined_recovery_scripts = backup_script + restore_script + import_script
        missing.extend(
            value for value in required_recovery_script
            if value not in joined_recovery_scripts
        )
        if str(contract['restore_database_host']) not in restore_script:
            missing.append(str(contract['restore_database_host']))
        for value in (
            str(contract['import_database_host']),
            str(contract['import_database_name']),
            str(contract['import_archive_prefix']),
            'IMPORT $TARGET_DATABASE $archive_digest',
            '--single-transaction',
        ):
            if value not in import_script:
                missing.append(value)
    else:
        for forbidden in ('\n  postgres:', 'postgres_data', 'database-provision'):
            if forbidden in compose:
                missing.append(f'forbidden:{forbidden.strip()}')
    for forbidden in (
        'polytopia2',
        'manage_application_commands.py',
        '--apply',
        '/var/run/docker.sock',
        'privileged: true',
        '\n    ports:',
    ):
        if forbidden in compose:
            missing.append(f'forbidden:{forbidden}')
    if missing:
        return _finding(
            'repository-assets',
            BLOCK,
            'Container assets disagree with the reviewed contract: '
            + ', '.join(missing),
        )
    return _finding(
        'repository-assets',
        PASS,
        (
            f'Dockerfile, {compose_name}, and recovery assets match the '
            'reviewed static contract.'
            if mode == 'bundled' else
            f'Dockerfile and {compose_name} match the reviewed static contract.'
        ),
    )


def _engine_findings(which: Callable[[str], str | None]) -> list[Finding]:
    docker = which('docker')
    standalone = which('docker-compose')
    findings: list[Finding] = []
    if docker:
        findings.append(_finding(
            'docker-cli', PASS, f'Docker CLI executable found: {docker}',
        ))
        findings.append(_finding(
            'compose-plugin',
            WARN,
            'Docker Compose plugin presence/version was not executed by this '
            'read-only doctor; the printed `docker compose ... config` command '
            'is the next explicit proof.',
        ))
    else:
        findings.append(_finding(
            'docker-cli',
            BLOCK,
            'Docker CLI executable was not found on PATH; no container command '
            'was attempted.',
        ))
    if standalone:
        findings.append(_finding(
            'standalone-compose',
            WARN,
            f'Legacy standalone docker-compose exists at {standalone}, but the '
            'reviewed flow requires the Docker Compose plugin syntax.',
        ))
    return findings


def reviewed_commands(mode: str) -> tuple[str, ...]:
    if mode not in MODES:
        raise ValueError(f'Unknown mode: {mode}')
    compose_file = (
        'deploy/container/compose.development.yaml'
        if mode == 'bundled'
        else 'deploy/container/compose.development.external-db.yaml'
    )
    base = f'docker compose --env-file deploy/container/.env --file {compose_file}'
    commands = [
        f'{base} --profile tools config',
        f'{base} build bot',
    ]
    if mode == 'bundled':
        commands.extend((
            f'{base} up -d postgres',
            f'{base} --profile tools run --rm database-provision',
        ))
    commands.extend((
        f'{base} --profile tools run --rm schema',
        f'{base} --profile tools run --rm schema --apply --confirm EXACT_TOKEN_FROM_PLAN',
        f'{base} up -d bot',
        f'{base} logs --tail 100 bot',
    ))
    return tuple(commands)


def run_doctor(
    project_root: Path,
    *,
    mode: str,
    which: Callable[[str], str | None] = shutil.which,
    git_probe: Callable[[Path], GitSnapshot] = _git_snapshot,
    host_platform: str = sys.platform,
    host_uid: int | None = None,
) -> DoctorReport:
    if mode not in MODES:
        raise ContainerDoctorError(
            f'Mode must be one of: {", ".join(MODES)}.'
        )
    root = Path(project_root).resolve()
    if host_uid is None:
        host_uid = os.getuid()
    contract = _read_toml(root / 'deploy/container/container-contract.toml')
    findings: list[Finding] = []

    try:
        git_state = git_probe(root)
    except ContainerDoctorError as exc:
        git_state = GitSnapshot(checkpoint='not-available', clean=False)
        findings.append(_finding('git-checkpoint', BLOCK, str(exc)))
    else:
        findings.append(_finding(
            'git-checkpoint',
            PASS if git_state.clean else BLOCK,
            (
                f'Git checkout is clean at {git_state.checkpoint}.'
                if git_state.clean
                else f'Git checkout at {git_state.checkpoint} is not clean.'
            ),
        ))

    findings.append(_validate_assets(root, mode=mode, contract=contract))
    env_path = root / 'deploy/container/.env'
    try:
        env_values = _parse_env_file(env_path)
    except ContainerDoctorError as exc:
        env_values = {}
        findings.append(_finding('compose-env', BLOCK, str(exc)))
    else:
        expected_images = {
            'POLYBOT_PYTHON_IMAGE': str(contract['python_image']),
            'POLYBOT_UV_IMAGE': str(contract['uv_image']),
        }
        if mode == 'bundled':
            expected_images['POLYBOT_POSTGRES_IMAGE'] = str(
                contract['postgres_image']
            )
        mismatched = [
            key for key, value in expected_images.items()
            if env_values.get(key) != value
        ]
        checkpoint = env_values.get('POLYBOT_SOURCE_CHECKPOINT', '')
        if checkpoint != git_state.checkpoint or not _CHECKPOINT.fullmatch(checkpoint):
            mismatched.append('POLYBOT_SOURCE_CHECKPOINT')
        for key in ('POLYBOT_RUNTIME_UID', 'POLYBOT_RUNTIME_GID'):
            value = env_values.get(key, '')
            if not value.isdigit() or int(value) <= 0:
                mismatched.append(key)
        if mode == 'bundled':
            for key in ('POLYBOT_RECOVERY_UID', 'POLYBOT_RECOVERY_GID'):
                value = env_values.get(key, '')
                if not value.isdigit() or int(value) <= 0:
                    mismatched.append(key)
        findings.append(_finding(
            'compose-env',
            BLOCK if mismatched else PASS,
            (
                'Compose environment disagrees with the reviewed images or '
                'clean Git checkpoint: ' + ', '.join(mismatched)
                if mismatched else
                'Compose environment pins the reviewed images and exact clean '
                'Git checkpoint.'
            ),
        ))
        if mode == 'bundled':
            recovery_uid = env_values.get('POLYBOT_RECOVERY_UID', '')
            recovery_gid = env_values.get('POLYBOT_RECOVERY_GID', '')
            if recovery_uid.isdigit() and recovery_gid.isdigit():
                findings.append(_backup_directory(
                    root / str(contract['backup_directory']),
                    uid=int(recovery_uid),
                    gid=int(recovery_gid),
                    host_platform=host_platform,
                    host_uid=host_uid,
                ))
            else:
                findings.append(_finding(
                    'backup-directory',
                    BLOCK,
                    'Backup directory ownership cannot be checked until the '
                    'recovery UID/GID are valid.',
                ))

        runtime_uid = env_values.get('POLYBOT_RUNTIME_UID', '')
        runtime_gid = env_values.get('POLYBOT_RUNTIME_GID', '')
        if runtime_uid.isdigit() and runtime_gid.isdigit():
            findings.append(_runtime_bind_ownership(
                (
                    root / 'deploy/container/config.development.ini',
                    root / 'deploy/container/server_settings_dev.py',
                ),
                uid=int(runtime_uid),
                gid=int(runtime_gid),
                host_platform=host_platform,
                host_uid=host_uid,
            ))
        else:
            findings.append(_finding(
                'runtime-bind-ownership',
                BLOCK,
                'Private bot configuration ownership cannot be checked until '
                'the runtime UID/GID are valid.',
            ))

    config_path = root / 'deploy/container/config.development.ini'
    config_finding, defaults = _read_config(config_path)
    findings.append(config_finding)
    if defaults is not None:
        findings.extend(_validate_config(defaults, mode=mode, contract=contract))
    findings.append(_validate_server_settings(
        root / 'deploy/container/server_settings_dev.py'
    ))

    if mode == 'bundled':
        admin_finding, admin_password = _secret_value(
            root / 'deploy/container/secrets/postgres-admin-password.txt',
            'postgres-admin-secret',
        )
        app_finding, app_password = _secret_value(
            root / 'deploy/container/secrets/polybot-database-password.txt',
            'database-app-secret',
        )
        findings.extend((admin_finding, app_finding))
        config_password = defaults.get('psql_password', '').strip() if defaults else ''
        if admin_password and app_password and config_password:
            matches = hmac.compare_digest(app_password, config_password)
            distinct = not hmac.compare_digest(admin_password, app_password)
            findings.append(_finding(
                'secret-agreement',
                PASS if matches and distinct else BLOCK,
                (
                    'Application config/secret agree and the administrative '
                    'password is distinct.'
                    if matches and distinct else
                    'Application config/secret must agree and the administrative '
                    'password must be distinct.'
                ),
            ))
        else:
            findings.append(_finding(
                'secret-agreement', BLOCK,
                'Secret agreement cannot be verified until every private input '
                'is valid.',
            ))

    findings.extend(_engine_findings(which))
    return DoctorReport(
        mode=mode,
        checkpoint=git_state.checkpoint,
        findings=tuple(findings),
        commands=reviewed_commands(mode),
    )


def format_report(report: DoctorReport) -> str:
    lines = [
        f'Container deployment doctor: {"READY" if report.ready else "BLOCKED"}',
        f'mode: {report.mode}',
        f'checkpoint: {report.checkpoint}',
        'checks:',
    ]
    for item in report.findings:
        lines.append(f'  [{item.status.upper()}] {item.key}: {item.message}')
    lines.append('reviewed next commands (not executed):')
    lines.extend(f'  {command}' for command in report.commands)
    return '\n'.join(lines)


def report_json(report: DoctorReport) -> str:
    return json.dumps(report.as_dict(), indent=2, sort_keys=True)
