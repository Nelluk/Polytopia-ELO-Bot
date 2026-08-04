"""Shared adapters and public presentation for player timezone preferences."""

from __future__ import annotations

import logging

import discord

from modules import player_registration, player_registration_workers
from modules import player_timezone_values as values
from modules import utilities
import settings


logger = logging.getLogger('polybot.' + __name__)


class TimezoneValidationError(ValueError):
    """The submitted timezone request is invalid."""


class TimezonePermissionError(PermissionError):
    """The requester cannot operate on the selected member."""


class TimezoneTargetError(TimezoneValidationError):
    """The retained prefix target grammar was not unambiguous."""


def parse_timezone_offset(value, *, allow_gmt: bool = True) -> int:
    """Expose the shared parser at the service boundary for callers/tests."""

    try:
        return values.parse_timezone_offset(value, allow_gmt=allow_gmt)
    except values.TimezoneOffsetError as exc:
        raise TimezoneValidationError(str(exc)) from exc


def parse_native_timezone_offset(value) -> int:
    """Parse only one canonical ``UTC±HH:MM`` slash value."""

    minutes = parse_timezone_offset(value, allow_gmt=False)
    normalized = values.format_timezone_offset(minutes)
    if str(value).strip() != normalized:
        raise TimezoneValidationError(
            'Choose a normalized offset such as `UTC-05:00` from the list.'
        )
    return minutes


def normalize_timezone_offset(value, *, allow_gmt: bool = True) -> str:
    return values.normalize_timezone_offset(value, allow_gmt=allow_gmt)


def effective_timezone_offset_minutes(member) -> int | None:
    return values.effective_timezone_offset_minutes(member)


def format_timezone_offset(minutes: int | None) -> str | None:
    return values.format_timezone_offset(minutes)


def capture_member_snapshot(member):
    return player_registration.capture_member_snapshot(member)


def _is_staff(member) -> bool:
    try:
        return bool(settings.is_staff(member))
    except Exception:
        return False


def build_request(
    *,
    actor,
    guild_id: int,
    target=None,
    target_snapshot: player_registration_workers.MemberSnapshot | None = None,
    offset: str | int | None = None,
    clear: bool = False,
    invoked_with: str = '/player timezone',
    native: bool = True,
    prefix: str = '$',
) -> 'player_timezone_workers.PlayerTimezoneRequest':
    """Capture Discord values and validate the fast permission boundary."""

    from modules import player_timezone_workers

    actor_snapshot = capture_member_snapshot(actor)
    if target is not None:
        target_snapshot = capture_member_snapshot(target)
    if target_snapshot is None:
        target_snapshot = actor_snapshot

    requester_is_staff = _is_staff(actor)
    if (
        target_snapshot.discord_id != actor_snapshot.discord_id
        and not requester_is_staff
    ):
        raise TimezonePermissionError(
            'Only server staff can view or change another member\'s timezone.'
        )
    if bool(clear) and offset is not None:
        raise TimezoneValidationError(
            'Choose an offset or clear the preference, not both.'
        )

    offset_minutes = None
    if offset is not None:
        offset_minutes = (
            parse_native_timezone_offset(offset)
            if native
            else parse_timezone_offset(offset, allow_gmt=True)
        )

    return player_timezone_workers.PlayerTimezoneRequest(
        guild_id=int(guild_id),
        requester_id=actor_snapshot.discord_id,
        actor=actor_snapshot,
        target=target_snapshot,
        offset_minutes=offset_minutes,
        clear=bool(clear),
        requester_is_staff=requester_is_staff,
        native=bool(native),
        invoked_with=str(invoked_with),
        prefix=str(prefix),
    )


async def build_prefix_request(ctx, args):
    """Adapt the compatible self/staff-target ``$settime`` grammar."""

    args = tuple(str(value) for value in args)
    if len(args) == 1:
        target = ctx.author
        offset_text = args[0]
    elif len(args) == 2 and args[0].upper() in {'UTC', 'GMT'}:
        # Preserve ``$settime UTC +5`` as a self-targeted form.
        target = ctx.author
        offset_text = ''.join(args)
    elif len(args) == 2:
        if not _is_staff(ctx.author):
            raise TimezonePermissionError(
                'You do not have permission to trigger this command.'
            )
        matches = await utilities.get_guild_member(ctx, args[0])
        if len(matches) == 0:
            raise TimezoneTargetError(
                f'Could not find a server member matching *{args[0]}*.'
            )
        if len(matches) > 1:
            raise TimezoneTargetError(
                f'Found {len(matches)} server members matching *{args[0]}*. '
                'Try specifying with an @Mention.'
            )
        target = matches[0]
        offset_text = args[1]
    else:
        raise TimezoneTargetError(
            f'Wrong number of arguments. Use `{ctx.prefix}settime '
            'my_time_zone_offset`. Example: '
            f'`{ctx.prefix}settime UTC-5:00`.'
        )

    try:
        return build_request(
            actor=ctx.author,
            target=target,
            guild_id=ctx.guild.id,
            offset=offset_text,
            invoked_with='settime',
            native=False,
            prefix=ctx.prefix,
        )
    except TimezoneValidationError as exc:
        raise TimezoneValidationError(
            f'{exc}\nExample usage: `{ctx.prefix}settime @Player '
            'time_zone_offset`'
        ) from exc


def _offset_text(result) -> str:
    return values.format_timezone_offset(result.offset_minutes) or 'not set'


def public_message(request, result) -> str:
    """Format public native reads and committed writes with attribution."""

    actor = result.actor_description
    target = result.target_description
    if not result.mutated:
        if result.offset_minutes is None:
            state = 'no account-wide fixed UTC offset is set'
        else:
            state = (
                'the account-wide fixed UTC offset is **'
                f'{_offset_text(result)}**'
            )
        return f'{actor} read {target}: {state}.'
    if result.cleared:
        action = 'cleared the account-wide fixed UTC offset'
    else:
        action = (
            'set the account-wide fixed UTC offset to **'
            f'{_offset_text(result)}**'
        )
    return f'{actor} {action} for {target}.'


def prefix_success_message(request, result) -> str:
    """Keep the established public prefix confirmation shape, normalized."""

    return (
        f'Player **{discord.utils.escape_markdown(result.target_name, as_needed=True)}** '
        'updated in system with timezone offset **'
        f'{_offset_text(result)}**.'
    )


async def autocomplete_offsets(
    interaction: discord.Interaction,
    current: str,
) -> list[discord.app_commands.Choice[str]]:
    """Return cheap, normalized, at-most-25 UTC offset suggestions."""

    del interaction
    return [
        discord.app_commands.Choice(name=value, value=value)
        for value in values.offset_suggestions(current, limit=25)
    ]


def public_interaction_sender(interaction):
    """Clear one private defer, then send the committed/read result publicly."""

    cleared = False

    async def send(content, **kwargs):
        nonlocal cleared
        if not cleared:
            cleared = True
            delete_original = getattr(
                interaction,
                'delete_original_response',
                None,
            )
            if delete_original is not None:
                try:
                    await delete_original()
                except Exception:
                    logger.exception(
                        'Could not clear private timezone response before public output'
                    )
        channel = getattr(interaction, 'channel', None)
        channel_send = getattr(channel, 'send', None)
        if channel_send is None:
            raise RuntimeError('The interaction has no public channel sender.')
        return await channel_send(content, **kwargs)

    return send
