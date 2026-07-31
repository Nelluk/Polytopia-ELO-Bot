"""Bounded worker-local reads for the unified game-detail workspace."""

from __future__ import annotations

import asyncio
import datetime
import functools
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import settings
from modules import exceptions, models


_game_detail_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix='polybot-game-detail',
)


class GameDetailError(ValueError):
    """A user-facing game-detail lookup failure."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        game_id: int | None = None,
        source_guild_id: int | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.game_id = game_id
        self.source_guild_id = source_guild_id


@dataclass(frozen=True)
class GameDetailRequest:
    """Primitive input captured on the Discord event-loop thread."""

    guild_id: int
    channel_id: int
    requester_discord_id: int
    game_id: int | None = None


@dataclass(frozen=True)
class GameDetailLineup:
    player_id: int
    discord_id: int
    player_name: str
    tribe_name: str
    tribe_emoji: str
    elo_label: str
    platform_name: str = ''


@dataclass(frozen=True)
class GameDetailDraftPick:
    position: int
    side_name: str
    player_name: str


@dataclass(frozen=True)
class GameDetailSide:
    side_id: int
    position: int
    name: str
    capacity: int
    team_id: int | None
    team_name: str
    team_emoji: str
    team_hidden: bool
    team_image_url: str
    team_elo_label: str
    squad_elo_label: str
    required_role_id: int | None
    channel_id: int | None
    external_guild_id: int | None
    win_confirmed: bool
    lineups: tuple[GameDetailLineup, ...]


@dataclass(frozen=True)
class GameDetailSnapshot:
    """Immutable data sufficient to render every game-detail section."""

    game_id: int
    guild_id: int
    name: str
    date: str
    completed_ts: str
    win_claimed_ts: str
    expiration: str
    is_pending: bool
    is_completed: bool
    is_confirmed: bool
    is_ranked: bool
    is_mobile: bool
    map_type: str
    notes: str
    league_season: int | None
    league_tier: int | None
    league_playoff: bool
    size: tuple[int, ...]
    game_channel_id: int | None
    host_discord_id: int | None
    host_name: str
    winner_side_id: int | None
    status_label: str
    result_label: str
    inferred_from_channel: bool
    cross_guild: bool
    sides: tuple[GameDetailSide, ...]
    series_record_label: str = ''
    pending_join_available: bool = False
    pending_full: bool = False
    pending_creator_name: str = ''
    pending_creator_discord_id: int | None = None
    pending_draft_order: tuple[GameDetailDraftPick, ...] = ()

    @property
    def channel_ids(self) -> tuple[int, ...]:
        """Return all channel IDs represented by the snapshot."""

        ids = []
        if self.game_channel_id is not None:
            ids.append(self.game_channel_id)
        for side in self.sides:
            for channel_id in (side.channel_id,):
                if channel_id is not None and channel_id not in ids:
                    ids.append(channel_id)
        return tuple(ids)


def _timestamp(value) -> str:
    if value is None:
        return ''
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat(sep=' ') if isinstance(value, datetime.datetime) else value.isoformat()
    return str(value)


def _is_post_moonrise(game) -> bool:
    game_date = getattr(game, 'date', None)
    if isinstance(game_date, str):
        try:
            game_date = datetime.date.fromisoformat(game_date[:10])
        except ValueError:
            return True
    return bool(game_date and game_date >= settings.moonrise_reset_date)


def _elo_label(player, lineup, game) -> str:
    post_moonrise = _is_post_moonrise(game)
    if bool(getattr(game, 'is_confirmed', False)):
        after_field = (
            'elo_after_game_moonrise'
            if post_moonrise
            else 'elo_after_game'
        )
        change_field = (
            'elo_change_player_moonrise'
            if post_moonrise
            else 'elo_change_player'
        )
        after = getattr(lineup, after_field, None)
        if after is not None:
            change = getattr(lineup, change_field, 0) or 0
            change_string = f'+{change}' if change >= 0 else str(change)
            return f'{after} {change_string}'

    current_field = 'elo_moonrise' if post_moonrise else 'elo'
    return str(getattr(player, current_field, getattr(player, 'elo', 0)))


def _team_elo_label(side) -> str:
    team = getattr(side, 'team', None)
    if team is None or bool(getattr(team, 'is_hidden', False)):
        return ''
    value = getattr(team, 'elo_alltime', None)
    if value is None:
        return ''
    change = getattr(side, 'elo_change_team_alltime', 0) or 0
    if change:
        change_string = f'+{change}' if change >= 0 else str(change)
        return f'{value} {change_string}'
    return str(value)


def _squad_elo_label(side) -> str:
    squad = getattr(side, 'squad', None)
    if squad is None:
        return ''
    value = getattr(squad, 'elo', None)
    if value is None:
        return ''
    change = getattr(side, 'elo_change_squad', 0) or 0
    if change:
        change_string = f'+{change}' if change >= 0 else str(change)
        return f'{value} {change_string}'
    return str(value)


def _lineup_snapshot(lineup, game) -> GameDetailLineup:
    player = lineup.player
    discord_member = player.discord_member
    tribe = getattr(lineup, 'tribe', None)
    player_name = (
        str(getattr(player, 'name', '') or '')
        or str(getattr(discord_member, 'name', '') or '')
        or f'Player {discord_member.discord_id}'
    )
    return GameDetailLineup(
        player_id=int(player.id),
        discord_id=int(discord_member.discord_id),
        player_name=player_name,
        tribe_name=str(getattr(tribe, 'name', '') or ''),
        tribe_emoji=str(getattr(tribe, 'emoji', '') or ''),
        elo_label=_elo_label(player, lineup, game),
        platform_name=(
            str(
                getattr(
                    discord_member,
                    'polytopia_name' if bool(getattr(game, 'is_mobile', True)) else 'name_steam',
                    '',
                )
                or ''
            )
        ),
    )


def _ordered_lineups(side):
    return sorted(
        list(getattr(side, 'lineup', ()) or ()),
        key=lambda lineup: int(getattr(lineup, 'id', 0)),
    )


def _side_name(side, lineups) -> str:
    capacity = int(getattr(side, 'size', 1) or 1)
    team = getattr(side, 'team', None)
    if not lineups and capacity == 1:
        return 'Open slot'
    if len(lineups) == 1 and capacity == 1:
        player = lineups[0].player
        return str(getattr(player, 'name', '') or 'Player')[:30]
    if team is not None:
        return str(getattr(team, 'name', '') or 'Unknown Team')
    return str(getattr(side, 'sidename', '') or 'Unknown Team')


def _side_snapshot(side, game) -> GameDetailSide:
    raw_lineups = _ordered_lineups(side)
    lineups = tuple(_lineup_snapshot(lineup, game) for lineup in raw_lineups)
    team = getattr(side, 'team', None)
    team_id = int(team.id) if team is not None else None
    return GameDetailSide(
        side_id=int(side.id),
        position=int(getattr(side, 'position', 0) or 0),
        name=_side_name(side, raw_lineups),
        capacity=int(getattr(side, 'size', 1) or 1),
        team_id=team_id,
        team_name=str(getattr(team, 'name', '') or ''),
        team_emoji=str(getattr(team, 'emoji', '') or ''),
        team_hidden=bool(getattr(team, 'is_hidden', False)) if team else False,
        team_image_url=str(getattr(team, 'image_url', '') or '') if team else '',
        team_elo_label=_team_elo_label(side),
        squad_elo_label=_squad_elo_label(side),
        required_role_id=(
            int(side.required_role_id)
            if getattr(side, 'required_role_id', None) is not None
            else None
        ),
        channel_id=(
            int(side.team_chan)
            if getattr(side, 'team_chan', None) is not None
            else None
        ),
        external_guild_id=(
            int(side.team_chan_external_server)
            if getattr(side, 'team_chan_external_server', None) is not None
            else None
        ),
        win_confirmed=bool(getattr(side, 'win_confirmed', False)),
        lineups=lineups,
    )


def _status_and_result(game, sides) -> tuple[str, str]:
    if bool(game.is_pending):
        expiration = getattr(game, 'expiration', None)
        if isinstance(expiration, str):
            try:
                expiration = datetime.datetime.fromisoformat(expiration)
            except ValueError:
                expiration = None
        if expiration is not None and expiration < datetime.datetime.now():
            return 'Expired open game', ''
        players = sum(len(side.lineups) for side in sides)
        capacity = sum(side.capacity for side in sides)
        if players >= capacity:
            return 'Full — waiting to start', ''
        return 'Open', ''
    if not bool(game.is_completed):
        return 'Incomplete', ''

    winner = getattr(game, 'winner', None)
    winner_side_id = int(winner.id) if winner is not None else None
    winner_name = next(
        (side.name for side in sides if side.side_id == winner_side_id),
        'Unknown side',
    )
    if bool(game.is_confirmed):
        return 'Completed', f'Winner: {winner_name}'
    return 'Unconfirmed winner report', f'Winner: {winner_name}'


def _series_record_label(game, sides) -> str:
    """Freeze the legacy embed's optional two-side series summary."""

    if bool(getattr(game, 'is_pending', False)) or len(sides) != 2:
        return ''
    series_record = getattr(game, 'series_record', None)
    if not callable(series_record):
        return ''
    try:
        first, second = series_record()
    except Exception:
        # A missing/legacy history row must not prevent the primary game card
        # from rendering. The rest of the snapshot remains authoritative.
        return ''

    first_wins = int(first[1])
    second_wins = int(second[1])
    if first_wins == 0:
        return ''
    if first_wins == second_wins:
        return (
            'The series record for these two opponents is tied at '
            f'{first_wins} - {second_wins}'
        )
    first_side_name = next(
        (
            side.name
            for side in sides
            if side.side_id == int(getattr(first[0], 'id', 0))
        ),
        'The first side',
    )
    return (
        f'{first_side_name} leads this series '
        f'{first_wins} - {second_wins}'
    )


