"""Bounded worker-local reads for player profile workspaces."""

from __future__ import annotations

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import datetime

from modules import models
import settings


MAX_GAMES = 500
_player_read_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix='polybot-player-read',
)


class PlayerNotFound(ValueError):
    pass


class AmbiguousPlayer(ValueError):
    pass


@dataclass(frozen=True)
class PlayerWorkspaceRequest:
    guild_id: int
    discord_id: int | None = None
    player_query: str | None = None


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
    local_rank: int | None
    local_ranked_count: int
    global_rank: int | None
    global_ranked_count: int
    games: tuple[PlayerGameRow, ...]


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


def load_player_workspace(
    request: PlayerWorkspaceRequest,
) -> PlayerWorkspaceSnapshot:
    """Load one complete immutable workspace snapshot."""

    with models.db.connection_context():
        player = _resolve_player(request)
        member = player.discord_member
        local_wins, local_losses = player.get_record()
        global_wins, global_losses = member.get_record()
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

        offset = member.timezone_offset
        timezone = ''
        if offset is not None:
            timezone = f'UTC+{offset}' if offset >= 0 else f'UTC{offset}'
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
            local_rank=int(local_rank) if local_rank is not None else None,
            local_ranked_count=int(local_count),
            global_rank=int(global_rank) if global_rank is not None else None,
            global_ranked_count=int(global_count),
            games=tuple(rows),
        )


async def run_player_workspace(
    request: PlayerWorkspaceRequest,
) -> PlayerWorkspaceSnapshot:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _player_read_executor,
        functools.partial(load_player_workspace, request),
    )
