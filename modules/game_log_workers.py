"""Bounded worker-local reads for permission-aware game audit logs."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import functools

import settings
from modules import models


MAX_LOG_ROWS = 500
MAX_QUERY_TERMS = 8
MAX_QUERY_TERM_LENGTH = 80
MAX_ROW_MESSAGE_LENGTH = 1_500


class GameLogReadError(ValueError):
    """Base user-facing game-log read failure."""


class GameLogPermissionError(GameLogReadError):
    """The requester cannot read the selected audit scope."""


class GameLogLookupError(GameLogReadError):
    """The selected game does not exist in the requester's guild."""


@dataclass(frozen=True, slots=True)
class GameLogKey:
    scope: str = 'guild'
    game_id: int | None = None
    include_terms: tuple[str, ...] = ()
    exclude_term: str = ''


@dataclass(frozen=True, slots=True)
class GameLogRequest:
    guild_id: int
    requester_id: int
    requester_is_staff: bool
    requester_is_owner: bool
    key: GameLogKey


@dataclass(frozen=True, slots=True)
class GameLogRow:
    log_id: int
    guild_id: int
    timestamp: str
    message: str
    message_truncated: bool


@dataclass(frozen=True, slots=True)
class GameLogSnapshot:
    key: GameLogKey
    title: str
    rows: tuple[GameLogRow, ...]
    truncated: bool


_game_log_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix='polybot-game-logs',
)


def _validate_term(value: str) -> str:
    term = str(value or '').strip()
    if len(term) > MAX_QUERY_TERM_LENGTH:
        raise GameLogReadError(
            f'Each log search term must be at most {MAX_QUERY_TERM_LENGTH} characters.'
        )
    return term


def normalize_key(key: GameLogKey) -> GameLogKey:
    scope = str(key.scope or '').lower()
    if scope not in {'game', 'guild', 'global'}:
        raise GameLogReadError('Choose a game, server, or global log scope.')
    game_id = int(key.game_id) if key.game_id is not None else None
    if scope == 'game' and (game_id is None or game_id <= 0):
        raise GameLogReadError('A positive game ID is required for game logs.')
    if scope != 'game':
        game_id = None
    terms = tuple(
        term for term in (_validate_term(value) for value in key.include_terms)
        if term
    )
    if len(terms) > MAX_QUERY_TERMS:
        raise GameLogReadError(
            f'Use at most {MAX_QUERY_TERMS} required search terms.'
        )
    return GameLogKey(
        scope=scope,
        game_id=game_id,
        include_terms=terms,
        exclude_term=_validate_term(key.exclude_term),
    )


def _requester_is_game_participant(
    *,
    game_id: int,
    guild_id: int,
    requester_id: int,
) -> bool:
    return (
        models.Lineup
        .select(models.Lineup.id)
        .join(models.Player)
        .join(models.DiscordMember)
        .where(
            (models.Lineup.game == int(game_id))
            & (models.Player.guild_id == int(guild_id))
            & (models.DiscordMember.discord_id == int(requester_id))
        )
        .exists()
    )


def _validate_scope(request: GameLogRequest, key: GameLogKey) -> None:
    requester_is_owner = (
        request.requester_is_owner
        and int(request.requester_id) == int(settings.owner_id)
    )
    if key.scope == 'global':
        if not requester_is_owner:
            raise GameLogPermissionError(
                'Only the bot owner can view logs across all servers.'
            )
        return
    if key.scope == 'guild':
        if not request.requester_is_staff and not requester_is_owner:
            raise GameLogPermissionError(
                'Non-staff users must select a game they participated in.'
            )
        return

    game = models.Game.get_or_none(
        (models.Game.id == int(key.game_id))
        & (models.Game.guild_id == int(request.guild_id))
    )
    if game is None:
        raise GameLogLookupError('No matching game was found in this server.')
    if not request.requester_is_staff and not _requester_is_game_participant(
        game_id=int(key.game_id),
        guild_id=int(request.guild_id),
        requester_id=int(request.requester_id),
    ):
        raise GameLogPermissionError(
            'You do not have permission to view logs for that game.'
        )


def _query_logs(request: GameLogRequest, key: GameLogKey):
    query = models.GameLog.select().where(models.GameLog.is_protected == 0)
    if key.scope == 'global':
        title = 'All-server audit logs'
    else:
        query = query.where(
            (models.GameLog.guild_id == int(request.guild_id))
            | (models.GameLog.guild_id == 0)
        )
        if key.scope == 'game':
            marker = f'__{int(key.game_id)}__'
            query = query.where(models.GameLog.message.contains(marker))
            title = f'Game {key.game_id} audit logs'
        else:
            title = 'Recent server audit logs'
    for term in key.include_terms:
        query = query.where(models.GameLog.message ** f'%{term}%')
    if key.exclude_term:
        query = query.where(~(models.GameLog.message ** f'%{key.exclude_term}%'))
    return query.order_by(
        -models.GameLog.message_ts,
        -models.GameLog.id,
    ).limit(MAX_LOG_ROWS + 1), title


def read_game_logs(request: GameLogRequest) -> GameLogSnapshot:
    """Read one authorized immutable log snapshot on a local connection."""

    key = normalize_key(request.key)
    with models.db.connection_context():
        _validate_scope(request, key)
        query, title = _query_logs(request, key)
        entries = tuple(query)
        truncated = len(entries) > MAX_LOG_ROWS
        rows = []
        for entry in entries[:MAX_LOG_ROWS]:
            message = str(entry.message or '')
            rows.append(GameLogRow(
                log_id=int(entry.id),
                guild_id=int(entry.guild_id),
                timestamp=entry.message_ts.strftime('%Y-%m-%d %H:%M:%S'),
                message=message[:MAX_ROW_MESSAGE_LENGTH],
                message_truncated=len(message) > MAX_ROW_MESSAGE_LENGTH,
            ))
        return GameLogSnapshot(
            key=key,
            title=title,
            rows=tuple(rows),
            truncated=truncated,
        )


async def run_game_log_read(request: GameLogRequest) -> GameLogSnapshot:
    """Submit one bounded read and drain its thread on cancellation."""

    loop = asyncio.get_running_loop()
    concurrent_future = _game_log_executor.submit(
        functools.partial(read_game_logs, request),
    )
    future = asyncio.wrap_future(concurrent_future, loop=loop)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError as cancellation:
        task = asyncio.current_task()
        while not concurrent_future.done():
            if task is not None:
                while task.cancelling():
                    task.uncancel()
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                continue
        try:
            concurrent_future.result()
        except BaseException:
            pass
        raise asyncio.CancelledError from cancellation
