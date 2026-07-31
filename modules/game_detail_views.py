"""Classic presentation for immutable game-detail snapshots."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import discord

from modules import game_detail_workers, image_storage


def _trim(value: str, limit: int = 3800) -> str:
    value = str(value or '')
    if len(value) <= limit:
        return value
    return value[:limit - 20].rstrip() + '\n…(truncated)'


@dataclass(frozen=True)
class GameDetailAsset:
    source: str
    description: str
    local_path: Path | None = None
    filename: str | None = None

    def to_file(self) -> discord.File | None:
        if self.local_path is None or self.filename is None:
            return None
        return discord.File(self.local_path, filename=self.filename)


@dataclass(frozen=True)
class GameDetailChannel:
    channel_id: int
    mention: str
    external_guild_id: int | None
    central: bool


@dataclass(frozen=True)
class GameDetailDisplay:
    """Event-loop-owned Discord display values derived from primitive IDs."""

    snapshot: game_detail_workers.GameDetailSnapshot
    player_labels: tuple[tuple[int, str], ...]
    host_label: str
    channels: tuple[GameDetailChannel, ...]
    role_labels: tuple[tuple[int, str], ...]
    source_guild_name: str
    asset: GameDetailAsset | None
    prefix: str = '$'
    join_emoji: str = ''

    def player_label(self, discord_id: int, fallback: str) -> str:
        return dict(self.player_labels).get(discord_id, fallback)

    def role_label(self, role_id: int | None) -> str:
        if role_id is None:
            return ''
        if self.snapshot.cross_guild:
            return 'source-server role restriction'
        return dict(self.role_labels).get(role_id, f'<@&{role_id}>')


@dataclass(frozen=True)
class ClassicGameDetailRender:
    """Legacy-style embed presentation built only from display DTO values."""

    embed: discord.Embed
    content: str | None
    asset: GameDetailAsset | None

    def new_file(self) -> discord.File | None:
        return self.asset.to_file() if self.asset else None


def _guilds_for_lookup(
    guild,
    bot,
    source_guild_id: int,
    *,
    include_other_guilds: bool = True,
):
    seen = set()
    candidates = []
    get_guild = getattr(bot, 'get_guild', None) if bot is not None else None
    candidate_guilds = [guild]
    if include_other_guilds:
        candidate_guilds.extend([
            get_guild(source_guild_id) if get_guild is not None else None,
            *(getattr(bot, 'guilds', ()) if bot is not None else ()),
        ])
    for candidate in candidate_guilds:
        if candidate is not None and candidate.id not in seen:
            seen.add(candidate.id)
            candidates.append(candidate)
    return candidates


def _member_for_id(discord_id: int, guilds, bot):
    for guild in guilds:
        member = guild.get_member(discord_id)
        if member is not None:
            return member
    get_user = getattr(bot, 'get_user', None) if bot is not None else None
    if get_user is not None:
        return get_user(discord_id)
    return None


def _member_label(member, fallback: str, discord_id: int) -> str:
    if member is None:
        return fallback
    display_name = getattr(member, 'display_name', None) or getattr(member, 'name', None)
    if not display_name:
        return fallback
    return f'{discord.utils.escape_markdown(display_name)} (<@{discord_id}>)'


def _resolve_asset(
    snapshot,
    winner_side,
    guilds,
    *,
    allow_player_avatar: bool = True,
) -> GameDetailAsset | None:
    if winner_side is None:
        return None

    if allow_player_avatar and len(winner_side.lineups) == 1:
        member = _member_for_id(
            winner_side.lineups[0].discord_id,
            guilds,
            None,
        )
        avatar = getattr(member, 'display_avatar', None) if member else None
        if avatar is not None:
            try:
                return GameDetailAsset(
                    source=str(avatar.replace(size=512, format='webp')),
                    description='Winning player avatar',
                )
            except (AttributeError, TypeError, ValueError):
                pass

    if winner_side.team_id is None:
        return None

    try:
        attachment = image_storage.local_attachment(
            'team',
            SimpleNamespace(id=winner_side.team_id),
        )
    except (OSError, ValueError, RuntimeError):
        attachment = None
    if attachment is not None:
        return GameDetailAsset(
            source=attachment.embed_url,
            description=f'{winner_side.team_name or "Winning team"} logo',
            local_path=attachment.path,
            filename=attachment.filename,
        )
    if winner_side.team_image_url:
        return GameDetailAsset(
            source=winner_side.team_image_url,
            description=f'{winner_side.team_name or "Winning team"} logo',
        )
    return None


def resolve_display(
    snapshot,
    *,
    guild=None,
    bot=None,
    prefix: str = '$',
    join_emoji: str = '',
) -> GameDetailDisplay:
    """Resolve Discord-only labels and media without touching Peewee."""

    cross_guild = snapshot.cross_guild
    guilds = _guilds_for_lookup(
        guild,
        bot,
        snapshot.guild_id,
        include_other_guilds=not cross_guild,
    )
    member_lookup_bot = None if cross_guild else bot
    if cross_guild:
        player_labels = [
            (lineup.discord_id, lineup.player_name)
            for side in snapshot.sides
            for lineup in side.lineups
        ]
    else:
        player_labels = []
        for side in snapshot.sides:
            for lineup in side.lineups:
                member = _member_for_id(
                    lineup.discord_id,
                    guilds,
                    member_lookup_bot,
                )
                player_labels.append((
                    lineup.discord_id,
                    _member_label(member, lineup.player_name, lineup.discord_id),
                ))

    host_label = snapshot.host_name
    if snapshot.host_discord_id is not None and not cross_guild:
        host = _member_for_id(
            snapshot.host_discord_id,
            guilds,
            member_lookup_bot,
        )
        host_label = _member_label(
            host,
            snapshot.host_name or f'<@{snapshot.host_discord_id}>',
            snapshot.host_discord_id,
        )

    role_labels = []
    if not cross_guild:
        role_ids = {
            side.required_role_id
            for side in snapshot.sides
            if side.required_role_id is not None
        }
        for role_id in sorted(role_ids):
            role = None
            for candidate_guild in guilds:
                role = candidate_guild.get_role(role_id)
                if role is not None:
                    break
            role_labels.append((
                role_id,
                getattr(role, 'mention', None) or f'<@&{role_id}>',
            ))

    channels = []
    if not cross_guild and snapshot.game_channel_id is not None:
        get_channel = getattr(bot, 'get_channel', None) if bot else None
        channel = get_channel(snapshot.game_channel_id) if get_channel else None
        channels.append(GameDetailChannel(
            channel_id=snapshot.game_channel_id,
            mention=getattr(channel, 'mention', None) or f'<#{snapshot.game_channel_id}>',
            external_guild_id=None,
            central=True,
        ))
    if not cross_guild:
        for side in snapshot.sides:
            if side.channel_id is None:
                continue
            get_channel = getattr(bot, 'get_channel', None) if bot else None
            channel = get_channel(side.channel_id) if get_channel else None
            channels.append(GameDetailChannel(
                channel_id=side.channel_id,
                mention=getattr(channel, 'mention', None) or f'<#{side.channel_id}>',
                external_guild_id=side.external_guild_id,
                central=False,
            ))

    get_guild = getattr(bot, 'get_guild', None) if bot else None
    source_guild = get_guild(snapshot.guild_id) if get_guild else None
    source_guild_name = getattr(source_guild, 'name', '') or f'guild {snapshot.guild_id}'
    winner_side = next(
        (
            side for side in snapshot.sides
            if side.side_id == snapshot.winner_side_id
        ),
        None,
    )
    return GameDetailDisplay(
        snapshot=snapshot,
        player_labels=tuple(player_labels),
        host_label=host_label,
        channels=tuple(channels),
        role_labels=tuple(role_labels),
        source_guild_name=source_guild_name,
        asset=_resolve_asset(
            snapshot,
            winner_side,
            guilds,
            allow_player_avatar=not cross_guild,
        ),
        prefix=prefix or '$',
        join_emoji=join_emoji,
    )


def _classic_size(snapshot) -> str:
    if not snapshot.size:
        return 'Unknown size'
    if max(snapshot.size) == 1 and len(snapshot.size) > 2:
        return 'FFA'
    return 'v'.join(str(value) for value in snapshot.size)


def _classic_headline(snapshot) -> str:
    side_strings = []
    for side in snapshot.sides:
        emoji = side.team_emoji if len(side.lineups) > 1 else ''
        side_name = side.name or 'Unknown Team'
        side_strings.append(
            f'{emoji} **{side_name}**' if emoji else f'**{side_name}**'
        )
    sides = ' *vs* '.join(side_strings)[:225]
    game_name = (
        f'\n\u00a0*{snapshot.name}*'
        if snapshot.name and snapshot.name.strip()
        else ''
    )
    return f'Game {snapshot.game_id}   {sides}{game_name}'


def _classic_season_label(snapshot) -> str:
    if snapshot.league_season is None:
        return ''
    tier = (
        f' Tier {snapshot.league_tier}'
        if snapshot.league_tier is not None
        else ''
    )
    playoff = ' playoff game' if snapshot.league_playoff else ' game'
    return f' - Season {snapshot.league_season}{tier}{playoff}'


def _classic_status_label(snapshot) -> str:
    if not snapshot.is_completed:
        return 'Incomplete'
    if snapshot.is_confirmed:
        return 'Completed'
    return 'Unconfirmed'


def _classic_embed_content(snapshot) -> str | None:
    parts = []
    if snapshot.notes:
        parts.append(f'**Notes:** {_trim(snapshot.notes, 1800)}')
    if snapshot.series_record_label:
        parts.append(snapshot.series_record_label)
    return '\n'.join(parts) or None


def _classic_completed_embed(display: GameDetailDisplay) -> ClassicGameDetailRender:
    snapshot = display.snapshot
    ranked = '' if snapshot.is_ranked else 'Unranked — '
    embed = discord.Embed(
        title=f'{_classic_headline(snapshot)} — {ranked}*{_classic_size(snapshot)}*'[:255]
    )
    if display.asset is not None:
        embed.set_thumbnail(url=display.asset.source)

    if snapshot.is_completed:
        winner = next(
            (
                side.name for side in snapshot.sides
                if side.side_id == snapshot.winner_side_id
            ),
            'Unknown side',
        )
        if len(embed.title) > 240:
            embed.title = embed.title.replace('**', '')
        embed.title = (embed.title + f'\n\nWINNER: {winner}')[:255]

    game_data = []
    for side in snapshot.sides:
        team_elo = side.team_elo_label if not side.team_hidden else '\u200b'
        squad_elo = side.squad_elo_label if len(side.lineups) > 1 else '\u200b'
        game_data.append((side, team_elo, squad_elo))

    use_separator = False
    for side, team_elo, squad_elo in game_data:
        if use_separator:
            embed.add_field(name='\u200b', value='\u200b', inline=False)

        if len(side.lineups) > 1:
            team_name = side.team_name or side.name or 'None'
            team_elo_text = f' ({team_elo})' if team_elo != '\u200b' else ''
            embed.add_field(
                name=f'__Lineup for Team **{team_name}**__{team_elo_text}',
                value=squad_elo or '\u200b',
                inline=False,
            )

        for lineup in side.lineups:
            tribe = f' {lineup.tribe_emoji}' if lineup.tribe_emoji else ''
            if len(side.lineups) > 1:
                field_name = f'**{lineup.player_name}**{tribe}'
            else:
                field_name = f'__**{lineup.player_name}**__{tribe}'
            embed.add_field(
                name=field_name,
                value=f'ELO: {lineup.elo_label}',
                inline=True,
            )
        if len(game_data) < 12:
            use_separator = True

    completed = (
        f' - Completed {snapshot.completed_ts}'
        if snapshot.completed_ts
        else ''
    )
    host = (
        f' - Hosted by {snapshot.host_name[:20]}'
        if snapshot.host_name
        else ''
    )
    map_prefix = f'{snapshot.map_type} - ' if snapshot.map_type else ''
    embed.set_footer(
        text=(
            f'{map_prefix}{_classic_status_label(snapshot)} - '
            f'Created {snapshot.date}{completed}{host}'
            f'{_classic_season_label(snapshot)}'
        )
    )
    return ClassicGameDetailRender(
        embed=embed,
        content=_classic_embed_content(snapshot),
        asset=display.asset,
    )


def _classic_expiration(snapshot) -> str:
    if not snapshot.expiration:
        return '\u200b'
    try:
        expiration = datetime.datetime.fromisoformat(snapshot.expiration)
    except ValueError:
        return snapshot.expiration
    remaining_seconds = (expiration - datetime.datetime.now()).total_seconds()
    if remaining_seconds < 0:
        return '*Expired*'
    return f'{int(remaining_seconds / 3600.0)} hours'


def _classic_pending_content(display: GameDetailDisplay) -> str | None:
    snapshot = display.snapshot
    if snapshot.pending_full:
        creator = snapshot.pending_creator_name or 'the creating player'
        content = (
            f'This match is now full and **{creator}** should create the game '
            'in Polytopia and mark it as started using '
            f'`{display.prefix}start {snapshot.game_id} Name of Game`'
            f'\nFriend codes can be copied easily with '
            f'__`{display.prefix}codes {snapshot.game_id}`__'
        )
        if snapshot.pending_draft_order:
            lines = ['\n__**Balanced Draft Order**__']
            lines.extend(
                f'__Side {pick.side_name}__:  {pick.player_name}'
                for pick in snapshot.pending_draft_order
            )
            content += '\n' + '\n'.join(lines)
        return content
    if snapshot.pending_join_available:
        if display.join_emoji:
            return (
                f'Join game {snapshot.game_id} by reacting with '
                f'{display.join_emoji}'
            )
        return (
            f'Join game {snapshot.game_id} with '
            f'`{display.prefix}join {snapshot.game_id}`'
        )
    return None


def _classic_pending_embed(display: GameDetailDisplay) -> ClassicGameDetailRender:
    snapshot = display.snapshot
    ranked = 'Unranked ' if not snapshot.is_ranked else ''
    platform = '' if snapshot.is_mobile else '🖥'
    title = f'**{ranked}Open Game {snapshot.game_id}** {platform}\n{_classic_size(snapshot)}'
    if snapshot.host_name:
        title += f' *hosted by* {snapshot.host_name}'
    embed = discord.Embed(title=title)

    if snapshot.pending_full:
        status = 'Full - Waiting to start'
    elif snapshot.pending_join_available:
        status = f'Open - `{display.prefix}join {snapshot.game_id}`'
    else:
        status = 'Expired' if snapshot.status_label == 'Expired open game' else snapshot.status_label
    embed.add_field(name='Status', value=status, inline=True)
    embed.add_field(name='Expires in', value=_classic_expiration(snapshot), inline=True)
    embed.add_field(name='Notes', value=snapshot.notes or '\u200b', inline=False)
    embed.add_field(name='\u200b', value='\u200b', inline=False)

    for side in snapshot.sides:
        side_name = f': **{side.sidename}**' if side.sidename else ''
        heading = (
            f'__Side {side.position}__{side_name} '
            f'*({len(side.lineups)}/{side.capacity})*'
        )
        player_lines = []
        for lineup in side.lineups:
            tribe = f' {lineup.tribe_emoji}' if lineup.tribe_emoji else ''
            team = f' {side.team_emoji}' if side.team_emoji else ''
            platform_name = (
                f'\n`{lineup.platform_name}`'
                if lineup.platform_name and len(side.lineups) < 10
                else ''
            )
            player_lines.append(
                f'**{lineup.player_name}** ({lineup.elo_label})'
                f'{tribe}{team}{platform_name}'
            )
        embed.add_field(
            name=heading,
            value='\n'.join(player_lines)[:1024] or '\u200b',
            inline=False,
        )

    return ClassicGameDetailRender(
        embed=embed,
        content=_classic_pending_content(display),
        asset=display.asset,
    )


def render_classic_game_detail(display: GameDetailDisplay) -> ClassicGameDetailRender:
    """Render the shared production-style presentation from one snapshot."""

    if display.snapshot.is_pending:
        return _classic_pending_embed(display)
    return _classic_completed_embed(display)
