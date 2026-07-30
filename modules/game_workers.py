"""Bounded synchronous workers for ordinary game database mutations."""

from __future__ import annotations

import asyncio
import datetime
import functools
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import peewee

from modules import exceptions, models


@dataclass(frozen=True)
class NewGameParticipant:
    """Immutable Discord-member data safe to pass into a worker."""

    discord_id: int
    discord_name: str
    discord_nick: str | None
    display_name: str
    role_names: tuple[str, ...]


@dataclass(frozen=True)
class NewGameRequest:
    guild_id: int
    name: str
    is_ranked: bool
    is_mobile: bool
    mod_override: bool
    requester_id: int
    requester_name: str
    requester_nick: str | None
    requester_description: str
    invoked_with: str
    escaped_game_name: str
    sides: tuple[tuple[NewGameParticipant, ...], ...]


@dataclass(frozen=True)
class NewGameResult:
    game_id: int
    warnings: tuple[str, ...]


class RankedStateValidationError(RuntimeError):
    """The game cannot receive the requested ranked-state correction."""


@dataclass(frozen=True)
class RankedStateResult:
    game_id: int
    is_ranked: bool


class GameExtensionValidationError(RuntimeError):
    """The game cannot receive the requested expiration extension."""


@dataclass(frozen=True)
class GameExtensionResult:
    game_id: int
    old_expiration: datetime.datetime
    new_expiration: datetime.datetime


class GameUnstartValidationError(RuntimeError):
    """The game cannot be returned to pending matchmaking."""


@dataclass(frozen=True)
class GameChannelTarget:
    gameside_id: int | None
    channel_id: int
    guild_id: int


@dataclass(frozen=True)
class GameUnstartResult:
    game_id: int
    game_name: str
    announcement_channel_id: int | None
    announcement_message_id: int | None
    mentions: tuple[str, ...]
    channel_targets: tuple[GameChannelTarget, ...]
    new_expiration: datetime.datetime


@dataclass(frozen=True)
class _RoleView:
    name: str


@dataclass(frozen=True)
class _MemberView:
    """Worker-local duck type used by existing model validation."""

    id: int
    name: str
    nick: str | None
    display_name: str
    roles: tuple[_RoleView, ...]


_game_write_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='polybot-game-write',
)


def _member_view(participant: NewGameParticipant) -> _MemberView:
    return _MemberView(
        id=participant.discord_id,
        name=participant.discord_name,
        nick=participant.discord_nick,
        display_name=participant.display_name,
        roles=tuple(_RoleView(name=name) for name in participant.role_names),
    )


def create_new_game(request: NewGameRequest) -> NewGameResult:
    """Create a complete tracked game in one worker-local transaction."""

    discord_groups = [
        [_member_view(participant) for participant in side]
        for side in request.sides
    ]

    with models.db.connection_context():
        with models.db.atomic():
            game, warnings = models.Game.create_game(
                discord_groups=discord_groups,
                name=request.name,
                is_ranked=request.is_ranked,
                guild_id=request.guild_id,
                is_mobile=request.is_mobile,
                mod_override=request.mod_override,
            )
            host_player, _ = models.Player.get_by_discord_id(
                discord_id=request.requester_id,
                guild_id=request.guild_id,
                discord_name=request.requester_name,
                discord_nick=request.requester_nick,
            )
            if host_player is None:
                raise exceptions.CheckFailedError(
                    'Could not load the registered game host.'
                )
            game.host = host_player
            game.save()
            models.GameLog.write(
                game_id=game.id,
                guild_id=request.guild_id,
                message=(
                    f'{request.requester_description} created game with '
                    f'`{request.invoked_with}` command with name '
                    f'*{request.escaped_game_name}*'
                ),
            )
            return NewGameResult(
                game_id=game.id,
                warnings=tuple(warnings),
            )


async def run_new_game_creation(request: NewGameRequest) -> NewGameResult:
    """Submit one creation workflow to the bounded game-write executor."""

    loop = asyncio.get_running_loop()
    call = functools.partial(create_new_game, request)
    return await loop.run_in_executor(_game_write_executor, call)


