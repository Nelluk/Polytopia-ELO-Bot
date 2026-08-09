"""Bounded worker-local reads for the game-search workspace."""

from __future__ import annotations

import asyncio
import datetime
import functools
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import logging
import re

import settings
from modules import models


logger = logging.getLogger(__name__)


MAX_GAMES = 500
STATUSES = (
    'all', 'open', 'active', 'completed', 'unconfirmed', 'unfinished',
    'joinable', 'all-open', 'waiting', 'mine',
    'nova-joinable', 'nova-all',
)
OUTCOMES = ('any', 'win', 'loss')
OPEN_GAME_STATUSES = frozenset({
    'joinable', 'all-open', 'waiting', 'mine',
    'nova-joinable', 'nova-all',
})
OPEN_GAME_VIEW_LABELS = {
    'joinable': 'Joinable for me',
    'all-open': 'All open',
    'waiting': 'Waiting to start',
    'mine': 'My open games',
    'nova-joinable': 'Joinable Nova games',
    'nova-all': 'All Nova games',
}
_REQUESTER_NOT_LOADED = object()
_game_search_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix='polybot-game-search',
)


class GameSearchError(ValueError):
    """User-facing invalid or unsupported search input."""


@dataclass(frozen=True)
class GameSearchKey:
    status: str = 'all'
    outcome: str = 'any'
    size: str = 'any'


@dataclass(frozen=True)
class GameSearchRequest:
    guild_id: int
    requester_discord_id: int
    query: str = ''
    key: GameSearchKey = GameSearchKey()
    staff: bool = False
    requester_level: int = 0
    requester_role_ids: tuple[int, ...] = ()
    requester_name: str = ''
    requester_nick: str | None = None
    ranked_filter: int = 2
    platform_filter: int = 2
    include_waitlist: bool = False


@dataclass(frozen=True)
class GameSearchRow:
    game_id: int
    name: str
    date: str
    status: str
    outcome: str
    ranked: bool
    size: str
    roster: str
    notes: str
    channel_mention: str
    host_name: str = ''
    players: int = 0
    capacity: int = 0
    expiration: str = ''
    platform_emoji: str = ''
    is_open_listing: bool = False


@dataclass(frozen=True)
class GameSearchSnapshot:
    query: str
    key: GameSearchKey
    description: str
    rows: tuple[GameSearchRow, ...]
    truncated: bool
    filtered_count: int = 0
    waitlist_ids: tuple[str, ...] = ()


def _parse_size(value: str) -> tuple[int, ...]:
    value = (value or 'any').lower().replace('vs', 'v')
    if value == 'any':
        return ()
    if not re.fullmatch(r'\d+(?:v\d+)+', value):
        raise GameSearchError(
            'Game size must look like `1v1`, `2v2`, or `1v1v1`.'
        )
    sizes = tuple(int(part) for part in value.split('v'))
    if any(size < 1 for size in sizes):
        raise GameSearchError('Every side must contain at least one player.')
    return sizes


def _parse_targets(query: str, guild_id: int):
    query = re.sub(r'<@[!&]?([0-9]{17,21})>', r'\1', query or '')
    target_list = [
        token.replace('"', '')
        for token in query.split()
        if len(token.replace('"', '')) > 2
    ]
    size = ()
    remaining = list(target_list)
    for token in tuple(remaining):
        try:
            parsed = _parse_size(token)
        except GameSearchError:
            continue
        if parsed:
            size = parsed
            remaining.remove(token)
            break

    players = []
    teams = []
    for token in tuple(remaining):
        if token.upper() in ('THE', 'OF', 'AND', '&'):
            remaining.remove(token)
            continue
        if token.isupper():
            continue
        team_matches = models.Team.get_by_name(token, guild_id)
        if len(team_matches) == 1:
            teams.append(team_matches[0])
            remaining.remove(token)
            continue
        player_matches = models.Player.string_matches(
            player_string=token,
            guild_id=guild_id,
            include_poly_info=False,
        )
        if player_matches:
            players.append(player_matches[0])
            remaining.remove(token)
    return players, teams, remaining, size


