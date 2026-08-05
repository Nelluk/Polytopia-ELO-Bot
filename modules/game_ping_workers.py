"""Bounded workers for the interactive game-ping notification workflow.

Only frozen primitive values cross the event-loop/worker boundary.  The
worker reloads the target players and games, rechecks the permission and
channel facts that can be represented without Discord objects, writes every
per-game audit row in one transaction, and returns a primitive post-commit
delivery plan.  Discord sends are deliberately owned by ``game_ping``.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import functools
import logging
from urllib.parse import urlparse

import peewee

from modules import models, utilities


logger = logging.getLogger('polybot.' + __name__)


MAX_GAMES = 50
MAX_GAME_CHOICES = 25
MAX_PARTICIPANTS_PER_GAME = 16
MAX_DESTINATIONS = 256
MAX_TEXT_SECTION_LENGTH = 4_000
MAX_TEXT_SECTIONS = 3
MAX_TEXT_LENGTH = MAX_TEXT_SECTION_LENGTH * MAX_TEXT_SECTIONS
# Role/everyone escaping can add a bounded zero-width character to authored
# text after the raw 12,000-character input ceiling.  Delivery still splits
# this formatted value into ordinary Discord-sized chunks.
MAX_FORMATTED_TEXT_LENGTH = MAX_TEXT_LENGTH * 2
MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 100 * 1024 * 1024


class GamePingValidationError(RuntimeError):
    """The submitted draft or current game state is not usable."""


class GamePingLookupError(GamePingValidationError):
    """The requested game or player could not be resolved."""


class GamePingPermissionError(GamePingValidationError):
    """The requester cannot use the requested game-ping scope."""


class GamePingConflictError(GamePingValidationError):
    """The selected game set changed while the draft was open."""


@dataclass(frozen=True, slots=True)
class AttachmentMetadata:
    """Validated Discord attachment metadata; no attachment body is stored."""

    filename: str
    url: str
    content_type: str
    size: int


@dataclass(frozen=True, slots=True)
class MemberSnapshot:
    """Discord/member facts captured before worker submission."""

    guild_id: int
    discord_id: int
    display_name: str
    name: str
    role_ids: tuple[int, ...] = ()
    role_names: tuple[str, ...] = ()
    level: int = 0
    is_staff: bool = False
    is_mod: bool = False
    description: str = ''


@dataclass(frozen=True, slots=True)
class ParticipantPermission:
    """One frozen current-channel readability fact."""

    game_id: int
    discord_id: int
    can_read: bool


@dataclass(frozen=True, slots=True)
class ChannelFacts:
    """Discord-only channel facts safe to recheck in the worker."""

    guild_id: int
    channel_id: int
    bot_channel_ids: tuple[int, ...] = ()
    private_bot_channel_ids: tuple[int, ...] = ()
    participant_permissions: tuple[ParticipantPermission, ...] = ()

    @property
    def bot_channels(self) -> tuple[int, ...]:
        return tuple(dict.fromkeys(
            (*self.bot_channel_ids, *self.private_bot_channel_ids),
        ))


@dataclass(frozen=True, slots=True)
class GamePingLoadRequest:
    """Primitive bounded candidate-load request."""

    guild_id: int
    requester: MemberSnapshot
    target_id: int
    explicit_game_id: int | None = None
    channel_id: int | None = None
    discover_all: bool = True


@dataclass(frozen=True, slots=True)
class GamePingDestination:
    """One post-commit channel destination and explicit user mentions."""

    game_id: int | None
    guild_id: int
    channel_id: int
    mention_ids: tuple[int, ...]
    kind: str = 'game'


@dataclass(frozen=True, slots=True)
class GamePingParticipant:
    discord_id: int
    display_name: str
    side_id: int | None = None


@dataclass(frozen=True, slots=True)
class GamePingGame:
    """A bounded immutable game snapshot used by the draft and preview."""

    game_id: int
    guild_id: int
    name: str
    is_pending: bool
    is_completed: bool
    is_confirmed: bool
    participants: tuple[GamePingParticipant, ...]
    destinations: tuple[GamePingDestination, ...]
    all_side_channels: bool


@dataclass(frozen=True, slots=True)
class GamePingLoadResult:
    """Candidate games and target information for a private composer."""

    guild_id: int
    target_id: int
    target_name: str
    games: tuple[GamePingGame, ...]
    total_games: int
    truncated: bool
    inferred_game_id: int | None = None
    all_scope_allowed: bool = False


@dataclass(frozen=True, slots=True)
class GamePingCommitRequest:
    """Primitive confirmation request for one atomic notification operation."""

    guild_id: int
    requester: MemberSnapshot
    target_id: int
    target_description: str
    scope: str
    game_ids: tuple[int, ...]
    channel_facts: ChannelFacts
    text: str
    attachments: tuple[AttachmentMetadata, ...] = ()
    truncated: bool = False
    invoked_with: str = '/game ping'


@dataclass(frozen=True, slots=True)
class GamePingCommitResult:
    """Primitive committed result.  It is terminal even if delivery fails."""

    guild_id: int
    requester_id: int
    target_id: int
    scope: str
    game_ids: tuple[int, ...]
    total_games: int
    truncated: bool
    recipient_ids: tuple[int, ...]
    recipient_names: tuple[str, ...]
    destinations: tuple[GamePingDestination, ...]
    text: str
    attachments: tuple[AttachmentMetadata, ...]
    requester_description: str = ''
    target_description: str = ''


_ping_read_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix='polybot-game-ping-read',
)
_ping_write_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='polybot-game-ping-write',
)


def _int_or_none(value) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _member_discord_id(player) -> int | None:
    member = getattr(player, 'discord_member', None)
    return _int_or_none(getattr(member, 'discord_id', None))


def _player_name(player, discord_id: int) -> str:
    member = getattr(player, 'discord_member', None)
    return str(
        getattr(player, 'name', None)
        or getattr(member, 'name', None)
        or f'user-{discord_id}'
    )


def _player_for_guild(discord_id: int, guild_id: int):
    """Find a guild player without using the upserting lookup helper."""

    query = (
        models.Player
        .select(models.Player, models.DiscordMember)
        .join(models.DiscordMember)
        .where(
            (models.Player.guild_id == int(guild_id))
            & (models.DiscordMember.discord_id == int(discord_id))
        )
    )
    return query.get_or_none()


def _registered_member(discord_id: int):
    getter = getattr(models.DiscordMember, 'get_or_none', None)
    if getter is None:
        return object()
    return getter(discord_id=int(discord_id))


def _game_ids_for_channel(guild_id: int, channel_id: int) -> tuple[int, ...]:
    query = (
        models.Game
        .select(models.Game.id)
        .join(models.GameSide, on=(models.GameSide.game == models.Game.id))
        .where(
            (models.Game.guild_id == int(guild_id))
            & (models.Game.is_confirmed == 0)
            & (
                (models.Game.game_chan == int(channel_id))
                | (models.GameSide.team_chan == int(channel_id))
            )
        )
        .distinct()
        .order_by(models.Game.id)
        .limit(2)
    )
    return tuple(int(row.id) for row in query)


def _prefetch_games(query) -> tuple:
    """Prefetch game sides and lineups in bounded batched reads."""

    side_query = models.GameSide.select().order_by(models.GameSide.position)
    lineup_query = (
        models.Lineup
        .select(models.Lineup, models.Player, models.DiscordMember)
        .join(models.Player)
        .join(models.DiscordMember)
        .order_by(models.Lineup.id)
    )
    try:
        return tuple(peewee.prefetch(query, side_query, lineup_query))
    except (AttributeError, TypeError):
        # Small offline fakes commonly expose an already-prefetched iterable.
        return tuple(query)


def _game_lineups(game, sides: tuple) -> tuple:
    lineups = tuple(getattr(game, 'lineup', ()) or ())
    if lineups:
        return lineups
    rows = []
    for side in sides:
        rows.extend(tuple(getattr(side, 'lineup', ()) or ()))
    return tuple(rows)


def _game_sides(game) -> tuple:
    return tuple(getattr(game, 'gamesides', ()) or ())


def _snapshot_game(game) -> GamePingGame:
    sides = _game_sides(game)
    lineups = _game_lineups(game, sides)
    by_side: dict[int, list[GamePingParticipant]] = {
        int(getattr(side, 'id')): []
        for side in sides
        if _int_or_none(getattr(side, 'id', None)) is not None
    }
    participants: list[GamePingParticipant] = []
    for lineup in lineups[:MAX_PARTICIPANTS_PER_GAME]:
        player = getattr(lineup, 'player', None)
        discord_id = _member_discord_id(player)
        if discord_id is None:
            continue
        participant = GamePingParticipant(
            discord_id=discord_id,
            display_name=_player_name(player, discord_id),
            side_id=_int_or_none(getattr(lineup, 'gameside_id', None))
            or _int_or_none(getattr(getattr(lineup, 'gameside', None), 'id', None)),
        )
        participants.append(participant)
        if participant.side_id in by_side:
            by_side[participant.side_id].append(participant)

    destinations: list[GamePingDestination] = []
    side_channel_count = 0
    for side in sides:
        channel_id = _int_or_none(getattr(side, 'team_chan', None))
        if channel_id is None:
            continue
        side_channel_count += 1
        destination_guild_id = (
            _int_or_none(getattr(side, 'team_chan_external_server', None))
            or int(game.guild_id)
        )
        side_id = _int_or_none(getattr(side, 'id', None))
        mention_ids = tuple(
            participant.discord_id for participant in by_side.get(side_id, ())
        )
        destinations.append(GamePingDestination(
            game_id=int(game.id),
            guild_id=destination_guild_id,
            channel_id=channel_id,
            mention_ids=mention_ids,
            kind='side',
        ))

    central_channel_id = _int_or_none(getattr(game, 'game_chan', None))
    if central_channel_id is not None:
        destinations.append(GamePingDestination(
            game_id=int(game.id),
            guild_id=int(game.guild_id),
            channel_id=central_channel_id,
            mention_ids=tuple(
                participant.discord_id for participant in participants
            ),
            kind='central',
        ))

    return GamePingGame(
        game_id=int(game.id),
        guild_id=int(game.guild_id),
        name=str(getattr(game, 'name', None) or ''),
        is_pending=bool(getattr(game, 'is_pending', False)),
        is_completed=bool(getattr(game, 'is_completed', False)),
        is_confirmed=bool(getattr(game, 'is_confirmed', False)),
        participants=tuple(participants),
        destinations=tuple(destinations),
        all_side_channels=(bool(sides) and side_channel_count >= len(sides)),
    )


def _load_games_by_ids(
    guild_id: int,
    game_ids: tuple[int, ...],
) -> tuple[GamePingGame, ...]:
    if not game_ids:
        return ()
    bounded_ids = tuple(dict.fromkeys(int(value) for value in game_ids))
    if len(bounded_ids) > MAX_GAMES:
        raise GamePingValidationError(
            f'A notification can include at most {MAX_GAMES} games.'
        )
    query = models.Game.select().where(
        (models.Game.guild_id == int(guild_id))
        & (models.Game.is_confirmed == 0)
        & (models.Game.id.in_(bounded_ids))
    ).order_by(-models.Game.id)
    return tuple(_snapshot_game(game) for game in _prefetch_games(query))


def _all_target_game_ids(guild_id: int, target_id: int) -> tuple[tuple[int, ...], int, bool]:
    player_game_ids = (
        models.Lineup
        .select(models.Lineup.game)
        .join(models.Game)
        .join_from(models.Lineup, models.Player)
        .join_from(models.Player, models.DiscordMember)
        .where(
            (models.Player.guild_id == int(guild_id))
            & (models.DiscordMember.discord_id == int(target_id))
            & (models.Game.is_confirmed == 0)
        )
        .order_by(-models.Lineup.game)
    )
    query = models.Game.select().where(
        (models.Game.guild_id == int(guild_id))
        & (models.Game.id.in_(player_game_ids))
        & (models.Game.is_confirmed == 0)
    ).order_by(-models.Game.id).limit(MAX_GAMES + 1)
    ids = tuple(int(game.id) for game in query)
    truncated = len(ids) > MAX_GAMES
    bounded = ids[:MAX_GAMES]
    return bounded, len(ids), truncated


def _target_name(player, target_id: int) -> str:
    return _player_name(player, target_id)


def _validate_requester_registered(requester: MemberSnapshot) -> None:
    if requester.guild_id <= 0:
        raise GamePingPermissionError('The requester guild is invalid.')
    if _registered_member(requester.discord_id) is None:
        raise GamePingPermissionError(
            'This command requires bot registration first.'
        )


def _validate_target_permission(
    requester: MemberSnapshot,
    target_id: int,
    *,
    scope: str | None = None,
) -> None:
    if int(target_id) == int(requester.discord_id):
        if scope == 'all' and requester.level <= 2:
            raise GamePingPermissionError(
                'You do not have permission to ping all of your incomplete '
                'games. Ask a server staff member for help.'
            )
        return
    if requester.level <= 3:
        raise GamePingPermissionError(
            'You do not have permission to use this command on another '
            "player's games."
        )


def prepare_candidates(request: GamePingLoadRequest) -> GamePingLoadResult:
    """Resolve a bounded target/game snapshot on a worker-local connection."""

    with models.db.connection_context():
        _validate_requester_registered(request.requester)
        _validate_target_permission(request.requester, request.target_id)
        target = _player_for_guild(request.target_id, request.guild_id)
        if target is None:
            raise GamePingLookupError(
                f'User <@{request.target_id}> is not a registered ELO player '
                'in this server.'
            )

        inferred_game_id = None
        channel_game_ids = ()
        if request.channel_id is not None:
            channel_game_ids = _game_ids_for_channel(
                request.guild_id,
                request.channel_id,
            )
            if len(channel_game_ids) == 1:
                inferred_game_id = channel_game_ids[0]
            elif len(channel_game_ids) > 1 and not request.discover_all and request.explicit_game_id is None:
                raise GamePingLookupError(
                    'More than one game uses this channel. Include a game ID '
                    'or choose one from the private composer.'
                )

        requested_ids: list[int] = []
        if request.explicit_game_id is not None:
            requested_ids.append(int(request.explicit_game_id))
        elif inferred_game_id is not None:
            requested_ids.append(int(inferred_game_id))
        elif not request.discover_all:
            raise GamePingLookupError(
                'Game ID was not included and the current channel does not '
                'identify one game.'
            )

        total_games = 0
        truncated = False
        if request.discover_all:
            all_ids, total_games, truncated = _all_target_game_ids(
                request.guild_id,
                request.target_id,
            )
            requested_ids.extend(all_ids)
        requested_ids = list(dict.fromkeys(requested_ids))[:MAX_GAMES]
        games = _load_games_by_ids(request.guild_id, tuple(requested_ids))
        games_by_id = {game.game_id: game for game in games}

        if request.explicit_game_id is not None and request.explicit_game_id not in games_by_id:
            raise GamePingLookupError(
                f'Game with ID {request.explicit_game_id} cannot be found in '
                'this Discord server.'
            )
        if inferred_game_id is not None and inferred_game_id not in games_by_id:
            raise GamePingLookupError(
                f'Game {inferred_game_id} could not be loaded from the '
                'current channel.'
            )

        if requested_ids and not request.discover_all:
            selected = games_by_id[requested_ids[0]]
            if (
                request.target_id not in {
                    participant.discord_id
                    for participant in selected.participants
                }
                and not request.requester.is_staff
            ):
                raise GamePingPermissionError(
                    f'You are not a participant in game {selected.game_id}.'
                )

        if not request.discover_all or not total_games:
            total_games = len(games)
        return GamePingLoadResult(
            guild_id=int(request.guild_id),
            target_id=int(request.target_id),
            target_name=_target_name(target, request.target_id),
            games=tuple(games),
            total_games=int(total_games),
            truncated=bool(truncated),
            inferred_game_id=inferred_game_id,
            all_scope_allowed=(request.requester.level > 2),
        )


def _participant_permissions(
    facts: ChannelFacts,
    game_id: int,
) -> dict[int, bool]:
    return {
        permission.discord_id: bool(permission.can_read)
        for permission in facts.participant_permissions
        if permission.game_id == int(game_id)
    }


def _dedupe_destinations(destinations: tuple[GamePingDestination, ...]) -> tuple[GamePingDestination, ...]:
    result: list[GamePingDestination] = []
    index_by_key: dict[tuple[int, int], int] = {}
    for destination in destinations:
        key = (int(destination.guild_id), int(destination.channel_id))
        existing_index = index_by_key.get(key)
        if existing_index is None:
            index_by_key[key] = len(result)
            result.append(destination)
            continue
        existing = result[existing_index]
        result[existing_index] = GamePingDestination(
            game_id=(existing.game_id if existing.game_id == destination.game_id else None),
            guild_id=existing.guild_id,
            channel_id=existing.channel_id,
            mention_ids=tuple(dict.fromkeys((*existing.mention_ids, *destination.mention_ids))),
            kind=(existing.kind if existing.kind == destination.kind else 'game'),
        )
    return tuple(result)


def _destinations_for_game(
    game: GamePingGame,
    request: GamePingCommitRequest,
) -> tuple[GamePingDestination, ...]:
    facts = request.channel_facts
    current_is_bot_channel = facts.channel_id in facts.bot_channels
    current_is_game_channel = any(
        destination.channel_id == facts.channel_id
        for destination in game.destinations
    )
    current_is_central_channel = any(
        destination.channel_id == facts.channel_id
        and destination.kind == 'central'
        for destination in game.destinations
    )
    readable = _participant_permissions(facts, game.game_id)
    all_readable = bool(game.participants) and all(
        readable.get(participant.discord_id, False)
        for participant in game.participants
    )

    if request.scope == 'all':
        if not current_is_bot_channel and not request.requester.is_mod:
            raise GamePingPermissionError(
                'All-incomplete game pings must be sent from a configured '
                'bot channel.'
            )
        return game.destinations

    if current_is_game_channel and game.all_side_channels:
        return game.destinations
    if current_is_central_channel:
        return game.destinations
    if request.requester.is_mod and game.all_side_channels:
        return game.destinations
    if all_readable or current_is_bot_channel:
        return _dedupe_destinations((*game.destinations, GamePingDestination(
            game_id=game.game_id,
            guild_id=game.guild_id,
            channel_id=facts.channel_id,
            mention_ids=tuple(
                participant.discord_id for participant in game.participants
            ),
            kind='requester',
        )))
    raise GamePingPermissionError(
        'This command cannot be used in this channel. Use a game channel, a '
        'configured bot channel, or a channel readable by every participant.'
    )


def _safe_attachment(attachment: AttachmentMetadata) -> None:
    if not attachment.filename or len(attachment.filename) > 255:
        raise GamePingValidationError('Attachment filenames must be non-empty and short.')
    if int(attachment.size) < 0 or int(attachment.size) > MAX_ATTACHMENT_BYTES:
        raise GamePingValidationError(
            f'Each attachment must be at most {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB.'
        )
    parsed = urlparse(str(attachment.url))
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password:
        raise GamePingValidationError(
            'Attachments must use safe HTTPS Discord URLs.'
        )
    host = parsed.hostname.lower() if parsed.hostname else ''
    if not (
        host == 'cdn.discordapp.com'
        or host == 'media.discordapp.net'
        or host.endswith('.discordapp.com')
        or host.endswith('.discordapp.net')
    ):
        raise GamePingValidationError(
            'Attachments must use safe HTTPS Discord URLs.'
        )


def _validate_draft(request: GamePingCommitRequest) -> None:
    if request.scope not in {'single', 'all'}:
        raise GamePingValidationError('Choose a single game or all incomplete games.')
    if not request.game_ids:
        raise GamePingValidationError('At least one incomplete game is required.')
    if len(request.game_ids) > MAX_GAMES:
        raise GamePingValidationError(
            f'A notification can include at most {MAX_GAMES} games.'
        )
    normalized_ids = tuple(int(game_id) for game_id in request.game_ids)
    if any(game_id <= 0 for game_id in normalized_ids) or len(set(normalized_ids)) != len(normalized_ids):
        raise GamePingValidationError('Game IDs must be unique positive integers.')
    if request.scope == 'single' and len(normalized_ids) != 1:
        raise GamePingValidationError('A single-game notification needs one game ID.')
    if len(request.text) > MAX_FORMATTED_TEXT_LENGTH:
        raise GamePingValidationError(
            f'Text sections may total at most {MAX_TEXT_LENGTH:,} characters.'
        )
    if len(request.attachments) > MAX_ATTACHMENTS:
        raise GamePingValidationError(
            f'You may attach at most {MAX_ATTACHMENTS} files.'
        )
    total_size = 0
    for attachment in request.attachments:
        _safe_attachment(attachment)
        total_size += int(attachment.size)
    if total_size > MAX_TOTAL_ATTACHMENT_BYTES:
        raise GamePingValidationError(
            f'Attachments may total at most {MAX_TOTAL_ATTACHMENT_BYTES // (1024 * 1024)} MB.'
        )
    if not request.text.strip() and not request.attachments:
        raise GamePingValidationError(
            'Add text or at least one attachment before confirming.'
        )


def _audit_message(
    request: GamePingCommitRequest,
    game: GamePingGame,
) -> str:
    safe_text = utilities.escape_role_mentions(request.text)
    attachment_names = ', '.join(
        attachment.filename for attachment in request.attachments
    ) or 'none'
    target = (
        f' for target {request.target_description}'
        if request.target_id != request.requester.discord_id
        else ''
    )
    return (
        f'{request.requester.description} committed a game ping notification '
        f'request{target} using '
        f'`{request.invoked_with}`; scope={request.scope}; '
        f'games={game.game_id}; attachments={attachment_names}; '
        f'message={safe_text}'
    )


def commit_notification(request: GamePingCommitRequest) -> GamePingCommitResult:
    """Reload, validate, and audit one notification atomically."""

    _validate_draft(request)
    with models.db.connection_context():
        if request.channel_facts.guild_id != int(request.guild_id):
            raise GamePingPermissionError(
                'The original server channel no longer matches this draft.'
            )
        _validate_requester_registered(request.requester)
        _validate_target_permission(
            request.requester,
            request.target_id,
            scope=request.scope,
        )
        target = _player_for_guild(request.target_id, request.guild_id)
        if target is None:
            raise GamePingLookupError(
                f'User <@{request.target_id}> is not a registered ELO player '
                'in this server.'
            )

        if request.scope == 'all':
            current_ids, _total, current_truncated = _all_target_game_ids(
                request.guild_id,
                request.target_id,
            )
            expected_ids = tuple(dict.fromkeys(int(value) for value in request.game_ids))
            if tuple(current_ids) != expected_ids:
                raise GamePingConflictError(
                    'The target player’s incomplete games changed while the '
                    'draft was open. Reopen `/game ping` to refresh it.'
                )
            truncated = bool(current_truncated or request.truncated)
        else:
            expected_ids = (int(request.game_ids[0]),)
            truncated = bool(request.truncated)

        games = _load_games_by_ids(request.guild_id, expected_ids)
        by_id = {game.game_id: game for game in games}
        if tuple(sorted(by_id)) != tuple(sorted(expected_ids)):
            raise GamePingConflictError(
                'One or more selected games changed or disappeared. Reopen '
                '`/game ping` to refresh the private draft.'
            )

        destinations: list[GamePingDestination] = []
        recipient_ids: list[int] = []
        recipient_names: list[str] = []
        for game_id in expected_ids:
            game = by_id[game_id]
            if game.is_confirmed:
                raise GamePingValidationError(
                    f'Game {game.game_id} is no longer incomplete.'
                )
            participant_ids = {participant.discord_id for participant in game.participants}
            if request.scope == 'single' and (
                request.target_id not in participant_ids
                and not request.requester.is_staff
            ):
                raise GamePingPermissionError(
                    f'You are not a participant in game {game.game_id}.'
                )
            destinations.extend(_destinations_for_game(game, request))
            for participant in game.participants:
                if participant.discord_id not in recipient_ids:
                    recipient_ids.append(participant.discord_id)
                    recipient_names.append(participant.display_name)

        if request.scope == 'all':
            # The prefix workflow has always shown a compact completion in the
            # invoking bot channel.  It is a real post-commit destination so
            # native uploads and the same text reach the requester-visible
            # fanout without leaking a private draft.
            destinations.append(GamePingDestination(
                game_id=None,
                guild_id=request.guild_id,
                channel_id=request.channel_facts.channel_id,
                mention_ids=tuple(recipient_ids),
                kind='requester-summary',
            ))
        destinations = list(_dedupe_destinations(tuple(destinations)))
        if len(destinations) > MAX_DESTINATIONS:
            raise GamePingValidationError(
                'The notification would reach too many destinations. Narrow '
                'the scope and try again.'
            )

        with models.db.atomic():
            for game_id in expected_ids:
                models.GameLog.write(
                    game_id=int(game_id),
                    guild_id=int(request.guild_id),
                    message=_audit_message(request, by_id[int(game_id)]),
                )

        return GamePingCommitResult(
            guild_id=int(request.guild_id),
            requester_id=int(request.requester.discord_id),
            target_id=int(request.target_id),
            scope=request.scope,
            game_ids=tuple(expected_ids),
            total_games=len(expected_ids),
            truncated=truncated,
            recipient_ids=tuple(recipient_ids),
            recipient_names=tuple(recipient_names),
            destinations=tuple(destinations),
            text=request.text,
            attachments=tuple(request.attachments),
            requester_description=request.requester.description,
            target_description=request.target_description,
        )


async def run_ping_candidates(
    request: GamePingLoadRequest,
) -> GamePingLoadResult:
    """Run a bounded candidate read without touching the event loop."""

    loop = asyncio.get_running_loop()
    concurrent_future = _ping_read_executor.submit(
        functools.partial(prepare_candidates, request),
    )
    future = asyncio.wrap_future(concurrent_future, loop=loop)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError as cancellation:
        task = asyncio.current_task()
        while not concurrent_future.done():
            if task is not None:
                while task.cancelling():
                    task.uncancel()
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                continue
        # The synchronous read has been drained; preserve ordinary task
        # cancellation semantics for the caller.
        try:
            concurrent_future.result()
        except BaseException:
            logger.exception('Canceled game-ping candidate read failed')
        raise asyncio.CancelledError from cancellation


class GamePingCancelled(asyncio.CancelledError):
    """Cancellation after a synchronous worker was drained."""

    def __init__(self, *, committed: bool, result=None, error=None):
        super().__init__()
        self.committed = bool(committed)
        self.result = result
        self.error = error


async def run_ping_commit(
    request: GamePingCommitRequest,
) -> GamePingCommitResult:
    """Run one commit and drain it before propagating cancellation."""

    loop = asyncio.get_running_loop()
    concurrent_future = _ping_write_executor.submit(
        functools.partial(commit_notification, request),
    )
    future = asyncio.wrap_future(concurrent_future, loop=loop)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError as cancellation:
        task = asyncio.current_task()
        while not concurrent_future.done():
            if task is not None:
                while task.cancelling():
                    task.uncancel()
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                continue
        try:
            result = concurrent_future.result()
        except BaseException as error:
            raise GamePingCancelled(
                committed=False,
                error=error,
            ) from cancellation
        raise GamePingCancelled(
            committed=True,
            result=result,
        ) from cancellation
