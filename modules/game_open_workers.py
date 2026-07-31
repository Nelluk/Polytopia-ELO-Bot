"""Bounded workers for atomic pending-game creation."""

from __future__ import annotations

import asyncio
import datetime
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import peewee

import settings
from modules import exceptions, models


logger = logging.getLogger('polybot.' + __name__)


class OpenGameSizeError(ValueError):
    """The supplied open-game size is not a supported shape."""


class OpenGameValidationError(RuntimeError):
    """The mutable state no longer permits pending-game creation."""


@dataclass(frozen=True)
class OpenGameSide:
    """Primitive side configuration crossing into the worker."""

    size: int
    required_role_id: int | None = None
    required_role_name: str | None = None


@dataclass(frozen=True)
class OpenGameRequest:
    """Immutable Discord-independent input for one open-game attempt."""

    guild_id: int
    requester_id: int
    requester_name: str
    requester_nick: str | None
    prefix: str
    requester_role_ids: tuple[int, ...]
    requester_role_names: tuple[str, ...]
    requester_level: int
    requester_is_mod: bool
    requester_is_staff: bool
    sides: tuple[OpenGameSide, ...]
    expiration_hours: int
    is_ranked: bool
    is_mobile: bool
    notes: str
    notes_display: str
    requester_description: str
    invoked_with: str
    role_lock_message: str = ''
    size_display: str | None = None
    log_notes_display: str | None = None

    @property
    def size_string(self) -> str:
        return self.size_display or 'v'.join(
            str(side.size) for side in self.sides
        )


@dataclass(frozen=True)
class OpenGameResult:
    """Immutable post-commit data needed for Discord effects."""

    game_id: int
    guild_id: int
    requester_id: int
    host_name: str
    size: tuple[int, ...]
    expiration_hours: int
    is_ranked: bool
    is_mobile: bool
    notes_display: str
    warnings: tuple[str, ...]
    role_locks: tuple[OpenGameSide, ...]
    size_display: str | None = None

    @property
    def size_string(self) -> str:
        return self.size_display or 'v'.join(str(size) for size in self.size)


def parse_game_size_token(
    token: str,
    *,
    max_game_size: int | None = None,
) -> tuple[tuple[int, ...], str]:
    """Parse an existing ``v``/``vs``/``FFA`` open-game shape."""

    token = str(token).strip()
    lower_token = token.lower()
    size_match = re.fullmatch(r"\d+(?:(v|vs)\d+)+", lower_token)
    if size_match:
        sizes = tuple(
            int(value) for value in re.findall(r'\d+', lower_token)
        )
        if min(sizes) < 1:
            raise OpenGameSizeError(
                f'Invalid game size **{token}**: Each side must have at '
                'least 1 player.'
            )
        normalized = 'v'.join(str(size) for size in sizes)
    else:
        ffa_match = re.fullmatch(r"(\d+)ffa", lower_token)
        if not ffa_match:
            raise OpenGameSizeError(
                f'Invalid game size **{token}**. Use a shape such as 1v1 '
                'or 6FFA.'
            )
        player_count = int(ffa_match.group(1))
        if player_count < 2:
            raise OpenGameSizeError(
                f'Invalid game size **{token}**: There must be at least 2 '
                'sides.'
            )
        sizes = tuple(1 for _ in range(player_count))
        normalized = 'v'.join(str(size) for size in sizes)

    if max_game_size is None:
        max_game_size = settings.max_game_size
    if sum(sizes) > max_game_size:
        raise OpenGameSizeError(
            f'Invalid game size **{token}**: Games can have a maximum of '
            f'{max_game_size} players.'
        )
    return sizes, normalized


def default_expiration_hours(total_players: int) -> int:
    """Return the legacy default expiration for an open-game shape."""

    if total_players < 4:
        return 24
    if total_players < 6:
        return 48
    return 96


def _team_for_roles(
    *,
    guild_id: int,
    role_names: tuple[str, ...],
):
    """Reload the host's current team from primitive Discord role names."""

    if not role_names:
        return None
    teams = models.Team.select().where(
        (models.Team.guild_id == guild_id)
        & models.Team.name.in_(role_names)
    )
    for team in teams:
        if team.name in role_names:
            return team
    return None


