"""Bounded workers for the pending-game start transition.

The start command has historically mixed Discord lookups, Peewee reads, and
the lifecycle transaction in one coroutine.  This module keeps the command's
two database phases explicit:

* a worker-local preflight captures the ordered lineup as primitive values;
* the event-loop adapter resolves cached Discord members and their roles; and
* a second worker reloads and revalidates the complete mutable state before
  committing the transition.

Only frozen snapshots cross the worker boundary.  Discord and Peewee objects
created by the worker never leave it, and all mutation plus its audit record
share one synchronous transaction.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass

import discord
import peewee

import settings
from modules import exceptions, game_open_workers, models, utilities


logger = logging.getLogger('polybot.' + __name__)


class GameStartValidationError(RuntimeError):
    """The current database state does not permit starting the game."""


@dataclass(frozen=True)
class StartMemberSnapshot:
    """Primitive Discord values captured outside the database worker."""

    guild_id: int
    discord_id: int
    discord_name: str
    discord_nick: str | None
    display_name: str
    role_ids: tuple[int, ...]
    role_names: tuple[str, ...]
    level: int
    is_mod: bool
    is_staff: bool
    description: str
    side_position: int
    lineup_id: int | None
    player_id: int | None
    player_name: str
    member_present: bool = True

    @property
    def mention(self) -> str:
        return f'<@{self.discord_id}>'


@dataclass(frozen=True)
class StartParticipantIdentity:
    """The ordered lineup identity captured by preflight."""

    side_position: int
    lineup_id: int | None
    player_id: int | None
    discord_id: int
    player_name: str
    discord_name: str


@dataclass(frozen=True)
class StartPreflightRequest:
    """Immutable input for the read-only first stage."""

    game_id: int
    guild_id: int
    name: str | None
    prefix: str
    requester: StartMemberSnapshot
    require_teams: bool
    invoked_with: str


@dataclass(frozen=True)
class StartPreflightResult:
    """Primitive game state that the adapter may safely inspect."""

    game_id: int
    guild_id: int
    participants: tuple[StartParticipantIdentity, ...]
    side_sizes: tuple[int, ...]
    host_id: int | None
    creator_id: int | None
    current_name: str | None
    notes: str | None
    expiration: datetime.datetime | None
    is_ranked: bool
    name_warning: str | None = None


@dataclass(frozen=True)
class StartRequest:
    """Frozen input for the authoritative transition worker."""

    game_id: int
    guild_id: int
    name: str
    prefix: str
    requester: StartMemberSnapshot
    participants: tuple[StartMemberSnapshot, ...]
    preflight: StartPreflightResult
    require_teams: bool
    invoked_with: str


@dataclass(frozen=True)
class StartResult:
    """Primitive post-commit data used by prefix and native adapters."""

    game_id: int
    guild_id: int
    name: str
    requester_id: int
    mentions: tuple[str, ...]
    participant_ids: tuple[int, ...]
    missing_member_warnings: tuple[str, ...]
    name_warning: str | None
    league_warning: str | None
    creator_id: int | None
    host_id: int | None


@dataclass(frozen=True)
class AnnouncementReferenceRequest:
    """Primitive post-commit persistence for a sent announcement card."""

    game_id: int
    guild_id: int
    channel_id: int
    message_id: int


@dataclass(frozen=True)
class _RoleView:
    id: int
    name: str


@dataclass(frozen=True)
class _MemberView:
    """Worker-local duck type accepted by existing team-role helpers."""

    id: int
    name: str
    nick: str | None
    display_name: str
    roles: tuple[_RoleView, ...]


def _member_view(member: StartMemberSnapshot) -> _MemberView:
    return _MemberView(
        id=member.discord_id,
        name=member.discord_name,
        nick=member.discord_nick,
        display_name=member.display_name,
        roles=tuple(
            _RoleView(id=role_id, name=role_name)
            for role_id, role_name in zip(
                member.role_ids,
                member.role_names,
            )
        ),
    )


def _discord_id(player) -> int | None:
    discord_member = getattr(player, 'discord_member', None)
    value = getattr(discord_member, 'discord_id', None)
    return int(value) if value is not None else None


def _host_id(game) -> int | None:
    return _discord_id(getattr(game, 'host', None))


def _creator_id(game) -> int | None:
    creator = game.creating_player()
    return _discord_id(creator)


def _helper_role(guild_id: int) -> str:
    roles = settings.guild_setting(guild_id, 'helper_roles') or ()
    return roles[0] if roles else 'ELO-Helper'


def _load_game(game_id: int):
    try:
        return models.Game.get_by_id(game_id)
    except peewee.DoesNotExist as exc:
        raise GameStartValidationError(
            f'Game with ID {game_id} cannot be found. Use the numeric game '
            'ID only.'
        ) from exc


def _check_authorized(game, request: StartPreflightRequest | StartRequest) -> None:
    requester = request.requester
    if requester.guild_id != request.guild_id:
        raise GameStartValidationError(
            'This request is associated with a different Discord server.'
        )

    registered = models.DiscordMember.get_or_none(
        discord_id=requester.discord_id,
    )
    if registered is None:
        raise GameStartValidationError(
            'This command requires bot registration first. Set your '
            'Polytopia account name with '
            f'__`{request.prefix}setname Your Polytopia Name`__ to get started.'
        )

    hosted_by_requester, host = game.is_hosted_by(requester.discord_id)
    created_by_requester = game.is_created_by(requester.discord_id)
    if hosted_by_requester or created_by_requester or requester.is_staff:
        return

    creating_player = game.creating_player()
    helper_role = _helper_role(request.guild_id)
    creating_name = getattr(creating_player, 'name', None)
    host_name = getattr(host, 'name', None)
    if creating_player and host:
        if host != creating_player:
            raise GameStartValidationError(
                f'Only the game host **{host_name}**, creating player '
                f'**{creating_name}**, or a **@{helper_role}** can do this.'
            )
        raise GameStartValidationError(
            f'Only the game host **{host_name}** or a **@{helper_role}** '
            'can do this.'
        )
    if creating_player:
        raise GameStartValidationError(
            f'Only the creating player **{creating_name}**, or a '
            f'**@{helper_role}** can do this.'
        )
    if host:
        raise GameStartValidationError(
            f'Only the game host **{host_name}** or a **@{helper_role}** '
            'can do this.'
        )
    raise GameStartValidationError(
        f'Only the game host or a **@{helper_role}** can do this.'
    )


def _validate_name(
    *,
    game_id: int,
    name: str | None,
    requester: StartMemberSnapshot,
    prefix: str,
) -> str | None:
    if not name:
        raise GameStartValidationError(
            'Game name is required. The game must be created **in Polytopia** '
            'first to get the correct name.\n'
            f'**Example usage**:\n__`{prefix}start 1025 Name of Game`__'
        )
    if utilities.is_valid_poly_gamename(input=name):
        return None
    if requester.level <= 3:
        raise GameStartValidationError(
            'That name looks made up. :thinking: You need to manually '
            'create the game __in Polytopia__, come back and input the name '
            'of the new game you made.\n'
            f'You can use `{prefix}names {game_id}` to get each player\'s '
            'Polytopia account name in an easy-to-copy format.'
        )
    return (
        '*Warning:* That game name looks made up - you are allowed to '
        'override due to your user level.'
    )


def _identities(game) -> tuple[tuple[StartParticipantIdentity, ...], tuple[int, ...]]:
    identities = []
    side_sizes = []
    for side in game.ordered_side_list():
        side_sizes.append(int(side.size))
        for lineup in side.ordered_player_list():
            player = lineup.player
            discord_member = player.discord_member
            identities.append(
                StartParticipantIdentity(
                    side_position=int(side.position),
                    lineup_id=(int(lineup.id) if getattr(lineup, 'id', None) is not None else None),
                    player_id=(int(player.id) if getattr(player, 'id', None) is not None else None),
                    discord_id=int(discord_member.discord_id),
                    player_name=str(player.name),
                    discord_name=str(discord_member.name),
                )
            )
    return tuple(identities), tuple(side_sizes)


def _identity_key(identity: StartParticipantIdentity):
    return (
        identity.side_position,
        identity.lineup_id,
        identity.player_id,
        identity.discord_id,
    )


def _preflight_state(game, request: StartPreflightRequest) -> StartPreflightResult:
    if game.guild_id != request.guild_id:
        raise GameStartValidationError(
            f'Game with ID {request.game_id} is associated with a different '
            'Discord server.'
        )
    _check_authorized(game, request)
    warning = _validate_name(
        game_id=game.id,
        name=request.name,
        requester=request.requester,
        prefix=request.prefix,
    )
    if not game.is_pending:
        raise GameStartValidationError(
            f'Game {game.id} has already started with name **{game.name}**'
        )
    players, capacity = game.capacity()
    if players != capacity:
        raise GameStartValidationError(
            f'Game {game.id} is not full.\nCapacity {players}/{capacity}.'
        )

    participants, side_sizes = _identities(game)
    return StartPreflightResult(
        game_id=int(game.id),
        guild_id=int(game.guild_id),
        participants=participants,
        side_sizes=side_sizes,
        host_id=_host_id(game),
        creator_id=_creator_id(game),
        current_name=getattr(game, 'name', None),
        notes=getattr(game, 'notes', None),
        expiration=getattr(game, 'expiration', None),
        is_ranked=bool(game.is_ranked),
        name_warning=warning,
    )


def preflight_start_game(request: StartPreflightRequest) -> StartPreflightResult:
    """Read and validate a pending game's current roster in a worker."""

    with models.db.connection_context():
        game = _load_game(request.game_id)
        return _preflight_state(game, request)


