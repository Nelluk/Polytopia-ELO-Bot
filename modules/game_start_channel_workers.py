"""Primitive planning and persistence for post-start Discord game channels."""

from __future__ import annotations

from dataclasses import dataclass

import peewee

from modules import game_open_workers, models


PCPLUS_GUILD_ID = 1289762588346814495


class StartedChannelError(RuntimeError):
    """A started-game channel target could not be reconciled safely."""


class StartedChannelConflictError(StartedChannelError):
    """The game/channel state changed after the start transaction."""


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
class StartedChannelTarget:
    kind: str
    side_id: int | None
    side_name: str
    team_name: str
    players: tuple[ChannelPlayer, ...]
    preferred_guild_id: int | None = None
    force_pcplus_guild: bool = False


@dataclass(frozen=True)
class StartedGameChannelPlan:
    game: ChannelGame
    roster_names: str
    side_targets: tuple[StartedChannelTarget, ...]
    central_target: StartedChannelTarget | None


@dataclass(frozen=True)
class PersistStartedChannelRequest:
    game_id: int
    guild_id: int
    channel_id: int
    channel_guild_id: int
    kind: str
    side_id: int | None = None


@dataclass(frozen=True)
class PersistStartedChannelResult:
    game_id: int
    kind: str
    side_id: int | None
    channel_id: int
    channel_guild_id: int
    already_persisted: bool


def _channel_player(lineup) -> ChannelPlayer:
    player = lineup.player
    return ChannelPlayer(
        name=str(player.name),
        discord_member=ChannelDiscordMember(
            discord_id=int(player.discord_member.discord_id),
        ),
    )


def _channel_game(game) -> ChannelGame:
    host = getattr(game, 'host', None)
    return ChannelGame(
        id=int(game.id),
        guild_id=int(game.guild_id),
        name=str(game.name or ''),
        notes=(str(game.notes) if game.notes is not None else None),
        host=(ChannelHost(name=str(host.name)) if host is not None else None),
        league_season=(
            int(game.league_season)
            if getattr(game, 'league_season', None) is not None
            else None
        ),
        league_tier=(
            int(game.league_tier)
            if getattr(game, 'league_tier', None) is not None
            else None
        ),
        league_playoff=bool(getattr(game, 'league_playoff', False)),
    )


def _side_name(side, players: tuple[ChannelPlayer, ...]) -> str:
    name_method = getattr(side, 'name', None)
    if callable(name_method):
        return str(name_method())
    team = getattr(side, 'team', None)
    if team is not None and getattr(team, 'name', None):
        return str(team.name)
    if getattr(side, 'sidename', None):
        return str(side.sidename)
    if len(players) == 1:
        return players[0].name[:30]
    return 'Unknown Team'


def freeze_started_channel_plan(game) -> StartedGameChannelPlan | None:
    """Freeze the final committed graph while the start worker owns it."""

    notes = str(getattr(game, 'notes', '') or '')
    if 'live' in notes.casefold():
        return None

    game_snapshot = _channel_game(game)
    sides = tuple(game.ordered_side_list())
    force_pcplus = (
        'PCPLUS' in notes.upper()
        or 'PCPLUS' in game_snapshot.name.upper()
    )
    side_targets = []
    roster_lines = []
    all_players = []

    for side in sides:
        lineups = tuple(side.ordered_player_list())
        players = tuple(_channel_player(lineup) for lineup in lineups)
        all_players.extend(players)
        side_name = _side_name(side, players)
        roster_lines.append(
            f'Side **{side_name}**: '
            f'{", ".join(player.name for player in players)}'
        )

        external_servers = tuple(
            (
                int(lineup.player.team.external_server)
                if (
                    getattr(lineup.player, 'team', None) is not None
                    and getattr(lineup.player.team, 'external_server', None)
                )
                else None
            )
            for lineup in lineups
        )
        preferred_guild_id = None
        if (
            external_servers
            and external_servers[0] is not None
            and all(value == external_servers[0] for value in external_servers)
        ):
            preferred_guild_id = external_servers[0]

        if len(players) < 2 or getattr(side, 'team_chan', None) is not None:
            continue
        team = getattr(side, 'team', None)
        side_targets.append(
            StartedChannelTarget(
                kind='side',
                side_id=int(side.id),
                side_name=side_name,
                team_name=str(getattr(team, 'name', '') or ''),
                players=players,
                preferred_guild_id=preferred_guild_id,
                force_pcplus_guild=force_pcplus,
            )
        )

    central_target = None
    if (
        getattr(game, 'game_chan', None) is None
        and (
            (len(sides) > 2 and len(all_players) > 5)
            or len(sides) > 3
        )
    ):
        central_target = StartedChannelTarget(
            kind='central',
            side_id=None,
            side_name='',
            team_name='',
            players=tuple(all_players),
        )

    return StartedGameChannelPlan(
        game=game_snapshot,
        roster_names='\n'.join(roster_lines),
        side_targets=tuple(side_targets),
        central_target=central_target,
    )


