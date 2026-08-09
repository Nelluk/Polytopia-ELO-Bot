"""Shared Free Agent signup-announcement lifecycle for slash and prefix."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import datetime
import logging

import discord

from modules import house_show, league_free_agents_workers as workers, utilities
import settings


logger = logging.getLogger('polybot.' + __name__)

PRODUCTION_DEFAULT_CHANNEL_ID = 1326604735863721984
DEVELOPMENT_DEFAULT_CHANNEL_ID = 480078679930830849
GRAD_ROLE_NAME = 'Nova Grad'
NOVAS_ROLE_NAME = 'The Novas'
FREE_AGENT_ROLE_NAME = 'Free Agent'
SIGNUP_EMOJI = '🔆'
CLOSE_EMOJI = '⏯'
CONCLUDE_EMOJI = '❎'
REACTIONS = (SIGNUP_EMOJI, CLOSE_EMOJI, CONCLUDE_EMOJI)


class FreeAgentPostDiscordError(workers.FreeAgentPostError):
    """A Discord preflight or pre-commit effect failed safely."""


class FreeAgentPostDuplicateError(workers.FreeAgentPostError):
    """An existing live signup announcement already owns the workflow."""


class FreeAgentPostReconciliationError(workers.FreeAgentPostError):
    """An uncommitted Discord announcement could not be removed."""


@dataclass(frozen=True)
class FreeAgentRoleSnapshot:
    grad_role_id: int
    grad_mention: str
    novas_role_id: int
    novas_mention: str
    free_agent_role_id: int
    free_agent_mention: str


@dataclass(frozen=True)
class FreeAgentPostResult:
    guild_id: int
    requester_id: int
    channel_id: int
    message_id: int
    message_link: str


def league_scope(guild_id: int) -> bool:
    return bool(house_show._league_scope(int(guild_id)))


def is_mod(member) -> bool:
    try:
        return bool(settings.is_mod(member))
    except Exception:
        return False


def access_error(member, guild_id: int) -> str | None:
    if not league_scope(guild_id):
        return 'Free Agent announcements are available only in the configured league server.'
    if not is_mod(member):
        return 'Only a Mod can post a Free Agent signup announcement.'
    return None


def default_channel(guild):
    channel_id = (
        PRODUCTION_DEFAULT_CHANNEL_ID
        if int(guild.id) == int(settings.server_ids['polychampions'])
        else DEVELOPMENT_DEFAULT_CHANNEL_ID
    )
    return guild.get_channel(channel_id)


def capture_roles(guild) -> FreeAgentRoleSnapshot:
    roles = {
        role.name: role
        for role in tuple(getattr(guild, 'roles', ()) or ())
        if role.name in {GRAD_ROLE_NAME, NOVAS_ROLE_NAME, FREE_AGENT_ROLE_NAME}
    }
    missing = [
        name for name in (GRAD_ROLE_NAME, NOVAS_ROLE_NAME, FREE_AGENT_ROLE_NAME)
        if name not in roles
    ]
    if missing:
        raise FreeAgentPostDiscordError(
            'The signup announcement cannot be posted until these exact roles '
            f'exist: {", ".join(missing)}.'
        )
    return FreeAgentRoleSnapshot(
        grad_role_id=int(roles[GRAD_ROLE_NAME].id),
        grad_mention=str(roles[GRAD_ROLE_NAME].mention),
        novas_role_id=int(roles[NOVAS_ROLE_NAME].id),
        novas_mention=str(roles[NOVAS_ROLE_NAME].mention),
        free_agent_role_id=int(roles[FREE_AGENT_ROLE_NAME].id),
        free_agent_mention=str(roles[FREE_AGENT_ROLE_NAME].mention),
    )


def normalize_added_message(value: str | None) -> str:
    message = str(value or '').strip()
    if len(message) > workers.MAX_ADDED_MESSAGE_LENGTH:
        raise workers.FreeAgentPostError(
            f'The additional message is limited to '
            f'{workers.MAX_ADDED_MESSAGE_LENGTH:,} characters.'
        )
    if any(ord(character) < 32 and character not in '\n\t' for character in message):
        raise workers.FreeAgentPostError(
            'The additional message contains unsupported control characters.'
        )
    return message


def announcement_content(
    *,
    roles: FreeAgentRoleSnapshot,
    added_message: str,
    actor_mention: str,
) -> str:
    content = (
        f'The league is now open for Free Agent signups! {roles.grad_mention}s '
        f'can react with a {SIGNUP_EMOJI} below to sign up. '
        f'{roles.novas_mention} who have not graduated have until the end of '
        'the signup period to meet requirements and sign up. If Free Agents '
        'have favorite teams, they may react to the team emojis in '
        '<#1489844936202260710> to note those preferences.'
    )
    if added_message:
        content += f'\n\n{added_message}'
    content += f'\n\n-# Signup opened by {actor_mention}'
    if len(content) > 2_000:
        raise workers.FreeAgentPostError(
            'The complete announcement exceeds Discord’s 2,000-character '
            'message limit. Shorten the additional message.'
        )
    return content


def preview_content(
    *,
    channel,
    roles: FreeAgentRoleSnapshot,
    added_message: str,
    actor_mention: str,
) -> str:
    return (
        '# Free Agent signup preview\n'
        f'**Destination:** {channel.mention}\n'
        f'**Seeded reactions:** {" ".join(REACTIONS)}\n\n'
        + announcement_content(
            roles=roles,
            added_message=added_message,
            actor_mention=actor_mention,
        )
    )


async def _existing_message(guild, state: workers.DraftState):
    if state.announcement_message_id is None or state.announcement_channel_id is None:
        return None
    channel = guild.get_channel(int(state.announcement_channel_id))
    if channel is None or not callable(getattr(channel, 'fetch_message', None)):
        return None
    try:
        return await channel.fetch_message(int(state.announcement_message_id))
    except discord.NotFound:
        return None
    except discord.DiscordException as exc:
        raise FreeAgentPostDiscordError(
            'The existing Free Agent announcement could not be checked. Try '
            'again before creating another signup.'
        ) from exc


async def _remove_uncommitted(message, reason: str) -> bool:
    try:
        await message.delete()
        return True
    except Exception:
        logger.exception(
            'Could not remove uncommitted Free Agent announcement %s',
            getattr(message, 'id', None),
        )
    try:
        current = str(getattr(message, 'content', '') or '')
        warning = (
            '\n\n⚠️ **This signup was not activated. Do not use its '
            f'reactions.** Staff reconciliation required: {reason}'
        )
        await message.edit(content=(current + warning)[:2_000])
    except Exception:
        logger.exception(
            'Could not mark uncommitted Free Agent announcement %s',
            getattr(message, 'id', None),
        )
    return False


async def post_announcement(
    *,
    cog,
    guild,
    actor,
    channel,
    added_message: str,
) -> FreeAgentPostResult:
    """Serialize, publish, persist, and expose one active signup pointer."""

    workers.free_agent_post_coordinator.claim()
    announcement = None
    committed = False
    cleanup_attempted = False
    try:
        error = access_error(actor, int(guild.id))
        if error:
            raise workers.FreeAgentPostError(error)
        channel_guild = getattr(channel, 'guild', None)
        if channel is None or int(getattr(channel_guild, 'id', 0)) != int(guild.id):
            raise FreeAgentPostDiscordError(
                'Choose a text channel from this server for the announcement.'
            )
        roles = capture_roles(guild)
        added_message = normalize_added_message(added_message)
        state = await workers.run_load_draft_state(int(guild.id))
        existing = await _existing_message(guild, state)
        if existing is not None:
            raise FreeAgentPostDuplicateError(
                'A Free Agent signup announcement is already active. Conclude '
                'it with the ❎ reaction before posting another one:\n'
                f'https://discord.com/channels/{int(guild.id)}/'
                f'{int(state.announcement_channel_id)}/'
                f'{int(state.announcement_message_id)}'
            )

        content = announcement_content(
            roles=roles,
            added_message=added_message,
            actor_mention=str(actor.mention),
        )
        try:
            announcement = await channel.send(
                content,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    users=[actor],
                    roles=[
                        discord.Object(id=roles.grad_role_id),
                        discord.Object(id=roles.novas_role_id),
                        discord.Object(id=roles.free_agent_role_id),
                    ],
                    replied_user=False,
                ),
            )
            for emoji in REACTIONS:
                await announcement.add_reaction(emoji)
        except Exception as exc:
            if announcement is not None:
                cleanup_attempted = True
                removed = await _remove_uncommitted(
                    announcement,
                    'Discord reaction setup failed.',
                )
                if not removed:
                    raise FreeAgentPostReconciliationError(
                        'The announcement could not be fully prepared or '
                        'removed. Do not retry; staff must delete or reconcile '
                        f'{announcement.jump_url}.'
                    ) from exc
                announcement = None
            raise FreeAgentPostDiscordError(
                'The signup announcement or its reactions could not be '
                'created. No active signup was recorded.'
            ) from exc

        try:
            persisted = await workers.run_persist_draft_state(
                workers.DraftPersistRequest(
                    guild_id=int(guild.id),
                    requester_id=int(actor.id),
                    requester_name=str(
                        getattr(actor, 'display_name', None)
                        or getattr(actor, 'name', None)
                        or f'user-{int(actor.id)}'
                    )[:100],
                    expected_message_id=state.announcement_message_id,
                    expected_channel_id=state.announcement_channel_id,
                    announcement_message_id=int(announcement.id),
                    announcement_channel_id=int(channel.id),
                    added_message=added_message,
                    opened_at=datetime.datetime.now(datetime.UTC).isoformat(),
                )
            )
            committed = True
        except (Exception, asyncio.CancelledError) as exc:
            cleanup_attempted = True
            removed = await _remove_uncommitted(
                announcement,
                'Database persistence failed.',
            )
            if not removed:
                raise FreeAgentPostReconciliationError(
                    'The signup pointer was not committed and the public '
                    'message could not be removed. Do not retry; staff must '
                    f'delete or reconcile {announcement.jump_url}.'
                ) from exc
            announcement = None
            if isinstance(exc, workers.FreeAgentPostError):
                raise
            raise FreeAgentPostDiscordError(
                'The signup announcement was removed because its state could '
                'not be saved. It is safe to retry.'
            ) from exc

        cog.announcement_message = int(persisted.announcement_message_id)
        link = (
            f'https://discord.com/channels/{int(guild.id)}/'
            f'{int(channel.id)}/{int(announcement.id)}'
        )
        try:
            await utilities.send_to_log_channel(
                guild,
                f'Free Agent signup announcement opened by {actor.mention}\n{link}',
            )
        except Exception:
            logger.exception(
                'Committed Free Agent announcement %s log relay failed',
                announcement.id,
            )
        return FreeAgentPostResult(
            guild_id=int(guild.id),
            requester_id=int(actor.id),
            channel_id=int(channel.id),
            message_id=int(announcement.id),
            message_link=link,
        )
    except (Exception, asyncio.CancelledError):
        if announcement is not None and not committed and not cleanup_attempted:
            # Most error paths already remove it; this idempotent second
            # attempt protects cancellation between Discord posting and the
            # persistence call.
            await _remove_uncommitted(announcement, 'The posting operation ended early.')
        raise
    finally:
        workers.free_agent_post_coordinator.release()
