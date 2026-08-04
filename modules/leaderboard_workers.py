"""Bounded worker-local reads for leaderboard snapshots."""

from __future__ import annotations

import asyncio
import datetime
import functools
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from io import BytesIO
import logging

import peewee

from modules import models
import settings


logger = logging.getLogger('polybot.' + __name__)


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
TEAM_LEADERBOARD_PAGE_SIZE = DEFAULT_PAGE_SIZE
TEAM_GRAPH_SERIES_LIMIT = TEAM_LEADERBOARD_PAGE_SIZE
TEAM_GRAPH_HISTORY_POINT_LIMIT = 250


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
    discord_id: int | None = None


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


@dataclass(frozen=True)
class TeamLeaderboardRoleSnapshot:
    """Primitive Discord role data captured before a read worker starts."""

    role_name: str
    role_color: str
    active_member_count: int


@dataclass(frozen=True)
class TeamLeaderboardRequest:
    """Immutable team-leaderboard input; no Discord/Peewee object crosses over."""

    guild_id: int
    database_guild_id: int | None = None
    include_archived: bool = False
    tier_number: int | None = None
    role_snapshots: tuple[TeamLeaderboardRoleSnapshot, ...] = ()
    graph_attachment_name: str = ''
    load_all_filters: bool = False
    team_enabled: bool = True
    channel_allowed: bool = True
    # Discord adapters set this explicitly; DB-only seams can read rows
    # without a live role snapshot, such as the gated schema test.
    require_role_match: bool = False


@dataclass(frozen=True)
class TeamLeaderboardRow:
    """One fully primitive team row and its current-reset ELO history."""

    rank: int
    team_id: int
    team_name: str
    team_emoji: str
    tier_number: int
    tier_name: str
    is_archived: bool
    member_count: int
    role_color: str
    elo: int
    wins: int
    losses: int
    history: tuple[tuple[object, int], ...]


@dataclass(frozen=True)
class TeamLeaderboardResult:
    """Immutable read snapshot; native refinements filter these rows in memory."""

    total_teams: int
    rows: tuple[TeamLeaderboardRow, ...]
    graph_attachment_name: str
    loaded_all_filters: bool


@dataclass(frozen=True)
class TeamLeaderboardPage:
    """One deterministic page materialized from a team snapshot."""

    title: str
    total_teams: int
    tier_number: int | None
    include_archived: bool
    page_index: int
    page_count: int
    start_rank: int
    end_rank: int
    rows: tuple[TeamLeaderboardRow, ...]


@dataclass(frozen=True)
class TeamLeaderboardGraph:
    """Immutable in-memory graph bytes ready for a Discord attachment."""

    filename: str
    png_bytes: bytes


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
                    discord_id=int(
                        player.discord_id
                        if global_scope
                        else player.discord_member.discord_id
                    ),
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


class TeamLeaderboardValidationError(ValueError):
    """The captured team-leaderboard request is invalid."""


class TeamLeaderboardPermissionError(TeamLeaderboardValidationError):
    """The captured team-leaderboard policy does not permit the read."""


def _configured_team_tiers() -> tuple[int, ...]:
    return tuple(
        int(number)
        for number, _name in tuple(
            getattr(settings, 'league_tiers', ()) or ()
        )
    )


def _team_tier_name(tier_number: int) -> str:
    try:
        return str(settings.tier_lookup(int(tier_number))[1])
    except Exception:
        return str(tier_number)


def _team_history_rows(team_id: int) -> tuple[tuple[object, int], ...]:
    """Load current-reset ELO history as primitive values on the worker."""

    elo_field = models.GameSide.team_elo_after_game
    query = (
        models.GameSide
        .select(
            models.Game.completed_ts,
            elo_field.alias('elo'),
        )
        .join(models.Game)
        .where(
            (models.GameSide.team_id == int(team_id))
            & elo_field.is_null(False)
        )
        .order_by(models.Game.completed_ts, models.Game.id)
    )
    if hasattr(query, 'dicts'):
        return tuple(
            (
                row.get('completed_ts'),
                int(row.get('elo')),
            )
            for row in query.dicts()
            if row.get('elo') is not None
        )
    return tuple(
        (
            getattr(row, 'completed_ts', None),
            int(getattr(row, 'elo', 0)),
        )
        for row in query
        if getattr(row, 'elo', None) is not None
    )


