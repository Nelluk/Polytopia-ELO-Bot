"""Bounded synchronous workers for ordinary game database mutations."""

from __future__ import annotations

import asyncio
import datetime
import functools
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import discord
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


class GameNotesValidationError(RuntimeError):
    """The current request or game state cannot be used for notes."""


class GameNotesLookupError(GameNotesValidationError):
    """A legacy notes target could not be resolved."""


class GameNotesPermissionError(GameNotesValidationError):
    """The requester cannot inspect or edit the requested game's notes."""


class GameNotesConflictError(GameNotesValidationError):
    """The immutable notes workspace was opened from stale state."""


@dataclass(frozen=True)
class GameNotesReadRequest:
    """Primitive input for a bounded current-notes read."""

    game_id: int | None
    guild_id: int
    channel_id: int
    requester_id: int
    allow_related_channel: bool = False
    legacy_tokens: tuple[str, ...] = ()


@dataclass(frozen=True)
class GameNotesMutationRequest:
    """Primitive input for one authoritative notes mutation."""

    game_id: int | None
    guild_id: int
    channel_id: int
    requester_id: int
    requester_level: int
    requester_is_staff: bool
    requester_description: str
    notes: str | None = None
    clear: bool = False
    expected_notes: str | None = None
    check_expected_notes: bool = False
    legacy_tokens: tuple[str, ...] = ()
    allow_related_channel: bool = False
    invoked_with: str = 'gamenotes'
    prefix: str = '$'
    truncate: bool = False
    legacy_none: bool = False
    mention_warning: bool = False


@dataclass(frozen=True)
class GameNotesTarget:
    """Resolved primitive target for a legacy notes mutation."""

    game_id: int


@dataclass(frozen=True)
class GameNotesReadResult:
    game_id: int
    guild_id: int
    notes: str | None
    is_pending: bool = False
    is_completed: bool = False
    host_discord_id: int | None = None
    announcement_channel_id: int | None = None
    announcement_message_id: int | None = None


@dataclass(frozen=True)
class GameNotesMutationResult:
    game_id: int
    guild_id: int
    old_notes: str | None
    notes: str | None
    cleared: bool = False
    mention_warning: bool = False
    is_pending: bool = False
    is_completed: bool = False
    announcement_channel_id: int | None = None
    announcement_message_id: int | None = None


class GameNameValidationError(RuntimeError):
    """The current request or game state cannot be used for a name change."""


class GameNameLookupError(GameNameValidationError):
    """A legacy prefix target could not be resolved."""


class GameNamePermissionError(GameNameValidationError):
    """The requester cannot inspect or edit the requested game name."""


class GameNameConflictError(GameNameValidationError):
    """The immutable name workspace was opened from stale state."""


@dataclass(frozen=True)
class GameNameReadRequest:
    """Primitive input for a bounded current-name read."""

    game_id: int
    guild_id: int
    channel_id: int
    requester_id: int
    allow_related_channel: bool = False


@dataclass(frozen=True)
class GameNameMutationRequest:
    """Primitive input for one authoritative game-name mutation."""

    game_id: int | None
    guild_id: int
    channel_id: int
    requester_id: int
    requester_level: int
    requester_is_staff: bool
    requester_description: str
    name: str | None = None
    clear: bool = False
    expected_name: str | None = None
    check_expected_name: bool = False
    legacy_tokens: tuple[str, ...] = ()
    allow_related_channel: bool = False
    invoked_with: str = 'rename'
    prefix: str = '$'


@dataclass(frozen=True)
class GameNameTarget:
    """Resolved primitive target for a legacy name mutation."""

    game_id: int
    inferred_from_channel: bool
    explicit_game_id: int | None = None


@dataclass(frozen=True)
class GameNameReadResult:
    game_id: int
    guild_id: int
    name: str | None
    is_pending: bool = False
    is_completed: bool = False
    announcement_channel_id: int | None = None
    announcement_message_id: int | None = None


