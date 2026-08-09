"""Bounded worker-local snapshots for automatic open-game lists."""

from __future__ import annotations

import asyncio
import datetime
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from modules import models


MAX_BROADCAST_GAMES = 12


@dataclass(frozen=True)
class GameListBroadcastRequest:
    guild_id: int
    ranked_filter: int
    as_of: datetime.datetime
    limit: int = MAX_BROADCAST_GAMES


@dataclass(frozen=True)
class GameListBroadcastRow:
    game_id: int
    host_name: str
    size: str
    players: int
    capacity: int
    expiration: str
    ranked: bool
    notes: str


@dataclass(frozen=True)
class GameListBroadcastSnapshot:
    guild_id: int
    ranked_filter: int
    rows: tuple[GameListBroadcastRow, ...]
    skipped_game_ids: tuple[int, ...]


_broadcast_read_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix='polybot-open-game-broadcast-read',
)


def _expiration_label(expiration, *, as_of: datetime.datetime) -> str:
    if expiration is None:
        return 'Exp'
    hours = int((expiration - as_of).total_seconds() / 3600.0)
    return 'Exp' if hours < 0 else f'{hours}H'


def _freeze_row(game, *, as_of: datetime.datetime) -> GameListBroadcastRow:
    players, capacity = game.capacity()
    creator = game.creating_player()
    return GameListBroadcastRow(
        game_id=int(game.id),
        host_name=(str(creator.name)[:35] if creator else '<Vacant>'),
        size=str(game.size_string()),
        players=int(players),
        capacity=int(capacity),
        expiration=_expiration_label(game.expiration, as_of=as_of),
        ranked=bool(game.is_ranked),
        notes=str(game.notes or ''),
    )


def load_game_list_broadcast(
    request: GameListBroadcastRequest,
) -> GameListBroadcastSnapshot:
    """Load at most the legacy 12 displayed rows on one local connection."""

    if request.ranked_filter not in (0, 1, 2):
        raise ValueError('Unknown ranked-game broadcast filter.')
    if request.limit < 1 or request.limit > MAX_BROADCAST_GAMES:
        raise ValueError(
            f'Open-game broadcasts support 1-{MAX_BROADCAST_GAMES} rows.'
        )
    rows = []
    skipped = []
    with models.db.connection_context():
        games = models.Game.search_pending(
            status_filter=2,
            ranked_filter=request.ranked_filter,
            guild_id=request.guild_id,
            limit=request.limit,
        )
        for game in games:
            try:
                rows.append(_freeze_row(game, as_of=request.as_of))
            except Exception:
                skipped.append(int(game.id))
    return GameListBroadcastSnapshot(
        guild_id=request.guild_id,
        ranked_filter=request.ranked_filter,
        rows=tuple(rows),
        skipped_game_ids=tuple(skipped),
    )


async def run_load_game_list_broadcast(
    request: GameListBroadcastRequest,
) -> GameListBroadcastSnapshot:
    """Run the bounded read without releasing ownership during cancellation."""

    future = _broadcast_read_executor.submit(load_game_list_broadcast, request)
    cancellation = None
    while not future.done():
        try:
            await asyncio.sleep(0.001)
        except asyncio.CancelledError as exc:
            cancellation = exc
    if cancellation is not None:
        raise cancellation
    return future.result()
