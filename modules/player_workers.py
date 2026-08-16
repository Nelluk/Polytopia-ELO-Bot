"""Bounded worker-local reads for player profile workspaces."""

from __future__ import annotations

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import datetime
from io import BytesIO
import logging
import uuid

from modules import models
from modules import player_timezone_values
import settings


MAX_GAMES = 500
MAX_HISTORY_POINTS = 500
_player_read_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix='polybot-player-read',
)
logger = logging.getLogger(__name__)


class PlayerNotFound(ValueError):
    pass


class AmbiguousPlayer(ValueError):
    pass


def _profile_badges(player, guild_id: int) -> tuple[str, ...]:
    """Return only one valid PolyChampions ordered set for presentation."""

    if int(guild_id) != int(settings.server_ids['polychampions']):
        return ()
    values = player.badges
    if not isinstance(values, (list, tuple)) or len(values) > 100:
        logger.warning('Player %s has a malformed badge array', player.id)
        return ()
    result = []
    seen = set()
    for value in values:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 200
            or any(character in '\r\n' for character in value)
            or value.casefold() in seen
        ):
            logger.warning('Player %s has a malformed stored badge', player.id)
            return ()
        seen.add(value.casefold())
        result.append(value)
    return tuple(result)


@dataclass(frozen=True)
class PlayerWorkspaceRequest:
    guild_id: int
    discord_id: int | None = None
    player_query: str | None = None
    requester_discord_id: int | None = None


@dataclass(frozen=True)
class PlayerGameRow:
    game_id: int
    name: str
    date: str
    status: str
    outcome: str
    ranked: bool
    season: int | None
    roster: str


@dataclass(frozen=True)
class PlayerRatingPoint:
    completed_at: datetime.datetime
    game_id: int
    current_elo: int | None
    all_time_elo: int | None


@dataclass(frozen=True)
class PlayerHeadToHead:
    requester_discord_id: int
    requester_name: str
    requester_wins: int
    target_discord_id: int
    target_name: str
    target_wins: int

    @property
    def total_games(self) -> int:
        return self.requester_wins + self.target_wins


@dataclass(frozen=True)
class PlayerHistoryGraph:
    filename: str
    png_bytes: bytes


@dataclass(frozen=True)
class PlayerWorkspaceSnapshot:
    player_id: int
    discord_id: int
    display_name: str
    polytopia_name: str | None
    team_name: str
    team_emoji: str
    squad_names: tuple[str, ...]
    timezone: str
    local_elo: int
    local_peak: int
    global_elo: int
    global_peak: int
    local_all_time: int
    local_all_time_peak: int
    global_all_time: int
    global_all_time_peak: int
    local_wins: int
    local_losses: int
    global_wins: int
    global_losses: int
    local_all_time_wins: int
    local_all_time_losses: int
    global_all_time_wins: int
    global_all_time_losses: int
    local_rank: int | None
    local_ranked_count: int
    global_rank: int | None
    global_ranked_count: int
    games: tuple[PlayerGameRow, ...]
    badges: tuple[str, ...] = ()
    guild_display_name: str = 'This server'
    local_history: tuple[PlayerRatingPoint, ...] = ()
    global_history: tuple[PlayerRatingPoint, ...] = ()
    history_truncated: bool = False
    head_to_head: PlayerHeadToHead | None = None


def _resolve_player(request: PlayerWorkspaceRequest):
    if request.guild_id <= 0:
        raise ValueError('guild_id must be a positive integer.')
    if request.discord_id is not None:
        matches = (
            models.Player.select()
            .join(models.DiscordMember)
            .where(
                (models.Player.guild_id == request.guild_id)
                & (models.DiscordMember.discord_id == request.discord_id)
            )
        )
        try:
            return matches.get()
        except models.Player.DoesNotExist as exc:
            raise PlayerNotFound('That member is not registered here.') from exc
    query = (request.player_query or '').strip()
    if not query:
        raise PlayerNotFound('No player was supplied.')
    matches = models.Player.string_matches(
        player_string=query,
        guild_id=request.guild_id,
    )
    if not matches:
        raise PlayerNotFound(f'Could not find a player matching “{query}”.')
    if len(matches) > 1:
        raise AmbiguousPlayer(
            f'More than one player matches “{query}”. Use an @mention.'
        )
    return matches[0]


