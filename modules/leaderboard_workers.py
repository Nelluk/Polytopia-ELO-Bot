"""Bounded worker-local reads for leaderboard snapshots."""

from __future__ import annotations

import asyncio
import datetime
import functools
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import peewee

from modules import models


VALID_SCOPES = frozenset({'local', 'global'})
VALID_RATINGS = frozenset({'current', 'peak'})
VALID_ERAS = frozenset({'current', 'all-time'})
VALID_POPULATIONS = frozenset({'active', 'all'})
VALID_ACTIVITY_VIEWS = frozenset({
    'server-30-days',
    'global-all-time',
})
VALID_SQUAD_PERIODS = frozenset({'current', 'all-time'})
DEFAULT_PAGE_SIZE = 10
MAX_LOADED_ROWS = 2000
MAX_ACTIVITY_SERVER_ROWS = 500
MAX_ACTIVITY_GLOBAL_ROWS = 1000
MAX_SQUAD_ROWS = 500


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


@dataclass(frozen=True)
class ActivityLeaderboardRequest:
    guild_id: int
    view: str = 'server-30-days'
    recent_cutoff: datetime.datetime | datetime.date | None = None


@dataclass(frozen=True)
class ActivityLeaderboardRow:
    rank: int
    name: str
    elo: int
    games: int
    team_emoji: str


@dataclass(frozen=True)
class ActivityLeaderboardResult:
    title: str
    total_players: int
    view: str
    rows: tuple[ActivityLeaderboardRow, ...]


@dataclass(frozen=True)
class SquadLeaderboardRequest:
    guild_id: int
    period: str = 'current'
    active_cutoff: datetime.datetime | datetime.date | None = None


@dataclass(frozen=True)
class SquadLeaderboardRow:
    rank: int
    squad_id: int
    squad_name: str
    member_names: tuple[str, ...]
    member_emojis: tuple[str, ...]
    elo: int
    wins: int
    losses: int


@dataclass(frozen=True)
class SquadLeaderboardResult:
    title: str
    total_squads: int
    rows: tuple[SquadLeaderboardRow, ...]


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


def load_activity_leaderboard(
    request: ActivityLeaderboardRequest,
) -> ActivityLeaderboardResult:
    """Load one immutable activity leaderboard on a local connection."""

    if request.view not in VALID_ACTIVITY_VIEWS:
        choices = ', '.join(sorted(VALID_ACTIVITY_VIEWS))
        raise ValueError(
            f'Invalid activity view {request.view!r}; expected {choices}.'
        )
    if request.guild_id <= 0:
        raise ValueError('guild_id must be a positive integer.')
    if (
        request.view == 'server-30-days'
        and request.recent_cutoff is None
    ):
        raise ValueError(
            'recent_cutoff is required for the server activity view.'
        )

    with models.db.connection_context():
        if request.view == 'global-all-time':
            query = (
                models.DiscordMember
                .select(
                    models.DiscordMember,
                    peewee.fn.COUNT(models.Lineup.id).alias('count'),
                )
                .join(models.Player)
                .join(models.Lineup)
                .join(models.Game)
                .where(models.Game.is_pending == 0)
                .group_by(models.DiscordMember.id)
                .order_by(-peewee.SQL('count'))
            )
            title = 'Most Active Players of All Time — Global'
            row_limit = MAX_ACTIVITY_GLOBAL_ROWS
            global_scope = True
        else:
            query = (
                models.Player
                .select(
                    models.Player,
                    peewee.fn.COUNT(models.Lineup.id).alias('count'),
                )
                .join(models.Lineup)
                .join(models.Game)
                .where(
                    (models.Lineup.player == models.Player.id)
                    & (
                        (models.Game.date > request.recent_cutoff)
                        | (
                            models.Game.completed_ts
                            > request.recent_cutoff
                        )
                    )
                    & (models.Game.guild_id == request.guild_id)
                )
                .group_by(models.Player.id)
                .order_by(-peewee.SQL('count'))
            )
            title = 'Most Active Players — This Server, Past 30 Days'
            row_limit = MAX_ACTIVITY_SERVER_ROWS
            global_scope = False

        total_players = query.count()
        rows = []
        for rank, player in enumerate(query[:row_limit], start=1):
            rows.append(
                ActivityLeaderboardRow(
                    rank=rank,
                    name=str(player.name),
                    elo=int(player.elo_moonrise),
                    games=int(player.count),
                    team_emoji=(
                        ''
                        if global_scope or not player.team
                        else str(player.team.emoji)
                    ),
                )
            )

    return ActivityLeaderboardResult(
        title=title,
        total_players=total_players,
        view=request.view,
        rows=tuple(rows),
    )