@dataclass(frozen=True)
class GameNameMutationResult:
    game_id: int
    guild_id: int
    old_name: str | None
    name: str | None
    requested_name: str | None
    cleared: bool = False
    normalized: bool = False
    truncated: bool = False
    name_warning: str | None = None
    league_warning: str = ''
    is_pending: bool = False
    is_completed: bool = False
    announcement_channel_id: int | None = None
    announcement_message_id: int | None = None


_game_map_read_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix='polybot-game-map-read',
)

_game_notes_read_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix='polybot-game-notes-read',
)

_game_name_read_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix='polybot-game-name-read',
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
    """Submit a map mutation and drain a canceled caller safely."""

    loop = asyncio.get_running_loop()
    call = functools.partial(set_game_map, request)
    concurrent_future = _game_write_executor.submit(call)
    future = asyncio.wrap_future(concurrent_future, loop=loop)
    completed = asyncio.Event()
    concurrent_future.add_done_callback(
        lambda _future: loop.call_soon_threadsafe(completed.set)
    )
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        # A running thread cannot be cancelled. Keep the game claim held
        # until its transaction actually finishes, including repeated
        # cancellation requests from shutdown or another caller.
        task = asyncio.current_task()
        if task is not None:
            task.uncancel()
        while not completed.is_set():
            try:
                await completed.wait()
            except asyncio.CancelledError:
                if task is not None:
                    task.uncancel()
        concurrent_future.result()
        raise asyncio.CancelledError


def _registered_game_notes_requester(requester_id: int) -> bool:
    """Recheck registration inside the worker-owned connection."""

    member_model = getattr(models, 'DiscordMember', None)
    getter = getattr(member_model, 'get_or_none', None)
    if getter is None:
        # Focused model fakes may omit registration tables. Production has the
        # model and therefore performs the authoritative lookup.
        return True
    return getter(discord_id=int(requester_id)) is not None


def _game_notes_registration_error() -> GameNotesPermissionError:
    return GameNotesPermissionError(
        'This command requires bot registration first. Type '
        '__`setname Your Mobile Name`__ or  '
        '__`steamname Your Steam Username`__ to get started.'
    )


def _load_game_for_notes(game_id: int):
    try:
        numeric_game_id = int(game_id)
    except (TypeError, ValueError) as exc:
        raise GameNotesValidationError(
            f'Invalid Game ID "{game_id}".'
        ) from exc
    if numeric_game_id <= 0:
        raise GameNotesValidationError(
            f'Game with ID {numeric_game_id} cannot be found.'
        )
    try:
        return models.Game.get_by_id(numeric_game_id)
    except peewee.DoesNotExist as exc:
        raise GameNotesValidationError(
            f'Game with ID {numeric_game_id} cannot be found.'
        ) from exc


def _uses_notes_channel(game, channel_id: int) -> bool:
    if not channel_id:
        return False
    uses_channel = getattr(game, 'uses_channel_id', None)
    if callable(uses_channel):
        return bool(uses_channel(int(channel_id)))
    return False


def _validate_notes_association(
    game,
    request: GameNotesReadRequest | GameNotesMutationRequest,
) -> None:
    if int(game.guild_id) == int(request.guild_id):
        return
    if (
        request.allow_related_channel
        and _uses_notes_channel(game, request.channel_id)
    ):
        return
    raise GameNotesValidationError(
        f'Game {game.id} is associated with a different discord server. '
        'Use this command from that server or a game-specific channel.'
    )


def _resolve_legacy_notes_game(request: GameNotesReadRequest | GameNotesMutationRequest):
    tokens = tuple(request.legacy_tokens or ())
    first_token = tokens[0] if tokens else None
    try:
        game = models.Game.by_channel_or_arg(
            chan_id=request.channel_id,
            arg=first_token,
        )
    except (ValueError, exceptions.MyBaseException) as exc:
        raise GameNotesLookupError(str(exc)) from exc
    _validate_notes_association(game, request)
    return game


