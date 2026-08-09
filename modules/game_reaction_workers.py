"""Bounded read snapshots for legacy join-game message/reaction adapters."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from modules import models


MAX_EXTERNAL_SERVERS = 500


class ReactionGameLookupError(RuntimeError):
    """A reaction-game lookup could not complete safely."""


@dataclass(frozen=True)
class ReactionGameRequest:
    game_id: int


@dataclass(frozen=True)
class ReactionGameSnapshot:
    game_id: int
    exists: bool
    guild_id: int | None
    is_pending: bool
    external_server_ids: tuple[int, ...]


_reaction_read_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix='game-reaction-read',
)


def _game_row(game_id: int):
    return (
        models.Game
        .select(
            models.Game.id,
            models.Game.guild_id,
            models.Game.is_pending,
        )
        .where(models.Game.id == game_id)
        .tuples()
        .first()
    )


def _external_server_ids(guild_id: int) -> tuple[int, ...]:
    return tuple(
        int(external_id)
        for (external_id,) in (
            models.Team
            .select(models.Team.external_server)
            .where(
                (models.Team.guild_id == int(guild_id))
                & (models.Team.external_server > 0)
            )
            .distinct()
            .order_by(models.Team.external_server)
            .limit(MAX_EXTERNAL_SERVERS + 1)
            .tuples()
        )
    )


def load_reaction_game(
    request: ReactionGameRequest,
) -> ReactionGameSnapshot:
    """Load one primitive reaction-routing snapshot on a local connection."""

    try:
        game_id = int(request.game_id)
    except (TypeError, ValueError) as exc:
        raise ReactionGameLookupError('The game ID must be an integer.') from exc
    if game_id <= 0:
        raise ReactionGameLookupError('The game ID must be positive.')

    with models.db.connection_context():
        game_row = _game_row(game_id)
        if game_row is None:
            return ReactionGameSnapshot(
                game_id=game_id,
                exists=False,
                guild_id=None,
                is_pending=False,
                external_server_ids=(),
            )

        _loaded_id, guild_id, is_pending = game_row
        external_rows = _external_server_ids(int(guild_id))
        if len(external_rows) > MAX_EXTERNAL_SERVERS:
            raise ReactionGameLookupError(
                'The game has too many related external servers to route '
                'safely.'
            )

    return ReactionGameSnapshot(
        game_id=game_id,
        exists=True,
        guild_id=int(guild_id),
        is_pending=bool(is_pending),
        external_server_ids=external_rows,
    )


async def _drain_future(future: Future):
    try:
        while not future.done():
            await asyncio.sleep(0.001)
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None:
            while task.cancelling():
                task.uncancel()
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
        raise
    return future.result()


async def run_load_reaction_game(
    request: ReactionGameRequest,
) -> ReactionGameSnapshot:
    return await _drain_future(
        _reaction_read_executor.submit(load_reaction_game, request)
    )
