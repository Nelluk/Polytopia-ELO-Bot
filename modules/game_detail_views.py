"""Components v2 presentation for immutable game-detail snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import discord

from modules import components_v2, game_detail_workers, image_storage


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


class GameDetailWorkspace(components_v2.RequesterLayoutView):
    """Public immutable game card with requester-only section navigation."""

    unauthorized_message = 'Only the requester can control this game view.'

    def __init__(
        self,
        *,
        requester_id: int,
        display: GameDetailDisplay,
        timeout: float = 300.0,
    ):
        super().__init__(requester_id=requester_id, timeout=timeout)
        self.display = display
        self.section = 'overview'
        self.rebuild()

    @property
    def page_count(self) -> int:
        return 1

    @property
    def expired_message(self) -> str:
        return (
            f'This game view has expired. Run `/game show '
            f'game_id:{self.display.snapshot.game_id}` again for a fresh card.'
        )

    def _lookup_channel(self, channel_id: int) -> str | None:
        for channel in self.display.channels:
            if channel.channel_id == channel_id:
                return channel.mention
        return None

    def _side_heading(self, side: game_detail_workers.GameDetailSide) -> str:
        team = ' '.join(
            value for value in (side.team_emoji, side.team_name or side.name)
            if value
        )
        capacity = f'{len(side.lineups)}/{side.capacity}'
        ratings = []
        if side.team_elo_label:
            ratings.append(f'Team ELO {side.team_elo_label}')
        if side.squad_elo_label:
            ratings.append(f'Squad ELO {side.squad_elo_label}')
        rating_text = f' · {" · ".join(ratings)}' if ratings else ''
        return f'**Side {side.position} — {team}** ({capacity}){rating_text}'

    def _player_line(self, lineup: game_detail_workers.GameDetailLineup) -> str:
        label = self.display.player_label(
            lineup.discord_id,
            lineup.player_name,
        )
        tribe = ' '.join(
            value for value in (lineup.tribe_emoji, lineup.tribe_name)
            if value
        )
        tribe_text = f' · {tribe}' if tribe else ''
        platform_text = ''
        if self.display.snapshot.is_pending and lineup.platform_name:
            platform_text = f' · `{lineup.platform_name}`'
        return f'• {label} · ELO {lineup.elo_label}{tribe_text}{platform_text}'

    def _pending_guidance(self) -> str:
        snapshot = self.display.snapshot
        if not snapshot.is_pending:
            return ''

        lines = []
        if snapshot.pending_join_available:
            join = f'**Join:** Use `{self.display.prefix}join {snapshot.game_id}`'
            if self.display.join_emoji:
                join += f' or react with {self.display.join_emoji}'
            lines.append(join + '.')
        if snapshot.pending_full:
            creator = snapshot.pending_creator_name or 'the creating player'
            lines.extend([
                f'**Next step:** **{creator}** should create the game in '
                f'Polytopia and mark it started with '
                f'`{self.display.prefix}start {snapshot.game_id} Name of Game`.',
                f'**Friend names:** copy them with '
                f'`{self.display.prefix}codes {snapshot.game_id}`.',
            ])
            if snapshot.pending_draft_order:
                lines.append('**Balanced draft order:**')
                lines.extend(
                    f'- Side {pick.side_name}: {pick.player_name}'
                    for pick in snapshot.pending_draft_order
                )
        return '\n'.join(lines)

    def _overview(self) -> str:
        snapshot = self.display.snapshot
        name = snapshot.name or 'Unnamed game'
        size = snapshot.size and 'v'.join(map(str, snapshot.size)) or 'Unknown size'
        ranked = 'Ranked' if snapshot.is_ranked else 'Unranked'
        platform = 'Mobile' if snapshot.is_mobile else 'Desktop'
        sides = ' vs '.join(side.name for side in snapshot.sides) or 'No sides recorded'
        lines = [
            f'# 🎮 Game {snapshot.game_id} — {name}',
            f'**{snapshot.status_label}** · {ranked} · {size} · {platform}',
            f'**Sides:** {sides}',
        ]
        if snapshot.result_label:
            lines.append(f'**{snapshot.result_label}**')
        if snapshot.date:
            lines.append(f'**Created:** {snapshot.date}')
        if snapshot.expiration:
            lines.append(f'**Deadline:** {snapshot.expiration}')
        if self.display.host_label:
            lines.append(f'**Host:** {self.display.host_label}')
        if snapshot.map_type:
            lines.append(f'**Map:** {snapshot.map_type}')
        if snapshot.notes:
            lines.append(f'**Notes:** {_trim(snapshot.notes, 700)}')
        if snapshot.series_record_label:
            lines.append(f'**Series:** {snapshot.series_record_label}')
        pending_guidance = self._pending_guidance()
        if pending_guidance:
            lines.append(pending_guidance)
        if snapshot.cross_guild:
            lines.insert(
                1,
                f'> This game belongs to **{self.display.source_guild_name}**; '
                'details are shown for cross-server compatibility.',
            )
        return '\n'.join(lines)

    def _players(self) -> str:
        if not self.display.snapshot.sides:
            return '*No sides or players are recorded for this game.*'
        blocks = []
        for side in self.display.snapshot.sides:
            lines = [self._side_heading(side)]
            if side.lineups:
                lines.extend(self._player_line(lineup) for lineup in side.lineups)
            else:
                lines.append('• No players recorded yet.')
            if side.required_role_id is not None:
                lines.append(f'• Locked role: {self.display.role_label(side.required_role_id)}')
            if side.channel_id is not None:
                channel = self._lookup_channel(side.channel_id)
                if channel is not None:
                    lines.append(f'• Side channel: {channel}')
            blocks.append('\n'.join(lines))
        return '\n\n'.join(blocks)

    def _status(self) -> str:
        snapshot = self.display.snapshot
        lines = [
            f'**Status:** {snapshot.status_label}',
            f'**Result:** {snapshot.result_label or "No result recorded"}',
            f'**Ranked:** {"Yes" if snapshot.is_ranked else "No"}',
            f'**Created:** {snapshot.date or "Not recorded"}',
            f'**Winner claimed:** {snapshot.win_claimed_ts or "Not recorded"}',
            f'**Completed:** {snapshot.completed_ts or "Not completed"}',
        ]
        if snapshot.expiration:
            lines.append(f'**Deadline:** {snapshot.expiration}')
        if self.display.host_label:
            lines.append(f'**Host:** {self.display.host_label}')
        return '\n'.join(lines)

    def _attributes(self) -> str:
        snapshot = self.display.snapshot
        lines = [
            f'**Map:** {snapshot.map_type or "Not recorded"}',
            f'**Platform:** {"Mobile" if snapshot.is_mobile else "Desktop"}',
            f'**Notes:** {_trim(snapshot.notes, 1800) if snapshot.notes else "None"}',
        ]
        if snapshot.league_season is not None:
            playoff = 'playoff' if snapshot.league_playoff else 'regular season'
            tier = f', tier {snapshot.league_tier}' if snapshot.league_tier is not None else ''
            lines.append(
                f'**Season metadata:** season {snapshot.league_season}{tier} '
                f'({playoff})'
            )
        else:
            lines.append('**Season metadata:** None recorded')
        return '\n'.join(lines)

    def _channels(self) -> str:
        if not self.display.channels:
            return '*No game or side channel links are recorded.*'
        lines = []
        for channel in self.display.channels:
            label = 'Game channel' if channel.central else 'Side channel'
            if channel.external_guild_id is not None:
                label += f' · external guild {channel.external_guild_id}'
            lines.append(f'**{label}:** {channel.mention}')
        return '\n'.join(lines)

    def _body(self) -> str:
        return {
            'overview': self._overview,
            'players': self._players,
            'status': self._status,
            'attributes': self._attributes,
            'channels': self._channels,
        }.get(self.section, self._overview)()

    async def authorize(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                self.unauthorized_message,
                ephemeral=True,
            )
            return False
        if self.is_finished():
            await interaction.response.send_message(
                self.expired_message,
                ephemeral=True,
            )
            return False
        return True

    async def _select_section(self, interaction: discord.Interaction) -> None:
        if not await self.authorize(interaction):
            return
        self.section = self.section_select.values[0]
        self.rebuild()
        kwargs = {'view': self}
        file = self.new_file()
        if file is not None:
            kwargs['attachments'] = [file]
        if interaction.response.is_done():
            await interaction.edit_original_response(**kwargs)
        else:
            await interaction.response.edit_message(**kwargs)

    def new_file(self) -> discord.File | None:
        return self.display.asset.to_file() if self.display.asset else None

    def rebuild(self) -> None:
        self.clear_items()
        components = [
            discord.ui.TextDisplay(_trim(self._body())),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
        ]
        if self.display.asset is not None:
            components.insert(
                1,
                discord.ui.MediaGallery(discord.MediaGalleryItem(
                    self.display.asset.source,
                    description=self.display.asset.description,
                )),
            )
        self.section_select = discord.ui.Select(
            placeholder='Explore game details',
            options=[
                discord.SelectOption(
                    label=label,
                    value=value,
                    default=self.section == value,
                )
                for value, label in (
                    ('overview', 'Overview'),
                    ('players', 'Players & sides'),
                    ('status', 'Status & dates'),
                    ('attributes', 'Map, notes & season'),
                    ('channels', 'Channel links'),
                )
            ],
        )
        self.section_select.callback = self._select_section
        components.extend([
            discord.ui.ActionRow(self.section_select),
            discord.ui.TextDisplay(
                '-# Results are public. Controls expire; rerun `/game show` '
                'for a fresh snapshot.'
            ),
        ])
        self.add_item(discord.ui.Container(
            *components,
            accent_colour=components_v2.DEFAULT_ACCENT,
        ))