def set_game_ranked_state(
    game_id: int,
    guild_id: int,
    is_ranked: bool,
    requester_description: str,
) -> RankedStateResult:
    """Set an incomplete game's ranked state in one local transaction."""

    with models.db.connection_context():
        with models.db.atomic():
            try:
                game = models.Game.get_by_id(game_id)
            except peewee.DoesNotExist as exc:
                raise RankedStateValidationError(
                    f'Game with ID {game_id} cannot be found.'
                ) from exc
            if game.guild_id != guild_id:
                raise RankedStateValidationError(
                    f'Game with ID {game_id} is associated with a different '
                    'Discord server.'
                )
            if game.is_completed or game.is_confirmed:
                raise RankedStateValidationError(
                    'This can only be used on an incomplete game.'
                )
            if game.is_ranked == is_ranked:
                state = 'ranked' if is_ranked else 'unranked'
                raise RankedStateValidationError(
                    f'Game {game.id} is already marked as {state}.'
                )

            game.is_ranked = is_ranked
            game.save()
            state = 'ranked' if is_ranked else 'unranked'
            models.GameLog.write(
                game_id=game.id,
                guild_id=guild_id,
                message=(
                    f'{requester_description} set game to be {state}.'
                ),
            )
            return RankedStateResult(game_id=game.id, is_ranked=is_ranked)


async def run_ranked_state_correction(
    game_id: int,
    guild_id: int,
    is_ranked: bool,
    requester_description: str,
) -> RankedStateResult:
    """Submit a ranked-state correction to the bounded game executor."""

    loop = asyncio.get_running_loop()
    call = functools.partial(
        set_game_ranked_state,
        game_id,
        guild_id,
        is_ranked,
        requester_description,
    )
    return await loop.run_in_executor(_game_write_executor, call)


def extend_pending_game(
    game_id: int,
    guild_id: int,
    requester_description: str,
    now: datetime.datetime | None = None,
) -> GameExtensionResult:
    """Extend one pending game's expiration in a local transaction."""

    now = now or datetime.datetime.now()
    with models.db.connection_context():
        with models.db.atomic():
            try:
                game = models.Game.get_by_id(game_id)
            except peewee.DoesNotExist as exc:
                raise GameExtensionValidationError(
                    f'Game with ID {game_id} cannot be found.'
                ) from exc
            if game.guild_id != guild_id:
                raise GameExtensionValidationError(
                    f'Game with ID {game_id} is associated with a different '
                    'Discord server.'
                )
            if not game.is_pending:
                raise GameExtensionValidationError(
                    f'Game {game.id} is no longer an open game so cannot be '
                    'extended.'
                )

            old_expiration = game.expiration
            if old_expiration < now:
                new_expiration = now + datetime.timedelta(hours=24)
            else:
                new_expiration = old_expiration + datetime.timedelta(hours=24)
            game.expiration = new_expiration
            game.save()
            models.GameLog.write(
                game_id=game.id,
                guild_id=guild_id,
                message=(
                    f'{requester_description} extended the pending-game '
                    f'deadline from {old_expiration} to {new_expiration}.'
                ),
            )
            return GameExtensionResult(
                game_id=game.id,
                old_expiration=old_expiration,
                new_expiration=new_expiration,
            )


async def run_pending_game_extension(
    game_id: int,
    guild_id: int,
    requester_description: str,
) -> GameExtensionResult:
    """Submit one extension to the bounded ordinary-game executor."""

    loop = asyncio.get_running_loop()
    call = functools.partial(
        extend_pending_game,
        game_id,
        guild_id,
        requester_description,
    )
    return await loop.run_in_executor(_game_write_executor, call)