def _validate_team_leaderboard_request(
    request: TeamLeaderboardRequest,
) -> tuple[int, int]:
    if int(request.guild_id) <= 0:
        raise TeamLeaderboardValidationError(
            'guild_id must be a positive integer.'
        )
    database_guild_id = int(
        request.database_guild_id
        if request.database_guild_id is not None
        else request.guild_id
    )
    if database_guild_id <= 0:
        raise TeamLeaderboardValidationError(
            'database_guild_id must be a positive integer.'
        )
    if request.tier_number is not None and int(request.tier_number) <= 0:
        raise TeamLeaderboardValidationError(
            'tier_number must be positive when supplied.'
        )
    configured_tiers = _configured_team_tiers()
    if (
        request.tier_number is not None
        and configured_tiers
        and int(request.tier_number) not in configured_tiers
    ):
        raise TeamLeaderboardValidationError(
            f'Unknown configured team tier {request.tier_number}.'
        )
    if not bool(request.team_enabled):
        raise TeamLeaderboardPermissionError(
            'Teams are not enabled on this server.'
        )
    if not bool(request.channel_allowed):
        raise TeamLeaderboardPermissionError(
            'This command can only be used in a designated bot spam channel.'
        )
    return int(request.guild_id), database_guild_id


def load_team_leaderboard(
    request: TeamLeaderboardRequest,
) -> TeamLeaderboardResult:
    """Load a complete team snapshot with one worker-local connection.

    ``load_all_filters`` is used by the native workspace. It intentionally
    loads every visible configured-tier team, including archived teams, so
    tier/population changes can only filter the immutable snapshot.
    """

    _, database_guild_id = _validate_team_leaderboard_request(request)
    role_by_name = {
        snapshot.role_name: snapshot
        for snapshot in request.role_snapshots
    }

    conditions = [
        (models.Team.is_hidden == 0),
        (models.Team.guild_id == database_guild_id),
        models.Team.league_tier.is_null(False),
    ]
    configured_tiers = _configured_team_tiers()
    if configured_tiers:
        conditions.append(models.Team.league_tier.in_(configured_tiers))
    if not request.load_all_filters:
        if not request.include_archived:
            conditions.append(models.Team.is_archived == 0)
        if request.tier_number is not None:
            conditions.append(
                models.Team.league_tier == int(request.tier_number)
            )

    rows = []
    with models.db.connection_context():
        query = (
            models.Team
            .select()
            .where(*conditions)
            .order_by(-models.Team.elo, models.Team.id)
        )
        for team in query:
            tier_number = int(team.league_tier)
            role_snapshot = role_by_name.get(str(team.name))
            if role_snapshot is None:
                if request.require_role_match:
                    logger.warning(
                        'Omitting team %s (id=%s) from team leaderboard: '
                        'no exact Discord role match.',
                        team.name,
                        team.id,
                    )
                    continue
                member_count = 0
                role_color = '#5865F2'
            else:
                member_count = int(role_snapshot.active_member_count)
                role_color = str(role_snapshot.role_color or '#5865F2')
            wins, losses = team.get_record(alltime=False)
            rows.append(
                TeamLeaderboardRow(
                    rank=len(rows) + 1,
                    team_id=int(team.id),
                    team_name=str(team.name),
                    team_emoji=str(getattr(team, 'emoji', '') or ''),
                    tier_number=tier_number,
                    tier_name=_team_tier_name(tier_number),
                    is_archived=bool(team.is_archived),
                    member_count=member_count,
                    role_color=role_color,
                    elo=int(team.elo),
                    wins=int(wins),
                    losses=int(losses),
                    history=_team_history_rows(int(team.id)),
                )
            )

    return TeamLeaderboardResult(
        total_teams=len(rows),
        rows=tuple(rows),
        graph_attachment_name=str(request.graph_attachment_name),
        loaded_all_filters=bool(request.load_all_filters),
    )


def _team_leaderboard_title(
    rows: tuple[TeamLeaderboardRow, ...],
    *,
    tier_number: int | None,
    include_archived: bool,
) -> str:
    title = 'Team Leaderboard'
    if tier_number is not None:
        tier_name = next(
            (
                row.tier_name for row in rows
                if row.tier_number == int(tier_number)
            ),
            _team_tier_name(int(tier_number)),
        )
        title += f' — {tier_name} Tier'
    if include_archived:
        title += ' — Including Archived Teams'
    return title


