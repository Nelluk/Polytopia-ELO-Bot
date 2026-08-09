"""Atomic worker boundary for configured vacant matchmaking lobbies."""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from modules import game_open_workers, models


EXISTING = 'existing'
CREATED = 'created'


@dataclass(frozen=True)
class LobbySideLock:
    """Frozen role lock for one configured lobby side."""

    role_id: int | None
    role_name: str | None


@dataclass(frozen=True)
class EnsureLobbyRequest:
    """Primitive configuration for one authoritative lobby check/create."""

    guild_id: int
    size: tuple[int, ...]
    size_display: str
    is_ranked: bool
    remake_partial: bool
    notes: str
    notes_log_display: str
    expiration_at: datetime.datetime
    role_locks: tuple[LobbySideLock, ...]


@dataclass(frozen=True)
class EnsureLobbyResult:
    """Outcome from one independently atomic configured-lobby request."""

    status: str
    game_id: int
    guild_id: int


def _validate_request(request: EnsureLobbyRequest) -> None:
    if len(request.size) < 2 or any(size < 1 for size in request.size):
        raise ValueError('Configured lobbies require at least two nonempty sides.')
    if len(request.role_locks) != len(request.size):
        raise ValueError('Configured lobby role locks must match its side count.')
    if not request.size_display.strip():
        raise ValueError('Configured lobby size display cannot be empty.')
    if request.expiration_at <= datetime.datetime.min:
        raise ValueError('Configured lobby expiration is invalid.')


def _candidate_lobbies(request: EnsureLobbyRequest):
    query = (
        models.Game
        .select()
        .where(
            (models.Game.guild_id == request.guild_id)
            & (models.Game.host.is_null(True))
            & (models.Game.is_pending == 1)
            & (models.Game.is_ranked == request.is_ranked)
            & (models.Game.notes == request.notes)
            & (models.Game.id.in_(
                models.Game.subq_open_games_with_capacity(
                    guild_id=request.guild_id,
                )
            ))
        )
        .order_by(models.Game.id)
    )
    return tuple(query.prefetch(
        models.GameSide,
        models.Lineup,
        models.Player,
    ))


def _find_existing_lobby(request: EnsureLobbyRequest):
    """Preserve legacy matching and ``remake_partial`` semantics."""

    for game in _candidate_lobbies(request):
        if game.size_string() != request.size_display:
            continue
        players, _capacity = game.capacity()
        if request.remake_partial and players > 0:
            continue
        return game
    return None


def ensure_configured_lobby(request: EnsureLobbyRequest) -> EnsureLobbyResult:
    """Recheck and, if needed, create one complete vacant lobby graph."""

    _validate_request(request)
    with models.db.connection_context():
        with models.db.atomic():
            existing = _find_existing_lobby(request)
            if existing is not None:
                return EnsureLobbyResult(
                    status=EXISTING,
                    game_id=int(existing.id),
                    guild_id=request.guild_id,
                )

            game = models.Game.create(
                host=None,
                notes=request.notes,
                guild_id=request.guild_id,
                is_pending=True,
                is_ranked=request.is_ranked,
                expiration=request.expiration_at,
                size=list(request.size),
            )
            models.GameLog.write(
                game_id=game,
                guild_id=request.guild_id,
                message=(
                    f'I created an '
                    f'{"unranked" if not request.is_ranked else ""} empty '
                    f'{request.size_display} lobby. '
                    f'{request.notes_log_display}'
                ),
            )
            for position, (side_size, role_lock) in enumerate(
                zip(request.size, request.role_locks),
                start=1,
            ):
                models.GameSide.create(
                    game=game,
                    size=side_size,
                    position=position,
                    required_role_id=role_lock.role_id,
                    sidename=role_lock.role_name,
                )
            return EnsureLobbyResult(
                status=CREATED,
                game_id=int(game.id),
                guild_id=request.guild_id,
            )


async def run_ensure_configured_lobby(
    request: EnsureLobbyRequest,
) -> EnsureLobbyResult:
    """Serialize creation with open/join/leave/start/delete pending writes."""

    return await game_open_workers.pending_game_coordinator.run_worker(
        ensure_configured_lobby,
        request,
    )
