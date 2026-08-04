"""Bounded worker-local reads and writes for player timezone preferences."""

from __future__ import annotations

import asyncio
import functools
from dataclasses import dataclass
import logging

import peewee

from modules import models, player_registration_workers, player_timezone_values


logger = logging.getLogger('polybot.' + __name__)

# P6.1 owns the bounded ordinary player-write executor.  Sharing it keeps
# registration and timezone writes serialized while avoiding a new default or
# unbounded executor and makes the worker boundary explicit in one place.
_player_write_executor = player_registration_workers._player_write_executor


class PlayerTimezoneValidationError(ValueError):
    """The timezone request is not internally valid."""


class PlayerTimezonePermissionError(PermissionError):
    """The actor is not allowed to read or write the requested target."""


class PlayerTimezoneNotFound(PlayerTimezoneValidationError):
    """The target is not represented by a Player in the current guild."""


@dataclass(frozen=True)
class PlayerTimezoneRequest:
    """Primitive-only input crossing into a synchronous worker."""

    guild_id: int
    requester_id: int
    actor: player_registration_workers.MemberSnapshot
    target: player_registration_workers.MemberSnapshot
    offset_minutes: int | None = None
    clear: bool = False
    requester_is_staff: bool = False
    native: bool = True
    invoked_with: str = '/player timezone'
    prefix: str = '$'

    @property
    def mutated(self) -> bool:
        return self.clear or self.offset_minutes is not None


@dataclass(frozen=True)
class PlayerTimezoneResult:
    """Immutable read or committed-write result."""

    guild_id: int
    requester_id: int
    target_id: int
    target_name: str
    actor_description: str
    target_description: str
    old_offset_minutes: int | None
    offset_minutes: int | None
    legacy_offset_hours: int | None
    cleared: bool
    mutated: bool

    @property
    def offset_display(self) -> str | None:
        if self.offset_minutes is None:
            return None
        return player_timezone_values.format_timezone_offset(
            self.offset_minutes,
        )


def _is_staff_snapshot(request: PlayerTimezoneRequest) -> bool:
    return player_registration_workers.is_staff_snapshot(
        request.guild_id,
        request.requester_id,
        request.actor.role_names,
    )


def _ensure_request_is_allowed(request: PlayerTimezoneRequest) -> None:
    if request.guild_id <= 0:
        raise PlayerTimezoneValidationError(
            'Timezone preferences require a valid Discord server.'
        )
    if request.requester_id != request.actor.discord_id:
        raise PlayerTimezonePermissionError(
            'The timezone actor snapshot is inconsistent.'
        )
    if request.target.discord_id != request.requester_id and not _is_staff_snapshot(
        request
    ):
        raise PlayerTimezonePermissionError(
            'Only server staff can view or change another member\'s timezone.'
        )
    if request.clear and request.offset_minutes is not None:
        raise PlayerTimezoneValidationError(
            'Choose an offset or clear the preference, not both.'
        )
    if request.offset_minutes is not None:
        try:
            player_timezone_values.format_timezone_offset(
                request.offset_minutes,
            )
        except player_timezone_values.TimezoneOffsetError as exc:
            raise PlayerTimezoneValidationError(str(exc)) from exc


def _load_target(request: PlayerTimezoneRequest):
    """Reload the account and its guild Player row from primitive IDs."""

    member = models.DiscordMember.get_or_none(
        discord_id=request.target.discord_id,
    )
    if member is None:
        raise PlayerTimezoneNotFound(
            'That member is not registered with the bot.'
        )
    player = (
        models.Player.select()
        .join(models.DiscordMember)
        .where(
            (models.Player.guild_id == request.guild_id)
            & (models.DiscordMember.discord_id == request.target.discord_id)
        )
        .get_or_none()
    )
    if player is None:
        raise PlayerTimezoneNotFound(
            'That member is not registered in this Discord server.'
        )
    return member, player


