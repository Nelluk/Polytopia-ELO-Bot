"""Bounded worker-local reads for the game-search workspace."""

from __future__ import annotations

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import re

from modules import models


MAX_GAMES = 500
STATUSES = (
    'all', 'open', 'active', 'completed', 'unconfirmed', 'unfinished',
)
OUTCOMES = ('any', 'win', 'loss')
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


@dataclass(frozen=True)
class GameSearchSnapshot:
    query: str
    key: GameSearchKey
    description: str
    rows: tuple[GameSearchRow, ...]
    truncated: bool


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


def load_game_search(request: GameSearchRequest) -> GameSearchSnapshot:
    """Load one immutable result page source on a worker-owned connection."""

    if request.guild_id <= 0 or request.requester_discord_id <= 0:
        raise GameSearchError('A valid guild and requester are required.')
    if request.key.status not in STATUSES:
        raise GameSearchError('Unknown game status filter.')
    if request.key.outcome not in OUTCOMES:
        raise GameSearchError('Unknown game result filter.')
    if request.key.status == 'unconfirmed' and not request.staff:
        raise GameSearchError(
            'Only staff can search unconfirmed winner reports.'
        )

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
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _game_search_executor,
        functools.partial(load_game_search, request),
    )