def _resolve_notes_game(
    request: GameNotesReadRequest | GameNotesMutationRequest,
):
    if request.game_id is not None:
        game = _load_game_for_notes(request.game_id)
        _validate_notes_association(game, request)
        return game
    return _resolve_legacy_notes_game(request)


def _notes_host_id(game) -> int | None:
    host = getattr(game, 'host', None)
    member = getattr(host, 'discord_member', None) if host else None
    host_id = getattr(member, 'discord_id', None)
    if host_id is None:
        return None
    return int(host_id)


def _notes_is_host(game, requester_id: int) -> bool:
    hosted_by = getattr(game, 'is_hosted_by', None)
    if callable(hosted_by):
        try:
            return bool(hosted_by(int(requester_id))[0])
        except (AttributeError, IndexError, TypeError, ValueError):
            pass
    return _notes_host_id(game) == int(requester_id)


def _validate_game_notes_edit_permission(
    game,
    request: GameNotesMutationRequest,
) -> None:
    if not _registered_game_notes_requester(request.requester_id):
        raise _game_notes_registration_error()

    requester_is_staff = bool(
        request.requester_is_staff or request.requester_level >= 5
    )
    if not _notes_is_host(game, request.requester_id) and not requester_is_staff:
        raise GameNotesPermissionError(
            'Only the game host or server staff can do this.'
        )
    if bool(getattr(game, 'is_completed', False)):
        raise GameNotesValidationError(
            'This game is completed and notes cannot be edited.'
        )
    if not bool(getattr(game, 'is_pending', False)) and not requester_is_staff:
        raise GameNotesPermissionError(
            'Only server staff can edit notes of an in-progress game.'
        )


def _normalize_game_notes(request: GameNotesMutationRequest) -> str | None:
    if request.clear:
        if request.notes not in (None, ''):
            raise GameNotesValidationError(
                'Choose either new notes or clear, not both.'
            )
        return None

    if request.notes is None or request.notes == '':
        raise GameNotesValidationError(
            'A note is required. Use Clear notes to remove the current note.'
        )

    notes = str(request.notes)
    if request.legacy_none and notes.lower() == 'none':
        return None
    if len(notes) > 150:
        if not request.truncate:
            raise GameNotesValidationError(
                'Notes must be 150 characters or fewer.'
            )
        notes = notes[:150]
    return notes


def _check_expected_game_notes(
    game,
    request: GameNotesMutationRequest,
) -> None:
    if not request.check_expected_notes:
        return
    current_notes = str(getattr(game, 'notes', '') or '')
    expected_notes = str(request.expected_notes or '')
    if current_notes != expected_notes:
        raise GameNotesConflictError(
            'These notes changed after this workspace was opened. Run '
            '`/game notes` again and retry your edit.'
        )


def _optional_int(value) -> int | None:
    return int(value) if value is not None else None


def prepare_legacy_game_notes(
    request: GameNotesMutationRequest,
) -> GameNotesTarget:
    """Resolve legacy game/channel grammar on a bounded read worker."""

    with models.db.connection_context():
        game = _resolve_notes_game(request)
        return GameNotesTarget(game_id=int(game.id))


def read_game_notes(request: GameNotesReadRequest) -> GameNotesReadResult:
    """Read current notes and state with a worker-owned connection."""

    with models.db.connection_context():
        if not _registered_game_notes_requester(request.requester_id):
            raise _game_notes_registration_error()
        game = _resolve_notes_game(request)
        return GameNotesReadResult(
            game_id=int(game.id),
            guild_id=int(game.guild_id),
            notes=(
                str(game.notes)
                if getattr(game, 'notes', None) is not None
                else None
            ),
            is_pending=bool(getattr(game, 'is_pending', False)),
            is_completed=bool(getattr(game, 'is_completed', False)),
            host_discord_id=_notes_host_id(game),
            announcement_channel_id=_optional_int(
                getattr(game, 'announcement_channel', None)
            ),
            announcement_message_id=_optional_int(
                getattr(game, 'announcement_message', None)
            ),
        )


