"""Bounded read workers for the native and legacy team-show card."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import datetime
import functools
from io import BytesIO
import logging

from peewee import fn

from modules import exceptions, image_storage, models, utilities
import settings


logger = logging.getLogger('polybot.' + __name__)

TEAM_ACTIVITY_RECENT = 'recent-30-days'
TEAM_ACTIVITY_COMPLETED = 'all-completed'
TEAM_ACTIVITY_VALUES = frozenset({
    TEAM_ACTIVITY_RECENT,
    TEAM_ACTIVITY_COMPLETED,
})


class TeamShowValidationError(RuntimeError):
    """The request contains an invalid or contradictory value."""


class TeamShowLookupError(TeamShowValidationError):
    """The requested or inferred team cannot be resolved unambiguously."""


class TeamShowPermissionError(TeamShowValidationError):
    """The captured guild/requester policy does not permit the read."""


@dataclass(frozen=True)
class TeamShowRoleSnapshot:
    """The primitive membership portion of one exact Discord role."""

    role_id: int | None
    role_name: str
    member_ids: tuple[int, ...]


@dataclass(frozen=True)
class TeamShowMemberSnapshot:
    """Stable display values captured before worker submission."""

    discord_id: int
    name: str
    display_name: str
    mention: str


@dataclass(frozen=True)
class TeamShowGuildSnapshot:
    """All Discord data needed by the worker, represented as primitives."""

    guild_id: int
    roles: tuple[TeamShowRoleSnapshot, ...]
    members: tuple[TeamShowMemberSnapshot, ...]


@dataclass(frozen=True)
class TeamShowRequest:
    """Immutable worker input; no live Discord or Peewee object crosses over."""

    guild_id: int
    requester_id: int
    team_lookup: str | None
    activity_mode: str
    team_enabled: bool
    channel_allowed: bool
    leadership_enabled: bool
    inactive_role_name: str | None
    guild_snapshot: TeamShowGuildSnapshot
    team_elo_reset_label: str
    requester_description: str = ''
    native: bool = True
    invoked_with: str = '/team show'
    prefix: str = '$'


@dataclass(frozen=True)
class TeamShowRosterRow:
    """One active exact-team-role member and both cached activity metrics."""

    discord_id: int
    name: str
    elo: int | None
    rank: int | None
    recent_games: int
    completed_games: int
    registered: bool


@dataclass(frozen=True)
class _TeamShowLoadedData:
    """Worker-local data before the off-loop graph renderer runs."""

    guild_id: int
    requester_id: int
    team_id: int
    team_name: str
    team_emoji: str
    house_name: str | None
    house_emoji: str | None
    league_tier: int | None
    tier_name: str | None
    external_server: int | None
    elo: int
    wins: int
    losses: int
    roster_rows: tuple[TeamShowRosterRow, ...]
    team_role_found: bool
    missing_role_name: str | None
    leaders: tuple[str, ...]
    coleaders: tuple[str, ...]
    recruiters: tuple[str, ...]
    captains: tuple[str, ...]
    recent_games: tuple[tuple[str, str], ...]
    alltime_history: tuple[tuple[object, object], ...]
    current_history: tuple[tuple[object, object], ...]
    local_image_bytes: bytes | None
    image_url: str | None
    activity_mode: str
    team_elo_reset_label: str


@dataclass(frozen=True)
class TeamShowResult:
    """Frozen result safe to hand back to Discord presentation code."""

    guild_id: int
    requester_id: int
    team_id: int
    team_name: str
    team_emoji: str
    house_name: str | None
    house_emoji: str | None
    league_tier: int | None
    tier_name: str | None
    external_server: int | None
    elo: int
    wins: int
    losses: int
    roster_rows: tuple[TeamShowRosterRow, ...]
    team_role_found: bool
    missing_role_name: str | None
    leaders: tuple[str, ...]
    coleaders: tuple[str, ...]
    recruiters: tuple[str, ...]
    captains: tuple[str, ...]
    recent_games: tuple[tuple[str, str], ...]
    graph_bytes: bytes
    local_image_bytes: bytes | None
    image_url: str | None
    activity_mode: str
    team_elo_reset_label: str


def _normalise_lookup(value: str | None) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _validate_request(request: TeamShowRequest) -> None:
    if int(request.guild_id) != int(request.guild_snapshot.guild_id):
        raise TeamShowValidationError('The captured guild does not match the request.')
    if request.activity_mode not in TEAM_ACTIVITY_VALUES:
        raise TeamShowValidationError('The team activity view is invalid.')
    if not bool(request.team_enabled):
        raise TeamShowPermissionError('Teams are not enabled on this server.')
    if not bool(request.channel_allowed):
        raise TeamShowPermissionError(
            'This command can only be used in a designated ELO bot channel.'
        )


def _role_by_exact_name(
    snapshot: TeamShowGuildSnapshot,
    role_name: str | None,
) -> TeamShowRoleSnapshot | None:
    if not role_name:
        return None
    return next(
        (
            role for role in snapshot.roles
            if role.role_name == str(role_name)
        ),
        None,
    )


def _member_by_id(
    snapshot: TeamShowGuildSnapshot,
) -> dict[int, TeamShowMemberSnapshot]:
    return {
        int(member.discord_id): member
        for member in snapshot.members
    }


def _member_mention(
    members: dict[int, TeamShowMemberSnapshot],
    member_id: int,
) -> str:
    member = members.get(int(member_id))
    return str(member.mention) if member is not None else f'<@{int(member_id)}>'


def _role_leadership(
    request: TeamShowRequest,
    *,
    team_name: str,
    house_name: str | None,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Reproduce the legacy exact-role leadership convention from snapshots."""

    if not request.leadership_enabled:
        return (), (), (), ()

    team_role = _role_by_exact_name(request.guild_snapshot, team_name)
    if team_role is None:
        return (), (), (), ()
    house_role = _role_by_exact_name(request.guild_snapshot, house_name)
    leader_role = _role_by_exact_name(
        request.guild_snapshot,
        'House Leader',
    )
    coleader_role = _role_by_exact_name(
        request.guild_snapshot,
        'House Co-Leader',
    )
    recruiter_role = _role_by_exact_name(
        request.guild_snapshot,
        'House Recruiter',
    )
    captain_role = _role_by_exact_name(
        request.guild_snapshot,
        'Team Captain',
    )
    members = _member_by_id(request.guild_snapshot)

    house_member_ids = (
        tuple(house_role.member_ids) if house_role is not None else ()
    )
    leader_ids = set(leader_role.member_ids) if leader_role else set()
    coleader_ids = set(coleader_role.member_ids) if coleader_role else set()
    recruiter_ids = set(recruiter_role.member_ids) if recruiter_role else set()
    captain_ids = set(captain_role.member_ids) if captain_role else set()

    return (
        tuple(
            _member_mention(members, member_id)
            for member_id in house_member_ids
            if member_id in leader_ids
        ),
        tuple(
            _member_mention(members, member_id)
            for member_id in house_member_ids
            if member_id in coleader_ids
        ),
        tuple(
            _member_mention(members, member_id)
            for member_id in house_member_ids
            if member_id in recruiter_ids
        ),
        tuple(
            _member_mention(members, member_id)
            for member_id in team_role.member_ids
            if member_id in captain_ids
        ),
    )


