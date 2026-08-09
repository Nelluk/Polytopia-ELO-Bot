"""Bounded worker-local reads and mutations for league token balances."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import datetime
import functools
import re

from modules import models


MAX_HOUSES = 100
MAX_LOG_ROWS = 250
MIN_TOKEN_BALANCE = -32768
MAX_TOKEN_BALANCE = 32767
MAX_NOTE_LENGTH = 500


class LeagueTokensError(RuntimeError):
    """Base user-facing league-token failure."""


class LeagueTokensPermissionError(LeagueTokensError):
    """The requester cannot perform this operation."""


class LeagueTokensLookupError(LeagueTokensError):
    """The requested House cannot be resolved uniquely."""


class LeagueTokensValidationError(LeagueTokensError):
    """The request contains invalid or conflicting values."""


class LeagueTokensConflictError(LeagueTokensValidationError):
    """The balance changed after the read snapshot."""


class LeagueTokensPublicationError(LeagueTokensError):
    """Committed or loaded public output could not be published."""


@dataclass(frozen=True)
class LeagueTokensReadRequest:
    guild_id: int
    requester_id: int
    requester_level: int
    league_scope: bool
    house_lookup: str | None


@dataclass(frozen=True)
class LeagueTokenHouse:
    house_id: int
    name: str
    emoji: str
    balance: int


@dataclass(frozen=True)
class LeagueTokenLog:
    log_id: int
    house_id: int | None
    timestamp: str
    message: str


@dataclass(frozen=True)
class LeagueTokensReadResult:
    guild_id: int
    requester_id: int
    requester_level: int
    houses: tuple[LeagueTokenHouse, ...]
    logs: tuple[LeagueTokenLog, ...]
    selected_house_id: int | None
    houses_truncated: bool
    logs_truncated: bool


@dataclass(frozen=True)
class LeagueTokensMutationRequest:
    guild_id: int
    requester_id: int
    requester_level: int
    league_scope: bool
    house_id: int
    expected_house_name: str
    expected_balance: int
    new_balance: int
    note: str | None
    requester_description: str


@dataclass(frozen=True)
class LeagueTokensMutationResult:
    guild_id: int
    house_id: int
    house_name: str
    old_balance: int
    new_balance: int
    note: str | None
    log_id: int
    timestamp: str
    audit_message: str


_HOUSE_ID_PATTERNS = (
    re.compile(r'House ID=(\d+)', re.IGNORECASE),
    re.compile(r'FATS id=(\d+)', re.IGNORECASE),
)


def _validate_scope(request) -> None:
    if not bool(request.league_scope):
        raise LeagueTokensPermissionError(
            'League token commands are available only in the configured league server.'
        )


def _resolve_house(houses, lookup: str | None):
    value = str(lookup or '').strip()
    if not value:
        return None
    exact = [row for row in houses if str(row.name).casefold() == value.casefold()]
    matches = exact or [
        row for row in houses if value.casefold() in str(row.name).casefold()
    ]
    if not matches:
        raise LeagueTokensLookupError(
            f'No matching House was found for "{value}".'
        )
    if len(matches) > 1:
        raise LeagueTokensLookupError(
            f'More than one matching House was found for "{value}".'
        )
    return matches[0]


def _token_log_query(guild_id: int):
    token_marker = (
        models.GameLog.message.contains('league tokens')
        | models.GameLog.message.contains('FATS id=')
    )
    return (
        models.GameLog.select()
        .where(
            ((models.GameLog.guild_id == int(guild_id)) | (models.GameLog.guild_id == 0))
            & (models.GameLog.is_protected == 0)
            & token_marker
        )
        .order_by(-models.GameLog.message_ts, -models.GameLog.id)
        .limit(MAX_LOG_ROWS + 1)
    )


def _house_id_from_log(message: str) -> int | None:
    for pattern in _HOUSE_ID_PATTERNS:
        match = pattern.search(message)
        if match:
            return int(match.group(1))
    return None


def _timestamp(value) -> str:
    if isinstance(value, datetime.datetime):
        return value.strftime('%Y-%m-%d %H:%M:%S')
    return str(value)


def load_league_tokens(
    request: LeagueTokensReadRequest,
) -> LeagueTokensReadResult:
    """Load all bounded token balances/history on a worker-owned connection."""

    _validate_scope(request)
    with models.db.connection_context():
        house_rows = tuple(
            models.House.select()
            .order_by(models.House.name, models.House.id)
            .limit(MAX_HOUSES + 1)
        )
        houses_truncated = len(house_rows) > MAX_HOUSES
        house_rows = house_rows[:MAX_HOUSES]
        selected = _resolve_house(house_rows, request.house_lookup)

        log_rows = tuple(_token_log_query(request.guild_id))
        logs_truncated = len(log_rows) > MAX_LOG_ROWS
        log_rows = log_rows[:MAX_LOG_ROWS]

        return LeagueTokensReadResult(
            guild_id=int(request.guild_id),
            requester_id=int(request.requester_id),
            requester_level=int(request.requester_level),
            houses=tuple(
                LeagueTokenHouse(
                    house_id=int(row.id),
                    name=str(row.name),
                    emoji=str(getattr(row, 'emoji', '') or ''),
                    balance=int(getattr(row, 'league_tokens', 0)),
                )
                for row in house_rows
            ),
            logs=tuple(
                LeagueTokenLog(
                    log_id=int(row.id),
                    house_id=_house_id_from_log(str(row.message or '')),
                    timestamp=_timestamp(row.message_ts),
                    message=str(row.message or ''),
                )
                for row in log_rows
            ),
            selected_house_id=(int(selected.id) if selected is not None else None),
            houses_truncated=houses_truncated,
            logs_truncated=logs_truncated,
        )


def validate_note(note: str | None) -> str | None:
    value = str(note or '').strip()
    if not value:
        return None
    if len(value) > MAX_NOTE_LENGTH:
        raise LeagueTokensValidationError(
            f'Token notes must be {MAX_NOTE_LENGTH} characters or fewer.'
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise LeagueTokensValidationError(
            'Token notes cannot contain control characters.'
        )
    return value


def mutate_league_tokens(
    request: LeagueTokensMutationRequest,
) -> LeagueTokensMutationResult:
    """Commit one token balance and actor-attributed audit row atomically."""

    _validate_scope(request)
    if int(request.requester_level) <= 4:
        raise LeagueTokensPermissionError(
            'You are not authorized to alter league tokens.'
        )
    if not MIN_TOKEN_BALANCE <= int(request.new_balance) <= MAX_TOKEN_BALANCE:
        raise LeagueTokensValidationError(
            f'Token balances must be between {MIN_TOKEN_BALANCE} and {MAX_TOKEN_BALANCE}.'
        )
    note = validate_note(request.note)

    with models.db.connection_context():
        try:
            house = models.House.get_by_id(int(request.house_id))
        except models.House.DoesNotExist as exc:
            raise LeagueTokensLookupError(
                'The requested House no longer exists.'
            ) from exc
        old_name = str(house.name)
        old_balance = int(house.league_tokens)
        if old_name != str(request.expected_house_name):
            raise LeagueTokensConflictError(
                'The House name changed before this update was applied.'
            )
        if old_balance != int(request.expected_balance):
            raise LeagueTokensConflictError(
                'The token balance changed before this update was applied. Run the command again.'
            )
        if old_balance == int(request.new_balance):
            raise LeagueTokensValidationError(
                f'House **{old_name}** already has {old_balance} league tokens.'
            )

        note_suffix = f' - Note: {note}' if note else ''
        audit_message = (
            f'{request.requester_description} updated league tokens (FATs) '
            f'for House ID={int(house.id)} {old_name} from {old_balance} '
            f'to {int(request.new_balance)}{note_suffix}'
        )
        with models.db.atomic():
            house.league_tokens = int(request.new_balance)
            house.save()
            log = models.GameLog.write(
                guild_id=int(request.guild_id),
                message=audit_message,
            )

        return LeagueTokensMutationResult(
            guild_id=int(request.guild_id),
            house_id=int(house.id),
            house_name=old_name,
            old_balance=old_balance,
            new_balance=int(request.new_balance),
            note=note,
            log_id=int(log.id),
            timestamp=_timestamp(log.message_ts),
            audit_message=audit_message,
        )


_league_tokens_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='polybot-league-tokens',
)


async def _run_worker(function, request, *, drain_on_cancel: bool):
    future = _league_tokens_executor.submit(functools.partial(function, request))
    try:
        while not future.done():
            await asyncio.sleep(0.001)
    except asyncio.CancelledError:
        if not drain_on_cancel:
            future.cancel()
            raise
        task = asyncio.current_task()
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
        raise asyncio.CancelledError
    return future.result()


async def run_league_tokens_read(request):
    return await _run_worker(load_league_tokens, request, drain_on_cancel=False)


async def run_league_tokens_mutation(request):
    return await _run_worker(mutate_league_tokens, request, drain_on_cancel=True)
