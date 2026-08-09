"""Bounded worker for the staff league-game CSV export."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import threading

import peewee

from modules import models, utilities


MAX_EXPORT_GAMES = 25_000
DEFAULT_ATTACHMENT_LIMIT = 8 * 1024 * 1024


class LeagueExportError(RuntimeError):
    """Base user-facing export failure."""


class LeagueExportPermissionError(LeagueExportError):
    """The requester is not allowed to export league data."""


class LeagueExportEmptyError(LeagueExportError):
    """No games match the fixed legacy export scope."""


class LeagueExportBusyError(LeagueExportError):
    """Another export already owns the single worker slot."""


class LeagueExportTooLargeError(LeagueExportError):
    """The result cannot be delivered through Discord."""


@dataclass(frozen=True)
class LeagueExportRequest:
    guild_id: int
    requester_id: int
    requester_is_staff: bool
    league_scope: bool
    include_logs: bool
    attachment_limit: int


@dataclass(frozen=True)
class LeagueExportResult:
    guild_id: int
    requester_id: int
    include_logs: bool
    game_count: int
    filename: str
    payload: bytes


_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='league-export')
_claim = threading.Lock()


def _query(request: LeagueExportRequest):
    query = models.Game.select()
    if request.include_logs:
        query = (
            models.Game.select(
                models.Game,
                peewee.fn.ARRAY_AGG(models.GameLog.message).alias('gamelogs'),
            )
            .join(
                models.GameLog,
                peewee.JOIN.LEFT_OUTER,
                on=(
                    models.GameLog.message
                    ** peewee.fn.CONCAT('__', models.Game.id, '__%')
                ),
            )
            .group_by(models.Game.id)
        )
    return query.where(
        (models.Game.is_confirmed == True)
        & (models.Game.guild_id == int(request.guild_id))
        & (models.Game.is_ranked == True)
        & (
            (models.Game.size == [2, 2])
            | (models.Game.size == [3, 3])
        )
    ).order_by(models.Game.date, models.Game.id)


def _generate(request: LeagueExportRequest) -> LeagueExportResult:
    if not request.league_scope or not request.requester_is_staff:
        raise LeagueExportPermissionError(
            'League exports require staff access in the configured league server.'
        )
    if int(request.guild_id) <= 0 or int(request.requester_id) <= 0:
        raise LeagueExportPermissionError('The server and requester must be valid.')
    attachment_limit = int(request.attachment_limit)
    if attachment_limit <= 0:
        attachment_limit = DEFAULT_ATTACHMENT_LIMIT

    with models.db.connection_context():
        query = _query(request)
        game_count = int(query.count())
        if game_count == 0:
            raise LeagueExportEmptyError(
                'No confirmed ranked 2v2 or 3v3 league games were found.'
            )
        if game_count > MAX_EXPORT_GAMES:
            raise LeagueExportTooLargeError(
                f'The export matches {game_count:,} games, above the safe '
                f'{MAX_EXPORT_GAMES:,}-game limit.'
            )
        payload = utilities.export_game_data_brief_bytes(
            query=query,
            export_logs=bool(request.include_logs),
        )

    if len(payload) > attachment_limit:
        raise LeagueExportTooLargeError(
            f'The compressed export is {len(payload):,} bytes, above this '
            f'server\'s {attachment_limit:,}-byte upload limit. Try again '
            'without logs.'
        )
    suffix = '-with-logs' if request.include_logs else ''
    return LeagueExportResult(
        guild_id=int(request.guild_id),
        requester_id=int(request.requester_id),
        include_logs=bool(request.include_logs),
        game_count=game_count,
        filename=f'league-games{suffix}.csv.gz',
        payload=bytes(payload),
    )


async def run_league_export(
    request: LeagueExportRequest,
) -> LeagueExportResult:
    """Reject conflicts promptly and drain non-cancellable thread work."""

    if not _claim.acquire(blocking=False):
        raise LeagueExportBusyError(
            'Another league export is already running. Try again after it finishes.'
        )

    def work():
        try:
            return _generate(request)
        finally:
            _claim.release()

    try:
        concurrent_future = _executor.submit(work)
    except BaseException:
        _claim.release()
        raise
    try:
        while not concurrent_future.done():
            await asyncio.sleep(0.001)
    except asyncio.CancelledError:
        task = asyncio.current_task()
        while not concurrent_future.done():
            if task is not None:
                task.uncancel()
            try:
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                continue
        try:
            concurrent_future.result()
        except BaseException as exc:
            raise asyncio.CancelledError from exc
        raise asyncio.CancelledError
    return concurrent_future.result()