def _resolve_team(request: TeamShowRequest):
    lookup = _normalise_lookup(request.team_lookup)
    if lookup is not None:
        try:
            matches = models.Team.get_by_name(
                team_name=lookup,
                guild_id=int(request.guild_id),
                include_hidden=False,
            )
        except TypeError:
            matches = models.Team.get_by_name(
                lookup,
                int(request.guild_id),
                False,
                False,
            )
        matches = tuple(matches)
        if not matches:
            raise TeamShowLookupError(
                f'No matching team was found for "{lookup}".'
            )
        if len(matches) > 1:
            raise TeamShowLookupError(
                f'More than one matching team was found for "{lookup}".'
            )
        return matches[0]

    player_model = getattr(models, 'Player', None)
    if player_model is None or not hasattr(player_model, 'select'):
        raise TeamShowLookupError(
            'Your team could not be inferred. Provide a team name.'
        )
    query = (
        models.Team.select()
        .join(player_model)
        .join(models.DiscordMember)
        .where(
            (player_model.guild_id == int(request.guild_id))
            & (models.DiscordMember.discord_id == int(request.requester_id))
            & player_model.team.is_null(False)
        )
        .distinct()
    )
    matches = tuple(query)
    if not matches:
        raise TeamShowLookupError(
            'Your team could not be inferred. Provide a team name.'
        )
    if len(matches) > 1:
        raise TeamShowLookupError(
            'Your team is ambiguous. Provide a team name.'
        )
    return matches[0]


