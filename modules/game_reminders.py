"""Discord presentation for immutable ranked game reminder snapshots."""

from __future__ import annotations

import datetime
import logging

import discord

import settings
from modules import game_detail_views, game_reminder_workers


logger = logging.getLogger('polybot.' + __name__)


def reminder_message(*, guild_name: str, guild_id: int, channel_id: int,
                     game_id: int) -> str:
    """Return native-first reminder copy without legacy name/code wording."""

    return (
        f'__You have a ranked game on **{guild_name}** that is waiting to be '
        'created.__\n'
        'Please visit the server bot channel: '
        f'<https://discordapp.com/channels/{guild_id}/{channel_id}/>\n'
        f'Use `/game show` with game ID `{game_id}` for the full card and '
        'draft-order player names. After manually creating the game in '
        'Polytopia, use `/game start` with that game ID and the exact game '
        'name shown in Polytopia.\n\n'
        '*(I do not respond to DMed commands. Use the linked server channel.)*'
    )


def _strict_bot_channel_id(guild_id: int) -> int | None:
    channels = settings.guild_setting(guild_id, 'bot_channels_strict') or ()
    return int(channels[0]) if channels else None


async def send_game_reminders(*, bot, as_of: datetime.datetime | None = None):
    """Load due reminders off-loop and send each Discord DM independently."""

    batch = await game_reminder_workers.run_load_game_reminders(
        game_reminder_workers.GameReminderRequest(
            as_of=as_of or datetime.datetime.now(),
        )
    )
    if batch.truncated:
        logger.warning(
            'Ranked game reminder discovery reached the %s-game bound; later '
            'candidates are deferred to the next cycle.',
            game_reminder_workers.MAX_REMINDER_CANDIDATES,
        )
    for game_id in batch.skipped_game_ids:
        logger.warning(
            'Skipping malformed ranked game reminder snapshot for game %s.',
            game_id,
        )
    for item in batch.items:
        guild = bot.get_guild(item.guild_id)
        if guild is None:
            logger.error(
                'Could not load guild %s for ranked game reminder %s.',
                item.guild_id,
                item.game_id,
            )
            continue
        creator = guild.get_member(item.creator_discord_id)
        if creator is None:
            logger.warning(
                'Could not load creator %s for ranked game reminder %s in '
                'guild %s.',
                item.creator_discord_id,
                item.game_id,
                item.guild_id,
            )
            continue
        try:
            channel_id = _strict_bot_channel_id(item.guild_id)
        except Exception:
            logger.exception(
                'Could not resolve the bot channel for ranked game reminder '
                '%s in guild %s.',
                item.game_id,
                item.guild_id,
            )
            continue
        if channel_id is None:
            logger.warning(
                'No strict bot channel is configured for ranked game reminder '
                '%s in guild %s.',
                item.game_id,
                item.guild_id,
            )
            continue
        try:
            prefix = settings.guild_setting(item.guild_id, 'command_prefix')
            display = game_detail_views.resolve_display(
                item.snapshot,
                guild=guild,
                bot=bot,
                prefix=prefix,
                join_emoji=getattr(settings, 'emoji_join_game', ''),
                presentation='slash',
            )
            rendered = game_detail_views.render_classic_game_detail(display)
            send_kwargs = {
                'embed': rendered.embed,
                'content': reminder_message(
                    guild_name=str(guild.name),
                    guild_id=int(guild.id),
                    channel_id=channel_id,
                    game_id=item.game_id,
                ),
            }
            attachment = rendered.new_file()
            if attachment is not None:
                send_kwargs['file'] = attachment
            await creator.send(**send_kwargs)
            logger.info(
                'Sent ranked game reminder to %s for game %s.',
                item.creator_discord_id,
                item.game_id,
            )
        except discord.DiscordException:
            logger.warning(
                'Discord rejected ranked game reminder %s for creator %s.',
                item.game_id,
                item.creator_discord_id,
                exc_info=True,
            )
        except Exception:
            logger.exception(
                'Could not render or send ranked game reminder %s for '
                'creator %s.',
                item.game_id,
                item.creator_discord_id,
            )
    return batch