def load_squad_leaderboard(
    request: SquadLeaderboardRequest,
) -> SquadLeaderboardResult:
    """Load one immutable squad leaderboard on a local connection."""

    if request.period not in VALID_SQUAD_PERIODS:
        choices = ', '.join(sorted(VALID_SQUAD_PERIODS))
        raise ValueError(
            f'Invalid squad period {request.period!r}; expected {choices}.'
        )
    if request.guild_id <= 0:
        raise ValueError('guild_id must be a positive integer.')
    if request.period == 'current' and request.active_cutoff is None:
        raise ValueError(
            'active_cutoff is required for the current squad leaderboard.'
        )

    date_cutoff = (
        datetime.date.min
        if request.period == 'all-time'
        else request.active_cutoff
    )
    title = (
        'Squad Leaderboard — All Time'
        if request.period == 'all-time'
        else 'Squad Leaderboard'
    )

    with models.db.connection_context():
        query = models.Squad.leaderboard(
            date_cutoff=date_cutoff,
            guild_id=request.guild_id,
        )
        total_squads = query.count()
        rows = []
        for rank, squad in enumerate(
            query[:MAX_SQUAD_ROWS],
            start=1,
        ):
            wins, losses = squad.get_record()
            members = squad.get_members()
            rows.append(
                SquadLeaderboardRow(
                    rank=rank,
                    squad_id=int(squad.id),
                    squad_name=str(squad.name or ''),
                    member_names=tuple(
                        str(member.name) for member in members
                    ),
                    member_emojis=tuple(
                        str(member.team.emoji)
                        for member in members
                        if member.team is not None
                    ),
                    elo=int(squad.elo),
                    wins=int(wins),
                    losses=int(losses),
                )
            )

    return SquadLeaderboardResult(
        title=title,
        total_squads=total_squads,
        rows=tuple(rows),
    )


async def run_player_leaderboard(
    request: PlayerLeaderboardRequest,
) -> PlayerLeaderboardResult:
    """Submit a player leaderboard read to the bounded read executor."""

    loop = asyncio.get_running_loop()
    call = functools.partial(load_player_leaderboard, request)
    return await loop.run_in_executor(_leaderboard_read_executor, call)


async def run_activity_leaderboard(
    request: ActivityLeaderboardRequest,
) -> ActivityLeaderboardResult:
    """Submit an activity leaderboard read to the bounded executor."""

    loop = asyncio.get_running_loop()
    call = functools.partial(load_activity_leaderboard, request)
    return await loop.run_in_executor(_leaderboard_read_executor, call)


async def run_squad_leaderboard(
    request: SquadLeaderboardRequest,
) -> SquadLeaderboardResult:
    """Submit a squad leaderboard read to the bounded executor."""

    loop = asyncio.get_running_loop()
    call = functools.partial(load_squad_leaderboard, request)
    return await loop.run_in_executor(_leaderboard_read_executor, call)


def leaderboard_page_rows(
    rows: tuple,
    page_index: int,
    page_size: int,
) -> tuple[tuple, int, int, int]:
    if page_size <= 0:
        raise ValueError('page_size must be positive.')
    page_count = max(1, (len(rows) + page_size - 1) // page_size)
    if page_index < 0 or page_index >= page_count:
        raise IndexError('page_index is outside the leaderboard.')
    start = page_index * page_size
    end = min(start + page_size, len(rows))
    return rows[start:end], page_count, start, end


def player_leaderboard_page(
    result: PlayerLeaderboardResult,
    page_index: int,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> PlayerLeaderboardPage:
    """Slice one validated immutable page from a leaderboard snapshot."""

    loaded_count = len(result.rows)
    rows, page_count, start, end = leaderboard_page_rows(
        result.rows,
        page_index,
        page_size,
    )
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