def _load_started_game(request: PersistStartedChannelRequest):
    try:
        game = models.Game.get_by_id(int(request.game_id))
    except peewee.DoesNotExist as exc:
        raise StartedChannelConflictError(
            f'Game {request.game_id} no longer exists.'
        ) from exc
    if int(game.guild_id) != int(request.guild_id):
        raise StartedChannelConflictError(
            f'Game {request.game_id} belongs to another Discord server.'
        )
    if bool(game.is_pending) or bool(game.is_completed):
        raise StartedChannelConflictError(
            f'Game {request.game_id} is no longer an active started game.'
        )
    return game


def persist_started_channel(
    request: PersistStartedChannelRequest,
) -> PersistStartedChannelResult:
    """Optimistically claim one Discord channel after it is created."""

    if min(
        request.game_id,
        request.guild_id,
        request.channel_id,
        request.channel_guild_id,
    ) <= 0:
        raise StartedChannelError('Channel reconciliation IDs must be valid.')
    if request.kind not in {'side', 'central'}:
        raise StartedChannelError('Channel target kind must be side or central.')
    if request.kind == 'side' and not request.side_id:
        raise StartedChannelError('A side channel requires a game-side ID.')
    if request.kind == 'central' and request.side_id is not None:
        raise StartedChannelError('A central channel cannot have a game-side ID.')

    with models.db.connection_context():
        with models.db.atomic():
            game = _load_started_game(request)
            if request.kind == 'central':
                if int(request.channel_guild_id) != int(request.guild_id):
                    raise StartedChannelConflictError(
                        'A central game channel must remain in the source guild.'
                    )
                existing = getattr(game, 'game_chan', None)
                if existing is not None:
                    if int(existing) == int(request.channel_id):
                        return PersistStartedChannelResult(
                            game_id=request.game_id,
                            kind=request.kind,
                            side_id=None,
                            channel_id=request.channel_id,
                            channel_guild_id=request.channel_guild_id,
                            already_persisted=True,
                        )
                    raise StartedChannelConflictError(
                        f'Game {request.game_id} already has a central channel.'
                    )
                updated = (
                    models.Game
                    .update(game_chan=int(request.channel_id))
                    .where(
                        (models.Game.id == int(request.game_id))
                        & (models.Game.guild_id == int(request.guild_id))
                        & (models.Game.is_pending == False)
                        & (models.Game.is_completed == False)
                        & (models.Game.game_chan.is_null(True))
                    )
                    .execute()
                )
            else:
                try:
                    side = models.GameSide.get_by_id(int(request.side_id))
                except peewee.DoesNotExist as exc:
                    raise StartedChannelConflictError(
                        f'Game side {request.side_id} no longer exists.'
                    ) from exc
                if int(side.game_id) != int(game.id):
                    raise StartedChannelConflictError(
                        f'Game side {request.side_id} belongs to another game.'
                    )
                existing = getattr(side, 'team_chan', None)
                expected_external = (
                    int(request.channel_guild_id)
                    if int(request.channel_guild_id) != int(request.guild_id)
                    else None
                )
                if existing is not None:
                    if (
                        int(existing) == int(request.channel_id)
                        and getattr(side, 'team_chan_external_server', None)
                        == expected_external
                    ):
                        return PersistStartedChannelResult(
                            game_id=request.game_id,
                            kind=request.kind,
                            side_id=request.side_id,
                            channel_id=request.channel_id,
                            channel_guild_id=request.channel_guild_id,
                            already_persisted=True,
                        )
                    raise StartedChannelConflictError(
                        f'Game side {request.side_id} already has a channel.'
                    )
                eligible_game = (
                    models.Game
                    .select(models.Game.id)
                    .where(
                        (models.Game.id == int(request.game_id))
                        & (models.Game.guild_id == int(request.guild_id))
                        & (models.Game.is_pending == False)
                        & (models.Game.is_completed == False)
                    )
                )
                updated = (
                    models.GameSide
                    .update(
                        team_chan=int(request.channel_id),
                        team_chan_external_server=expected_external,
                    )
                    .where(
                        (models.GameSide.id == int(request.side_id))
                        & (models.GameSide.game.in_(eligible_game))
                        & (models.GameSide.team_chan.is_null(True))
                    )
                    .execute()
                )

            if int(updated) != 1:
                raise StartedChannelConflictError(
                    'The started-game channel target changed before it could '
                    'be persisted.'
                )
            return PersistStartedChannelResult(
                game_id=request.game_id,
                kind=request.kind,
                side_id=request.side_id,
                channel_id=request.channel_id,
                channel_guild_id=request.channel_guild_id,
                already_persisted=False,
            )


async def run_persist_started_channel(
    request: PersistStartedChannelRequest,
) -> PersistStartedChannelResult:
    return await game_open_workers.pending_game_coordinator.run_worker(
        persist_started_channel,
        request,
    )