def set_game_notes(
    request: GameNotesMutationRequest,
) -> GameNotesMutationResult:
    """Commit one notes change and its audit entry atomically."""

    with models.db.connection_context():
        with models.db.atomic():
            game = _resolve_notes_game(request)
            _validate_game_notes_edit_permission(game, request)
            _check_expected_game_notes(game, request)
            old_notes = getattr(game, 'notes', None)
            new_notes = _normalize_game_notes(request)
            game.notes = new_notes
            game.save()
            models.GameLog.write(
                game_id=int(game.id),
                guild_id=int(request.guild_id),
                message=(
                    f'{request.requester_description} edited game notes: '
                    f'{new_notes}'
                ),
            )
            return GameNotesMutationResult(
                game_id=int(game.id),
                guild_id=int(game.guild_id),
                old_notes=(str(old_notes) if old_notes is not None else None),
                notes=new_notes,
                cleared=new_notes is None,
                mention_warning=bool(request.mention_warning),
                is_pending=bool(getattr(game, 'is_pending', False)),
                is_completed=bool(getattr(game, 'is_completed', False)),
                announcement_channel_id=_optional_int(
                    getattr(game, 'announcement_channel', None)
                ),
                announcement_message_id=_optional_int(
                    getattr(game, 'announcement_message', None)
                ),
            )


async def run_prepare_legacy_game_notes(
    request: GameNotesMutationRequest,
) -> GameNotesTarget:
    """Submit legacy target resolution to the notes read executor."""

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _game_notes_read_executor,
        functools.partial(prepare_legacy_game_notes, request),
    )


async def run_game_notes_read(
    request: GameNotesReadRequest,
) -> GameNotesReadResult:
    """Submit a bounded current-notes read."""

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _game_notes_read_executor,
        functools.partial(read_game_notes, request),
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


async def run_game_notes_mutation(
    request: GameNotesMutationRequest,
) -> GameNotesMutationResult:
    """Submit notes mutation and drain a canceled caller safely."""

    loop = asyncio.get_running_loop()
    call = functools.partial(set_game_notes, request)
    concurrent_future = _game_write_executor.submit(call)
    future = asyncio.wrap_future(concurrent_future, loop=loop)
    completed = asyncio.Event()
    concurrent_future.add_done_callback(
        lambda _future: loop.call_soon_threadsafe(completed.set)
    )
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        # A running synchronous transaction cannot be canceled. Preserve the
        # per-game claim until the transaction has actually finished, even if
        # shutdown delivers cancellation repeatedly.
        task = asyncio.current_task()
        if task is not None:
            task.uncancel()
        while not completed.is_set():
            try:
                await completed.wait()
            except asyncio.CancelledError:
                if task is not None:
                    task.uncancel()
        concurrent_future.result()
        raise asyncio.CancelledError


def _registered_game_name_requester(requester_id: int) -> bool:
    """Recheck global registration inside the worker-owned connection."""

    member_model = getattr(models, 'DiscordMember', None)
    getter = getattr(member_model, 'get_or_none', None)
    if getter is None:
        # Focused model fakes may omit registration tables. Production has the
        # model and therefore performs the authoritative lookup.
        return True
    return getter(discord_id=int(requester_id)) is not None


def _game_name_registration_error() -> GameNamePermissionError:
    return GameNamePermissionError(
        'This command requires bot registration first. Type '
        '__`setname Your Mobile Name`__ or  '
        '__`steamname Your Steam Username`__ to get started.'
    )


