"""Discord-side service for confirmed native team archival."""

from __future__ import annotations

from dataclasses import dataclass

import discord

from modules import team_archive_workers as workers
from modules import team_attributes, team_attributes_workers, team_emoji, utilities


TeamArchiveActor = team_emoji.TeamEmojiActor
capture_actor = team_emoji.capture_actor


@dataclass(frozen=True)
class TeamArchivePreflight:
    team_id: int
    team_name: str
    team_role_id: int
    team_role_name: str


def native_access_error(member, guild_id: int) -> str | None:
    if not team_attributes._team_enabled(guild_id):
        return 'Teams are not enabled on this server.'
    if not team_attributes._league_scope(guild_id):
        return 'Teams can only be archived in the PolyChampions league server.'
    if not team_attributes._requester_is_mod(member):
        return 'You do not have permission to archive teams.'
    return None


async def run_preflight(*, member, guild, team_lookup: str) -> TeamArchivePreflight:
    """Resolve the Team off-loop, then capture its exact Discord role."""

    current = await team_attributes.run_read(
        team_attributes.build_read_request(
            member=member,
            guild_id=int(guild.id),
            attribute=team_attributes_workers.TEAM_ATTRIBUTE_TIER,
            team_lookup=team_lookup,
            invoked_with='/team archive',
        )
    )
    if current.is_archived:
        raise workers.TeamArchiveValidationError(
            f'Team **{current.team_name}** is already archived.'
        )
    role = utilities.guild_role_by_name(
        guild,
        name=current.team_name,
        allow_partial=False,
    )
    if role is None:
        raise workers.TeamArchiveValidationError(
            f':warning: No role exactly matching **{current.team_name}**. '
            'The Team must have its exact membership role before archival.'
        )
    return TeamArchivePreflight(
        team_id=int(current.team_id),
        team_name=str(current.team_name),
        team_role_id=int(role.id),
        team_role_name=str(role.name),
    )


def build_request(
    *,
    member,
    guild_id: int,
    preflight: TeamArchivePreflight,
    confirmed: bool,
) -> workers.TeamArchiveRequest:
    return workers.TeamArchiveRequest(
        guild_id=int(guild_id),
        requester_id=int(member.id),
        requester_is_mod=team_attributes._requester_is_mod(member),
        team_enabled=team_attributes._team_enabled(guild_id),
        league_scope=team_attributes._league_scope(guild_id),
        team_lookup=str(preflight.team_name),
        expected_team_id=int(preflight.team_id),
        team_role_id=int(preflight.team_role_id),
        team_role_name=str(preflight.team_role_name),
        requester_description=capture_actor(member).identity,
        confirmed=bool(confirmed),
    )


async def run_archive(request: workers.TeamArchiveRequest):
    return await workers.run_team_archive(request)


def success_message(
    result: workers.TeamArchiveResult,
    *,
    actor: TeamArchiveActor,
) -> str:
    team_name = discord.utils.escape_mentions(
        discord.utils.escape_markdown(result.team_name)
    )
    return (
        f':warning: {actor.label} successfully archived Team '
        f'**{team_name}** (ID `{result.team_id}`). It will no longer appear '
        'in ordinary active-Team workflows. This operation cannot be undone '
        'through the bot.'
    )


def display_team_name(value: str) -> str:
    """Escape a committed Team name before private reconciliation output."""

    return discord.utils.escape_mentions(discord.utils.escape_markdown(value))
