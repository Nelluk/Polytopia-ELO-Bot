"""Bounded workers for atomic pending-game joins and leaves.

The public command and reaction adapters capture Discord state into the frozen
snapshots in this module.  The synchronous workers reload every mutable game
record and keep the lineup, player refresh, and audit log in one transaction.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import peewee

import settings
from modules import game_open_workers, models


logger = logging.getLogger('polybot.' + __name__)


class PendingGameJoinValidationError(RuntimeError):
    """The current database state does not permit a join."""


class PendingGameLeaveValidationError(RuntimeError):
    """The current database state does not permit a leave."""


@dataclass(frozen=True)
class MemberSnapshot:
    """Discord-member values safe to cross into a worker thread."""

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
    inactive_role_name: str | None = None
    inactive_role_present: bool = False

    @property
    def mention(self) -> str:
        return f'<@{self.discord_id}>'


@dataclass(frozen=True)
class JoinRequest:
    """Immutable input for one pending-game join attempt."""

    game_id: int
    guild_id: int
    prefix: str
    member: MemberSnapshot
    author: MemberSnapshot
    side_arg: str | None = None
    log_note: str = ''
    invoked_with: str = 'join'
    notification_member_id: int | None = None


@dataclass(frozen=True)
class JoinResult:
    """Primitive data needed for post-commit Discord effects."""

    game_id: int
    guild_id: int
    member_id: int
    side_position: int
    messages: tuple[str, ...]
    players: int
    capacity: int
    creator_id: int | None
    host_id: int | None
    remove_inactive_role: bool
    inactive_role_name: str | None
    waitlist_ids: tuple[str, ...] = ()

    @property
    def is_full(self) -> bool:
        return self.players >= self.capacity


@dataclass(frozen=True)
class LeaveRequest:
    """Immutable input for one pending-game leave attempt."""

    game_id: int
    guild_id: int
    prefix: str
    member: MemberSnapshot
    author: MemberSnapshot
    log_note: str = ''
    invoked_with: str = 'leave'


@dataclass(frozen=True)
class LeaveResult:
    """Primitive data needed for post-commit leave output."""

    game_id: int
    guild_id: int
    member_id: int
    host_warning: str | None
    message: str


@dataclass(frozen=True)
class _RoleView:
    id: int
    name: str


@dataclass(frozen=True)
class _MemberView:
    """Worker-local duck type for existing team/role model helpers."""

    id: int
    name: str
    nick: str | None
    display_name: str
    roles: tuple[_RoleView, ...]


def _member_view(member: MemberSnapshot) -> _MemberView:
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


def _account_name(discord_member) -> str | None:
    return (
        getattr(discord_member, 'polytopia_name', None)
        or getattr(discord_member, 'name_steam', None)
    )


def _waitlist_ids(guild_id: int, discord_id: int) -> tuple[str, ...]:
    """Reload the current full-game backlog for one player."""

    waitlist_hosting = [
        str(game.id)
        for game in models.Game.search_pending(
            status_filter=1,
            guild_id=guild_id,
            host_discord_id=discord_id,
        )
    ]
    waitlist_creating = [
        str(row.game)
        for row in models.Game.waiting_for_creator(
            creator_discord_id=discord_id,
        )
    ]
    # The legacy output used a set.  Sorting keeps the same IDs while making
    # concurrent-test and operator output deterministic.
    return tuple(sorted(set(waitlist_hosting + waitlist_creating), key=int))


def _friend_guidance(
    *,
    game,
    player,
    member: MemberSnapshot,
    prefix: str,
    players_before: int,
    capacity: int,
    creating_player,
    same_actor: bool,
    messages: list[str],
) -> None:
    """Append the legacy host/friend guidance after a successful join."""

    if (
        players_before + 1 < capacity
        and creating_player == player
        and same_actor
        and member.level <= 1
    ):
        messages.append(
            ':bulb: Since you are joining **side 1**, you will be the host '
            'of this game and will be notified when it is full. It will be '
            'your responsibility to create the game in Polytopia. '
            f'You can specify a non-host side to join; see `{prefix}help '
            'join` in a bot channel.'
        )
    elif creating_player and creating_player != player and member.level <= 3:
        host_account_name = _account_name(creating_player.discord_member)
        if host_account_name:
            messages.append(
                ':bulb: To help get the game set up more quickly, send the '
                'game host a friend request within Polytopia. The in-game '
                f'name of the host is `{host_account_name}`.'
            )
        else:
            messages.append(
                ':bulb: The game host must register a canonical Polytopia '
                f'account name with `{prefix}setname` before sending a friend '
                'request.'
            )


def join_game(request: JoinRequest) -> JoinResult:
    """Join a pending game in one worker-local synchronous transaction."""

    member = request.member
    author = request.author
    if author.discord_id != member.discord_id and author.level < 4:
        raise PendingGameJoinValidationError(
            'You do not have permissions to add another person to a game. '
            'Tell them to use the join command themselves.'
        )
    member_view = _member_view(member)
    players_before = 0
    capacity = 0
    messages: list[str] = []
    remove_inactive_role = False

    with models.db.connection_context():
        with models.db.atomic():
            try:
                game = models.Game.get_by_id(request.game_id)
            except peewee.DoesNotExist as exc:
                raise PendingGameJoinValidationError(
                    f'Game with ID {request.game_id} cannot be found.'
                ) from exc

            if game.guild_id != request.guild_id:
                raise PendingGameJoinValidationError(
                    f'Game with ID {request.game_id} is associated with a '
                    'different Discord server.'
                )

            players_before, capacity = game.capacity()
            if not game.is_pending:
                raise PendingGameJoinValidationError(
                    'The game has already started and can no longer be joined.'
                )

            player, _ = models.Player.get_by_discord_id(
                discord_id=member.discord_id,
                discord_name=member.discord_name,
                discord_nick=member.discord_nick,
                guild_id=request.guild_id,
            )
            if not player:
                raise PendingGameJoinValidationError(
                    f'*{member.discord_name}* was found in the server but is '
                    'not registered with me. Players can register a canonical '
                    'Polytopia account name with '
                    f'`{request.prefix}setname`.'
                )

            if game.has_player(player)[0]:
                leave_kick = (
                    f'`{request.prefix}leave {game.id}`'
                    if author.discord_id == member.discord_id
                    else f'`{request.prefix}kick {game.id} '
                    f'{member.discord_name}`'
                )
                raise PendingGameJoinValidationError(
                    f'**{player.name}** is already in game {game.id}. If you '
                    f'are trying to change sides, use {leave_kick} first.'
                )

            if player.is_banned or player.discord_member.is_banned:
                if author.is_mod:
                    messages.append(
                        f'**{player.name}** has been **ELO Banned** -- '
                        '*moderator over-ride* :thinking:'
                    )
                else:
                    raise PendingGameJoinValidationError(
                        f'**{player.name}** has been **ELO Banned** and '
                        'cannot join any new games. :cry:'
                    )

            if not _account_name(player.discord_member):
                raise PendingGameJoinValidationError(
                    f'**{player.name}** does not have a canonical Polytopia '
                    'account name on file. Use '
                    f'`{request.prefix}setname` to set one.'
                )

            if member.inactive_role_present:
                if author.discord_id == member.discord_id:
                    remove_inactive_role = True
                    role_name = member.inactive_role_name or 'inactive'
                    messages.append(
                        f'You have the inactive role **{role_name}**. '
                        'Removing it since you seem to be active! '
                        ':smiling_face_with_3_hearts:'
                    )
                else:
                    role_name = member.inactive_role_name or 'inactive'
                    raise PendingGameJoinValidationError(
                        f'**{player.name}** has the inactive role *{role_name}* '
                        ' - cannot join them to a game until the role is '
                        f'removed. The role will be removed if they use the '
                        f'`{request.prefix}join` command themselves.'
                    )

            waitlist = _waitlist_ids(request.guild_id, member.discord_id)
            if len(waitlist) > 2 and member.level < 3:
                raise PendingGameJoinValidationError(
                    f'You are the host of {len(waitlist)} games that are '
                    'waiting to start. You cannot join new games until that '
                    f'is complete. Game IDs: **{", ".join(waitlist)}**\n'
                    f'Type __`{request.prefix}game IDNUM`__ for more details, '
                    f'ie `{request.prefix}game {waitlist[0]}`\n'
                    'You must create each game in Polytopia and invite the '
                    f'other players using their friend codes, and then use '
                    f'the `{request.prefix}start` command in this bot.'
                )

            on_team, player_team = models.Player.is_in_team(
                guild_id=request.guild_id,
                discord_member=member_view,
            )
            if settings.guild_setting(request.guild_id, 'require_teams') and not on_team:
                raise PendingGameJoinValidationError(
                    f'**{member.discord_name}** must join a Team in order to '
                    'participate in games on this server.'
                )

            if request.side_arg is not None and str(request.side_arg) != '':
                side, side_open = game.get_side(str(request.side_arg))
                if not side:
                    raise PendingGameJoinValidationError(
                        f'Could not find side matching {request.side_arg} in '
                        f'game {self_id(game)}. You can use a side number or '
                        'name if available.'
                    )
                if not side_open:
                    raise PendingGameJoinValidationError(
                        f'That side of game {self_id(game)} is already full. '
                        f'See `{request.prefix}game {self_id(game)}` for details.'
                    )
            else:
                side, has_role_locked_side = game.first_open_side(
                    roles=list(member.role_ids),
                )
                if not side:
                    if players_before < capacity:
                        if has_role_locked_side:
                            raise PendingGameJoinValidationError(
                                f'Game {self_id(game)} is limited to specific '
                                'roles, and your eligible side is **full**. '
                                f'See details with `{request.prefix}game '
                                f'{self_id(game)}`'
                            )
                        if author.level >= 5:
                            raise PendingGameJoinValidationError(
                                f'Game {self_id(game)} is limited to specific '
                                'roles. You can override this restriction by '
                                'specifying the side to join.'
                            )
                        raise PendingGameJoinValidationError(
                            f'Game {self_id(game)} is limited to specific '
                            'roles. You are not allowed to join. See details '
                            f'with `{request.prefix}game {self_id(game)}`'
                        )
                    raise PendingGameJoinValidationError(
                        f'Game {self_id(game)} is completely full!'
                    )

            if side.required_role_id and side.required_role_id not in member.role_ids:
                if author.level >= 5:
                    messages.append(
                        f'Side {side.position} of game {self_id(game)} is '
                        f'limited to players with the **@{side.sidename}** '
                        'role. *Overriding restriction due to staff '
                        'privileges.*'
                    )
                else:
                    raise PendingGameJoinValidationError(
                        f'Side {side.position} of game {self_id(game)} is '
                        f'limited to players with the **@{side.sidename}** '
                        'role. You are not allowed to join.'
                    )

            is_member_host = game.is_hosted_by(member.discord_id)[0]
            if is_member_host and side.position != 1:
                messages.append(
                    ':bulb: Since you are not joining side 1 you will not be '
                    'the game creator.'
                )

            game_allowed, join_error_message = settings.can_user_join_game(
                user_level=author.level,
                game_size=capacity,
                is_ranked=game.is_ranked,
                is_host=False,
            )
            if not game_allowed:
                raise PendingGameJoinValidationError(join_error_message)

            min_elo, max_elo, min_elo_g, max_elo_g = game.elo_requirements()
            is_author_host = game.is_hosted_by(author.discord_id)[0]
            if player.elo_moonrise < min_elo or player.elo_moonrise > max_elo:
                if not is_author_host and not author.is_mod:
                    raise PendingGameJoinValidationError(
                        f'This game has an ELO restriction of {min_elo} - '
                        f'{max_elo} and **{player.name}** has an ELO of '
                        f'**{player.elo_moonrise}**. Cannot join! :cry: Use '
                        f'`{request.prefix}games` to list games you *can* join.'
                    )
                messages.append(
                    f'This game has an ELO restriction of {min_elo} - '
                    f'{max_elo}. Bypassing because you are game host or a mod.'
                )

            if (
                player.discord_member.elo_moonrise < min_elo_g
                or player.discord_member.elo_moonrise > max_elo_g
            ):
                if not is_author_host and not author.is_mod:
                    raise PendingGameJoinValidationError(
                        f'This game has a global ELO restriction of {min_elo_g} '
                        f'- {max_elo_g} and **{player.name}** has an ELO of '
                        f'**{player.discord_member.elo_moonrise}**. Cannot '
                        'join! :cry:'
                    )
                messages.append(
                    f'This game has a global ELO restriction of {min_elo_g} '
                    f'- {max_elo_g}. Bypassing because you are game host or a '
                    'mod.'
                )

            player_restricted_list = re.findall(
                r'<@!?(\d+)>',
                game.notes or '',
            )
            if (
                player_restricted_list
                and str(member.discord_id) not in player_restricted_list
                and len(player_restricted_list) >= capacity - 1
            ):
                raise PendingGameJoinValidationError(
                    f'Game {self_id(game)} is limited to specific players. '
                    'You are not allowed to join. See game notes for details: '
                    f'`{request.prefix}game {self_id(game)}`'
                )

            logger.info(
                'Checks passed. Joining player %s to side %s of game %s',
                member.discord_id,
                side.position,
                self_id(game),
            )

            lineup = models.Lineup.create(
                player=player,
                game=game,
                gameside=side,
            )
            # Keep the detected team refresh in the same transaction as the
            # lineup and audit record.  A refresh failure rolls back all three.
            player.team = player_team
            player.save()

            messages.append(
                f'Joining {member.mention} to side {side.position} of game '
                f'{self_id(game)}'
            )
            log_by_str = (
                f'(Command issued by {author.description})'
                if author.discord_id != member.discord_id
                else ''
            )
            invocation_note = (
                f'({request.invoked_with})'
                if request.invoked_with.startswith('/')
                else ''
            )
            audit_notes = ' '.join(
                note for note in (log_by_str, request.log_note, invocation_note)
                if note
            )
            models.GameLog.write(
                game_id=game.id,
                guild_id=request.guild_id,
                message=(
                    f'Side {side.position} joined by '
                    f'{models.GameLog.member_string(player.discord_member)} '
                    f'{audit_notes}'
                ),
            )

            creating_player = game.creating_player()
            _friend_guidance(
                game=game,
                player=player,
                member=member,
                prefix=request.prefix,
                players_before=players_before,
                capacity=capacity,
                creating_player=creating_player,
                same_actor=(author.discord_id == member.discord_id),
                messages=messages,
            )

            notification_id = request.notification_member_id or member.discord_id
            waitlist_after = _waitlist_ids(request.guild_id, notification_id)
            if len(waitlist_after) > 1:
                start_str = (
                    f'Type __`{request.prefix}game IDNUM`__ for more details, '
                    f'ie `{request.prefix}game {waitlist_after[0]}`'
                )
                messages.append(
                    f':warning: You have full games waiting to start: '
                    f'**{", ".join(waitlist_after)}**\n{start_str}'
                )

            return JoinResult(
                game_id=game.id,
                guild_id=request.guild_id,
                member_id=member.discord_id,
                side_position=side.position,
                messages=tuple(messages),
                players=players_before + 1,
                capacity=capacity,
                creator_id=(
                    creating_player.discord_member.discord_id
                    if creating_player else None
                ),
                host_id=(
                    game.host.discord_member.discord_id
                    if game.host else None
                ),
                remove_inactive_role=remove_inactive_role,
                inactive_role_name=member.inactive_role_name,
                waitlist_ids=waitlist_after,
            )


def self_id(game) -> int:
    """Return a game ID without relying on a lazy relation."""

    return int(game.id)


def leave_game(request: LeaveRequest) -> LeaveResult:
    """Leave a pending game in one worker-local synchronous transaction."""

    member = request.member
    author = request.author
    with models.db.connection_context():
        with models.db.atomic():
            try:
                game = models.Game.get_by_id(request.game_id)
            except peewee.DoesNotExist as exc:
                raise PendingGameLeaveValidationError(
                    f'Game with ID {request.game_id} cannot be found.'
                ) from exc

            if game.guild_id != request.guild_id:
                raise PendingGameLeaveValidationError(
                    f'Game with ID {request.game_id} is associated with a '
                    'different Discord server.'
                )

            is_hosted_by_member = game.is_hosted_by(member.discord_id)[0]
            if is_hosted_by_member and author.level < 4:
                raise PendingGameLeaveValidationError(
                    'You do not have permissions to leave your own match.\n'
                    f'If you want to delete use `{request.prefix}delete '
                    f'{game.id}`'
                )

            if not game.is_pending:
                raise PendingGameLeaveValidationError(
                    f'Game {game.id} has already started and cannot be left.'
                )

            lineup = game.player(discord_id=member.discord_id)
            if not lineup:
                raise PendingGameLeaveValidationError(
                    f'You are not a member of game {game.id}'
                )

            host_warning = None
            if is_hosted_by_member:
                if request.invoked_with == 'reaction':
                    host_warning = (
                        '**Warning:** You are leaving your own game. You will '
                        'still be the host. If you want to delete use the '
                        '`delete` command in a bot channel.'
                    )
                else:
                    host_warning = (
                        '**Warning:** You are leaving your own game. You will '
                        'still be the host. If you want to delete use '
                        f'`{request.prefix}delete {game.id}`'
                    )

            invocation_note = (
                f' ({request.invoked_with})'
                if request.invoked_with.startswith('/')
                else ''
            )
            note = f' {request.log_note}' if request.log_note else ''
            models.GameLog.write(
                game_id=game.id,
                guild_id=request.guild_id,
                message=f'{member.description} left the game{note}{invocation_note}.',
            )
            lineup.delete_instance()

            return LeaveResult(
                game_id=game.id,
                guild_id=request.guild_id,
                member_id=member.discord_id,
                host_warning=host_warning,
                message=(
                    f'Removing you from game {game.id}.'
                    if request.invoked_with == 'reaction'
                    else 'Removing you from the game.'
                ),
            )


async def run_join(request: JoinRequest) -> JoinResult:
    """Run a join through the shared pending-game coordinator."""

    return await game_open_workers.pending_game_coordinator.run_worker(
        join_game,
        request,
    )


async def run_leave(request: LeaveRequest) -> LeaveResult:
    """Run a leave through the shared pending-game coordinator."""

    return await game_open_workers.pending_game_coordinator.run_worker(
        leave_game,
        request,
    )
