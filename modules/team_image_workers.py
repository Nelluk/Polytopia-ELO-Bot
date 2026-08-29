"""Bounded workers for the focused team-image read/edit workflow."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import functools
import hashlib
from pathlib import Path

import peewee

from modules import image_storage, models, team_emoji_workers, team_record_scope


TEAM_IMAGE_LOCAL = 'local'
TEAM_IMAGE_URL = 'url'
TEAM_IMAGE_CLEAR = 'clear'
TEAM_IMAGE_OPERATIONS = frozenset({
    TEAM_IMAGE_LOCAL,
    TEAM_IMAGE_URL,
    TEAM_IMAGE_CLEAR,
})


class TeamImageValidationError(RuntimeError):
    """The request contains an invalid or contradictory image value."""


class TeamImageLookupError(TeamImageValidationError):
    """The requested team cannot be resolved unambiguously."""


class TeamImagePermissionError(TeamImageValidationError):
    """The captured requester or guild policy does not permit the operation."""


class TeamImageConflictError(TeamImageValidationError):
    """The image changed after the request's immutable snapshot was captured."""


@dataclass(frozen=True)
class TeamImageReadRequest:
    """Immutable primitive input for an effective-image read."""

    guild_id: int
    requester_id: int
    requester_is_mod: bool
    team_enabled: bool
    team_lookup: str | None
    requester_description: str
    invoked_with: str = '/team image'


@dataclass(frozen=True)
class TeamImageReadResult:
    """Immutable effective-image snapshot suitable for Discord rendering."""

    guild_id: int
    team_id: int
    team_name: str
    image_url: str | None
    effective_source: str
    local_image_bytes: bytes | None
    local_digest: str | None


@dataclass(frozen=True)
class TeamImageMutationRequest:
    """Immutable input for one DB/audit image mutation."""

    guild_id: int
    requester_id: int
    requester_is_mod: bool
    team_enabled: bool
    team_id: int
    operation: str
    image_url: str | None
    staged_path: str | None
    expected_image_url: str | None
    expected_local_digest: str | None
    requester_description: str
    ignored_url: bool = False
    native: bool = True
    invoked_with: str = '/team image'


@dataclass(frozen=True)
class TeamImageMutationResult:
    """Immutable result after the synchronous DB/audit transaction commits."""

    guild_id: int
    team_id: int
    team_name: str
    operation: str
    old_image_url: str | None
    image_url: str | None
    old_local_digest: str | None
    ignored_url: bool
    native: bool
    local_image_bytes: bytes | None = None


def _validate_access(request) -> None:
    if not bool(request.team_enabled):
        raise TeamImagePermissionError('Teams are not enabled on this server.')
    if not bool(request.requester_is_mod):
        raise TeamImagePermissionError(
            'You do not have permission to manage team images.'
        )


def _resolve_team(request):
    try:
        return team_emoji_workers._resolve_team(
            request,
            include_hidden=True,
        )
    except team_emoji_workers.TeamEmojiLookupError as exc:
        raise TeamImageLookupError(str(exc)) from exc


def _read_local_state(team_id: int) -> tuple[bytes | None, str | None]:
    data = image_storage.local_image_bytes('team', int(team_id))
    digest = hashlib.sha256(data).hexdigest() if data is not None else None
    return data, digest


def _effective_source(image_url: str | None, local_data: bytes | None) -> str:
    if local_data is not None:
        return TEAM_IMAGE_LOCAL
    if image_url:
        return TEAM_IMAGE_URL
    return 'none'


def _read_result(team, *, guild_id: int) -> TeamImageReadResult:
    local_data, local_digest = _read_local_state(int(team.id))
    image_url = str(team.image_url) if team.image_url else None
    return TeamImageReadResult(
        guild_id=int(guild_id),
        team_id=int(team.id),
        team_name=str(team.name),
        image_url=image_url,
        effective_source=_effective_source(image_url, local_data),
        local_image_bytes=local_data,
        local_digest=local_digest,
    )


def read_team_image(request: TeamImageReadRequest) -> TeamImageReadResult:
    """Read DB state and the effective local file on a worker-owned connection."""

    with models.db.connection_context():
        _validate_access(request)
        team = _resolve_team(request)
        return _read_result(team, guild_id=request.guild_id)


def _reload_team(request: TeamImageMutationRequest):
    try:
        team = models.Team.get_by_id(int(request.team_id))
    except peewee.DoesNotExist as exc:
        raise TeamImageLookupError('The requested team no longer exists.') from exc
    if int(getattr(team, 'guild_id')) != (
        team_record_scope.persistent_team_guild_id(request.guild_id)
    ):
        raise TeamImageLookupError(
            'The requested team does not belong to this server.'
        )
    return team


