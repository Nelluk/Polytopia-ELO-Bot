"""Production-only owner backup orchestration with bounded process handling."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import pwd
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time

import settings


logger = logging.getLogger('polybot.' + __name__)

PRODUCTION_ENVIRONMENT = 'production'
PRODUCTION_DATABASE = 'polytopia2'
PRODUCTION_ROOT = Path('/home/nelluk/PolyBot39')
PRODUCTION_USER = 'nelluk'
DEPLOYED_SCRIPT = Path('/home/nelluk/backup_db.sh')
GIT_EXECUTABLE = Path('/usr/bin/git')
RELEASE_MANIFEST_NAME = '.operator-backup-release.json'
RELEASE_MANIFEST_SCHEMA = 1
RELEASE_MANIFEST_CONFIRMATION = 'P9-M3-PRODUCTION-BACKUP-RELEASE-APPLY'
REPORTING_PARTIAL_EXIT = 20
LOCK_BUSY_EXIT = 75
MAX_PROCESS_SECONDS = 12 * 60
TERMINATE_GRACE_SECONDS = 10
MAX_CAPTURE_BYTES = 16 * 1024
_CHECKPOINT = re.compile(r'^[0-9a-f]{40}$')
_DIGEST = re.compile(r'^[0-9a-f]{64}$')
_FILE_EXECUTOR = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='polybot-operator-backup-files',
)


class BackupError(RuntimeError):
    """Base class for safe operator-facing backup failures."""


class BackupPermissionError(BackupError):
    """The requester is not the configured owner."""


class BackupEnvironmentError(BackupError):
    """The runtime is not the exact reviewed production target."""


class BackupSourceError(BackupError):
    """The tracked and deployed backup sources are not safe to execute."""


class BackupConflictError(BackupError):
    """This process already has one active manual backup."""


class BackupExecutionError(BackupError):
    """The child process could not produce a trustworthy result."""


@dataclass(frozen=True)
class BackupRequest:
    guild_id: int
    channel_id: int
    requester_id: int
    requester_description: str


@dataclass(frozen=True)
class BackupRuntime:
    environment: str
    database_name: str
    project_root: Path
    owner_id: int
    current_uid: int
    current_username: str
    source_script: Path
    deployed_script: Path
    reporting_exporter: Path
    reporting_python: Path
    current_executable: Path
    release_manifest: Path


@dataclass(frozen=True)
class BackupReleaseManifest:
    schema_version: int
    release_checkpoint: str
    backup_script_sha256: str
    reporting_exporter_sha256: str
    python_resolved_path: str
    python_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            'schema_version': self.schema_version,
            'release_checkpoint': self.release_checkpoint,
            'backup_script_sha256': self.backup_script_sha256,
            'reporting_exporter_sha256': self.reporting_exporter_sha256,
            'python_resolved_path': self.python_resolved_path,
            'python_sha256': self.python_sha256,
        }


@dataclass(frozen=True)
class BackupPreflight:
    source_digest: str
    release_checkpoint: str


@dataclass(frozen=True)
class BackupArtifact:
    label: str
    size_bytes: int
    modified_at: int


@dataclass(frozen=True)
class BackupResult:
    category: str
    returncode: int | None
    duration_seconds: float
    artifacts: tuple[BackupArtifact, ...] = ()


@dataclass(frozen=True)
class ActiveBackup:
    requester_id: int
    guild_id: int
    channel_id: int
    started_monotonic: float


def capture_runtime() -> BackupRuntime:
    """Freeze primitive runtime identity without reading production artifacts."""

    profile = settings.runtime_profile
    current_uid = os.geteuid()
    try:
        current_username = pwd.getpwuid(current_uid).pw_name
    except KeyError:
        current_username = ''
    return BackupRuntime(
        environment=str(profile.environment),
        database_name=str(profile.database_name),
        project_root=Path(profile.project_root),
        owner_id=int(settings.owner_id),
        current_uid=int(current_uid),
        current_username=str(current_username),
        source_script=Path(profile.project_root) / 'scripts/backup_db.sh',
        deployed_script=DEPLOYED_SCRIPT,
        reporting_exporter=(
            Path(profile.project_root) / 'scripts/export_reporting_duckdb.py'
        ),
        reporting_python=Path(profile.project_root) / '.venv/bin/python',
        current_executable=Path(sys.executable),
        release_manifest=(
            Path(profile.project_root) / RELEASE_MANIFEST_NAME
        ),
    )


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(64 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _assert_production_runtime(runtime: BackupRuntime) -> None:
    if (
        runtime.environment != PRODUCTION_ENVIRONMENT
        or runtime.database_name != PRODUCTION_DATABASE
        or runtime.project_root != PRODUCTION_ROOT
    ):
        raise BackupEnvironmentError(
            'Manual database backup is available only from the exact '
            'production runtime. No backup was started.'
        )
    if runtime.current_username != PRODUCTION_USER:
        raise BackupEnvironmentError(
            'The production backup must run as the reviewed Unix account. '
            'No backup was started.'
        )


def _lstat_regular(
    path: Path,
    *,
    label: str,
    current_uid: int,
    private: bool = False,
    executable: bool = False,
) -> os.stat_result:
    try:
        details = path.lstat()
    except OSError as exc:
        raise BackupSourceError(
            f'The {label} is unavailable. No backup was started.'
        ) from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise BackupSourceError(
            f'The {label} must be a regular non-symlink file. No backup was '
            'started.'
        )
    if details.st_uid != current_uid:
        raise BackupSourceError(
            f'The {label} has an unexpected owner. No backup was started.'
        )
    mode = stat.S_IMODE(details.st_mode)
    if mode & 0o022:
        raise BackupSourceError(
            f'The {label} is writable outside its owner. No backup was '
            'started.'
        )
    if private and mode & 0o077:
        raise BackupSourceError(
            f'The {label} is accessible outside its owner. No backup was '
            'started.'
        )
    if executable and not mode & stat.S_IXUSR:
        raise BackupSourceError(
            f'The {label} is not owner-executable. No backup was started.'
        )
    return details


def _git_output_sync(project_root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            [str(GIT_EXECUTABLE), *arguments],
            cwd=str(project_root),
            capture_output=True,
            check=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackupSourceError(
            'The production checkout could not be verified. No backup was '
            'started.'
        ) from exc
    return result.stdout.strip()


def _checkout_checkpoint(runtime: BackupRuntime) -> str:
    status = _git_output_sync(
        runtime.project_root,
        'status',
        '--porcelain',
        '--untracked-files=no',
    )
    if status:
        raise BackupSourceError(
            'The production checkout has tracked changes. No backup was '
            'started.'
        )
    checkpoint = _git_output_sync(
        runtime.project_root,
        'rev-parse',
        '--verify',
        'HEAD',
    )
    if not _CHECKPOINT.fullmatch(checkpoint):
        raise BackupSourceError(
            'The production checkout checkpoint is invalid. No backup was '
            'started.'
        )
    for path in (runtime.source_script, runtime.reporting_exporter):
        try:
            relative = path.relative_to(runtime.project_root)
        except ValueError as exc:
            raise BackupSourceError(
                'A backup source is outside the production checkout. No '
                'backup was started.'
            ) from exc
        _git_output_sync(
            runtime.project_root,
            'ls-files',
            '--error-unmatch',
            '--',
            relative.as_posix(),
        )
    return checkpoint


def _parse_release_manifest(path: Path) -> BackupReleaseManifest:
    try:
        with path.open(encoding='utf-8') as stream:
            value = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BackupSourceError(
            'The production backup release manifest could not be read. No '
            'backup was started.'
        ) from exc
    required = {
        'schema_version',
        'release_checkpoint',
        'backup_script_sha256',
        'reporting_exporter_sha256',
        'python_resolved_path',
        'python_sha256',
    }
    if not isinstance(value, dict) or set(value) != required:
        raise BackupSourceError(
            'The production backup release manifest has invalid fields. No '
            'backup was started.'
        )
    if type(value['schema_version']) is not int or (
        value['schema_version'] != RELEASE_MANIFEST_SCHEMA
    ):
        raise BackupSourceError(
            'The production backup release manifest schema is unsupported. '
            'No backup was started.'
        )
    checkpoint = value['release_checkpoint']
    digests = (
        value['backup_script_sha256'],
        value['reporting_exporter_sha256'],
        value['python_sha256'],
    )
    if not isinstance(checkpoint, str) or not _CHECKPOINT.fullmatch(checkpoint):
        raise BackupSourceError(
            'The production backup release checkpoint is invalid. No backup '
            'was started.'
        )
    if any(
        not isinstance(digest, str) or not _DIGEST.fullmatch(digest)
        for digest in digests
    ):
        raise BackupSourceError(
            'A production backup release digest is invalid. No backup was '
            'started.'
        )
    python_path = value['python_resolved_path']
    if not isinstance(python_path, str) or not Path(python_path).is_absolute():
        raise BackupSourceError(
            'The production backup runtime path is invalid. No backup was '
            'started.'
        )
    return BackupReleaseManifest(
        schema_version=value['schema_version'],
        release_checkpoint=checkpoint,
        backup_script_sha256=digests[0],
        reporting_exporter_sha256=digests[1],
        python_resolved_path=python_path,
        python_sha256=digests[2],
    )


def build_release_manifest_sync(
    runtime: BackupRuntime,
    *,
    expected_checkpoint: str,
) -> BackupReleaseManifest:
    """Build reviewed non-secret provenance without writing any file."""

    _assert_production_runtime(runtime)
    checkpoint = _checkout_checkpoint(runtime)
    if checkpoint != expected_checkpoint:
        raise BackupSourceError(
            'The requested release does not match the clean production '
            'checkout. No manifest was written.'
        )
    _lstat_regular(
        runtime.source_script,
        label='tracked backup script',
        current_uid=runtime.current_uid,
        executable=True,
    )
    _lstat_regular(
        runtime.deployed_script,
        label='deployed backup script',
        current_uid=runtime.current_uid,
        private=True,
        executable=True,
    )
    _lstat_regular(
        runtime.reporting_exporter,
        label='reporting exporter',
        current_uid=runtime.current_uid,
    )
    source_digest = _digest(runtime.source_script)
    if _digest(runtime.deployed_script) != source_digest:
        raise BackupSourceError(
            'The deployed backup script differs from reviewed source. No '
            'manifest was written.'
        )
    try:
        resolved_python = runtime.reporting_python.resolve(strict=True)
        same_runtime = os.path.samefile(
            runtime.reporting_python,
            runtime.current_executable,
        )
    except OSError as exc:
        raise BackupSourceError(
            'The reporting runtime could not be verified. No manifest was '
            'written.'
        ) from exc
    _lstat_regular(
        resolved_python,
        label='resolved reporting runtime',
        current_uid=runtime.current_uid,
        executable=True,
    )
    if not same_runtime:
        raise BackupSourceError(
            'The reporting exporter would not use the running bot interpreter. '
            'No manifest was written.'
        )
    return BackupReleaseManifest(
        schema_version=RELEASE_MANIFEST_SCHEMA,
        release_checkpoint=checkpoint,
        backup_script_sha256=source_digest,
        reporting_exporter_sha256=_digest(runtime.reporting_exporter),
        python_resolved_path=str(resolved_python),
        python_sha256=_digest(resolved_python),
    )


def write_release_manifest_sync(
    runtime: BackupRuntime,
    manifest: BackupReleaseManifest,
) -> None:
    """Atomically install one private production release trust record."""

    path = runtime.release_manifest
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        raise BackupSourceError('The release manifest path is unavailable.') from exc
    if existing is not None and (
        stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)
    ):
        raise BackupSourceError(
            'The release manifest target must be a regular non-symlink file.'
        )
    payload = (
        json.dumps(manifest.as_dict(), indent=2, sort_keys=True) + '\n'
    ).encode('utf-8')
    temporary_path = None
    file_descriptor = None
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f'.{path.name}.',
            suffix='.tmp',
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, 'wb') as output:
            file_descriptor = None
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError as exc:
        raise BackupSourceError(
            'The production backup release manifest could not be installed.'
        ) from exc
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def validate_runtime_sync(
    requester_id: int,
    runtime: BackupRuntime,
) -> BackupPreflight:
    """Fail closed before the host script can be spawned."""

    if int(requester_id) != int(runtime.owner_id):
        raise BackupPermissionError(
            'Only the configured bot owner can run a production backup.'
        )
    _assert_production_runtime(runtime)

    _lstat_regular(
        runtime.release_manifest,
        label='production backup release manifest',
        current_uid=runtime.current_uid,
        private=True,
    )
    manifest = _parse_release_manifest(runtime.release_manifest)
    checkpoint = _checkout_checkpoint(runtime)
    if checkpoint != manifest.release_checkpoint:
        raise BackupSourceError(
            'The production checkout does not match the reviewed backup '
            'release. No backup was started.'
        )
    _lstat_regular(
        runtime.source_script,
        label='tracked backup script',
        current_uid=runtime.current_uid,
        executable=True,
    )
    _lstat_regular(
        runtime.deployed_script,
        label='deployed backup script',
        current_uid=runtime.current_uid,
        private=True,
        executable=True,
    )
    _lstat_regular(
        runtime.reporting_exporter,
        label='reporting exporter',
        current_uid=runtime.current_uid,
    )
    try:
        resolved_python = runtime.reporting_python.resolve(strict=True)
        same_runtime = os.path.samefile(
            runtime.reporting_python,
            runtime.current_executable,
        )
    except OSError as exc:
        raise BackupSourceError(
            'The reporting runtime could not be verified. No backup was '
            'started.'
        ) from exc
    _lstat_regular(
        resolved_python,
        label='resolved reporting runtime',
        current_uid=runtime.current_uid,
        executable=True,
    )
    if not same_runtime or str(resolved_python) != manifest.python_resolved_path:
        raise BackupSourceError(
            'The reporting exporter is not bound to the reviewed bot runtime. '
            'No backup was started.'
        )

    try:
        source_digest = _digest(runtime.source_script)
        deployed_digest = _digest(runtime.deployed_script)
        exporter_digest = _digest(runtime.reporting_exporter)
        python_digest = _digest(resolved_python)
    except OSError as exc:
        raise BackupSourceError(
            'A reviewed backup release file could not be read safely. No '
            'backup was started.'
        ) from exc
    if (
        source_digest != manifest.backup_script_sha256
        or deployed_digest != manifest.backup_script_sha256
        or exporter_digest != manifest.reporting_exporter_sha256
        or python_digest != manifest.python_sha256
    ):
        raise BackupSourceError(
            'A backup executable differs from the reviewed release manifest. '
            'No backup was started.'
        )
    return BackupPreflight(
        source_digest=source_digest,
        release_checkpoint=checkpoint,
    )


async def validate_runtime(
    requester_id: int,
    runtime: BackupRuntime | None = None,
) -> BackupPreflight:
    frozen_runtime = runtime or capture_runtime()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _FILE_EXECUTOR,
        validate_runtime_sync,
        int(requester_id),
        frozen_runtime,
    )


def _artifact_paths(started_at: float) -> tuple[tuple[str, Path], ...]:
    weekday = time.strftime('%A', time.localtime(started_at))
    return (
        ('Full database', Path('/home/nelluk/polytopia_full_backup.sqlc')),
        (
            'Partial database',
            Path(f'/home/nelluk/backups/polytopia_bak-{weekday}.sqlc'),
        ),
        (
            'Public GameLog',
            Path('/home/nelluk/backups/polytopia_gamelogs.csv.gz'),
        ),
        (
            'Local images',
            Path(f'/home/nelluk/backups/polytopia_images-{weekday}.tar.gz'),
        ),
        (
            'Reporting snapshot',
            Path('/home/nelluk/backups/polytopia_reporting.duckdb'),
        ),
    )


def inspect_artifacts_sync(
    started_at: float,
    require_reporting: bool,
) -> tuple[BackupArtifact, ...]:
    artifacts = []
    paths = _artifact_paths(started_at)
    required = paths if require_reporting else paths[:-1]
    for label, path in required:
        try:
            details = path.stat()
        except OSError as exc:
            raise BackupExecutionError(
                f'{label} was not available after the backup process.'
            ) from exc
        if not stat.S_ISREG(details.st_mode) or details.st_size <= 0:
            raise BackupExecutionError(
                f'{label} was not a non-empty regular artifact after backup.'
            )
        if details.st_mtime < started_at - 2:
            raise BackupExecutionError(
                f'{label} was not refreshed by the backup process.'
            )
        artifacts.append(BackupArtifact(
            label=label,
            size_bytes=int(details.st_size),
            modified_at=int(details.st_mtime),
        ))
    return tuple(artifacts)


async def _inspect_artifacts(
    started_at: float,
    require_reporting: bool,
) -> tuple[BackupArtifact, ...]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _FILE_EXECUTOR,
        inspect_artifacts_sync,
        started_at,
        require_reporting,
    )


async def _read_bounded(stream) -> tuple[bytes, bool]:
    if stream is None:
        return b'', False
    retained = bytearray()
    truncated = False
    while True:
        block = await stream.read(4096)
        if not block:
            break
        remaining = MAX_CAPTURE_BYTES - len(retained)
        if remaining > 0:
            retained.extend(block[:remaining])
        if len(block) > remaining:
            truncated = True
    return bytes(retained), truncated


async def _spawn_process(script: Path):
    return await asyncio.create_subprocess_exec(
        str(script),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )


def _signal_process_group(process, requested_signal: signal.Signals) -> None:
    os.killpg(int(process.pid), requested_signal)


async def _terminate_process(process, wait_task: asyncio.Task) -> None:
    if wait_task.done():
        await asyncio.shield(wait_task)
        return
    try:
        _signal_process_group(process, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(
            asyncio.shield(wait_task),
            timeout=TERMINATE_GRACE_SECONDS,
        )
        return
    except asyncio.TimeoutError:
        pass
    try:
        _signal_process_group(process, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await asyncio.shield(wait_task)


async def _drain_output_tasks(stdout_task, stderr_task):
    """Drain both child pipes before surfacing either reader failure."""

    results = await asyncio.gather(
        stdout_task,
        stderr_task,
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return tuple(results)


async def _terminate_and_drain_process(
    process,
    wait_task: asyncio.Task,
    output_task: asyncio.Task,
) -> None:
    await _terminate_process(process, wait_task)
    await asyncio.shield(output_task)


async def _wait_for_cleanup(cleanup_task: asyncio.Task) -> None:
    """Keep cleanup shielded through every repeated caller cancellation."""

    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            continue
    cleanup_task.result()


def _bounded_diagnostic(value: bytes, truncated: bool) -> str:
    text = value.decode('utf-8', errors='replace').replace('\x00', '')
    text = ' '.join(text.split())
    if truncated:
        text += ' …[truncated]'
    return text


async def execute_backup(
    request: BackupRequest,
    *,
    runtime: BackupRuntime | None = None,
) -> BackupResult:
    """Revalidate, run, drain, and classify one production backup process."""

    frozen_runtime = runtime or capture_runtime()
    await validate_runtime(request.requester_id, frozen_runtime)
    started_at = time.time()
    started_monotonic = time.monotonic()
    logger.info(
        'Operator backup started requester=%s requester_description=%r '
        'guild=%s channel=%s source_digest_verified=true',
        request.requester_id,
        request.requester_description,
        request.guild_id,
        request.channel_id,
    )
    try:
        process = await _spawn_process(frozen_runtime.deployed_script)
    except OSError as exc:
        logger.exception(
            'Operator backup spawn failed requester=%s guild=%s channel=%s',
            request.requester_id,
            request.guild_id,
            request.channel_id,
        )
        raise BackupExecutionError(
            'The production backup process could not be started.'
        ) from exc

    stdout_task = asyncio.create_task(_read_bounded(process.stdout))
    stderr_task = asyncio.create_task(_read_bounded(process.stderr))
    output_task = asyncio.create_task(
        _drain_output_tasks(stdout_task, stderr_task)
    )
    wait_task = asyncio.create_task(process.wait())
    timed_out = False
    try:
        try:
            returncode = await asyncio.wait_for(
                asyncio.shield(wait_task),
                timeout=MAX_PROCESS_SECONDS,
            )
        except asyncio.TimeoutError:
            timed_out = True
            await _terminate_process(process, wait_task)
            returncode = None
        stdout_result, stderr_result = await asyncio.shield(output_task)
    except asyncio.CancelledError as cancellation:
        cleanup_task = asyncio.create_task(_terminate_and_drain_process(
            process,
            wait_task,
            output_task,
        ))
        await _wait_for_cleanup(cleanup_task)
        logger.warning(
            'Operator backup cancelled after child drain requester=%s '
            'guild=%s channel=%s duration=%.3f',
            request.requester_id,
            request.guild_id,
            request.channel_id,
            max(0.0, time.monotonic() - started_monotonic),
        )
        raise cancellation

    duration = max(0.0, time.monotonic() - started_monotonic)
    stdout, stdout_truncated = stdout_result
    stderr, stderr_truncated = stderr_result
    diagnostic = _bounded_diagnostic(
        stderr or stdout,
        stderr_truncated or stdout_truncated,
    )

    try:
        if timed_out:
            category = 'timeout'
            artifacts = ()
        elif returncode == 0:
            category = 'success'
            artifacts = await _inspect_artifacts(started_at, True)
        elif returncode == REPORTING_PARTIAL_EXIT:
            category = 'reporting_failed'
            artifacts = await _inspect_artifacts(started_at, False)
        elif returncode == LOCK_BUSY_EXIT:
            category = 'busy'
            artifacts = ()
        else:
            category = 'core_failed'
            artifacts = ()
    except BackupExecutionError:
        logger.exception(
            'Operator backup artifact validation failed requester=%s '
            'guild=%s channel=%s returncode=%s duration=%.3f',
            request.requester_id,
            request.guild_id,
            request.channel_id,
            returncode,
            duration,
        )
        raise

    log_method = logger.info if category in {'success', 'busy'} else logger.error
    log_method(
        'Operator backup result requester=%s requester_description=%r '
        'guild=%s channel=%s category=%s returncode=%s duration=%.3f '
        'diagnostic=%r',
        request.requester_id,
        request.requester_description,
        request.guild_id,
        request.channel_id,
        category,
        returncode,
        duration,
        diagnostic,
    )
    return BackupResult(
        category=category,
        returncode=returncode,
        duration_seconds=duration,
        artifacts=artifacts,
    )


class BackupCoordinator:
    """Reject overlapping in-process manual backups without queuing."""

    def __init__(self):
        self.active: ActiveBackup | None = None

    async def run(
        self,
        request: BackupRequest,
        *,
        runtime: BackupRuntime | None = None,
    ) -> BackupResult:
        if self.active is not None:
            raise BackupConflictError(
                'Another manual backup is already active in this bot process.'
            )
        self.active = ActiveBackup(
            requester_id=int(request.requester_id),
            guild_id=int(request.guild_id),
            channel_id=int(request.channel_id),
            started_monotonic=time.monotonic(),
        )
        try:
            return await execute_backup(request, runtime=runtime)
        finally:
            self.active = None


backup_coordinator = BackupCoordinator()


def format_result(result: BackupResult) -> str:
    duration = f'{result.duration_seconds:.1f}s'
    if result.category == 'busy':
        return (
            'Another scheduled or manual backup currently holds the host '
            'lock. No second backup was started.'
        )
    if result.category == 'timeout':
        return (
            f'The backup exceeded its {MAX_PROCESS_SECONDS // 60}-minute '
            'limit and its process group was stopped. Inspect the host '
            'artifacts and logs before retrying.'
        )
    if result.category == 'core_failed':
        return (
            f'The core backup process failed after {duration}. Previous '
            'validated artifacts were preserved where atomic publication '
            'applies. Inspect the structured host log before retrying.'
        )

    artifact_lines = '\n'.join(
        f'- **{artifact.label}:** {artifact.size_bytes:,} bytes; '
        f'<t:{artifact.modified_at}:F>'
        for artifact in result.artifacts
    )
    if result.category == 'reporting_failed':
        heading = (
            'Core disaster-recovery backup completed, but the DuckDB '
            'reporting export failed. The previous reporting snapshot was '
            'preserved.'
        )
    else:
        heading = 'Core backup and DuckDB reporting export completed.'
    return f'{heading}\nDuration: **{duration}**\n{artifact_lines}'