def _load_game_for_name(game_id: int):
    try:
        numeric_game_id = int(game_id)
    except (TypeError, ValueError) as exc:
        raise GameNameValidationError(
            f'Invalid game ID "{game_id}".'
        ) from exc
    if numeric_game_id <= 0:
        raise GameNameValidationError(
            f'Invalid game ID "{game_id}".'
        )
    try:
        return models.Game.get_by_id(numeric_game_id)
    except peewee.DoesNotExist as exc:
        raise GameNameValidationError(
            f'Game with ID {numeric_game_id} cannot be found.'
        ) from exc


def _uses_name_channel(game, channel_id: int) -> bool:
    if not channel_id:
        return False
    uses_channel = getattr(game, 'uses_channel_id', None)
    if callable(uses_channel):
        return bool(uses_channel(int(channel_id)))
    return False


def _validate_name_association(
    game,
    request: GameNameReadRequest | GameNameMutationRequest,
    *,
    allow_related_channel: bool | None = None,
) -> None:
    if int(game.guild_id) == int(request.guild_id):
        return
    if allow_related_channel is None:
        allow_related_channel = bool(request.allow_related_channel)
    if allow_related_channel and _uses_name_channel(game, request.channel_id):
        return
    raise GameNameValidationError(
        f'Game {game.id} is associated with a different discord server. '
        'Use this command from that server or a game-specific channel.'
    )


def _parse_legacy_name_game_id(token: str | None) -> int | None:
    if token is None:
        return None
    try:
        return int(str(token).strip('#'))
    except (TypeError, ValueError):
        return None


def _resolve_legacy_name_game(request: GameNameMutationRequest) -> GameNameTarget:
    tokens = tuple(request.legacy_tokens or ())
    first_token = tokens[0] if tokens else None
    explicit_game_id = _parse_legacy_name_game_id(first_token)

    if not tokens:
        raise GameNameValidationError(
            'No arguments provided. Please provide a game ID and new name.'
        )

    try:
        game = models.Game.by_channel_id(chan_id=request.channel_id)
    except exceptions.TooManyMatches as exc:
        raise GameNameLookupError(
            'Error looking up game based on current channel - please contact '
            'the bot owner.'
        ) from exc
    except exceptions.NoMatches:
        if explicit_game_id is None:
            raise GameNameLookupError(
                'No game was found for the current channel.'
            )
        game = _load_game_for_name(explicit_game_id)
        _validate_name_association(
            game,
            request,
            allow_related_channel=False,
        )
        return GameNameTarget(
            game_id=int(game.id),
            inferred_from_channel=False,
            explicit_game_id=explicit_game_id,
        )
    except (ValueError, exceptions.MyBaseException) as exc:
        raise GameNameLookupError(str(exc)) from exc

    _validate_name_association(
        game,
        request,
        allow_related_channel=True,
    )
    return GameNameTarget(
        game_id=int(game.id),
        inferred_from_channel=True,
        explicit_game_id=explicit_game_id,
    )


def _resolve_name_game(
    request: GameNameReadRequest | GameNameMutationRequest,
):
    if request.game_id is None:
        target = _resolve_legacy_name_game(request)
        game = _load_game_for_name(target.game_id)
        _validate_name_association(
            game,
            request,
            allow_related_channel=target.inferred_from_channel,
        )
        return game
    else:
        game = _load_game_for_name(request.game_id)
    _validate_name_association(game, request)
    return game


def _name_is_host(game, requester_id: int) -> bool:
    hosted_by = getattr(game, 'is_hosted_by', None)
    if callable(hosted_by):
        try:
            return bool(hosted_by(int(requester_id))[0])
        except (AttributeError, IndexError, TypeError, ValueError):
            pass
    host = getattr(game, 'host', None)
    member = getattr(host, 'discord_member', None) if host else None
    return getattr(member, 'discord_id', None) == int(requester_id)


def _name_creator(game):
    creator = getattr(game, 'creating_player', None)
    if not callable(creator):
        return None
    try:
        return creator()
    except (
        AttributeError,
        IndexError,
        TypeError,
        ValueError,
        peewee.PeeweeException,
    ):
        return None


