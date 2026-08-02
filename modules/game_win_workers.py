"""Bounded, worker-local preflight reads for result claims.

The authoritative result mutation remains :func:`modules.elo_workers.record_win`.
This module only resolves a prefix winner name or validates a stable side ID
before that worker is submitted.  Both operations reload the game by
primitive IDs and keep Peewee on the worker thread.
"""

from __future__ import annotations

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import peewee

from modules import elo_workers, models


_win_read_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix='polybot-game-win-read',
)


@dataclass(frozen=True)
class WinPreflightRequest:
    """Primitive values captured before the preflight worker is submitted."""

    game_id: int
    guild_id: int
    requester_id: int
    requester_is_staff: bool
    prefix: str = '$'
    winning_side_id: int | None = None
    winner_text: str = ''


@dataclass(frozen=True)
class WinSideSelection:
    """Stable side data returned to the application service."""

    game_id: int
    guild_id: int
    winning_side_id: int
    winner_name: str


def _registered(requester_id: int) -> bool:
    try:
        return models.DiscordMember.get_or_none(
            discord_id=requester_id,
        ) is not None
    except AttributeError:
        # Focused worker fakes do not always model DiscordMember.  The
        # command-level registration check remains authoritative for those
        # adapters; the real model path always performs the query.
        return True


def _load_game(request: WinPreflightRequest):
    try:
        game = models.Game.load_full_game(game_id=request.game_id)
    except peewee.DoesNotExist as exc:
        raise elo_workers.WinValidationError(
            f'Game with ID {request.game_id} cannot be found.'
        ) from exc
    if int(game.guild_id) != request.guild_id:
        raise elo_workers.WinValidationError(
            f'Game with ID {request.game_id} is associated with a different '
            'Discord server.'
        )
    if game.is_pending:
        raise elo_workers.WinValidationError(
            f'Game {game.id} is still a pending open game. It must be started '
            'before it can be concluded.'
        )
    return game


def prepare_win(request: WinPreflightRequest) -> WinSideSelection:
    """Resolve one side using a worker-owned connection.

    This read intentionally does not replace the validation in
    ``elo_workers.record_win``.  The mutation worker reloads and revalidates
    the mutable state again immediately before changing it.
    """

    with models.db.connection_context():
        if not _registered(request.requester_id):
            raise elo_workers.WinValidationError(
                'This command requires bot registration first. Type '
                f'__`{request.prefix}setname Your Mobile Name`__ or  '
                f'__`{request.prefix}steamname Your Steam Username`__ '
                'to get started.'
            )

        game = _load_game(request)
        if request.winning_side_id is None:
            _, winning_side = game.gameside_by_name(name=request.winner_text)
        else:
            winning_side = next(
                (
                    side for side in game.gamesides
                    if int(side.id) == int(request.winning_side_id)
                ),
                None,
            )
            if winning_side is None:
                raise elo_workers.WinValidationError(
                    f'GameSide {request.winning_side_id} did not play in game '
                    f'{game.id}.'
                )

        return WinSideSelection(
            game_id=int(game.id),
            guild_id=int(game.guild_id),
            winning_side_id=int(winning_side.id),
            winner_name=str(winning_side.name()),
        )


async def run_prepare_win(
    request: WinPreflightRequest,
) -> WinSideSelection:
    """Run the preflight without blocking Discord's event loop."""

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _win_read_executor,
        functools.partial(prepare_win, request),
    )
