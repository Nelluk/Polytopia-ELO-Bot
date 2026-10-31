"""Bounded game-context discovery for structured staff-help reports."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import functools
import logging
import peewee

from modules import exceptions, game_detail_workers, models


logger = logging.getLogger('polybot.' + __name__)

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
    snapshot: game_detail_workers.GameDetailSnapshot | None = None


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
            try:
                snapshot = game_detail_workers.snapshot_loaded_game(
                    game, request_guild_id=int(game.guild_id),
                )
            except Exception:
                logger.warning('Staff-help game summary unavailable (game=%s).', game.id)
                snapshot = None
            return RelatedGame(
                game_id=int(game.id),
                guild_id=int(game.guild_id),
                name=str(game.name or '')[:80],
                status=_status(game),
                snapshot=snapshot,
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

    return await _run(functools.partial(
        find_related_game, channel_id=channel_id, game_id=game_id,
    ))


def record_request(
        *, guild_id: int, game_id: int | None, requester_id: int,
        requester_name: str, summary: str, details: str,
        context: str | None) -> None:
    """Record the accepted request before publishing Discord effects."""
    import discord

    with models.db.connection_context(), models.db.atomic():
        models.GameLog.write(
            guild_id=guild_id,
            game_id=game_id or 0,
            message=(
                f'**{discord.utils.escape_markdown(requester_name)}** (`{requester_id}`) '
                f'requested staffhelp: *{summary}*\n{details}'
                + (f'\n{context}' if context else '')
            ),
        )


async def run_record_request(**kwargs) -> None:
    await _run(functools.partial(record_request, **kwargs))


async def _run(operation):
    loop = asyncio.get_running_loop()
    concurrent_future = _executor.submit(operation)
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
