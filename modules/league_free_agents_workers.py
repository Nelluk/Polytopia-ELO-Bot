"""Worker-local draft-announcement state for league Free Agent signups."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import functools
import threading

from modules import models


MAX_ADDED_MESSAGE_LENGTH = 1_200


class FreeAgentPostError(RuntimeError):
    """Base user-facing Free Agent announcement failure."""


class FreeAgentPostBusyError(FreeAgentPostError):
    """Another post operation currently owns the process-local coordinator."""


class FreeAgentPostConflictError(FreeAgentPostError):
    """The persisted signup state changed after preflight."""


@dataclass(frozen=True)
class DraftState:
    guild_id: int
    announcement_message_id: int | None
    announcement_channel_id: int | None
    draft_open: bool
    added_message: str


@dataclass(frozen=True)
class DraftPersistRequest:
    guild_id: int
    requester_id: int
    requester_name: str
    expected_message_id: int | None
    expected_channel_id: int | None
    announcement_message_id: int
    announcement_channel_id: int
    added_message: str
    opened_at: str


@dataclass(frozen=True)
class DraftPersistResult:
    guild_id: int
    requester_id: int
    announcement_message_id: int
    announcement_channel_id: int
    added_message: str


class FreeAgentPostCoordinator:
    """Nonblocking single-flight guard spanning Discord and database effects."""

    def __init__(self):
        self._lock = threading.Lock()
        self._active = False

    def claim(self) -> None:
        with self._lock:
            if self._active:
                raise FreeAgentPostBusyError(
                    'Another Free Agent announcement is already being posted. '
                    'Wait for it to finish before trying again.'
                )
            self._active = True

    def release(self) -> None:
        with self._lock:
            self._active = False

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active


free_agent_post_coordinator = FreeAgentPostCoordinator()


_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='polybot-free-agent-post',
)


def _optional_id(value) -> int | None:
    return int(value) if value is not None else None


def _config_dict(record) -> dict:
    value = getattr(record, 'polychamps_draft', None)
    return dict(value) if isinstance(value, dict) else {}


def _state(guild_id: int, value: dict) -> DraftState:
    message = value.get('announcement_message')
    channel = value.get('announcement_channel')
    added_message = value.get('draft_message', value.get('added_message', ''))
    return DraftState(
        guild_id=int(guild_id),
        announcement_message_id=_optional_id(message),
        announcement_channel_id=_optional_id(channel),
        draft_open=bool(value.get('draft_open', False)),
        added_message=str(added_message or ''),
    )


def _validate_persist_request(request: DraftPersistRequest) -> None:
    if request.guild_id <= 0 or request.requester_id <= 0:
        raise FreeAgentPostError('Invalid guild or requester identity.')
    if request.announcement_message_id <= 0 or request.announcement_channel_id <= 0:
        raise FreeAgentPostError('Invalid announcement destination identity.')
    if len(request.added_message) > MAX_ADDED_MESSAGE_LENGTH:
        raise FreeAgentPostError(
            f'The additional message is limited to {MAX_ADDED_MESSAGE_LENGTH:,} '
            'characters.'
        )
    if any(ord(character) < 32 and character not in '\n\t' for character in request.added_message):
        raise FreeAgentPostError('The additional message contains unsupported control characters.')


def load_draft_state(guild_id: int) -> DraftState:
    """Read current configuration without creating a row."""

    with models.db.connection_context():
        record = models.Configuration.get_or_none(
            models.Configuration.guild_id == int(guild_id)
        )
        value = _config_dict(record) if record is not None else {}
    return _state(int(guild_id), value)


def persist_draft_state(request: DraftPersistRequest) -> DraftPersistResult:
    """Atomically persist the new pointer and actor-attributed audit record."""

    _validate_persist_request(request)
    with models.db.connection_context():
        with models.db.atomic():
            record, _ = models.Configuration.get_or_create(
                guild_id=int(request.guild_id)
            )
            # Lock the authoritative row before optimistic comparison/write.
            record = (
                models.Configuration
                .select()
                .where(models.Configuration.guild_id == int(request.guild_id))
                .for_update()
                .get()
            )
            current = _state(int(request.guild_id), _config_dict(record))
            if (
                current.announcement_message_id != request.expected_message_id
                or current.announcement_channel_id != request.expected_channel_id
            ):
                raise FreeAgentPostConflictError(
                    'The Free Agent announcement state changed while this post '
                    'was being prepared. The new Discord message was not activated.'
                )

            config = _config_dict(record)
            config.update({
                'announcement_message': int(request.announcement_message_id),
                'announcement_channel': int(request.announcement_channel_id),
                'draft_open': True,
                'date_opened': str(request.opened_at),
                # Keep both spellings because historical listeners read
                # draft_message while the model default used added_message.
                'draft_message': str(request.added_message),
                'added_message': str(request.added_message),
            })
            record.polychamps_draft = config
            record.save(only=[models.Configuration.polychamps_draft])
            models.GameLog.write(
                guild_id=int(request.guild_id),
                message=(
                    f'**{request.requester_name}** (`{int(request.requester_id)}`) '
                    'opened a Free Agent signup announcement '
                    f'(`{int(request.announcement_channel_id)}`/'
                    f'`{int(request.announcement_message_id)}`).'
                ),
            )

    return DraftPersistResult(
        guild_id=int(request.guild_id),
        requester_id=int(request.requester_id),
        announcement_message_id=int(request.announcement_message_id),
        announcement_channel_id=int(request.announcement_channel_id),
        added_message=str(request.added_message),
    )


async def _run(function, argument, *, return_after_cancellation: bool = False):
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(_executor, functools.partial(function, argument))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        task = asyncio.current_task()
        while not future.done():
            if task is not None:
                while task.cancelling():
                    task.uncancel()
            try:
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                continue
        result = future.result()
        if return_after_cancellation:
            if task is not None:
                while task.cancelling():
                    task.uncancel()
            return result
        raise


async def run_load_draft_state(guild_id: int) -> DraftState:
    return await _run(load_draft_state, int(guild_id))


async def run_persist_draft_state(
    request: DraftPersistRequest,
) -> DraftPersistResult:
    # Once persistence starts, report its known committed result even if the
    # Discord callback is cancelled. This prevents deleting an announcement
    # whose pointer/audit transaction actually committed.
    return await _run(
        persist_draft_state,
        request,
        return_after_cancellation=True,
    )
