"""Shared Discord-facing service for promotion and trade cards."""

from __future__ import annotations

import io

import discord

import settings
from modules import (
    house_show,
    interaction_lifecycle,
    league_roster_cards_workers as workers,
    utilities,
)


def access_error(member, guild_id: int, channel_id: int | None) -> str | None:
    if not house_show._league_scope(int(guild_id)):
        return 'Roster cards are available only in the configured league server.'
    try:
        if not settings.is_staff(member):
            return 'Only Helpers, Mods, and the bot owner can create roster cards.'
    except Exception:
        return 'Only Helpers, Mods, and the bot owner can create roster cards.'

    strict = house_show._setting(guild_id, 'bot_channels_strict', None)
    ordinary = house_show._setting(guild_id, 'bot_channels', None)
    if strict is None and ordinary is None:
        return None
    if settings.is_mod(member):
        return None
    private = house_show._setting(guild_id, 'bot_channels_private', ()) or ()
    allowed = strict if strict is not None else ordinary
    try:
        channel_ids = {int(value) for value in (*(allowed or ()), *private)}
    except (TypeError, ValueError):
        return 'The configured bot-channel policy is invalid.'
    if channel_id is not None and int(channel_id) in channel_ids:
        return None
    return (
        'This command can only be used in a designated bot spam channel. Try: '
        + ' '.join(f'<#{int(value)}>' for value in (allowed or ()))
    )


def capture_role_colours(guild) -> tuple[workers.RoleColourSnapshot, ...]:
    rows = []
    for role in tuple(getattr(guild, 'roles', ()) or ()):
        colour = getattr(role, 'colour', None) or getattr(role, 'color', None)
        numeric = getattr(colour, 'value', None)
        rows.append(
            workers.RoleColourSnapshot(
                name=str(role.name),
                colour=(f'#{int(numeric):06x}' if isinstance(numeric, int) else str(colour)),
            )
        )
    return tuple(rows)


def avatar_url(member) -> str:
    avatar = member.display_avatar.replace(size=512, format='png')
    return str(avatar)


def raw_or_avatar(raw_url: str | None, member) -> workers.ImageSource:
    if raw_url is not None and str(raw_url).strip():
        return workers.ImageSource(kind='url', value=str(raw_url).strip())
    return workers.ImageSource(kind='url', value=avatar_url(member))


def raw_or_team(raw_url: str | None, team: str) -> workers.ImageSource:
    return workers.ImageSource(
        kind='team',
        value=str(team).strip(),
        fallback_url=(str(raw_url).strip() if raw_url is not None else None),
    )


async def prefix_lookup_source(ctx, value: str) -> workers.ImageSource:
    value = str(value or '').strip()
    if value.casefold().startswith(('http://', 'https://')):
        return workers.ImageSource(kind='url', value=value)
    matches = await utilities.get_guild_member(ctx, value)
    fallback = avatar_url(matches[0]) if len(matches) == 1 else None
    return workers.ImageSource(kind='lookup', value=value, fallback_url=fallback)


def request(
    *, guild, mode: str, top_text: str, bottom_text: str,
    left: workers.ImageSource, right: workers.ImageSource,
) -> workers.RosterCardRequest:
    return workers.RosterCardRequest(
        guild_id=int(guild.id),
        mode=str(mode),
        top_text=str(top_text),
        bottom_text=str(bottom_text),
        left=left,
        right=right,
        role_colours=capture_role_colours(guild),
    )


def discord_file(result: workers.RosterCardResult) -> discord.File:
    return discord.File(io.BytesIO(result.image_bytes), filename=result.filename)


def public_caption(actor, mode: str) -> str:
    action = 'generated a promotion card' if mode == 'promote' else 'generated a trade card'
    return f'{actor.mention} {action}.'


run_roster_card = workers.run_roster_card
public_interaction_sender = interaction_lifecycle.public_interaction_sender
