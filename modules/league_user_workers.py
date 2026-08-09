"""Bounded worker-local database checks for small league user commands."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import functools

from modules import models


MAX_TEAM_ROLES = 250


class LeagueUserError(RuntimeError):
    """Base user-facing league user-command failure."""


class LeagueUserPermissionError(LeagueUserError):
    """The command is unavailable in this guild or to this requester."""


@dataclass(frozen=True)
class LeagueJoinRequest:
    guild_id: int
    requester_id: int
    requester_name: str
    requester_nick: str
    league_scope: bool


@dataclass(frozen=True)
class LeagueTeamRole:
    team_id: int
    name: str
    emoji: str


@dataclass(frozen=True)
class LeagueJoinResult:
    guild_id: int
    requester_id: int
    registered: bool
    local_player_created: bool
    team_roles: tuple[LeagueTeamRole, ...]
    team_roles_truncated: bool


_league_user_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='polybot-league-user',
)


def load_join_eligibility(request: LeagueJoinRequest) -> LeagueJoinResult:
    """Load registration and team-role data on a worker-owned connection.

    ``Player.get_by_discord_id`` may preserve the legacy behavior of creating
    this guild's Player row when an account-wide DiscordMember already exists,
    so the lookup is enclosed in one synchronous transaction.
    """

    if not request.league_scope:
        raise LeagueUserPermissionError(
            'This command is available only in the configured league server.'
        )

    with models.db.connection_context():
        with models.db.atomic():
            player, created = models.Player.get_by_discord_id(
                discord_id=int(request.requester_id),
                discord_name=str(request.requester_name),
                discord_nick=str(request.requester_nick),
                guild_id=int(request.guild_id),
            )
            team_rows = tuple(
                models.Team.select(
                    models.Team.id,
                    models.Team.name,
                    models.Team.emoji,
                )
                .where(models.Team.guild_id == int(request.guild_id))
                .order_by(models.Team.name, models.Team.id)
                .limit(MAX_TEAM_ROLES + 1)
            )

    truncated = len(team_rows) > MAX_TEAM_ROLES
    return LeagueJoinResult(
        guild_id=int(request.guild_id),
        requester_id=int(request.requester_id),
        registered=player is not None,
        local_player_created=bool(created and player is not None),
        team_roles=tuple(
            LeagueTeamRole(
                team_id=int(team.id),
                name=str(team.name),
                emoji=str(team.emoji or ''),
            )
            for team in team_rows[:MAX_TEAM_ROLES]
        ),
        team_roles_truncated=truncated,
    )


async def _run_worker(function, request):
    loop = asyncio.get_running_loop()
    call = functools.partial(function, request)
    future = loop.run_in_executor(_league_user_executor, call)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        task = asyncio.current_task()
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


async def run_join_eligibility(request: LeagueJoinRequest) -> LeagueJoinResult:
    return await _run_worker(load_join_eligibility, request)