def _pending_expired(game) -> bool:
    expiration = getattr(game, 'expiration', None)
    if isinstance(expiration, str):
        try:
            expiration = datetime.datetime.fromisoformat(expiration)
        except ValueError:
            expiration = None
    return bool(
        expiration is not None
        and expiration < datetime.datetime.now()
    )


def _pending_metadata(game, sides):
    if not bool(getattr(game, 'is_pending', False)):
        return False, False, '', None, ()

    players = sum(len(side.lineups) for side in sides)
    capacity = sum(side.capacity for side in sides)
    pending_full = players >= capacity
    pending_join_available = not pending_full and not _pending_expired(game)

    creator = sides[0].lineups[0] if sides and sides[0].lineups else None
    creator_name = creator.player_name if creator else ''
    creator_discord_id = creator.discord_id if creator else None

    draft_order = ()
    if pending_full and max(
        (side.capacity for side in sides),
        default=0,
    ) > 1:
        draft_order_method = getattr(game, 'draft_order', None)
        if callable(draft_order_method):
            try:
                draft_order = tuple(
                    GameDetailDraftPick(
                        position=int(pick.get('position', 0)),
                        side_name=str(
                            pick.get('sidename')
                            or f'Side {pick.get("position", 0)}'
                        ),
                        player_name=str(
                            getattr(pick.get('player'), 'name', '') or 'Player'
                        ),
                    )
                    for pick in draft_order_method()
                )
            except Exception:
                draft_order = ()

    return (
        pending_join_available,
        pending_full,
        creator_name,
        creator_discord_id,
        draft_order,
    )


