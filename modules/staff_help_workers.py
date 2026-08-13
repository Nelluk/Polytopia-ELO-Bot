"""Bounded game-context discovery for structured staff-help reports."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import functools
import peewee

from modules import exceptions, models


_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='polybot-staff-help',
)


@dataclass(frozen=True, slots=True)
class RelatedGame:
    """Privacy-bounded game identity used only for routing and context."""

    game_id: int
    guild_id: int
    name: str
    status: str


def _status(game) -> str:
    if bool(game.is_pending):
        return 'Open'
    if not bool(game.is_completed):
        return 'Incomplete'
    if bool(game.is_confirmed):
        return 'Completed'
    return 'Unconfirmed'


def find_related_game(
        *,
        channel_id: int,
        game_id: int | None) -> RelatedGame | None:
    """Resolve the legacy channel-first/game-ID fallback on a worker connection."""

    try:
        with models.db.connection_context():
            game = models.Game.by_channel_or_arg(
                chan_id=int(channel_id),
                arg=None if game_id is None else str(int(game_id)),
            )
            return RelatedGame(
                game_id=int(game.id),
                guild_id=int(game.guild_id),
                name=str(game.name or '')[:80],
                status=_status(game),
            )
    except (
        ValueError,
        exceptions.MyBaseException,
        models.DoesNotExist,
        peewee.PeeweeException,
    ):
        return None


async def run_find_related_game(
        *,
        channel_id: int,
        game_id: int | None) -> RelatedGame | None:
    """Run discovery off-loop and drain an already-started read on cancellation."""

    loop = asyncio.get_running_loop()
    concurrent_future = _executor.submit(functools.partial(
        find_related_game,
        channel_id=channel_id,
        game_id=game_id,
    ))
    future = asyncio.wrap_future(concurrent_future, loop=loop)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError as cancellation:
        current = asyncio.current_task()
        while not concurrent_future.done():
            if current is not None:
                while current.cancelling():
                    current.uncancel()
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                continue
        try:
            concurrent_future.result()
        except BaseException:
            pass
        raise asyncio.CancelledError from cancellation
