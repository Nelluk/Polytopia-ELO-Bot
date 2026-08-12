"""Guarded logical export of the stopped host development database.

This module is deliberately development-only.  It publishes a custom-format
archive for the isolated container restore drill; it never restores a database
and never addresses a production database.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Mapping, Sequence

from modules import beta_operations, beta_readiness
from runtime_config import RuntimeProfile


DATABASE_NAME = 'polytopia_dev'
DATABASE_ROLE = 'polybot_dev'
DATABASE_HOSTS = frozenset({'localhost', '127.0.0.1'})
DATABASE_PORT = 5432
POSTGRES_MAJOR = 18
MINIMUM_HEADROOM_BYTES = 64 * 1024 * 1024
ARCHIVE_PREFIX = 'polybot-polytopia_dev'
BACKUP_RELATIVE_PATH = Path('deploy/container/backups')
CHECKPOINT_PATTERN = re.compile(r'^[0-9a-f]{40}$')
TIMESTAMP_PATTERN = re.compile(r'^\d{8}T\d{6}Z$')


class HostDevelopmentExportError(RuntimeError):
    """The host development export was unsafe or unsuccessful."""


@dataclass(frozen=True)
class ExportPlan:
    checkpoint: str
    backup_root: Path
    confirmation: str


@dataclass(frozen=True)
class ExportResult:
    archive: Path
    digest_path: Path
    sha256: str
    bytes_written: int
    sessions_before: int
    sessions_after: int


def build_plan(profile: RuntimeProfile, checkpoint: str) -> ExportPlan:
    """Validate the fixed development target and build an exact plan."""

    try:
        beta_readiness.validate_database_profile(profile)
        beta_operations.assert_beta_profile(profile)
    except (beta_readiness.ReadinessInventoryError,
            beta_operations.BetaOperationsError) as exc:
        raise HostDevelopmentExportError(str(exc)) from exc
    host = str(profile.database_host or '').strip()
    try:
        port = int(profile.database_port or DATABASE_PORT)
    except (TypeError, ValueError) as exc:
        raise HostDevelopmentExportError('Development database port is invalid.') from exc
    if host not in DATABASE_HOSTS or port != DATABASE_PORT:
        raise HostDevelopmentExportError(
            'Host export requires the local development PostgreSQL endpoint '
            f'on port {DATABASE_PORT}.'
        )
    if not CHECKPOINT_PATTERN.fullmatch(checkpoint):
        raise HostDevelopmentExportError(
            'Checkpoint must be one lowercase Git SHA-1.'
        )
    root = Path(profile.project_root).resolve()
    backup_root = (root / BACKUP_RELATIVE_PATH).resolve()
    try:
        backup_root.relative_to(root)
    except ValueError as exc:
        raise HostDevelopmentExportError(
            'Backup path escaped the development checkout.'
        ) from exc
    return ExportPlan(
        checkpoint=checkpoint,
        backup_root=backup_root,
        confirmation=f'EXPORT {DATABASE_NAME} {checkpoint}',
    )


def format_plan(plan: ExportPlan) -> str:
    return '\n'.join((
        'Host development database export plan',
        f'source database: {DATABASE_NAME}',
        f'source role: {DATABASE_ROLE}',
        'required writer state: durable beta stopped; writer lock held for '
        'the export',
        'session evidence: zero other source sessions sampled immediately '
        'before and after pg_dump',
        'limitation: those two samples do not prove that no transient session '
        'existed between them',
        f'archive directory: {plan.backup_root}',
        'writes: one validated custom-format archive and exact SHA-256 sidecar',
        f'confirmation: {plan.confirmation}',
    ))


def _private_directory(path: Path) -> None:
    if path.exists():
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise HostDevelopmentExportError(
                'Backup destination is not a safe directory.'
            )
        if info.st_uid != os.getuid():
            raise HostDevelopmentExportError('Backup destination has the wrong owner.')
        os.chmod(path, 0o700)
        return
    path.mkdir(parents=True, mode=0o700)
    os.chmod(path, 0o700)


def _client(path: str, label: str) -> str:
    resolved = shutil.which(path)
    if not resolved:
        raise HostDevelopmentExportError(f'{label} is unavailable.')
    return resolved


def _database_environment(profile: RuntimeProfile) -> dict[str, str]:
    return {
        **os.environ,
        'PGHOST': str(profile.database_host),
        'PGPORT': str(profile.database_port or DATABASE_PORT),
        'PGDATABASE': DATABASE_NAME,
        'PGUSER': DATABASE_ROLE,
        'PGPASSWORD': profile.database_password,
    }


def _run_text(
        argv: Sequence[str], *, environment: Mapping[str, str], label: str) -> str:
    try:
        result = subprocess.run(
            list(argv), env=dict(environment), capture_output=True, check=False,
            text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HostDevelopmentExportError(f'{label} could not complete.') from exc
    if result.returncode:
        raise HostDevelopmentExportError(
            f'{label} failed; no archive was published.'
        )
    return result.stdout.strip()


def _scalar(psql: str, query: str, *, environment: Mapping[str, str], label: str) -> str:
    return _run_text(
        (psql, '-X', '-v', 'ON_ERROR_STOP=1', '-Atqc', query),
        environment=environment,
        label=label,
    )


def _session_count(psql: str, *, environment: Mapping[str, str]) -> int:
    value = _scalar(
        psql,
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE datname = current_database() AND pid <> pg_backend_pid()",
        environment=environment,
        label='Development session observation',
    )
    try:
        count = int(value)
    except ValueError as exc:
        raise HostDevelopmentExportError(
            'Development session count was not numeric.'
        ) from exc
    if count < 0:
        raise HostDevelopmentExportError('Development session count was invalid.')
    return count


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def export_database(
        profile: RuntimeProfile,
        plan: ExportPlan,
        *,
        confirmation: str,
        timestamp: str) -> ExportResult:
    """Hold the beta writer lock and atomically publish one logical archive."""

    if confirmation != plan.confirmation:
        raise HostDevelopmentExportError(
            'Export confirmation does not match the exact plan.'
        )
    if not TIMESTAMP_PATTERN.fullmatch(timestamp):
        raise HostDevelopmentExportError('Export timestamp is invalid.')
    if build_plan(profile, plan.checkpoint) != plan:
        raise HostDevelopmentExportError('Export plan no longer matches the runtime profile.')

    paths = beta_operations.operation_paths(profile, create=True)
    psql = _client('psql', 'psql')
    pg_dump = _client('pg_dump', 'pg_dump')
    pg_restore = _client('pg_restore', 'pg_restore')
    environment = _database_environment(profile)
    archive_name = f'{ARCHIVE_PREFIX}-{timestamp}-{plan.checkpoint}.dump'
    archive = plan.backup_root / archive_name
    digest_path = plan.backup_root / f'{archive_name}.sha256'

    _private_directory(plan.backup_root)
    if archive.exists() or archive.is_symlink() or digest_path.exists() or digest_path.is_symlink():
        raise HostDevelopmentExportError(
            'Refusing to replace an existing export artifact.'
        )

    temporary_archive: Path | None = None
    temporary_digest: Path | None = None
    published_archive = False
    try:
        with beta_operations.BetaWriterLock(paths.writer_lock):
            identity = _scalar(
                psql,
                "SELECT current_database() || ':' || current_user",
                environment=environment,
                label='Development database identity check',
            )
            if identity != f'{DATABASE_NAME}:{DATABASE_ROLE}':
                raise HostDevelopmentExportError(
                    'Live database identity is not the approved development target.'
                )
            version = _scalar(
                psql, 'SHOW server_version_num', environment=environment,
                label='PostgreSQL version check',
            )
            if not version.isdigit() or int(version) // 10000 != POSTGRES_MAJOR:
                raise HostDevelopmentExportError(
                    f'Host export requires PostgreSQL major {POSTGRES_MAJOR}.'
                )
            sessions_before = _session_count(psql, environment=environment)
            if sessions_before:
                raise HostDevelopmentExportError(
                    'Refusing export while another source-database session exists.'
                )
            database_bytes_raw = _scalar(
                psql, 'SELECT pg_database_size(current_database())',
                environment=environment, label='Development database size check',
            )
            try:
                database_bytes = int(database_bytes_raw)
            except ValueError as exc:
                raise HostDevelopmentExportError(
                    'Development database size was not numeric.'
                ) from exc
            available = shutil.disk_usage(plan.backup_root).free
            if available < database_bytes + MINIMUM_HEADROOM_BYTES:
                raise HostDevelopmentExportError(
                    'Backup destination lacks source-size plus 64 MiB headroom.'
                )

            archive_fd, archive_temp_name = tempfile.mkstemp(
                prefix='.polybot-host-export.', dir=plan.backup_root,
            )
            os.close(archive_fd)
            temporary_archive = Path(archive_temp_name)
            os.chmod(temporary_archive, 0o600)
            _run_text((
                pg_dump,
                f'--host={profile.database_host}',
                f'--port={profile.database_port or DATABASE_PORT}',
                f'--username={DATABASE_ROLE}',
                f'--dbname={DATABASE_NAME}',
                '--format=custom', '--compress=9', '--no-owner', '--no-acl',
                '--lock-wait-timeout=10s', f'--file={temporary_archive}',
            ), environment=environment, label='Host development pg_dump')
            if not temporary_archive.is_file() or temporary_archive.stat().st_size <= 0:
                raise HostDevelopmentExportError(
                    'pg_dump produced an empty archive.'
                )
            _run_text(
                (pg_restore, '--list', str(temporary_archive)),
                environment=environment,
                label='Temporary archive validation',
            )
            sessions_after = _session_count(psql, environment=environment)
            if sessions_after:
                raise HostDevelopmentExportError(
                    'Another source-database session was present after pg_dump; '
                    'no archive was published.'
                )
            digest = _sha256(temporary_archive)
            digest_fd, digest_temp_name = tempfile.mkstemp(
                prefix='.polybot-host-digest.', dir=plan.backup_root,
            )
            temporary_digest = Path(digest_temp_name)
            with os.fdopen(digest_fd, 'w', encoding='ascii') as stream:
                stream.write(f'{digest}  {archive_name}\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary_digest, 0o600)
            with temporary_archive.open('rb') as stream:
                os.fsync(stream.fileno())
            os.replace(temporary_archive, archive)
            temporary_archive = None
            published_archive = True
            os.replace(temporary_digest, digest_path)
            temporary_digest = None
            directory_fd = os.open(
                plan.backup_root,
                os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            result = ExportResult(
                archive=archive,
                digest_path=digest_path,
                sha256=digest,
                bytes_written=archive.stat().st_size,
                sessions_before=sessions_before,
                sessions_after=sessions_after,
            )
            published_archive = False
            return result
    finally:
        if temporary_archive is not None:
            temporary_archive.unlink(missing_ok=True)
        if temporary_digest is not None:
            temporary_digest.unlink(missing_ok=True)
        if published_archive:
            archive.unlink(missing_ok=True)
            digest_path.unlink(missing_ok=True)
