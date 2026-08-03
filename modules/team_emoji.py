"""Shared service for the focused team-emoji read/edit workflow."""

from __future__ import annotations

from dataclasses import dataclass
import logging

import discord

import settings
from modules import exceptions, team_emoji_workers


logger = logging.getLogger('polybot.' + __name__)


@dataclass(frozen=True)
class TeamEmojiActor:
    """Safe, event-loop-captured identity for public native output."""

    discord_id: int
    mention: str
    identity: str

    @property
    def label(self) -> str:
        return f'{self.mention} / {self.identity}'


def capture_actor(member) -> TeamEmojiActor:
    """Capture stable identity text before submitting worker work."""

    discord_id = int(member.id)
    raw_name = str(
        getattr(member, 'display_name', None)
        or getattr(member, 'name', None)
        or f'user-{discord_id}'
    )
    safe_name = discord.utils.escape_mentions(
        discord.utils.escape_markdown(raw_name),
    )
    mention = getattr(member, 'mention', None)
    if callable(mention):
        mention = mention()
    return TeamEmojiActor(
        discord_id=discord_id,
        mention=str(mention or f'<@{discord_id}>'),
        identity=f'**{safe_name}** (`{discord_id}`)',
    )


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


def _requester_description(member) -> str:
    return capture_actor(member).identity


def build_read_request(
    *,
    member,
    guild_id: int,
    team_lookup: str | None = None,
    invoked_with: str = 'team_emoji',
) -> team_emoji_workers.TeamEmojiReadRequest:
    """Capture only immutable primitive values for a read worker."""

    return team_emoji_workers.TeamEmojiReadRequest(
        guild_id=int(guild_id),
        requester_id=int(member.id),
        requester_is_mod=_requester_is_mod(member),
        team_enabled=_team_enabled(guild_id),
        team_lookup=(str(team_lookup) if team_lookup is not None else None),
        requester_description=_requester_description(member),
        invoked_with=str(invoked_with),
    )


def build_mutation_request(
    *,
    member,
    guild_id: int,
    team_lookup: str | None = None,
    emoji: str | None = None,
    clear: bool = False,
    expected_emoji: str | None = None,
    native: bool = True,
    invoked_with: str = '/team emoji',
) -> team_emoji_workers.TeamEmojiMutationRequest:
    """Capture Discord/member values into an immutable mutation request."""

    return team_emoji_workers.TeamEmojiMutationRequest(
        guild_id=int(guild_id),
        requester_id=int(member.id),
        requester_is_mod=_requester_is_mod(member),
        team_enabled=_team_enabled(guild_id),
        team_lookup=(str(team_lookup) if team_lookup is not None else None),
        emoji=(str(emoji) if emoji is not None else None),
        clear=bool(clear),
        requester_description=_requester_description(member),
        expected_emoji=(
            str(expected_emoji) if expected_emoji is not None else None
        ),
        native=bool(native),
        invoked_with=str(invoked_with),
    )


async def run_read(
    request: team_emoji_workers.TeamEmojiReadRequest,
) -> team_emoji_workers.TeamEmojiReadResult:
    return await team_emoji_workers.run_team_emoji_read(request)


async def run_mutation(
    request: team_emoji_workers.TeamEmojiMutationRequest,
    *,
    after_commit=None,
) -> team_emoji_workers.TeamEmojiMutationResult:
    result = await team_emoji_workers.run_team_emoji_mutation(request)
    if after_commit is not None:
        await after_commit(result)
    return result


def _display(value: str | None) -> str:
    if value is None or value == '':
        return 'None'
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value)),
    )


def _team_display(value: str) -> str:
    return discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value)),
    )


def read_message(
    result: team_emoji_workers.TeamEmojiReadResult,
    *,
    actor: TeamEmojiActor | None = None,
) -> str:
    """Render a current-value read."""

    message = (
        f'Emoji for team **{_team_display(result.team_name)}**: '
        f'{_display(result.emoji)}'
    )
    if actor is not None:
        message += f'\nRequested by {actor.label}.'
    return message


def legacy_read_message(
    result: team_emoji_workers.TeamEmojiReadResult,
) -> str:
    """Preserve the established `$team_emoji` read wording exactly."""

    return f'Emoji for team **{result.team_name}**: {result.emoji}'


def native_mutation_message(
    result: team_emoji_workers.TeamEmojiMutationResult,
    *,
    actor: TeamEmojiActor,
) -> str:
    team_name = _team_display(result.team_name)
    if result.cleared:
        return f'{actor.label} cleared the emoji for Team **{team_name}**.'
    return (
        f'{actor.label} updated the emoji for Team **{team_name}** to '
        f'{_display(result.emoji)}.'
    )


def legacy_mutation_message(
    result: team_emoji_workers.TeamEmojiMutationResult,
) -> str:
    """Preserve the established `$team_emoji` success wording."""

    return (
        f'Team **{result.team_name}** updated with new emoji: '
        f'{result.emoji}'
    )


async def _send_reconciliation_warning(send, content: str) -> None:
    try:
        await send(content)
    except Exception:
        logger.exception('Committed team-emoji warning could not be sent')


async def publish_mutation_result(
    result: team_emoji_workers.TeamEmojiMutationResult,
    *,
    send,
    actor: TeamEmojiActor | None = None,
) -> None:
    """Publish committed output; callers may add future consumer refreshes."""

    message = (
        legacy_mutation_message(result)
        if actor is None
        else native_mutation_message(result, actor=actor)
    )
    try:
        await send(message)
    except Exception:
        logger.exception(
            'Committed team-emoji mutation for team %s could not publish',
            result.team_id,
        )
        await _send_reconciliation_warning(
            send,
            f':warning: Team **{_team_display(result.team_name)}** emoji was '
            'saved, but the public success message could not be sent. An '
            'operator must reconcile the team presentation.',
        )


def public_interaction_sender(interaction):
    """Return a public sender that clears one private deferred response."""

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
                        'Could not clear the private deferred team-emoji '
                        'response before public output'
                    )
        channel = getattr(interaction, 'channel', None)
        channel_send = getattr(channel, 'send', None)
        if channel_send is None:
            raise RuntimeError('The interaction has no public channel sender.')
        return await channel_send(content, **kwargs)

    return send
