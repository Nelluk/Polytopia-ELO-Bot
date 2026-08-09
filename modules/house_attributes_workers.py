"""Bounded worker-local reads and mutations for House name and image."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import functools
import hashlib
from pathlib import Path

import peewee

from modules import image_storage, models


HOUSE_ATTRIBUTE_NAME = 'name'
HOUSE_ATTRIBUTE_IMAGE = 'image'
HOUSE_IMAGE_LOCAL = 'local'
HOUSE_IMAGE_CLEAR = 'clear'


class HouseAttributeError(RuntimeError):
    """Base user-facing House attribute failure."""


class HouseAttributeValidationError(HouseAttributeError):
    """The request contains invalid or contradictory input."""


class HouseAttributeLookupError(HouseAttributeValidationError):
    """The requested House cannot be resolved unambiguously."""


class HouseAttributePermissionError(HouseAttributeValidationError):
    """The requester or guild policy does not permit this operation."""


class HouseAttributeConflictError(HouseAttributeValidationError):
    """The House changed after the immutable preflight snapshot."""


@dataclass(frozen=True)
class HouseAttributeReadRequest:
    guild_id: int
    requester_id: int
    requester_is_mod: bool
    league_scope: bool
    channel_allowed: bool
    house_lookup: str | None
    requester_role_names: tuple[str, ...]
    attribute: str
    requester_description: str


@dataclass(frozen=True)
class HouseAttributeReadResult:
    guild_id: int
    house_id: int
    house_name: str
    attribute: str
    image_url: str | None
    effective_image_source: str
    local_image_bytes: bytes | None
    local_image_digest: str | None


@dataclass(frozen=True)
class HouseAttributeMutationRequest:
    guild_id: int
    requester_id: int
    requester_is_mod: bool
    league_scope: bool
    channel_allowed: bool
    house_id: int
    attribute: str
    value: str | None
    image_operation: str | None
    staged_path: str | None
    expected_name: str
    expected_image_url: str | None
    expected_local_digest: str | None
    requester_description: str


@dataclass(frozen=True)
class HouseAttributeMutationResult:
    guild_id: int
    house_id: int
    attribute: str
    old_name: str
    house_name: str
    image_operation: str | None
    old_image_url: str | None
    image_url: str | None
    local_image_bytes: bytes | None = None


def _validate_scope(request) -> None:
    if not bool(request.league_scope):
        raise HouseAttributePermissionError(
            'House commands are available only in the configured league server.'
        )
    if not bool(request.channel_allowed):
        raise HouseAttributePermissionError(
            'This command can only be used in a designated ELO bot channel.'
        )


def _resolve_house(request: HouseAttributeReadRequest):
    lookup = str(request.house_lookup or '').strip()
    houses = tuple(models.House.select().order_by(models.House.name))
    if lookup:
        exact = [row for row in houses if str(row.name).casefold() == lookup.casefold()]
        matches = exact or [
            row for row in houses if lookup.casefold() in str(row.name).casefold()
        ]
        if not matches:
            raise HouseAttributeLookupError(
                f'No matching House was found for "{lookup}".'
            )
        if len(matches) > 1:
            raise HouseAttributeLookupError(
                f'More than one matching House was found for "{lookup}".'
            )
        return matches[0]

    roles = set(request.requester_role_names)
    matches = [row for row in houses if str(row.name) in roles]
    if not matches:
        raise HouseAttributeLookupError(
            'Your House could not be inferred. Choose a House explicitly.'
        )
    if len(matches) > 1:
        raise HouseAttributeLookupError(
            'Your House is ambiguous. Choose a House explicitly.'
        )
    return matches[0]


def _local_state(house_id: int) -> tuple[bytes | None, str | None]:
    data = image_storage.local_image_bytes('house', int(house_id))
    digest = hashlib.sha256(data).hexdigest() if data is not None else None
    return data, digest


def read_house_attribute(
    request: HouseAttributeReadRequest,
) -> HouseAttributeReadResult:
    """Read one House attribute using a worker-owned connection."""

    _validate_scope(request)
    if request.attribute not in {HOUSE_ATTRIBUTE_NAME, HOUSE_ATTRIBUTE_IMAGE}:
        raise HouseAttributeValidationError('The House attribute is invalid.')
    with models.db.connection_context():
        house = _resolve_house(request)
        local_data, local_digest = _local_state(int(house.id))
        image_url = str(house.image_url) if house.image_url else None
        source = 'local' if local_data is not None else ('url' if image_url else 'none')
        return HouseAttributeReadResult(
            guild_id=int(request.guild_id),
            house_id=int(house.id),
            house_name=str(house.name),
            attribute=str(request.attribute),
            image_url=image_url,
            effective_image_source=source,
            local_image_bytes=local_data,
            local_image_digest=local_digest,
        )


def validate_house_name(value: str | None) -> str:
    value = str(value or '').strip()
    if not value:
        raise HouseAttributeValidationError('House names cannot be empty.')
    if len(value) > 50:
        raise HouseAttributeValidationError(
            'House names must be 50 characters or fewer.'
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise HouseAttributeValidationError(
            'House names cannot contain control characters.'
        )
    return value


def _reload_house(request: HouseAttributeMutationRequest):
    try:
        return models.House.get_by_id(int(request.house_id))
    except peewee.DoesNotExist as exc:
        raise HouseAttributeLookupError(
            'The requested House no longer exists.'
        ) from exc


def mutate_house_attribute(
    request: HouseAttributeMutationRequest,
) -> HouseAttributeMutationResult:
    """Commit one House name/image state and its audit row atomically."""

    _validate_scope(request)
    if not request.requester_is_mod:
        raise HouseAttributePermissionError(
            'You do not have permission to manage House attributes.'
        )
    if request.attribute not in {HOUSE_ATTRIBUTE_NAME, HOUSE_ATTRIBUTE_IMAGE}:
        raise HouseAttributeValidationError('The House attribute is invalid.')

    with models.db.connection_context():
        house = _reload_house(request)
        old_name = str(house.name)
        old_url = str(house.image_url) if house.image_url else None
        _, current_digest = _local_state(int(house.id))
        if old_name != request.expected_name:
            raise HouseAttributeConflictError(
                'The House name changed before this update was applied.'
            )
        if (
            old_url != request.expected_image_url
            or current_digest != request.expected_local_digest
        ):
            raise HouseAttributeConflictError(
                f'House {old_name} changed before this update was applied.'
            )

        new_name = old_name
        new_url = old_url
        image_operation = request.image_operation
        if request.attribute == HOUSE_ATTRIBUTE_NAME:
            if image_operation is not None or request.staged_path is not None:
                raise HouseAttributeValidationError(
                    'Image input is not valid for a House name update.'
                )
            new_name = validate_house_name(request.value)
            if new_name == old_name:
                raise HouseAttributeValidationError(
                    f'House **{old_name}** already has that name.'
                )
        else:
            if request.value is not None:
                raise HouseAttributeValidationError(
                    'Text input is not valid for a House image update.'
                )
            if image_operation not in {HOUSE_IMAGE_LOCAL, HOUSE_IMAGE_CLEAR}:
                raise HouseAttributeValidationError(
                    'The House image operation is invalid.'
                )
            if image_operation == HOUSE_IMAGE_LOCAL:
                if not request.staged_path or not Path(request.staged_path).is_file():
                    raise HouseAttributeValidationError(
                        'The staged House image is no longer available.'
                    )
            elif request.staged_path is not None:
                raise HouseAttributeValidationError(
                    'A staged upload is not valid when clearing an image.'
                )
            new_url = None

        try:
            with models.db.atomic():
                house.name = new_name
                house.image_url = new_url
                house.save()
                if request.attribute == HOUSE_ATTRIBUTE_NAME:
                    change = (
                        f'renamed House {old_name!r} to {new_name!r}'
                    )
                elif image_operation == HOUSE_IMAGE_LOCAL:
                    change = f'updated the local image for House {old_name}'
                else:
                    change = f'cleared the image for House {old_name}'
                models.GameLog.write(
                    guild_id=int(request.guild_id),
                    message=f'{request.requester_description} {change}',
                )
        except peewee.IntegrityError as exc:
            raise HouseAttributeValidationError(
                f'A House named **{new_name}** already exists.'
            ) from exc

        return HouseAttributeMutationResult(
            guild_id=int(request.guild_id),
            house_id=int(house.id),
            attribute=str(request.attribute),
            old_name=old_name,
            house_name=new_name,
            image_operation=image_operation,
            old_image_url=old_url,
            image_url=new_url,
        )


_house_attribute_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='polybot-house-attribute',
)


async def _run_worker(function, request, *, drain_on_cancel: bool):
    future = _house_attribute_executor.submit(functools.partial(function, request))
    try:
        while not future.done():
            await asyncio.sleep(0.001)
    except asyncio.CancelledError:
        if not drain_on_cancel:
            future.cancel()
            raise
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
            future.result()
        except BaseException:
            pass
        raise asyncio.CancelledError
    return future.result()


async def run_house_attribute_read(request):
    return await _run_worker(read_house_attribute, request, drain_on_cancel=False)


async def run_house_attribute_mutation(request):
    return await _run_worker(mutate_house_attribute, request, drain_on_cancel=True)
