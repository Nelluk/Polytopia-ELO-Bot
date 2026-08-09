"""Bounded worker-local reads for ranked full-game reminder DMs."""

from __future__ import annotations

import asyncio
import datetime
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from modules import game_detail_workers, models


MAX_REMINDER_CANDIDATES = 500
REMINDER_SUPPRESSION = datetime.timedelta(hours=12)


@dataclass(frozen=True)
class GameReminderRequest:
    as_of: datetime.datetime
    limit: int = MAX_REMINDER_CANDIDATES


@dataclass(frozen=True)
class GameReminderItem:
    game_id: int
    guild_id: int
    creator_discord_id: int
    snapshot: game_detail_workers.GameDetailSnapshot


@dataclass(frozen=True)
class GameReminderBatch:
    items: tuple[GameReminderItem, ...]
    suppressed_game_ids: tuple[int, ...]
    skipped_game_ids: tuple[int, ...]
    truncated: bool


_reminder_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix='polybot-game-reminder-read',
)


def _candidate_games(limit: int):
    query = (
        models.Game
        .select()
        .where(
            (models.Game.id.not_in(
                models.Game.subq_open_games_with_capacity()
            ))
            & (models.Game.is_pending == 1)
            & (models.Game.is_ranked == 1)
        )
        .order_by(models.Game.expiration, models.Game.id)
        .limit(limit + 1)
    )
    return tuple(query.prefetch(
        models.GameSide,
        models.Lineup,
        models.Player,
    ))


def _recent_join(game, *, cutoff: datetime.datetime) -> bool:
    last_joiner = models.GameLog.search(
        keywords=f'_{game.id}_ joined',
        guild_id=game.guild_id,
        limit=1,
    ).first()
    return bool(last_joiner and last_joiner.message_ts > cutoff)


def load_game_reminders(request: GameReminderRequest) -> GameReminderBatch:
    """Freeze due reminder cards without carrying models across the boundary."""

    limit = int(request.limit)
    if limit < 1 or limit > MAX_REMINDER_CANDIDATES:
        raise ValueError(
            f'Game reminder limit must be between 1 and '
            f'{MAX_REMINDER_CANDIDATES}.'
        )
    cutoff = request.as_of - REMINDER_SUPPRESSION
    items = []
    suppressed = []
    skipped = []
    with models.db.connection_context():
        candidates = _candidate_games(limit)
        truncated = len(candidates) > limit
        for game in candidates[:limit]:
            game_id = int(game.id)
            if _recent_join(game, cutoff=cutoff):
                suppressed.append(game_id)
                continue
            try:
                creator = game.creating_player()
                creator_member = creator.discord_member if creator else None
                creator_discord_id = int(creator_member.discord_id)
                detail_request = game_detail_workers.GameDetailRequest(
                    guild_id=int(game.guild_id),
                    channel_id=0,
                    requester_discord_id=creator_discord_id,
                    game_id=game_id,
                )
                snapshot = game_detail_workers._snapshot_from_game(
                    game,
                    request=detail_request,
                    inferred_from_channel=False,
                )
            except Exception:
                skipped.append(game_id)
                continue
            items.append(GameReminderItem(
                game_id=game_id,
                guild_id=int(game.guild_id),
                creator_discord_id=creator_discord_id,
                snapshot=snapshot,
            ))
    return GameReminderBatch(
        items=tuple(items),
        suppressed_game_ids=tuple(suppressed),
        skipped_game_ids=tuple(skipped),
        truncated=truncated,
    )


async def run_load_game_reminders(
    request: GameReminderRequest,
) -> GameReminderBatch:
    """Submit one bounded read and retain ownership through cancellation."""

    future = _reminder_executor.submit(load_game_reminders, request)
    cancellation = None
    while not future.done():
        try:
            await asyncio.sleep(0.001)
        except asyncio.CancelledError as exc:
            cancellation = exc
    if cancellation is not None:
        raise cancellation
    return future.result()
