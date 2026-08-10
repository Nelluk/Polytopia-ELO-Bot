"""Production-only owner backup orchestration with bounded process handling."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import logging
import os
from pathlib import Path
import pwd
import signal
import stat
import time

import settings


logger = logging.getLogger('polybot.' + __name__)

PRODUCTION_ENVIRONMENT = 'production'
PRODUCTION_DATABASE = 'polytopia2'
PRODUCTION_ROOT = Path('/home/nelluk/PolyBot39')
PRODUCTION_USER = 'nelluk'
DEPLOYED_SCRIPT = Path('/home/nelluk/backup_db.sh')
REPORTING_PARTIAL_EXIT = 20
LOCK_BUSY_EXIT = 75
MAX_PROCESS_SECONDS = 30 * 60
TERMINATE_GRACE_SECONDS = 10
MAX_CAPTURE_BYTES = 16 * 1024
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


@dataclass(frozen=True)
class BackupPreflight:
    source_digest: str


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
    )


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(64 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def validate_runtime_sync(
    requester_id: int,
    runtime: BackupRuntime,
) -> BackupPreflight:
    """Fail closed before the host script can be spawned."""

    if int(requester_id) != int(runtime.owner_id):
        raise BackupPermissionError(
            'Only the configured bot owner can run a production backup.'
        )
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

    source = runtime.source_script
    deployed = runtime.deployed_script
    try:
        source_stat = source.stat()
        deployed_stat = deployed.stat()
    except OSError as exc:
        raise BackupSourceError(
            'The reviewed backup source or deployed script is unavailable. '
            'No backup was started.'
        ) from exc
    if not stat.S_ISREG(source_stat.st_mode) or not stat.S_ISREG(
        deployed_stat.st_mode
    ):
        raise BackupSourceError(
            'The reviewed backup source and deployed script must be regular '
            'files. No backup was started.'
        )
    if deployed_stat.st_uid != runtime.current_uid:
        raise BackupSourceError(
            'The deployed backup script has an unexpected owner. No backup '
            'was started.'
        )
    if deployed_stat.st_mode & 0o077:
        raise BackupSourceError(
            'The deployed backup script is accessible outside its owner. No '
            'backup was started.'
        )
    if not deployed_stat.st_mode & stat.S_IXUSR:
        raise BackupSourceError(
            'The deployed backup script is not owner-executable. No backup '
            'was started.'
        )

    try:
        source_digest = _digest(source)
        deployed_digest = _digest(deployed)
    except OSError as exc:
        raise BackupSourceError(
            'The backup scripts could not be read safely. No backup was '
            'started.'
        ) from exc
    if source_digest != deployed_digest:
        raise BackupSourceError(
            'The deployed backup script differs from reviewed source. No '
            'backup was started.'
        )
    return BackupPreflight(source_digest=source_digest)


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
