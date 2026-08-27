"""Small fail-closed runtime boundary for the upstream development bot.

The direct Compose launcher uses this module to validate its fixed development
identity and process-wide writer lock. It contains no
Discord control socket, fixture management, or release-announcement workflow.
"""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Any, Mapping, Sequence

from runtime_config import RuntimeProfile


BETA_GUILD_ID = 478571892832206869
BETA_APPLICATION_ID = 479029527553638401
BETA_STAFFHELP_MIRROR_CHANNEL_ID = 480078679930830849
BETA_DATABASE_NAME = 'polytopia_dev'
BETA_DATABASE_ROLE = 'polybot_dev'
BETA_STARTUP_SYNC_ENV = 'POLYBOT_BETA_STARTUP_SYNC'
BETA_STATE_DIRECTORY = 'beta-operations'
BETA_WRITER_LOCK = 'beta-writer.lock'

_CHECKPOINT = re.compile(r'^[0-9a-f]{40}$')


class BetaOperationsError(RuntimeError):
    """Base error for an expected development-runtime refusal."""


class BetaRuntimeInvariantError(BetaOperationsError):
    """The bot would not be running in the approved development profile."""


class BetaPathError(BetaOperationsError):
    """A runtime state or lock path is unsafe."""


@dataclass(frozen=True, slots=True)
class BetaOperationPaths:
    project_root: Path
    log_root: Path
    state_root: Path
    writer_lock: Path


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
        path.mkdir(parents=True, mode=mode)
        info = _lstat(path)
    if info is None or not stat.S_ISDIR(info.st_mode):
        raise BetaPathError(f'{label} is not a directory: {path}')
    try:
        os.chmod(path, mode)
    except OSError as exc:
        raise BetaPathError(f'Could not protect {label}: {path}') from exc


def _validate_profile_paths(profile: RuntimeProfile) -> tuple[Path, Path]:
    project_root = Path(profile.project_root).resolve()
    log_root = Path(profile.log_root).resolve()
    production_root = Path('/srv/polyelo/PolyBot39').resolve()
    if project_root == production_root or project_root.is_relative_to(production_root):
        raise BetaRuntimeInvariantError(
            'The development bot may not use the production checkout.'
        )
    try:
        log_root.relative_to(project_root)
    except ValueError as exc:
        raise BetaRuntimeInvariantError(
            'The development log root must remain inside its checkout.'
        ) from exc
    return project_root, log_root


def operation_paths(
        profile: RuntimeProfile,
        *,
        create: bool = False) -> BetaOperationPaths:
    project_root, log_root = _validate_profile_paths(profile)
    state_root = log_root / BETA_STATE_DIRECTORY
    if create:
        _ensure_directory(log_root, 0o750, label='development log root')
        _ensure_directory(state_root, 0o700, label='development runtime state root')
    else:
        info = _reject_symlink(state_root, label='development runtime state root')
        if info is not None and not stat.S_ISDIR(info.st_mode):
            raise BetaPathError('Development runtime state root is not a directory.')
    return BetaOperationPaths(
        project_root=project_root,
        log_root=log_root,
        state_root=state_root,
        writer_lock=state_root / BETA_WRITER_LOCK,
    )


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


def assert_clean_checkout(project_root: Path) -> None:
    try:
        result = subprocess.run(
            ['git', 'status', '--porcelain', '--untracked-files=all'],
            cwd=str(Path(project_root).resolve()),
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
            'The development runtime requires a clean checkout.'
        )


def _environment_value(environ: Mapping[str, str], key: str) -> str:
    return str(environ.get(key, '')).strip()


def assert_beta_profile(
        profile: RuntimeProfile,
        *,
        environ: Mapping[str, str] | None = None,
        require_service_environment: bool = False) -> None:
    environment = os.environ if environ is None else environ
    if profile.environment != 'development':
        raise BetaRuntimeInvariantError('POLYBOT_ENV must be development.')
    if int(profile.expected_bot_id) != BETA_APPLICATION_ID:
        raise BetaRuntimeInvariantError(
            f'Expected development application must be {BETA_APPLICATION_ID}.'
        )
    allowed_guilds = tuple(sorted(int(value) for value in profile.allowed_guild_ids))
    if allowed_guilds != (BETA_GUILD_ID,):
        raise BetaRuntimeInvariantError(
            f'The development bot must allow only guild {BETA_GUILD_ID}.'
        )
    if profile.database_name != BETA_DATABASE_NAME:
        raise BetaRuntimeInvariantError(
            f'The development database must be {BETA_DATABASE_NAME}.'
        )
    if profile.database_user != BETA_DATABASE_ROLE:
        raise BetaRuntimeInvariantError(
            f'The development database role must be {BETA_DATABASE_ROLE}.'
        )
    if profile.background_tasks_enabled or profile.api_enabled or profile.bullet_enabled:
        raise BetaRuntimeInvariantError(
            'Background tasks, API, and Bullet integration must all be disabled.'
        )
    _validate_profile_paths(profile)
    if require_service_environment:
        required = {
            'POLYBOT_ENV': 'development',
            BETA_STARTUP_SYNC_ENV: 'disabled',
            'POLYBOT_BETA_APPLICATION_ID': str(BETA_APPLICATION_ID),
            'POLYBOT_BETA_GUILD_ID': str(BETA_GUILD_ID),
            'POLYBOT_BETA_DATABASE': BETA_DATABASE_NAME,
            'POLYBOT_BETA_DATABASE_ROLE': BETA_DATABASE_ROLE,
        }
        for key, expected in required.items():
            if _environment_value(environment, key) != expected:
                raise BetaRuntimeInvariantError(
                    f'{key} must be exactly {expected!r} for the development bot.'
                )


def validate_beta_launch(
        profile: RuntimeProfile,
        argv: Sequence[str],
        *,
        environ: Mapping[str, str] | None = None) -> None:
    if tuple(argv) != ('--skip_tasks',):
        raise BetaRuntimeInvariantError(
            'The development launcher accepts only --skip_tasks.'
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
            'The development bot target must be bot.py inside its checkout.'
        )
    environment = os.environ if environ is None else environ
    supervisor = _environment_value(environment, 'POLYBOT_RESTART_SUPERVISOR')
    if supervisor != 'compose':
        raise BetaRuntimeInvariantError(
            'The development bot must run through the direct Compose supervisor.'
        )


class BetaWriterLock:
    """A process lock held for the complete supervised bot lifetime."""

    def __init__(self, path: Path):
        self.path = path
        self._file_descriptor: int | None = None

    def acquire(self) -> None:
        if self._file_descriptor is not None:
            raise BetaRuntimeInvariantError('The writer lock is already held.')
        _reject_symlink(self.path, label='development writer lock')
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
                'Another development writer already holds the process lock.'
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
