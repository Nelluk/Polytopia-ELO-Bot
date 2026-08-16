"""Bounded worker-local reads for the native squad-show workspace."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
import functools
import itertools
import logging

import peewee

from modules import models, utilities
import settings


logger = logging.getLogger('polybot.' + __name__)


MAX_SQUAD_MATCHES = 50
SQUAD_MEMBER_MIN = 1
SQUAD_MEMBER_MAX = 3
RECENT_GAME_LIMIT = 10
SQUAD_SHOW_PAGE_SIZE = 10


class SquadShowValidationError(ValueError):
    """The request contains an invalid or contradictory value."""


class SquadShowPermissionError(SquadShowValidationError):
    """The captured guild/requester policy does not permit the read."""


class SquadShowLookupError(SquadShowValidationError):
    """The requested squad or member cannot be resolved in this guild."""


class SquadShowPlayerNotFound(SquadShowLookupError):
    """A selected Discord member has no registered player in this guild."""


class SquadShowSquadNotFound(SquadShowLookupError):
    """The requested squad does not exist or is not visible here."""


class SquadShowWrongGuild(SquadShowLookupError):
    """The requested squad belongs to another guild."""


@dataclass(frozen=True)
class SquadShowRequest:
    """Immutable worker input; no Discord or Peewee object crosses over."""

    guild_id: int
    requester_id: int
    member_ids: tuple[int, ...] = ()
    squad_id: int | None = None
    team_enabled: bool = True
    channel_allowed: bool = True
    # This is only a display snapshot.  Name mutation workers revalidate
    # authority independently at submission time.
    requester_is_staff: bool = False


@dataclass(frozen=True)
class SquadShowMember:
    """One squad member represented only by display primitives."""

    player_id: int
    discord_id: int
    name: str
    team_emoji: str


@dataclass(frozen=True)
class SquadShowRecentGame:
    """The established legacy game headline and summary strings."""

    headline: str
    summary: str


@dataclass(frozen=True)
class SquadShowCard:
    """The complete dense card loaded for one eligible squad."""

    guild_id: int
    squad_id: int
    squad_name: str
    members: tuple[SquadShowMember, ...]
    elo: int
    wins: int
    losses: int
    leaderboard_rank: int | None
    leaderboard_length: int
    recent_games: tuple[SquadShowRecentGame, ...]
    # Captured eligibility controls whether the contextual editor is shown;
    # it is never accepted as mutation authority by the write worker.
    can_edit_name: bool = False


@dataclass(frozen=True)
class SquadShowResult:
    """Immutable snapshot used by the public workspace controls."""

    guild_id: int
    requester_id: int
    member_ids: tuple[int, ...]
    cards: tuple[SquadShowCard, ...]
    selected_squad_id: int | None
    total_matches: int
    truncated: bool

    @property
    def is_detail(self) -> bool:
        return self.selected_squad_id is not None


_squad_show_read_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix='polybot-squad-show-read',
)


def _as_int(value, *, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise SquadShowValidationError(
            f'{field} must be a positive integer.'
        ) from None


def _validate_request(request: SquadShowRequest) -> None:
    guild_id = _as_int(request.guild_id, field='guild_id')
    requester_id = _as_int(request.requester_id, field='requester_id')
    if guild_id <= 0 or requester_id <= 0:
        raise SquadShowValidationError(
            'Squad lookup requires a valid Discord server and requester.'
        )

    member_ids = tuple(_as_int(value, field='member') for value in request.member_ids)
    if request.squad_id is None and not (
        SQUAD_MEMBER_MIN <= len(member_ids) <= SQUAD_MEMBER_MAX
    ):
        raise SquadShowValidationError(
            'Choose between one and three different Discord members.'
        )
    if request.squad_id is not None and len(member_ids) > SQUAD_MEMBER_MAX:
        raise SquadShowValidationError(
            'Choose at most three Discord members.'
        )
    if len(set(member_ids)) != len(member_ids):
        raise SquadShowValidationError(
            'Choose each Discord member only once.'
        )
    if any(member_id <= 0 for member_id in member_ids):
        raise SquadShowValidationError(
            'Every selected Discord member must be valid.'
        )

    if request.squad_id is not None and _as_int(
        request.squad_id,
        field='squad_id',
    ) <= 0:
        raise SquadShowValidationError(
            'squad_id must be a positive integer.'
        )
    if not bool(request.team_enabled):
        raise SquadShowPermissionError('Teams are not enabled on this server.')
    if not bool(request.channel_allowed):
        raise SquadShowPermissionError(
            'This command can only be used in a designated ELO bot channel.'
        )


def _player_discord_id(player) -> int | None:
    member = getattr(player, 'discord_member', None)
    value = getattr(member, 'discord_id', None)
    if value is None:
        value = getattr(player, 'discord_id', None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _load_players(
    guild_id: int,
    member_ids: tuple[int, ...],
) -> tuple[object, ...]:
    """Resolve every selected member to one guild-local registered player."""

    query = (
        models.Player
        .select(models.Player, models.DiscordMember)
        .join(models.DiscordMember)
        .where(
            (models.Player.guild_id == int(guild_id))
            & models.DiscordMember.discord_id.in_(member_ids)
        )
    )
    by_discord_id = {
        player_id: player
        for player in query
        if (player_id := _player_discord_id(player)) is not None
    }
    missing = [
        member_id for member_id in member_ids
        if member_id not in by_discord_id
    ]
    if missing:
        if len(missing) == 1:
            raise SquadShowPlayerNotFound(
                f'<@{missing[0]}> is not a registered player on this server.'
            )
        mentions = ', '.join(f'<@{member_id}>' for member_id in missing)
        raise SquadShowPlayerNotFound(
            f'These members are not registered players on this server: '
            f'{mentions}.'
        )
    return tuple(by_discord_id[member_id] for member_id in member_ids)


def _bounded_query_rows(query, limit: int) -> tuple[object, ...]:
    """Materialize no more than ``limit`` rows at the database when possible."""

    query_limit = getattr(query, 'limit', None)
    if callable(query_limit):
        return tuple(query_limit(int(limit)))
    try:
        iterator = iter(query)
    except TypeError:
        return tuple(query[:limit])
    return tuple(itertools.islice(iterator, int(limit)))


def _guild_scoped_matching_query(
    player_models: tuple[object, ...],
    guild_id: int,
):
    """Preserve the legacy search and add its required guild boundary."""

    query = models.Squad.get_all_matching_squads(
        player_models,
        guild_id=int(guild_id),
    )
    # The legacy helper uses guild_id to choose its eligibility threshold but
    # does not constrain the final GameSide query.  Native discovery must not
    # leak a squad from another guild, so apply that boundary before counting
    # or loading the bounded result.  The helper's ordering and thresholds
    # remain authoritative.
    guild_squads = models.Squad.select(models.Squad.id).where(
        models.Squad.guild_id == int(guild_id)
    )
    return query.where(models.GameSide.squad.in_(guild_squads))


def _card_member(player) -> SquadShowMember:
    discord_id = _player_discord_id(player)
    if discord_id is None:
        raise SquadShowLookupError('A squad member has an invalid Discord ID.')
    discord_member = getattr(player, 'discord_member', None)
    name = getattr(player, 'name', None)
    if name is None:
        name = getattr(discord_member, 'name', None)
    team = getattr(player, 'team', None)
    return SquadShowMember(
        player_id=int(getattr(player, 'id')),
        discord_id=discord_id,
        name=str(name or f'user-{discord_id}'),
        team_emoji=str(getattr(team, 'emoji', '') or '') if team else '',
    )


def _recent_games(squad) -> tuple[SquadShowRecentGame, ...]:
    query = (
        models.GameSide
        .select(models.Game)
        .join(models.Game)
        .where(models.GameSide.squad == squad)
        .order_by(-models.Game.date, -models.Game.id)
    )
    # ``summarize_game_list`` is the established legacy renderer.  It is
    # intentionally called inside the worker, where any lazy Peewee reads it
    # performs remain on the worker-local connection.
    summaries = utilities.summarize_game_list(query[:RECENT_GAME_LIMIT])
    return tuple(
        SquadShowRecentGame(
            headline=str(headline),
            summary=str(summary),
        )
        for headline, summary in summaries[:RECENT_GAME_LIMIT]
    )


def _load_card(
    squad,
    *,
    leaderboard: tuple[int | None, int] | None = None,
) -> SquadShowCard:
    members = tuple(_card_member(player) for player in squad.get_members())
    wins, losses = squad.get_record()
    if leaderboard is None:
        rank, leaderboard_length = squad.leaderboard_rank(settings.date_cutoff)
    else:
        rank, leaderboard_length = leaderboard
    return SquadShowCard(
        guild_id=int(squad.guild_id),
        squad_id=int(squad.id),
        squad_name=str(squad.name or ''),
        members=members,
        elo=int(squad.elo),
        wins=int(wins),
        losses=int(losses),
        leaderboard_rank=(int(rank) if rank is not None else None),
        leaderboard_length=int(leaderboard_length),
        recent_games=_recent_games(squad),
    )


def _card_for_request(
    squad,
    request: SquadShowRequest,
    *,
    leaderboard: tuple[int | None, int] | None = None,
) -> SquadShowCard:
    """Add requester-only display eligibility to an otherwise dense card."""

    card = _load_card(squad, leaderboard=leaderboard)
    if bool(request.requester_is_staff):
        can_edit_name = True
    else:
        has_player = getattr(squad, 'has_player', None)
        can_edit_name = bool(
            callable(has_player)
            and has_player(discord_id=int(request.requester_id))
        )
    return replace(card, can_edit_name=can_edit_name)


def _squad_from_match_row(row):
    return getattr(row, 'squad', row)


def _leaderboard_positions(guild_id: int) -> tuple[dict[int, int], int]:
    """Load one shared rank snapshot instead of rescanning it per card."""

    rows = tuple(
        models.Squad.leaderboard(
            date_cutoff=settings.date_cutoff,
            guild_id=int(guild_id),
        ).tuples()
    )
    return (
        {int(row[0]): index for index, row in enumerate(rows, start=1)},
        len(rows),
    )


def load_squad_show(request: SquadShowRequest) -> SquadShowResult:
    """Load one exact card or a bounded, fully selectable search snapshot."""

    _validate_request(request)
    guild_id = int(request.guild_id)
    member_ids = tuple(int(member_id) for member_id in request.member_ids)

    with models.db.connection_context():
        if request.squad_id is not None:
            squad_id = int(request.squad_id)
            try:
                squad = models.Squad.get(id=squad_id)
            except peewee.DoesNotExist:
                raise SquadShowSquadNotFound(
                    f'Squad with ID {squad_id} cannot be found.'
                ) from None
            if int(squad.guild_id) != guild_id:
                raise SquadShowWrongGuild(
                    f'Squad with ID {squad_id} is affiliated with a different '
                    'Discord server.'
                )
            card = _card_for_request(squad, request)
            return SquadShowResult(
                guild_id=guild_id,
                requester_id=int(request.requester_id),
                member_ids=member_ids,
                cards=(card,),
                selected_squad_id=card.squad_id,
                total_matches=1,
                truncated=False,
            )

        players = _load_players(guild_id, member_ids)
        matching_query = _guild_scoped_matching_query(players, guild_id)
        try:
            total_matches = int(matching_query.count())
        except (AttributeError, TypeError, ValueError):
            total_matches = 0
        matching_rows = _bounded_query_rows(
            matching_query,
            MAX_SQUAD_MATCHES + 1,
        )
        squads = []
        seen_ids = set()
        for row in matching_rows:
            squad = _squad_from_match_row(row)
            squad_id = getattr(squad, 'id', None)
            try:
                squad_id = int(squad_id)
            except (TypeError, ValueError):
                continue
            if squad_id in seen_ids or int(squad.guild_id) != guild_id:
                continue
            seen_ids.add(squad_id)
            squads.append(squad)

        # A lightweight fake query may not expose count(); the materialized
        # rows still give a correct bounded result in that case.  A real query
        # count is retained so the view can accurately label truncation.
        if not total_matches:
            total_matches = len(squads)
        truncated = total_matches > MAX_SQUAD_MATCHES or len(squads) > MAX_SQUAD_MATCHES
        if squads:
            rank_by_squad_id, leaderboard_length = _leaderboard_positions(guild_id)
        else:
            rank_by_squad_id, leaderboard_length = {}, 0
        cards = tuple(
            _card_for_request(
                squad,
                request,
                leaderboard=(
                    rank_by_squad_id.get(int(squad.id)),
                    leaderboard_length,
                ),
            )
            for squad in squads[:MAX_SQUAD_MATCHES]
        )

    return SquadShowResult(
        guild_id=guild_id,
        requester_id=int(request.requester_id),
        member_ids=member_ids,
        cards=cards,
        selected_squad_id=(cards[0].squad_id if len(cards) == 1 else None),
        total_matches=int(total_matches),
        truncated=bool(truncated),
    )


async def run_squad_show(request: SquadShowRequest) -> SquadShowResult:
    """Run the read and safely drain it if the awaiting task is cancelled."""

    concurrent_future = _squad_show_read_executor.submit(
        functools.partial(load_squad_show, request)
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
                'Cancelled squad-show worker completed with an error'
            )
        raise asyncio.CancelledError
    return concurrent_future.result()