def _query_count_by_player(query) -> dict[int, int]:
    counts = {}
    for player_id, count in query.tuples():
        player_id = getattr(player_id, 'id', player_id)
        counts[int(player_id)] = int(count or 0)
    return counts


def _roster_metric_counts(
    player_ids: tuple[int, ...],
    *,
    completed: bool,
) -> dict[int, int]:
    if not player_ids:
        return {}
    conditions = [models.Lineup.player.in_(player_ids)]
    if completed:
        conditions.extend((
            models.Game.is_completed == 1,
            models.Game.is_ranked == 1,
        ))
    else:
        cutoff = datetime.datetime.now() - datetime.timedelta(days=30)
        conditions.append(
            (models.Game.date > cutoff) | (models.Game.completed_ts > cutoff)
        )
    query = (
        models.Lineup
        .select(
            models.Lineup.player,
            fn.COUNT(models.Lineup.id).alias('metric_count'),
        )
        .join(models.Game)
        .where(*conditions)
        .group_by(models.Lineup.player)
    )
    return _query_count_by_player(query)


def _load_player_rows(
    guild_id: int,
    member_ids: tuple[int, ...],
):
    """Load the entire active roster in one guild-scoped player query."""

    if not member_ids:
        return ()
    player_query = (
        models.Player
        .select(models.Player, models.DiscordMember)
        .join(models.DiscordMember)
        .where(
            (models.Player.guild_id == int(guild_id))
            & models.DiscordMember.discord_id.in_(member_ids)
        )
    )
    return tuple(player_query)


def _rank_by_player(
    guild_id: int,
) -> dict[int, int]:
    query = models.Player.leaderboard(
        date_cutoff=settings.date_cutoff,
        guild_id=int(guild_id),
    )
    return {
        int(getattr(player, 'id')): index
        for index, player in enumerate(query, start=1)
    }


def _history_rows(team_id: int, elo_field) -> tuple[tuple[object, object], ...]:
    query = (
        models.GameSide
        .select(
            models.Game.completed_ts.alias('completed_ts'),
            elo_field.alias('elo'),
        )
        .join(models.Game)
        .where(
            (models.GameSide.team == int(team_id))
            & elo_field.is_null(False)
        )
        .order_by(models.Game.completed_ts)
    )
    if hasattr(query, 'dicts'):
        return tuple(
            (row.get('completed_ts'), row.get('elo'))
            for row in query.dicts()
        )
    return tuple(
        (
            getattr(row, 'completed_ts', None),
            getattr(row, 'elo', getattr(row, elo_field.name, None)),
        )
        for row in query
    )


def _tier_name(tier: int | None) -> str | None:
    if tier is None:
        return None
    try:
        return str(settings.tier_lookup(int(tier))[1])
    except (
        AttributeError,
        TypeError,
        ValueError,
        exceptions.NoMatches,
    ):
        return None