def _snapshot_from_game(
    game,
    *,
    request: GameDetailRequest,
    inferred_from_channel: bool,
) -> GameDetailSnapshot:
    sides = tuple(
        _side_snapshot(side, game)
        for side in sorted(
            list(getattr(game, 'gamesides', ()) or ()),
            key=lambda side: int(getattr(side, 'position', 0)),
        )
    )
    status_label, result_label = _status_and_result(game, sides)
    (
        pending_join_available,
        pending_full,
        pending_creator_name,
        pending_creator_discord_id,
        pending_draft_order,
    ) = _pending_metadata(game, sides)
    host = getattr(game, 'host', None)
    host_member = getattr(host, 'discord_member', None) if host else None
    winner = getattr(game, 'winner', None)
    return GameDetailSnapshot(
        game_id=int(game.id),
        guild_id=int(game.guild_id),
        name=str(getattr(game, 'name', '') or ''),
        date=_timestamp(getattr(game, 'date', None)),
        completed_ts=_timestamp(getattr(game, 'completed_ts', None)),
        win_claimed_ts=_timestamp(getattr(game, 'win_claimed_ts', None)),
        expiration=_timestamp(getattr(game, 'expiration', None)),
        is_pending=bool(game.is_pending),
        is_completed=bool(game.is_completed),
        is_confirmed=bool(game.is_confirmed),
        is_ranked=bool(game.is_ranked),
        is_mobile=bool(getattr(game, 'is_mobile', True)),
        map_type=str(getattr(game, 'map_type', '') or ''),
        notes=str(getattr(game, 'notes', '') or ''),
        league_season=(
            int(game.league_season)
            if getattr(game, 'league_season', None) is not None
            else None
        ),
        league_tier=(
            int(game.league_tier)
            if getattr(game, 'league_tier', None) is not None
            else None
        ),
        league_playoff=bool(getattr(game, 'league_playoff', False)),
        size=tuple(int(value) for value in (getattr(game, 'size', ()) or ())),
        game_channel_id=(
            int(game.game_chan)
            if getattr(game, 'game_chan', None) is not None
            else None
        ),
        host_discord_id=(
            int(host_member.discord_id)
            if host_member is not None
            else None
        ),
        host_name=str(getattr(host, 'name', '') or '') if host else '',
        winner_side_id=int(winner.id) if winner is not None else None,
        status_label=status_label,
        result_label=result_label,
        inferred_from_channel=inferred_from_channel,
        cross_guild=int(game.guild_id) != request.guild_id,
        sides=sides,
        series_record_label=_series_record_label(game, sides),
        pending_join_available=pending_join_available,
        pending_full=pending_full,
        pending_creator_name=pending_creator_name,
        pending_creator_discord_id=pending_creator_discord_id,
        pending_draft_order=pending_draft_order,
    )


