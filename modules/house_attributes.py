"""Shared native adapters for focused House name and image attributes."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from io import BytesIO
import functools
import logging

import discord

import settings
from modules import house_attributes_workers as workers
from modules import house_show, image_storage, team_emoji


logger = logging.getLogger('polybot.' + __name__)

_house_filesystem_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix='polybot-house-image-fs',
)

capture_actor = team_emoji.capture_actor
public_interaction_sender = team_emoji.public_interaction_sender


@dataclass(frozen=True)
class _BlockingOutcome:
    value: object
    cancelled: bool


class HouseImageDownloadError(RuntimeError):
    """A Discord attachment could not be downloaded safely."""


class HouseImagePublicationError(RuntimeError):
    """The database commit succeeded but filesystem publication did not."""

    def __init__(self, result, *, detail: str, staged_path: str | None = None):
        self.result = result
        self.detail = str(detail)
        self.staged_path = staged_path
        super().__init__(self.detail)


def _requester_is_mod(member) -> bool:
    try:
        return bool(settings.is_mod(member))
    except Exception:
        return False


def native_access_error(
    member,
    guild_id: int,
    channel_id: int | None,
    *,
    mutation: bool,
) -> str | None:
    error = house_show.native_access_error(member, guild_id, channel_id)
    if error:
        return error
    if mutation and not _requester_is_mod(member):
        return 'You do not have permission to manage House attributes.'
    return None


def build_read_request(
    *,
    member,
    guild_id: int,
    channel_id: int | None,
    house_lookup: str | None,
    attribute: str,
) -> workers.HouseAttributeReadRequest:
    return workers.HouseAttributeReadRequest(
        guild_id=int(guild_id),
        requester_id=int(member.id),
        requester_is_mod=_requester_is_mod(member),
        league_scope=house_show._league_scope(guild_id),
        channel_allowed=house_show._channel_allowed(member, guild_id, channel_id),
        house_lookup=(str(house_lookup) if house_lookup is not None else None),
        requester_role_names=tuple(
            str(role.name) for role in tuple(getattr(member, 'roles', ()) or ())
        ),
        attribute=str(attribute),
        requester_description=capture_actor(member).identity,
    )


def build_mutation_request(
    *,
    member,
    current: workers.HouseAttributeReadResult,
    attribute: str,
    value: str | None = None,
    image_operation: str | None = None,
    staged_path: str | None = None,
) -> workers.HouseAttributeMutationRequest:
    return workers.HouseAttributeMutationRequest(
        guild_id=int(current.guild_id),
        requester_id=int(member.id),
        requester_is_mod=_requester_is_mod(member),
        league_scope=house_show._league_scope(current.guild_id),
        channel_allowed=True,
        house_id=int(current.house_id),
        attribute=str(attribute),
        value=(str(value) if value is not None else None),
        image_operation=(
            str(image_operation) if image_operation is not None else None
        ),
        staged_path=(str(staged_path) if staged_path is not None else None),
        expected_name=str(current.house_name),
        expected_image_url=current.image_url,
        expected_local_digest=current.local_image_digest,
        requester_description=capture_actor(member).identity,
    )


async def run_read(request):
    return await workers.run_house_attribute_read(request)


async def _run_blocking(function, *args, report_cancellation: bool = False):
    future = _house_filesystem_executor.submit(functools.partial(function, *args))
    cancelled = False
    try:
        while not future.done():
            await asyncio.sleep(0.001)
    except asyncio.CancelledError:
        cancelled = True
        task = asyncio.current_task()
        while not future.done():
            if task is not None:
                while task.cancelling():
                    task.uncancel()
            try:
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                continue
    try:
        result = future.result()
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
    house_id: int,
) -> image_storage.StagedImage:
    if attachment.size and attachment.size > image_storage.MAX_UPLOAD_BYTES:
        raise image_storage.ImageStorageError(
            'The attached image is larger than 5 MiB.'
        )
    try:
        data = await attachment.read()
    except Exception as exc:
        raise HouseImageDownloadError(
            'The House image attachment could not be downloaded.'
        ) from exc
    if not isinstance(data, bytes):
        data = bytes(data)
    outcome = await _run_blocking(
        image_storage.stage_normalised_image,
        data,
        'house',
        int(house_id),
        report_cancellation=True,
    )
    if isinstance(outcome, _BlockingOutcome):
        await cleanup_staged(outcome.value)
        raise asyncio.CancelledError
    return outcome


async def cleanup_staged(staged: image_storage.StagedImage | None) -> None:
    if staged is None:
        return
    try:
        await _run_blocking(image_storage.cleanup_staged_image, staged.path)
    except Exception:
        logger.exception('Could not clean staged House image %s', staged.path)


async def _publish_filesystem(result, staged) -> None:
    if result.image_operation == workers.HOUSE_IMAGE_LOCAL:
        if staged is None:
            raise image_storage.ImageStorageError(
                'The staged House image is missing.'
            )
        await _run_blocking(
            image_storage.publish_staged_image,
            staged.path,
            'house',
            result.house_id,
        )
    elif result.image_operation == workers.HOUSE_IMAGE_CLEAR:
        await _run_blocking(
            image_storage.remove_local_image,
            'house',
            result.house_id,
        )


async def _run_mutation_inner(request, staged):
    try:
        result = await workers.run_house_attribute_mutation(request)
    except BaseException:
        await cleanup_staged(staged)
        raise
    try:
        await _publish_filesystem(result, staged)
    except BaseException as exc:
        raise HouseImagePublicationError(
            result,
            detail=str(exc),
            staged_path=(staged.path if staged is not None else None),
        ) from exc
    if staged is not None:
        result = replace(result, local_image_bytes=staged.data)
    return result


async def run_mutation(request, *, staged=None):
    lock = image_storage.update_lock('house', request.house_id)
    async with lock:
        operation = asyncio.create_task(_run_mutation_inner(request, staged))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            while not operation.done():
                if task is not None:
                    while task.cancelling():
                        task.uncancel()
                try:
                    await asyncio.shield(operation)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            try:
                operation.result()
            except BaseException:
                logger.exception(
                    'Cancelled House attribute operation %s finished with an error',
                    request.house_id,
                )
            raise asyncio.CancelledError


def _display(value) -> str:
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value or 'None'))
    )


async def _send_local_image(send, content: str, data: bytes, house_id: int):
    image_file = discord.File(
        BytesIO(data),
        filename=f'house-logo-{int(house_id)}.png',
    )
    try:
        await send(content, file=image_file)
    finally:
        image_file.close()


async def publish_read(result, *, send, actor) -> None:
    if result.attribute == workers.HOUSE_ATTRIBUTE_NAME:
        await send(
            f'Current name for House **{_display(result.house_name)}**.\n'
            f'Requested by {actor.label}.'
        )
        return
    if result.effective_image_source == 'local':
        await _send_local_image(
            send,
            f'Current image for House **{_display(result.house_name)}** is '
            f'locally stored.\nRequested by {actor.label}.',
            result.local_image_bytes,
            result.house_id,
        )
    elif result.effective_image_source == 'url':
        await send(
            f'Current image for House **{_display(result.house_name)}**: '
            f'<{result.image_url}>\nRequested by {actor.label}.'
        )
    else:
        await send(
            f'House **{_display(result.house_name)}** does not have an image '
            f'set.\nRequested by {actor.label}.'
        )


async def publish_mutation(result, *, send, actor) -> None:
    if result.attribute == workers.HOUSE_ATTRIBUTE_NAME:
        message = (
            f'{actor.label} renamed House **{_display(result.old_name)}** to '
            f'**{_display(result.house_name)}**. Rename its exact Discord role '
            'separately if the role should continue to identify membership.'
        )
        await send(message)
        return
    if result.image_operation == workers.HOUSE_IMAGE_LOCAL:
        await _send_local_image(
            send,
            f'{actor.label} updated the image for House '
            f'**{_display(result.house_name)}**.',
            result.local_image_bytes,
            result.house_id,
        )
    else:
        await send(
            f'{actor.label} cleared the image for House '
            f'**{_display(result.house_name)}**.'
        )


def publication_failure_message(error: HouseImagePublicationError, *, actor) -> str:
    action = (
        'replacement could not be published; the previous local image may '
        'remain visible and the staged replacement was retained'
        if error.result.image_operation == workers.HOUSE_IMAGE_LOCAL
        else 'previous local image could not be removed and may remain visible'
    )
    return (
        f':warning: {actor.label} committed the House image change, but the '
        f'{action}. House **{_display(error.result.house_name)}** requires '
        'operator reconciliation.'
    )