def _result(
    request: PlayerTimezoneRequest,
    *,
    member,
    player,
    old_offset_minutes: int | None,
    offset_minutes: int | None,
    cleared: bool,
    mutated: bool,
) -> PlayerTimezoneResult:
    return PlayerTimezoneResult(
        guild_id=request.guild_id,
        requester_id=request.requester_id,
        target_id=request.target.discord_id,
        target_name=str(
            getattr(player, 'name', None)
            or getattr(member, 'name', None)
            or request.target.display_name
        ),
        actor_description=request.actor.description,
        target_description=request.target.description,
        old_offset_minutes=old_offset_minutes,
        offset_minutes=offset_minutes,
        legacy_offset_hours=(
            int(getattr(member, 'timezone_offset', None))
            if getattr(member, 'timezone_offset', None) is not None
            else None
        ),
        cleared=cleared,
        mutated=mutated,
    )


def read_timezone(request: PlayerTimezoneRequest) -> PlayerTimezoneResult:
    """Read the effective preference using a worker-owned connection."""

    _ensure_request_is_allowed(request)
    if request.mutated:
        raise PlayerTimezoneValidationError('This request is a mutation.')
    with models.db.connection_context():
        member, player = _load_target(request)
        offset_minutes = player_timezone_values.effective_timezone_offset_minutes(
            member,
        )
        return _result(
            request,
            member=member,
            player=player,
            old_offset_minutes=offset_minutes,
            offset_minutes=offset_minutes,
            cleared=False,
            mutated=False,
        )


def write_timezone(request: PlayerTimezoneRequest) -> PlayerTimezoneResult:
    """Atomically write only the new representation and its audit entry."""

    _ensure_request_is_allowed(request)
    if not request.mutated:
        raise PlayerTimezoneValidationError('This request is a read.')

    with models.db.connection_context():
        with models.db.atomic():
            member, player = _load_target(request)
            if (
                request.target.discord_id != request.requester_id
                and not _is_staff_snapshot(request)
            ):
                raise PlayerTimezonePermissionError(
                    'Only server staff can view or change another member\'s timezone.'
                )
            old_offset_minutes = (
                player_timezone_values.effective_timezone_offset_minutes(member)
            )
            new_offset_minutes = (
                None if request.clear else int(request.offset_minutes)
            )
            # The tombstone is new-schema state, not legacy-field mutation.
            # It is required to make an explicit clear survive the temporary
            # minutes-null -> legacy-hours fallback period.
            member.timezone_offset_minutes = new_offset_minutes
            member.timezone_offset_cleared = bool(request.clear)
            member.save(only=[
                models.DiscordMember.timezone_offset_minutes,
                models.DiscordMember.timezone_offset_cleared,
            ])

            new_display = (
                player_timezone_values.format_timezone_offset(new_offset_minutes)
                if new_offset_minutes is not None
                else 'not set'
            )
            audit_message = (
                f'{request.actor.description} changed the account-wide fixed '
                f'UTC offset for {request.target.description} to {new_display}.'
            )
            models.GameLog.write(
                message=audit_message,
                guild_id=request.guild_id,
                game_id=0,
            )
            return _result(
                request,
                member=member,
                player=player,
                old_offset_minutes=old_offset_minutes,
                offset_minutes=new_offset_minutes,
                cleared=bool(request.clear),
                mutated=True,
            )


async def _run_sync(function, request: PlayerTimezoneRequest):
    """Run one bounded player job and drain it before propagating cancel."""

    concurrent_future = _player_write_executor.submit(
        functools.partial(function, request),
    )
    try:
        while not concurrent_future.done():
            await asyncio.sleep(0.01)
        return concurrent_future.result()
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None:
            task.uncancel()
        while not concurrent_future.done():
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                if task is not None:
                    task.uncancel()
        concurrent_future.result()
        raise asyncio.CancelledError


async def run_timezone_request(
    request: PlayerTimezoneRequest,
) -> PlayerTimezoneResult:
    """Dispatch a read or write through the shared bounded player executor."""

    return await _run_sync(
        write_timezone if request.mutated else read_timezone,
        request,
    )
