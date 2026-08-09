"""Worker-owned cleanup for deleted Discord game-channel references."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from modules import models


class ChannelReferenceError(RuntimeError):
    """A deleted-channel reference graph could not be reconciled."""


class ChannelReferenceConflictError(ChannelReferenceError):
    """A channel reference changed during the cleanup transaction."""


@dataclass(frozen=True)
class ChannelDeleteRequest:
    channel_id: int
    guild_id: int
    channel_name: str


@dataclass(frozen=True)
class ChannelDeleteResult:
    channel_id: int
    guild_id: int
    gameside_ids: tuple[int, ...]
    side_game_ids: tuple[int, ...]
    game_ids: tuple[int, ...]

    @property
    def cleared_side_count(self) -> int:
        return len(self.gameside_ids)

    @property
    def cleared_game_count(self) -> int:
        return len(self.game_ids)


_channel_reference_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='channel-reference-cleanup',
)


def _side_reference_rows(channel_id: int) -> tuple[tuple[int, int], ...]:
    return tuple(
        (int(side_id), int(game_id))
        for side_id, game_id in (
            models.GameSide
            .select(models.GameSide.id, models.GameSide.game)
            .where(models.GameSide.team_chan == int(channel_id))
            .order_by(models.GameSide.id)
            .tuples()
        )
    )


def _game_reference_ids(channel_id: int) -> tuple[int, ...]:
    return tuple(
        int(game_id)
        for (game_id,) in (
            models.Game
            .select(models.Game.id)
            .where(models.Game.game_chan == int(channel_id))
            .order_by(models.Game.id)
            .tuples()
        )
    )


def _clear_side_references(channel_id: int, side_ids: tuple[int, ...]) -> int:
    return int(
        models.GameSide
        .update(team_chan=None)
        .where(
            (models.GameSide.id.in_(side_ids))
            & (models.GameSide.team_chan == int(channel_id))
        )
        .execute()
    )


def _clear_game_references(channel_id: int, game_ids: tuple[int, ...]) -> int:
    return int(
        models.Game
        .update(game_chan=None)
        .where(
            (models.Game.id.in_(game_ids))
            & (models.Game.game_chan == int(channel_id))
        )
        .execute()
    )


def clear_deleted_channel_references(
    request: ChannelDeleteRequest,
) -> ChannelDeleteResult:
    """Clear every exact channel pointer in one worker-local transaction."""

    if request.channel_id <= 0 or request.guild_id <= 0:
        raise ChannelReferenceError('Channel and guild IDs must be valid.')
    if not str(request.channel_name).strip():
        raise ChannelReferenceError('The deleted channel name is required.')

    with models.db.connection_context():
        with models.db.atomic():
            side_rows = _side_reference_rows(int(request.channel_id))
            game_ids = _game_reference_ids(int(request.channel_id))

            if side_rows:
                side_ids = tuple(row[0] for row in side_rows)
                cleared_sides = _clear_side_references(
                    int(request.channel_id),
                    side_ids,
                )
                if int(cleared_sides) != len(side_ids):
                    raise ChannelReferenceConflictError(
                        'A game-side channel reference changed during cleanup.'
                    )

            if game_ids:
                cleared_games = _clear_game_references(
                    int(request.channel_id),
                    game_ids,
                )
                if int(cleared_games) != len(game_ids):
                    raise ChannelReferenceConflictError(
                        'A full-game channel reference changed during cleanup.'
                    )

    return ChannelDeleteResult(
        channel_id=int(request.channel_id),
        guild_id=int(request.guild_id),
        gameside_ids=tuple(row[0] for row in side_rows),
        side_game_ids=tuple(row[1] for row in side_rows),
        game_ids=game_ids,
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


async def run_channel_reference_cleanup(
    request: ChannelDeleteRequest,
) -> ChannelDeleteResult:
    future = _channel_reference_executor.submit(
        clear_deleted_channel_references,
        request,
    )
    return await _drain_future(future)
