"""Bounded worker-local cleanup for Discord guild-member departures."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

import peewee

from modules import models


class MemberRemovalError(RuntimeError):
    """The departure cleanup could not commit coherently."""


class MemberRemovalConflictError(MemberRemovalError):
    """The pending-lineup graph changed during departure cleanup."""


@dataclass(frozen=True)
class MemberRemovalRequest:
    guild_id: int
    member_id: int
    member_description: str


@dataclass(frozen=True)
class MemberRemovalResult:
    guild_id: int
    member_id: int
    registered: bool
    player_id: int | None
    pending_game_ids: tuple[int, ...]
    deleted_pending_count: int
    incomplete_game_ids: tuple[int, ...]

    @property
    def incomplete_count(self) -> int:
        return len(self.incomplete_game_ids)


_member_removal_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='member-removal-cleanup',
)


def _player_for_member(request: MemberRemovalRequest):
    return (
        models.Player
        .select(models.Player)
        .join(models.DiscordMember)
        .where(
            (models.DiscordMember.discord_id == int(request.member_id))
            & (models.Player.guild_id == int(request.guild_id))
        )
        .get()
    )


def _lineup_rows(*, player_id: int, pending: bool):
    query = (
        models.Lineup
        .select(models.Lineup.id, models.Game.id)
        .join(models.Game)
        .where(
            (models.Lineup.player == int(player_id))
            & (models.Game.is_pending == bool(pending))
        )
    )
    if not pending:
        query = query.where(models.Game.is_completed == False)
    return tuple(
        (int(lineup_id), int(game_id))
        for lineup_id, game_id in query.order_by(
            models.Game.id,
            models.Lineup.id,
        ).tuples()
    )


def _cleanup_member_removal(
    request: MemberRemovalRequest,
) -> MemberRemovalResult:
    if request.guild_id <= 0 or request.member_id <= 0:
        raise MemberRemovalError('Guild and member IDs must be valid.')
    if not str(request.member_description).strip():
        raise MemberRemovalError('The departing member description is required.')

    with models.db.connection_context():
        with models.db.atomic():
            try:
                player = _player_for_member(request)
            except peewee.DoesNotExist:
                return MemberRemovalResult(
                    guild_id=int(request.guild_id),
                    member_id=int(request.member_id),
                    registered=False,
                    player_id=None,
                    pending_game_ids=(),
                    deleted_pending_count=0,
                    incomplete_game_ids=(),
                )

            pending_rows = _lineup_rows(
                player_id=int(player.id),
                pending=True,
            )
            incomplete_rows = _lineup_rows(
                player_id=int(player.id),
                pending=False,
            )
            for _lineup_id, game_id in pending_rows:
                models.GameLog.write(
                    game_id=game_id,
                    guild_id=int(request.guild_id),
                    message=(
                        f'{request.member_description} left the game while '
                        'leaving the server.'
                    ),
                )

            pending_lineup_ids = tuple(row[0] for row in pending_rows)
            deleted_count = 0
            if pending_lineup_ids:
                deleted_count = (
                    models.Lineup
                    .delete()
                    .where(models.Lineup.id.in_(pending_lineup_ids))
                    .execute()
                )
                if int(deleted_count) != len(pending_lineup_ids):
                    raise MemberRemovalConflictError(
                        'Pending lineups changed during member-removal cleanup.'
                    )

    return MemberRemovalResult(
        guild_id=int(request.guild_id),
        member_id=int(request.member_id),
        registered=True,
        player_id=int(player.id),
        pending_game_ids=tuple(row[1] for row in pending_rows),
        deleted_pending_count=int(deleted_count),
        incomplete_game_ids=tuple(row[1] for row in incomplete_rows),
    )


async def _drain_future(future: Future):
    try:
        while not future.done():
            await asyncio.sleep(0.001)
    except asyncio.CancelledError:
        task = asyncio.current_task()
        while not future.done():
            if task is not None:
                task.uncancel()
            try:
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                continue
        try:
            future.result()
        except BaseException as exc:
            raise asyncio.CancelledError from exc
        raise asyncio.CancelledError
    return future.result()


async def run_member_removal(
    request: MemberRemovalRequest,
) -> MemberRemovalResult:
    future = _member_removal_executor.submit(
        _cleanup_member_removal,
        request,
    )
    return await _drain_future(future)
