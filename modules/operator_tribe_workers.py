"""Bounded worker boundary for global Tribe emoji operator access."""

from __future__ import annotations

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import settings
from modules import models, team_emoji_workers


MAX_TRIBES = 100
MAX_AUTOCOMPLETE = 25


class OperatorTribeError(RuntimeError):
    """Base class for safe operator Tribe errors."""


class OperatorTribePermissionError(OperatorTribeError):
    """The requester is not the configured bot owner."""


class OperatorTribeLookupError(OperatorTribeError):
    """A Tribe lookup is missing or ambiguous."""


class OperatorTribeValidationError(OperatorTribeError):
    """An emoji or request value is invalid."""


@dataclass(frozen=True)
class OperatorTribeReadRequest:
    guild_id: int
    requester_id: int
    tribe_lookup: str


@dataclass(frozen=True)
class OperatorTribeMutationRequest:
    guild_id: int
    requester_id: int
    requester_description: str
    tribe_lookup: str
    emoji: str


@dataclass(frozen=True)
class OperatorTribeResult:
    guild_id: int
    tribe_id: int
    tribe_name: str
    old_emoji: str
    emoji: str
    changed: bool


@dataclass(frozen=True)
class OperatorTribeAutocompleteRequest:
    requester_id: int
    current: str
    limit: int = MAX_AUTOCOMPLETE


@dataclass(frozen=True)
class OperatorTribeAutocompleteResult:
    tribe_id: int
    tribe_name: str


def _validate_owner(requester_id: int) -> None:
    if int(requester_id) != int(settings.owner_id):
        raise OperatorTribePermissionError(
            'Only the configured bot owner can manage Tribe emojis.'
        )


def _tribe_rows() -> tuple:
    query = models.Tribe.select().order_by(models.Tribe.name, models.Tribe.id)
    return tuple(query.limit(MAX_TRIBES))


def _resolve_tribe(tribe_lookup: str):
    lookup = str(tribe_lookup or '').strip()
    if not lookup:
        raise OperatorTribeLookupError('Choose a Tribe.')

    rows = _tribe_rows()
    folded = lookup.casefold()
    exact = tuple(
        tribe for tribe in rows
        if str(tribe.name).casefold() == folded
    )
    if len(exact) == 1:
        return exact[0]

    matches = tuple(
        tribe for tribe in rows
        if str(tribe.name).casefold().startswith(folded)
    )
    if not matches:
        raise OperatorTribeLookupError(
            f'No Tribe matched "{lookup}".'
        )
    if len(matches) > 1:
        names = ', '.join(str(tribe.name) for tribe in matches[:10])
        raise OperatorTribeLookupError(
            f'Tribe "{lookup}" is ambiguous: {names}.'
        )
    return matches[0]


def _result(request, tribe, *, old_emoji: str, emoji: str) -> OperatorTribeResult:
    return OperatorTribeResult(
        guild_id=int(request.guild_id),
        tribe_id=int(tribe.id),
        tribe_name=str(tribe.name),
        old_emoji=old_emoji,
        emoji=emoji,
        changed=(old_emoji != emoji),
    )


def read_tribe_emoji(request: OperatorTribeReadRequest) -> OperatorTribeResult:
    """Read one global Tribe on a worker-local connection."""

    with models.db.connection_context():
        _validate_owner(request.requester_id)
        tribe = _resolve_tribe(request.tribe_lookup)
        emoji = str(tribe.emoji or '')
        return _result(request, tribe, old_emoji=emoji, emoji=emoji)


def set_tribe_emoji(
    request: OperatorTribeMutationRequest,
) -> OperatorTribeResult:
    """Atomically update one Tribe emoji and its actor-attributed audit."""

    with models.db.connection_context():
        with models.db.atomic():
            _validate_owner(request.requester_id)
            try:
                emoji = team_emoji_workers.validate_emoji(request.emoji)
            except team_emoji_workers.TeamEmojiValidationError as exc:
                raise OperatorTribeValidationError(str(exc)) from exc
            tribe = _resolve_tribe(request.tribe_lookup)
            old_emoji = str(tribe.emoji or '')
            if old_emoji == emoji:
                return _result(
                    request,
                    tribe,
                    old_emoji=old_emoji,
                    emoji=emoji,
                )

            tribe.emoji = emoji
            tribe.save()
            models.GameLog.write(
                game_id=0,
                guild_id=int(request.guild_id),
                message=(
                    f'{request.requester_description} set the global Tribe '
                    f'emoji for **{tribe.name}** to {emoji!r}; previous '
                    f'value was {old_emoji!r} (/operator tribe emoji)'
                ),
            )
            return _result(
                request,
                tribe,
                old_emoji=old_emoji,
                emoji=emoji,
            )


def list_tribes(
    request: OperatorTribeAutocompleteRequest,
) -> tuple[OperatorTribeAutocompleteResult, ...]:
    """Return a bounded owner-only autocomplete catalog."""

    with models.db.connection_context():
        _validate_owner(request.requester_id)
        current = str(request.current or '').strip().casefold()
        limit = min(max(int(request.limit), 1), MAX_AUTOCOMPLETE)
        matches = (
            tribe for tribe in _tribe_rows()
            if not current or current in str(tribe.name).casefold()
        )
        return tuple(
            OperatorTribeAutocompleteResult(
                tribe_id=int(tribe.id),
                tribe_name=str(tribe.name),
            )
            for tribe in tuple(matches)[:limit]
        )


_operator_tribe_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='polybot-operator-tribe',
)


async def _run_worker(function, request, *, drain_on_cancel: bool):
    loop = asyncio.get_running_loop()
    concurrent_future = _operator_tribe_executor.submit(
        functools.partial(function, request)
    )
    future = asyncio.wrap_future(concurrent_future, loop=loop)
    if not drain_on_cancel:
        return await future
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        task = asyncio.current_task()
        while not future.done():
            if task is not None:
                while task.cancelling():
                    task.uncancel()
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                continue
        future.result()
        raise asyncio.CancelledError


async def run_read(request: OperatorTribeReadRequest) -> OperatorTribeResult:
    return await _run_worker(read_tribe_emoji, request, drain_on_cancel=False)


async def run_mutation(
    request: OperatorTribeMutationRequest,
) -> OperatorTribeResult:
    return await _run_worker(set_tribe_emoji, request, drain_on_cancel=True)


async def run_autocomplete(
    request: OperatorTribeAutocompleteRequest,
) -> tuple[OperatorTribeAutocompleteResult, ...]:
    return await _run_worker(list_tribes, request, drain_on_cancel=False)