def _stale_state_message(game_id: int) -> str:
    return (
        f'Game {game_id} changed while it was being prepared. Its player '
        'lineup or pending-game state is no longer the same; refresh the '
        'game and try the start command again.'
    )


def _revalidate_state(game, request: StartRequest) -> None:
    preflight = request.preflight
    if game.guild_id != request.guild_id:
        raise GameStartValidationError(
            f'Game with ID {request.game_id} is associated with a different '
            'Discord server.'
        )
    _check_authorized(game, request)
    _validate_name(
        game_id=game.id,
        name=request.name,
        requester=request.requester,
        prefix=request.prefix,
    )
    if not game.is_pending:
        raise GameStartValidationError(
            f'Game {game.id} has already started with name **{game.name}**'
        )
    players, capacity = game.capacity()
    if players != capacity:
        raise GameStartValidationError(
            f'Game {game.id} is not full.\nCapacity {players}/{capacity}.'
        )

    current_participants, current_sizes = _identities(game)
    if tuple(_identity_key(item) for item in current_participants) != tuple(
        _identity_key(item) for item in preflight.participants
    ):
        raise GameStartValidationError(_stale_state_message(game.id))
    if current_sizes != preflight.side_sizes:
        raise GameStartValidationError(_stale_state_message(game.id))
    if _host_id(game) != preflight.host_id or _creator_id(game) != preflight.creator_id:
        raise GameStartValidationError(_stale_state_message(game.id))
    if (
        getattr(game, 'name', None) != preflight.current_name
        or getattr(game, 'notes', None) != preflight.notes
        or getattr(game, 'expiration', None) != preflight.expiration
        or bool(game.is_ranked) != preflight.is_ranked
    ):
        raise GameStartValidationError(_stale_state_message(game.id))


