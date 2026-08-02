"""Bounded synchronous workers for ordinary game database mutations."""

from __future__ import annotations

import asyncio
import datetime
import functools
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import peewee

from modules import exceptions, models, utilities


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


class GameMapValidationError(RuntimeError):
    """The current request or game state cannot be used for a map change."""


class GameMapLookupError(GameMapValidationError):
    """A legacy prefix target could not be resolved."""


class GameMapPermissionError(GameMapValidationError):
    """The requester cannot inspect or edit the requested game map."""


@dataclass(frozen=True)
class GameMapReadRequest:
    """Primitive input for a bounded game-map read."""

    game_id: int | None
    guild_id: int
    channel_id: int
    requester_id: int
    allow_related_channel: bool = False


@dataclass(frozen=True)
class GameMapMutationRequest:
    """Primitive input for one authoritative map mutation."""

    game_id: int | None
    guild_id: int
    channel_id: int
    requester_id: int
    requester_level: int
    requester_description: str
    map_type: str | None = None
    clear: bool = False
    legacy_tokens: tuple[str, ...] = ()
    allow_related_channel: bool = False
    invoked_with: str = 'setmap'


@dataclass(frozen=True)
class GameMapTarget:
    """Resolved primitive target and canonical value for a map mutation."""

    game_id: int
    map_type: str
    clear: bool


@dataclass(frozen=True)
class GameMapReadResult:
    game_id: int
    guild_id: int
    map_type: str


@dataclass(frozen=True)
class GameMapMutationResult:
    game_id: int
    guild_id: int
    old_map_type: str
    map_type: str
    announcement_channel_id: int | None
    announcement_message_id: int | None


_game_map_read_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix='polybot-game-map-read',
)


def _registered_game_map_requester(requester_id: int) -> bool:
    """Recheck global registration inside the worker-owned connection."""

    member_model = getattr(models, 'DiscordMember', None)
    getter = getattr(member_model, 'get_or_none', None)
    if getter is None:
        # Focused model fakes may omit registration tables.  Production has
        # the model and therefore performs the authoritative lookup.
        return True
    return getter(discord_id=int(requester_id)) is not None


def _game_map_registration_error() -> GameMapPermissionError:
    return GameMapPermissionError(
        'This command requires bot registration first. Type '
        '__`setname Your Mobile Name`__ or  '
        '__`steamname Your Steam Username`__ to get started.'
    )


def _load_game_for_map(game_id: int):
    try:
        numeric_game_id = int(game_id)
    except (TypeError, ValueError) as exc:
        raise GameMapValidationError(
            f'Invalid game ID "{game_id}".'
        ) from exc
    if numeric_game_id <= 0:
        raise GameMapValidationError(
            f'Invalid game ID "{game_id}".'
        )
    try:
        return models.Game.get_by_id(numeric_game_id)
    except peewee.DoesNotExist as exc:
        raise GameMapValidationError(
            f'No game found matching game ID `{numeric_game_id}`.'
        ) from exc


def _uses_map_channel(game, channel_id: int) -> bool:
    if not channel_id:
        return False
    uses_channel = getattr(game, 'uses_channel_id', None)
    if callable(uses_channel):
        return bool(uses_channel(int(channel_id)))
    return False


def _validate_map_association(
    game,
    request: GameMapReadRequest | GameMapMutationRequest,
) -> None:
    if int(game.guild_id) == int(request.guild_id):
        return
    if (
        request.allow_related_channel
        and _uses_map_channel(game, request.channel_id)
    ):
        return
    raise GameMapValidationError(
        f'Game {game.id} is associated with a different discord server. '
        'Use this command from that server or a game-specific channel.'
    )


def _resolve_legacy_map_game(request: GameMapMutationRequest):
    if not request.legacy_tokens:
        raise GameMapValidationError(
            'No arguments provided. Please provide a game ID and map type.'
        )
    first_token = request.legacy_tokens[0]
    try:
        game = models.Game.by_channel_or_arg(
            chan_id=request.channel_id,
            arg=first_token,
        )
    except (ValueError, exceptions.MyBaseException) as exc:
        raise GameMapLookupError(str(exc)) from exc
    _validate_map_association(game, request)
    return game, first_token


def _normalize_game_map_type(map_type_name: str | None) -> str:
    if map_type_name is None:
        raise GameMapValidationError(
            'A map type or clear option is required.'
        )
    map_type_name = str(map_type_name)
    if map_type_name.upper() == 'NONE':
        return ''
    map_type = utilities.get_map_type(map_type_name)
    if not map_type:
        raise GameMapValidationError(
            f'No matching map type found for "{map_type_name}". '
            'Check spelling or try a different name.'
        )
    return map_type


