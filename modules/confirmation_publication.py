"""Model-free Discord effects for one committed game confirmation."""

from __future__ import annotations

import logging
import re

import discord

import settings
from modules import channels, game_detail_views, nova_graduation, utilities
from modules.confirmation_publication_workers import (
    ChampionRoleEffect,
    ConfirmationPublicationSnapshot,
    ExperienceRoleEffect,
)
from modules.league import grad_role_name, novas_role_name


logger = logging.getLogger('polybot.' + __name__)


def _role(guild, name: str):
    return discord.utils.get(guild.roles, name=name)


async def _publish_experience_role(effect: ExperienceRoleEffect, bot) -> None:
    for guild_id in effect.guild_ids:
        guild = bot.get_guild(guild_id)
        member = guild.get_member(effect.discord_id) if guild else None
        if guild is None or member is None:
            logger.debug(
                'Skipping experience role for member %s guild %s',
                effect.discord_id,
                guild_id,
            )
            continue
        if effect.earned_role_name is None:
            logger.debug('No relevant achievement role for member %s', effect.discord_id)
            continue
        earned = _role(guild, effect.earned_role_name)
        if earned is None:
            logger.debug(
                'Missing experience role %s in guild %s',
                effect.earned_role_name,
                guild_id,
            )
            continue
        removable = tuple(
            role
            for name in effect.removable_role_names
            if (role := _role(guild, name)) is not None
        )
        member_roles = tuple(getattr(member, 'roles', ()) or ())
        if earned in member_roles and not any(role in member_roles for role in removable):
            continue
        try:
            await member.remove_roles(*removable)
            await member.add_roles(earned)
        except discord.DiscordException:
            logger.exception(
                'Could not update experience role for member %s guild %s',
                effect.discord_id,
                guild_id,
            )


def _member_log_string(member) -> str:
    name = getattr(member, 'display_name', None) or getattr(member, 'name', None)
    return f'**{name or "Unknown member"}** (`{member.id}`)'


async def _publish_champion_roles(effect: ChampionRoleEffect, bot) -> None:
    for guild_effect in effect.guilds:
        guild = bot.get_guild(guild_effect.guild_id)
        if guild is None:
            continue
        role = _role(guild, 'ELO Champion')
        if role is None:
            logger.warning('Could not load ELO Champion role in guild %s', guild.id)
            continue
        local_member = (
            guild.get_member(guild_effect.local_champion_discord_id)
            if guild_effect.local_champion_discord_id is not None
            else None
        )
        global_member = (
            guild.get_member(effect.global_champion_discord_id)
            if effect.global_champion_discord_id is not None
            else None
        )
        champions = []
        champion_ids = set()
        for member in (local_member, global_member):
            if member is not None and int(member.id) not in champion_ids:
                champion_ids.add(int(member.id))
                champions.append(member)
        champions = tuple(champions)
        messages = []
        assigned_ids = {
            int(member.id) for member in tuple(getattr(role, 'members', ()) or ())
        }
        try:
            for old_champion in tuple(getattr(role, 'members', ()) or ()):
                if old_champion in champions:
                    continue
                await old_champion.remove_roles(
                    role,
                    reason='Recurring reset of champion list',
                )
                messages.append(
                    f'{_member_log_string(old_champion)} lost '
                    '**ELO Champion** role.'
                )
                assigned_ids.discard(int(old_champion.id))
            for member, reason in (
                (local_member, 'Local champion'),
                (global_member, 'Global champion'),
            ):
                if member is None or int(member.id) in assigned_ids:
                    continue
                await member.add_roles(role, reason=reason)
                assigned_ids.add(int(member.id))
                messages.append(
                    f'{_member_log_string(member)} given role for '
                    f'{reason.lower()} **ELO Champion**'
                )
        except discord.DiscordException:
            logger.exception('Could not reconcile champion role in guild %s', guild.id)
            continue
        if messages:
            await utilities.send_to_log_channel(guild, '\n'.join(messages))


