"""Post-read Discord presentation for automatic open-game lists."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
import logging

import discord

import settings
from modules import game_list_broadcast_workers


logger = logging.getLogger('polybot.' + __name__)

DEFAULT_DELETE_AFTER = 60 * 60


@dataclass(frozen=True)
class GameListBroadcastCycleResult:
    sent_targets: tuple[tuple[int, int, int], ...]
    skipped_channel_ids: tuple[int, ...]


def _channel_mode(channel_id: int, *, guild_id: int) -> tuple[int, str]:
    ranked_channel = settings.guild_setting(guild_id, 'ranked_game_channel')
    unranked_channel = settings.guild_setting(
        guild_id,
        'unranked_game_channel',
    )
    if channel_id == ranked_channel:
        return 1, 'Current ranked open games'
    if channel_id == unranked_channel:
        return 0, 'Current unranked open games'
    return 2, 'Current open games'


def render_game_list(
    snapshot: game_list_broadcast_workers.GameListBroadcastSnapshot,
    *,
    title: str,
) -> discord.Embed:
    """Render one native-first, cross-play open-game list."""

    embed = discord.Embed(
        title=(
            f'{title}\nUse `/game join` with an ID to join, or '
            '`/game show` with an ID for details.'
        )
    )
    embed.add_field(
        name=f'`{"ID":<8}{"Host":<40} {"Type":<7} {"Capacity":<7} {"Exp":>4} `',
        value='\u200b',
        inline=False,
    )
    for row in snapshot.rows:
        notes = row.notes if row.notes else '\u200b'
        ranked = '*Unranked*' if not row.ranked else ''
        ranked = ranked + ' - ' if row.notes and ranked else ranked
        capacity = f' {row.players}/{row.capacity}'
        embed.add_field(
            name=(
                f'`{row.game_id:<8}{row.host_name:<40} '
                f'{row.size:<7} {capacity:<7} {row.expiration:>5}`'
            ),
            value=f'{ranked}{notes}\n \u200b',
            inline=False,
        )
    return embed


async def broadcast_open_game_lists(
    *,
    bot,
    as_of: datetime.datetime | None = None,
    delete_after: int = DEFAULT_DELETE_AFTER,
) -> GameListBroadcastCycleResult:
    """Read and send every configured guild/channel independently."""

    frozen_time = as_of or datetime.datetime.now()
    sent_targets = []
    skipped_channels = []
    for guild in tuple(bot.guilds):
        try:
            channel_ids = tuple(
                settings.guild_setting(
                    guild.id,
                    'match_challenge_channels',
                ) or ()
            )
        except Exception:
            logger.exception(
                'Could not resolve open-game broadcast channels for guild %s.',
                guild.id,
            )
            continue
        for channel_id_value in channel_ids:
            try:
                channel_id = int(channel_id_value)
            except (TypeError, ValueError):
                logger.warning(
                    'Ignoring invalid open-game broadcast channel value %r '
                    'in guild %s.',
                    channel_id_value,
                    guild.id,
                )
                continue
            channel = guild.get_channel(channel_id)
            if channel is None:
                logger.warning(
                    'Configured open-game broadcast channel %s is missing in '
                    'guild %s.',
                    channel_id,
                    guild.id,
                )
                skipped_channels.append(channel_id)
                continue
            try:
                ranked_filter, title = _channel_mode(
                    channel_id,
                    guild_id=int(guild.id),
                )
                snapshot = await (
                    game_list_broadcast_workers.run_load_game_list_broadcast(
                        game_list_broadcast_workers.GameListBroadcastRequest(
                            guild_id=int(guild.id),
                            ranked_filter=ranked_filter,
                            as_of=frozen_time,
                        ),
                    )
                )
                for game_id in snapshot.skipped_game_ids:
                    logger.warning(
                        'Skipped malformed game %s in open-game broadcast '
                        'channel %s.',
                        game_id,
                        channel_id,
                    )
                if not snapshot.rows:
                    continue
                message = await channel.send(
                    embed=render_game_list(snapshot, title=title),
                    delete_after=delete_after,
                )
                target = (int(guild.id), channel_id, int(message.id))
                bot.purgable_messages = (
                    list(getattr(bot, 'purgable_messages', ())[-20:])
                    + [target]
                )
                sent_targets.append(target)
                logger.info(
                    'Broadcast open-game list to channel %s in message %s.',
                    channel_id,
                    message.id,
                )
            except discord.DiscordException:
                logger.warning(
                    'Discord rejected open-game broadcast channel %s in '
                    'guild %s.',
                    channel_id,
                    guild.id,
                    exc_info=True,
                )
                skipped_channels.append(channel_id)
            except Exception:
                logger.exception(
                    'Open-game broadcast channel %s failed in guild %s; later '
                    'channels will still be processed.',
                    channel_id,
                    guild.id,
                )
                skipped_channels.append(channel_id)
    return GameListBroadcastCycleResult(
        sent_targets=tuple(sent_targets),
        skipped_channel_ids=tuple(skipped_channels),
    )