def _name_is_creator(game, requester_id: int) -> bool:
    is_created_by = getattr(game, 'is_created_by', None)
    if callable(is_created_by):
        try:
            return bool(is_created_by(discord_id=int(requester_id)))
        except (
            AttributeError,
            IndexError,
            TypeError,
            ValueError,
            peewee.PeeweeException,
        ):
            pass
    creator = _name_creator(game)
    member = getattr(creator, 'discord_member', None) if creator else None
    return getattr(member, 'discord_id', None) == int(requester_id)


def _name_creator_display(game) -> str:
    creator = _name_creator(game)
    if creator is None:
        return 'the game creator'
    return str(getattr(creator, 'name', None) or 'the game creator')


def _validate_game_name_edit_permission(
    game,
    request: GameNameMutationRequest,
) -> None:
    if not _registered_game_name_requester(request.requester_id):
        raise _game_name_registration_error()

    if bool(getattr(game, 'is_pending', False)):
        raise GameNameValidationError('This game has not started yet.')

    if request.clear and request.requester_level <= 3:
        raise GameNamePermissionError(
            'You do not have permissions to delete a game name.'
        )

    requester_is_staff = bool(
        request.requester_is_staff or request.requester_level >= 5
    )
    if not (
        _name_is_host(game, request.requester_id)
        or requester_is_staff
        or _name_is_creator(game, request.requester_id)
    ):
        raise GameNamePermissionError(
            f'Only the game creator **{_name_creator_display(game)}** or '
            'server staff can do this.'
        )


def _check_expected_game_name(
    game,
    request: GameNameMutationRequest,
) -> None:
    if not request.check_expected_name:
        return
    current_name = str(getattr(game, 'name', None) or '')
    expected_name = str(request.expected_name or '')
    if current_name != expected_name:
        raise GameNameConflictError(
            'This game name changed after this workspace was opened. Run '
            '`/game name` again and retry your edit.'
        )


def prepare_legacy_game_name(
    request: GameNameMutationRequest,
) -> GameNameTarget:
    """Resolve legacy channel/ID grammar on a bounded read worker."""

    with models.db.connection_context():
        return _resolve_legacy_name_game(request)


def read_game_name(request: GameNameReadRequest) -> GameNameReadResult:
    """Read the current tracked name with a worker-owned connection."""

    with models.db.connection_context():
        if not _registered_game_name_requester(request.requester_id):
            raise _game_name_registration_error()
        game = _resolve_name_game(request)
        return GameNameReadResult(
            game_id=int(game.id),
            guild_id=int(game.guild_id),
            name=(
                str(game.name)
                if getattr(game, 'name', None) is not None
                else None
            ),
            is_pending=bool(getattr(game, 'is_pending', False)),
            is_completed=bool(getattr(game, 'is_completed', False)),
            announcement_channel_id=_optional_int(
                getattr(game, 'announcement_channel', None)
            ),
            announcement_message_id=_optional_int(
                getattr(game, 'announcement_message', None)
            ),
        )


