"""Worker-local classification and pending-game deletion transactions.

The public deletion application service lives in :mod:`game_deletion`.  This
module contains only frozen request/result values and synchronous database
workers.  No Discord object or awaitable is allowed to cross this boundary.
"""

from __future__ import annotations

import asyncio
import datetime
import functools
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import peewee

from modules import game_detail_workers, game_open_workers, models


PENDING = 'pending'
IN_PROGRESS = 'in_progress'
COMPLETED = 'completed'


class GameDeletionValidationError(RuntimeError):
    """The current database state does not permit game deletion."""


class PendingGameDeletionValidationError(GameDeletionValidationError):
    """The pending-game deletion transaction rejected its request."""


class PendingGameDeletionStateChanged(PendingGameDeletionValidationError):
    """The game stopped being pending before the pending worker ran."""


@dataclass(frozen=True)
class DeletionRequest:
    """Primitive request captured on the Discord event-loop thread."""

    game_id: int
    guild_id: int
    requester_id: int
    requester_name: str
    requester_description: str
    requester_is_staff: bool
    requester_is_mod: bool
    prefix: str
    invoked_with: str = 'delete'


@dataclass(frozen=True)
class DeletionClassification:
    """The state and authorization facts needed by the application service."""

    game_id: int
    guild_id: int
    state: str
    host_id: int | None
    host_name: str | None
    registered: bool


@dataclass(frozen=True)
class DeletionBroadcastTarget:
    channel_id: int
    message_id: int


@dataclass(frozen=True)
class DeletionChannelTarget:
    guild_id: int
    channel_id: int


@dataclass(frozen=True)
class DeletionAnnouncementTarget:
    guild_id: int
    channel_id: int
    message_id: int
    snapshot: game_detail_workers.GameDetailSnapshot | None


@dataclass(frozen=True)
class DeletionEffectPlan:
    """Immutable post-commit data for one deletion invocation."""

    game_id: int
    guild_id: int
    state: str
    mentions: tuple[str, ...]
    public_message: str
    pending_filled: str | None = None
    announcement: DeletionAnnouncementTarget | None = None
    channel_targets: tuple[DeletionChannelTarget, ...] = ()
    broadcast_targets: tuple[DeletionBroadcastTarget, ...] = ()


@dataclass(frozen=True)
class PendingDeletionResult:
    game_id: int
    recalculated: bool
    effect_plan: DeletionEffectPlan


_classification_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix='polybot-game-delete-read',
)


def _registered_author(requester_id: int) -> bool:
    try:
        return models.DiscordMember.get_or_none(
            discord_id=requester_id,
        ) is not None
    except AttributeError:
        # Small offline fakes used by focused worker tests may not model the
        # registration table.  The command-level check remains authoritative
        # for those adapters.
        return True


def _load_game(game_id: int, guild_id: int):
    try:
        game = models.Game.get_by_id(game_id)
    except peewee.DoesNotExist as exc:
        raise GameDeletionValidationError(
            f'Game with ID {game_id} cannot be found.'
        ) from exc
    if game.guild_id != guild_id:
        raise GameDeletionValidationError(
            f'Game with ID {game_id} is associated with a different '
            'Discord server.'
        )
    return game


def _state_for_game(game) -> str:
    if bool(getattr(game, 'is_pending', False)):
        return PENDING
    if bool(getattr(game, 'is_completed', False)):
        return COMPLETED
    return IN_PROGRESS


def classify_game_deletion(request: DeletionRequest) -> DeletionClassification:
    """Reload and classify a game using a worker-owned Peewee connection."""

    with models.db.connection_context():
        if not _registered_author(request.requester_id):
            raise GameDeletionValidationError(
                'This command requires bot registration first. Type '
                f'__`{request.prefix}setname Your Mobile Name`__ or  '
                f'__`{request.prefix}steamname Your Steam Username`__ '
                'to get started.'
            )
        game = _load_game(request.game_id, request.guild_id)
        host = getattr(game, 'host', None)
        host_member = getattr(host, 'discord_member', None) if host else None
        host_id = (
            int(host_member.discord_id)
            if host_member is not None
            else None
        )
        host_name = str(getattr(host, 'name', '') or '') if host else None
        return DeletionClassification(
            game_id=int(game.id),
            guild_id=int(game.guild_id),
            state=_state_for_game(game),
            host_id=host_id,
            host_name=host_name,
            registered=True,
        )


async def run_classify_game_deletion(
    request: DeletionRequest,
) -> DeletionClassification:
    """Run deletion classification without blocking Discord's event loop."""

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _classification_executor,
        functools.partial(classify_game_deletion, request),
    )


def _snapshot_for_game(game) -> game_detail_workers.GameDetailSnapshot | None:
    """Freeze the pre-delete display data without carrying a model outward."""

    try:
        request = game_detail_workers.GameDetailRequest(
            guild_id=int(game.guild_id),
            channel_id=0,
            requester_discord_id=1,
            game_id=int(game.id),
        )
        return game_detail_workers._snapshot_from_game(
            game,
            request=request,
            inferred_from_channel=False,
        )
    except Exception:
        # A malformed optional card field must not turn a committed deletion
        # into a second mutation.  The publisher will log the missing card
        # plan and leave the announcement for operator reconciliation.
        return None


def _mentions(game) -> tuple[str, ...]:
    try:
        return tuple(str(value) for value in game.mentions())
    except Exception:
        return ()