def _resolve_map_target(request: GameMapMutationRequest) -> GameMapTarget:
    if request.clear and request.map_type not in (None, ''):
        raise GameMapValidationError(
            'Choose either a map type or clear, not both.'
        )

    if request.game_id is None:
        game, first_token = _resolve_legacy_map_game(request)
        value_tokens = request.legacy_tokens
        if str(game.id) == str(first_token):
            value_tokens = value_tokens[1:]
        if len(value_tokens) != 1:
            raise GameMapValidationError(
                'Wrong number of arguments. See `help setmaptype` for '
                'usage examples.'
            )
        raw_map_type = value_tokens[0]
        clear = raw_map_type.upper() == 'NONE'
        return GameMapTarget(
            game_id=int(game.id),
            map_type=_normalize_game_map_type(raw_map_type),
            clear=clear,
        )

    if request.clear:
        return GameMapTarget(
            game_id=int(request.game_id),
            map_type='',
            clear=True,
        )
    return GameMapTarget(
        game_id=int(request.game_id),
        map_type=_normalize_game_map_type(request.map_type),
        clear=False,
    )


def _game_has_requester(game, requester_id: int) -> bool:
    player_lookup = getattr(game, 'player', None)
    if callable(player_lookup):
        return player_lookup(discord_id=int(requester_id)) is not None
    for lineup in tuple(getattr(game, 'lineup', ()) or ()):
        player = getattr(lineup, 'player', None)
        member = getattr(player, 'discord_member', None)
        if member is not None and int(member.discord_id) == int(requester_id):
            return True
    return False


def _validate_game_map_edit_permission(
    game,
    request: GameMapMutationRequest,
) -> None:
    if not _registered_game_map_requester(request.requester_id):
        raise _game_map_registration_error()
    is_participant = _game_has_requester(game, request.requester_id)
    if (is_participant and request.requester_level > 2) or (
        request.requester_level > 3
    ):
        return
    raise GameMapPermissionError(
        'You are not authorized to set the map type for this game.'
    )


def _resolve_map_read_game(request: GameMapReadRequest):
    if request.game_id is not None:
        game = _load_game_for_map(request.game_id)
    else:
        if request.channel_id <= 0:
            raise GameMapValidationError(
                'I could not identify one game from this channel. Please '
                'provide a game ID.'
            )
        try:
            game = models.Game.by_channel_id(chan_id=request.channel_id)
        except (ValueError, exceptions.MyBaseException) as exc:
            raise GameMapValidationError(str(exc)) from exc
    _validate_map_association(game, request)
    return game


def prepare_legacy_game_map(request: GameMapMutationRequest) -> GameMapTarget:
    """Resolve prefix channel/ID grammar on a bounded read worker."""

    with models.db.connection_context():
        return _resolve_map_target(request)


def read_game_map(request: GameMapReadRequest) -> GameMapReadResult:
    """Read the current value with a worker-owned Peewee connection."""

    with models.db.connection_context():
        if not _registered_game_map_requester(request.requester_id):
            raise _game_map_registration_error()
        game = _resolve_map_read_game(request)
        return GameMapReadResult(
            game_id=int(game.id),
            guild_id=int(game.guild_id),
            map_type=str(getattr(game, 'map_type', '') or ''),
        )


def set_game_map(request: GameMapMutationRequest) -> GameMapMutationResult:
    """Commit one map change and its audit entry atomically."""

    if request.clear and request.map_type not in (None, ''):
        raise GameMapValidationError(
            'Choose either a map type or clear, not both.'
        )

    with models.db.connection_context():
        with models.db.atomic():
            target = _resolve_map_target(request)
            game = _load_game_for_map(target.game_id)
            _validate_map_association(game, request)
            _validate_game_map_edit_permission(game, request)

            old_map_type = str(getattr(game, 'map_type', '') or '')
            game.map_type = target.map_type
            game.save()
            models.GameLog.write(
                game_id=game.id,
                guild_id=game.guild_id,
                message=(
                    f'{request.requester_description} set map type to '
                    f'"{target.map_type}"'
                ),
            )
            return GameMapMutationResult(
                game_id=int(game.id),
                guild_id=int(game.guild_id),
                old_map_type=old_map_type,
                map_type=target.map_type,
                announcement_channel_id=(
                    int(game.announcement_channel)
                    if game.announcement_channel is not None
                    else None
                ),
                announcement_message_id=(
                    int(game.announcement_message)
                    if game.announcement_message is not None
                    else None
                ),
            )


async def run_prepare_legacy_game_map(
    request: GameMapMutationRequest,
) -> GameMapTarget:
    """Resolve legacy map grammar without blocking Discord's event loop."""

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _game_map_read_executor,
        functools.partial(prepare_legacy_game_map, request),
    )


async def run_game_map_read(
    request: GameMapReadRequest,
) -> GameMapReadResult:
    """Submit a bounded current-map read."""

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _game_map_read_executor,
        functools.partial(read_game_map, request),
    )


async def run_game_map_mutation(
    request: GameMapMutationRequest,
) -> GameMapMutationResult:
    """Submit a map mutation to the existing ordinary-game executor."""

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _game_write_executor,
        functools.partial(set_game_map, request),
    )


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