def _status(game) -> str:
    if game.is_pending:
        return 'open'
    if not game.is_completed:
        return 'active'
    if not game.is_confirmed:
        return 'unconfirmed'
    return 'completed'


def _matches_status(game, status: str) -> bool:
    if status == 'unfinished':
        return _status(game) in ('open', 'active', 'unconfirmed')
    return status == 'all' or _status(game) == status


def _target_outcome(game, players, teams) -> str:
    if not game.is_completed or not game.is_confirmed:
        return '—'
    if players:
        _, side = game.has_player(player=players[0])
        return 'Win' if side and game.winner_id == side.id else 'Loss'
    if teams:
        side = next(
            (
                game_side for game_side in game.gamesides
                if game_side.team_id == teams[0].id
            ),
            None,
        )
        return 'Win' if side and game.winner_id == side.id else 'Loss'
    return '—'


def _model_id(value) -> int | None:
    value = getattr(value, 'id', value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _lineup_player_id(lineup) -> int | None:
    player_id = getattr(lineup, 'player_id', None)
    if player_id is not None:
        return _model_id(player_id)
    return _model_id(getattr(lineup, 'player', None))


def _side_team_id(side) -> int | None:
    team_id = getattr(side, 'team_id', None)
    if team_id is not None:
        return _model_id(team_id)
    return _model_id(getattr(side, 'team', None))


def _open_query_matches(
    game,
    *,
    players,
    teams,
    title_terms,
    size_filter,
) -> bool:
    """Apply the existing search query semantics to one pending game."""

    if size_filter:
        try:
            game_size = tuple(int(size) for size in game.size)
        except (AttributeError, TypeError, ValueError):
            return False
        if game_size != tuple(size_filter):
            return False

    if players:
        game_player_ids = {
            player_id
            for player_id in (
                _lineup_player_id(lineup)
                for lineup in getattr(game, 'lineup', ())
            )
            if player_id is not None
        }
        if not {
            _model_id(player)
            for player in players
        }.issubset(game_player_ids):
            return False

    if teams:
        game_team_ids = {
            team_id
            for side in getattr(game, 'gamesides', ())
            if getattr(side, 'size', 0) > 1
            for team_id in (_side_team_id(side),)
            if team_id is not None
        }
        if not {
            _model_id(team)
            for team in teams
        }.issubset(game_team_ids):
            return False

    if title_terms:
        clean_search_terms = re.sub(
            r'[^0-9a-zA-Z ]',
            '',
            ' '.join(title_terms),
        ).split()
        searchable_fields = (
            str(getattr(game, 'name', '') or '').casefold(),
            str(getattr(game, 'notes', '') or '').casefold(),
        )
        if not any(
            all(term.casefold() in field for term in clean_search_terms)
            for field in searchable_fields
        ):
            return False

    return True


def _lookup_registered_requester(request: GameSearchRequest):
    """Read the guild player without the upsert side effect of the join path."""

    query = models.Player.select(
        models.Player,
        models.DiscordMember,
    ).join(models.DiscordMember).where(
        (models.DiscordMember.discord_id == request.requester_discord_id)
        & (models.Player.guild_id == request.guild_id)
    )
    return query.get_or_none()


def _requester_is_participant(game, requester_discord_id: int) -> bool:
    has_player = getattr(game, 'has_player', None)
    if callable(has_player):
        try:
            if has_player(discord_id=requester_discord_id)[0]:
                return True
        except TypeError:
            if has_player(requester_discord_id)[0]:
                return True

    is_hosted_by = getattr(game, 'is_hosted_by', None)
    if callable(is_hosted_by):
        return bool(is_hosted_by(requester_discord_id)[0])
    return False


def _requester_can_join_open_game(
    game,
    request: GameSearchRequest,
    *,
    capacity: int,
    requester_player=_REQUESTER_NOT_LOADED,
) -> bool:
    """Mirror the legacy joinability presentation rules in one read service.

    The reviewed join worker still revalidates all mutable state before a
    mutation.  This function only decides whether a game belongs in the
    requester-aware discovery view and never writes or invokes that service.
    """

    if _requester_is_participant(game, request.requester_discord_id):
        return True

    allowed, _ = settings.can_user_join_game(
        user_level=request.requester_level,
        game_size=capacity,
        is_ranked=bool(game.is_ranked),
        is_host=False,
    )
    if not allowed:
        return False

    player_restricted_list = re.findall(
        r'<@!?(\d+)>',
        getattr(game, 'notes', '') or '',
    )
    if (
        player_restricted_list
        and str(request.requester_discord_id) not in player_restricted_list
        and len(player_restricted_list) >= capacity - 1
    ):
        return False

    first_open_side = getattr(game, 'first_open_side', None)
    if callable(first_open_side):
        open_side, _ = first_open_side(
            roles=list(request.requester_role_ids),
        )
        if not open_side:
            return False

    player = (
        _lookup_registered_requester(request)
        if requester_player is _REQUESTER_NOT_LOADED
        else requester_player
    )
    if player is None:
        # This intentionally preserves the legacy listing behavior.  The
        # join worker will still require registration when the user acts.
        return True

    min_elo, max_elo, min_elo_g, max_elo_g = game.elo_requirements()
    if (
        player.elo_moonrise < min_elo
        or player.elo_moonrise > max_elo
        or player.discord_member.elo_moonrise < min_elo_g
        or player.discord_member.elo_moonrise > max_elo_g
    ):
        return False

    return bool(
        player.discord_member.polytopia_name
        or player.discord_member.name_steam
    )


def _open_waitlist_ids(guild_id: int, discord_id: int) -> tuple[str, ...]:
    """Reload the legacy full-game backlog inside the read worker."""

    waitlist_hosting = [
        str(game.id)
        for game in _bounded_query(models.Game.search_pending(
            status_filter=1,
            guild_id=guild_id,
            host_discord_id=discord_id,
            limit=MAX_GAMES + 1,
        ))
    ]
    waitlist_creating = [
        str(row.game)
        for row in _bounded_query(models.Game.waiting_for_creator(
            creator_discord_id=discord_id,
            guild_id=guild_id,
            limit=MAX_GAMES + 1,
        ))
    ]
    return tuple(sorted(set(waitlist_hosting + waitlist_creating), key=int))


def _bounded_query(query):
    """Materialize at most one extra row so DTO truncation is explicit."""

    limit = getattr(query, 'limit', None)
    if callable(limit):
        query = limit(MAX_GAMES + 1)
    return list(query)


def _expiration_label(expiration) -> str:
    if expiration is None:
        return 'Exp'
    hours = int(
        (expiration - datetime.datetime.now()).total_seconds() / 3600.0
    )
    return 'Exp' if hours < 0 else f'{hours}H'


def _open_game_row(game) -> GameSearchRow:
    players, capacity = game.capacity()
    creating_player = game.creating_player()
    host_name = (
        creating_player.name[:35] if creating_player else '<Vacant>'
    )
    return GameSearchRow(
        game_id=int(game.id),
        name=str(game.name or f'Game {game.id}'),
        date=str(game.date),
        status='open',
        outcome='—',
        ranked=bool(game.is_ranked),
        size=str(game.size_string()),
        roster=str(game.get_gamesides_string()),
        notes=str(game.notes or ''),
        channel_mention='',
        host_name=host_name,
        players=int(players),
        capacity=int(capacity),
        expiration=_expiration_label(game.expiration),
        platform_emoji=str(game.platform_emoji()),
        is_open_listing=True,
    )


def _open_base_games(request: GameSearchRequest):
    mode = request.key.status
    if mode == 'waiting':
        games = _bounded_query(models.Game.search_pending(
            status_filter=1,
            guild_id=request.guild_id,
            ranked_filter=request.ranked_filter,
            limit=MAX_GAMES + 1,
        ))
        return sorted(games, key=lambda game: int(game.id), reverse=True)
    if mode == 'mine':
        joined = _bounded_query(models.Game.search_pending(
            guild_id=request.guild_id,
            player_discord_id=request.requester_discord_id,
            limit=MAX_GAMES + 1,
        ))
        hosting = _bounded_query(models.Game.search_pending(
            status_filter=0,
            guild_id=request.guild_id,
            host_discord_id=request.requester_discord_id,
            limit=MAX_GAMES + 1,
        ))
        by_id = {int(game.id): game for game in (*joined, *hosting)}
        return [by_id[game_id] for game_id in sorted(by_id, reverse=True)]

    if mode not in {
        'joinable', 'all-open', 'nova-joinable', 'nova-all',
    }:
        raise GameSearchError('Unknown open-game view.')
    return _bounded_query(models.Game.search_pending(
        status_filter=2,
        guild_id=request.guild_id,
        ranked_filter=request.ranked_filter,
        platform_filter=request.platform_filter,
        limit=MAX_GAMES + 1,
    ))


def _load_open_game_search(request: GameSearchRequest) -> GameSearchSnapshot:
    if request.ranked_filter not in (0, 1, 2):
        raise GameSearchError('Unknown ranked-game filter.')
    if request.platform_filter not in (0, 1, 2):
        raise GameSearchError('Unknown platform filter.')

    with models.db.connection_context():
        players, teams, title_terms, query_size = _parse_targets(
            request.query,
            request.guild_id,
        )
        selected_size = _parse_size(request.key.size)
        size_filter = selected_size or query_size
        if request.key.outcome != 'any':
            raise GameSearchError(
                'Outcome filters are available for general game views.'
            )

        games = _open_base_games(request)
        base_count = len(games)
        games = [
            game for game in games
            if _open_query_matches(
                game,
                players=players,
                teams=teams,
                title_terms=title_terms,
                size_filter=size_filter,
            )
        ]

        filter_unjoinable = request.key.status in {
            'joinable', 'nova-joinable',
        }
        novas_only = request.key.status in {'nova-joinable', 'nova-all'}
        requester_player = (
            _lookup_registered_requester(request)
            if filter_unjoinable
            else None
        )
        filtered_count = 0
        rows = []
        for game in games:
            if (
                _model_id(getattr(game, 'guild_id', request.guild_id))
                != request.guild_id
            ):
                # The guild predicate is authoritative in the query. Keep a
                # second DTO-boundary guard so a stale/replaced query cannot
                # publish a cross-guild row.
                continue
            if request.key.status in {
                'joinable', 'all-open', 'nova-joinable', 'nova-all',
            }:
                players_in_game, capacity = game.capacity()
                if players_in_game >= capacity:
                    # search_pending(status_filter=2) normally enforces this
                    # in SQL. Keep the invariant at the DTO boundary too so
                    # a stale/replaced query cannot publish full games.
                    continue
            if filter_unjoinable:
                _, capacity = game.capacity()
                if not _requester_can_join_open_game(
                    game,
                    request,
                    capacity=capacity,
                    requester_player=requester_player,
                ):
                    filtered_count += 1
                    continue

            if novas_only and (
                not game.notes or 'NOVA' not in game.notes.upper()
            ):
                filtered_count += 1
                continue

            rows.append(_open_game_row(game))

        truncated = base_count > MAX_GAMES or len(rows) > MAX_GAMES
        rows = rows[:MAX_GAMES]
        labels = [
            f'view: {OPEN_GAME_VIEW_LABELS[request.key.status]}',
        ]
        if players:
            labels.append(
                'players: ' + ', '.join(str(player.name) for player in players)
            )
        if teams:
            labels.append(
                'teams: ' + ', '.join(str(team.name) for team in teams)
            )
        if title_terms:
            labels.append('title/notes: ' + ' '.join(title_terms))
        if size_filter:
            labels.append('size: ' + 'v'.join(map(str, size_filter)))
        return GameSearchSnapshot(
            query=(request.query or '').strip(),
            key=request.key,
            description=' · '.join(labels),
            rows=tuple(rows),
            truncated=truncated,
            filtered_count=filtered_count,
            waitlist_ids=(
                _open_waitlist_ids(
                    request.guild_id,
                    request.requester_discord_id,
                )
                if request.include_waitlist
                else ()
            ),
        )


def load_game_search(request: GameSearchRequest) -> GameSearchSnapshot:
    """Load one immutable result page source on a worker-owned connection."""

    if request.guild_id <= 0 or request.requester_discord_id <= 0:
        raise GameSearchError('A valid guild and requester are required.')
    if request.key.status not in STATUSES:
        raise GameSearchError('Unknown game search view.')
    if request.key.outcome not in OUTCOMES:
        raise GameSearchError('Unknown game result filter.')
    if request.key.status == 'unconfirmed' and not request.staff:
        raise GameSearchError(
            'Only staff can search unconfirmed winner reports.'
        )
    if request.key.status in OPEN_GAME_STATUSES:
        return _load_open_game_search(request)

    with models.db.connection_context():
        players, teams, title_terms, query_size = _parse_targets(
            request.query,
            request.guild_id,
        )
        selected_size = _parse_size(request.key.size)
        size_filter = selected_size or query_size
        if request.key.outcome != 'any' and not (players or teams):
            raise GameSearchError(
                'Choose a player or team before filtering wins or losses.'
            )

        status_filter = {
            'all': 0,
            'open': 0,
            'active': 0,
            'completed': 1,
            'unconfirmed': 5,
            'unfinished': 2,
        }[request.key.status]
        if request.key.outcome == 'win':
            status_filter = 3
        elif request.key.outcome == 'loss':
            status_filter = 4

        query = models.Game.search(
            status_filter=status_filter,
            player_filter=players,
            team_filter=teams,
            title_filter=title_terms,
            guild_id=request.guild_id,
            size_filter=size_filter,
        )
        games = [
            game for game in query
            if _matches_status(game, request.key.status)
        ]
        truncated = len(games) > MAX_GAMES
        games = games[:MAX_GAMES]
        rows = []
        for game in games:
            channel_mention = ''
            if len(players) == 1 and request.key.status == 'unfinished':
                _, player_side = game.has_player(player=players[0])
                if player_side and player_side.team_chan:
                    channel_mention = f'<#{player_side.team_chan}>'
            rows.append(GameSearchRow(
                game_id=int(game.id),
                name=str(game.name or f'Game {game.id}'),
                date=str(game.date),
                status=_status(game),
                outcome=_target_outcome(game, players, teams),
                ranked=bool(game.is_ranked),
                size=str(game.size_string()),
                roster=str(game.get_gamesides_string()),
                notes=str(game.notes or ''),
                channel_mention=channel_mention,
            ))

        labels = []
        if players:
            labels.append(
                'players: ' + ', '.join(str(player.name) for player in players)
            )
        if teams:
            labels.append(
                'teams: ' + ', '.join(str(team.name) for team in teams)
            )
        if title_terms:
            labels.append('title/notes: ' + ' '.join(title_terms))
        if size_filter:
            labels.append('size: ' + 'v'.join(map(str, size_filter)))
        return GameSearchSnapshot(
            query=(request.query or '').strip(),
            key=request.key,
            description=' · '.join(labels) or 'No text filter',
            rows=tuple(rows),
            truncated=truncated,
        )


async def run_game_search(
    request: GameSearchRequest,
) -> GameSearchSnapshot:
    """Run a bounded read and drain its thread before cancellation returns."""

    concurrent_future = _game_search_executor.submit(
        functools.partial(load_game_search, request)
    )
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
                'Cancelled game-search worker completed with an error'
            )
        raise asyncio.CancelledError
    return concurrent_future.result()