def _load_game(request: GameDetailRequest):
    inferred_from_channel = request.game_id is None
    if inferred_from_channel:
        try:
            associated_game = models.Game.by_channel_id(
                chan_id=request.channel_id,
            )
        except exceptions.TooManyMatches as exc:
            raise GameDetailError(
                'This channel is associated with multiple games. Please '
                'provide a game ID.',
                code='ambiguous_channel',
            ) from exc
        except exceptions.NoMatches as exc:
            raise GameDetailError(
                'I could not identify one game from this channel. Please '
                'provide a game ID.',
                code='missing_channel_game',
            ) from exc
        game_id = int(associated_game.id)
    else:
        game_id = request.game_id

    try:
        game = models.Game.load_full_game(game_id=int(game_id))
    except (ValueError, TypeError) as exc:
        raise GameDetailError(
            f'Invalid game ID "{game_id}".',
            code='invalid_id',
        ) from exc
    except models.DoesNotExist as exc:
        raise GameDetailError(
            f'Game with ID {game_id} cannot be found.',
            code='not_found',
        ) from exc

    if (
        int(game.guild_id) != request.guild_id
        and bool(getattr(game, 'is_pending', False))
    ):
        raise GameDetailError(
            f'Game with ID {game.id} is associated with a different Discord '
            'server.',
            code='cross_guild_pending',
            game_id=int(game.id),
            source_guild_id=int(game.guild_id),
        )
    return game, inferred_from_channel


def load_game_detail(request: GameDetailRequest) -> GameDetailSnapshot:
    """Load and freeze one game detail record on a worker-owned connection."""

    if request.guild_id <= 0 or request.requester_discord_id <= 0:
        raise GameDetailError(
            'A valid guild and requester are required.',
            code='invalid_request',
        )
    if request.channel_id < 0:
        raise GameDetailError(
            'A valid channel is required.',
            code='invalid_request',
        )
    if request.game_id is None and request.channel_id == 0:
        raise GameDetailError(
            'I could not identify one game from this channel. Please provide '
            'a game ID.',
            code='missing_channel_game',
        )
    if request.game_id is not None:
        try:
            request_game_id = int(request.game_id)
        except (TypeError, ValueError) as exc:
            raise GameDetailError(
                f'Invalid game ID "{request.game_id}".',
                code='invalid_id',
            ) from exc
        if request_game_id <= 0:
            raise GameDetailError(
                f'Invalid game ID "{request.game_id}".',
                code='invalid_id',
            )
        request = GameDetailRequest(
            guild_id=request.guild_id,
            channel_id=request.channel_id,
            requester_discord_id=request.requester_discord_id,
            game_id=request_game_id,
        )

    with models.db.connection_context():
        game, inferred_from_channel = _load_game(request)
        return _snapshot_from_game(
            game,
            request=request,
            inferred_from_channel=inferred_from_channel,
        )


async def run_game_detail(
    request: GameDetailRequest,
) -> GameDetailSnapshot:
    """Submit a bounded game-detail read without blocking Discord."""

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _game_detail_executor,
        functools.partial(load_game_detail, request),
    )
