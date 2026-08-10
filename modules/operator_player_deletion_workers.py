"""Bounded preview and transaction graph for owner-only player deletion."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import peewee

import runtime_config
import settings
from modules import models


class PlayerDeletionError(RuntimeError):
    """Base class for errors safe to show privately to the operator."""


class PlayerDeletionPermissionError(PlayerDeletionError):
    """The requester is not the configured bot owner."""


class PlayerDeletionValidationError(PlayerDeletionError):
    """The requested identity cannot be deleted safely."""


class PlayerDeletionStaleError(PlayerDeletionError):
    """The graph changed after preview and must be loaded again."""


@dataclass(frozen=True)
class PlayerDeletionPreviewRequest:
    guild_id: int
    requester_id: int
    target_id: int


@dataclass(frozen=True)
class PlayerDeletionCommitRequest:
    guild_id: int
    requester_id: int
    requester_description: str
    target_id: int
    expected_fingerprint: str
    confirmation_text: str


@dataclass(frozen=True)
class PlayerDeletionGuildPreview:
    player_id: int
    guild_id: int
    name: str
    nick: str | None
    team_id: int | None
    rating_summary: tuple[str, ...]
    trophies_present: bool
    is_banned: bool
    squad_memberships: int
    house_preferences: int
    lineups: int
    hosted_games: int
    bid_references: int


@dataclass(frozen=True)
class PlayerDeletionPreview:
    guild_id: int
    target_id: int
    target_name: str
    account_metadata: tuple[str, ...]
    global_rating_summary: tuple[str, ...]
    players: tuple[PlayerDeletionGuildPreview, ...]
    player_count: int
    squad_membership_count: int
    house_preference_count: int
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    fingerprint: str


@dataclass(frozen=True)
class PlayerDeletionResult:
    guild_id: int
    target_id: int
    target_name: str
    players_deleted: int
    squad_memberships_deleted: int
    house_preferences_deleted: int


@dataclass(frozen=True)
class _Graph:
    preview: PlayerDeletionPreview
    target_member_id: int
    player_ids: tuple[int, ...]


_RATING_FIELDS = (
    'elo',
    'elo_max',
    'elo_alltime',
    'elo_max_alltime',
    'elo_moonrise',
    'elo_max_moonrise',
)


def _validate_request(requester_id: int, guild_id: int, target_id: int) -> None:
    if int(requester_id) != int(settings.owner_id):
        raise PlayerDeletionPermissionError(
            'Only the configured bot owner can delete stored player identities.'
        )
    if int(guild_id) <= 0:
        raise PlayerDeletionValidationError(
            'This command can only be used in a server.'
        )
    protected_ids = {
        int(settings.owner_id),
        *(int(value) for value in settings.superuser_ids),
        int(settings.bot_id),
        int(settings.bot_id_beta),
        int(runtime_config.LEGACY_PRODUCTION_BOT_ID),
    }
    if int(target_id) in protected_ids:
        raise PlayerDeletionValidationError(
            'That protected owner, superuser, or bot identity cannot be deleted.'
        )


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


def _rating_summary(record) -> tuple[str, ...]:
    return tuple(
        f'{field}={int(getattr(record, field))}'
        for field in _RATING_FIELDS
        if int(getattr(record, field)) != 1000
    )


def _account_metadata(member) -> tuple[str, ...]:
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
    if member.date_polychamps_invite_sent is not None:
        labels.append('PolyChampions invite date')
    if member.trophies:
        labels.append('legacy trophies')
    if member.boost_level is not None:
        labels.append('legacy boost level')
    if member.is_banned:
        labels.append('account ban state')
    return tuple(labels)


def _id_summary(values: tuple[int, ...]) -> str:
    shown = ', '.join(str(value) for value in values[:10])
    return shown + ('' if len(values) <= 10 else ', …')


def _build_graph(request) -> _Graph:
    member = models.DiscordMember.get_or_none(
        models.DiscordMember.discord_id == int(request.target_id)
    )
    if member is None:
        raise PlayerDeletionValidationError(
            f'No stored player identity uses Discord ID {request.target_id}.'
        )

    players = _rows(
        models.Player.select()
        .where(models.Player.discord_member == member)
        .order_by(models.Player.guild_id, models.Player.id)
    )
    player_ids = tuple(int(row.id) for row in players)
    lineups = () if not player_ids else _rows(
        models.Lineup.select()
        .where(models.Lineup.player.in_(player_ids))
        .order_by(models.Lineup.id)
    )
    hosted_games = () if not player_ids else _rows(
        models.Game.select()
        .where(models.Game.host.in_(player_ids))
        .order_by(models.Game.id)
    )
    squads = () if not player_ids else _rows(
        models.SquadMember.select()
        .where(models.SquadMember.player.in_(player_ids))
        .order_by(models.SquadMember.id)
    )
    preferences = () if not player_ids else _rows(
        models.PlayerHousePreference.select()
        .where(models.PlayerHousePreference.player.in_(player_ids))
        .order_by(models.PlayerHousePreference.id)
    )
    bids = () if not player_ids else _rows(
        models.Bid.select()
        .where(
            (models.Bid.player.in_(player_ids))
            | (models.Bid.bidder.in_(player_ids))
        )
        .order_by(models.Bid.id)
    )
    applications = _rows(
        models.ApiApplication.select()
        .where(models.ApiApplication.owner == member)
        .order_by(models.ApiApplication.id)
    )

    lineup_game_ids = tuple(sorted({int(row.game_id) for row in lineups}))
    hosted_game_ids = tuple(int(row.id) for row in hosted_games)
    bid_ids = tuple(int(row.id) for row in bids)
    application_ids = tuple(int(row.id) for row in applications)
    blockers = []
    if lineups:
        blockers.append(
            f'{len(lineups)} Lineup row(s) reference this identity in game(s) '
            f'{_id_summary(lineup_game_ids)}.'
        )
    if hosted_games:
        blockers.append(
            f'{len(hosted_games)} hosted game(s) reference this identity: '
            f'{_id_summary(hosted_game_ids)}.'
        )
    if bids:
        blockers.append(
            f'{len(bids)} legacy bid row(s) reference this identity: '
            f'{_id_summary(bid_ids)}.'
        )
    if applications:
        blockers.append(
            f'{len(applications)} legacy API application(s) are owned by '
            f'this identity: {_id_summary(application_ids)}.'
        )

    account_metadata = _account_metadata(member)
    global_ratings = _rating_summary(member)
    warnings = []
    if account_metadata:
        warnings.append(
            'Account metadata will be discarded: '
            + ', '.join(account_metadata) + '.'
        )
    if global_ratings:
        warnings.append(
            'Non-default global rating history will be discarded: '
            + ', '.join(global_ratings) + '.'
        )

    guild_previews = []
    for player in players:
        player_lineups = sum(row.player_id == player.id for row in lineups)
        player_hosts = sum(row.host_id == player.id for row in hosted_games)
        player_squads = sum(row.player_id == player.id for row in squads)
        player_preferences = sum(
            row.player_id == player.id for row in preferences
        )
        player_bids = sum(
            row.player_id == player.id or row.bidder_id == player.id
            for row in bids
        )
        ratings = _rating_summary(player)
        details = []
        if player.team_id is not None:
            details.append(f'Team {player.team_id}')
        if player.nick:
            details.append('nickname')
        if ratings:
            details.append('non-default rating history')
        if player.trophies:
            details.append('legacy trophies')
        if player.is_banned:
            details.append('guild ban state')
        if details:
            warnings.append(
                f'Guild {int(player.guild_id)} Player {int(player.id)} will '
                f'lose {", ".join(details)}.'
            )
        guild_previews.append(PlayerDeletionGuildPreview(
            player_id=int(player.id),
            guild_id=int(player.guild_id),
            name=str(player.name or ''),
            nick=(None if player.nick is None else str(player.nick)),
            team_id=(None if player.team_id is None else int(player.team_id)),
            rating_summary=ratings,
            trophies_present=bool(player.trophies),
            is_banned=bool(player.is_banned),
            squad_memberships=player_squads,
            house_preferences=player_preferences,
            lineups=player_lineups,
            hosted_games=player_hosts,
            bid_references=player_bids,
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
        'member': _primitive_row(member, member_fields),
        'players': [_primitive_row(row, player_fields) for row in players],
        'lineups': [
            _primitive_row(row, ('id', 'game_id', 'gameside_id', 'player_id'))
            for row in lineups
        ],
        'hosts': [(int(row.id), int(row.host_id)) for row in hosted_games],
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
        'applications': application_ids,
    }
    preview = PlayerDeletionPreview(
        guild_id=int(request.guild_id),
        target_id=int(request.target_id),
        target_name=str(member.name),
        account_metadata=account_metadata,
        global_rating_summary=global_ratings,
        players=tuple(guild_previews),
        player_count=len(players),
        squad_membership_count=len(squads),
        house_preference_count=len(preferences),
        blockers=tuple(blockers),
        warnings=tuple(warnings),
        fingerprint=_fingerprint(state),
    )
    return _Graph(
        preview=preview,
        target_member_id=int(member.id),
        player_ids=player_ids,
    )


def _lock(query):
    if isinstance(models.db, peewee.PostgresqlDatabase):
        query = query.for_update()
    return tuple(query)


def _lock_graph(target_id: int) -> None:
    members = _lock(
        models.DiscordMember.select()
        .where(models.DiscordMember.discord_id == int(target_id))
        .order_by(models.DiscordMember.id)
    )
    if not members:
        return
    member_ids = tuple(int(row.id) for row in members)
    players = _lock(
        models.Player.select()
        .where(models.Player.discord_member.in_(member_ids))
        .order_by(models.Player.id)
    )
    player_ids = tuple(int(row.id) for row in players)
    if player_ids:
        _lock(
            models.Lineup.select()
            .where(models.Lineup.player.in_(player_ids))
            .order_by(models.Lineup.id)
        )
        _lock(
            models.Game.select()
            .where(models.Game.host.in_(player_ids))
            .order_by(models.Game.id)
        )
        _lock(
            models.SquadMember.select()
            .where(models.SquadMember.player.in_(player_ids))
            .order_by(models.SquadMember.id)
        )
        _lock(
            models.PlayerHousePreference.select()
            .where(models.PlayerHousePreference.player.in_(player_ids))
            .order_by(models.PlayerHousePreference.id)
        )
        _lock(
            models.Bid.select()
            .where(
                (models.Bid.player.in_(player_ids))
                | (models.Bid.bidder.in_(player_ids))
            )
            .order_by(models.Bid.id)
        )
    _lock(
        models.ApiApplication.select()
        .where(models.ApiApplication.owner.in_(member_ids))
        .order_by(models.ApiApplication.id)
    )


def load_preview(request: PlayerDeletionPreviewRequest) -> PlayerDeletionPreview:
    with models.db.connection_context():
        _validate_request(request.requester_id, request.guild_id, request.target_id)
        return _build_graph(request).preview


def delete_player(request: PlayerDeletionCommitRequest) -> PlayerDeletionResult:
    with models.db.connection_context():
        with models.db.atomic():
            _validate_request(
                request.requester_id,
                request.guild_id,
                request.target_id,
            )
            expected_text = f'DELETE {int(request.target_id)}'
            if request.confirmation_text != expected_text:
                raise PlayerDeletionValidationError(
                    f'Type exactly `{expected_text}` to confirm deletion.'
                )
            _lock_graph(request.target_id)
            graph = _build_graph(request)
            if graph.preview.fingerprint != request.expected_fingerprint:
                raise PlayerDeletionStaleError(
                    'The player graph changed after preview. Run the command '
                    'again and review a fresh preview.'
                )
            if graph.preview.blockers:
                raise PlayerDeletionValidationError(
                    'Deletion is blocked: ' + ' '.join(graph.preview.blockers)
                )

            if graph.player_ids:
                squads_deleted = (
                    models.SquadMember.delete()
                    .where(models.SquadMember.player.in_(graph.player_ids))
                    .execute()
                )
                preferences_deleted = (
                    models.PlayerHousePreference.delete()
                    .where(
                        models.PlayerHousePreference.player.in_(
                            graph.player_ids
                        )
                    )
                    .execute()
                )
                players_deleted = (
                    models.Player.delete()
                    .where(models.Player.id.in_(graph.player_ids))
                    .execute()
                )
            else:
                squads_deleted = 0
                preferences_deleted = 0
                players_deleted = 0

            expected_counts = (
                graph.preview.squad_membership_count,
                graph.preview.house_preference_count,
                graph.preview.player_count,
            )
            actual_counts = (
                int(squads_deleted),
                int(preferences_deleted),
                int(players_deleted),
            )
            if actual_counts != expected_counts:
                raise PlayerDeletionStaleError(
                    'The deletion graph changed while committing. The '
                    'transaction rolled back; run a fresh preview.'
                )
            member_deleted = (
                models.DiscordMember.delete()
                .where(models.DiscordMember.id == graph.target_member_id)
                .execute()
            )
            if int(member_deleted) != 1:
                raise PlayerDeletionStaleError(
                    'The stored identity changed while committing. The '
                    'transaction rolled back; run a fresh preview.'
                )

            models.GameLog.write(
                game_id=0,
                guild_id=int(request.guild_id),
                message=(
                    f'{request.requester_description} deleted orphan stored '
                    f'player identity **{graph.preview.target_name}** '
                    f'`{request.target_id}` (/operator player delete); '
                    f'players={players_deleted}, '
                    f'squad_memberships={squads_deleted}, '
                    f'house_preferences={preferences_deleted}'
                ),
            )
            return PlayerDeletionResult(
                guild_id=int(request.guild_id),
                target_id=int(request.target_id),
                target_name=graph.preview.target_name,
                players_deleted=int(players_deleted),
                squad_memberships_deleted=int(squads_deleted),
                house_preferences_deleted=int(preferences_deleted),
            )


_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='polybot-player-deletion',
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


async def run_preview(
    request: PlayerDeletionPreviewRequest,
) -> PlayerDeletionPreview:
    return await _run(load_preview, request, drain_on_cancel=True)


async def run_commit(
    request: PlayerDeletionCommitRequest,
) -> PlayerDeletionResult:
    return await _run(delete_player, request, drain_on_cancel=True)
