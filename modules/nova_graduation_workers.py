"""Bounded read workers for automatic Nova graduation eligibility."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import logging

from peewee import Case, fn

import settings
from modules import models


logger = logging.getLogger('polybot.' + __name__)

MAX_PARTICIPANTS = 50
MAX_LINEUP_ROWS = 20_000
MAX_SIDE_ROWS = 20_000


class NovaGraduationError(RuntimeError):
    """The bounded eligibility snapshot could not be loaded safely."""


@dataclass(frozen=True)
class NovaParticipantSnapshot:
    discord_id: int
    member_name: str
    mention: str
    has_nova_role: bool
    has_grad_role: bool


@dataclass(frozen=True)
class NovaGraduationRequest:
    game_id: int
    guild_id: int
    allowed_guild_ids: tuple[int, ...]
    participants: tuple[NovaParticipantSnapshot, ...]


@dataclass(frozen=True)
class NovaGraduationCandidate:
    discord_id: int
    member_name: str
    mention: str
    global_elo: int
    wins: int
    losses: int
    qualifying_game_ids: tuple[int, ...]


@dataclass(frozen=True)
class NovaGraduationResult:
    game_id: int
    guild_id: int
    candidates: tuple[NovaGraduationCandidate, ...]
    draft_open: bool
    draft_channel_id: int | None
    draft_message_id: int | None


def _validate_request(request: NovaGraduationRequest) -> None:
    if request.game_id <= 0 or request.guild_id <= 0:
        raise NovaGraduationError('Game and guild IDs must be positive.')
    allowed = {int(value) for value in request.allowed_guild_ids if value}
    if request.guild_id not in allowed:
        raise NovaGraduationError(
            f'Guild {request.guild_id} is outside Nova graduation scope.'
        )
    if len(request.participants) > MAX_PARTICIPANTS:
        raise NovaGraduationError(
            f'Nova graduation is limited to {MAX_PARTICIPANTS} participants.'
        )
    ids = [int(item.discord_id) for item in request.participants]
    if any(value <= 0 for value in ids) or len(ids) != len(set(ids)):
        raise NovaGraduationError(
            'Nova participant IDs must be positive and unique.'
        )


def _eligible_snapshots(request: NovaGraduationRequest):
    return tuple(
        item for item in request.participants
        if item.has_nova_role and not item.has_grad_role
    )


def _load_players(guild_id: int, discord_ids: tuple[int, ...]):
    query = (
        models.Player
        .select(models.Player, models.DiscordMember)
        .join(models.DiscordMember)
        .where(
            (models.Player.guild_id == int(guild_id))
            & (models.DiscordMember.discord_id.in_(discord_ids))
        )
    )
    return {
        int(player.discord_member.discord_id): player
        for player in query
    }


def _load_game_rows(player_ids: tuple[int, ...]):
    rows = list(
        models.Lineup
        .select(
            models.Lineup.player,
            models.Game.id.alias('game_id'),
            models.Game.date,
            models.Game.is_pending,
            models.Game.is_completed,
        )
        .join(models.Game)
        .where(models.Lineup.player.in_(player_ids))
        .order_by(
            models.Lineup.player,
            -models.Game.date,
            -models.Game.id,
            models.Lineup.id,
        )
        .limit(MAX_LINEUP_ROWS + 1)
        .dicts()
    )
    if len(rows) > MAX_LINEUP_ROWS:
        raise NovaGraduationError(
            'Nova graduation game history exceeded its safe read bound.'
        )
    return tuple(rows)


def _load_smallest_sides(game_ids: tuple[int, ...]):
    if not game_ids:
        return {}
    rows = list(
        models.GameSide
        .select(models.GameSide.game, models.GameSide.size)
        .where(models.GameSide.game.in_(game_ids))
        .order_by(models.GameSide.game, models.GameSide.id)
        .limit(MAX_SIDE_ROWS + 1)
        .tuples()
    )
    if len(rows) > MAX_SIDE_ROWS:
        raise NovaGraduationError(
            'Nova graduation side history exceeded its safe read bound.'
        )
    smallest = {}
    for game_id, size in rows:
        game_id = int(game_id)
        size = int(size)
        smallest[game_id] = min(size, smallest.get(game_id, size))
    return smallest


def _load_global_records(discord_member_ids: tuple[int, ...]):
    if not discord_member_ids:
        return {}
    date_min, date_max = models.moonrise_or_air_date_range(version=None)
    win_status = Case(
        None,
        ((models.Game.winner == models.Lineup.gameside, 1),),
        0,
    )
    rows = (
        models.Lineup
        .select(
            models.Player.discord_member.alias('discord_member_id'),
            fn.COALESCE(fn.SUM(win_status), 0).alias('wins'),
            fn.COUNT(models.Lineup.id).alias('games'),
        )
        .join(models.Game)
        .switch(models.Lineup)
        .join(models.Player)
        .where(
            models.Player.discord_member.in_(discord_member_ids)
            & (models.Game.is_completed == 1)
            & (models.Game.is_confirmed == 1)
            & (models.Game.is_ranked == 1)
            & (models.Game.guild_id.in_(settings.servers_included_in_global_lb()))
            & (models.Game.date >= date_min)
            & (models.Game.date <= date_max)
        )
        .group_by(models.Player.discord_member)
        .dicts()
    )
    result = {}
    for row in rows:
        wins = int(row['wins'])
        result[int(row['discord_member_id'])] = (
            wins,
            int(row['games']) - wins,
        )
    return result


def _load_draft_state(guild_id: int):
    configuration = models.Configuration.get_or_none(
        models.Configuration.guild_id == int(guild_id)
    )
    if configuration is None:
        return False, None, None
    draft = configuration.polychamps_draft or {}
    if not bool(draft.get('draft_open')):
        return False, None, None
    try:
        channel_id = int(draft.get('announcement_channel'))
        message_id = int(draft.get('announcement_message'))
    except (TypeError, ValueError):
        return True, None, None
    if channel_id <= 0 or message_id <= 0:
        return True, None, None
    return True, channel_id, message_id


def load_nova_graduation(
    request: NovaGraduationRequest,
) -> NovaGraduationResult:
    """Load all candidate eligibility and draft state without writing."""

    _validate_request(request)
    snapshots = _eligible_snapshots(request)
    if not snapshots:
        return NovaGraduationResult(
            request.game_id,
            request.guild_id,
            (),
            False,
            None,
            None,
        )

    with models.db.connection_context():
        players = _load_players(
            request.guild_id,
            tuple(item.discord_id for item in snapshots),
        )
        player_ids = tuple(int(player.id) for player in players.values())
        game_rows = _load_game_rows(player_ids) if player_ids else ()
        game_ids = tuple(dict.fromkeys(int(row['game_id']) for row in game_rows))
        smallest_sides = _load_smallest_sides(game_ids)

        rows_by_player = {player_id: [] for player_id in player_ids}
        for row in game_rows:
            rows_by_player.setdefault(int(row['player']), []).append(row)

        qualifying = {}
        for discord_id, player in players.items():
            game_ids_for_player = []
            has_completed = False
            for row in rows_by_player.get(int(player.id), ()):
                game_id = int(row['game_id'])
                if smallest_sides.get(game_id, 0) <= 1:
                    continue
                if not bool(row['is_pending']):
                    game_ids_for_player.append(game_id)
                if bool(row['is_completed']):
                    has_completed = True
            if len(game_ids_for_player) >= 2 and has_completed:
                qualifying[discord_id] = tuple(game_ids_for_player)

        qualifying_players = tuple(
            players[discord_id] for discord_id in qualifying
        )
        records = _load_global_records(
            tuple(
                int(player.discord_member_id)
                for player in qualifying_players
            )
        )
        draft_open, draft_channel_id, draft_message_id = (
            _load_draft_state(request.guild_id)
            if qualifying else (False, None, None)
        )

        candidates = []
        for snapshot in snapshots:
            player = players.get(snapshot.discord_id)
            if player is None or snapshot.discord_id not in qualifying:
                continue
            wins, losses = records.get(
                int(player.discord_member_id),
                (0, 0),
            )
            candidates.append(NovaGraduationCandidate(
                discord_id=snapshot.discord_id,
                member_name=snapshot.member_name,
                mention=snapshot.mention,
                global_elo=int(player.discord_member.elo_moonrise),
                wins=wins,
                losses=losses,
                qualifying_game_ids=qualifying[snapshot.discord_id],
            ))

        return NovaGraduationResult(
            game_id=request.game_id,
            guild_id=request.guild_id,
            candidates=tuple(candidates),
            draft_open=draft_open,
            draft_channel_id=draft_channel_id,
            draft_message_id=draft_message_id,
        )


_nova_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix='polybot-nova-read',
)


async def run_load_nova_graduation(
    request: NovaGraduationRequest,
) -> NovaGraduationResult:
    future = _nova_executor.submit(load_nova_graduation, request)
    try:
        while not future.done():
            await asyncio.sleep(0.001)
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None:
            while task.cancelling():
                task.uncancel()
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
    return future.result()
