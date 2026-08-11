"""Fail-closed operational controls for the development beta.

This module deliberately contains no database imports.  The durable beta
launcher validates the development-only runtime and holds one process lock;
the release controller accepts explicit local requests from an operator and
uses the already-authenticated bot for the one Discord post.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import unicodedata
from typing import Any, Mapping, Sequence

import discord

from modules import beta_readiness
from runtime_config import RuntimeProfile


BETA_GUILD_ID = 478571892832206869
BETA_APPLICATION_ID = 479029527553638401
BETA_DATABASE_NAME = 'polytopia_dev'
BETA_DATABASE_ROLE = 'polybot_dev'
BETA_PUBLIC_RELEASE_CHANNEL_ID = 481779940124000256
BETA_PUBLIC_RELEASE_CHANNEL_NAME = 'todo-and-changelog'
BETA_STAFFHELP_MIRROR_CHANNEL_ID = 480078679930830849
BETA_STAFFHELP_MIRROR_CHANNEL_NAME = 'admin-spam'
BETA_TESTER_ROLE_NAME = 'testers'
BETA_CONTROL_ENV = 'POLYBOT_BETA_CONTROL'
BETA_STARTUP_SYNC_ENV = 'POLYBOT_BETA_STARTUP_SYNC'
BETA_CHECKPOINT_ENV = 'POLYBOT_BETA_CHECKPOINT'
BETA_STATE_DIRECTORY = 'beta-operations'
BETA_MANIFEST_DIRECTORY = 'release-manifests'
BETA_DRAFT_DIRECTORY = 'drafts'
BETA_PREPARED_DIRECTORY = 'prepared'
BETA_TEMPLATE_FILENAME = 'template.json'
BETA_CONTROL_SOCKET = 'release-control.sock'
BETA_RELEASE_STATE = 'release-state.json'
BETA_TESTER_ROLE_STATE = 'tester-role.json'
BETA_WRITER_LOCK = 'beta-writer.lock'

MANIFEST_SCHEMA_VERSION = 1
RELEASE_STATE_SCHEMA_VERSION = 1
ROLE_STATE_SCHEMA_VERSION = 1
DRAFT_CHECKPOINT = '0' * 40
MAX_RELEASE_ID_LENGTH = 64
MAX_TITLE_LENGTH = 120
MAX_SUMMARY_LENGTH = 500
MAX_CHANGED_COMMANDS = 12
MAX_COMMAND_LENGTH = 80
MAX_LIMITATIONS = 8
MAX_LIMITATION_LENGTH = 240
MAX_SMOKE_TESTS = 12
MAX_SMOKE_TEST_LENGTH = 200
MAX_NOTIFY_USERS = 5
MAX_ANNOUNCEMENT_LENGTH = 1900
MAX_HISTORY_SCAN = 100
MAX_SOCKET_REQUEST_BYTES = 64 * 1024
MAX_SOCKET_RESPONSE_OVERHEAD_BYTES = 1024
MAX_SOCKET_RESPONSE_BYTES = (
    beta_readiness.MAX_SNAPSHOT_BYTES + MAX_SOCKET_RESPONSE_OVERHEAD_BYTES
)

_RELEASE_ID = re.compile(r'^[a-z0-9][a-z0-9._-]{0,63}$')
_CHECKPOINT = re.compile(r'^[0-9a-f]{40}$')
_COMMAND = re.compile(
    r'^(?:/|\$)[a-z][a-z0-9_-]{0,63}'
    r'(?:\s+[a-z0-9][a-z0-9_-]{0,63}){0,4}$',
    re.IGNORECASE,
)


class BetaOperationsError(RuntimeError):
    """Base error for expected operational refusal."""


class BetaRuntimeInvariantError(BetaOperationsError):
    """The durable service would not be operating in the approved profile."""


class BetaPathError(BetaOperationsError):
    """A state, socket, lock, or manifest path is unsafe."""


class ReleaseManifestError(BetaOperationsError, ValueError):
    """A release manifest is invalid or does not match the running build."""


class ReleaseRoleError(BetaOperationsError):
    """The tester role cannot be resolved uniquely and authoritatively."""


class ReleaseDeliveryError(BetaOperationsError):
    """A release could not be delivered or its outcome is uncertain."""


class ReleasePostFailure(ReleaseDeliveryError):
    """A send failed before Discord acceptance when ``retryable`` is true."""

    def __init__(self, message: str, *, retryable: bool):
        super().__init__(message)
        self.retryable = bool(retryable)


@dataclass(frozen=True, slots=True)
class BetaOperationPaths:
    project_root: Path
    log_root: Path
    state_root: Path
    draft_directory: Path
    prepared_directory: Path
    writer_lock: Path
    release_state: Path
    tester_role_state: Path
    socket_path: Path


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    schema_version: int
    release_id: str
    expected_checkpoint: str
    title: str
    bounded_summary: str
    changed_commands: tuple[str, ...]
    known_limitations: tuple[str, ...]
    smoke_test_checklist: tuple[str, ...]
    ping_testers: bool
    notify_user_ids: tuple[int, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        value = {
            'schema_version': self.schema_version,
            'release_id': self.release_id,
            'expected_checkpoint': self.expected_checkpoint,
            'title': self.title,
            'bounded_summary': self.bounded_summary,
            'changed_commands': list(self.changed_commands),
            'known_limitations': list(self.known_limitations),
            'smoke_test_checklist': list(self.smoke_test_checklist),
            'ping_testers': self.ping_testers,
        }
        if self.notify_user_ids:
            value['notify_user_ids'] = list(self.notify_user_ids)
        return value


@dataclass(frozen=True, slots=True)
class TesterRoleBinding:
    guild_id: int
    role_name: str
    role_id: int
    resolved_at: str


@dataclass(frozen=True, slots=True)
class ReleaseDeliveryResult:
    release_id: str
    status: str
    message_id: int | None
    attempts: int


@dataclass(frozen=True, slots=True)
class ReleasePreparationResult:
    manifest: ReleaseManifest
    fingerprint: str
    draft_path: Path
    prepared_path: Path
    status: str


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec='milliseconds',
    ).replace('+00:00', 'Z')


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _reject_symlink(path: Path, *, label: str) -> os.stat_result | None:
    info = _lstat(path)
    if info is not None and stat.S_ISLNK(info.st_mode):
        raise BetaPathError(f'{label} must not be a symlink: {path}')
    return info


def _ensure_directory(path: Path, mode: int, *, label: str) -> None:
    info = _reject_symlink(path, label=label)
    if info is None:
        try:
            path.mkdir(mode=mode)
        except FileExistsError:
            info = _reject_symlink(path, label=label)
        else:
            info = _lstat(path)
    if info is None or not stat.S_ISDIR(info.st_mode):
        raise BetaPathError(f'{label} is not a directory: {path}')
    try:
        os.chmod(path, mode)
    except OSError as exc:
        raise BetaPathError(f'Could not protect {label}: {path}') from exc


def _ensure_directory_tree(path: Path, mode: int, *, label: str) -> None:
    missing: list[Path] = []
    current = path
    while _lstat(current) is None:
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for directory in reversed(missing):
        _ensure_directory(directory, mode, label=label)
    _ensure_directory(path, mode, label=label)


def _validate_profile_paths(profile: RuntimeProfile) -> tuple[Path, Path]:
    project_root = Path(profile.project_root).resolve()
    log_root = Path(profile.log_root).resolve()
    production_root = Path('/home/nelluk/PolyBot39').resolve()
    production_log_root = (project_root / 'logs').resolve()
    if project_root == production_root or project_root.is_relative_to(production_root):
        raise BetaRuntimeInvariantError(
            'The development beta may not use the production checkout.'
        )
    if log_root == production_log_root:
        raise BetaRuntimeInvariantError(
            'The development beta may not use the production log root.'
        )
    try:
        log_root.relative_to(project_root)
    except ValueError as exc:
        raise BetaRuntimeInvariantError(
            'The development beta log root must remain inside its checkout.'
        ) from exc
    return project_root, log_root


def operation_paths(
        profile: RuntimeProfile,
        *,
        create: bool = False) -> BetaOperationPaths:
    project_root, log_root = _validate_profile_paths(profile)
    state_root = log_root / BETA_STATE_DIRECTORY
    draft_directory = state_root / BETA_DRAFT_DIRECTORY
    prepared_directory = state_root / BETA_PREPARED_DIRECTORY
    if create:
        _ensure_directory_tree(log_root, 0o750, label='development log root')
        _ensure_directory(state_root, 0o700, label='beta operation state root')
        _ensure_directory(draft_directory, 0o700, label='beta release draft directory')
        _ensure_directory(
            prepared_directory,
            0o700,
            label='prepared beta release directory',
        )
    else:
        info = _reject_symlink(state_root, label='beta operation state root')
        if info is not None and not stat.S_ISDIR(info.st_mode):
            raise BetaPathError('Beta operation state root is not a directory.')
    return BetaOperationPaths(
        project_root=project_root,
        log_root=log_root,
        state_root=state_root,
        draft_directory=draft_directory,
        prepared_directory=prepared_directory,
        writer_lock=state_root / BETA_WRITER_LOCK,
        release_state=state_root / BETA_RELEASE_STATE,
        tester_role_state=state_root / BETA_TESTER_ROLE_STATE,
        socket_path=state_root / BETA_CONTROL_SOCKET,
    )


def _load_json_file(
        path: Path,
        *,
        absent: Any,
        label: str,
        require_private: bool) -> Any:
    info = _reject_symlink(path, label=label)
    if info is None:
        return absent
    if not stat.S_ISREG(info.st_mode):
        raise BetaPathError(f'{label} is not a regular file: {path}')
    if require_private and stat.S_IMODE(info.st_mode) & 0o077:
        raise BetaPathError(f'{label} permissions are too broad: {path.name}')
    try:
        with path.open(encoding='utf-8') as stream:
            return json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BetaPathError(f'Could not read {label}: {path.name}') from exc


def _read_json(path: Path, *, absent: Any) -> Any:
    return _load_json_file(
        path,
        absent=absent,
        label='state file',
        require_private=True,
    )


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    _reject_symlink(path, label='state file')
    payload = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8') + b'\n'
    temporary_path: Path | None = None
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f'.{path.name}.',
            suffix='.tmp',
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        os.fchmod(file_descriptor, 0o600)
        offset = 0
        while offset < len(payload):
            written = os.write(file_descriptor, payload[offset:])
            if written <= 0:
                raise OSError('short state-file write')
            offset += written
        os.fsync(file_descriptor)
        os.close(file_descriptor)
        os.replace(temporary_path, path)
        temporary_path = None
        directory_fd = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise BetaPathError(f'Could not atomically write {path.name}.') from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def current_checkpoint(project_root: Path) -> str:
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--verify', 'HEAD'],
            cwd=str(project_root),
            capture_output=True,
            check=True,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BetaRuntimeInvariantError(
            'Could not determine the development checkout checkpoint.'
        ) from exc
    checkpoint = result.stdout.strip()
    if not _CHECKPOINT.fullmatch(checkpoint):
        raise BetaRuntimeInvariantError('The development checkpoint is invalid.')
    return checkpoint


def _assert_clean_checkout(project_root: Path) -> None:
    try:
        result = subprocess.run(
            ['git', 'status', '--porcelain', '--untracked-files=all'],
            cwd=str(project_root),
            capture_output=True,
            check=True,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BetaRuntimeInvariantError(
            'Could not verify the development checkout is clean.'
        ) from exc
    if result.stdout.strip():
        raise BetaRuntimeInvariantError(
            'The durable beta requires a clean development checkout.'
        )


def assert_clean_checkout(project_root: Path) -> None:
    """Public read-only checkout guard used by operator validation."""

    _assert_clean_checkout(Path(project_root).resolve())


def _environment_value(environ: Mapping[str, str], key: str) -> str:
    return str(environ.get(key, '')).strip()


def assert_beta_profile(
        profile: RuntimeProfile,
        *,
        environ: Mapping[str, str] | None = None,
        require_service_environment: bool = False) -> None:
    """Reject every profile that is not the fixed development beta."""

    environment = os.environ if environ is None else environ
    if profile.environment != 'development':
        raise BetaRuntimeInvariantError('POLYBOT_ENV must be development.')
    if int(profile.expected_bot_id) != BETA_APPLICATION_ID:
        raise BetaRuntimeInvariantError(
            f'Expected beta application must be {BETA_APPLICATION_ID}.'
        )
    allowed_guilds = tuple(sorted(int(value) for value in profile.allowed_guild_ids))
    if allowed_guilds != (BETA_GUILD_ID,):
        raise BetaRuntimeInvariantError(
            f'The beta must allow only guild {BETA_GUILD_ID}.'
        )
    if profile.database_name != BETA_DATABASE_NAME:
        raise BetaRuntimeInvariantError(
            f'The beta database must be {BETA_DATABASE_NAME}.'
        )
    if profile.database_user != BETA_DATABASE_ROLE:
        raise BetaRuntimeInvariantError(
            f'The beta database role must be {BETA_DATABASE_ROLE}.'
        )
    if profile.background_tasks_enabled or profile.api_enabled or profile.bullet_enabled:
        raise BetaRuntimeInvariantError(
            'Background tasks, API, and Bullet integration must all be disabled.'
        )
    _validate_profile_paths(profile)
    if require_service_environment:
        required = {
            'POLYBOT_ENV': 'development',
            BETA_CONTROL_ENV: 'enabled',
            BETA_STARTUP_SYNC_ENV: 'disabled',
            'POLYBOT_BETA_APPLICATION_ID': str(BETA_APPLICATION_ID),
            'POLYBOT_BETA_GUILD_ID': str(BETA_GUILD_ID),
            'POLYBOT_BETA_DATABASE': BETA_DATABASE_NAME,
            'POLYBOT_BETA_DATABASE_ROLE': BETA_DATABASE_ROLE,
        }
        for key, expected in required.items():
            if _environment_value(environment, key) != expected:
                raise BetaRuntimeInvariantError(
                    f'{key} must be exactly {expected!r} for the durable beta.'
                )


def validate_beta_launch(
        profile: RuntimeProfile,
        argv: Sequence[str],
        *,
        environ: Mapping[str, str] | None = None) -> str:
    """Validate launcher arguments/profile and return the clean HEAD SHA."""

    if tuple(argv) != ('--skip_tasks',):
        raise BetaRuntimeInvariantError(
            'The durable beta launcher accepts only --skip_tasks.'
        )
    assert_beta_profile(
        profile,
        environ=environ,
        require_service_environment=True,
    )
    project_root = Path(profile.project_root).resolve()
    bot_path = (project_root / 'bot.py').resolve()
    if bot_path != project_root / 'bot.py' or not bot_path.is_file():
        raise BetaRuntimeInvariantError(
            'The durable beta bot target must be bot.py inside the development checkout.'
        )
    _assert_clean_checkout(project_root)
    return current_checkpoint(project_root)


class BetaWriterLock:
    """A non-inheriting-by-default file lock held for the bot lifetime."""

    def __init__(self, path: Path):
        self.path = path
        self._file_descriptor: int | None = None

    def acquire(self) -> None:
        if self._file_descriptor is not None:
            raise BetaRuntimeInvariantError('The beta writer lock is already held.')
        _reject_symlink(self.path, label='beta writer lock')
        flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_CLOEXEC', 0)
        flags |= getattr(os, 'O_NOFOLLOW', 0)
        try:
            file_descriptor = os.open(self.path, flags, 0o600)
            os.fchmod(file_descriptor, 0o600)
            fcntl.flock(file_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            try:
                os.close(file_descriptor)
            except (UnboundLocalError, OSError):
                pass
            raise BetaRuntimeInvariantError(
                'Another development beta writer already holds the lock.'
            ) from exc
        os.set_inheritable(file_descriptor, True)
        self._file_descriptor = file_descriptor

    def release(self) -> None:
        if self._file_descriptor is None:
            return
        file_descriptor = self._file_descriptor
        self._file_descriptor = None
        try:
            fcntl.flock(file_descriptor, fcntl.LOCK_UN)
        finally:
            os.close(file_descriptor)

    def __enter__(self) -> 'BetaWriterLock':
        self.acquire()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.release()


def _line_value(value: Any, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ReleaseManifestError(f'{field_name} must be a string.')
    normalized = unicodedata.normalize('NFKC', value).strip()
    if not normalized:
        raise ReleaseManifestError(f'{field_name} must not be empty.')
    if len(normalized) > maximum:
        raise ReleaseManifestError(
            f'{field_name} must be at most {maximum} characters.'
        )
    if any(
            character in '\r\n' or
            unicodedata.category(character).startswith('C')
            for character in normalized):
        raise ReleaseManifestError(f'{field_name} contains a control character.')
    if '@everyone' in normalized.casefold() or '@here' in normalized.casefold():
        raise ReleaseManifestError(f'{field_name} may not contain a broadcast mention.')
    return normalized


def validate_release_manifest(
        value: Mapping[str, Any],
        *,
        current_checkpoint: str | None = None) -> ReleaseManifest:
    if not isinstance(value, Mapping):
        raise ReleaseManifestError('The release manifest must be a JSON object.')
    required_keys = {
        'schema_version', 'release_id', 'expected_checkpoint', 'title',
        'bounded_summary', 'changed_commands', 'known_limitations',
        'smoke_test_checklist', 'ping_testers',
    }
    allowed_keys = required_keys | {'notify_user_ids'}
    keys = set(value)
    missing = required_keys - keys
    extra = keys - allowed_keys
    if missing or extra:
        detail = []
        if missing:
            detail.append('missing ' + ', '.join(sorted(missing)))
        if extra:
            detail.append('unknown ' + ', '.join(sorted(extra)))
        raise ReleaseManifestError('Invalid manifest fields: ' + '; '.join(detail))
    if (
            type(value['schema_version']) is not int
            or value['schema_version'] != MANIFEST_SCHEMA_VERSION):
        raise ReleaseManifestError('Unsupported release manifest schema version.')
    release_id = _line_value(value['release_id'], 'release_id', MAX_RELEASE_ID_LENGTH).lower()
    if not _RELEASE_ID.fullmatch(release_id):
        raise ReleaseManifestError(
            'release_id must contain only lowercase letters, digits, dot, underscore, or hyphen.'
        )
    checkpoint = value['expected_checkpoint']
    if not isinstance(checkpoint, str) or not _CHECKPOINT.fullmatch(checkpoint):
        raise ReleaseManifestError('expected_checkpoint must be a 40-character lowercase Git SHA.')
    if current_checkpoint is not None and checkpoint != current_checkpoint:
        raise ReleaseManifestError(
            f'Manifest checkpoint {checkpoint} does not match running checkpoint {current_checkpoint}.'
        )
    title = _line_value(value['title'], 'title', MAX_TITLE_LENGTH)
    summary = _line_value(value['bounded_summary'], 'bounded_summary', MAX_SUMMARY_LENGTH)

    def bounded_list(key: str, maximum_items: int, maximum_length: int) -> tuple[str, ...]:
        raw = value[key]
        if not isinstance(raw, list):
            raise ReleaseManifestError(f'{key} must be a JSON array.')
        if len(raw) > maximum_items:
            raise ReleaseManifestError(f'{key} may contain at most {maximum_items} items.')
        return tuple(
            _line_value(item, f'{key}[{index}]', maximum_length)
            for index, item in enumerate(raw)
        )

    commands = bounded_list('changed_commands', MAX_CHANGED_COMMANDS, MAX_COMMAND_LENGTH)
    for command in commands:
        if not _COMMAND.fullmatch(command):
            raise ReleaseManifestError(
                f'changed_commands contains an invalid command reference: {command!r}.'
            )
    limitations = bounded_list('known_limitations', MAX_LIMITATIONS, MAX_LIMITATION_LENGTH)
    smoke_tests = bounded_list(
        'smoke_test_checklist', MAX_SMOKE_TESTS, MAX_SMOKE_TEST_LENGTH
    )
    if not smoke_tests:
        raise ReleaseManifestError('smoke_test_checklist must contain at least one item.')
    if type(value['ping_testers']) is not bool:
        raise ReleaseManifestError('ping_testers must be a JSON boolean.')
    raw_notify_users = value.get('notify_user_ids', [])
    if not isinstance(raw_notify_users, list):
        raise ReleaseManifestError('notify_user_ids must be a JSON array.')
    if len(raw_notify_users) > MAX_NOTIFY_USERS:
        raise ReleaseManifestError(
            f'notify_user_ids may contain at most {MAX_NOTIFY_USERS} users.'
        )
    notify_user_ids = []
    for index, user_id in enumerate(raw_notify_users):
        if (
            type(user_id) is not int
            or user_id < 100000000000000
            or user_id > 999999999999999999999
        ):
            raise ReleaseManifestError(
                f'notify_user_ids[{index}] must be a Discord user ID.'
            )
        if user_id in notify_user_ids:
            raise ReleaseManifestError('notify_user_ids must not contain duplicates.')
        notify_user_ids.append(user_id)
    manifest = ReleaseManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        release_id=release_id,
        expected_checkpoint=checkpoint,
        title=title,
        bounded_summary=summary,
        changed_commands=commands,
        known_limitations=limitations,
        smoke_test_checklist=smoke_tests,
        ping_testers=value['ping_testers'],
        notify_user_ids=tuple(notify_user_ids),
    )
    # Render with a placeholder role to enforce the one-message Discord bound
    # before any live channel/role lookup or post attempt.
    if len(build_release_announcement(manifest, tester_role_id=1 if manifest.ping_testers else None)) > MAX_ANNOUNCEMENT_LENGTH:
        raise ReleaseManifestError(
            f'The rendered announcement must be at most {MAX_ANNOUNCEMENT_LENGTH} characters.'
        )
    return manifest


def manifest_fingerprint(manifest: ReleaseManifest) -> str:
    encoded = json.dumps(
        manifest.as_dict(),
        ensure_ascii=True,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _safe_manifest_path(profile: RuntimeProfile, manifest_path: Path) -> Path:
    project_root, _log_root = _validate_profile_paths(profile)
    manifest_root = project_root / BETA_MANIFEST_DIRECTORY
    raw_path = manifest_path if manifest_path.is_absolute() else project_root / manifest_path
    if raw_path.parent != manifest_root or raw_path.suffix != '.json':
        raise BetaPathError(
            f'Manifest must be a direct .json file under {manifest_root}.'
        )
    manifest_info = _reject_symlink(
        manifest_root,
        label='release manifest directory',
    )
    if manifest_info is None or not stat.S_ISDIR(manifest_info.st_mode):
        raise BetaPathError('Release manifest directory is not a directory.')
    info = _reject_symlink(raw_path, label='release manifest')
    if info is None or not stat.S_ISREG(info.st_mode):
        raise BetaPathError(f'Release manifest is not a regular file: {raw_path.name}')
    return raw_path


def load_release_manifest(
        profile: RuntimeProfile,
        manifest_path: Path,
        *,
        current_checkpoint: str | None = None) -> ReleaseManifest:
    path = _safe_manifest_path(profile, Path(manifest_path))
    try:
        with path.open(encoding='utf-8') as stream:
            value = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseManifestError(f'Could not read manifest {path.name}.') from exc
    return validate_release_manifest(value, current_checkpoint=current_checkpoint)


def _safe_operational_manifest_path(
        profile: RuntimeProfile,
        manifest_path: Path,
        *,
        directory_name: str,
        label: str) -> Path:
    paths = operation_paths(profile, create=False)
    directory = getattr(paths, directory_name)
    directory_info = _reject_symlink(directory, label=label + ' directory')
    if directory_info is None or not stat.S_ISDIR(directory_info.st_mode):
        raise BetaPathError(f'{label} directory is not available: {directory}')
    raw_path = (
        manifest_path
        if manifest_path.is_absolute()
        else paths.project_root / manifest_path
    )
    if raw_path.parent != directory or raw_path.suffix != '.json':
        raise BetaPathError(
            f'{label} must be a direct .json file under {directory}.'
        )
    info = _reject_symlink(raw_path, label=label)
    if info is None or not stat.S_ISREG(info.st_mode):
        raise BetaPathError(f'{label} is not a regular file: {raw_path.name}')
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise BetaPathError(f'{label} permissions are too broad: {raw_path.name}')
    return raw_path


def _load_operational_manifest_value(path: Path, *, label: str) -> Mapping[str, Any]:
    value = _load_json_file(
        path,
        absent=None,
        label=label,
        require_private=True,
    )
    if not isinstance(value, Mapping):
        raise ReleaseManifestError(f'{label} must contain a JSON object.')
    return value


def init_release_draft(profile: RuntimeProfile, release_id: str) -> Path:
    """Copy the tracked template into a private ignored draft path."""

    normalized_id = _line_value(
        release_id,
        'release_id',
        MAX_RELEASE_ID_LENGTH,
    ).lower()
    if not _RELEASE_ID.fullmatch(normalized_id):
        raise ReleaseManifestError(
            'release_id must contain only lowercase letters, digits, dot, underscore, or hyphen.'
        )
    paths = operation_paths(profile, create=True)
    draft_path = paths.draft_directory / f'{normalized_id}.json'
    if _lstat(draft_path) is not None:
        raise ReleaseManifestError(
            f'A release draft already exists for {normalized_id}; edit it or choose a new ID.'
        )

    template_path = paths.project_root / BETA_MANIFEST_DIRECTORY / BETA_TEMPLATE_FILENAME
    template_info = _reject_symlink(
        template_path,
        label='tracked release template',
    )
    if template_info is None or not stat.S_ISREG(template_info.st_mode):
        raise BetaPathError('The tracked release template is unavailable.')
    template_value = _load_json_file(
        template_path,
        absent=None,
        label='tracked release template',
        require_private=False,
    )
    if not isinstance(template_value, Mapping):
        raise ReleaseManifestError('The tracked release template must be a JSON object.')
    template_manifest = validate_release_manifest(
        template_value,
        current_checkpoint=DRAFT_CHECKPOINT,
    )
    draft_value = template_manifest.as_dict()
    draft_value['release_id'] = normalized_id
    _write_json(draft_path, draft_value)
    return draft_path


def _prepared_record(
        manifest: ReleaseManifest,
        fingerprint: str,
        *,
        draft_path: Path,
        prepared_path: Path) -> dict[str, Any]:
    return {
        'fingerprint': fingerprint,
        'manifest': manifest.as_dict(),
        'expected_checkpoint': manifest.expected_checkpoint,
        'draft_path': str(draft_path),
        'prepared_path': str(prepared_path),
        'prepared_at': utc_timestamp(),
    }


def prepare_release_manifest(
        profile: RuntimeProfile,
        draft_path: Path,
        *,
        current_checkpoint: str) -> ReleasePreparationResult:
    """Inject a reviewed clean HEAD into an ignored operational manifest.

    The draft must retain the all-zero checkpoint from the tracked template.
    This makes it impossible to mistake a draft for a self-pinned committed
    release file.  The final manifest and its fingerprint are archived in the
    atomic release state before the operator can deliver it.
    """

    if not _CHECKPOINT.fullmatch(current_checkpoint):
        raise BetaRuntimeInvariantError('The preparation checkpoint is invalid.')
    paths = operation_paths(profile, create=True)
    safe_draft_path = _safe_operational_manifest_path(
        profile,
        Path(draft_path),
        directory_name='draft_directory',
        label='release draft',
    )
    draft_value = _load_operational_manifest_value(
        safe_draft_path,
        label='release draft',
    )
    if draft_value.get('expected_checkpoint') != DRAFT_CHECKPOINT:
        raise ReleaseManifestError(
            'The release draft must retain the all-zero checkpoint placeholder; '
            'prepare injects the reviewed checkout HEAD.'
        )
    prepared_value = dict(draft_value)
    prepared_value['expected_checkpoint'] = current_checkpoint
    manifest = validate_release_manifest(
        prepared_value,
        current_checkpoint=current_checkpoint,
    )
    if manifest.release_id != safe_draft_path.stem:
        raise ReleaseManifestError(
            'The release draft ID must match its filename.'
        )
    fingerprint = manifest_fingerprint(manifest)
    prepared_path = paths.prepared_directory / f'{manifest.release_id}.json'
    state = _read_release_state(paths)
    prepared = state['prepared']
    existing = prepared.get(manifest.release_id)
    if existing is not None:
        if (
                not isinstance(existing, dict)
                or existing.get('fingerprint') != fingerprint
                or existing.get('manifest') != manifest.as_dict()):
            raise ReleaseDeliveryError(
                'The release ID is already prepared with different content; '
                'do not overwrite its audit record.'
            )

    existing_file = _lstat(prepared_path)
    if existing_file is not None:
        existing_value = _load_operational_manifest_value(
            prepared_path,
            label='prepared release manifest',
        )
        if dict(existing_value) != manifest.as_dict():
            raise ReleaseDeliveryError(
                'The prepared release file conflicts with its requested content.'
            )
    else:
        _write_json(prepared_path, manifest.as_dict())

    if existing is None:
        prepared[manifest.release_id] = _prepared_record(
            manifest,
            fingerprint,
            draft_path=safe_draft_path,
            prepared_path=prepared_path,
        )
        _write_release_state(paths, state)
        status = 'prepared'
    else:
        status = 'already-prepared'
    return ReleasePreparationResult(
        manifest=manifest,
        fingerprint=fingerprint,
        draft_path=safe_draft_path,
        prepared_path=prepared_path,
        status=status,
    )


def load_prepared_release_manifest(
        profile: RuntimeProfile,
        manifest_path: Path,
        *,
        current_checkpoint: str | None = None) -> ReleaseManifest:
    """Load only a mode-protected manifest archived by ``prepare``."""

    path = _safe_operational_manifest_path(
        profile,
        Path(manifest_path),
        directory_name='prepared_directory',
        label='prepared release manifest',
    )
    value = _load_operational_manifest_value(
        path,
        label='prepared release manifest',
    )
    manifest = validate_release_manifest(
        value,
        current_checkpoint=current_checkpoint,
    )
    state = _read_release_state(operation_paths(profile, create=False))
    record = state['prepared'].get(manifest.release_id)
    if (
            not isinstance(record, dict)
            or record.get('fingerprint') != manifest_fingerprint(manifest)
            or record.get('manifest') != manifest.as_dict()
            or record.get('prepared_path') != str(path)):
        raise ReleaseManifestError(
            'The prepared manifest is not the archived audited release state.'
        )
    return manifest


def build_release_announcement(
        manifest: ReleaseManifest,
        *,
        tester_role_id: int | None = None) -> str:
    if manifest.ping_testers and tester_role_id is None:
        raise ReleaseRoleError('A tester role is required for a pinged release.')
    def escape(value: str) -> str:
        return discord.utils.escape_mentions(discord.utils.escape_markdown(value))

    lines = [
        f'📣 **{escape(manifest.title)}**',
        escape(manifest.bounded_summary),
        f'**Release ID:** `{manifest.release_id}`',
        f'**Checkpoint:** `{manifest.expected_checkpoint}`',
    ]
    if manifest.ping_testers:
        lines.append(f'Tester ping: <@&{int(tester_role_id)}>')
    if manifest.notify_user_ids:
        lines.append(
            'Requested reviewer notification: '
            + ' '.join(f'<@{user_id}>' for user_id in manifest.notify_user_ids)
        )
    if manifest.changed_commands:
        lines.append('**Changed commands:** ' + ', '.join(
            f'`{escape(command)}`' for command in manifest.changed_commands
        ))
    else:
        lines.append('**Changed commands:** none')
    if manifest.known_limitations:
        lines.append('**Known limitations:**\n' + '\n'.join(
            f'- {escape(item)}' for item in manifest.known_limitations
        ))
    else:
        lines.append('**Known limitations:** none')
    lines.append('## 🧪 WHAT TO TEST\n' + '\n'.join(
        f'- [ ] {escape(item)}' for item in manifest.smoke_test_checklist
    ))
    # The marker is visible and searchable, making recovery after a process
    # crash possible without a second client or a duplicate post.
    lines.append(f'Beta release marker: `POLYBOT_BETA_RELEASE:{manifest.release_id}`')
    return '\n'.join(lines)


def _read_release_state(paths: BetaOperationPaths) -> dict[str, Any]:
    value = _read_json(
        paths.release_state,
        absent={'schema_version': RELEASE_STATE_SCHEMA_VERSION, 'releases': {}},
    )
    if not isinstance(value, dict) or value.get('schema_version') != RELEASE_STATE_SCHEMA_VERSION:
        raise BetaPathError('The release state file has an unsupported schema.')
    releases = value.get('releases')
    if not isinstance(releases, dict):
        raise BetaPathError('The release state file has invalid release entries.')
    prepared = value.setdefault('prepared', {})
    if not isinstance(prepared, dict):
        raise BetaPathError('The release state file has invalid prepared entries.')
    return value


def _write_release_state(paths: BetaOperationPaths, value: Mapping[str, Any]) -> None:
    _write_json(paths.release_state, value)


def _read_role_binding(paths: BetaOperationPaths) -> TesterRoleBinding | None:
    value = _read_json(paths.tester_role_state, absent=None)
    if value is None:
        return None
    if not isinstance(value, dict) or value.get('schema_version') != ROLE_STATE_SCHEMA_VERSION:
        raise BetaPathError('The tester-role state file has an unsupported schema.')
    try:
        guild_id = int(value['guild_id'])
        role_id = int(value['role_id'])
        role_name = str(value['role_name'])
        resolved_at = str(value['resolved_at'])
    except (KeyError, TypeError, ValueError) as exc:
        raise BetaPathError('The tester-role state file is invalid.') from exc
    if guild_id != BETA_GUILD_ID or role_name != BETA_TESTER_ROLE_NAME or role_id <= 0 or not resolved_at:
        raise BetaPathError('The tester-role state file does not identify the fixed beta role.')
    return TesterRoleBinding(guild_id, role_name, role_id, resolved_at)


def _write_role_binding(paths: BetaOperationPaths, role: TesterRoleBinding) -> None:
    _write_json(paths.tester_role_state, {
        'schema_version': ROLE_STATE_SCHEMA_VERSION,
        'guild_id': role.guild_id,
        'role_id': role.role_id,
        'role_name': role.role_name,
        'resolved_at': role.resolved_at,
    })


def _discord_send_is_certainly_rejected(exc: BaseException) -> bool:
    if isinstance(exc, (discord.Forbidden, discord.NotFound, discord.InvalidArgument)):
        return True
    if isinstance(exc, discord.HTTPException):
        status = getattr(getattr(exc, 'response', None), 'status', None)
        return status in {400, 401, 403, 404, 405, 413, 415, 429}
    return False


class BetaReleaseService:
    """Explicit idempotent release delivery through one authenticated bot."""

    def __init__(
            self,
            bot: Any,
            profile: RuntimeProfile,
            startup_checkpoint: str):
        assert_beta_profile(profile)
        if not _CHECKPOINT.fullmatch(startup_checkpoint):
            raise BetaRuntimeInvariantError('The bot startup checkpoint is invalid.')
        self.bot = bot
        self.profile = profile
        self.startup_checkpoint = startup_checkpoint
        self.paths = operation_paths(profile, create=True)
        self._lock = asyncio.Lock()

    def _assert_authenticated_identity(self) -> None:
        user = getattr(self.bot, 'user', None)
        if user is None or int(getattr(user, 'id', 0)) != BETA_APPLICATION_ID:
            raise BetaRuntimeInvariantError(
                'The authenticated Discord application is not the expected beta bot.'
            )
        if hasattr(self.bot, 'is_ready') and not self.bot.is_ready():
            raise BetaRuntimeInvariantError('The beta bot is not ready for an explicit release operation.')

    def _guild(self) -> Any:
        guild = self.bot.get_guild(BETA_GUILD_ID)
        if guild is None or int(getattr(guild, 'id', 0)) != BETA_GUILD_ID:
            raise ReleaseDeliveryError('The expected development guild is not loaded.')
        if BETA_GUILD_ID not in tuple(int(value) for value in self.profile.allowed_guild_ids):
            raise ReleaseDeliveryError('The release guild is outside the runtime allowlist.')
        return guild

    async def _public_channel(self, guild: Any) -> Any:
        channel = guild.get_channel(BETA_PUBLIC_RELEASE_CHANNEL_ID)
        if channel is None:
            fetch_channel = getattr(self.bot, 'fetch_channel', None)
            if callable(fetch_channel):
                channel = await fetch_channel(BETA_PUBLIC_RELEASE_CHANNEL_ID)
        if channel is None:
            raise ReleaseDeliveryError('The public release channel is not available.')
        channel_guild = getattr(channel, 'guild', None)
        if (
                int(getattr(channel, 'id', 0)) != BETA_PUBLIC_RELEASE_CHANNEL_ID
                or int(getattr(channel_guild, 'id', 0)) != BETA_GUILD_ID
                or str(getattr(channel, 'name', '')) != BETA_PUBLIC_RELEASE_CHANNEL_NAME
                or not callable(getattr(channel, 'send', None))):
            raise ReleaseDeliveryError(
                'The configured public release target is not the exact development channel.'
            )
        if not callable(getattr(channel, 'history', None)):
            raise ReleaseDeliveryError(
                'The public release channel must support bounded history checks for idempotency.'
            )
        return channel

    def _matching_role(self, guild: Any, role_id: int) -> Any:
        matches = [
            role for role in getattr(guild, 'roles', ())
            if str(getattr(role, 'name', '')) == BETA_TESTER_ROLE_NAME
        ]
        if len(matches) != 1:
            raise ReleaseRoleError(
                'The testers role is missing or ambiguous; no mention is allowed.'
            )
        role = matches[0]
        if int(getattr(role, 'id', 0)) != role_id:
            raise ReleaseRoleError(
                'The persisted testers role ID does not match the live role; re-resolve it.'
            )
        return role

    def _role_for_ping(self, guild: Any) -> Any:
        binding = _read_role_binding(self.paths)
        if binding is None:
            raise ReleaseRoleError(
                'The testers role is not pinned; run the separately approved role-resolution step first.'
            )
        return self._matching_role(guild, binding.role_id)

    async def resolve_tester_role(self) -> TesterRoleBinding:
        async with self._lock:
            self._assert_authenticated_identity()
            guild = self._guild()
            matches = [
                role for role in getattr(guild, 'roles', ())
                if str(getattr(role, 'name', '')) == BETA_TESTER_ROLE_NAME
            ]
            if len(matches) != 1:
                raise ReleaseRoleError(
                    'The testers role must resolve to exactly one role before its ID is persisted.'
                )
            role = matches[0]
            role_id = int(getattr(role, 'id', 0))
            if role_id <= 0:
                raise ReleaseRoleError('The resolved testers role ID is invalid.')
            binding = TesterRoleBinding(
                guild_id=BETA_GUILD_ID,
                role_name=BETA_TESTER_ROLE_NAME,
                role_id=role_id,
                resolved_at=utc_timestamp(),
            )
            _write_role_binding(self.paths, binding)
            return binding

    async def _find_marker(self, channel: Any, marker: str) -> Any | None:
        try:
            history = channel.history(limit=MAX_HISTORY_SCAN)
            async for message in history:
                if marker in str(getattr(message, 'content', '')):
                    return message
        except Exception as exc:
            raise ReleaseDeliveryError(
                'Could not complete the bounded idempotency history check; no post was attempted.'
            ) from exc
        return None

    async def deliver(self, value: Mapping[str, Any]) -> ReleaseDeliveryResult:
        async with self._lock:
            manifest = validate_release_manifest(
                value,
                current_checkpoint=self.startup_checkpoint,
            )
            fingerprint = manifest_fingerprint(manifest)
            state = _read_release_state(self.paths)
            releases = state['releases']
            existing = releases.get(manifest.release_id)
            was_in_flight = False
            if existing is not None:
                if not isinstance(existing, dict) or existing.get('fingerprint') != fingerprint:
                    raise ReleaseDeliveryError(
                        'The release ID is already associated with a different manifest.'
                    )
                status = existing.get('status')
                if status == 'posted':
                    return ReleaseDeliveryResult(
                        manifest.release_id,
                        'already-posted',
                        existing.get('message_id'),
                        int(existing.get('attempts', 1)),
                    )
                if status not in {'failed', 'posting'}:
                    raise ReleaseDeliveryError('The release state is invalid or incomplete.')
                if status == 'failed' and not existing.get('retryable', False):
                    raise ReleaseDeliveryError(
                        'The previous release post has an uncertain outcome; reconcile it before retrying.'
                    )
                was_in_flight = status == 'posting'

            prepared = state['prepared'].get(manifest.release_id)
            if (
                    not isinstance(prepared, dict)
                    or prepared.get('fingerprint') != fingerprint
                    or prepared.get('manifest') != manifest.as_dict()):
                raise ReleaseDeliveryError(
                    'The release must be prepared from the tracked template at the '
                    'reviewed checkpoint before delivery.'
                )

            self._assert_authenticated_identity()
            guild = self._guild()
            channel = await self._public_channel(guild)
            tester_role = self._role_for_ping(guild) if manifest.ping_testers else None
            marker = f'POLYBOT_BETA_RELEASE:{manifest.release_id}'
            found = await self._find_marker(channel, marker)
            if found is not None:
                message_id = getattr(found, 'id', None)
                record = {
                    'fingerprint': fingerprint,
                    'status': 'posted',
                    'message_id': int(message_id) if message_id is not None else None,
                    'channel_id': BETA_PUBLIC_RELEASE_CHANNEL_ID,
                    'expected_checkpoint': manifest.expected_checkpoint,
                    'attempts': int(existing.get('attempts', 0)) if existing else 0,
                    'updated_at': utc_timestamp(),
                }
                releases[manifest.release_id] = record
                _write_release_state(self.paths, state)
                return ReleaseDeliveryResult(
                    manifest.release_id,
                    'already-posted',
                    record['message_id'],
                    record['attempts'],
                )
            if was_in_flight:
                raise ReleaseDeliveryError(
                    'A prior release post is still unresolved; no duplicate post was attempted.'
                )

            attempts = int(existing.get('attempts', 0)) + 1 if existing else 1
            releases[manifest.release_id] = {
                'fingerprint': fingerprint,
                'status': 'posting',
                'message_id': None,
                'channel_id': BETA_PUBLIC_RELEASE_CHANNEL_ID,
                'expected_checkpoint': manifest.expected_checkpoint,
                'attempts': attempts,
                'updated_at': utc_timestamp(),
            }
            _write_release_state(self.paths, state)
            content = build_release_announcement(
                manifest,
                tester_role_id=(int(getattr(tester_role, 'id')) if tester_role else None),
            )
            allowed_mentions = discord.AllowedMentions(
                everyone=False,
                users=[
                    discord.Object(id=user_id)
                    for user_id in manifest.notify_user_ids
                ],
                roles=(
                    [discord.Object(id=int(getattr(tester_role, 'id')))]
                    if tester_role is not None
                    else []
                ),
                replied_user=False,
            )
            try:
                message = await channel.send(
                    content,
                    allowed_mentions=allowed_mentions,
                )
            except Exception as exc:
                retryable = _discord_send_is_certainly_rejected(exc)
                releases[manifest.release_id] = {
                    **releases[manifest.release_id],
                    'status': 'failed' if retryable else 'posting',
                    'retryable': retryable,
                    'error': type(exc).__name__,
                    'updated_at': utc_timestamp(),
                }
                _write_release_state(self.paths, state)
                raise ReleasePostFailure(
                    'The release post failed before a confirmed Discord result.'
                    if retryable
                    else 'The release post outcome is uncertain; retry is blocked to prevent a duplicate.',
                    retryable=retryable,
                ) from exc
            message_id = getattr(message, 'id', None)
            releases[manifest.release_id] = {
                **releases[manifest.release_id],
                'status': 'posted',
                'message_id': int(message_id) if message_id is not None else None,
                'retryable': False,
                'updated_at': utc_timestamp(),
            }
            _write_release_state(self.paths, state)
            return ReleaseDeliveryResult(
                manifest.release_id,
                'posted',
                int(message_id) if message_id is not None else None,
                attempts,
            )

    def status(self) -> Mapping[str, Any]:
        return _read_release_state(self.paths)

    async def readiness_inventory(self) -> Mapping[str, Any]:
        """Return a bounded read-only inventory from this authenticated bot."""

        async with self._lock:
            self._assert_authenticated_identity()
            binding = _read_role_binding(self.paths)
            if binding is None:
                raise ReleaseRoleError(
                    'The testers role is not pinned; readiness inventory is refused.'
                )
            try:
                return beta_readiness.build_discord_inventory(
                    bot=self.bot,
                    profile=self.profile,
                    pinned_tester_role_id=binding.role_id,
                    public_channel_id=BETA_PUBLIC_RELEASE_CHANNEL_ID,
                    public_channel_name=BETA_PUBLIC_RELEASE_CHANNEL_NAME,
                    staffhelp_channel_id=BETA_STAFFHELP_MIRROR_CHANNEL_ID,
                    staffhelp_channel_name=BETA_STAFFHELP_MIRROR_CHANNEL_NAME,
                    tester_role_name=BETA_TESTER_ROLE_NAME,
                )
            except beta_readiness.ReadinessInventoryError as exc:
                raise ReleaseDeliveryError(str(exc)) from exc


def beta_control_enabled(environ: Mapping[str, str] | None = None) -> bool:
    values = os.environ if environ is None else environ
    return _environment_value(values, BETA_CONTROL_ENV) == 'enabled'


class BetaReleaseControl:
    """A local-only control socket attached to the authenticated beta bot."""

    def __init__(
            self,
            bot: Any,
            profile: RuntimeProfile,
            startup_checkpoint: str | None = None):
        if not beta_control_enabled():
            raise BetaRuntimeInvariantError('Beta release control is not enabled.')
        assert_beta_profile(profile, require_service_environment=True)
        self.bot = bot
        self.profile = profile
        self.startup_checkpoint = startup_checkpoint or _environment_value(
            os.environ,
            BETA_CHECKPOINT_ENV,
        )
        if not _CHECKPOINT.fullmatch(self.startup_checkpoint):
            raise BetaRuntimeInvariantError(
                'The durable beta launcher must provide a valid startup checkpoint.'
            )
        self.service = BetaReleaseService(bot, profile, self.startup_checkpoint)
        self.paths = self.service.paths
        self.server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        existing = _lstat(self.paths.socket_path)
        if existing is not None:
            if stat.S_ISLNK(existing.st_mode) or not stat.S_ISSOCK(existing.st_mode):
                raise BetaPathError('The release control socket path is not a socket.')
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_unix_connection(str(self.paths.socket_path)),
                    timeout=0.2,
                )
            except Exception:
                self.paths.socket_path.unlink()
            else:
                writer.close()
                await writer.wait_closed()
                raise BetaRuntimeInvariantError(
                    'Another beta release control socket is already active.'
                )
        self.server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(self.paths.socket_path),
            limit=MAX_SOCKET_REQUEST_BYTES,
        )
        os.chmod(self.paths.socket_path, 0o600)

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        info = _lstat(self.paths.socket_path)
        if info is not None and stat.S_ISSOCK(info.st_mode):
            self.paths.socket_path.unlink()

    async def _dispatch(self, request: Mapping[str, Any]) -> dict[str, Any]:
        operation = request.get('operation')
        if operation == 'deliver':
            manifest = request.get('manifest')
            if not isinstance(manifest, Mapping):
                raise ReleaseManifestError('The control request has no manifest object.')
            result = await self.service.deliver(manifest)
            return {
                'release_id': result.release_id,
                'status': result.status,
                'message_id': result.message_id,
                'attempts': result.attempts,
            }
        if operation == 'resolve-tester-role':
            binding = await self.service.resolve_tester_role()
            return {
                'guild_id': binding.guild_id,
                'role_name': binding.role_name,
                'role_id': binding.role_id,
                'resolved_at': binding.resolved_at,
            }
        if operation == 'status':
            return dict(self.service.status())
        if operation == 'readiness-inventory':
            return dict(await self.service.readiness_inventory())
        if operation in {'beta-lab-persona-status', 'beta-lab-persona-setup'}:
            from modules import beta_lab_personas
            self.service._assert_authenticated_identity()
            guild = self.service._guild()
            try:
                if operation == 'beta-lab-persona-setup':
                    if request.get('confirm') != 'PREPARE-BETA-LAB-PERSONAS':
                        raise BetaOperationsError(
                            'Persona setup requires the exact confirmation token.'
                        )
                    binding = await beta_lab_personas.setup_roles(self.profile, guild)
                    status = beta_lab_personas.PersonaStatus(
                        True,
                        'The dedicated zero-permission Team and staff-persona roles are ready.',
                        binding.team_role_id,
                        binding.staff_role_id,
                    )
                else:
                    status = beta_lab_personas.role_status(self.profile, guild)
            except beta_lab_personas.BetaLabPersonaError as exc:
                raise BetaOperationsError(str(exc)) from exc
            return {
                'ready': status.ready,
                'detail': status.detail,
                'team_role_id': status.team_role_id,
                'staff_role_id': status.staff_role_id,
            }
        if operation in {'beta-lab-status', 'beta-lab-plan'}:
            from modules import beta_lab_workers
            self.service._assert_authenticated_identity()
            self.service._guild()
            status = await beta_lab_workers.run_status(BETA_GUILD_ID)
            return (
                status.plan_dict()
                if operation == 'beta-lab-plan'
                else status.as_dict()
            )
        if operation == 'beta-lab-refresh':
            from modules import beta_lab_workers
            self.service._assert_authenticated_identity()
            self.service._guild()
            if request.get('pack') != beta_lab_workers.RESULTS:
                raise BetaOperationsError(
                    'The foundation can refresh only the game-results pack.'
                )
            if request.get('confirm') != beta_lab_workers.REFRESH_CONFIRMATION:
                raise BetaOperationsError(
                    'Beta Lab refresh requires the exact confirmation token.'
                )
            result = await beta_lab_workers.refresh_results(
                guild_id=BETA_GUILD_ID,
                actor='Local Beta Lab operator',
            )
            return result.as_dict()
        raise BetaOperationsError('Unknown beta control operation.')

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.readline()
            if not raw or len(raw) > MAX_SOCKET_REQUEST_BYTES:
                raise BetaOperationsError('The control request is empty or too large.')
            request = json.loads(raw.decode('utf-8'))
            if not isinstance(request, Mapping):
                raise BetaOperationsError('The control request must be a JSON object.')
            response = {'ok': True, 'result': await self._dispatch(request)}
        except Exception as exc:
            response = {
                'ok': False,
                'error': str(exc),
                'error_type': type(exc).__name__,
            }
        try:
            payload = json.dumps(
                response,
                ensure_ascii=True,
                sort_keys=True,
                separators=(',', ':'),
            ).encode('utf-8') + b'\n'
            if len(payload) > MAX_SOCKET_RESPONSE_BYTES:
                payload = json.dumps({
                    'ok': False,
                    'error': 'The beta control response is too large.',
                    'error_type': 'BetaOperationsError',
                }, separators=(',', ':')).encode('utf-8') + b'\n'
            writer.write(payload)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


async def send_control_request(
        profile: RuntimeProfile,
        request: Mapping[str, Any],
        *,
        timeout: float = 15.0) -> Mapping[str, Any]:
    assert_beta_profile(profile)
    paths = operation_paths(profile, create=False)
    info = _reject_symlink(paths.socket_path, label='release control socket')
    if info is None or not stat.S_ISSOCK(info.st_mode):
        raise BetaOperationsError('The durable beta release control socket is not active.')
    writer = None
    raw = b''
    try:
        request_payload = json.dumps(
            dict(request), ensure_ascii=True, separators=(',', ':')
        ).encode('utf-8') + b'\n'
        if len(request_payload) > MAX_SOCKET_REQUEST_BYTES:
            raise BetaOperationsError('The beta control request is too large.')
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(
                str(paths.socket_path),
                limit=MAX_SOCKET_RESPONSE_BYTES,
            ),
            timeout=timeout,
        )
        writer.write(request_payload)
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
    except (OSError, asyncio.TimeoutError, asyncio.LimitOverrunError) as exc:
        raise BetaOperationsError('The beta release control request did not complete.') from exc
    finally:
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except OSError:
                pass
    if len(raw) > MAX_SOCKET_RESPONSE_BYTES or not raw:
        raise BetaOperationsError('The beta control returned an invalid response.')
    try:
        response = json.loads(raw.decode('utf-8'))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BetaOperationsError('The beta control returned malformed JSON.') from exc
    if not isinstance(response, Mapping) or not response.get('ok'):
        raise BetaOperationsError(str(response.get('error', 'The beta operation failed.')))
    result = response.get('result')
    if not isinstance(result, Mapping):
        raise BetaOperationsError('The beta control returned no operation result.')
    return result