def _rating_history(
    player,
    *,
    global_scope: bool,
) -> tuple[tuple[PlayerRatingPoint, ...], bool]:
    """Load one bounded ordered ELO series as primitive values."""

    if global_scope:
        current_field = models.Lineup.elo_after_game_global_moonrise
        all_time_field = models.Lineup.elo_after_game_global_alltime
        participant_filter = (
            models.Player.discord_member_id == player.discord_member_id
        )
    else:
        current_field = models.Lineup.elo_after_game_moonrise
        all_time_field = models.Lineup.elo_after_game_alltime
        participant_filter = models.Lineup.player_id == player.id

    query = (
        models.Lineup
        .select(
            models.Game.completed_ts.alias('completed_at'),
            models.Game.id.alias('game_id'),
            current_field.alias('current_elo'),
            all_time_field.alias('all_time_elo'),
        )
        .join(models.Game)
    )
    if global_scope:
        query = query.join_from(models.Lineup, models.Player)
    query = (
        query
        .where(
            participant_filter
            & (models.Game.is_completed == 1)
            & (models.Game.is_confirmed == 1)
            & (models.Game.is_ranked == 1)
            & (models.Game.completed_ts.is_null(False))
            & (
                current_field.is_null(False)
                | all_time_field.is_null(False)
            )
        )
        .order_by(-models.Game.completed_ts, -models.Game.id)
        .limit(MAX_HISTORY_POINTS + 1)
    )
    newest_first = list(query.dicts())
    truncated = len(newest_first) > MAX_HISTORY_POINTS
    newest_first = newest_first[:MAX_HISTORY_POINTS]
    points = []
    for row in reversed(newest_first):
        current = row['current_elo']
        points.append(PlayerRatingPoint(
            completed_at=row['completed_at'],
            game_id=int(row['game_id']),
            current_elo=int(current) if current is not None else None,
            all_time_elo=(
                int(row['all_time_elo'])
                if row['all_time_elo'] is not None
                else None
            ),
        ))
    return tuple(points), truncated


def _head_to_head(
    player,
    request: PlayerWorkspaceRequest,
) -> PlayerHeadToHead | None:
    requester_discord_id = request.requester_discord_id
    if (
        requester_discord_id is None
        or int(requester_discord_id) == int(player.discord_member.discord_id)
    ):
        return None
    try:
        requester = (
            models.Player
            .select()
            .join(models.DiscordMember)
            .where(
                (models.Player.guild_id == request.guild_id)
                & (
                    models.DiscordMember.discord_id
                    == int(requester_discord_id)
                )
            )
            .get()
        )
    except models.Player.DoesNotExist:
        return None

    player_ids = (int(player.id), int(requester.id))
    matching_games = (
        models.Lineup
        .select(models.Lineup.game)
        .join(models.Game)
        .where(
            models.Lineup.player.in_(player_ids)
            & (models.Game.guild_id == request.guild_id)
            & (models.Game.size == [1, 1])
            & (models.Game.is_completed == 1)
            & (models.Game.is_confirmed == 1)
            & (models.Game.is_ranked == 1)
        )
        .group_by(models.Lineup.game)
        .having(
            models.fn.COUNT(models.fn.DISTINCT(models.Lineup.player)) == 2
        )
    )
    wins = {
        int(row['player']): int(row['wins'])
        for row in (
            models.Lineup
            .select(
                models.Lineup.player,
                models.fn.COUNT(models.Lineup.id).alias('wins'),
            )
            .join(models.Game)
            .where(
                models.Lineup.game.in_(matching_games)
                & models.Lineup.player.in_(player_ids)
                & (models.Lineup.gameside == models.Game.winner)
            )
            .group_by(models.Lineup.player)
            .dicts()
        )
    }
    return PlayerHeadToHead(
        requester_discord_id=int(requester_discord_id),
        requester_name=str(requester.name),
        requester_wins=wins.get(int(requester.id), 0),
        target_discord_id=int(player.discord_member.discord_id),
        target_name=str(player.name),
        target_wins=wins.get(int(player.id), 0),
    )