def set_game_name(
    request: GameNameMutationRequest,
) -> GameNameMutationResult:
    """Commit the name, derived league fields, and audit entry atomically."""

    with models.db.connection_context():
        with models.db.atomic():
            game = _resolve_name_game(request)
            _validate_game_name_edit_permission(game, request)
            _check_expected_game_name(game, request)

            if request.clear and request.name not in (None, ''):
                raise GameNameValidationError(
                    'Choose either a new game name or Clear name, not both.'
                )
            requested_name = None if request.clear else request.name
            if not request.clear and requested_name in (None, ''):
                raise GameNameValidationError(
                    'A new game name is required. Use Clear name to remove it.'
                )

            name_warning = None
            if requested_name is not None and not utilities.is_valid_poly_gamename(
                input=str(requested_name),
            ):
                if request.requester_level <= 2:
                    raise GameNameValidationError(
                        'That name looks made up. :thinking: You need to '
                        'manually create the game __in Polytopia__, come back '
                        'and input the name of the new game you made.\n'
                        f'You can use `{request.prefix}code NAME` to get the '
                        'code of each player in this game.'
                    )
                name_warning = (
                    ':warning: That game name looks made up - you are '
                    'allowed to override due to your user level.'
                )

            old_name_value = getattr(game, 'name', None)
            old_name = (
                str(old_name_value)
                if old_name_value is not None
                else None
            )
            if request.clear:
                game.name = None
            else:
                game.name = str(requested_name)
            stored_name_value = getattr(game, 'name', None)
            stored_name = (
                str(stored_name_value)
                if stored_name_value is not None
                else None
            )
            normalized = (
                requested_name is not None
                and stored_name != str(requested_name)
            )
            truncated = bool(
                requested_name is not None
                and len(str(requested_name)) > 35
                and stored_name is not None
                and len(stored_name) < len(str(requested_name))
            )
            game.save()

            league_warning = ''
            if game.update_league_fields():
                league_warning = (
                    '\n:warning: Detected a difference in the season game '
                    'status. New status is:\nGame season: '
                    f'`{game.league_season}`, Team tier: '
                    f'`{game.league_tier}`,  Playoff game? '
                    f'`{game.league_playoff}`'
                )

            models.GameLog.write(
                game_id=int(game.id),
                guild_id=int(game.guild_id),
                message=(
                    f'{request.requester_description} renamed the game to *'
                    f'{discord.utils.escape_markdown(str(stored_name))}*'
                ),
            )

            return GameNameMutationResult(
                game_id=int(game.id),
                guild_id=int(game.guild_id),
                old_name=old_name,
                name=stored_name,
                requested_name=(
                    str(requested_name)
                    if requested_name is not None
                    else None
                ),
                cleared=stored_name is None,
                normalized=normalized,
                truncated=truncated,
                name_warning=name_warning,
                league_warning=league_warning,
                is_pending=bool(getattr(game, 'is_pending', False)),
                is_completed=bool(getattr(game, 'is_completed', False)),
                announcement_channel_id=_optional_int(
                    getattr(game, 'announcement_channel', None)
                ),
                announcement_message_id=_optional_int(
                    getattr(game, 'announcement_message', None)
                ),
            )


async def run_prepare_legacy_game_name(
    request: GameNameMutationRequest,
) -> GameNameTarget:
    """Submit legacy name target resolution to the bounded read executor."""

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _game_name_read_executor,
        functools.partial(prepare_legacy_game_name, request),
    )


async def run_game_name_read(
    request: GameNameReadRequest,
) -> GameNameReadResult:
    """Submit a bounded current-name read."""

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _game_name_read_executor,
        functools.partial(read_game_name, request),
    )


async def run_game_name_mutation(
    request: GameNameMutationRequest,
) -> GameNameMutationResult:
    """Submit a name mutation and drain a canceled caller safely."""

    loop = asyncio.get_running_loop()
    call = functools.partial(set_game_name, request)
    concurrent_future = _game_write_executor.submit(call)
    future = asyncio.wrap_future(concurrent_future, loop=loop)
    completed = asyncio.Event()
    concurrent_future.add_done_callback(
        lambda _future: loop.call_soon_threadsafe(completed.set)
    )
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        # A running synchronous transaction cannot be canceled. The caller
        # remains attached until the worker has finished so a keyed game
        # claim can be released only after the database transition drains.
        task = asyncio.current_task()
        if task is not None:
            task.uncancel()
        while not completed.is_set():
            try:
                await completed.wait()
            except asyncio.CancelledError:
                if task is not None:
                    task.uncancel()
        concurrent_future.result()
        raise asyncio.CancelledError


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
