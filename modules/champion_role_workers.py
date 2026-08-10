"""Bounded database workers for recurring ELO Champion reconciliation."""

from __future__ import annotations

import asyncio
import datetime
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import logging

from modules import models


MAX_CHAMPION_GUILDS = 100
logger = logging.getLogger('polybot.' + __name__)


class ChampionRoleWorkerError(RuntimeError):
    """A champion role worker request was invalid or could not be recorded."""


@dataclass(frozen=True)
class ChampionRoleRequest:
    guild_ids: tuple[int, ...]
    date_cutoff: datetime.datetime


@dataclass(frozen=True)
class ChampionGuildTarget:
    guild_id: int
    local_champion_discord_id: int | None


@dataclass(frozen=True)
class ChampionRolePlan:
    global_champion_discord_id: int | None
    guilds: tuple[ChampionGuildTarget, ...]


@dataclass(frozen=True)
class ChampionAuditRequest:
    guild_id: int
    messages: tuple[str, ...]


@dataclass(frozen=True)
class ChampionAuditResult:
    guild_id: int
    message: str


_champion_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix='polybot-champion-role',
)


def _normalise_request(request: ChampionRoleRequest) -> ChampionRoleRequest:
    guild_ids = tuple(dict.fromkeys(int(value) for value in request.guild_ids))
    if not guild_ids or len(guild_ids) > MAX_CHAMPION_GUILDS:
        raise ChampionRoleWorkerError(
            'Champion discovery requires between 1 and '
            f'{MAX_CHAMPION_GUILDS} guild IDs.'
        )
    if any(value <= 0 for value in guild_ids):
        raise ChampionRoleWorkerError('Champion guild IDs must be positive.')
    if not isinstance(request.date_cutoff, datetime.datetime):
        raise ChampionRoleWorkerError(
            'Champion discovery requires a datetime cutoff.'
        )
    return ChampionRoleRequest(guild_ids, request.date_cutoff)


def _global_champion(date_cutoff: datetime.datetime) -> int | None:
    champion = (
        models.DiscordMember
        .leaderboard(
            date_cutoff=date_cutoff,
            guild_id=None,
            max_flag=False,
        )
        .limit(1)
        .first()
    )
    if champion is None or int(champion.elo_field) == 1000:
        return None
    return int(champion.discord_id)


def _local_champion(
    guild_id: int,
    date_cutoff: datetime.datetime,
) -> int | None:
    champion = (
        models.Player
        .leaderboard(
            date_cutoff=date_cutoff,
            guild_id=guild_id,
            max_flag=False,
        )
        .limit(1)
        .first()
    )
    if champion is None or int(champion.elo_field) == 1000:
        return None
    return int(champion.discord_member.discord_id)


def load_champion_role_plan(request: ChampionRoleRequest) -> ChampionRolePlan:
    """Freeze desired global/local champion IDs on a worker connection."""

    request = _normalise_request(request)
    with models.db.connection_context():
        global_champion = _global_champion(request.date_cutoff)
        guilds = tuple(
            ChampionGuildTarget(
                guild_id=guild_id,
                local_champion_discord_id=_local_champion(
                    guild_id,
                    request.date_cutoff,
                ),
            )
            for guild_id in request.guild_ids
        )
    return ChampionRolePlan(global_champion, guilds)


def record_champion_role_audit(
    request: ChampionAuditRequest,
) -> ChampionAuditResult:
    """Persist only Discord role effects that actually completed."""

    guild_id = int(request.guild_id)
    messages = tuple(str(message).strip() for message in request.messages)
    if guild_id <= 0 or not messages or any(not message for message in messages):
        raise ChampionRoleWorkerError(
            'Champion audit requires a guild and non-empty messages.'
        )
    message = '\n'.join(messages)
    with models.db.connection_context():
        with models.db.atomic():
            models.GameLog.write(guild_id=guild_id, message=message)
    return ChampionAuditResult(guild_id=guild_id, message=message)


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
            logger.exception(
                'Cancelled champion-role worker completed with an error'
            )
        raise
    return future.result()


async def run_load_champion_role_plan(
    request: ChampionRoleRequest,
) -> ChampionRolePlan:
    return await _drain_future(
        _champion_executor.submit(load_champion_role_plan, request)
    )


async def run_record_champion_role_audit(
    request: ChampionAuditRequest,
) -> ChampionAuditResult:
    return await _drain_future(
        _champion_executor.submit(record_champion_role_audit, request)
    )
