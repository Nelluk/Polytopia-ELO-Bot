"""Bounded synchronous workers for the focused team-emoji attribute."""

from __future__ import annotations

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import re
import unicodedata

from modules import models


# Discord custom emoji are rendered from their serialized form.  Resolving a
# cached Emoji/PartialEmoji object would make a valid value depend on the bot's
# current cache and on which guild owns the emoji, so validation is syntax-only.
CUSTOM_EMOJI_PATTERN = re.compile(
    r"^<a?:[A-Za-z0-9_]{2,32}:[0-9]{15,22}>$"
)

# These ranges cover the ordinary emoji blocks used by Discord, together with
# the legacy symbol blocks that contain emoji-capable characters such as
# hearts and weather symbols.  Variation selectors, ZWJ, keycaps, skin tones,
# and regional indicators are accepted as sequence components below.
_EMOJI_BASE_RANGES = (
    (0x1F000, 0x1FAFF),
    (0x2600, 0x27FF),
    (0x2300, 0x23FF),
    (0x2B00, 0x2BFF),
    (0x00A9, 0x00AE),
    (0x203C, 0x2049),
    (0x2122, 0x2139),
)
_EMOJI_COMPONENTS = {
    0x200D,  # zero-width joiner
    0x20E3,  # combining enclosing keycap
    0xFE0E,  # text variation selector
    0xFE0F,  # emoji variation selector
}
_SKIN_TONE_RANGE = (0x1F3FB, 0x1F3FF)
_REGIONAL_INDICATOR_RANGE = (0x1F1E6, 0x1F1FF)
_KEYCAP_PATTERN = re.compile(r'^[0-9#*]\ufe0f?\u20e3$')


class TeamEmojiValidationError(RuntimeError):
    """The request contains an invalid or contradictory value."""


class TeamEmojiLookupError(TeamEmojiValidationError):
    """The requested team cannot be resolved unambiguously."""


class TeamEmojiPermissionError(TeamEmojiValidationError):
    """The requester's captured permission snapshot is insufficient."""


class TeamEmojiConflictError(TeamEmojiValidationError):
    """The mutation was based on a stale current value."""


@dataclass(frozen=True)
class TeamEmojiReadRequest:
    """Immutable primitive input for one current-value read."""

    guild_id: int
    requester_id: int
    requester_is_mod: bool
    team_enabled: bool
    team_lookup: str | None
    requester_description: str
    invoked_with: str = 'team_emoji'


@dataclass(frozen=True)
class TeamEmojiMutationRequest:
    """Immutable primitive input for one atomic emoji mutation."""

    guild_id: int
    requester_id: int
    requester_is_mod: bool
    team_enabled: bool
    team_lookup: str | None
    emoji: str | None
    clear: bool
    requester_description: str
    expected_emoji: str | None = None
    native: bool = True
    invoked_with: str = 'team_emoji'


@dataclass(frozen=True)
class TeamEmojiReadResult:
    guild_id: int
    team_id: int
    team_name: str
    emoji: str


@dataclass(frozen=True)
class TeamEmojiMutationResult:
    guild_id: int
    team_id: int
    team_name: str
    old_emoji: str
    emoji: str
    cleared: bool
    native: bool


@dataclass(frozen=True)
class TeamAutocompleteRequest:
    """Immutable, guild-scoped input for a cheap team-name suggestion read."""

    guild_id: int
    current: str
    limit: int = 25


@dataclass(frozen=True)
class TeamAutocompleteResult:
    """One visible team suggestion returned from the worker."""

    team_id: int
    team_name: str


