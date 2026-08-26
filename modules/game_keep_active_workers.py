"""Bounded, atomic renewal of started incomplete-game cleanup deadlines."""

from __future__ import annotations

import datetime
from dataclasses import dataclass

import peewee

import settings
from modules import incomplete_game_purge_workers as purge, models, utilities


KEEP_ACTIVE_DAYS = 30
EXTENDED = 'extended'
SKIPPED_STATE_CHANGED = 'skipped_state_changed'


class KeepActiveError(RuntimeError):
    """The requester or game cannot use keep-active."""


class KeepActiveNotFound(KeepActiveError):
    pass


class KeepActivePermissionError(KeepActiveError):
    pass


class KeepActiveValidationError(KeepActiveError):
    pass


@dataclass(frozen=True)
class KeepActiveRequest:
    game_id: int
    actor_id: int
    actor_description: str
    invocation_guild_id: int | None
    invocation_channel_id: int | None = None
    require_warning_target: bool = False
    actor_is_staff: bool = False
    as_of: datetime.date | None = None
    warning_protected_through: datetime.date | None = None


@dataclass(frozen=True)
class KeepActiveResult:
    game_id: int
    owner_guild_id: int
    old_protected_through: datetime.date
    new_protected_through: datetime.date
    actor_id: int
    status: str = EXTENDED


def _load_locked_game(game_id: int):
    try:
        return models.Game.select().where(
            models.Game.id == int(game_id)
        ).for_update().get()
    except peewee.DoesNotExist as exc:
        raise KeepActiveNotFound(
            f'Game {int(game_id)} could not be found.'
        ) from exc


def _participant_ids(game) -> frozenset[int]:
    values = set()
    for lineup in tuple(getattr(game, 'lineup', ()) or ()):
        player = getattr(lineup, 'player', None)
        member = getattr(player, 'discord_member', None)
        discord_id = getattr(member, 'discord_id', None)
        if discord_id is None:
            discord_id = getattr(player, 'discord_id', None)
        if discord_id is not None:
            values.add(int(discord_id))
    return frozenset(values)


def _warning_target(game, guild_id: int, channel_id: int) -> bool:
    for target in purge._warning_targets(game):
        if target.guild_id == int(guild_id) and target.channel_id == int(channel_id):
            return True
    return False


def _authorize(request: KeepActiveRequest, game) -> None:
    owner_guild_id = int(game.guild_id)
    invocation_guild_id = request.invocation_guild_id
    is_participant = int(request.actor_id) in _participant_ids(game)
    if is_participant:
        pass
    elif request.actor_is_staff:
        if invocation_guild_id != owner_guild_id:
            raise KeepActivePermissionError(
                'This game cannot be kept active from this server.'
            )
    else:
        raise KeepActivePermissionError(
            'Only a participant in this game can keep it active.'
        )
    if request.require_warning_target:
        if invocation_guild_id is None or request.invocation_channel_id is None:
            raise KeepActivePermissionError(
                'This warning is no longer associated with a game channel.'
            )
        if not _warning_target(
            game, invocation_guild_id, request.invocation_channel_id,
        ):
            raise KeepActivePermissionError(
                'This warning is no longer associated with this game channel.'
            )


def keep_game_active(request: KeepActiveRequest) -> KeepActiveResult:
    as_of = request.as_of or datetime.date.today()
    with models.db.connection_context():
        with models.db.atomic():
            game = _load_locked_game(request.game_id)
            _authorize(request, game)
            count = len(tuple(getattr(game, 'lineup', ()) or ()))
            if not purge._is_started_incomplete(game):
                raise KeepActiveValidationError(
                    f'Game {game.id} is completed, pending, confirmed, or exempt.'
                )
            effective = purge.effective_protected_through(
                game, player_count=count,
            )
            if effective is None:
                raise KeepActiveValidationError(
                    f'Game {game.id} is not governed by the cleanup policy.'
                )
            if (
                request.require_warning_target
                and request.warning_protected_through != effective
            ):
                raise KeepActiveValidationError(
                    'This warning is stale; use the current cleanup warning '
                    'to keep the game active.'
                )
            if as_of < effective - datetime.timedelta(days=purge.PURGE_WARNING_DAYS):
                raise KeepActiveValidationError(
                    f'Game {game.id} cannot be kept active until its cleanup '
                    'warning window begins.'
                )
            new_deadline = max(as_of, effective) + datetime.timedelta(
                days=KEEP_ACTIVE_DAYS,
            )
            game.cleanup_deferred_until = new_deadline
            game.save(only=[models.Game.cleanup_deferred_until])
            models.GameLog.write(
                game_id=int(game.id),
                guild_id=int(game.guild_id),
                message=(
                    f'{request.actor_description} kept game {game.id} active '
                    f'from {effective.isoformat()} through '
                    f'{new_deadline.isoformat()}.'
                ),
                is_protected=True,
            )
            return KeepActiveResult(
                game_id=int(game.id),
                owner_guild_id=int(game.guild_id),
                old_protected_through=effective,
                new_protected_through=new_deadline,
                actor_id=int(request.actor_id),
            )


async def run_keep_game_active(request: KeepActiveRequest) -> KeepActiveResult:
    lock_acquired = False

    def lock_game():
        nonlocal lock_acquired
        utilities.lock_game(request.game_id)
        lock_acquired = True

    def unlock_game():
        if lock_acquired:
            utilities.unlock_game(request.game_id)

    return await settings.elo_job_coordinator.run(
        operation='keep_active_incomplete_game',
        game_id=request.game_id,
        requester_id=request.actor_id,
        requester_name=request.actor_description,
        worker=keep_game_active,
        worker_args=(request,),
        before_submit=lock_game,
        after_complete=unlock_game,
    )


async def run_keep_active(request: KeepActiveRequest) -> KeepActiveResult:
    """Compatibility alias used by the Discord service and offline callers."""

    return await run_keep_game_active(request)
