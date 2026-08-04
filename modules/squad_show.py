"""Async adapters and public presentation helpers for ``/squad show``."""

from __future__ import annotations

import asyncio
import logging

import discord

import settings
from modules import exceptions, squad_show_workers


logger = logging.getLogger('polybot.' + __name__)

SQUAD_SHOW_CONTROL_TIMEOUT = 300.0
PRIVATE_RESPONSE_DELETE_TIMEOUT = 2.0


def _setting(guild_id: int, name: str, default=None):
    try:
        return settings.guild_setting(int(guild_id), name)
    except (AttributeError, KeyError, TypeError, exceptions.CheckFailedError):
        return default


def _is_mod(member) -> bool:
    try:
        return bool(settings.is_mod(member))
    except (AttributeError, TypeError, exceptions.CheckFailedError):
        return False


def _is_staff(member) -> bool:
    try:
        return bool(settings.is_staff(member))
    except (AttributeError, TypeError, exceptions.CheckFailedError):
        return False


def _member_id(member) -> int | None:
    value = getattr(member, 'id', member)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _channel_allowed(member, guild_id: int, channel_id: int | None) -> bool:
    """Mirror the retained non-strict ``in_bot_channel`` prefix check."""

    bot_channels = _setting(guild_id, 'bot_channels', None)
    if bot_channels is None or _is_mod(member):
        return True
    private_channels = _setting(guild_id, 'bot_channels_private', ()) or ()
    try:
        allowed_channels = {
            int(value)
            for value in (*bot_channels, *private_channels)
        }
    except (TypeError, ValueError):
        return False
    return channel_id is not None and int(channel_id) in allowed_channels


def native_access_error(
    member,
    guild_id: int,
    channel_id: int | None,
) -> str | None:
    """Return the private preflight error for the legacy read boundary."""

    if not bool(_setting(guild_id, 'allow_teams', False)):
        return 'Teams are not enabled on this server.'
    if _channel_allowed(member, guild_id, channel_id):
        return None
    bot_channels = _setting(guild_id, 'bot_channels', ()) or ()
    tags = ' '.join(f'<#{int(value)}>' for value in bot_channels)
    return (
        'This command can only be used in a designated ELO bot channel. '
        f'Try: {tags}'
    )


def capture_member_ids(values) -> tuple[int, ...]:
    """Freeze a UserSelect value list into one-to-three primitive IDs."""

    values = tuple(values or ())
    if not (
        squad_show_workers.SQUAD_MEMBER_MIN
        <= len(values)
        <= squad_show_workers.SQUAD_MEMBER_MAX
    ):
        raise squad_show_workers.SquadShowValidationError(
            'Choose between one and three different Discord members.'
        )
    member_ids = tuple(_member_id(value) for value in values)
    if any(member_id is None or member_id <= 0 for member_id in member_ids):
        raise squad_show_workers.SquadShowValidationError(
            'Every selected Discord member must be valid.'
        )
    if len(set(member_ids)) != len(member_ids):
        raise squad_show_workers.SquadShowValidationError(
            'Choose each Discord member only once.'
        )
    return tuple(int(member_id) for member_id in member_ids)


def build_request(
    *,
    member,
    guild,
    squad_id: int | None = None,
    member_ids: tuple[int, ...] | None = None,
    channel_id: int | None = None,
) -> squad_show_workers.SquadShowRequest:
    """Capture Discord/config values before submitting a bounded read."""

    guild_id = int(guild.id)
    captured_member_ids = (
        capture_member_ids(member_ids)
        if member_ids is not None
        else (int(member.id),)
    )
    return squad_show_workers.SquadShowRequest(
        guild_id=guild_id,
        requester_id=int(member.id),
        member_ids=captured_member_ids,
        squad_id=(int(squad_id) if squad_id is not None else None),
        team_enabled=bool(_setting(guild_id, 'allow_teams', False)),
        channel_allowed=_channel_allowed(member, guild_id, channel_id),
        requester_is_staff=_is_staff(member),
    )


def public_interaction_sender(interaction):
    """Clear one private deferred response before sending the public view."""

    cleared = False

    async def send(content=None, **kwargs):
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
                    await asyncio.wait_for(
                        delete_original(),
                        timeout=PRIVATE_RESPONSE_DELETE_TIMEOUT,
                    )
                except TimeoutError:
                    logger.warning(
                        'Timed out clearing private squad-show response; '
                        'continuing with public output'
                    )
                except Exception:
                    logger.exception(
                        'Could not clear private squad-show response before '
                        'public output'
                    )
        channel = getattr(interaction, 'channel', None)
        channel_send = getattr(channel, 'send', None)
        if channel_send is None:
            raise RuntimeError('The interaction has no public channel sender.')
        if content is not None:
            kwargs = {'content': content, **kwargs}
        return await channel_send(**kwargs)

    return send


async def publish_native(interaction, view) -> object:
    """Publish a successful snapshot publicly and retain its message handle."""

    sender = public_interaction_sender(interaction)
    message = await sender(view=view)
    view.message = message
    return message
