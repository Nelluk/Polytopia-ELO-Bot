"""Bounded discovery and transactional workers for expired open games."""

from __future__ import annotations

import asyncio
import datetime
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import peewee

from modules import game_deletion_workers, game_open_workers, models


MAX_PURGE_CANDIDATES = 500
FULL_GAME_GRACE = datetime.timedelta(days=3)
PURGED = 'purged'
SKIPPED_STATE_CHANGED = 'skipped_state_changed'


@dataclass(frozen=True)
class ExpiredGameDiscoveryRequest:
    guild_id: int
    as_of: datetime.datetime
    limit: int = MAX_PURGE_CANDIDATES


@dataclass(frozen=True)
class ExpiredGameDiscoveryResult:
    game_ids: tuple[int, ...]
    truncated: bool


@dataclass(frozen=True)
class ExpiredGamePurgeRequest:
    game_id: int
    guild_id: int
    as_of: datetime.datetime
    announcement_channel_id: int | None


@dataclass(frozen=True)
class ExpiredGameEffectPlan:
    game_id: int
    guild_id: int
    announcement_channel_id: int | None
    public_message: str
    broadcast_targets: tuple[
        game_deletion_workers.DeletionBroadcastTarget, ...
    ]


@dataclass(frozen=True)
class ExpiredGamePurgeResult:
    game_id: int
    status: str
    effect_plan: ExpiredGameEffectPlan | None = None


_discovery_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix='polybot-expired-game-read',
)


def discover_expired_game_ids(
    request: ExpiredGameDiscoveryRequest,
) -> ExpiredGameDiscoveryResult:
    """Return a bounded deterministic candidate list on a local connection."""

    limit = int(request.limit)
    if limit < 1 or limit > MAX_PURGE_CANDIDATES:
        raise ValueError(
            f'Expired-game discovery limit must be between 1 and '
            f'{MAX_PURGE_CANDIDATES}.'
        )
    grace_deadline = request.as_of - FULL_GAME_GRACE
    with models.db.connection_context():
        open_game_ids = models.Game.subq_open_games_with_capacity(
            guild_id=request.guild_id,
        )
        query = (
            models.Game
            .select(models.Game.id)
            .where(
                (models.Game.guild_id == request.guild_id)
                & (models.Game.is_pending == 1)
                & (
                    (models.Game.expiration < grace_deadline)
                    | (
                        (models.Game.expiration < request.as_of)
                        & (models.Game.id.in_(open_game_ids))
                    )
                )
            )
            .order_by(models.Game.expiration, models.Game.id)
            .limit(limit + 1)
        )
        ids = tuple(int(row[0]) for row in query.tuples())
    return ExpiredGameDiscoveryResult(
        game_ids=ids[:limit],
        truncated=len(ids) > limit,
    )


async def run_discover_expired_game_ids(
    request: ExpiredGameDiscoveryRequest,
) -> ExpiredGameDiscoveryResult:
    future = _discovery_executor.submit(discover_expired_game_ids, request)
    # Keep ownership of non-cancellable connection work until the actual
    # worker thread finishes. Polling also avoids depending on a cross-thread
    # event-loop wakeup in headless runtimes.
    cancellation = None
    while not future.done():
        try:
            await asyncio.sleep(0.001)
        except asyncio.CancelledError as exc:
            cancellation = exc
    if cancellation is not None:
        raise cancellation
    return future.result()


def _load_locked_game(request: ExpiredGamePurgeRequest):
    try:
        return (
            models.Game
            .select()
            .where(
                (models.Game.id == request.game_id)
                & (models.Game.guild_id == request.guild_id)
            )
            .for_update()
            .get()
        )
    except peewee.DoesNotExist:
        return None


def _is_eligible(game, *, as_of: datetime.datetime) -> bool:
    if not bool(getattr(game, 'is_pending', False)):
        return False
    expiration = getattr(game, 'expiration', None)
    if expiration is None or expiration >= as_of:
        return False
    players, capacity = game.capacity()
    return players < capacity or expiration < as_of - FULL_GAME_GRACE