def _load_team_show_data(request: TeamShowRequest) -> _TeamShowLoadedData:
    """Load all DB/filesystem state synchronously on a worker-owned connection."""

    _validate_request(request)
    with models.db.connection_context():
        team = _resolve_team(request)
        team_id = int(team.id)
        team_name = str(team.name)
        house = getattr(team, 'house', None)
        house_name = (
            str(getattr(house, 'name'))
            if house is not None and getattr(house, 'name', None)
            else None
        )
        house_emoji = (
            str(getattr(house, 'emoji'))
            if house is not None and getattr(house, 'emoji', None)
            else None
        )
        league_tier = (
            int(team.league_tier)
            if getattr(team, 'league_tier', None) is not None
            else None
        )
        external_server = (
            int(team.external_server)
            if getattr(team, 'external_server', None) is not None
            else None
        )

        team_role = _role_by_exact_name(request.guild_snapshot, team_name)
        member_lookup = _member_by_id(request.guild_snapshot)
        missing_role_name = None if team_role is not None else team_name
        active_member_ids = ()
        if team_role is not None:
            inactive_role = _role_by_exact_name(
                request.guild_snapshot,
                request.inactive_role_name,
            )
            inactive_ids = set(inactive_role.member_ids) if inactive_role else set()
            active_member_ids = tuple(
                member_id
                for member_id in team_role.member_ids
                if member_id not in inactive_ids
            )

        player_rows = _load_player_rows(
            request.guild_id,
            active_member_ids,
        )
        player_by_discord_id = {
            int(player.discord_member.discord_id): player
            for player in player_rows
        }
        player_ids = tuple(int(player.id) for player in player_rows)
        recent_counts = _roster_metric_counts(
            player_ids,
            completed=False,
        )
        completed_counts = _roster_metric_counts(
            player_ids,
            completed=True,
        )
        rank_by_player = _rank_by_player(request.guild_id) if player_ids else {}

        roster_rows = []
        for member_id in active_member_ids:
            member = member_lookup.get(int(member_id))
            player = player_by_discord_id.get(int(member_id))
            if player is None:
                display_name = (
                    member.display_name
                    if member is not None
                    else f'user-{int(member_id)}'
                )
                roster_rows.append(
                    TeamShowRosterRow(
                        discord_id=int(member_id),
                        name=str(display_name),
                        elo=None,
                        rank=None,
                        recent_games=0,
                        completed_games=0,
                        registered=False,
                    )
                )
                continue
            roster_rows.append(
                TeamShowRosterRow(
                    discord_id=int(member_id),
                    name=str(
                        member.display_name
                        if member is not None
                        else player.discord_member.name
                    ),
                    elo=int(player.elo_moonrise),
                    rank=rank_by_player.get(int(player.id)),
                    recent_games=recent_counts.get(int(player.id), 0),
                    completed_games=completed_counts.get(int(player.id), 0),
                    registered=True,
                )
            )

        if hasattr(team, 'get_record'):
            wins, losses = team.get_record(alltime=False)
        else:
            wins, losses = 0, 0

        recent_games = tuple(
            utilities.summarize_game_list(
                models.Game.search(team_filter=[team])[:5]
            )
        )
        alltime_history = _history_rows(
            team_id,
            models.GameSide.team_elo_after_game_alltime,
        )
        current_history = (
            _history_rows(team_id, models.GameSide.team_elo_after_game)
            if alltime_history
            else ()
        )
        local_image_bytes = image_storage.local_image_bytes('team', team_id)
        leaders, coleaders, recruiters, captains = _role_leadership(
            request,
            team_name=team_name,
            house_name=house_name,
        ) if team_role is not None else ((), (), (), ())

        return _TeamShowLoadedData(
            guild_id=int(request.guild_id),
            requester_id=int(request.requester_id),
            team_id=team_id,
            team_name=team_name,
            team_emoji=str(getattr(team, 'emoji', '') or ''),
            house_name=house_name,
            house_emoji=house_emoji,
            league_tier=league_tier,
            tier_name=_tier_name(league_tier),
            external_server=external_server,
            elo=int(getattr(team, 'elo', 0)),
            wins=int(wins),
            losses=int(losses),
            roster_rows=tuple(roster_rows),
            team_role_found=team_role is not None,
            missing_role_name=missing_role_name,
            leaders=leaders,
            coleaders=coleaders,
            recruiters=recruiters,
            captains=captains,
            recent_games=recent_games,
            alltime_history=alltime_history,
            current_history=current_history,
            local_image_bytes=(
                bytes(local_image_bytes)
                if local_image_bytes is not None
                else None
            ),
            image_url=(
                str(team.image_url)
                if getattr(team, 'image_url', None)
                else None
            ),
            activity_mode=request.activity_mode,
            team_elo_reset_label=str(request.team_elo_reset_label),
        )


