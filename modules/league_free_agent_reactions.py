"""Asynchronous lifecycle for the retained Free Agent announcement reactions."""

from __future__ import annotations

import asyncio
import logging

import discord

from modules import league_free_agents_workers as workers, utilities
import settings


logger = logging.getLogger('polybot.' + __name__)

# Gateway reaction events are ordered through one process-local lock so a
# signup cannot cross a close/conclude transition between its state read and
# Discord role effect. The authoritative database transitions also lock the
# Configuration row for cross-task safety.
reaction_lifecycle_lock = asyncio.Lock()


def _actor_name(member) -> str:
    return str(
        getattr(member, 'display_name', None)
        or getattr(member, 'name', None)
        or f'user-{int(member.id)}'
    )[:100]


async def _remove_reaction(message, emoji: str, member) -> None:
    try:
        await message.remove_reaction(emoji, member)
    except discord.DiscordException:
        logger.warning(
            'Could not remove Free Agent reaction %s from member %s on message %s',
            emoji,
            getattr(member, 'id', None),
            getattr(message, 'id', None),
            exc_info=True,
        )


async def _direct_message(member, content: str) -> None:
    if not content:
        return
    try:
        await member.send(content)
    except discord.DiscordException:
        logger.warning(
            'Could not send Free Agent lifecycle DM to member %s',
            getattr(member, 'id', None),
            exc_info=True,
        )


async def _relay_log(guild, content: str) -> None:
    try:
        await utilities.send_to_log_channel(guild, content)
    except Exception:
        logger.exception('Free Agent staff-log relay failed for guild %s', guild.id)


