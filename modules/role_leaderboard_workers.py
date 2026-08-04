"""Bounded, worker-local reads for the native role leaderboard."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
import datetime
import functools
import logging
from typing import Iterable

import peewee

from modules import models


logger = logging.getLogger('polybot.' + __name__)


ROLE_LEADERBOARD_PAGE_SIZE = 8
MAX_ROLE_MEMBER_SNAPSHOTS = 5000
MAX_LOADED_ROWS = 2000
MAX_SELECTED_ROLES = 5

VALID_MATCH_MODES = frozenset({'all', 'any'})
VALID_SORTS = frozenset({
    'global_elo',
    'local_elo',
    'total_games',
    'recent_games',
})
VALID_SCOPES = frozenset({'global', 'local'})


@dataclass(frozen=True)
class RoleLeaderboardRoleSnapshot:
    """Primitive current-guild role information captured on the event loop."""

    role_id: int
    name: str
    managed: bool = False
    is_default: bool = False


@dataclass(frozen=True)
class RoleLeaderboardMemberSnapshot:
    """Primitive Discord member information captured before a DB read."""

    discord_id: int
    name: str
    role_ids: tuple[int, ...]


@dataclass(frozen=True)
class RoleLeaderboardRequest:
    """Immutable role lookup input; no Discord or Peewee objects cross over."""

    guild_id: int
    selected_role_ids: tuple[int, ...]
    selected_role_names: tuple[str, ...]
    match_mode: str = 'all'
    sort_key: str = 'global_elo'
    scope: str = 'global'
    member_snapshots: tuple[RoleLeaderboardMemberSnapshot, ...] = ()
    role_snapshots: tuple[RoleLeaderboardRoleSnapshot, ...] = ()
    inactive_role_id: int | None = None
    global_guild_ids: tuple[int, ...] = ()
    recent_cutoff: datetime.datetime | None = None


@dataclass(frozen=True)
class RoleLeaderboardRow:
    """One fully primitive player row and both rating/record scopes."""

    discord_id: int
    name: str
    role_ids: tuple[int, ...]
    global_elo: int
    local_elo: int
    global_wins: int
    global_losses: int
    local_wins: int
    local_losses: int
    total_games: int
    recent_games: int
    rank: int = 0


@dataclass(frozen=True)
class RoleLeaderboardResult:
    """Immutable loaded metrics for every captured current-guild member."""

    rows: tuple[RoleLeaderboardRow, ...]
    loaded_count: int
    candidate_count: int
    truncated: bool
    inactive_role_id: int | None


@dataclass(frozen=True)
class RoleLeaderboardPage:
    """One deterministic page derived only from a loaded result."""

    title: str
    selected_role_ids: tuple[int, ...]
    selected_role_names: tuple[str, ...]
    match_mode: str
    sort_key: str
    scope: str
    total_matched: int
    loaded_count: int
    page_index: int
    page_count: int
    start_rank: int
    end_rank: int
    rows: tuple[RoleLeaderboardRow, ...]
    truncated: bool


class RoleLeaderboardValidationError(ValueError):
    """The captured role-leaderboard request or component state is invalid."""


def _normalise_sort(value: str) -> str:
    aliases = {
        'g_elo': 'global_elo',
        'global': 'global_elo',
        'elo': 'local_elo',
        'local': 'local_elo',
        'games': 'total_games',
        'recent': 'recent_games',
    }
    return aliases.get(str(value).strip().lower(), str(value).strip().lower())


def _validate_request(request: RoleLeaderboardRequest) -> None:
    if int(request.guild_id) <= 0:
        raise RoleLeaderboardValidationError(
            'guild_id must be a positive integer.'
        )
    if not 1 <= len(request.selected_role_ids) <= MAX_SELECTED_ROLES:
        raise RoleLeaderboardValidationError(
            f'Select between 1 and {MAX_SELECTED_ROLES} roles.'
        )
    if len(set(request.selected_role_ids)) != len(request.selected_role_ids):
        raise RoleLeaderboardValidationError('Selected roles must be unique.')
    if request.match_mode not in VALID_MATCH_MODES:
        raise RoleLeaderboardValidationError(
            'Choose either all-role or any-role matching.'
        )
    sort_key = _normalise_sort(request.sort_key)
    if sort_key not in VALID_SORTS:
        raise RoleLeaderboardValidationError('Choose a valid leaderboard sort.')
    if request.scope not in VALID_SCOPES:
        raise RoleLeaderboardValidationError('Choose a valid ELO scope.')
    if len(request.member_snapshots) > MAX_ROLE_MEMBER_SNAPSHOTS:
        raise RoleLeaderboardValidationError(
            'The captured guild member set exceeds the bounded limit.'
        )
    role_by_id = {role.role_id: role for role in request.role_snapshots}
    for role_id in request.selected_role_ids:
        role = role_by_id.get(int(role_id))
        if role is None:
            raise RoleLeaderboardValidationError(
                'One or more selected roles are not in this guild.'
            )
        if role.is_default or role.managed:
            raise RoleLeaderboardValidationError(
                'Everyone, bot-managed, and integration roles cannot be used.'
            )


def _tuples(query) -> Iterable[tuple]:
    """Read query tuples without materialising a lazy query outside the worker."""

    return query.tuples()


def _record_counts(
    *,
    guild_id: int,
    discord_ids: tuple[int, ...],
    player_ids: tuple[int, ...],
    global_guild_ids: tuple[int, ...],
    global_scope: bool,
) -> dict[int, tuple[int, int]]:
    """Batch one current-era W/L scope in a single aggregate query."""

    if global_scope and not global_guild_ids:
        return {}
    if not global_scope and not player_ids:
        return {}

    key_field = (
        models.DiscordMember.discord_id
        if global_scope
        else models.Player.id
    )
    winner_case = peewee.Case(
        None,
        ((models.Game.winner == models.Lineup.gameside_id, 1),),
        0,
    )
    loser_case = peewee.Case(
        None,
        ((models.Game.winner != models.Lineup.gameside_id, 1),),
        0,
    )
    date_min, date_max = models.moonrise_or_air_date_range()
    conditions = [
        (models.Game.is_completed == 1),
        (models.Game.is_confirmed == 1),
        (models.Game.is_ranked == 1),
        (models.Game.date >= date_min),
        (models.Game.date <= date_max),
    ]
    if global_scope:
        conditions.extend((
            models.Game.guild_id.in_(global_guild_ids),
            models.DiscordMember.discord_id.in_(discord_ids),
        ))
    else:
        conditions.extend((
            models.Game.guild_id == int(guild_id),
            models.Player.id.in_(player_ids),
        ))

    query = (
        models.Lineup
        .select(
            key_field.alias('role_leaderboard_key'),
            peewee.fn.SUM(winner_case).alias('wins'),
            peewee.fn.SUM(loser_case).alias('losses'),
        )
        .join(models.Game)
        .join_from(models.Lineup, models.GameSide)
        .join_from(models.Lineup, models.Player)
        .join_from(models.Player, models.DiscordMember)
        .where(*conditions)
        .group_by(key_field)
    )
    return {
        int(key): (int(wins or 0), int(losses or 0))
        for key, wins, losses in _tuples(query)
    }


def _game_counts(
    discord_ids: tuple[int, ...],
    recent_cutoff: datetime.datetime,
) -> dict[int, tuple[int, int]]:
    """Batch all-time and 14-day lineup counts for the captured members."""

    if not discord_ids:
        return {}
    recent_case = peewee.Case(
        None,
        ((
            (models.Game.date > recent_cutoff)
            | (models.Game.completed_ts > recent_cutoff),
            1,
        ),),
        0,
    )
    query = (
        models.Lineup
        .select(
            models.DiscordMember.discord_id.alias('role_leaderboard_key'),
            peewee.fn.COUNT(models.Lineup.id).alias('total_games'),
            peewee.fn.SUM(recent_case).alias('recent_games'),
        )
        .join(models.Game)
        .join_from(models.Lineup, models.Player)
        .join_from(models.Player, models.DiscordMember)
        .where(models.DiscordMember.discord_id.in_(discord_ids))
        .group_by(models.DiscordMember.discord_id)
    )
    return {
        int(key): (int(total or 0), int(recent or 0))
        for key, total, recent in _tuples(query)
    }


def load_role_leaderboard(
    request: RoleLeaderboardRequest,
    *,
    recent_cutoff: datetime.datetime | None = None,
) -> RoleLeaderboardResult:
    """Load one bounded role snapshot with one worker-local DB connection."""

    _validate_request(request)
    candidates = tuple(request.member_snapshots)
    candidate_count = len(candidates)
    if not candidates:
        return RoleLeaderboardResult(
            rows=(),
            loaded_count=0,
            candidate_count=0,
            truncated=False,
            inactive_role_id=request.inactive_role_id,
        )

    candidates_by_id = {
        int(member.discord_id): member
        for member in candidates
    }
    discord_ids = tuple(candidates_by_id)
    with models.db.connection_context():
        player_query = (
            models.Player
            .select(models.Player, models.DiscordMember)
            .join(models.DiscordMember)
            .where(
                (models.Player.guild_id == int(request.guild_id))
                & (models.DiscordMember.discord_id.in_(discord_ids))
            )
            .order_by(models.Player.id)
        )
        registered_count = int(player_query.count())
        players = tuple(player_query.limit(MAX_LOADED_ROWS))
        players_by_discord_id = {}
        player_ids = []
        for player in players:
            discord_member = player.discord_member
            discord_id = int(discord_member.discord_id)
            if discord_id in players_by_discord_id:
                continue
            players_by_discord_id[discord_id] = player
            player_ids.append(int(player.id))

        global_records = _record_counts(
            guild_id=request.guild_id,
            discord_ids=tuple(players_by_discord_id),
            player_ids=tuple(player_ids),
            global_guild_ids=tuple(request.global_guild_ids),
            global_scope=True,
        )
        local_records = _record_counts(
            guild_id=request.guild_id,
            discord_ids=tuple(players_by_discord_id),
            player_ids=tuple(player_ids),
            global_guild_ids=tuple(request.global_guild_ids),
            global_scope=False,
        )
        games = _game_counts(
            tuple(players_by_discord_id),
            request.recent_cutoff or recent_cutoff or (
                datetime.datetime.now()
                - datetime.timedelta(days=14)
            ),
        )

    rows = []
    for discord_id, player in players_by_discord_id.items():
        member = candidates_by_id[discord_id]
        discord_member = player.discord_member
        global_wins, global_losses = global_records.get(discord_id, (0, 0))
        local_wins, local_losses = local_records.get(int(player.id), (0, 0))
        total_games, recent_games = games.get(discord_id, (0, 0))
        name = getattr(player, 'name', None) or getattr(
            discord_member,
            'name',
            None,
        ) or member.name
        rows.append(
            RoleLeaderboardRow(
                discord_id=discord_id,
                name=str(name),
                role_ids=tuple(sorted(set(int(role_id) for role_id in member.role_ids))),
                global_elo=int(getattr(discord_member, 'elo_moonrise', 0)),
                local_elo=int(getattr(player, 'elo_moonrise', 0)),
                global_wins=global_wins,
                global_losses=global_losses,
                local_wins=local_wins,
                local_losses=local_losses,
                total_games=total_games,
                recent_games=recent_games,
            )
        )

    return RoleLeaderboardResult(
        rows=tuple(sorted(rows, key=lambda row: row.discord_id)),
        loaded_count=len(rows),
        candidate_count=candidate_count,
        truncated=registered_count > MAX_LOADED_ROWS,
        inactive_role_id=request.inactive_role_id,
    )


def _matching_rows(
    result: RoleLeaderboardResult,
    *,
    selected_role_ids: tuple[int, ...],
    match_mode: str,
) -> tuple[RoleLeaderboardRow, ...]:
    selected = set(int(role_id) for role_id in selected_role_ids)
    if match_mode == 'all':
        matches = (
            row for row in result.rows
            if selected.issubset(row.role_ids)
        )
    elif match_mode == 'any':
        matches = (
            row for row in result.rows
            if selected.intersection(row.role_ids)
        )
    else:
        raise RoleLeaderboardValidationError('Choose all or any role matching.')

    if result.inactive_role_id is not None and result.inactive_role_id not in selected:
        matches = (
            row for row in matches
            if result.inactive_role_id not in row.role_ids
        )
    return tuple(matches)


def role_leaderboard_page(
    result: RoleLeaderboardResult,
    *,
    selected_role_ids: tuple[int, ...],
    selected_role_names: tuple[str, ...],
    match_mode: str,
    sort_key: str,
    scope: str,
    page_index: int = 0,
    page_size: int = ROLE_LEADERBOARD_PAGE_SIZE,
    descending: bool = True,
) -> RoleLeaderboardPage:
    """Filter, sort, and page an immutable snapshot without another read."""

    if page_size <= 0:
        raise ValueError('page_size must be positive.')
    if not 1 <= len(selected_role_ids) <= MAX_SELECTED_ROLES:
        raise RoleLeaderboardValidationError(
            f'Select between 1 and {MAX_SELECTED_ROLES} roles.'
        )
    if len(set(selected_role_ids)) != len(selected_role_ids):
        raise RoleLeaderboardValidationError('Selected roles must be unique.')
    sort_key = _normalise_sort(sort_key)
    if sort_key not in VALID_SORTS:
        raise RoleLeaderboardValidationError('Choose a valid leaderboard sort.')
    if scope not in VALID_SCOPES:
        raise RoleLeaderboardValidationError('Choose a valid ELO scope.')
    if match_mode not in VALID_MATCH_MODES:
        raise RoleLeaderboardValidationError('Choose all or any role matching.')
    matching = _matching_rows(
        result,
        selected_role_ids=selected_role_ids,
        match_mode=match_mode,
    )
    metric = {
        'global_elo': lambda row: row.global_elo,
        'local_elo': lambda row: row.local_elo,
        'total_games': lambda row: row.total_games,
        'recent_games': lambda row: row.recent_games,
    }[sort_key]
    ranked = tuple(
        replace(row, rank=index)
        for index, row in enumerate(
            sorted(
                matching,
                key=lambda row: (
                    -metric(row) if descending else metric(row),
                    row.discord_id,
                ),
            ),
            start=1,
        )
    )
    page_count = max(1, (len(ranked) + page_size - 1) // page_size)
    if page_index < 0 or page_index >= page_count:
        raise IndexError('page_index is outside the role leaderboard.')
    start = page_index * page_size
    end = min(start + page_size, len(ranked))
    page_rows = ranked[start:end]
    role_label = ', '.join(selected_role_names) or 'selected roles'
    match_label = 'all selected roles' if match_mode == 'all' else 'any selected role'
    title = f'Role Leaderboard — {role_label} ({match_label})'
    return RoleLeaderboardPage(
        title=title,
        selected_role_ids=tuple(selected_role_ids),
        selected_role_names=tuple(selected_role_names),
        match_mode=match_mode,
        sort_key=sort_key,
        scope=scope,
        total_matched=len(ranked),
        loaded_count=result.loaded_count,
        page_index=page_index,
        page_count=page_count,
        start_rank=page_rows[0].rank if page_rows else 0,
        end_rank=page_rows[-1].rank if page_rows else 0,
        rows=tuple(page_rows),
        truncated=bool(result.truncated),
    )


_role_leaderboard_read_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix='polybot-role-leaderboard-read',
)


async def _run_bounded_role_call(call):
    """Run a read off-loop and drain the worker when the awaiter is cancelled."""

    concurrent_future = _role_leaderboard_read_executor.submit(call)
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
                'Cancelled role leaderboard worker completed with an error'
            )
        raise asyncio.CancelledError
    return concurrent_future.result()


async def run_role_leaderboard(
    request: RoleLeaderboardRequest,
    *,
    recent_cutoff: datetime.datetime | None = None,
) -> RoleLeaderboardResult:
    """Submit one role snapshot read to the bounded role executor."""

    return await _run_bounded_role_call(
        functools.partial(
            load_role_leaderboard,
            request,
            recent_cutoff=recent_cutoff,
        )
    )