def _is_in_range(codepoint: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(start <= codepoint <= end for start, end in ranges)


def is_unicode_emoji(value: str) -> bool:
    """Return whether *value* is an emoji sequence, without external lookup."""

    if not isinstance(value, str) or not value or value != value.strip():
        return False
    if _KEYCAP_PATTERN.fullmatch(value):
        return True

    has_base = False
    for character in value:
        codepoint = ord(character)
        if _is_in_range(codepoint, _EMOJI_BASE_RANGES):
            has_base = True
            continue
        if codepoint in _EMOJI_COMPONENTS:
            continue
        if _SKIN_TONE_RANGE[0] <= codepoint <= _SKIN_TONE_RANGE[1]:
            continue
        if (
            _REGIONAL_INDICATOR_RANGE[0]
            <= codepoint
            <= _REGIONAL_INDICATOR_RANGE[1]
        ):
            has_base = True
            continue
        # Combining marks are allowed only as sequence decorations.  The
        # explicit component set above handles the emoji-relevant ones; this
        # branch keeps other combining marks from becoming arbitrary text.
        if unicodedata.category(character).startswith('M'):
            continue
        return False

    return has_base


def is_valid_emoji(value: str | None) -> bool:
    """Accept Unicode emoji and syntax-valid custom emoji strings."""

    if value is None or not isinstance(value, str) or len(value) > 100:
        return False
    return bool(CUSTOM_EMOJI_PATTERN.fullmatch(value)) or is_unicode_emoji(value)


def validate_emoji(value: str | None) -> str:
    """Validate and return the exact database value to store."""

    if not is_valid_emoji(value):
        raise TeamEmojiValidationError(
            'Valid Unicode or custom emoji syntax was not detected.'
        )
    return str(value)


def _validate_access(request) -> None:
    if not bool(request.team_enabled):
        raise TeamEmojiPermissionError('Teams are not enabled on this server.')
    if not bool(request.requester_is_mod):
        raise TeamEmojiPermissionError(
            'You do not have permission to manage team emojis.'
        )


def _normalise_lookup(value: str | None) -> str | None:
    if value is None:
        return None
    lookup = str(value).strip()
    return lookup or None


def _team_name(team) -> str:
    return str(getattr(team, 'name', ''))


def _team_id(team) -> int:
    return int(getattr(team, 'id'))


def _team_matches(
    team_lookup: str,
    guild_id: int,
    *,
    include_hidden: bool = True,
):
    try:
        matches = models.Team.get_by_name(
            team_name=team_lookup,
            guild_id=int(guild_id),
            include_hidden=bool(include_hidden),
        )
    except TypeError:
        # Small model doubles and older local model shims may not expose the
        # keyword-only spelling, while the real Team helper does.
        matches = models.Team.get_by_name(
            team_lookup,
            int(guild_id),
            False,
            bool(include_hidden),
        )
    return tuple(matches)


def _inferred_team_matches(request: TeamEmojiReadRequest | TeamEmojiMutationRequest):
    """Resolve teams from the requester's persisted guild player rows."""

    player_model = getattr(models, 'Player', None)
    if player_model is None or not hasattr(player_model, 'select'):
        return ()

    query = (
        models.Team.select()
        .join(player_model)
        .join(models.DiscordMember)
        .where(
            (player_model.guild_id == int(request.guild_id))
            & (models.DiscordMember.discord_id == int(request.requester_id))
            & player_model.team.is_null(False)
        )
        .distinct()
    )
    return tuple(query)


def _resolve_team(request, *, include_hidden: bool = True):
    team_lookup = _normalise_lookup(request.team_lookup)
    if team_lookup is not None:
        matches = _team_matches(
            team_lookup,
            request.guild_id,
            include_hidden=include_hidden,
        )
        if not matches:
            raise TeamEmojiLookupError(
                f'No matching team was found for "{team_lookup}".'
            )
        if len(matches) > 1:
            raise TeamEmojiLookupError(
                f'More than one matching team was found for "{team_lookup}".'
            )
        return matches[0]

    matches = _inferred_team_matches(request)
    if not matches:
        raise TeamEmojiLookupError(
            'Your team could not be inferred. Provide a team name.'
        )
    if len(matches) > 1:
        raise TeamEmojiLookupError(
            'Your team is ambiguous. Provide a team name.'
        )
    return matches[0]


def list_team_autocomplete(
    request: TeamAutocompleteRequest,
) -> tuple[TeamAutocompleteResult, ...]:
    """Return at most 25 visible teams from one guild.

    Autocomplete is deliberately a small read-only worker.  Hidden and
    archived teams remain addressable through the legacy-compatible explicit
    lookup path, but are not suggested as ordinary team attributes.
    """

    with models.db.connection_context():
        limit = min(max(int(request.limit), 1), 25)
        current = str(request.current or '').strip()
        query = models.Team.select(models.Team.id, models.Team.name).where(
            (models.Team.guild_id == int(request.guild_id))
            & (models.Team.is_hidden == 0)
            & (models.Team.is_archived == 0)
        )
        if current:
            query = query.where(models.Team.name.contains(current))
        query = query.order_by(models.Team.name, models.Team.id).limit(limit)
        return tuple(
            TeamAutocompleteResult(
                team_id=int(team.id),
                team_name=str(team.name),
            )
            for team in query
        )


def _read_values(team) -> tuple[int, int, str, str]:
    return (
        _team_id(team),
        int(getattr(team, 'guild_id')),
        _team_name(team),
        str(getattr(team, 'emoji', '') or ''),
    )


def read_team_emoji(request: TeamEmojiReadRequest) -> TeamEmojiReadResult:
    """Read one team emoji using a worker-local Peewee connection."""

    with models.db.connection_context():
        _validate_access(request)
        team = _resolve_team(request)
        team_id, guild_id, team_name, emoji = _read_values(team)
        return TeamEmojiReadResult(
            guild_id=guild_id,
            team_id=team_id,
            team_name=team_name,
            emoji=emoji,
        )


def _write_audit(request, *, team_name: str, old_emoji: str, new_emoji: str) -> None:
    if request.clear:
        change = f'cleared the emoji for Team {team_name}'
    else:
        change = f'set the emoji for Team {team_name} to {new_emoji!r}'
    invocation_note = (
        f' ({request.invoked_with})'
        if str(request.invoked_with).startswith('/')
        else ''
    )
    models.GameLog.write(
        guild_id=int(request.guild_id),
        message=(
            f'{request.requester_description} {change}; '
            f'previous value was {old_emoji!r}{invocation_note}'
        ),
    )


def set_team_emoji(request: TeamEmojiMutationRequest) -> TeamEmojiMutationResult:
    """Validate, mutate, and audit one team emoji in one transaction."""

    with models.db.connection_context():
        with models.db.atomic():
            _validate_access(request)
            if request.clear and request.emoji is not None:
                raise TeamEmojiValidationError(
                    'Choose either an emoji or `clear`, not both.'
                )
            new_emoji = '' if request.clear else validate_emoji(request.emoji)
            team = _resolve_team(request)
            team_id, guild_id, team_name, old_emoji = _read_values(team)
            if (
                request.expected_emoji is not None
                and old_emoji != request.expected_emoji
            ):
                raise TeamEmojiConflictError(
                    f'Team {team_name} changed before this update was applied.'
                )
            team.emoji = new_emoji
            team.save()
            _write_audit(
                request,
                team_name=team_name,
                old_emoji=old_emoji,
                new_emoji=new_emoji,
            )
            return TeamEmojiMutationResult(
                guild_id=guild_id,
                team_id=team_id,
                team_name=team_name,
                old_emoji=old_emoji,
                emoji=new_emoji,
                cleared=bool(request.clear),
                native=bool(request.native),
            )


_team_emoji_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='polybot-team-emoji',
)


