"""Immutable post-commit presentation helpers for game metadata mutations."""

from __future__ import annotations

from dataclasses import dataclass
import logging

import discord

from modules import channels, game_detail_views, game_join_leave


logger = logging.getLogger('polybot.' + __name__)


def resolve_game_guild(*, game_guild_id: int, guild, bot):
    """Resolve the source guild for a cross-guild game presentation."""

    game_guild_id = int(game_guild_id)
    if int(getattr(guild, 'id', 0) or 0) == game_guild_id:
        return guild
    get_guild = getattr(bot, 'get_guild', None)
    source_guild = get_guild(game_guild_id) if callable(get_guild) else None
    if source_guild is None:
        raise LookupError(f'Game guild {game_guild_id} is unavailable.')
    return source_guild


async def load_card(
    *,
    game_id: int,
    guild,
    bot,
    prefix: str,
    presentation: str,
    requester_id: int,
    channel_id: int,
) -> game_join_leave.PostCommitGameCard:
    """Load one committed game through the established bounded detail reader."""

    return await game_join_leave.load_post_commit_game_card(
        game_id=int(game_id),
        guild=guild,
        bot=bot,
        prefix=str(prefix),
        presentation=str(presentation),
        requester_id=int(requester_id),
        channel_id=int(channel_id or 0),
    )


async def refresh_announcement(
    card: game_join_leave.PostCommitGameCard,
    *,
    guild,
    channel_id: int | None,
    message_id: int | None,
) -> bool | None:
    """Edit a tracked announcement using only the immutable rendered card."""

    if channel_id is None or message_id is None:
        return None
    channel = guild.get_channel(int(channel_id))
    if channel is None:
        raise LookupError(
            f'Announcement channel {int(channel_id)} is unavailable.'
        )
    message = await channel.fetch_message(int(message_id))
    kwargs = game_detail_views.classic_edit_kwargs(message, card.rendered)
    # Announcement updates historically preserved any component view.  The
    # classic helper defaults to explicitly clearing it for interactive card
    # refreshes, so omit that key here.
    kwargs.pop('view', None)
    await message.edit(**kwargs)
    return True


async def send_dense_card(
    destination,
    card: game_join_leave.PostCommitGameCard,
) -> None:
    """Send a fresh attachment-backed copy of one immutable card."""

    await game_join_leave.send_post_commit_game_card(
        destination,
        card,
        content=card.rendered.content,
    )


@dataclass(frozen=True)
class _ChannelGameView:
    """Minimal non-Peewee view accepted by the legacy channel-name helper."""

    id: int
    name: str
    league_season: int | None
    league_tier: int | None
    league_playoff: bool

    def is_season_game(self):
        if self.league_season:
            return (
                self.league_season,
                self.league_tier,
                self.league_playoff,
            )
        return ()


async def rename_game_channels(
    card: game_join_leave.PostCommitGameCard,
    *,
    guild,
    guild_list,
) -> None:
    """Rename tracked game/team channels from frozen snapshot values."""

    snapshot = card.snapshot
    game_view = _ChannelGameView(
        id=int(snapshot.game_id),
        name=str(snapshot.name or ''),
        league_season=snapshot.league_season,
        league_tier=snapshot.league_tier,
        league_playoff=bool(snapshot.league_playoff),
    )
    guilds = tuple(guild_list or (guild,))
    source_guild = discord.utils.get(guilds, id=int(snapshot.guild_id)) or guild

    for side in snapshot.sides:
        if side.channel_id is None:
            continue
        if side.external_guild_id is not None:
            side_guild = discord.utils.get(
                guilds,
                id=int(side.external_guild_id),
            )
            if side_guild is None:
                logger.warning(
                    'Could not resolve external guild %s for game %s side %s',
                    side.external_guild_id,
                    snapshot.game_id,
                    side.side_id,
                )
                continue
        else:
            side_guild = source_guild
        await channels.update_game_channel_name(
            side_guild,
            channel_id=int(side.channel_id),
            game=game_view,
            team_name=str(side.team_name or ''),
        )

    if snapshot.game_channel_id is not None:
        await channels.update_game_channel_name(
            source_guild,
            channel_id=int(snapshot.game_channel_id),
            game=game_view,
            team_name=None,
        )