async def _publish_game_channels(snapshot, *, bot, message: str) -> None:
    for target in snapshot.side_channel_targets:
        guild = bot.get_guild(target.guild_id)
        if guild is None:
            logger.warning(
                'Could not load guild %s for confirmation channel %s',
                target.guild_id,
                target.channel_id,
            )
            continue
        await channels.send_message_to_channel(
            guild,
            channel_id=target.channel_id,
            message=message,
            suppress_errors=True,
        )
    if snapshot.game_channel_id is not None:
        guild = bot.get_guild(snapshot.game.guild_id)
        if guild is not None:
            await channels.send_message_to_channel(
                guild,
                channel_id=snapshot.game_channel_id,
                message=message,
                suppress_errors=True,
            )


async def _send_rendered(destination, rendered) -> None:
    kwargs = {'embed': rendered.embed, 'content': rendered.content}
    attachment = rendered.new_file()
    if attachment is not None:
        kwargs['file'] = attachment
    await destination.send(**kwargs)


async def publish_confirmed_game(
    *,
    guild,
    prefix: str,
    current_channel,
    snapshot: ConfirmationPublicationSnapshot,
    bot=None,
) -> None:
    """Publish a committed confirmation without loading or writing models."""

    bot = bot or settings.bot
    display = game_detail_views.resolve_display(
        snapshot.game,
        guild=guild,
        bot=bot,
        prefix=prefix,
        presentation='prefix',
    )
    rendered = game_detail_views.render_classic_game_detail(display)
    announce_channel_id = settings.guild_setting(guild.id, 'game_announce_channel')

    purge_message = (
        '*This channel will be purged soon.* Purging will be skipped if the '
        'channel or its category has "archive" in the name, or has "Manage '
        'Channel" denied to me.'
    )
    reminder_message = ''
    if snapshot.game.league_season is not None:
        reminder_message = (
            f'\n:bulb: Please use `{prefix}setmap` to log the map and '
            f'`{prefix}settribes` to log the tribes that were selected.'
        )
        purge_message = (
            'This channel will not be purged as it is a Season game.\n'
            f'{reminder_message}'
        )
    elif snapshot.game.guild_id == settings.server_ids.get('polychampions'):
        name_notes = f'{snapshot.game.name} {snapshot.game.notes}'
        size = snapshot.game.size
        if size in ((2, 2), (3, 3)) and re.search(r'[PJ]?S\d', name_notes, re.I):
            reminder_message = (
                '\n:bulb: This game looks like an incorrectly named '
                f'**Season Game**! You might want to use `{prefix}rename` '
                'and include the season tag at the beginning.'
            )

    await _publish_game_channels(
        snapshot,
        bot=bot,
        message=(
            f'The game is over with **{snapshot.winner_name}** victorious. '
            f'{purge_message}'
        ),
    )
    for effect in snapshot.experience_roles:
        await _publish_experience_role(effect, bot)
    if snapshot.champion_roles is not None:
        await _publish_champion_roles(snapshot.champion_roles, bot)
    if snapshot.nova is not None:
        outcome = await nova_graduation.publish_nova_graduation(
            guild=guild,
            result=snapshot.nova,
            output_channel=current_channel,
            nova_role_name=novas_role_name,
            grad_role_name=grad_role_name,
        )
        for warning in outcome.warnings:
            logger.warning('%s', warning)
            try:
                await current_channel.send(warning)
            except Exception:
                logger.exception(
                    'Could not publish Nova warning for confirmed game %s',
                    snapshot.game.game_id,
                )

    roster = ' '.join(snapshot.roster_mentions)
    announcement = guild.get_channel(announce_channel_id) if announce_channel_id else None
    if announcement is not None:
        await announcement.send(
            f'Game concluded! Congrats **{snapshot.winner_name}**. Roster: {roster}'
        )
        await _send_rendered(announcement, rendered)
        await current_channel.send(
            f'Game concluded! See {announcement.mention} for full details.'
        )
        return

    await current_channel.send(
        f'Game concluded! Congrats **{snapshot.winner_name}**. '
        f'Roster: {roster}{reminder_message}'
    )
    await _send_rendered(current_channel, rendered)
