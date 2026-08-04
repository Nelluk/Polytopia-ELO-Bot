"""Native adapter for the focused `/team create` workflow."""

from __future__ import annotations

import logging

import discord

import settings
from modules import exceptions, team_creation_workers, team_emoji


logger = logging.getLogger('polybot.' + __name__)

TeamCreationActor = team_emoji.TeamEmojiActor
capture_actor = team_emoji.capture_actor


def _team_enabled(guild_id: int) -> bool:
    try:
        return bool(settings.guild_setting(int(guild_id), 'allow_teams'))
    except (AttributeError, KeyError, TypeError, exceptions.CheckFailedError):
        return False


def _requester_is_mod(member) -> bool:
    try:
        return bool(settings.is_mod(member))
    except (AttributeError, TypeError, exceptions.CheckFailedError):
        return False


def native_access_error(member, guild_id: int) -> str | None:
    """Return the private pre-defer denial matching the prefix boundary."""

    if not _team_enabled(guild_id):
        return 'Teams are not enabled on this server.'
    if not _requester_is_mod(member):
        return 'You do not have permission to create teams.'
    return None


def build_request(
    *,
    member,
    guild_id: int,
    name: str | None,
    native: bool = True,
    invoked_with: str = '/team create',
) -> team_creation_workers.TeamCreationRequest:
    """Capture only immutable primitive/member-safe values."""

    return team_creation_workers.TeamCreationRequest(
        guild_id=int(guild_id),
        requester_id=int(member.id),
        requester_is_mod=_requester_is_mod(member),
        team_enabled=_team_enabled(guild_id),
        name=str(name) if name is not None else None,
        requester_description=capture_actor(member).identity,
        native=bool(native),
        invoked_with=str(invoked_with),
    )


async def run_create(
    request: team_creation_workers.TeamCreationRequest,
) -> team_creation_workers.TeamCreationResult:
    return await team_creation_workers.run_team_creation(request)


def _display(value: str) -> str:
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value)),
    )


def native_success_message(
    result: team_creation_workers.TeamCreationResult,
    *,
    actor: TeamCreationActor,
) -> str:
    """Render public committed success with the membership convention."""

    team_name = _display(result.team_name)
    return (
        f'{actor.label} created Team **{team_name}** (ID `{result.team_id}`).\n'
        f'Players with a Discord role exactly matching **{team_name}** are '
        'considered team members. Use the focused `/team emoji`, `/team '
        'image`, `/team name`, `/team server`, `/team house`, and `/team '
        'tier` commands to configure team attributes.'
    )


async def publish_success(result, *, send, actor: TeamCreationActor) -> None:
    """Publish only after the worker transaction has committed."""

    try:
        await send(native_success_message(result, actor=actor))
    except Exception:
        logger.exception(
            'Committed team creation for team %s could not publish',
            result.team_id,
        )
        try:
            await send(
                f':warning: Team **{_display(result.team_name)}** was created '
                'but the public success message could not be sent. An '
                'operator must reconcile the team presentation.'
            )
        except Exception:
            logger.exception('Team creation warning could not be sent')
