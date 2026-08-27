"""Development-only structured beta feedback capture for ``/staffhelp``.

The JSONL file written by this module is the authoritative WB1 feedback
stream.  Discord staff-channel messages are deliberately a later mirror and
are never part of the persistence transaction.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
import unicodedata
from io import BytesIO
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

import discord

from modules.beta_operations import (
    BETA_GUILD_ID,
    BETA_STAFFHELP_MIRROR_CHANNEL_ID,
)
from runtime_config import RuntimeProfile, get_runtime_profile


logger = logging.getLogger('polybot.' + __name__)

SCHEMA_VERSION = 1
MAX_SUMMARY_LENGTH = 160
MAX_DETAILS_LENGTH = 4000
MAX_CONTEXT_LENGTH = 1000
MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 20 * 1024 * 1024

_REPORT_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]{20,}$')
_CHECKPOINT_PATTERN = re.compile(r'^[A-Za-z0-9._:/-]{1,128}$')
_GAME_ID_PATTERN = re.compile(r'\b\d{4,6}\b')
_COMMAND_REFERENCE_PATTERN = re.compile(r'(?<!\w)(?:/|\$)[a-z][a-z0-9_-]{1,63}', re.I)
_SAFE_FILENAME_PATTERN = re.compile(r'[^A-Za-z0-9._ -]+')
_ALLOWED_ATTACHMENT_TYPES = {
    'image/gif': '.gif',
    'image/jpeg': '.jpg',
    'image/png': '.png',
    'image/webp': '.webp',
    'application/pdf': '.pdf',
    'text/markdown': '.md',
    'text/plain': '.txt',
}
_ALLOWED_ATTACHMENT_EXTENSIONS = {
    '.gif': 'image/gif',
    '.jpeg': 'image/jpeg',
    '.jpg': 'image/jpeg',
    '.md': 'text/markdown',
    '.pdf': 'application/pdf',
    '.png': 'image/png',
    '.txt': 'text/plain',
    '.webp': 'image/webp',
}


class FeedbackError(Exception):
    """Base class for expected feedback intake failures."""


class FeedbackValidationError(FeedbackError, ValueError):
    """The reporter supplied an invalid or over-limit value."""


class FeedbackStorageError(FeedbackError):
    """The authoritative local record could not be committed."""

    def __init__(self, message: str, *, may_have_committed: bool = False):
        super().__init__(message)
        self.may_have_committed = bool(may_have_committed)


class FeedbackRuntimeGateError(FeedbackStorageError):
    """The beta store was requested outside a validated development profile."""


@dataclass(frozen=True, slots=True)
class FeedbackPaths:
    """Validated paths for one development runtime."""

    project_root: Path
    log_root: Path
    root: Path
    record_file: Path
    attachments_root: Path
    staging_root: Path


@dataclass(frozen=True, slots=True)
class AttachmentInput:
    """Immutable attachment bytes captured before filesystem work."""

    attachment_id: int | None
    filename: str
    content_type: str
    extension: str
    data: bytes
    source_url: str | None = None

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass(frozen=True, slots=True)
class FeedbackReportDraft:
    """Only primitive, event-loop-captured values submitted to the worker."""

    category: str
    summary: str
    details: str
    context: str | None
    game_id: int | None
    command_reference: str | None
    requester_id: int
    requester_display_name: str
    guild_id: int
    channel_id: int
    source: str
    timestamp_utc: str
    attachments: tuple[AttachmentInput, ...] = ()
    git_checkpoint: str | None = None


@dataclass(frozen=True, slots=True)
class StoredAttachment:
    """Immutable metadata and bytes for a successfully staged attachment."""

    attachment_id: int | None
    filename: str
    content_type: str
    size: int
    sha256: str
    storage_name: str
    data: bytes


@dataclass(frozen=True, slots=True)
class StoredReport:
    """An authoritative record that has been durably appended."""

    report_id: str
    record: Mapping[str, Any]
    attachments: tuple[StoredAttachment, ...]
    paths: FeedbackPaths


@dataclass(frozen=True, slots=True)
class NativeSubmission:
    """Native result, including a non-fatal relay warning."""

    report: StoredReport
    relay_ok: bool


@dataclass(frozen=True, slots=True)
class FeedbackReadIssue:
    """A malformed or truncated JSONL line that was not presented as valid."""

    line_number: int
    kind: str
    message: str


@dataclass(frozen=True, slots=True)
class FeedbackReadResult:
    records: tuple[Mapping[str, Any], ...]
    issues: tuple[FeedbackReadIssue, ...]
    present: bool


_APPEND_LOCK = threading.Lock()
_DEFAULT_STORE: FeedbackStore | None = None


def utc_timestamp(now: datetime | None = None) -> str:
    """Return a compact, explicit UTC timestamp for a captured event."""

    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def safe_display_name(member: Any) -> str:
    """Capture a display name without allowing mentions or markdown control."""

    discord_id = int(member.id)
    raw_name = str(
        getattr(member, 'display_name', None)
        or getattr(member, 'name', None)
        or f'user-{discord_id}'
    )
    # Remove controls before escaping mentions.  The zero-width character
    # discord.py inserts for @everyone is itself a format control and must not
    # be stripped after the escape has made the value safe.
    raw_name = _sanitize_text(raw_name, MAX_CONTEXT_LENGTH)
    escaped = discord.utils.escape_mentions(
        discord.utils.escape_markdown(raw_name),
    )
    return escaped[:MAX_CONTEXT_LENGTH] or f'user-{discord_id}'


def _sanitize_text(value: Any, limit: int | None = None) -> str:
    if value is None:
        return ''
    text = unicodedata.normalize('NFKC', str(value))
    cleaned = ''.join(
        character
        for character in text
        if character in '\n\r\t' or not unicodedata.category(character).startswith('C')
    )
    cleaned = cleaned.strip()
    if limit is not None:
        cleaned = cleaned[:limit]
    return cleaned


def _optional_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = _sanitize_text(value, limit)
    return text or None


def _positive_id(value: Any, field_name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise FeedbackValidationError(f'{field_name} must be an integer.') from exc
    if parsed <= 0:
        raise FeedbackValidationError(f'{field_name} must be positive.')
    return parsed


def _reference_fields(context: str | None) -> tuple[int | None, str | None]:
    if not context:
        return None, None
    game_match = _GAME_ID_PATTERN.search(context)
    command_match = _COMMAND_REFERENCE_PATTERN.search(context)
    return (
        int(game_match.group(0)) if game_match else None,
        command_match.group(0) if command_match else None,
    )


def build_report_draft(
        *,
        category: str,
        summary: str,
        details: str,
        context: str | None,
        requester_id: int,
        requester_display_name: str,
        guild_id: int,
        channel_id: int,
        source: str,
        attachments: Iterable[AttachmentInput] = (),
        game_id: int | None = None,
        command_reference: str | None = None,
        timestamp: str | None = None,
        git_checkpoint: str | None = None,
) -> FeedbackReportDraft:
    """Validate and freeze a report before any blocking storage work."""

    normalized_category = _sanitize_text(category, 20).lower()
    if normalized_category not in {'help', 'bug', 'feature'}:
        raise FeedbackValidationError('Choose help, bug, or feature.')
    normalized_summary = _sanitize_text(summary)
    if not normalized_summary:
        raise FeedbackValidationError('A short summary is required.')
    if len(normalized_summary) > MAX_SUMMARY_LENGTH:
        raise FeedbackValidationError('The summary is too long.')
    normalized_details = _sanitize_text(details)
    if not normalized_details:
        raise FeedbackValidationError('A detailed description is required.')
    if len(normalized_details) > MAX_DETAILS_LENGTH:
        raise FeedbackValidationError('The detailed description is too long.')
    normalized_context = _optional_text(context, MAX_CONTEXT_LENGTH)
    normalized_source = _sanitize_text(source, 20).lower()
    if normalized_source != 'slash':
        raise FeedbackValidationError('The feedback source is invalid.')

    frozen_attachments = tuple(attachments)
    if len(frozen_attachments) > MAX_ATTACHMENTS:
        raise FeedbackValidationError(
            f'You may attach at most {MAX_ATTACHMENTS} files.'
        )
    total_size = 0
    for attachment in frozen_attachments:
        if not isinstance(attachment, AttachmentInput):
            raise FeedbackValidationError('An attachment could not be captured.')
        if attachment.size > MAX_ATTACHMENT_BYTES:
            raise FeedbackValidationError(
                f'Each attachment must be at most {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB.'
            )
        total_size += attachment.size
    if total_size > MAX_TOTAL_ATTACHMENT_BYTES:
        raise FeedbackValidationError(
            f'Attachments may total at most {MAX_TOTAL_ATTACHMENT_BYTES // (1024 * 1024)} MB.'
        )

    normalized_game_id = None if game_id is None else _positive_id(game_id, 'game_id')
    normalized_command = _optional_text(command_reference, 100)
    checkpoint = _optional_text(git_checkpoint, 128)
    if checkpoint and not _CHECKPOINT_PATTERN.fullmatch(checkpoint):
        checkpoint = None

    return FeedbackReportDraft(
        category=normalized_category,
        summary=normalized_summary,
        details=normalized_details,
        context=normalized_context,
        game_id=normalized_game_id,
        command_reference=normalized_command,
        requester_id=_positive_id(requester_id, 'requester_id'),
        requester_display_name=safe_display_name(
            type('CapturedMember', (), {
                'id': requester_id,
                'display_name': requester_display_name,
            })(),
        ),
        guild_id=_positive_id(guild_id, 'guild_id'),
        channel_id=_positive_id(channel_id, 'channel_id'),
        source=normalized_source,
        timestamp_utc=timestamp or utc_timestamp(),
        attachments=frozen_attachments,
        git_checkpoint=checkpoint,
    )


def _safe_attachment_filename(value: Any, fallback: str) -> str:
    filename = unicodedata.normalize('NFKC', str(value or fallback))
    filename = filename.replace('/', '_').replace('\\', '_').replace('..', '_')
    filename = _SAFE_FILENAME_PATTERN.sub('_', filename).strip(' .')
    return (filename[:120] or fallback)


def _attachment_type(filename: str, content_type: Any) -> tuple[str, str]:
    normalized_type = str(content_type or '').split(';', 1)[0].strip().lower()
    if normalized_type in _ALLOWED_ATTACHMENT_TYPES:
        return normalized_type, _ALLOWED_ATTACHMENT_TYPES[normalized_type]
    extension = Path(filename).suffix.lower()
    guessed_type = _ALLOWED_ATTACHMENT_EXTENSIONS.get(extension)
    if normalized_type in {'', 'application/octet-stream'} and guessed_type:
        return guessed_type, _ALLOWED_ATTACHMENT_TYPES[guessed_type]
    raise FeedbackValidationError(
        'Attachments must be PNG, JPEG, WebP, GIF, PDF, Markdown, or plain text.'
    )


async def capture_attachments(attachments: Sequence[Any]) -> tuple[AttachmentInput, ...]:
    """Read only Discord-provided attachments within explicit size/type limits."""

    if len(attachments) > MAX_ATTACHMENTS:
        raise FeedbackValidationError(
            f'You may attach at most {MAX_ATTACHMENTS} files.'
        )
    captured: list[AttachmentInput] = []
    total_size = 0
    for index, attachment in enumerate(attachments, start=1):
        filename = _safe_attachment_filename(
            getattr(attachment, 'filename', None),
            f'attachment-{index}',
        )
        content_type, extension = _attachment_type(
            filename,
            getattr(attachment, 'content_type', None),
        )
        declared_size = getattr(attachment, 'size', None)
        if declared_size is not None:
            try:
                if int(declared_size) > MAX_ATTACHMENT_BYTES:
                    raise FeedbackValidationError(
                        f'Each attachment must be at most {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB.'
                    )
            except (TypeError, ValueError) as exc:
                raise FeedbackValidationError('An attachment size is invalid.') from exc
        read = getattr(attachment, 'read', None)
        if not callable(read):
            raise FeedbackValidationError('An attachment could not be read.')
        try:
            data = bytes(await read())
        except Exception as exc:
            raise FeedbackValidationError(
                'An attachment could not be downloaded from Discord.'
            ) from exc
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise FeedbackValidationError(
                f'Each attachment must be at most {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB.'
            )
        total_size += len(data)
        if total_size > MAX_TOTAL_ATTACHMENT_BYTES:
            raise FeedbackValidationError(
                f'Attachments may total at most {MAX_TOTAL_ATTACHMENT_BYTES // (1024 * 1024)} MB.'
            )
        attachment_id = getattr(attachment, 'id', None)
        try:
            attachment_id = int(attachment_id) if attachment_id is not None else None
        except (TypeError, ValueError):
            attachment_id = None
        source_url = getattr(attachment, 'url', None)
        captured.append(AttachmentInput(
            attachment_id=attachment_id,
            filename=filename,
            content_type=content_type,
            extension=extension,
            data=data,
            source_url=(str(source_url) if source_url else None),
        ))
    return tuple(captured)


def _validate_profile(profile: RuntimeProfile) -> tuple[Path, Path]:
    if getattr(profile, 'environment', None) != 'development':
        raise FeedbackRuntimeGateError(
            'The beta feedback store is available only in the development runtime.'
        )
    project_root = Path(profile.project_root).resolve()
    log_root = Path(profile.log_root).resolve()
    production_log_root = (project_root / 'logs').resolve()
    if log_root == production_log_root:
        raise FeedbackRuntimeGateError(
            'The development feedback store cannot use the production log root.'
        )
    try:
        log_root.relative_to(project_root)
    except ValueError as exc:
        raise FeedbackRuntimeGateError(
            'The development feedback store must remain inside the checkout.'
        ) from exc
    return project_root, log_root


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _assert_not_symlink(path: Path) -> os.stat_result | None:
    info = _lstat(path)
    if info is not None and stat.S_ISLNK(info.st_mode):
        raise FeedbackStorageError(f'Feedback path is a symlink: {path.name}')
    return info


def _ensure_directory(path: Path, mode: int) -> None:
    info = _assert_not_symlink(path)
    if info is None:
        try:
            path.mkdir(mode=mode, parents=False)
        except FileExistsError:
            info = _assert_not_symlink(path)
        else:
            info = _lstat(path)
    if info is None or not stat.S_ISDIR(info.st_mode):
        raise FeedbackStorageError(f'Feedback path is not a directory: {path.name}')
    try:
        os.chmod(path, mode)
    except OSError as exc:
        raise FeedbackStorageError(
            f'Could not set restrictive permissions on {path.name}.'
        ) from exc


def _ensure_directory_tree(path: Path, mode: int) -> None:
    missing: list[Path] = []
    current = path
    while _lstat(current) is None:
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for directory in reversed(missing):
        _ensure_directory(directory, mode)
    _ensure_directory(path, mode)


def feedback_paths(profile: RuntimeProfile | None = None, *, create: bool = False) -> FeedbackPaths:
    """Return the same gated path set for the writer and read-only utility."""

    selected_profile = profile or get_runtime_profile()
    project_root, log_root = _validate_profile(selected_profile)
    root = log_root / 'beta-feedback'
    record_file = root / 'reports.jsonl'
    attachments_root = root / 'attachments'
    staging_root = attachments_root / '.staging'

    if create:
        _ensure_directory_tree(log_root, 0o750)
        _ensure_directory(root, 0o700)
        _ensure_directory(attachments_root, 0o700)
        _ensure_directory(staging_root, 0o700)
    else:
        for directory in (root, attachments_root, staging_root):
            info = _assert_not_symlink(directory)
            if info is not None and not stat.S_ISDIR(info.st_mode):
                raise FeedbackStorageError(
                    f'Feedback path is not a directory: {directory.name}'
                )
        info = _assert_not_symlink(record_file)
        if info is not None and not stat.S_ISREG(info.st_mode):
            raise FeedbackStorageError(
                'The beta feedback JSONL path is not a regular file.'
            )
    return FeedbackPaths(
        project_root=project_root,
        log_root=log_root,
        root=root,
        record_file=record_file,
        attachments_root=attachments_root,
        staging_root=staging_root,
    )


def _safe_checkpoint(project_root: Path) -> str:
    try:
        completed = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=str(project_root),
            capture_output=True,
            check=True,
            text=True,
            timeout=0.75,
        )
    except (OSError, subprocess.SubprocessError):
        return 'unknown'
    value = completed.stdout.strip()
    return value if _CHECKPOINT_PATTERN.fullmatch(value) else 'unknown'


def _new_report_id() -> str:
    # Keep generated IDs safe as standalone command-line option values.  A
    # raw URL-safe token may begin with ``-``, which argparse interprets as a
    # new option when an operator runs ``--report-id ID``.
    return f'r{secrets.token_urlsafe(18).rstrip("=")}'


def _report_id_is_safe(report_id: str) -> bool:
    return bool(_REPORT_ID_PATTERN.fullmatch(report_id))


def _remove_controlled_directory(path: Path) -> None:
    info = _assert_not_symlink(path)
    if info is None:
        return
    if not stat.S_ISDIR(info.st_mode):
        raise FeedbackStorageError(f'Cannot clean non-directory staging path: {path.name}')
    shutil.rmtree(path)


def _write_attachment(path: Path, data: bytes) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
    try:
        file_descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise FeedbackStorageError(
            f'Could not create feedback attachment {path.name}.'
        ) from exc
    try:
        offset = 0
        while offset < len(data):
            written = os.write(file_descriptor, data[offset:])
            if written <= 0:
                raise OSError('short attachment write')
            offset += written
        os.fsync(file_descriptor)
        os.fchmod(file_descriptor, 0o600)
    except OSError as exc:
        raise FeedbackStorageError(
            f'Could not persist feedback attachment {path.name}.'
        ) from exc
    finally:
        os.close(file_descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
    flags |= getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
    try:
        file_descriptor = os.open(path, flags)
    except OSError as exc:
        raise FeedbackStorageError(
            f'Could not open feedback directory {path.name} for synchronization.'
        ) from exc
    try:
        os.fsync(file_descriptor)
    except OSError as exc:
        raise FeedbackStorageError(
            f'Could not synchronize feedback directory {path.name}.'
        ) from exc
    finally:
        os.close(file_descriptor)


def _append_record_line(path: Path, payload: bytes) -> None:
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    flags |= getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
    try:
        file_descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise FeedbackStorageError(
            'Could not open the beta feedback JSONL stream.'
        ) from exc
    bytes_written = 0
    try:
        os.fchmod(file_descriptor, 0o600)
        while bytes_written < len(payload):
            written = os.write(file_descriptor, payload[bytes_written:])
            if written <= 0:
                raise OSError('short JSONL write')
            bytes_written += written
        os.fsync(file_descriptor)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise FeedbackStorageError(
            'The beta feedback JSONL append may not have completed.',
            may_have_committed=bytes_written > 0,
        ) from exc
    finally:
        try:
            os.close(file_descriptor)
        except OSError as exc:
            if bytes_written:
                raise FeedbackStorageError(
                    'The beta feedback JSONL close may not have completed.',
                    may_have_committed=True,
                ) from exc


def _record_attachment(attachment: AttachmentInput, storage_name: str) -> tuple[StoredAttachment, dict[str, Any]]:
    digest = hashlib.sha256(attachment.data).hexdigest()
    stored = StoredAttachment(
        attachment_id=attachment.attachment_id,
        filename=attachment.filename,
        content_type=attachment.content_type,
        size=attachment.size,
        sha256=digest,
        storage_name=storage_name,
        data=attachment.data,
    )
    metadata = {
        'attachment_id': attachment.attachment_id,
        'content_type': attachment.content_type,
        'filename': attachment.filename,
        'sha256': digest,
        'size': attachment.size,
        'storage_name': storage_name,
    }
    return stored, metadata


def _record_payload(draft: FeedbackReportDraft, report_id: str, checkpoint: str,
                    attachment_metadata: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        'schema_version': SCHEMA_VERSION,
        'report_id': report_id,
        'category': draft.category,
        'summary': draft.summary,
        'details': draft.details,
        'context': draft.context,
        'game_id': draft.game_id,
        'command_reference': draft.command_reference,
        'requester_id': draft.requester_id,
        'requester_display_name': draft.requester_display_name,
        'guild_id': draft.guild_id,
        'channel_id': draft.channel_id,
        'source': draft.source,
        'timestamp_utc': draft.timestamp_utc,
        'git_checkpoint': checkpoint,
        'attachments': list(attachment_metadata),
    }


class FeedbackStore:
    """One-worker append-only store with cancellation-drain semantics."""

    def __init__(self, profile: RuntimeProfile | None = None):
        self.profile = profile
        self.executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix='polybot-beta-feedback',
        )

    def _append_sync(self, draft: FeedbackReportDraft) -> StoredReport:
        paths = feedback_paths(self.profile, create=True)
        report_id = _new_report_id()
        if not _report_id_is_safe(report_id):
            raise FeedbackStorageError('Could not generate a safe feedback report ID.')

        with _APPEND_LOCK:
            stage_directory: Path | None = None
            attachment_directory: Path | None = None
            published_attachment_directory = False
            stored_attachments: list[StoredAttachment] = []
            attachment_metadata: list[Mapping[str, Any]] = []
            try:
                if draft.attachments:
                    stage_directory = Path(tempfile.mkdtemp(
                        prefix='stage-',
                        dir=paths.staging_root,
                    ))
                    os.chmod(stage_directory, 0o700)
                    for index, attachment in enumerate(draft.attachments, start=1):
                        storage_name = f'attachment-{index:02d}{attachment.extension}'
                        file_path = stage_directory / storage_name
                        stored, metadata = _record_attachment(attachment, storage_name)
                        _write_attachment(file_path, attachment.data)
                        stored_attachments.append(stored)
                        attachment_metadata.append(metadata)

                    attachment_directory = paths.attachments_root / report_id
                    if _lstat(attachment_directory) is not None:
                        raise FeedbackStorageError('The generated report ID already exists.')
                    os.rename(stage_directory, attachment_directory)
                    os.chmod(attachment_directory, 0o700)
                    _fsync_directory(paths.attachments_root)
                    stage_directory = None
                    published_attachment_directory = True

                checkpoint = (
                    draft.git_checkpoint
                    or _safe_checkpoint(paths.project_root)
                )
                record = _record_payload(
                    draft,
                    report_id,
                    checkpoint,
                    attachment_metadata,
                )
                encoded_record = (
                    json.dumps(
                        record,
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(',', ':'),
                    ).encode('utf-8') + b'\n'
                )
                _append_record_line(paths.record_file, encoded_record)
                return StoredReport(
                    report_id=report_id,
                    record=MappingProxyType(record),
                    attachments=tuple(stored_attachments),
                    paths=paths,
                )
            except FeedbackStorageError as exc:
                if stage_directory is not None:
                    try:
                        _remove_controlled_directory(stage_directory)
                    except FeedbackStorageError:
                        logger.error(
                            'Could not clean beta feedback staging directory; report not acknowledged.',
                            exc_info=True,
                        )
                if attachment_directory is not None and not exc.may_have_committed:
                    try:
                        _remove_controlled_directory(attachment_directory)
                    except FeedbackStorageError:
                        logger.error(
                            'Could not clean beta feedback attachment directory; report not acknowledged.',
                            exc_info=True,
                        )
                if published_attachment_directory and exc.may_have_committed:
                    logger.warning(
                        'Beta feedback append may be uncertain after attachment publication; '
                        'report was not acknowledged (report_id=%s).',
                        report_id,
                    )
                raise
            except Exception as exc:
                if stage_directory is not None:
                    try:
                        _remove_controlled_directory(stage_directory)
                    except Exception:
                        logger.error(
                            'Could not clean unexpected beta feedback staging failure.',
                            exc_info=True,
                        )
                if attachment_directory is not None and not published_attachment_directory:
                    try:
                        _remove_controlled_directory(attachment_directory)
                    except Exception:
                        logger.error(
                            'Could not clean unexpected beta feedback attachment failure.',
                            exc_info=True,
                        )
                raise FeedbackStorageError(
                    'The beta feedback record could not be stored.'
                ) from exc

    async def store(self, draft: FeedbackReportDraft) -> StoredReport:
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self.executor, self._append_sync, draft)
        try:
            # Polling keeps the event loop responsive on the supported Python
            # runtime even when a worker completes while its cross-thread
            # future callback is waiting for the loop wakeup pipe.
            while not future.done():
                await asyncio.sleep(0.001)
            return future.result()
        except asyncio.CancelledError:
            task = asyncio.current_task()
            while not future.done():
                if task is not None and hasattr(task, 'uncancel'):
                    task.uncancel()
                await asyncio.sleep(0.001)
            # Retrieve the worker exception before re-raising cancellation.
            try:
                completed = future.result()
            except Exception:
                logger.debug(
                    'Cancelled feedback submission completed with a worker failure.',
                    exc_info=True,
                )
            else:
                if isinstance(completed, StoredReport):
                    logger.warning(
                        'Cancelled beta feedback submission committed a report; '
                        'no acknowledgement was sent (report_id=%s).',
                        completed.report_id,
                    )
            raise


def _default_feedback_store() -> FeedbackStore:
    global _DEFAULT_STORE
    profile = get_runtime_profile()
    if _DEFAULT_STORE is None or _DEFAULT_STORE.profile is not profile:
        _DEFAULT_STORE = FeedbackStore(profile)
    return _DEFAULT_STORE


async def store_report(draft: FeedbackReportDraft) -> StoredReport:
    return await _default_feedback_store().store(draft)


def staff_help_channel(bot: Any, guild_id: int) -> Any | None:
    """Resolve the configured staff channel without touching the feedback store."""

    try:
        import settings

        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return None
        if (
                getattr(getattr(settings, 'runtime_profile', None), 'environment', None)
                == 'development'
                and int(guild_id) == BETA_GUILD_ID):
            configured_id = BETA_STAFFHELP_MIRROR_CHANNEL_ID
        else:
            configured_id = settings.guild_setting(int(guild_id), 'staff_help_channel')
        if not configured_id:
            return None
        return guild.get_channel(int(configured_id))
    except Exception:
        return None


def _relay_chunks(value: str, limit: int = 1750) -> list[str]:
    if not value:
        return ['']
    return [value[index:index + limit] for index in range(0, len(value), limit)]


async def relay_native(channel: Any, report: StoredReport) -> None:
    """Mirror a committed native report into the configured staff channel."""

    record = report.record
    requester = (
        f"{str(record['requester_display_name'])[:120]} "
        f"(`{record['requester_id']}`)"
    )
    header_lines = [
        f"Beta feedback `{report.report_id}`",
        f"Category: `{record['category']}` | Source: `{record['source']}`",
        f"Requester: {requester}",
        f"Guild/channel: `{record['guild_id']}` / `{record['channel_id']}`",
        f"Summary: {record['summary']}",
    ]
    if record.get('game_id') is not None:
        header_lines.append(f"Game: `{record['game_id']}`")
    if record.get('command_reference'):
        header_lines.append(f"Command: `{record['command_reference']}`")
    if record.get('context'):
        header_lines.append(f"Context: {str(record['context'])[:400]}")

    detail_chunks = _relay_chunks(f"Details:\n{record['details']}", limit=1000)
    header = '\n'.join(header_lines)
    messages = [header + '\n' + detail_chunks[0]]
    messages.extend(detail_chunks[1:])
    allowed_mentions = discord.AllowedMentions.none()
    for index, content in enumerate(messages):
        files = None
        if index == len(messages) - 1 and report.attachments:
            files = [
                discord.File(BytesIO(attachment.data), filename=attachment.storage_name)
                for attachment in report.attachments
            ]
        send_kwargs = {'allowed_mentions': allowed_mentions}
        if files:
            send_kwargs['files'] = files
        await channel.send(content, **send_kwargs)


async def submit_native_report(bot: Any, draft: FeedbackReportDraft) -> NativeSubmission:
    """Commit first, then attempt a best-effort staff-channel mirror."""

    report = await store_report(draft)
    channel = staff_help_channel(bot, draft.guild_id)
    if channel is None:
        logger.warning(
            'Beta feedback report recorded without a staff channel (report_id=%s guild=%s).',
            report.report_id,
            draft.guild_id,
        )
        return NativeSubmission(report=report, relay_ok=False)
    try:
        await relay_native(channel, report)
    except Exception as exc:
        logger.warning(
            'Beta feedback report recorded but staff relay failed '
            '(report_id=%s guild=%s channel=%s error=%s).',
            report.report_id,
            draft.guild_id,
            getattr(channel, 'id', 'unknown'),
            type(exc).__name__,
        )
        return NativeSubmission(report=report, relay_ok=False)
    return NativeSubmission(report=report, relay_ok=True)


def _valid_record(record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    required = {
        'schema_version', 'report_id', 'category', 'summary', 'details',
        'context', 'game_id', 'command_reference', 'requester_id',
        'requester_display_name', 'guild_id', 'channel_id', 'source',
        'timestamp_utc', 'git_checkpoint', 'attachments',
    }
    if not required.issubset(record):
        return False
    return (
        record['schema_version'] == SCHEMA_VERSION
        and isinstance(record['report_id'], str)
        and _report_id_is_safe(record['report_id'])
        and record['category'] in {'help', 'bug', 'feature'}
        and record['source'] == 'slash'
        and isinstance(record['summary'], str)
        and isinstance(record['details'], str)
        and isinstance(record['attachments'], list)
    )


def read_feedback_records(
        profile: RuntimeProfile | None = None,
        *,
        report_id: str | None = None,
        query: str | None = None,
        limit: int | None = None) -> FeedbackReadResult:
    """Read valid JSONL records without opening attachment payloads."""

    paths = feedback_paths(profile, create=False)
    info = _lstat(paths.record_file)
    if info is None:
        return FeedbackReadResult(records=(), issues=(), present=False)
    if not stat.S_ISREG(info.st_mode):
        raise FeedbackStorageError('The beta feedback JSONL path is not a regular file.')

    records: list[Mapping[str, Any]] = []
    issues: list[FeedbackReadIssue] = []
    normalized_query = query.casefold() if query else None
    try:
        with paths.record_file.open('rb') as stream:
            for line_number, raw_line in enumerate(stream, start=1):
                is_last_without_newline = not raw_line.endswith(b'\n')
                encoded_line = raw_line[:-1] if raw_line.endswith(b'\n') else raw_line
                try:
                    line = encoded_line.decode('utf-8')
                    parsed = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    issues.append(FeedbackReadIssue(
                        line_number=line_number,
                        kind='truncated' if is_last_without_newline else 'malformed',
                        message='line could not be decoded as one complete JSON object',
                    ))
                    continue
                if is_last_without_newline:
                    issues.append(FeedbackReadIssue(
                        line_number=line_number,
                        kind='truncated',
                        message='final JSONL line has no terminating newline',
                    ))
                    continue
                if not _valid_record(parsed):
                    issues.append(FeedbackReadIssue(
                        line_number=line_number,
                        kind='malformed',
                        message='record does not match the supported schema',
                    ))
                    continue
                if report_id is not None and parsed['report_id'] != report_id:
                    continue
                if normalized_query is not None:
                    searchable = '\n'.join(
                        str(parsed.get(field) or '')
                        for field in (
                            'report_id', 'category', 'summary', 'details',
                            'context', 'command_reference',
                        )
                    ).casefold()
                    if normalized_query not in searchable:
                        continue
                records.append(MappingProxyType(parsed))
    except OSError as exc:
        raise FeedbackStorageError('Could not read the beta feedback JSONL stream.') from exc
    if limit is not None:
        bounded_limit = max(0, int(limit))
        records = records[-bounded_limit:] if bounded_limit else []
    return FeedbackReadResult(
        records=tuple(records),
        issues=tuple(issues),
        present=True,
    )
