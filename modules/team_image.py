"""Shared adapters and post-commit publication for ``/team image``."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from io import BytesIO
import functools
import logging

import discord

import settings
from modules import exceptions, image_storage, team_emoji, team_image_workers


logger = logging.getLogger('polybot.' + __name__)

_image_filesystem_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix='polybot-team-image-fs',
)


@dataclass(frozen=True)
class _BlockingOutcome:
    value: object
    cancelled: bool

TeamImageActor = team_emoji.TeamEmojiActor
capture_actor = team_emoji.capture_actor
public_interaction_sender = team_emoji.public_interaction_sender


class TeamImageDownloadError(RuntimeError):
    """A Discord attachment could not be downloaded safely."""


class TeamImagePublicationError(RuntimeError):
    """The DB commit succeeded but the effective filesystem source did not publish."""

    def __init__(self, result, *, detail: str, staged_path: str | None = None):
        self.result = result
        self.detail = str(detail)
        self.staged_path = staged_path
        super().__init__(self.detail)


def _team_enabled(guild_id: int) -> bool:
    try:
        return bool(settings.guild_setting(int(guild_id), 'allow_teams'))
    except (AttributeError, KeyError, TypeError, exceptions.CheckFailedError):
        return False


def _requester_is_mod(member) -> bool:
    try:
        return bool(settings.is_mod(member))
    except (AttributeError, TypeError, exceptions.CheckFailedError):
        return False


def native_access_error(member, guild_id: int) -> str | None:
    """Return the existing team-image permission denial before defer."""

    if not _team_enabled(guild_id):
        return 'Teams are not enabled on this server.'
    if not _requester_is_mod(member):
        return 'You do not have permission to manage team images.'
    return None


def build_read_request(
    *,
    member,
    guild_id: int,
    team_lookup: str | None,
    invoked_with: str = '/team image',
) -> team_image_workers.TeamImageReadRequest:
    """Capture only immutable primitive/member-safe values for a read."""

    return team_image_workers.TeamImageReadRequest(
        guild_id=int(guild_id),
        requester_id=int(member.id),
        requester_is_mod=_requester_is_mod(member),
        team_enabled=_team_enabled(guild_id),
        team_lookup=(str(team_lookup) if team_lookup is not None else None),
        requester_description=capture_actor(member).identity,
        invoked_with=str(invoked_with),
    )


def build_mutation_request(
    *,
    member,
    guild_id: int,
    team_id: int,
    operation: str,
    image_url: str | None = None,
    staged_path: str | None = None,
    expected_image_url: str | None = None,
    expected_local_digest: str | None = None,
    ignored_url: bool = False,
    native: bool = True,
    invoked_with: str = '/team image',
) -> team_image_workers.TeamImageMutationRequest:
    """Capture a worker request without passing Discord/Peewee objects."""

    return team_image_workers.TeamImageMutationRequest(
        guild_id=int(guild_id),
        requester_id=int(member.id),
        requester_is_mod=_requester_is_mod(member),
        team_enabled=_team_enabled(guild_id),
        team_id=int(team_id),
        operation=str(operation),
        image_url=(str(image_url) if image_url is not None else None),
        staged_path=(str(staged_path) if staged_path is not None else None),
        expected_image_url=(
            str(expected_image_url) if expected_image_url is not None else None
        ),
        expected_local_digest=(
            str(expected_local_digest)
            if expected_local_digest is not None
            else None
        ),
        requester_description=capture_actor(member).identity,
        ignored_url=bool(ignored_url),
        native=bool(native),
        invoked_with=str(invoked_with),
    )


async def run_read(request):
    return await team_image_workers.run_team_image_read(request)


async def _run_blocking(function, *args, report_cancellation: bool = False):
    """Run blocking filesystem work off-loop and finish it after cancellation."""

    concurrent_future = _image_filesystem_executor.submit(
        functools.partial(function, *args)
    )
    cancelled = False
    try:
        while not concurrent_future.done():
            await asyncio.sleep(0.001)
    except asyncio.CancelledError:
        cancelled = True
        current = asyncio.current_task()
        while not concurrent_future.done():
            if current is not None:
                current.uncancel()
            try:
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                continue
    try:
        result = concurrent_future.result()
    except BaseException:
        if cancelled:
            raise asyncio.CancelledError
        raise
    if cancelled:
        if report_cancellation:
            return _BlockingOutcome(result, True)
        raise asyncio.CancelledError
    return result


async def stage_attachment(
    attachment: discord.Attachment,
    *,
    team_id: int,
) -> image_storage.StagedImage:
    """Download asynchronously, then inspect/stage the image off the event loop."""

    if attachment.size and attachment.size > image_storage.MAX_UPLOAD_BYTES:
        raise image_storage.ImageStorageError(
            'The attached image is larger than 5 MiB.'
        )
    try:
        data = await attachment.read()
    except Exception as exc:
        raise TeamImageDownloadError(
            'The image attachment could not be downloaded.'
        ) from exc
    if not isinstance(data, bytes):
        data = bytes(data)
    outcome = await _run_blocking(
        image_storage.stage_normalised_image,
        data,
        'team',
        int(team_id),
        report_cancellation=True,
    )
    if isinstance(outcome, _BlockingOutcome):
        await _cleanup_staged(outcome.value)
        raise asyncio.CancelledError
    return outcome


async def _cleanup_staged(staged: image_storage.StagedImage | None) -> None:
    if staged is None:
        return
    try:
        await _run_blocking(image_storage.cleanup_staged_image, staged.path)
    except Exception:
        logger.exception('Could not clean staged team image %s', staged.path)


async def _publish_filesystem(
    result: team_image_workers.TeamImageMutationResult,
    staged: image_storage.StagedImage | None,
) -> None:
    if result.operation == team_image_workers.TEAM_IMAGE_LOCAL:
        if staged is None:
            raise image_storage.ImageStorageError(
                'The staged replacement image is missing.'
            )
        await _run_blocking(
            image_storage.publish_staged_image,
            staged.path,
            'team',
            result.team_id,
        )
    elif result.operation in {
        team_image_workers.TEAM_IMAGE_URL,
        team_image_workers.TEAM_IMAGE_CLEAR,
    }:
        await _run_blocking(
            image_storage.remove_local_image,
            'team',
            result.team_id,
        )


async def _run_mutation_inner(
    request: team_image_workers.TeamImageMutationRequest,
    staged: image_storage.StagedImage | None,
):
    try:
        result = await team_image_workers.run_team_image_mutation(request)
    except BaseException:
        await _cleanup_staged(staged)
        raise

    try:
        await _publish_filesystem(result, staged)
    except BaseException as exc:
        # A staged replacement is intentionally retained after a committed DB
        # mutation so an operator can recover it if publication fails.
        raise TeamImagePublicationError(
            result,
            detail=str(exc),
            staged_path=(staged.path if staged is not None else None),
        ) from exc

    if staged is not None:
        result = replace(result, local_image_bytes=staged.data)
    return result


async def run_mutation(
    request: team_image_workers.TeamImageMutationRequest,
    *,
    staged: image_storage.StagedImage | None = None,
):
    """Commit DB/audit, publish the staged filesystem state, then return."""

    lock = image_storage.update_lock('team', request.team_id)
    async with lock:
        operation = asyncio.create_task(_run_mutation_inner(request, staged))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            while not operation.done():
                if current is not None:
                    current.uncancel()
                try:
                    await asyncio.shield(operation)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    # The shielded operation is complete; inspect its result
                    # below while preserving the caller's cancellation.
                    break
            try:
                completed_result = operation.result()
            except asyncio.CancelledError:
                pass
            except BaseException:
                logger.exception(
                    'Cancelled team image operation for committed team %s '
                    'finished with an exception',
                    request.team_id,
                )
            else:
                logger.info(
                    'Caller cancelled after team image operation committed '
                    'and filesystem publication completed for team %s '
                    '(operation=%s)',
                    completed_result.team_id,
                    completed_result.operation,
                )
            raise asyncio.CancelledError


def _display(value) -> str:
    if value is None or value == '':
        return 'None'
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value)),
    )


def _team_display(value: str) -> str:
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value)),
    )


def read_message(
    result: team_image_workers.TeamImageReadResult,
    *,
    actor: TeamImageActor,
) -> str:
    if result.effective_source == team_image_workers.TEAM_IMAGE_LOCAL:
        message = (
            f'Current image for team **{_team_display(result.team_name)}** '
            'is locally stored.'
        )
    elif result.effective_source == team_image_workers.TEAM_IMAGE_URL:
        message = (
            f'Current image for team **{_team_display(result.team_name)}**: '
            f'<{result.image_url}>'
        )
    else:
        message = (
            f'Team **{_team_display(result.team_name)}** does not have an '
            'image set.'
        )
    return f'{message}\nRequested by {actor.label}.'


def legacy_read_message(result: team_image_workers.TeamImageReadResult) -> str:
    """Preserve the established prefix read wording."""

    if result.effective_source == team_image_workers.TEAM_IMAGE_LOCAL:
        return f'Locally stored image for team **{result.team_name}**:'
    if result.effective_source == team_image_workers.TEAM_IMAGE_URL:
        return f'Image for team **{result.team_name}**: <{result.image_url}>'
    return f'Team **{result.team_name}** does not have an image set.'


def native_mutation_message(
    result: team_image_workers.TeamImageMutationResult,
    *,
    actor: TeamImageActor,
) -> str:
    team_name = _team_display(result.team_name)
    if result.operation == team_image_workers.TEAM_IMAGE_CLEAR:
        return f'{actor.label} cleared the image for Team **{team_name}**.'
    return f'{actor.label} updated the image for Team **{team_name}** with a local image.'


def legacy_mutation_message(
    result: team_image_workers.TeamImageMutationResult,
) -> str:
    if result.operation == team_image_workers.TEAM_IMAGE_LOCAL:
        ignored = ' The supplied URL was ignored.' if result.ignored_url else ''
        return (
            f'Team **{result.team_name}** updated with a local image.'
            f'{ignored}'
        )
    if result.operation == team_image_workers.TEAM_IMAGE_URL:
        return f'Team **{result.team_name}** updated with a direct image URL.'
    return f'Team **{result.team_name}** image cleared.'


async def _send_with_local_file(send, content: str, result) -> None:
    if not result.local_image_bytes:
        raise RuntimeError('The committed local image bytes were unavailable.')
    image_file = discord.File(
        BytesIO(result.local_image_bytes),
        filename=f'team-logo-{result.team_id}.png',
    )
    try:
        await send(content, file=image_file)
    finally:
        image_file.close()


async def publish_read(result, *, send, actor: TeamImageActor) -> None:
    message = read_message(result, actor=actor)
    if result.effective_source == team_image_workers.TEAM_IMAGE_LOCAL:
        await _send_with_local_file(send, message, result)
    else:
        await send(message)


async def publish_legacy_read(result, *, send) -> None:
    """Publish the established prefix read output without changing its wording."""

    message = legacy_read_message(result)
    if result.effective_source == team_image_workers.TEAM_IMAGE_LOCAL:
        await _send_with_local_file(send, message, result)
    else:
        await send(message)


async def _send_reconciliation_warning(send, content: str) -> None:
    try:
        await send(content)
    except Exception:
        logger.exception('Committed team-image warning could not be sent')


async def publish_mutation_result(
    result: team_image_workers.TeamImageMutationResult,
    *,
    send,
    actor: TeamImageActor | None = None,
) -> None:
    message = (
        legacy_mutation_message(result)
        if actor is None
        else native_mutation_message(result, actor=actor)
    )
    try:
        if result.operation == team_image_workers.TEAM_IMAGE_LOCAL:
            await _send_with_local_file(send, message, result)
        else:
            await send(message)
            if actor is None and result.operation == team_image_workers.TEAM_IMAGE_URL:
                await send(result.image_url)
    except Exception:
        logger.exception(
            'Committed team-image mutation for team %s could not publish',
            result.team_id,
        )
        identity = actor.label if actor is not None else 'The team image'
        await _send_reconciliation_warning(
            send,
            f':warning: {identity} committed the image for Team '
            f'**{_team_display(result.team_name)}**, but the public success '
            'message could not be sent. An operator must reconcile the team '
            'presentation.',
        )


def publication_failure_message(
    error: TeamImagePublicationError,
    *,
    actor: TeamImageActor,
) -> str:
    result = error.result
    if result.operation == team_image_workers.TEAM_IMAGE_LOCAL:
        detail = (
            'The database change committed, but the replacement could not be '
            'published; the previous local image remains visible and the '
            'staged replacement was retained for reconciliation.'
        )
    elif result.operation == team_image_workers.TEAM_IMAGE_URL:
        detail = (
            'The database URL committed, but the previous local image could '
            'not be removed and may still be visible until reconciliation.'
        )
    else:
        detail = (
            'The clear committed, but the previous local image could not be '
            'removed and may still be visible until reconciliation.'
        )
    return (
        f'{actor.label}: {detail} No public success was published. '
        f'Team **{_team_display(result.team_name)}** requires reconciliation.'
    )
