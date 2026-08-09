"""Bounded read worker for the league team-channel cache."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from modules import models


MAX_LEAGUE_TEAM_CHANNELS = 2_000


class LeagueChannelCacheError(RuntimeError):
    """The league team-channel cache could not be loaded safely."""


@dataclass(frozen=True)
class LeagueChannelCacheRequest:
    guild_id: int


@dataclass(frozen=True)
class LeagueChannelCacheResult:
    guild_id: int
    channel_ids: tuple[int, ...]

    @property
    def channel_count(self) -> int:
        return len(self.channel_ids)


_league_channel_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='league-channel-cache',
)


def _league_team_ids(guild_id: int) -> tuple[int, ...]:
    return tuple(
        int(team_id)
        for (team_id,) in (
            models.Team
            .select(models.Team.id)
            .where(
                (models.Team.guild_id == int(guild_id))
                & (models.Team.is_hidden == False)
            )
            .order_by(models.Team.id)
            .tuples()
        )
    )


def _league_channel_ids(
    *,
    guild_id: int,
    team_ids: tuple[int, ...],
) -> tuple[int, ...]:
    if not team_ids:
        return ()
    return _bounded_channel_ids(
        (
            models.GameSide
            .select(models.GameSide.team_chan)
            .join(models.Game)
            .where(
                (models.GameSide.team_chan.is_null(False))
                & (models.GameSide.game.guild_id == int(guild_id))
                & (models.GameSide.game.is_confirmed == False)
                & (models.GameSide.team.in_(team_ids))
            )
            .order_by(models.GameSide.id)
            .limit(MAX_LEAGUE_TEAM_CHANNELS + 1)
            .tuples()
        )
    )


def _bounded_channel_ids(rows) -> tuple[int, ...]:
    channel_ids = tuple(int(channel_id) for (channel_id,) in rows)
    if len(channel_ids) > MAX_LEAGUE_TEAM_CHANNELS:
        raise LeagueChannelCacheError(
            'The league team-channel cache exceeds its safe row limit.'
        )
    return channel_ids


def load_league_team_channels(
    request: LeagueChannelCacheRequest,
) -> LeagueChannelCacheResult:
    """Load the complete bounded cache using a worker-local connection."""

    if int(request.guild_id) <= 0:
        raise LeagueChannelCacheError('The guild ID must be valid.')
    with models.db.connection_context():
        team_ids = _league_team_ids(int(request.guild_id))
        channel_ids = _league_channel_ids(
            guild_id=int(request.guild_id),
            team_ids=team_ids,
        )
    return LeagueChannelCacheResult(
        guild_id=int(request.guild_id),
        channel_ids=channel_ids,
    )


async def _drain_future(future: Future):
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


async def run_load_league_team_channels(
    request: LeagueChannelCacheRequest,
) -> LeagueChannelCacheResult:
    return await _drain_future(
        _league_channel_executor.submit(load_league_team_channels, request)
    )