def create_open_game(request: OpenGameRequest) -> OpenGameResult:
    """Create the complete pending-game graph in one local transaction."""

    with models.db.connection_context():
        with models.db.atomic():
            if (
                request.guild_id == 814317488418193478
                and not request.requester_is_staff
            ):
                raise OpenGameValidationError(
                    'For **The Polympics** only server staff may open games.'
                )
            team_sizes = tuple(side.size for side in request.sides)
            if (
                len(team_sizes) < 2
                or any(size < 1 for size in team_sizes)
                or sum(team_sizes) > settings.max_game_size
            ):
                raise OpenGameValidationError(
                    'Invalid game size. Include a shape such as 1v1 or 6FFA.'
                )
            if not 1 <= request.expiration_hours <= 168:
                raise OpenGameValidationError(
                    'Invalid expiration. Must be between 1H and 168H.'
                )
            try:
                host, _ = models.Player.get_by_discord_id(
                    discord_id=request.requester_id,
                    discord_name=request.requester_name,
                    discord_nick=request.requester_nick,
                    guild_id=request.guild_id,
                )
            except peewee.PeeweeException:
                raise

            if host is None:
                raise OpenGameValidationError(
                    'You must be a registered player before hosting a match. '
                    f'Try `{request.prefix}setname Your Mobile Name`'
                )

            host_team = _team_for_roles(
                guild_id=request.guild_id,
                role_names=request.requester_role_names,
            )
            if settings.guild_setting(request.guild_id, 'require_teams') and not host_team:
                raise OpenGameValidationError(
                    'You must join a Team in order to participate in games '
                    'on this server.'
                )

            max_open = max(1, request.requester_level * 3)
            if request.requester_level > 5:
                max_open = 75
            open_count = (
                models.Game.select()
                .where(
                    (models.Game.host == host)
                    & (models.Game.is_pending == 1)
                )
                .count()
            )
            if open_count >= max_open:
                raise OpenGameValidationError(
                    f'You have too many open games already (max of '
                    f'{max_open}). Try using `{request.prefix}delete` on an '
                    'existing one.'
                )

            if request.is_mobile and not host.discord_member.polytopia_name:
                raise OpenGameValidationError(
                    f'**{host.name}** does not have a mobile name on file. '
                    f'Use `{request.prefix}setname` to set one, or try '
                    f'`{request.prefix}opensteam` for a Steam game.'
                )
            if not request.is_mobile and not host.discord_member.name_steam:
                raise OpenGameValidationError(
                    f'**{host.name}** does not have a Steam username on file '
                    f'and this is a Steam game 🖥. Use `{request.prefix}'
                    f'steamname` to set one, or try `{request.prefix}opengame` '
                    'for a Mobile game.'
                )

            total_players = sum(side.size for side in request.sides)
            game_allowed, join_error_message = settings.can_user_join_game(
                user_level=request.requester_level,
                game_size=total_players,
                is_ranked=request.is_ranked,
                is_host=True,
            )
            if not game_allowed:
                raise OpenGameValidationError(join_error_message)

            if not settings.guild_setting(
                request.guild_id,
                'allow_uneven_teams',
            ) and not all(size == team_sizes[0] for size in team_sizes):
                raise OpenGameValidationError(
                    'Uneven team games are not allowed on this server.'
                )

            warnings: list[str] = []
            server_size_max = settings.guild_setting(
                request.guild_id,
                'max_team_size',
            )
            if max(team_sizes) > server_size_max:
                if (
                    settings.guild_setting(
                        request.guild_id,
                        'allow_uneven_teams',
                    )
                    and min(team_sizes) <= server_size_max
                ):
                    warnings.append(':warning: Team sizes are uneven.')
                elif request.requester_is_mod:
                    warnings.append('Moderator over-riding server size limits')
                elif not request.is_ranked and max(team_sizes) <= server_size_max + 1:
                    logger.info(
                        'Opening unranked game that exceeds server_size_max'
                    )
                else:
                    raise OpenGameValidationError(
                        f'Maximum ranked team size on this server is '
                        f'{server_size_max}. Maximum team size for an '
                        f'unranked game is {server_size_max + 1}.'
                    )

            host.team = host_team
            host.save()

            expiration = datetime.datetime.now() + datetime.timedelta(
                hours=request.expiration_hours
            )
            game = models.Game.create(
                host=host,
                expiration=expiration,
                notes=request.notes,
                guild_id=request.guild_id,
                is_pending=True,
                is_ranked=request.is_ranked,
                size=list(team_sizes),
                is_mobile=request.is_mobile,
            )

            for position, side in enumerate(request.sides, start=1):
                preset_team = None
                if side.required_role_name:
                    try:
                        preset_team = models.Team.get_or_except(
                            team_name=side.required_role_name,
                            guild_id=request.guild_id,
                            require_exact=True,
                        )
                    except exceptions.NoSingleMatch:
                        preset_team = None
                models.GameSide.create(
                    game=game,
                    size=side.size,
                    position=position,
                    required_role_id=side.required_role_id,
                    sidename=side.required_role_name,
                    team=preset_team,
                )

            first_side, _ = game.first_open_side(
                roles=list(request.requester_role_ids)
            )
            if not first_side:
                if request.requester_level >= 4:
                    warnings.append(
                        ':warning: All sides in this game are locked to a '
                        "specific @Role - and you don't have any of those "
                        'roles. You are not a player in this game.'
                    )
                else:
                    raise OpenGameValidationError(
                        ':warning All sides in this game are locked to a '
                        "specific @Role - and you don't have any of those "
                        'roles. Game not created.'
                    )
            else:
                models.Lineup.create(
                    player=host,
                    game=game,
                    gameside=first_side,
                )
                if first_side.position > 1:
                    warnings.append(
                        ':warning: You are not joined to side 1, due to the '
                        'ordering of the role restrictions. Therefore you '
                        'will not be the game host.'
                    )

            models.GameLog.write(
                game_id=game.id,
                guild_id=request.guild_id,
                message=(
                    f'{request.requester_description} opened new '
                    f'{request.size_string} game. Notes: '
                    f'*{request.log_notes_display or request.notes_display}*'
                ),
            )

            if request.role_lock_message:
                warnings.insert(0, request.role_lock_message)

            return OpenGameResult(
                game_id=game.id,
                guild_id=request.guild_id,
                requester_id=request.requester_id,
                host_name=host.name,
                size=team_sizes,
                expiration_hours=request.expiration_hours,
                is_ranked=request.is_ranked,
                is_mobile=request.is_mobile,
                notes_display=request.notes_display,
                warnings=tuple(warnings),
                role_locks=request.sides,
                size_display=request.size_display,
            )


