"""Bounded synchronous workers for ordinary game database mutations."""

from __future__ import annotations

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

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


_new_game_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='polybot-newgame',
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
    return await loop.run_in_executor(_new_game_executor, call)