def unstart_game(
    game_id: int,
    guild_id: int,
    requester_description: str,
    invoked_with: str,
    invocation_channel_id: int | None = None,
    now: datetime.datetime | None = None,
) -> GameUnstartResult:
    """Return one started game to pending state in a local transaction."""

    now = now or datetime.datetime.now()
    with models.db.connection_context():
        with models.db.atomic():
            try:
                game = models.Game.get_by_id(game_id)
            except peewee.DoesNotExist as exc:
                raise GameUnstartValidationError(
                    f'Game with ID {game_id} cannot be found.'
                ) from exc
            if game.guild_id != guild_id:
                raise GameUnstartValidationError(
                    f'Game with ID {game_id} is associated with a different '
                    'Discord server.'
                )
            if game.is_completed or game.is_confirmed:
                raise GameUnstartValidationError(
                    f'Game {game.id} is marked as completed already.'
                )
            if game.is_pending:
                raise GameUnstartValidationError(
                    f'Game {game.id} is already a pending matchmaking '
                    'session.'
                )

            gamesides = tuple(game.gamesides)
            channel_targets = []
            for gameside in gamesides:
                if gameside.team_chan:
                    channel_targets.append(GameChannelTarget(
                        gameside_id=gameside.id,
                        channel_id=gameside.team_chan,
                        guild_id=(
                            gameside.team_chan_external_server or guild_id
                        ),
                    ))
            if game.game_chan:
                channel_targets.append(GameChannelTarget(
                    gameside_id=None,
                    channel_id=game.game_chan,
                    guild_id=guild_id,
                ))
            if (
                invocation_channel_id is not None
                and any(
                    target.channel_id == invocation_channel_id
                    for target in channel_targets
                )
            ):
                raise GameUnstartValidationError(
                    'This command must be used from a channel that is not '
                    'related to the game.'
                )

            tomorrow = now + datetime.timedelta(hours=24)
            if game.expiration is None or game.expiration < tomorrow:
                game.expiration = tomorrow
            game.is_pending = True
            game.save()
            models.GameLog.write(
                game_id=game.id,
                guild_id=guild_id,
                message=(
                    f'{requester_description} changed in-progress game to '
                    f'an open game. (`{invoked_with}`)'
                ),
            )
            return GameUnstartResult(
                game_id=game.id,
                game_name=game.name,
                announcement_channel_id=game.announcement_channel,
                announcement_message_id=game.announcement_message,
                mentions=tuple(game.mentions()),
                channel_targets=tuple(channel_targets),
                new_expiration=game.expiration,
            )


async def run_game_unstart(
    game_id: int,
    guild_id: int,
    requester_description: str,
    invoked_with: str,
    invocation_channel_id: int | None = None,
) -> GameUnstartResult:
    """Submit one unstart transition to the bounded game-write executor."""

    loop = asyncio.get_running_loop()
    call = functools.partial(
        unstart_game,
        game_id,
        guild_id,
        requester_description,
        invoked_with,
        invocation_channel_id,
    )
    return await loop.run_in_executor(_game_write_executor, call)


def clear_deleted_game_channels(
    game_id: int,
    guild_id: int,
    deleted_targets: tuple[GameChannelTarget, ...],
) -> int:
    """Clear channel references after their Discord channels were deleted."""

    cleared = 0
    with models.db.connection_context():
        with models.db.atomic():
            game = models.Game.get_by_id(game_id)
            if game.guild_id != guild_id:
                raise GameUnstartValidationError(
                    f'Game with ID {game_id} is associated with a different '
                    'Discord server.'
                )
            for target in deleted_targets:
                if target.gameside_id is None:
                    if game.game_chan == target.channel_id:
                        game.game_chan = None
                        game.save()
                        cleared += 1
                    continue
                gameside = models.GameSide.get_by_id(target.gameside_id)
                if (
                    gameside.game_id == game_id
                    and gameside.team_chan == target.channel_id
                ):
                    gameside.team_chan = None
                    gameside.team_chan_external_server = None
                    gameside.save()
                    cleared += 1
    return cleared


async def run_deleted_channel_reconciliation(
    game_id: int,
    guild_id: int,
    deleted_targets: tuple[GameChannelTarget, ...],
) -> int:
    """Reconcile successful post-commit Discord channel deletions."""

    loop = asyncio.get_running_loop()
    call = functools.partial(
        clear_deleted_game_channels,
        game_id,
        guild_id,
        deleted_targets,
    )
    return await loop.run_in_executor(_game_write_executor, call)
