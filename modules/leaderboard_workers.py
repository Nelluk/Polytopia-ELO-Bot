"""Bounded worker-local reads for player leaderboards."""

from __future__ import annotations

import asyncio
import datetime
import functools
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from modules import models


VALID_SCOPES = frozenset({'local', 'global'})
VALID_RATINGS = frozenset({'current', 'peak'})
VALID_ERAS = frozenset({'current', 'all-time'})
VALID_POPULATIONS = frozenset({'active', 'all'})
DEFAULT_PAGE_SIZE = 10
MAX_LOADED_ROWS = 2000


@dataclass(frozen=True)
class PlayerLeaderboardRequest:
    guild_id: int
    scope: str = 'local'
    rating: str = 'current'
    era: str = 'current'
    population: str = 'active'
    active_cutoff: datetime.datetime | datetime.date | None = None


@dataclass(frozen=True)
class PlayerLeaderboardRow:
    rank: int
    name: str
    elo: int
    wins: int
    losses: int
    team_emoji: str


@dataclass(frozen=True)
class PlayerLeaderboardResult:
    title: str
    total_ranked: int
    rows: tuple[PlayerLeaderboardRow, ...]


@dataclass(frozen=True)
class PlayerLeaderboardPage:
    title: str
    total_ranked: int
    loaded_count: int
    page_index: int
    page_count: int
    start_rank: int
    end_rank: int
    rows: tuple[PlayerLeaderboardRow, ...]


_leaderboard_read_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix='polybot-leaderboard-read',
)


def _validate_request(request: PlayerLeaderboardRequest) -> None:
    values = (
        ('scope', request.scope, VALID_SCOPES),
        ('rating', request.rating, VALID_RATINGS),
        ('era', request.era, VALID_ERAS),
        ('population', request.population, VALID_POPULATIONS),
    )
    for field, value, valid_values in values:
        if value not in valid_values:
            choices = ', '.join(sorted(valid_values))
            raise ValueError(f'Invalid {field} {value!r}; expected {choices}.')
    if request.guild_id <= 0:
        raise ValueError('guild_id must be a positive integer.')
    if request.population == 'active' and request.active_cutoff is None:
        raise ValueError('active_cutoff is required for active leaderboards.')


def _leaderboard_title(request: PlayerLeaderboardRequest) -> str:
    title = (
        'Global Leaderboard'
        if request.scope == 'global'
        else 'Individual Leaderboard'
    )
    if request.population == 'all':
        title += ' - Including Inactive Players'
    if request.rating == 'peak':
        title += ' - Maximum ELO Achieved'
    if request.era == 'all-time':
        title += ' - Alltime (not reset)'
    return title


def load_player_leaderboard(
    request: PlayerLeaderboardRequest,
) -> PlayerLeaderboardResult:
    """Load one immutable leaderboard snapshot on a local connection."""

    _validate_request(request)
    target_model = (
        models.DiscordMember
        if request.scope == 'global'
        else models.Player
    )
    date_cutoff = (
        datetime.date.min
        if request.population == 'all'
        else request.active_cutoff
    )
    max_flag = request.rating == 'peak'
    version = 'ALLTIME' if request.era == 'all-time' else None
    global_scope = request.scope == 'global'

    with models.db.connection_context():
        query = target_model.leaderboard(
            date_cutoff=date_cutoff,
            guild_id=request.guild_id,
            max_flag=max_flag,
            version=version,
        )
        total_ranked = query.count()
        rows = []
        for rank, player in enumerate(
            query[:MAX_LOADED_ROWS],
            start=1,
        ):
            wins, losses = player.get_record(version=version)
            team_emoji = (
                player.team.emoji
                if not global_scope and player.team
                else ''
            )
            rows.append(
                PlayerLeaderboardRow(
                    rank=rank,
                    name=str(player.name),
                    elo=int(player.elo_field),
                    wins=int(wins),
                    losses=int(losses),
                    team_emoji=str(team_emoji),
                )
            )

    return PlayerLeaderboardResult(
        title=_leaderboard_title(request),
        total_ranked=total_ranked,
        rows=tuple(rows),
    )


async def run_player_leaderboard(
    request: PlayerLeaderboardRequest,
) -> PlayerLeaderboardResult:
    """Submit a player leaderboard read to the bounded read executor."""

    loop = asyncio.get_running_loop()
    call = functools.partial(load_player_leaderboard, request)
    return await loop.run_in_executor(_leaderboard_read_executor, call)


def player_leaderboard_page(
    result: PlayerLeaderboardResult,
    page_index: int,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> PlayerLeaderboardPage:
    """Slice one validated immutable page from a leaderboard snapshot."""

    if page_size <= 0:
        raise ValueError('page_size must be positive.')
    loaded_count = len(result.rows)
    page_count = max(1, (loaded_count + page_size - 1) // page_size)
    if page_index < 0 or page_index >= page_count:
        raise IndexError('page_index is outside the leaderboard.')

    start = page_index * page_size
    end = min(start + page_size, loaded_count)
    rows = result.rows[start:end]
    return PlayerLeaderboardPage(
        title=result.title,
        total_ranked=result.total_ranked,
        loaded_count=loaded_count,
        page_index=page_index,
        page_count=page_count,
        start_rank=rows[0].rank if rows else 0,
        end_rank=rows[-1].rank if rows else 0,
        rows=rows,
    )