def _creator(game):
    try:
        return game.creating_player()
    except Exception:
        return None


def _player_description(player) -> str:
    if player is None:
        return 'an unknown game creator'
    member = getattr(player, 'discord_member', None)
    if member is None:
        return 'an unknown game creator'
    try:
        return models.GameLog.member_string(member)
    except Exception:
        return 'an unknown game creator'


def _player_mention(player) -> str | None:
    if player is None:
        return None
    try:
        return str(player.mention())
    except Exception:
        return None


def _build_effect_plan(
    game,
    request: ExpiredGamePurgeRequest,
) -> tuple[ExpiredGameEffectPlan, str]:
    players, capacity = game.capacity()
    try:
        mentions = tuple(str(value) for value in game.mentions())
    except Exception:
        mentions = ()
    mention_text = f'Notifying players: {" ".join(mentions)}'
    creator = _creator(game)
    creator_description = _player_description(creator)
    creator_mention = _player_mention(creator)
    host = getattr(game, 'host', None)
    creator_id = getattr(creator, 'id', None)
    host_id = getattr(host, 'id', None)
    host_mention = _player_mention(host)
    host_text = (
        f' (Matchmaking host {host_mention})'
        if host_mention and host_id != creator_id
        else ''
    )
    ranked = 'ranked ' if bool(getattr(game, 'is_ranked', False)) else ''

    if not players:
        log_text = 'Bot purged an empty pending game.'
        public_message = ''
    elif players >= capacity:
        log_text = (
            f'Bot purged a {ranked}full pending game because '
            f'{creator_description} did not start it.'
        )
        subject = creator_mention or 'its creator'
        public_message = (
            f'Purging expired game {game.id}. This game was full but '
            f'{subject} never `start`-ed it. :rage:\n'
            f'{mention_text}{host_text}'
        )
    else:
        hosted_by = (
            f' hosted by {creator_description}' if creator is not None else ''
        )
        log_text = (
            f'Bot purged a {ranked}pending game{hosted_by} because it did '
            'not fill in time.'
        )
        public_message = (
            f'Purging expired game {game.id}. This game did not fill prior '
            f'to expiration.\n{mention_text}{host_text}'
        )

    broadcast_targets = game_deletion_workers.freeze_broadcast_targets(game)
    target_text = ','.join(
        f'{target.channel_id}/{target.message_id}'
        for target in broadcast_targets
    ) or 'none'
    audit_text = (
        f'{log_text} Reconciliation targets: announcement_channel='
        f'{request.announcement_channel_id or "none"}; '
        f'external_broadcasts={target_text}.'
    )
    return ExpiredGameEffectPlan(
        game_id=int(game.id),
        guild_id=int(request.guild_id),
        announcement_channel_id=request.announcement_channel_id,
        public_message=public_message,
        broadcast_targets=broadcast_targets,
    ), audit_text


def purge_expired_game(
    request: ExpiredGamePurgeRequest,
) -> ExpiredGamePurgeResult:
    """Revalidate and purge one expired pending graph atomically."""

    with models.db.connection_context():
        with models.db.atomic():
            game = _load_locked_game(request)
            if game is None or not _is_eligible(game, as_of=request.as_of):
                return ExpiredGamePurgeResult(
                    game_id=int(request.game_id),
                    status=SKIPPED_STATE_CHANGED,
                )
            effect_plan, audit_text = _build_effect_plan(game, request)
            models.GameLog.write(
                game_id=int(game.id),
                guild_id=int(request.guild_id),
                message=audit_text,
                is_protected=True,
            )
            game_deletion_workers.delete_pending_records(game)
            return ExpiredGamePurgeResult(
                game_id=int(game.id),
                status=PURGED,
                effect_plan=effect_plan,
            )


async def run_purge_expired_game(
    request: ExpiredGamePurgeRequest,
) -> ExpiredGamePurgeResult:
    return await game_open_workers.pending_game_coordinator.run_worker(
        purge_expired_game,
        request,
    )