async def run_bounded_team_worker(
    function,
    request,
    *,
    drain_on_cancel: bool,
):
    """Run a primitive team worker on the shared bounded executor."""

    loop = asyncio.get_running_loop()
    concurrent_future = _team_emoji_executor.submit(
        functools.partial(function, request)
    )
    future = asyncio.wrap_future(concurrent_future, loop=loop)
    if not drain_on_cancel:
        return await future
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        task = asyncio.current_task()
        while not future.done():
            if task is not None:
                task.uncancel()
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
        future.result()
        raise asyncio.CancelledError


async def run_team_emoji_read(
    request: TeamEmojiReadRequest,
) -> TeamEmojiReadResult:
    """Submit a bounded current-value read."""

    return await run_bounded_team_worker(
        read_team_emoji,
        request,
        drain_on_cancel=False,
    )


async def run_team_autocomplete(
    request: TeamAutocompleteRequest,
) -> tuple[TeamAutocompleteResult, ...]:
    """Run autocomplete through the same bounded team executor as P8.1."""

    return await run_bounded_team_worker(
        list_team_autocomplete,
        request,
        drain_on_cancel=False,
    )


async def run_team_emoji_mutation(
    request: TeamEmojiMutationRequest,
) -> TeamEmojiMutationResult:
    """Submit a mutation and drain synchronous work if the caller cancels."""

    return await run_bounded_team_worker(
        set_team_emoji,
        request,
        drain_on_cancel=True,
    )