class PendingGameCoordinator:
    """Serialize pending-game workers and track real thread completion."""

    def __init__(self, *, max_workers: int = 1):
        self.executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix='polybot-pending-game',
        )
        self._active = 0
        self._lock = threading.Lock()

    @property
    def active_count(self) -> int:
        with self._lock:
            return self._active

    def _reserve(self) -> None:
        with self._lock:
            self._active += 1

    def _release(self, _future) -> None:
        with self._lock:
            self._active -= 1

    async def run(self, request: OpenGameRequest) -> OpenGameResult:
        self._reserve()
        try:
            future = self.executor.submit(create_open_game, request)
        except BaseException:
            self._release(None)
            raise
        released = threading.Event()

        def release(finished):
            try:
                self._release(finished)
            finally:
                released.set()

        future.add_done_callback(release)
        # Do not chain cancellation to the concurrent future. A cancelled
        # interaction may stop awaiting, but the database worker keeps its
        # connection/transaction lifecycle and releases the slot only after
        # the thread really finishes.
        while not future.done() or not released.is_set():
            # Polling avoids depending on a cross-thread event-loop wakeup
            # while retaining a responsive loop and leaving the concurrent
            # future entirely owned by the worker thread.
            await asyncio.sleep(0.001)
        return future.result()


pending_game_coordinator = PendingGameCoordinator()


async def run_open_game_creation(request: OpenGameRequest) -> OpenGameResult:
    """Run one atomic pending-game creation through the coordinator."""

    return await pending_game_coordinator.run(request)