def team_leaderboard_page(
    result: TeamLeaderboardResult,
    *,
    tier_number: int | None = None,
    include_archived: bool = False,
    page_index: int = 0,
    page_size: int = TEAM_LEADERBOARD_PAGE_SIZE,
) -> TeamLeaderboardPage:
    """Filter/rank/page a loaded snapshot without database access."""

    matching = tuple(
        row for row in result.rows
        if (include_archived or not row.is_archived)
        and (
            tier_number is None
            or row.tier_number == int(tier_number)
        )
    )
    ranked_rows = tuple(
        replace(row, rank=index)
        for index, row in enumerate(matching, start=1)
    )
    page_rows, page_count, start, end = leaderboard_page_rows(
        ranked_rows,
        page_index,
        page_size,
    )
    return TeamLeaderboardPage(
        title=_team_leaderboard_title(
            ranked_rows,
            tier_number=tier_number,
            include_archived=include_archived,
        ),
        total_teams=len(ranked_rows),
        tier_number=(int(tier_number) if tier_number is not None else None),
        include_archived=bool(include_archived),
        page_index=page_index,
        page_count=page_count,
        start_rank=page_rows[0].rank if page_rows else 0,
        end_rank=page_rows[-1].rank if page_rows else 0,
        rows=tuple(page_rows),
    )


def render_team_leaderboard_graph(
    page: TeamLeaderboardPage,
    filename: str,
) -> TeamLeaderboardGraph:
    """Render at most one selected page with an owned Agg Figure/canvas."""

    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    figure = Figure(figsize=(12, 8))
    canvas = FigureCanvasAgg(figure)
    axis = figure.add_subplot()
    try:
        figure.suptitle('Team ELO History', fontsize=16)
        plotted = False
        for row in page.rows[:TEAM_GRAPH_SERIES_LIMIT]:
            history = row.history
            if len(history) > TEAM_GRAPH_HISTORY_POINT_LIMIT:
                last_index = len(history) - 1
                history = tuple(
                    history[
                        round(
                            index * last_index
                            / (TEAM_GRAPH_HISTORY_POINT_LIMIT - 1)
                        )
                    ]
                    for index in range(TEAM_GRAPH_HISTORY_POINT_LIMIT)
                )
            if not history:
                continue
            dates = [point[0] for point in history]
            elos = [point[1] for point in history]
            axis.plot(
                dates,
                elos,
                'o-',
                markersize=3,
                linewidth=1.5,
                label=row.team_name,
                color=row.role_color or '#5865F2',
            )
            plotted = True
        axis.yaxis.grid()
        axis.spines['top'].set_visible(False)
        axis.spines['right'].set_visible(False)
        axis.spines['left'].set_visible(False)
        if plotted:
            axis.legend(loc='best')
            figure.autofmt_xdate()
        output = BytesIO()
        canvas.print_png(output)
        return TeamLeaderboardGraph(
            filename=str(filename),
            png_bytes=bytes(output.getvalue()),
        )
    finally:
        figure.clear()


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


async def _run_bounded_leaderboard_call(call):
    """Run a leaderboard read/render and drain it safely on cancellation."""

    concurrent_future = _leaderboard_read_executor.submit(call)
    try:
        while not concurrent_future.done():
            await asyncio.sleep(0.001)
    except asyncio.CancelledError:
        task = asyncio.current_task()
        while not concurrent_future.done():
            if task is not None:
                while task.cancelling():
                    task.uncancel()
            try:
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                continue
        try:
            concurrent_future.result()
        except BaseException:
            logger.exception(
                'Cancelled leaderboard worker completed with an error'
            )
        raise asyncio.CancelledError
    return concurrent_future.result()


async def run_team_leaderboard(
    request: TeamLeaderboardRequest,
) -> TeamLeaderboardResult:
    """Submit a team snapshot read to the bounded leaderboard executor."""

    return await _run_bounded_leaderboard_call(
        functools.partial(load_team_leaderboard, request)
    )


async def run_team_leaderboard_graph(
    page: TeamLeaderboardPage,
    filename: str,
) -> TeamLeaderboardGraph:
    """Render a page graph off-loop without opening a database connection."""

    return await _run_bounded_leaderboard_call(
        functools.partial(render_team_leaderboard_graph, page, filename)
    )


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