async def _publish_reconciliation(channel, content: str) -> None:
    try:
        await channel.send(
            content,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.DiscordException:
        logger.exception(
            'Could not publish Free Agent reconciliation warning in channel %s',
            getattr(channel, 'id', None),
        )


def _expected_state(state, *, guild_id: int, channel_id: int, message_id: int) -> None:
    if (
        int(state.guild_id) != int(guild_id)
        or state.announcement_channel_id != int(channel_id)
        or state.announcement_message_id != int(message_id)
    ):
        raise workers.FreeAgentPostConflictError(
            'This is no longer the active Free Agent signup announcement.'
        )


async def handle_signup_reaction(
    *,
    member,
    channel,
    message,
    reaction_added: bool,
    signup_emoji: str,
    grad_role_name: str,
    free_agent_role_name: str,
) -> None:
    """Apply or remove the role while retaining the reaction roster UX."""

    if member is None:
        return
    guild = member.guild
    grad_role = discord.utils.get(guild.roles, name=grad_role_name)
    free_agent_role = discord.utils.get(guild.roles, name=free_agent_role_name)
    if grad_role is None or free_agent_role is None:
        if reaction_added:
            await _remove_reaction(message, signup_emoji, member)
        await _direct_message(
            member,
            'The Free Agent signup roles are not configured correctly. Staff '
            'have been asked to reconcile the signup.',
        )
        logger.error(
            'Free Agent signup roles missing in guild %s (grad=%s free_agent=%s)',
            guild.id,
            bool(grad_role),
            bool(free_agent_role),
        )
        return

    link = (
        f'https://discord.com/channels/{int(guild.id)}/'
        f'{int(channel.id)}/{int(message.id)}'
    )
    async with reaction_lifecycle_lock:
        try:
            state = await workers.run_load_draft_state(int(guild.id))
            _expected_state(
                state,
                guild_id=guild.id,
                channel_id=channel.id,
                message_id=message.id,
            )
        except Exception:
            logger.exception(
                'Could not load authoritative Free Agent signup state for guild %s',
                guild.id,
            )
            if reaction_added:
                await _remove_reaction(message, signup_emoji, member)
            await _direct_message(
                member,
                'The signup could not be verified, so no role was changed. '
                'Please try again or contact staff.',
            )
            return

        action = None
        member_message = ''
        if reaction_added:
            if state.draft_open and grad_role in member.roles:
                try:
                    await member.add_roles(
                        free_agent_role,
                        reason='Member signed up as Free Agent',
                    )
                except discord.DiscordException:
                    logger.exception(
                        'Could not add Free Agent role to member %s', member.id
                    )
                    await _remove_reaction(message, signup_emoji, member)
                    await _direct_message(
                        member,
                        'Discord could not add the Free Agent role. Your signup '
                        'reaction was removed; please try again or contact staff.',
                    )
                    return
                action = 'join'
                member_message = (
                    'You now are signed up for the PolyChampions Auction 🎉\n\n'
                    'You may be contacted by recruiters. It is in your best '
                    'interest to chat and get to know the different houses. Be '
                    'open minded. Ask questions. (If a recruiter trashes another '
                    'team or forces you to choose a team before the auction, '
                    'please report this to mods.)\n\nOnce you talk to some '
                    'recruiters, you may indicate preferences for certain houses. '
                    'Before the bidding starts on Sunday, please react to the team '
                    'emojis in <#1489844936202260710> to note your favorite(s). '
                    'Only the house(s) you select will be allowed to place a bid '
                    "on you. If you don't select, then any house may bid on you. "
                    f'\n{link}'
                )
            else:
                await _remove_reaction(message, signup_emoji, member)
                if not state.draft_open:
                    member_message = (
                        'The draft has been closed to new signups - your signup '
                        'has been rejected.'
                    )
                else:
                    member_message = (
                        'Your signup has been rejected. You do not have the '
                        f'**{grad_role.name}** role. Try again once you have met '
                        'the graduation requirements.'
                    )
        elif free_agent_role in member.roles:
            try:
                await member.remove_roles(
                    free_agent_role,
                    reason='Member removed from Free Agent signup',
                )
            except discord.DiscordException:
                logger.exception(
                    'Could not remove Free Agent role from member %s', member.id
                )
                await _direct_message(
                    member,
                    'Discord could not remove the Free Agent role. Contact staff '
                    'if the role remains after you removed your reaction.',
                )
                return
            action = 'leave'
            member_message = (
                'You have been removed from the Free Agent list. You can sign '
                f'back up at the announcement message:\n{link}'
            )
        else:
            return

        if action is not None:
            try:
                await workers.run_write_signup_audit(
                    workers.SignupAuditRequest(
                        guild_id=int(guild.id),
                        requester_id=int(member.id),
                        requester_name=_actor_name(member),
                        expected_message_id=int(message.id),
                        expected_channel_id=int(channel.id),
                        action=action,
                        role_name=str(free_agent_role.name),
                    )
                )
            except Exception:
                # The Discord role is the authoritative signup outcome. An
                # audit failure must be visible to staff but must not pretend
                # the completed role effect failed or repeat it.
                logger.exception(
                    'Free Agent %s role effect committed but audit failed for member %s',
                    action,
                    member.id,
                )
                await _relay_log(
                    guild,
                    '⚠️ Free Agent signup reconciliation required: '
                    f'{member.mention} completed `{action}`, but the database '
                    f'audit failed. Announcement: {link}',
                )
            else:
                verb = 'received' if action == 'join' else 'lost'
                await _relay_log(
                    guild,
                    f'{member.mention} ({member.name}) {verb} the '
                    f'{free_agent_role.name} role through {link}',
                )
        await _direct_message(member, member_message)


async def toggle_signup_state(
    *,
    cog,
    member,
    channel,
    message,
    close_emoji: str,
    closed_message: str,
    open_format: str,
    grad_role_name: str,
    novas_role_name: str,
    free_agent_role_name: str,
) -> None:
    """Commit an open/close toggle, then reconcile the public message."""

    await _remove_reaction(message, close_emoji, member)
    if not settings.is_mod(member):
        return
    roles = [
        discord.utils.get(member.guild.roles, name=name)
        for name in (grad_role_name, novas_role_name, free_agent_role_name)
    ]
    if any(role is None for role in roles):
        await _direct_message(
            member,
            'The signup roles are incomplete, so the open/close state was not changed.',
        )
        return

    async with reaction_lifecycle_lock:
        try:
            result = await workers.run_transition_draft_state(
                workers.DraftTransitionRequest(
                    guild_id=int(member.guild.id),
                    requester_id=int(member.id),
                    requester_name=_actor_name(member),
                    expected_message_id=int(message.id),
                    expected_channel_id=int(channel.id),
                    operation='toggle',
                )
            )
        except Exception:
            logger.exception('Free Agent open/close transaction failed')
            await _direct_message(
                member,
                'The signup state could not be changed. No database change was committed.',
            )
            return

        if result.draft_open:
            new_content = open_format.format(
                roles[0].mention,
                roles[1].mention,
                roles[2].mention,
                result.added_message,
            )
            state_word = 'opened'
        else:
            new_content = f'~~{message.content}~~\n{closed_message}'
            state_word = 'closed'
        cog.announcement_message = int(result.announcement_message_id)
        await _relay_log(
            member.guild,
            f'Free Agent signup {state_word} by {member.mention}.',
        )
        try:
            await message.edit(content=new_content)
        except discord.DiscordException:
            logger.exception(
                'Committed Free Agent %s state could not update message %s',
                state_word,
                message.id,
            )
            await _publish_reconciliation(
                channel,
                '⚠️ The Free Agent signup database state was committed as '
                f'**{state_word}**, but the announcement could not be updated. '
                f'Staff must reconcile {message.jump_url}.',
            )


async def conclude_signup(
    *,
    cog,
    member,
    channel,
    message,
    free_agent_count: int,
) -> None:
    """Atomically clear the pointer, then conclude the Discord announcement."""

    async with reaction_lifecycle_lock:
        try:
            await workers.run_transition_draft_state(
                workers.DraftTransitionRequest(
                    guild_id=int(member.guild.id),
                    requester_id=int(member.id),
                    requester_name=_actor_name(member),
                    expected_message_id=int(message.id),
                    expected_channel_id=int(channel.id),
                    operation='conclude',
                )
            )
        except Exception:
            logger.exception('Free Agent conclude transaction failed')
            await _direct_message(
                member,
                'The signup could not be concluded. Its active database state '
                'was preserved.',
            )
            return

        cog.announcement_message = None
        await _relay_log(
            member.guild,
            f'Free Agent signup successfully concluded by {member.mention}.',
        )
        try:
            await message.clear_reactions()
            await message.edit(
                content=(
                    f'{message.content}\nThis signup is concluded. '
                    f'{int(free_agent_count)} members are currently Free Agents.'
                )[:2_000]
            )
        except discord.DiscordException:
            logger.exception(
                'Committed Free Agent conclusion could not update message %s',
                message.id,
            )
            await _publish_reconciliation(
                channel,
                '⚠️ The Free Agent signup was concluded in the database, but '
                f'its announcement needs manual reconciliation: {message.jump_url}',
            )
