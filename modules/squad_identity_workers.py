"""Bounded worker-local writes for native squad identity changes."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import functools
import logging
import unicodedata

import discord
import peewee

from modules import models, player_registration_workers


logger = logging.getLogger('polybot.' + __name__)


MAX_SQUAD_NAME_LENGTH = 50


class SquadNameValidationError(ValueError):
    """The requested squad-name operation is invalid or unsafe."""


class SquadNamePermissionError(SquadNameValidationError):
    """The current worker-side member/staff context cannot edit the squad."""


class SquadNameLookupError(SquadNameValidationError):
    """The requested squad cannot be resolved in the requested guild."""


class SquadNameNotFound(SquadNameLookupError):
    """The requested squad ID does not exist."""


class SquadNameWrongGuild(SquadNameLookupError):
    """The requested squad belongs to a different guild."""


class SquadNameConflictError(SquadNameValidationError):
    """The contextual edit was based on stale squad state."""


@dataclass(frozen=True)
class SquadNameMutationRequest:
    """Frozen primitive input for one atomic squad-name write."""

    guild_id: int
    squad_id: int
    requester_id: int
    requester_is_staff: bool
    requester_description: str
    requester_role_names: tuple[str, ...] = ()
    name: str | None = None
    clear: bool = False
    expected_name: str | None = None
    check_expected_name: bool = False
    # This controls only contextual button visibility.  The worker deliberately
    # does not use it as authority; it reloads squad membership and checks the
    # current staff snapshot carried by this submission.
    captured_can_edit: bool = False
    native: bool = True
    invoked_with: str = '/squad name'


@dataclass(frozen=True)
class SquadNameMutationResult:
    """Primitive post-commit result for public attribution and reconciliation."""

    guild_id: int
    squad_id: int
    requester_id: int
    requester_description: str
    old_name: str
    name: str
    cleared: bool
    truncated: bool
    native: bool


def normalize_squad_name(value: str) -> tuple[str, bool]:
    """Normalize whitespace and retain the legacy 50-character ceiling.

    Newlines and tabs are ordinary whitespace and collapse to one space. Other
    Unicode control/format characters are rejected so they cannot create an
    invisible identity or bidi rendering boundary. Markdown and mentions are
    escaped by the presentation adapter, not stored in the database.
    """

    if not isinstance(value, str):
        raise SquadNameValidationError('A squad name must be text.')
    for character in value:
        category = unicodedata.category(character)
        if category.startswith('C') and character not in '\t\n\r':
            raise SquadNameValidationError(
                'Squad names cannot contain control or invisible formatting '
                'characters.'
            )
    normalized = ' '.join(value.split())
    if not normalized:
        raise SquadNameValidationError(
            'A squad name is required unless clear is true.'
        )
    truncated = len(normalized) > MAX_SQUAD_NAME_LENGTH
    normalized = normalized[:MAX_SQUAD_NAME_LENGTH].rstrip()
    if not normalized:
        raise SquadNameValidationError(
            'A squad name is required unless clear is true.'
        )
    return normalized, truncated


def _validate_request(request: SquadNameMutationRequest) -> None:
    try:
        guild_id = int(request.guild_id)
        squad_id = int(request.squad_id)
        requester_id = int(request.requester_id)
    except (TypeError, ValueError) as exc:
        raise SquadNameValidationError(
            'guild_id, squad_id, and requester_id must be integers.'
        ) from exc
    if guild_id <= 0 or squad_id <= 0 or requester_id <= 0:
        raise SquadNameValidationError(
            'guild_id, squad_id, and requester_id must be positive integers.'
        )
    if not isinstance(request.clear, bool):
        raise SquadNameValidationError('clear must be a boolean.')
    if request.clear and request.name is not None:
        raise SquadNameValidationError(
            'Choose either a squad name or clear=true, not both.'
        )
    if not request.clear and request.name is None:
        raise SquadNameValidationError(
            'Supply name to edit the squad or clear=true to remove it.'
        )
    if request.check_expected_name and request.expected_name is None:
        raise SquadNameValidationError(
            'The expected squad name snapshot is incomplete.'
        )


def _load_squad(request: SquadNameMutationRequest):
    try:
        return models.Squad.get(id=int(request.squad_id))
    except peewee.DoesNotExist:
        raise SquadNameNotFound(
            f'Squad with ID {int(request.squad_id)} cannot be found.'
        ) from None


def _has_authority(squad, request: SquadNameMutationRequest) -> bool:
    has_player = getattr(squad, 'has_player', None)
    is_member = bool(
        callable(has_player)
        and has_player(discord_id=int(request.requester_id))
    )
    if is_member:
        return True
    if request.requester_role_names:
        try:
            return bool(
                player_registration_workers.is_staff_snapshot(
                    request.guild_id,
                    request.requester_id,
                    request.requester_role_names,
                )
            )
        except Exception:
            # Worker-side settings failure fails closed for role-based staff.
            return False
    # Focused offline callers may provide only the captured primitive boolean;
    # production requests always carry role names and take the authoritative
    # settings-backed snapshot branch above.
    return bool(request.requester_is_staff)


def _audit_message(
    request: SquadNameMutationRequest,
    result: SquadNameMutationResult,
) -> str:
    if result.cleared:
        change = f'cleared the name of squad {result.squad_id}'
    else:
        change = (
            f'set the name of squad {result.squad_id} to '
            f'{discord.utils.escape_mentions(discord.utils.escape_markdown(result.name))}'
        )
    return f'{request.requester_description} {change}.'


def set_squad_name(
    request: SquadNameMutationRequest,
) -> SquadNameMutationResult:
    """Reload, authorize, mutate, and audit one squad name synchronously."""

    _validate_request(request)
    with models.db.connection_context():
        squad = _load_squad(request)
        if int(squad.guild_id) != int(request.guild_id):
            raise SquadNameWrongGuild(
                f'Squad with ID {int(request.squad_id)} is affiliated with a '
                'different Discord server.'
            )
        if not _has_authority(squad, request):
            raise SquadNamePermissionError(
                'A squad name can only be set by server staff or a member of '
                'that squad.'
            )

        current_name = str(getattr(squad, 'name', '') or '')
        if (
            bool(request.check_expected_name)
            and current_name != str(request.expected_name or '')
        ):
            raise SquadNameConflictError(
                'This squad name changed before the edit was applied. Run '
                '`/squad show` again for a fresh card.'
            )

        if request.clear:
            new_name = ''
            truncated = False
        else:
            new_name, truncated = normalize_squad_name(request.name)

        # The name save and actor-attributed audit are one wholly synchronous
        # database boundary.  No Discord object or await is reachable here.
        with models.db.atomic():
            squad.name = new_name
            squad.save()
            result = SquadNameMutationResult(
                guild_id=int(request.guild_id),
                squad_id=int(squad.id),
                requester_id=int(request.requester_id),
                requester_description=str(request.requester_description),
                old_name=current_name,
                name=new_name,
                cleared=bool(request.clear),
                truncated=bool(truncated),
                native=bool(request.native),
            )
            models.GameLog.write(
                game_id=0,
                guild_id=int(request.guild_id),
                message=_audit_message(request, result),
            )
            return result


_squad_name_write_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='polybot-squad-name-write',
)


async def run_squad_name_mutation(
    request: SquadNameMutationRequest,
) -> SquadNameMutationResult:
    """Run the ordinary write without blocking the Discord event loop."""

    concurrent_future = _squad_name_write_executor.submit(
        functools.partial(set_squad_name, request)
    )
    try:
        while not concurrent_future.done():
            await asyncio.sleep(0.001)
    except asyncio.CancelledError:
        # A synchronous transaction cannot be interrupted safely. Drain the
        # worker before propagating cancellation to the interaction task.
        task = asyncio.current_task()
        while not concurrent_future.done():
            if task is not None:
                while task.cancelling():
                    task.uncancel()
            try:
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                continue
        concurrent_future.result()
        raise asyncio.CancelledError
    return concurrent_future.result()
