"""Pure timezone-offset parsing and compatibility helpers.

The bot stores a fixed UTC offset, not an IANA timezone or a geographic
location.  Keeping these helpers free of Discord, Peewee, and settings imports
lets readers and offline tests share the same bounded representation.
"""

from __future__ import annotations

import re


MIN_OFFSET_MINUTES = -12 * 60
MAX_OFFSET_MINUTES = 14 * 60
OFFSET_STEP_MINUTES = 15

_OFFSET_PATTERN = re.compile(
    r'^(?P<prefix>UTC|GMT)(?P<sign>[+-])(?P<hours>\d{1,2})'
    r'(?::(?P<minutes>\d{2}))?$',
    re.IGNORECASE,
)


class TimezoneOffsetError(ValueError):
    """The supplied value is not a supported fixed UTC offset."""


def _validate_minutes(minutes: int) -> int:
    minutes = int(minutes)
    if not MIN_OFFSET_MINUTES <= minutes <= MAX_OFFSET_MINUTES:
        raise TimezoneOffsetError(
            'Use a UTC offset from UTC-12:00 through UTC+14:00.'
        )
    if minutes % OFFSET_STEP_MINUTES:
        raise TimezoneOffsetError(
            'UTC offsets must use 15-minute increments.'
        )
    return minutes


def parse_timezone_offset(value: str | int | None, *, allow_gmt: bool = True) -> int:
    """Parse a UTC/GMT offset into canonical minutes.

    Native autocomplete values are normalized ``UTC±HH:MM`` strings.  The
    parser also accepts the compact whole/quarter/half-hour forms used by the
    retained ``$settime`` grammar and normalizes them before storage.
    """

    if isinstance(value, bool):
        raise TimezoneOffsetError('A UTC offset must be a string.')
    if isinstance(value, int):
        return _validate_minutes(value)

    text = str(value or '').strip()
    if text.upper() == 'GMT' and not allow_gmt:
        raise TimezoneOffsetError('Use the UTC prefix for this command.')
    if text.upper() in {'UTC', 'GMT'}:
        return 0
    match = _OFFSET_PATTERN.fullmatch(text)
    if match is None:
        raise TimezoneOffsetError(
            'Use a normalized offset such as `UTC-05:00` or `UTC+05:30`.'
        )
    if not allow_gmt and match.group('prefix').upper() != 'UTC':
        raise TimezoneOffsetError('Use the UTC prefix for this command.')

    hours = int(match.group('hours'))
    minutes_part = int(match.group('minutes') or 0)
    if minutes_part not in (0, 15, 30, 45):
        raise TimezoneOffsetError(
            'UTC offsets must use 15-minute increments.'
        )
    total = hours * 60 + minutes_part
    if match.group('sign') == '-':
        total = -total
    return _validate_minutes(total)


def format_timezone_offset(minutes: int | None) -> str | None:
    """Format valid offset minutes as ``UTC±HH:MM``."""

    if minutes is None:
        return None
    minutes = _validate_minutes(minutes)
    sign = '+' if minutes >= 0 else '-'
    absolute = abs(minutes)
    hours, minute_part = divmod(absolute, 60)
    return f'UTC{sign}{hours:02d}:{minute_part:02d}'


def normalize_timezone_offset(value: str | int | None, *, allow_gmt: bool = True) -> str:
    """Parse and return one normalized fixed-offset string."""

    return format_timezone_offset(
        parse_timezone_offset(value, allow_gmt=allow_gmt)
    )  # type: ignore[return-value]


def effective_timezone_offset_minutes(member) -> int | None:
    """Read the canonical preference with a safe legacy fallback.

    ``timezone_offset_cleared`` is an additive tombstone.  Without it, a
    cleared nullable minutes field would incorrectly expose the legacy whole-
    hour value again.  A populated minutes value always wins over legacy data.
    ``getattr`` keeps transitional read helpers usable with old test doubles
    and serialized objects that predate the additive fields.
    """

    minutes = getattr(member, 'timezone_offset_minutes', None)
    if minutes is not None:
        return int(minutes)
    if bool(getattr(member, 'timezone_offset_cleared', False)):
        return None
    legacy_hours = getattr(member, 'timezone_offset', None)
    if legacy_hours is None:
        return None
    return int(legacy_hours) * 60


def effective_timezone_offset(member) -> str | None:
    """Return the effective preference in normalized display form."""

    minutes = effective_timezone_offset_minutes(member)
    if minutes is None:
        return None
    try:
        return format_timezone_offset(minutes)
    except TimezoneOffsetError:
        # Legacy whole-hour data predates the bounded native range.  Preserve
        # its read visibility without allowing it to become a new write value.
        sign = '+' if minutes >= 0 else '-'
        absolute = abs(minutes)
        hours, minute_part = divmod(absolute, 60)
        return f'UTC{sign}{hours:02d}:{minute_part:02d}'


def offset_suggestions(current: str | None = None, *, limit: int = 25) -> tuple[str, ...]:
    """Return at most Discord's bounded autocomplete result count."""

    limit = max(0, min(int(limit), 25))
    query = str(current or '').strip().casefold()
    values = (
        format_timezone_offset(minutes)
        for minutes in range(
            MIN_OFFSET_MINUTES,
            MAX_OFFSET_MINUTES + OFFSET_STEP_MINUTES,
            OFFSET_STEP_MINUTES,
        )
    )
    matches = tuple(
        value for value in values
        if not query or query in value.casefold()
    )
    return matches[:limit]
