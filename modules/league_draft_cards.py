"""Discord-facing service for native league draft cards."""

from __future__ import annotations

import io

import discord

import settings
from modules import house_show, interaction_lifecycle
from modules import league_draft_cards_workers as workers


def access_error(member, guild_id: int) -> str | None:
    if not house_show._league_scope(int(guild_id)):
        return 'Draft cards are available only in the configured league server.'
    try:
        allowed = settings.is_staff(member) or any(
            str(getattr(role, 'name', '')) == 'Drafter'
            for role in tuple(getattr(member, 'roles', ()) or ())
        )
    except Exception:
        allowed = False
    if not allowed:
        return 'Only Drafters, Helpers, Mods, and the bot owner can create draft cards.'
    return None


def avatar_url(member) -> str:
    return str(member.display_avatar.replace(size=256, format='png'))


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


def request(*, guild, player, team: str) -> workers.DraftCardRequest:
    return workers.DraftCardRequest(
        guild_id=int(guild.id),
        player_discord_id=int(player.id),
        player_name=str(player.name),
        player_avatar_url=avatar_url(player),
        team_name=str(team).strip(),
        role_colours=capture_role_colours(guild),
    )


def discord_file(result: workers.DraftCardResult) -> discord.File:
    return discord.File(io.BytesIO(result.image_bytes), filename=result.filename)


def public_caption(actor, result: workers.DraftCardResult) -> str:
    return (
        f'{actor.mention} generated a draft card for '
        f'**{result.player_name}** selecting **{result.team_name}**.'
    )


run_draft_card = workers.run_draft_card
public_interaction_sender = interaction_lifecycle.public_interaction_sender