def load_player_workspace(
    request: PlayerWorkspaceRequest,
) -> PlayerWorkspaceSnapshot:
    """Load one complete immutable workspace snapshot."""

    with models.db.connection_context():
        player = _resolve_player(request)
        member = player.discord_member
        local_wins, local_losses = player.get_record()
        global_wins, global_losses = member.get_record()
        local_all_time_wins, local_all_time_losses = player.get_record(
            version='alltime',
        )
        global_all_time_wins, global_all_time_losses = member.get_record(
            version='alltime',
        )
        local_rank, local_count = player.leaderboard_rank(settings.date_cutoff)
        global_rank, global_count = member.leaderboard_rank(
            settings.date_cutoff
        )

        games = list(
            models.Game.search(
                player_filter=[player],
                guild_id=request.guild_id,
            )[:MAX_GAMES]
        )
        rows = []
        squad_names = set()
        for game in games:
            _, side = game.has_player(discord_id=member.discord_id)
            if side and side.squad:
                squad_names.add(side.squad.name or f'Squad #{side.squad.id}')
            if game.is_pending:
                status = 'Open'
            elif not game.is_completed:
                status = 'Incomplete'
            elif game.is_confirmed:
                status = 'Completed'
            else:
                status = 'Unconfirmed'
            if game.is_completed and game.is_confirmed and side:
                outcome = 'Win' if game.winner_id == side.id else 'Loss'
            else:
                outcome = '—'
            rows.append(PlayerGameRow(
                game_id=int(game.id),
                name=str(game.name or f'Game {game.id}'),
                date=str(game.date),
                status=status,
                outcome=outcome,
                ranked=bool(game.is_ranked),
                season=(
                    int(game.league_season)
                    if game.league_season is not None
                    else None
                ),
                roster=str(game.get_gamesides_string()),
            ))

        timezone = player_timezone_values.effective_timezone_offset(member) or ''
        local_history, local_truncated = _rating_history(
            player,
            global_scope=False,
        )
        global_history, global_truncated = _rating_history(
            player,
            global_scope=True,
        )
        head_to_head = _head_to_head(player, request)
        try:
            guild_display_name = str(settings.guild_setting(
                request.guild_id,
                'display_name',
            ))
        except Exception:
            guild_display_name = 'This server'
        return PlayerWorkspaceSnapshot(
            player_id=int(player.id),
            discord_id=int(member.discord_id),
            display_name=str(player.name),
            # Player.name is the guild display label, not a registered
            # account-wide Polytopia name. Keep an unset canonical value
            # explicit for the native workspace.
            polytopia_name=(
                str(member.polytopia_name)
                if member.polytopia_name
                else None
            ),
            team_name=str(player.team.name) if player.team else '',
            team_emoji=str(player.team.emoji or '') if player.team else '',
            squad_names=tuple(sorted(squad_names)),
            timezone=timezone,
            local_elo=int(player.elo_moonrise),
            local_peak=int(player.elo_max_moonrise),
            global_elo=int(member.elo_moonrise),
            global_peak=int(member.elo_max_moonrise),
            local_all_time=int(player.elo_alltime),
            local_all_time_peak=int(player.elo_max_alltime),
            global_all_time=int(member.elo_alltime),
            global_all_time_peak=int(member.elo_max_alltime),
            local_wins=int(local_wins),
            local_losses=int(local_losses),
            global_wins=int(global_wins),
            global_losses=int(global_losses),
            local_all_time_wins=int(local_all_time_wins),
            local_all_time_losses=int(local_all_time_losses),
            global_all_time_wins=int(global_all_time_wins),
            global_all_time_losses=int(global_all_time_losses),
            local_rank=int(local_rank) if local_rank is not None else None,
            local_ranked_count=int(local_count),
            global_rank=int(global_rank) if global_rank is not None else None,
            global_ranked_count=int(global_count),
            games=tuple(rows),
            badges=_profile_badges(player, request.guild_id),
            guild_display_name=guild_display_name,
            local_history=local_history,
            global_history=global_history,
            history_truncated=local_truncated or global_truncated,
            head_to_head=head_to_head,
        )


def render_player_history_graph(
    snapshot: PlayerWorkspaceSnapshot,
    era: str,
) -> PlayerHistoryGraph:
    """Render immutable history points with an object-owned Agg canvas."""

    if era not in {'current', 'all_time'}:
        raise ValueError('History era must be current or all_time.')
    value_name = 'current_elo' if era == 'current' else 'all_time_elo'
    series = (
        (snapshot.guild_display_name, snapshot.local_history, '#5865F2'),
        ('Global', snapshot.global_history, '#57F287'),
    )
    if not any(
        any(getattr(point, value_name) is not None for point in points)
        for _label, points, _colour in series
    ):
        return PlayerHistoryGraph(filename='', png_bytes=b'')

    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    figure = Figure(figsize=(10, 6))
    canvas = FigureCanvasAgg(figure)
    axis = figure.add_subplot()
    try:
        title_era = 'Current-reset' if era == 'current' else 'All-time'
        figure.suptitle(f'{title_era} ELO history', fontsize=16)
        for label, points, colour in series:
            filtered = tuple(
                point for point in points
                if getattr(point, value_name) is not None
            )
            if not filtered:
                continue
            axis.plot(
                [point.completed_at for point in filtered],
                [getattr(point, value_name) for point in filtered],
                'o-',
                markersize=3,
                linewidth=1.5,
                label=label,
                color=colour,
            )
        axis.yaxis.grid()
        axis.spines['top'].set_visible(False)
        axis.spines['right'].set_visible(False)
        axis.spines['left'].set_visible(False)
        axis.legend(loc='best')
        figure.autofmt_xdate()
        output = BytesIO()
        canvas.print_png(output)
        return PlayerHistoryGraph(
            filename=(
                f'player-{snapshot.player_id}-{era}-'
                f'{uuid.uuid4().hex}.png'
            ),
            png_bytes=bytes(output.getvalue()),
        )
    finally:
        figure.clear()


async def _run_bounded_player_call(call):
    concurrent_future = _player_read_executor.submit(call)
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
            logger.exception('Cancelled player worker completed with an error')
        raise asyncio.CancelledError
    return concurrent_future.result()


async def run_player_workspace(
    request: PlayerWorkspaceRequest,
) -> PlayerWorkspaceSnapshot:
    return await _run_bounded_player_call(
        functools.partial(load_player_workspace, request)
    )


async def run_player_history_graph(
    snapshot: PlayerWorkspaceSnapshot,
    era: str,
) -> PlayerHistoryGraph:
    """Render one cached player-history view without another database read."""

    return await _run_bounded_player_call(
        functools.partial(render_player_history_graph, snapshot, era)
    )