def _validate_mutation(request: TeamImageMutationRequest) -> str:
    _validate_access(request)
    operation = str(request.operation)
    if operation not in TEAM_IMAGE_OPERATIONS:
        raise TeamImageValidationError('The team image operation is invalid.')
    if operation == TEAM_IMAGE_LOCAL and not request.staged_path:
        raise TeamImageValidationError('A staged image is required.')
    if operation == TEAM_IMAGE_URL:
        if not request.image_url:
            raise TeamImageValidationError('An image URL is required.')
        image_storage.validate_http_url(request.image_url)
    if operation == TEAM_IMAGE_CLEAR and (
        request.image_url is not None or request.staged_path is not None
    ):
        raise TeamImageValidationError(
            'Choose either an image replacement or `clear`, not both.'
        )
    if operation != TEAM_IMAGE_URL and request.image_url is not None:
        raise TeamImageValidationError('An image URL is not valid for this operation.')
    if operation != TEAM_IMAGE_LOCAL and request.staged_path is not None:
        raise TeamImageValidationError('A staged upload is not valid for this operation.')
    return operation


def _audit_message(
    request: TeamImageMutationRequest,
    *,
    team_name: str,
    old_image_url: str | None,
    new_image_url: str | None,
) -> str:
    if request.operation == TEAM_IMAGE_LOCAL:
        change = f'updated the local image for Team {team_name}'
        if request.ignored_url:
            change += '; the supplied URL was ignored'
    elif request.operation == TEAM_IMAGE_URL:
        change = (
            f'updated the image URL for Team {team_name} to '
            f'{new_image_url}'
        )
    else:
        change = f'cleared the image for Team {team_name}'
    invocation_note = (
        f' ({request.invoked_with})'
        if str(request.invoked_with).startswith('/')
        else ''
    )
    return (
        f'{request.requester_description} {change}; previous URL was '
        f'{old_image_url!r}{invocation_note}'
    )


def set_team_image(
    request: TeamImageMutationRequest,
) -> TeamImageMutationResult:
    """Commit one image URL/source state and audit row synchronously.

    Filesystem publication is deliberately not performed here. The caller
    publishes a staged file or removes the old local override only after this
    transaction returns successfully.
    """

    with models.db.connection_context():
        operation = _validate_mutation(request)
        if operation == TEAM_IMAGE_LOCAL and not Path(request.staged_path).is_file():
            raise TeamImageValidationError('The staged image is no longer available.')

        team = _reload_team(request)
        current_url = str(team.image_url) if team.image_url else None
        _, current_digest = _read_local_state(int(team.id))
        if current_url != request.expected_image_url or (
            current_digest != request.expected_local_digest
        ):
            raise TeamImageConflictError(
                f'Team {team.name} changed before this image update was applied.'
            )

        new_url = (
            image_storage.validate_http_url(request.image_url)
            if operation == TEAM_IMAGE_URL
            else None
        )
        with models.db.atomic():
            team.image_url = new_url
            team.save()
            result = TeamImageMutationResult(
                guild_id=int(request.guild_id),
                team_id=int(team.id),
                team_name=str(team.name),
                operation=operation,
                old_image_url=current_url,
                image_url=new_url,
                old_local_digest=current_digest,
                ignored_url=bool(request.ignored_url),
                native=bool(request.native),
            )
            models.GameLog.write(
                guild_id=int(request.guild_id),
                message=_audit_message(
                    request,
                    team_name=result.team_name,
                    old_image_url=current_url,
                    new_image_url=new_url,
                ),
            )
            return result


_team_image_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='polybot-team-image',
)


async def _run_bounded_worker(function, request, *, drain_on_cancel: bool):
    concurrent_future = _team_image_executor.submit(
        functools.partial(function, request)
    )
    # Poll only at a yield point.  A very fast concurrent future can finish
    # while asyncio.wrap_future is wiring its callback, leaving a failure
    # future pending on some supported Python/runtime combinations.  This
    # bounded yield keeps the loop responsive and lets us retrieve the result
    # directly once the worker has completed.
    try:
        while not concurrent_future.done():
            await asyncio.sleep(0.001)
    except asyncio.CancelledError:
        if not drain_on_cancel:
            concurrent_future.cancel()
            raise
        task = asyncio.current_task()
        while not concurrent_future.done():
            if task is not None:
                task.uncancel()
            try:
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                continue
        try:
            concurrent_future.result()
        except BaseException as exc:
            raise asyncio.CancelledError from exc
        raise asyncio.CancelledError
    return concurrent_future.result()


async def run_team_image_read(
    request: TeamImageReadRequest,
) -> TeamImageReadResult:
    return await _run_bounded_worker(
        read_team_image,
        request,
        drain_on_cancel=False,
    )


async def run_team_image_mutation(
    request: TeamImageMutationRequest,
) -> TeamImageMutationResult:
    return await _run_bounded_worker(
        set_team_image,
        request,
        drain_on_cancel=True,
    )