def _render_graph(data: _TeamShowLoadedData) -> bytes:
    """Render the established ELO plot to owned immutable bytes."""

    if not data.alltime_history:
        return b''
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    figure = Figure()
    canvas = FigureCanvasAgg(figure)
    axis = figure.add_subplot()
    try:
        figure.suptitle(f'ELO History ({data.team_name})', fontsize=16)
        figure.autofmt_xdate()
        if data.current_history:
            axis.plot(
                [point[0] for point in data.current_history],
                [point[1] for point in data.current_history],
                'o',
                markersize=3,
                label=f'Since {data.team_elo_reset_label}',
            )
        axis.plot(
            [point[0] for point in data.alltime_history],
            [point[1] for point in data.alltime_history],
            'o',
            markersize=3,
            label='Alltime',
        )
        axis.yaxis.grid()
        axis.spines['top'].set_visible(False)
        axis.spines['right'].set_visible(False)
        axis.spines['left'].set_visible(False)
        axis.legend(loc='best')
        output = BytesIO()
        canvas.print_png(output)
        return output.getvalue()
    finally:
        figure.clear()


def _finalise_team_show(data: _TeamShowLoadedData) -> TeamShowResult:
    return TeamShowResult(
        guild_id=data.guild_id,
        requester_id=data.requester_id,
        team_id=data.team_id,
        team_name=data.team_name,
        team_emoji=data.team_emoji,
        house_name=data.house_name,
        house_emoji=data.house_emoji,
        league_tier=data.league_tier,
        tier_name=data.tier_name,
        external_server=data.external_server,
        elo=data.elo,
        wins=data.wins,
        losses=data.losses,
        roster_rows=data.roster_rows,
        team_role_found=data.team_role_found,
        missing_role_name=data.missing_role_name,
        leaders=data.leaders,
        coleaders=data.coleaders,
        recruiters=data.recruiters,
        captains=data.captains,
        recent_games=data.recent_games,
        graph_bytes=bytes(_render_graph(data)),
        local_image_bytes=data.local_image_bytes,
        image_url=data.image_url,
        activity_mode=data.activity_mode,
        team_elo_reset_label=data.team_elo_reset_label,
    )


def load_team_show(request: TeamShowRequest) -> TeamShowResult:
    """Synchronously load and render one card on a worker thread."""

    return _finalise_team_show(_load_team_show_data(request))


_team_show_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='polybot-team-show',
)


async def run_team_show(
    request: TeamShowRequest,
) -> TeamShowResult:
    """Run the blocking read/render and drain it safely on cancellation."""

    concurrent_future = _team_show_executor.submit(
        functools.partial(load_team_show, request)
    )
    try:
        # Polling at a short cooperative interval avoids a completion-callback
        # race on supported Python/runtime combinations while keeping the
        # event loop responsive during slow Peewee or Matplotlib work.
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
            logger.exception('Cancelled team-show worker completed with an error')
        raise asyncio.CancelledError
    return concurrent_future.result()
