"""Worker-owned database reconciliation for Discord guild-member joins."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

import peewee

from modules import models


PCPLUS_GUILD_ID = 1289762588346814495


class MemberJoinError(RuntimeError):
    """A member-join database operation could not complete coherently."""


class MemberJoinConflictError(MemberJoinError):
    """The game-side channel state changed during Discord reconciliation."""


@dataclass(frozen=True)
class MemberJoinRequest:
    guild_id: int
    member_id: int
    discord_name: str
    discord_nick: str | None


@dataclass(frozen=True)
class ChannelTarget:
    game_id: int
    channel_id: int


@dataclass(frozen=True)
class ChannelDiscordMember:
    discord_id: int


@dataclass(frozen=True)
class ChannelPlayer:
    name: str
    discord_member: ChannelDiscordMember


@dataclass(frozen=True)
class ChannelHost:
    name: str


@dataclass(frozen=True)
class ChannelGame:
    id: int
    guild_id: int
    name: str
    notes: str | None
    host: ChannelHost | None
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


@dataclass(frozen=True)
class MissingSideChannel:
    game: ChannelGame
    gameside_id: int
    side_name: str
    team_name: str
    players: tuple[ChannelPlayer, ...]
    preferred_guild_id: int | None
    force_pcplus_guild: bool

    @property
    def roster_names(self) -> str:
        names = ', '.join(player.name for player in self.players)
        return f'Side **{self.side_name}**: {names}'


@dataclass(frozen=True)
class MemberJoinResult:
    guild_id: int
    member_id: int
    registered: bool
    local_player_created: bool
    player_id: int | None
    side_channels: tuple[ChannelTarget, ...]
    game_channels: tuple[ChannelTarget, ...]
    missing_side_channels: tuple[MissingSideChannel, ...]


@dataclass(frozen=True)
class PersistSideChannelRequest:
    game_id: int
    gameside_id: int
    channel_id: int
    channel_guild_id: int


_member_join_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='member-join-reconciliation',
)


def _validate_join_request(request: MemberJoinRequest) -> None:
    if request.guild_id <= 0 or request.member_id <= 0:
        raise MemberJoinError('Guild and member IDs must be valid.')
    if not str(request.discord_name).strip():
        raise MemberJoinError('The Discord account name is required.')


def _channel_target_rows(*, player_id: int, guild_id: int, side: bool):
    if side:
        query = (
            models.Lineup
            .select(models.Game.id, models.GameSide.team_chan)
            .join(models.GameSide)
            .join(models.Game)
            .where(
                (models.Game.is_completed == False)
                & (models.Lineup.player == int(player_id))
                & (models.GameSide.team_chan > 0)
                & (
                    (models.GameSide.team_chan_external_server == int(guild_id))
                    | (models.Game.guild_id == int(guild_id))
                )
            )
            .order_by(models.Game.id, models.GameSide.id, models.Lineup.id)
        )
    else:
        query = (
            models.Lineup
            .select(models.Game.id, models.Game.game_chan)
            .join(models.Game)
            .where(
                (models.Game.is_completed == False)
                & (models.Lineup.player == int(player_id))
                & (models.Game.game_chan > 0)
                & (models.Game.guild_id == int(guild_id))
            )
            .order_by(models.Game.id, models.Lineup.id)
        )
    seen = set()
    rows = []
    for game_id, channel_id in query.tuples():
        key = (int(game_id), int(channel_id))
        if key not in seen:
            seen.add(key)
            rows.append(ChannelTarget(*key))
    return tuple(rows)


def _side_name(side, players) -> str:
    if len(players) == 1 and int(side.size) == 1:
        return players[0].name[:30]
    if side.team is not None:
        return str(side.team.name)
    if side.sidename:
        return str(side.sidename)
    return 'Unknown Team'


def _missing_side_snapshot(side) -> MissingSideChannel | None:
    lineup_rows = tuple(
        models.Lineup
        .select(models.Lineup, models.Player, models.DiscordMember, models.Team)
        .join(models.Player)
        .join(models.DiscordMember)
        .switch(models.Player)
        .join(models.Team, peewee.JOIN.LEFT_OUTER)
        .where(models.Lineup.gameside == int(side.id))
        .order_by(models.Lineup.id)
    )
    # Existing channel creation intentionally skips one-player sides.
    if len(lineup_rows) < 2:
        return None
    players = tuple(
        ChannelPlayer(
            name=str(lineup.player.name),
            discord_member=ChannelDiscordMember(
                discord_id=int(lineup.player.discord_member.discord_id),
            ),
        )
        for lineup in lineup_rows
    )
    external_servers = tuple(
        int(lineup.player.team.external_server)
        if lineup.player.team is not None
        and lineup.player.team.external_server
        else None
        for lineup in lineup_rows
    )
    preferred_guild_id = (
        external_servers[0]
        if external_servers[0]
        and all(value == external_servers[0] for value in external_servers)
        else None
    )
    game = side.game
    force_pcplus = bool(
        (game.notes and 'PCPLUS' in game.notes.upper())
        or (game.name and 'PCPLUS' in game.name.upper())
    )
    return MissingSideChannel(
        game=ChannelGame(
            id=int(game.id),
            guild_id=int(game.guild_id),
            name=str(game.name or ''),
            notes=str(game.notes) if game.notes is not None else None,
            host=(
                ChannelHost(name=str(game.host.name))
                if game.host is not None else None
            ),
            league_season=(
                int(game.league_season)
                if game.league_season is not None else None
            ),
            league_tier=(
                int(game.league_tier)
                if game.league_tier is not None else None
            ),
            league_playoff=bool(game.league_playoff),
        ),
        gameside_id=int(side.id),
        side_name=_side_name(side, players),
        team_name=str(side.team.name) if side.team is not None else '',
        players=players,
        preferred_guild_id=(
            PCPLUS_GUILD_ID if force_pcplus else preferred_guild_id
        ),
        force_pcplus_guild=force_pcplus,
    )


def _missing_side_channels(*, player_id: int, guild_id: int):
    sides = (
        models.GameSide
        .select(models.GameSide, models.Game, models.Team, models.Player)
        .join(models.Game)
        .switch(models.GameSide)
        .join(models.Team, peewee.JOIN.LEFT_OUTER)
        .switch(models.GameSide)
        .join(models.Lineup)
        .join(models.Player)
        .where(
            (models.Game.is_completed == False)
            & (models.Lineup.player == int(player_id))
            & (models.GameSide.team_chan.is_null(True))
            & (
                (models.GameSide.team_chan_external_server == int(guild_id))
                | (models.Game.guild_id == int(guild_id))
            )
        )
        .order_by(models.Game.id, models.GameSide.id)
    )
    snapshots = []
    seen = set()
    for side in sides:
        if int(side.id) in seen:
            continue
        seen.add(int(side.id))
        snapshot = _missing_side_snapshot(side)
        if snapshot is not None:
            snapshots.append(snapshot)
    return tuple(snapshots)


def load_member_join(request: MemberJoinRequest) -> MemberJoinResult:
    """Upsert the local Player when eligible and freeze channel work."""

    _validate_join_request(request)
    with models.db.connection_context():
        with models.db.atomic():
            player, created = models.Player.get_by_discord_id(
                discord_id=int(request.member_id),
                discord_name=str(request.discord_name),
                discord_nick=request.discord_nick,
                guild_id=int(request.guild_id),
            )
            if player is None:
                return MemberJoinResult(
                    guild_id=int(request.guild_id),
                    member_id=int(request.member_id),
                    registered=False,
                    local_player_created=False,
                    player_id=None,
                    side_channels=(),
                    game_channels=(),
                    missing_side_channels=(),
                )
            player_id = int(player.id)
            side_channels = _channel_target_rows(
                player_id=player_id,
                guild_id=int(request.guild_id),
                side=True,
            )
            game_channels = _channel_target_rows(
                player_id=player_id,
                guild_id=int(request.guild_id),
                side=False,
            )
            missing = _missing_side_channels(
                player_id=player_id,
                guild_id=int(request.guild_id),
            )
    return MemberJoinResult(
        guild_id=int(request.guild_id),
        member_id=int(request.member_id),
        registered=True,
        local_player_created=bool(created),
        player_id=player_id,
        side_channels=side_channels,
        game_channels=game_channels,
        missing_side_channels=missing,
    )


def persist_side_channel(request: PersistSideChannelRequest) -> None:
    """Claim a newly created Discord side channel using optimistic state."""

    if min(
        request.game_id,
        request.gameside_id,
        request.channel_id,
        request.channel_guild_id,
    ) <= 0:
        raise MemberJoinError('Channel reconciliation IDs must be valid.')
    with models.db.connection_context():
        with models.db.atomic():
            try:
                game = models.Game.get_by_id(int(request.game_id))
                side = models.GameSide.get_by_id(int(request.gameside_id))
            except peewee.DoesNotExist as exc:
                raise MemberJoinConflictError(
                    'The game side no longer exists.'
                ) from exc
            if int(side.game_id) != int(game.id) or bool(game.is_completed):
                raise MemberJoinConflictError(
                    'The game side is no longer eligible for recreation.'
                )
            updated = (
                models.GameSide
                .update(
                    team_chan=int(request.channel_id),
                    team_chan_external_server=(
                        int(request.channel_guild_id)
                        if int(request.channel_guild_id) != int(game.guild_id)
                        else None
                    ),
                )
                .where(
                    (models.GameSide.id == int(request.gameside_id))
                    & (models.GameSide.game == int(request.game_id))
                    & (models.GameSide.team_chan.is_null(True))
                )
                .execute()
            )
            if int(updated) != 1:
                raise MemberJoinConflictError(
                    'Another reconciliation already claimed this game side.'
                )


async def _drain_future(future: Future):
    try:
        while not future.done():
            await asyncio.sleep(0.001)
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None:
            while task.cancelling():
                task.uncancel()
        while not future.done():
            if task is not None:
                while task.cancelling():
                    task.uncancel()
            try:
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                continue
        try:
            future.result()
        except BaseException:
            pass
        raise
    return future.result()


async def _run(function, request):
    future = _member_join_executor.submit(function, request)
    return await _drain_future(future)


async def run_member_join(request: MemberJoinRequest) -> MemberJoinResult:
    return await _run(load_member_join, request)


async def run_persist_side_channel(request: PersistSideChannelRequest) -> None:
    # Once Discord has created a channel, cancellation must not strand an
    # unknown database claim. Drain and surface the authoritative outcome so
    # the listener can greet a committed channel or delete an unclaimed one.
    future = _member_join_executor.submit(persist_side_channel, request)
    try:
        while not future.done():
            await asyncio.sleep(0.001)
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None:
            while task.cancelling():
                task.uncancel()
        while not future.done():
            if task is not None:
                while task.cancelling():
                    task.uncancel()
            try:
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                continue
    return future.result()
