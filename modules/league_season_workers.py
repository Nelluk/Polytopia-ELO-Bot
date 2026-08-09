"""Bounded worker-local reads for PolyChampions season records."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import functools

from peewee import Case, fn

from modules import models


MAX_SEASON_ROWS = 500


class LeagueSeasonError(RuntimeError):
    """Base user-facing season-read failure."""


class LeagueSeasonPermissionError(LeagueSeasonError):
    """The requester is outside the configured scope."""


@dataclass(frozen=True)
class LeagueSeasonRequest:
    guild_id: int
    requester_id: int
    season: int | None
    league_scope: bool
    channel_allowed: bool
    tier_labels: tuple[tuple[int, str], ...]


@dataclass(frozen=True)
class LeagueSeasonTeamRow:
    team_id: int
    team_name: str
    team_emoji: str
    regular_wins: int
    regular_losses: int
    regular_incomplete: int
    postseason_wins: int
    postseason_losses: int
    postseason_incomplete: int


@dataclass(frozen=True)
class LeagueSeasonTier:
    tier_number: int
    tier_name: str
    teams: tuple[LeagueSeasonTeamRow, ...]


@dataclass(frozen=True)
class LeagueSeasonResult:
    guild_id: int
    requester_id: int
    season: int | None
    title: str
    tiers: tuple[LeagueSeasonTier, ...]
    historical_note: str | None
    rows_truncated: bool


_league_season_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix='polybot-league-season',
)


HISTORICAL_NOTE = (
    'Records from the first two seasons (ie. the dark ages when I did not '
    'exist) are mostly lost to antiquity, but some information remains:\n'
    '**The Sparkies** won Season 1 and **The Jets** won season 2, and if you '
    'squint you can just make out the records below:\n'
    'https://i.imgur.com/L7FPr1d.png'
)


def _validate(request: LeagueSeasonRequest) -> None:
    if not request.league_scope:
        raise LeagueSeasonPermissionError(
            'League season records are available only in the configured '
            'league server.'
        )
    if not request.channel_allowed:
        raise LeagueSeasonPermissionError(
            'This command can only be used in a designated ELO bot channel.'
        )


def _season_query(request: LeagueSeasonRequest):
    regular_wins = fn.SUM(Case(
        None,
        [(
            (models.GameSide.id == models.Game.winner)
            & (~models.Game.league_playoff)
            & models.Game.is_confirmed,
            1,
        )],
        0,
    )).alias('regular_wins')
    regular_losses = fn.SUM(Case(
        None,
        [(
            (models.GameSide.id != models.Game.winner)
            & (~models.Game.league_playoff)
            & models.Game.is_confirmed,
            1,
        )],
        0,
    )).alias('regular_losses')
    regular_incomplete = fn.SUM(Case(
        None,
        [(
            (~models.Game.is_confirmed) & (~models.Game.league_playoff),
            1,
        )],
        0,
    )).alias('regular_incomplete')
    postseason_wins = fn.SUM(Case(
        None,
        [(
            (models.GameSide.id == models.Game.winner)
            & models.Game.league_playoff
            & models.Game.is_confirmed,
            1,
        )],
        0,
    )).alias('postseason_wins')
    postseason_losses = fn.SUM(Case(
        None,
        [(
            (models.GameSide.id != models.Game.winner)
            & models.Game.league_playoff
            & models.Game.is_confirmed,
            1,
        )],
        0,
    )).alias('postseason_losses')
    postseason_incomplete = fn.SUM(Case(
        None,
        [(
            (~models.Game.is_confirmed) & models.Game.league_playoff,
            1,
        )],
        0,
    )).alias('postseason_incomplete')

    season_filter = (
        models.Game.league_season == int(request.season)
        if request.season
        else models.Game.league_season.is_null(False)
    )
    return (
        models.Team.select(
            models.Team.id,
            models.Team.name,
            models.Team.emoji,
            models.Game.league_tier.alias('league_tier'),
            regular_wins,
            regular_losses,
            regular_incomplete,
            postseason_wins,
            postseason_losses,
            postseason_incomplete,
        )
        .join(models.GameSide, on=(models.Team.id == models.GameSide.team_id))
        .join(models.Game, on=(models.GameSide.game_id == models.Game.id))
        .where(
            (models.Game.guild_id == int(request.guild_id))
            & (models.Game.league_tier.is_null(False))
            & (models.Game.is_pending == False)
            & season_filter
        )
        .group_by(
            models.Team.id,
            models.Team.name,
            models.Team.emoji,
            models.Game.league_tier,
        )
        .order_by(
            models.Game.league_tier,
            postseason_wins.desc(),
            regular_wins.desc(),
            regular_incomplete.desc(),
            models.Team.id,
        )
        .limit(MAX_SEASON_ROWS + 1)
    )


def _tier_name(
    tier_number: int,
    labels: dict[int, str],
    season: int | None,
) -> str:
    name = str(labels.get(int(tier_number), f'Tier {int(tier_number)}'))
    if season is not None and season <= 16:
        if name == 'Gold':
            return 'Pro'
        if name == 'Silver':
            return 'Jr'
    return name


def load_league_season(request: LeagueSeasonRequest) -> LeagueSeasonResult:
    """Read one bounded season snapshot on a worker-owned connection."""

    _validate(request)
    normalized_season = int(request.season) if request.season else None
    title = (
        f'Season {normalized_season} Records'
        if normalized_season is not None
        else 'League Records - All Seasons'
    )
    if normalized_season in {1, 2}:
        return LeagueSeasonResult(
            guild_id=int(request.guild_id),
            requester_id=int(request.requester_id),
            season=normalized_season,
            title=title,
            tiers=(),
            historical_note=HISTORICAL_NOTE,
            rows_truncated=False,
        )

    with models.db.connection_context():
        rows = tuple(_season_query(request))
    truncated = len(rows) > MAX_SEASON_ROWS
    rows = rows[:MAX_SEASON_ROWS]
    grouped = defaultdict(list)
    for row in rows:
        grouped[int(row.league_tier)].append(LeagueSeasonTeamRow(
            team_id=int(row.id),
            team_name=str(row.name),
            team_emoji=str(row.emoji or ''),
            regular_wins=int(row.regular_wins or 0),
            regular_losses=int(row.regular_losses or 0),
            regular_incomplete=int(row.regular_incomplete or 0),
            postseason_wins=int(row.postseason_wins or 0),
            postseason_losses=int(row.postseason_losses or 0),
            postseason_incomplete=int(row.postseason_incomplete or 0),
        ))
    labels = {int(number): str(name) for number, name in request.tier_labels}
    tiers = tuple(
        LeagueSeasonTier(
            tier_number=tier_number,
            tier_name=_tier_name(tier_number, labels, normalized_season),
            teams=tuple(grouped[tier_number]),
        )
        for tier_number in sorted(grouped)
    )
    return LeagueSeasonResult(
        guild_id=int(request.guild_id),
        requester_id=int(request.requester_id),
        season=normalized_season,
        title=title,
        tiers=tiers,
        historical_note=None,
        rows_truncated=truncated,
    )


async def _run_worker(function, request):
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(
        _league_season_executor,
        functools.partial(function, request),
    )
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
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
        raise


async def run_league_season(request: LeagueSeasonRequest) -> LeagueSeasonResult:
    return await _run_worker(load_league_season, request)