def _snapshot_by_identity(request: StartRequest):
    return {
        _identity_key(
            StartParticipantIdentity(
                side_position=member.side_position,
                lineup_id=member.lineup_id,
                player_id=member.player_id,
                discord_id=member.discord_id,
                player_name=member.player_name,
                discord_name=member.discord_name,
            )
        ): member
        for member in request.participants
    }


def _missing_member_warning(identity: StartParticipantIdentity) -> str:
    return (
        f'Player *{identity.player_name}* not found on this server. (Maybe '
        'they left?) Game will still be created.'
    )


def start_game(request: StartRequest) -> StartResult:
    """Commit the complete pending-to-started transition atomically."""

    with models.db.connection_context():
        with models.db.atomic():
            game = _load_game(request.game_id)
            _revalidate_state(game, request)

            current_sides = tuple(game.ordered_side_list())
            snapshots = _snapshot_by_identity(request)
            discord_groups = []
            missing_warnings = []
            ordered_participants = []
            for side in current_sides:
                side_members = []
                for lineup in side.ordered_player_list():
                    player = lineup.player
                    discord_member = player.discord_member
                    identity = StartParticipantIdentity(
                        side_position=int(side.position),
                        lineup_id=(int(lineup.id) if getattr(lineup, 'id', None) is not None else None),
                        player_id=(int(player.id) if getattr(player, 'id', None) is not None else None),
                        discord_id=int(discord_member.discord_id),
                        player_name=str(player.name),
                        discord_name=str(discord_member.name),
                    )
                    member = snapshots[_identity_key(identity)]
                    ordered_participants.append(identity)
                    if member.member_present:
                        side_members.append(_member_view(member))
                    else:
                        side_members.append(None)
                        missing_warnings.append(
                            _missing_member_warning(identity)
                        )
                discord_groups.append(side_members)

            try:
                teams_for_each_member, final_teams = models.Game.pregame_check(
                    discord_groups=discord_groups,
                    guild_id=request.guild_id,
                    require_teams=request.require_teams,
                )
            except (peewee.PeeweeException, exceptions.CheckFailedError):
                raise

            for team_group, allied_team, side in zip(
                teams_for_each_member,
                final_teams,
                current_sides,
            ):
                side_players = []
                current_lineups = tuple(side.ordered_player_list())
                for team, lineup in zip(team_group, current_lineups):
                    lineup.player.team = team
                    lineup.player.save()
                    side_players.append(lineup.player)

                if len(side_players) > 1:
                    squad = models.Squad.upsert(
                        player_list=side_players,
                        guild_id=request.guild_id,
                    )
                    side.squad = squad

                if not side.team:
                    # Preserve preselected teams from open-game setup.
                    side.team = allied_team
                side.save()

            game.name = request.name
            game.date = datetime.datetime.today()
            game.is_pending = False
            game.save()
            game.update_league_fields()

            league_warning = None
            if game.league_season:
                league_warning = (
                    '\n:warning: Detected season game information. Status is:'
                    f'\nGame season: `{game.league_season}`, Team tier: '
                    f'`{game.league_tier}`,  Playoff game? '
                    f'`{game.league_playoff}`'
                )

            invocation_note = (
                f' ({request.invoked_with})'
                if request.invoked_with.startswith('/')
                else ''
            )
            models.GameLog.write(
                game_id=game.id,
                guild_id=request.guild_id,
                message=(
                    f'{request.requester.description} started game with name '
                    f'*{discord.utils.escape_markdown(str(game.name))}*'
                    f'{invocation_note}'
                ),
            )

            return StartResult(
                game_id=int(game.id),
                guild_id=int(request.guild_id),
                name=str(game.name),
                requester_id=int(request.requester.discord_id),
                mentions=tuple(f'<@{item.discord_id}>' for item in ordered_participants),
                participant_ids=tuple(item.discord_id for item in ordered_participants),
                missing_member_warnings=tuple(missing_warnings),
                name_warning=request.preflight.name_warning,
                league_warning=league_warning,
                creator_id=_creator_id(game),
                host_id=_host_id(game),
            )


def persist_announcement_reference(
    request: AnnouncementReferenceRequest,
) -> None:
    """Persist Discord-derived announcement IDs after the public send."""

    with models.db.connection_context():
        with models.db.atomic():
            game = _load_game(request.game_id)
            if game.guild_id != request.guild_id:
                raise GameStartValidationError(
                    f'Game {request.game_id} belongs to another Discord server.'
                )
            game.announcement_channel = request.channel_id
            game.announcement_message = request.message_id
            game.save()


async def run_start_preflight(
    request: StartPreflightRequest,
) -> StartPreflightResult:
    """Serialize preflight with the other pending-game operations."""

    return await game_open_workers.pending_game_coordinator.run_worker(
        preflight_start_game,
        request,
    )


async def run_start(request: StartRequest) -> StartResult:
    """Serialize the authoritative start transition with open/join/leave/kick."""

    return await game_open_workers.pending_game_coordinator.run_worker(
        start_game,
        request,
    )


async def run_announcement_persistence(
    request: AnnouncementReferenceRequest,
) -> None:
    """Run the small post-commit metadata write in a worker-local connection."""

    return await game_open_workers.pending_game_coordinator.run_worker(
        persist_announcement_reference,
        request,
    )