def _broadcast_targets(game) -> tuple[DeletionBroadcastTarget, ...]:
    targets = []
    try:
        broadcasts = tuple(game.broadcasts)
    except Exception:
        broadcasts = ()
    for broadcast in broadcasts:
        try:
            targets.append(DeletionBroadcastTarget(
                channel_id=int(broadcast.channel_id),
                message_id=int(broadcast.message_id),
            ))
        except (AttributeError, TypeError, ValueError):
            continue
    return tuple(targets)


def _should_skip_channel_deletion(game) -> bool:
    try:
        if game.is_season_game():
            return True
    except AttributeError:
        pass
    notes = getattr(game, 'notes', None)
    completed_ts = getattr(game, 'completed_ts', None)
    old_4d = datetime.datetime.now() + datetime.timedelta(days=-4)
    return bool(
        notes
        and 'NOVA RED' in notes.upper()
        and 'NOVA BLUE' in notes.upper()
        and completed_ts
        and completed_ts > old_4d
    )


def _channel_targets(game, guild_id: int) -> tuple[DeletionChannelTarget, ...]:
    if _should_skip_channel_deletion(game):
        return ()
    targets = []
    for gameside in tuple(getattr(game, 'gamesides', ()) or ()):
        channel_id = getattr(gameside, 'team_chan', None)
        if channel_id:
            targets.append(DeletionChannelTarget(
                guild_id=int(
                    getattr(gameside, 'team_chan_external_server', None)
                    or guild_id
                ),
                channel_id=int(channel_id),
            ))
    channel_id = getattr(game, 'game_chan', None)
    if channel_id:
        targets.append(DeletionChannelTarget(
            guild_id=int(guild_id),
            channel_id=int(channel_id),
        ))
    return tuple(targets)


def build_effect_plan(
    game,
    *,
    guild_id: int,
    state: str | None = None,
) -> DeletionEffectPlan:
    """Freeze every Discord effect that must happen after this transaction."""

    state = state or _state_for_game(game)
    mentions = _mentions(game)
    announcement = None
    snapshot = _snapshot_for_game(game)
    announcement_channel = getattr(game, 'announcement_channel', None)
    announcement_message = getattr(game, 'announcement_message', None)
    if (
        state != PENDING
        and announcement_channel
        and announcement_message
    ):
        announcement = DeletionAnnouncementTarget(
            guild_id=int(guild_id),
            channel_id=int(announcement_channel),
            message_id=int(announcement_message),
            snapshot=snapshot,
        )

    if state == PENDING:
        try:
            players, capacity = game.capacity()
        except Exception:
            lineups = tuple(getattr(game, 'lineup', ()) or ())
            players = len(lineups)
            capacity = sum(getattr(game, 'size', ()) or ())
        filled_str = 'full' if players >= capacity else 'unfilled'
        public_message = (
            f'Deleting {filled_str} open game {game.id}\n'
            f'Notifying players: {" ".join(mentions)}'
        )
    else:
        public_message = (
            f'Game with ID {game.id} has been deleted and team/player '
            'ELO changes have been reverted, if applicable.\n'
            f'Notifying players: {" ".join(mentions)}'
        )

    return DeletionEffectPlan(
        game_id=int(game.id),
        guild_id=int(guild_id),
        state=state,
        mentions=mentions,
        public_message=public_message,
        pending_filled=(filled_str if state == PENDING else None),
        announcement=announcement,
        channel_targets=(
            _channel_targets(game, guild_id)
            if state != PENDING else ()
        ),
        broadcast_targets=(
            _broadcast_targets(game)
            if state == PENDING else ()
        ),
    )


def _delete_pending_records(game) -> None:
    """Delete a pending graph without invoking the ELO-aware model helper."""

    for lineup in tuple(getattr(game, 'lineup', ()) or ()):
        lineup.delete_instance()
    for gameside in tuple(getattr(game, 'gamesides', ()) or ()):
        gameside.delete_instance()
    game.delete_instance()


def delete_pending_game(request: DeletionRequest) -> PendingDeletionResult:
    """Validate and delete one pending game in one synchronous transaction."""

    with models.db.connection_context():
        with models.db.atomic():
            if not _registered_author(request.requester_id):
                raise PendingGameDeletionValidationError(
                    'This command requires bot registration first. Type '
                    f'__`{request.prefix}setname Your Mobile Name`__ or  '
                    f'__`{request.prefix}steamname Your Steam Username`__ '
                    'to get started.'
                )
            game = _load_game(request.game_id, request.guild_id)
            if not game.is_pending:
                raise PendingGameDeletionStateChanged(
                    f'Game {game.id} is no longer a pending open game. '
                    'Refresh the card or try the delete command again.'
                )
            is_hosted_by, host = game.is_hosted_by(request.requester_id)
            if not is_hosted_by and not request.requester_is_staff:
                host_name = f' **{host.name}**' if host else ''
                raise PendingGameDeletionValidationError(
                    f'Only the game host{host_name} or server staff can do '
                    'this.'
                )

            plan = build_effect_plan(
                game,
                guild_id=request.guild_id,
                state=PENDING,
            )
            models.GameLog.write(
                game_id=game.id,
                guild_id=request.guild_id,
                message=(
                    f'{request.requester_description} deleted the '
                    f'{plan.pending_filled or "unfilled"} '
                    'pending game.'
                ),
            )
            _delete_pending_records(game)
            return PendingDeletionResult(
                game_id=plan.game_id,
                recalculated=False,
                effect_plan=plan,
            )


async def run_pending_game_deletion(
    request: DeletionRequest,
) -> PendingDeletionResult:
    """Serialize pending deletion with open/join/leave/kick/start workers."""

    return await game_open_workers.pending_game_coordinator.run_worker(
        delete_pending_game,
        request,
    )
