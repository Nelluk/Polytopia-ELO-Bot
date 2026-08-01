"""Bounded worker for atomic removal from a pending game.

The command adapters capture Discord values into frozen snapshots.  This
module reloads the game and lineup in a worker-local Peewee connection, then
performs the removal, audit log, and near-expiration extension in one
synchronous transaction.
"""

from __future__ import annotations

import datetime
import logging
import re
from dataclasses import dataclass

import peewee

import settings
from modules import game_join_workers, game_open_workers, models


logger = logging.getLogger('polybot.' + __name__)


class PendingGameKickValidationError(RuntimeError):
    """The current database state does not permit a pending-game kick."""


@dataclass(frozen=True)
class KickRequest:
    """Immutable primitive input for one pending-game kick."""

    game_id: int
    guild_id: int
    prefix: str
    author: game_join_workers.MemberSnapshot
    target: game_join_workers.MemberSnapshot | None = None
    target_query: str | None = None
    invoked_with: str = 'kick'


@dataclass(frozen=True)
class KickResult:
    """Primitive post-commit data needed by both invocation adapters."""

    game_id: int
    guild_id: int
    author_id: int
    target_id: int
    target_name: str
    removal_message: str
    expiration_reset: bool

    @property
    def expiration_message(self) -> str | None:
        if not self.expiration_reset:
            return None
        return (
            f'Game {self.game_id} expiration has been reset to 24 hours '
            'from now'
        )


_RAW_ID = re.compile(r'^[0-9]{15,21}$')
_MENTION_ID = re.compile(r'^<@!?([0-9]+)>$')


def _discord_id_from_query(value: str) -> int | None:
    """Keep the legacy raw-ID and mention lookup behavior without Discord."""

    value = str(value).strip()
    if _RAW_ID.fullmatch(value):
        return int(value)
    mention = _MENTION_ID.fullmatch(value)
    return int(mention.group(1)) if mention else None


def _matching_lineups(game, request: KickRequest):
    """Find one target, preserving legacy exact/substring name semantics."""

    lineups = tuple(game.lineup)
    if request.target is not None:
        target_id = request.target.discord_id
        return tuple(
            lineup for lineup in lineups
            if lineup.player.discord_member.discord_id == target_id
        )

    query = str(request.target_query or '').strip()
    target_id = _discord_id_from_query(query)
    if target_id is not None:
        return tuple(
            lineup for lineup in lineups
            if lineup.player.discord_member.discord_id == target_id
        )

    folded_query = query.casefold()
    exact = tuple(
        lineup for lineup in lineups
        if str(lineup.player.name).casefold() == folded_query
    )
    if exact:
        return exact
    return tuple(
        lineup for lineup in lineups
        if folded_query and folded_query in str(lineup.player.name).casefold()
    )


def _registered_author(author_id: int) -> bool:
    return models.DiscordMember.get_or_none(
        discord_id=author_id,
    ) is not None


def kick_game(request: KickRequest) -> KickResult:
    """Remove one pending-game lineup and audit it atomically."""

    with models.db.connection_context():
        with models.db.atomic():
            if (
                request.author.guild_id != request.guild_id
                or (
                    request.target is not None
                    and request.target.guild_id != request.guild_id
                )
            ):
                raise PendingGameKickValidationError(
                    'This request is associated with a different Discord '
                    'server.'
                )

            if not _registered_author(request.author.discord_id):
                raise PendingGameKickValidationError(
                    'This command requires bot registration first. Type '
                    f'__`{request.prefix}setname Your Mobile Name`__ or '
                    f'__`{request.prefix}steamname Your Steam Username`__ '
                    'to get started.'
                )

            try:
                game = models.Game.get_by_id(request.game_id)
            except peewee.DoesNotExist as exc:
                raise PendingGameKickValidationError(
                    f'Game with ID {request.game_id} cannot be found.'
                ) from exc

            if game.guild_id != request.guild_id:
                raise PendingGameKickValidationError(
                    f'Game with ID {request.game_id} is associated with a '
                    'different Discord server.'
                )

            is_hosted_by, host = game.is_hosted_by(request.author.discord_id)
            if not is_hosted_by and not request.author.is_staff:
                host_name = f' **{host.name}**' if host else ''
                helper_role = settings.guild_setting(
                    request.guild_id,
                    'helper_roles',
                )[0]
                raise PendingGameKickValidationError(
                    f'Only the game host{host_name} or a **@{helper_role}** '
                    'can do this.'
                )

            if not game.is_pending:
                raise PendingGameKickValidationError(
                    f'Game {game.id} has already started.'
                )

            matches = _matching_lineups(game, request)
            if not matches:
                target_label = request.target_query or (
                    request.target.discord_id
                    if request.target is not None
                    else ''
                )
                raise PendingGameKickValidationError(
                    f'Could not find a match for '
                    f'**{target_label}** '
                    f'in game {game.id}.'
                )
            if len(matches) > 1:
                matched_names = ', '.join(
                    str(lineup.player.name) for lineup in matches
                )
                raise PendingGameKickValidationError(
                    f'Could not uniquely match '
                    f'**{request.target_query}** in game {game.id}. '
                    f'Matches: {matched_names}.'
                )

            lineup = matches[0]
            target_id = lineup.player.discord_member.discord_id
            if target_id == request.author.discord_id:
                raise PendingGameKickValidationError('Stop kicking yourself!')

            target_name = lineup.player.name
            lineup.delete_instance()

            invocation_note = (
                f' ({request.invoked_with})'
                if request.invoked_with.startswith('/')
                else ''
            )
            models.GameLog.write(
                game_id=game.id,
                guild_id=request.guild_id,
                message=(
                    f'{request.author.description} kicked '
                    f'{models.GameLog.member_string(lineup.player.discord_member)}'
                    f'{invocation_note}'
                ),
            )

            expiration_reset = bool(
                game.expiration is not None
                and game.expiration < (
                    datetime.datetime.now() + datetime.timedelta(hours=2)
                )
            )
            if expiration_reset:
                game.expiration = (
                    datetime.datetime.now() + datetime.timedelta(hours=24)
                )
                game.save()

            return KickResult(
                game_id=game.id,
                guild_id=request.guild_id,
                author_id=request.author.discord_id,
                target_id=target_id,
                target_name=target_name,
                removal_message=(
                    f'Removing **{target_name}** from the game.'
                ),
                expiration_reset=expiration_reset,
            )


async def run_kick(request: KickRequest) -> KickResult:
    """Run a kick through the shared serialized pending-game coordinator."""

    return await game_open_workers.pending_game_coordinator.run_worker(
        kick_game,
        request,
    )
