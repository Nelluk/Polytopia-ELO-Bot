"""Bounded worker and transaction graph for configured-superuser migration."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import peewee

import settings
from modules import models


class PlayerMigrationError(RuntimeError):
    """Base class for errors safe to show privately to an operator."""


class PlayerMigrationPermissionError(PlayerMigrationError):
    """The requester is not in the configured superuser set."""


class PlayerMigrationValidationError(PlayerMigrationError):
    """The requested identities cannot be migrated safely."""


class PlayerMigrationStaleError(PlayerMigrationError):
    """The graph changed after preview and must be loaded again."""


@dataclass(frozen=True)
class PlayerMigrationPreviewRequest:
    guild_id: int
    requester_id: int
    source_id: int
    destination_id: int
    destination_name: str


@dataclass(frozen=True)
class PlayerMigrationCommitRequest:
    guild_id: int
    requester_id: int
    requester_description: str
    source_id: int
    destination_id: int
    destination_name: str
    expected_fingerprint: str


@dataclass(frozen=True)
class PlayerMigrationGuildPreview:
    guild_id: int
    source_player_id: int | None
    destination_player_id: int | None
    disposition: str
    source_team_id: int | None
    destination_team_id: int | None
    lineups: int
    hosted_games: int
    squad_memberships: int
    house_preferences: int
    bids: int


@dataclass(frozen=True)
class PlayerMigrationPreview:
    guild_id: int
    source_id: int
    source_name: str
    destination_id: int
    destination_name: str
    destination_exists: bool
    destination_completed_games: int
    destination_metadata: tuple[str, ...]
    guilds: tuple[PlayerMigrationGuildPreview, ...]
    blockers: tuple[str, ...]
    fingerprint: str


@dataclass(frozen=True)
class PlayerMigrationResult:
    guild_id: int
    source_id: int
    source_name: str
    destination_id: int
    destination_name: str
    destination_identity_removed: bool
    players_reparented: int
    players_merged: int
    lineups_reassigned: int
    hosts_reassigned: int
    squad_memberships_reassigned: int
    squad_memberships_deduplicated: int
    house_preferences_reassigned: int
    house_preferences_deduplicated: int
    bids_reassigned: int
    player_names_refreshed: int


@dataclass(frozen=True)
class _Graph:
    preview: PlayerMigrationPreview
    source_member_id: int
    destination_member_id: int | None
    source_players: tuple[tuple[int, int], ...]
    destination_players: tuple[tuple[int, int], ...]


def _validate_requester(requester_id: int) -> None:
    if int(requester_id) not in {int(value) for value in settings.superuser_ids}:
        raise PlayerMigrationPermissionError(
            'Only a configured bot superuser can migrate player identities.'
        )


def _member_metadata(member) -> tuple[str, ...]:
    if member is None:
        return ()
    labels = []
    if member.polytopia_name:
        labels.append('canonical Polytopia name')
    if member.timezone_offset_minutes is not None:
        labels.append('canonical timezone')
    elif member.timezone_offset_cleared:
        labels.append('cleared timezone preference')
    if member.name_steam:
        labels.append('legacy Steam name')
    if member.polytopia_id:
        labels.append('legacy Polytopia code')
    if member.timezone_offset is not None:
        labels.append('legacy timezone')
    if member.trophies:
        labels.append('legacy trophies')
    if member.boost_level is not None:
        labels.append('legacy boost level')
    if member.is_banned:
        labels.append('account ban state')
    return tuple(labels)


def _rows(query) -> tuple:
    return tuple(query)


def _primitive_row(row, fields: tuple[str, ...]) -> tuple:
    return tuple(getattr(row, field) for field in fields)


def _fingerprint(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(',', ':'),
        default=str,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _build_graph(request) -> _Graph:
    source = models.DiscordMember.get_or_none(
        models.DiscordMember.discord_id == int(request.source_id)
    )
    if source is None:
        raise PlayerMigrationValidationError(
            f'No stored player identity uses Discord ID {request.source_id}.'
        )
    destination = models.DiscordMember.get_or_none(
        models.DiscordMember.discord_id == int(request.destination_id)
    )

    source_players = _rows(
        models.Player.select()
        .where(models.Player.discord_member == source)
        .order_by(models.Player.guild_id, models.Player.id)
    )
    destination_players = () if destination is None else _rows(
        models.Player.select()
        .where(models.Player.discord_member == destination)
        .order_by(models.Player.guild_id, models.Player.id)
    )
    source_by_guild = {int(row.guild_id): row for row in source_players}
    destination_by_guild = {
        int(row.guild_id): row for row in destination_players
    }
    destination_player_ids = tuple(row.id for row in destination_players)
    source_player_ids = tuple(row.id for row in source_players)
    all_player_ids = source_player_ids + destination_player_ids

    lineups = () if not all_player_ids else _rows(
        models.Lineup.select()
        .where(models.Lineup.player.in_(all_player_ids))
        .order_by(models.Lineup.id)
    )
    hosted_games = () if not all_player_ids else _rows(
        models.Game.select()
        .where(models.Game.host.in_(all_player_ids))
        .order_by(models.Game.id)
    )
    squads = () if not all_player_ids else _rows(
        models.SquadMember.select()
        .where(models.SquadMember.player.in_(all_player_ids))
        .order_by(models.SquadMember.id)
    )
    preferences = () if not all_player_ids else _rows(
        models.PlayerHousePreference.select()
        .where(models.PlayerHousePreference.player.in_(all_player_ids))
        .order_by(models.PlayerHousePreference.id)
    )
    bids = () if not all_player_ids else _rows(
        models.Bid.select()
        .where(
            (models.Bid.player.in_(all_player_ids))
            | (models.Bid.bidder.in_(all_player_ids))
        )
        .order_by(models.Bid.id)
    )
    applications = () if destination is None else _rows(
        models.ApiApplication.select()
        .where(models.ApiApplication.owner == destination)
        .order_by(models.ApiApplication.id)
    )

    destination_lineups = tuple(
        row for row in lineups if row.player_id in destination_player_ids
    )
    destination_game_ids = tuple(sorted({row.game_id for row in destination_lineups}))
    completed_game_ids = () if not destination_game_ids else tuple(
        row.id for row in models.Game.select(models.Game.id)
        .where(
            models.Game.id.in_(destination_game_ids)
            & (models.Game.is_completed == True)
        )
        .order_by(models.Game.id)
    )
    source_game_ids = {row.game_id for row in lineups if row.player_id in source_player_ids}
    shared_game_ids = tuple(sorted(source_game_ids.intersection(destination_game_ids)))

    blockers = []
    if int(request.source_id) == int(request.destination_id):
        blockers.append('Source and destination Discord IDs are identical.')
    if completed_game_ids:
        blockers.append(
            'The destination identity has completed games: '
            + ', '.join(str(value) for value in completed_game_ids[:10])
            + ('.' if len(completed_game_ids) <= 10 else ', …')
        )
    if shared_game_ids:
        blockers.append(
            'Both identities occur in the same game: '
            + ', '.join(str(value) for value in shared_game_ids[:10])
            + ('.' if len(shared_game_ids) <= 10 else ', …')
        )
    if applications:
        blockers.append(
            'The destination owns a legacy API application; reconcile it '
            'manually before migration.'
        )

    guild_previews = []
    for guild_id in sorted(set(source_by_guild) | set(destination_by_guild)):
        source_player = source_by_guild.get(guild_id)
        destination_player = destination_by_guild.get(guild_id)
        if source_player and destination_player:
            disposition = 'merge destination player into source player'
            if (
                source_player.team_id is not None
                and destination_player.team_id is not None
                and source_player.team_id != destination_player.team_id
            ):
                blockers.append(
                    f'Guild {guild_id} has conflicting source/destination '
                    f'teams ({source_player.team_id} vs '
                    f'{destination_player.team_id}).'
                )
        elif destination_player:
            disposition = 'reparent destination player to source identity'
        else:
            disposition = 'retain source player'
        destination_id = destination_player.id if destination_player else None
        destination_ids = () if destination_id is None else (destination_id,)
        guild_previews.append(PlayerMigrationGuildPreview(
            guild_id=guild_id,
            source_player_id=source_player.id if source_player else None,
            destination_player_id=destination_id,
            disposition=disposition,
            source_team_id=source_player.team_id if source_player else None,
            destination_team_id=(
                destination_player.team_id if destination_player else None
            ),
            lineups=sum(row.player_id in destination_ids for row in lineups),
            hosted_games=sum(row.host_id in destination_ids for row in hosted_games),
            squad_memberships=sum(row.player_id in destination_ids for row in squads),
            house_preferences=sum(row.player_id in destination_ids for row in preferences),
            bids=sum(
                row.player_id in destination_ids or row.bidder_id in destination_ids
                for row in bids
            ),
        ))

    member_fields = (
        'id', 'discord_id', 'name', 'name_steam', 'elo', 'elo_max',
        'elo_alltime', 'elo_max_alltime', 'elo_moonrise',
        'elo_max_moonrise', 'polytopia_id', 'polytopia_name', 'is_banned',
        'timezone_offset', 'timezone_offset_minutes',
        'timezone_offset_cleared', 'date_polychamps_invite_sent', 'trophies',
        'boost_level',
    )
    player_fields = (
        'id', 'discord_member_id', 'guild_id', 'nick', 'name', 'team_id',
        'elo', 'elo_max', 'elo_alltime', 'elo_max_alltime', 'elo_moonrise',
        'elo_max_moonrise', 'trophies', 'is_banned',
    )
    state = {
        'source': _primitive_row(source, member_fields),
        'destination': (
            None if destination is None
            else _primitive_row(destination, member_fields)
        ),
        'players': [
            _primitive_row(row, player_fields)
            for row in source_players + destination_players
        ],
        'lineups': [
            _primitive_row(row, ('id', 'game_id', 'gameside_id', 'player_id'))
            for row in lineups
        ],
        'hosts': [(row.id, row.host_id) for row in hosted_games],
        'squads': [
            _primitive_row(row, ('id', 'player_id', 'squad_id'))
            for row in squads
        ],
        'preferences': [
            _primitive_row(row, ('id', 'player_id', 'house_id'))
            for row in preferences
        ],
        'bids': [
            _primitive_row(row, ('id', 'player_id', 'bidder_id'))
            for row in bids
        ],
        'applications': [row.id for row in applications],
        'completed_games': completed_game_ids,
    }
    preview = PlayerMigrationPreview(
        guild_id=int(request.guild_id),
        source_id=int(request.source_id),
        source_name=str(source.name),
        destination_id=int(request.destination_id),
        destination_name=str(request.destination_name),
        destination_exists=destination is not None,
        destination_completed_games=len(completed_game_ids),
        destination_metadata=_member_metadata(destination),
        guilds=tuple(guild_previews),
        blockers=tuple(blockers),
        fingerprint=_fingerprint(state),
    )
    return _Graph(
        preview=preview,
        source_member_id=int(source.id),
        destination_member_id=(None if destination is None else int(destination.id)),
        source_players=tuple((int(row.guild_id), int(row.id)) for row in source_players),
        destination_players=tuple(
            (int(row.guild_id), int(row.id)) for row in destination_players
        ),
    )


def _lock(query):
    if isinstance(models.db, peewee.PostgresqlDatabase):
        query = query.for_update()
    return tuple(query)


def _lock_graph(source_id: int, destination_id: int) -> None:
    identities = _lock(
        models.DiscordMember.select()
        .where(models.DiscordMember.discord_id.in_((source_id, destination_id)))
        .order_by(models.DiscordMember.id)
    )
    identity_ids = tuple(row.id for row in identities)
    if not identity_ids:
        return
    players = _lock(
        models.Player.select()
        .where(models.Player.discord_member.in_(identity_ids))
        .order_by(models.Player.id)
    )
    player_ids = tuple(row.id for row in players)
    if not player_ids:
        return
    lineups = _lock(
        models.Lineup.select()
        .where(models.Lineup.player.in_(player_ids))
        .order_by(models.Lineup.id)
    )
    game_ids = {row.game_id for row in lineups}
    game_ids.update(
        row.id for row in _lock(
            models.Game.select()
            .where(models.Game.host.in_(player_ids))
            .order_by(models.Game.id)
        )
    )
    if game_ids:
        _lock(
            models.Game.select()
            .where(models.Game.id.in_(tuple(sorted(game_ids))))
            .order_by(models.Game.id)
        )
    _lock(models.SquadMember.select().where(models.SquadMember.player.in_(player_ids)).order_by(models.SquadMember.id))
    _lock(models.PlayerHousePreference.select().where(models.PlayerHousePreference.player.in_(player_ids)).order_by(models.PlayerHousePreference.id))
    _lock(models.Bid.select().where((models.Bid.player.in_(player_ids)) | (models.Bid.bidder.in_(player_ids))).order_by(models.Bid.id))


def load_preview(request: PlayerMigrationPreviewRequest) -> PlayerMigrationPreview:
    with models.db.connection_context():
        _validate_requester(request.requester_id)
        return _build_graph(request).preview


def _display_name(discord_name: str, nick: str | None) -> str:
    return models.Player.generate_display_name(
        player_name=discord_name,
        player_nick=nick,
    )


def migrate_player(request: PlayerMigrationCommitRequest) -> PlayerMigrationResult:
    with models.db.connection_context():
        with models.db.atomic():
            _validate_requester(request.requester_id)
            _lock_graph(request.source_id, request.destination_id)
            graph = _build_graph(request)
            if graph.preview.fingerprint != request.expected_fingerprint:
                raise PlayerMigrationStaleError(
                    'The player graph changed after preview. Run the command '
                    'again and review a fresh preview.'
                )
            if graph.preview.blockers:
                raise PlayerMigrationValidationError(
                    'Migration is blocked: ' + ' '.join(graph.preview.blockers)
                )

            source_member = models.DiscordMember.get_by_id(graph.source_member_id)
            destination_member = (
                None if graph.destination_member_id is None
                else models.DiscordMember.get_by_id(graph.destination_member_id)
            )
            source_by_guild = {
                guild_id: models.Player.get_by_id(player_id)
                for guild_id, player_id in graph.source_players
            }
            destination_by_guild = {
                guild_id: models.Player.get_by_id(player_id)
                for guild_id, player_id in graph.destination_players
            }
            counts = {
                'players_reparented': 0,
                'players_merged': 0,
                'lineups_reassigned': 0,
                'hosts_reassigned': 0,
                'squad_memberships_reassigned': 0,
                'squad_memberships_deduplicated': 0,
                'house_preferences_reassigned': 0,
                'house_preferences_deduplicated': 0,
                'bids_reassigned': 0,
                'player_names_refreshed': 0,
            }

            for guild_id in sorted(destination_by_guild):
                destination_player = destination_by_guild[guild_id]
                source_player = source_by_guild.get(guild_id)
                if source_player is None:
                    destination_player.discord_member = source_member
                    destination_player.save(only=[models.Player.discord_member])
                    source_by_guild[guild_id] = destination_player
                    counts['players_reparented'] += 1
                    continue

                if source_player.team_id is None and destination_player.team_id is not None:
                    source_player.team = destination_player.team_id
                if destination_player.nick:
                    source_player.nick = destination_player.nick
                source_player.save(only=[models.Player.team, models.Player.nick])

                counts['lineups_reassigned'] += (
                    models.Lineup.update(player=source_player)
                    .where(models.Lineup.player == destination_player)
                    .execute()
                )
                counts['hosts_reassigned'] += (
                    models.Game.update(host=source_player)
                    .where(models.Game.host == destination_player)
                    .execute()
                )

                existing_squads = {
                    row.squad_id for row in models.SquadMember.select()
                    .where(models.SquadMember.player == source_player)
                }
                for row in tuple(models.SquadMember.select().where(models.SquadMember.player == destination_player).order_by(models.SquadMember.id)):
                    if row.squad_id in existing_squads:
                        row.delete_instance()
                        counts['squad_memberships_deduplicated'] += 1
                    else:
                        row.player = source_player
                        row.save(only=[models.SquadMember.player])
                        existing_squads.add(row.squad_id)
                        counts['squad_memberships_reassigned'] += 1

                existing_houses = {
                    row.house_id for row in models.PlayerHousePreference.select()
                    .where(models.PlayerHousePreference.player == source_player)
                }
                for row in tuple(models.PlayerHousePreference.select().where(models.PlayerHousePreference.player == destination_player).order_by(models.PlayerHousePreference.id)):
                    if row.house_id in existing_houses:
                        row.delete_instance()
                        counts['house_preferences_deduplicated'] += 1
                    else:
                        row.player = source_player
                        row.save(only=[models.PlayerHousePreference.player])
                        existing_houses.add(row.house_id)
                        counts['house_preferences_reassigned'] += 1

                counts['bids_reassigned'] += (
                    models.Bid.update(player=source_player)
                    .where(models.Bid.player == destination_player)
                    .execute()
                )
                counts['bids_reassigned'] += (
                    models.Bid.update(bidder=source_player)
                    .where(models.Bid.bidder == destination_player)
                    .execute()
                )
                destination_player.delete_instance()
                counts['players_merged'] += 1

            destination_removed = destination_member is not None
            if destination_member is not None:
                destination_member.delete_instance()

            source_member.discord_id = int(request.destination_id)
            source_member.name = str(request.destination_name)
            source_member.save(only=[
                models.DiscordMember.discord_id,
                models.DiscordMember.name,
            ])
            for player in source_by_guild.values():
                player.name = _display_name(request.destination_name, player.nick)
                player.save(only=[models.Player.name])
                counts['player_names_refreshed'] += 1

            models.GameLog.write(
                game_id=0,
                guild_id=int(request.guild_id),
                message=(
                    f'{request.requester_description} migrated stored player '
                    f'identity **{graph.preview.source_name}** '
                    f'`{request.source_id}` to **{request.destination_name}** '
                    f'`{request.destination_id}` (/operator player migrate); '
                    f'counts={counts!r}'
                ),
            )
            return PlayerMigrationResult(
                guild_id=int(request.guild_id),
                source_id=int(request.source_id),
                source_name=graph.preview.source_name,
                destination_id=int(request.destination_id),
                destination_name=str(request.destination_name),
                destination_identity_removed=destination_removed,
                **counts,
            )


_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='polybot-player-migration',
)


async def _run(function, request, *, drain_on_cancel: bool):
    loop = asyncio.get_running_loop()
    concurrent_future = _executor.submit(functools.partial(function, request))
    future = asyncio.wrap_future(concurrent_future, loop=loop)
    if not drain_on_cancel:
        return await future
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        task = asyncio.current_task()
        while not future.done():
            if task is not None:
                while task.cancelling():
                    task.uncancel()
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                continue
        future.result()
        raise


async def run_preview(request: PlayerMigrationPreviewRequest) -> PlayerMigrationPreview:
    return await _run(load_preview, request, drain_on_cancel=False)


async def run_commit(request: PlayerMigrationCommitRequest) -> PlayerMigrationResult:
    return await _run(migrate_player, request, drain_on_cancel=True)
