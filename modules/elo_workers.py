"""Synchronous, transaction-bound ELO mutation workers."""

from __future__ import annotations

import datetime
from dataclasses import dataclass

import peewee

from modules import game_deletion_workers, models


class UnwinValidationError(RuntimeError):
    """The game changed or is not eligible for the requested unwin."""


class RecalculationValidationError(RuntimeError):
    """The requested recalculation starting point is not eligible."""


class WinValidationError(RuntimeError):
    """The game changed or is not eligible for the requested win."""


class DeleteValidationError(game_deletion_workers.GameDeletionValidationError):
    """The game changed or is not eligible for completed-game deletion."""


@dataclass(frozen=True)
class UnwinResult:
    game_id: int
    message: str
    post_unwin_messaging: bool
    previously_confirmed: bool


@dataclass(frozen=True)
class WinResult:
    game_id: int
    confirmed: bool
    all_sides_confirmed: bool
    winner_name: str
    confirmed_count: int
    side_count: int
    new_confirmation: bool
    first_claim: bool
    previous_winner_name: str | None
    previous_confirmed_count: int
    previous_side_count: int


@dataclass(frozen=True)
class ConfirmedWinResult:
    game_id: int
    winner_name: str


@dataclass(frozen=True)
class DeleteResult:
    game_id: int
    recalculated: bool
    effect_plan: game_deletion_workers.DeletionEffectPlan | None = None


def _load_game(
    game_id: int,
    guild_id: int,
    error_type=UnwinValidationError,
):
    try:
        game = models.Game.get_by_id(game_id)
    except peewee.DoesNotExist as exc:
        raise error_type(
            f'Game with ID {game_id} cannot be found.'
        ) from exc
    if game.guild_id != guild_id:
        raise error_type(
            f'Game with ID {game_id} is associated with a different '
            'Discord server.'
        )
    return game


def unwin_game(
    game_id: int,
    guild_id: int,
    requester_id: int,
    requester_description: str,
    is_staff: bool,
) -> UnwinResult:
    """Mutate an unwin entirely within one worker-local transaction."""

    with models.db.connection_context():
        with models.db.atomic():
            game = _load_game(game_id, guild_id)
            if game.is_pending:
                raise UnwinValidationError(
                    f'Game {game.id} is marked as *pending / not started*. '
                    'This command cannot be used.'
                )
            if not game.is_completed:
                raise UnwinValidationError(
                    f'Game {game.id} is marked as *Incomplete*. '
                    'This command cannot be used.'
                )

            if is_staff:
                game.confirmations_reset()
                models.GameLog.write(
                    game_id=game.id,
                    guild_id=guild_id,
                    message=(
                        f'{requester_description} staffer used unwin command.'
                    ),
                )
                if game.is_confirmed:
                    timestamp = game.completed_ts
                    ranked = game.is_ranked
                    game.reverse_elo_changes()
                    game.completed_ts = None
                    game.is_confirmed = False
                    game.is_completed = False
                    game.winner = None
                    game.save()
                    if ranked:
                        models.Game.recalculate_elo_since(timestamp=timestamp)
                        message = (
                            f'Game {game.id} has been marked as *Incomplete*. '
                            'ELO changes have been reverted and ELO from all '
                            'subsequent games recalculated.'
                        )
                    else:
                        message = (
                            f'Unranked game {game.id} has been marked as '
                            '*Incomplete*.'
                        )
                    return UnwinResult(
                        game_id=game.id,
                        message=message,
                        post_unwin_messaging=True,
                        previously_confirmed=True,
                    )

                game.completed_ts = None
                game.is_completed = False
                game.winner = None
                game.save()
                return UnwinResult(
                    game_id=game.id,
                    message=(
                        f'Unconfirmed Game {game.id} has been marked as '
                        '*Incomplete*.'
                    ),
                    post_unwin_messaging=True,
                    previously_confirmed=False,
                )

            has_player, author_side = game.has_player(
                discord_id=requester_id
            )
            if not has_player:
                raise UnwinValidationError(
                    f'You are not a player in game {game.id} and do not have '
                    'server staff permissions.'
                )
            if game.is_confirmed:
                raise UnwinValidationError(
                    f'Game {game.id} has been confirmed already. Only server '
                    'staff can use this command on confirmed games.'
                )
            if not author_side.win_confirmed:
                raise UnwinValidationError(
                    f'Your side **{author_side.name()}** has no record of '
                    f'confirming a win from game {game.id} - this command '
                    'cannot be used.'
                )

            if author_side == game.winner:
                models.GameLog.write(
                    game_id=game.id,
                    guild_id=guild_id,
                    message=(
                        f'{requester_description} removes their self-win '
                        'claim and confirmations have reset.'
                    ),
                )
                game.confirmations_reset()
                game.completed_ts = None
                game.is_completed = False
                game.winner = None
                game.save()
                return UnwinResult(
                    game_id=game.id,
                    message=(
                        f'Your unconfirmed win in game {game.id} has been '
                        'reset and the game is now marked as *Incomplete*.'
                    ),
                    post_unwin_messaging=True,
                    previously_confirmed=False,
                )

            models.GameLog.write(
                game_id=game.id,
                guild_id=guild_id,
                message=(
                    f'{requester_description} removed their confirmation of '
                    'the game winner.'
                ),
            )
            author_side.win_confirmed = False
            author_side.save()
            confirmed_count, side_count, _ = game.confirmations_count()
            return UnwinResult(
                game_id=game.id,
                message=(
                    f'Your confirmation that **{game.winner.name()}** won '
                    f'game {game.id} has been *removed*. The win is still '
                    f'pending confirmation. {confirmed_count} of {side_count} '
                    'sides are marked as confirming.'
                ),
                post_unwin_messaging=False,
                previously_confirmed=False,
            )


def record_win(
    game_id: int,
    guild_id: int,
    winning_side_id: int,
    requester_id: int,
    requester_description: str,
    is_staff: bool,
) -> WinResult:
    """Record a win claim or finalize it in one worker transaction."""

    with models.db.connection_context():
        with models.db.atomic():
            game = _load_game(
                game_id, guild_id, error_type=WinValidationError
            )
            if game.is_pending:
                raise WinValidationError(
                    f'Game {game.id} is still a pending open game. It must be '
                    'started before it can be concluded.'
                )
            if game.is_completed and game.is_confirmed:
                raise WinValidationError(
                    f'Game with ID {game.id} is already marked as completed.'
                )

            winning_side = next(
                (
                    side for side in game.gamesides
                    if side.id == winning_side_id
                ),
                None,
            )
            if winning_side is None:
                raise WinValidationError(
                    f'GameSide {winning_side_id} did not play in game '
                    f'{game.id}.'
                )

            previous_winner_name = None
            previous_confirmed_count = 0
            previous_side_count = 0
            if (
                game.is_completed
                and game.winner
                and game.winner.id != winning_side.id
            ):
                previous_winner_name = game.winner.name()
                (
                    previous_confirmed_count,
                    previous_side_count,
                    _,
                ) = game.confirmations_count()
                game.confirmations_reset()

            has_player, author_side = game.has_player(
                discord_id=requester_id
            )
            new_confirmation = False
            if is_staff and not has_player:
                confirm_win = True
                all_sides_confirmed = False
            else:
                if not has_player:
                    raise WinValidationError(
                        'You were not a participant in this game.'
                    )
                new_confirmation = not author_side.win_confirmed
                winning_side.win_confirmed = True
                author_side.win_confirmed = True
                winning_side.save()
                author_side.save()
                _, _, confirm_win = game.confirmations_count()
                all_sides_confirmed = confirm_win

            first_claim = not bool(game.win_claimed_ts)
            if not confirm_win and first_claim:
                game.win_claimed_ts = datetime.datetime.now()
                game.save()

            game.declare_winner(
                winning_side=winning_side,
                confirm=confirm_win,
            )
            confirmed_count, side_count, _ = game.confirmations_count()
            models.GameLog.write(
                game_id=game.id,
                guild_id=guild_id,
                message=(
                    f'Win confirm logged by {requester_description} for '
                    f'winner **{winning_side.name()}**'
                ),
            )
            return WinResult(
                game_id=game.id,
                confirmed=confirm_win,
                all_sides_confirmed=all_sides_confirmed,
                winner_name=winning_side.name(),
                confirmed_count=confirmed_count,
                side_count=side_count,
                new_confirmation=new_confirmation,
                first_claim=first_claim,
                previous_winner_name=previous_winner_name,
                previous_confirmed_count=previous_confirmed_count,
                previous_side_count=previous_side_count,
            )


def confirm_game(
    game_id: int,
    guild_id: int,
    requester_description: str,
) -> ConfirmedWinResult:
    """Finalize a previously claimed winner in one worker transaction."""

    with models.db.connection_context():
        with models.db.atomic():
            game = _load_game(
                game_id, guild_id, error_type=WinValidationError
            )
            if not game.is_completed or not game.winner:
                raise WinValidationError(
                    f'Game {game.id} has no declared winner yet.'
                )
            if game.is_confirmed:
                raise WinValidationError(
                    f'Game with ID {game.id} is already confirmed as '
                    'completed.'
                )
            winner_name = game.winner.name()
            game.declare_winner(winning_side=game.winner, confirm=True)
            models.GameLog.write(
                game_id=game.id,
                guild_id=guild_id,
                message=(
                    f'{requester_description} confirmed winner '
                    f'**{winner_name}** and processed ELO changes.'
                ),
            )
            return ConfirmedWinResult(
                game_id=game.id,
                winner_name=winner_name,
            )


def delete_game(
    game_id: int,
    guild_id: int,
    requester_description: str,
) -> DeleteResult:
    """Delete a non-pending game and reverse ELO in one transaction."""

    with models.db.connection_context():
        with models.db.atomic():
            game = _load_game(
                game_id,
                guild_id,
                error_type=DeleteValidationError,
            )
            if game.is_pending:
                raise DeleteValidationError(
                    f'Game {game.id} is pending and must use the pending-game '
                    'deletion path.'
                )
            recalculated = bool(
                game.winner and game.is_confirmed and game.is_ranked
            )
            effect_plan = game_deletion_workers.build_effect_plan(
                game,
                guild_id=guild_id,
            )
            models.GameLog.write(
                game_id=game.id,
                guild_id=guild_id,
                message=f'{requester_description} deleted the game.',
            )
            game.delete_game()
            return DeleteResult(
                game_id=game_id,
                recalculated=recalculated,
                effect_plan=effect_plan,
            )


def recalculate_games_from(game_id: int) -> datetime.datetime:
    """Recalculate from a reloaded game in one worker-local transaction."""

    with models.db.connection_context():
        with models.db.atomic():
            try:
                game = models.Game.get_by_id(game_id)
            except peewee.DoesNotExist as exc:
                raise RecalculationValidationError(
                    f'No game found for id {game_id}'
                ) from exc
            if not game.completed_ts:
                raise RecalculationValidationError(
                    f'Game {game.id} is not completed. Choose a completed '
                    'game.'
                )
            timestamp = game.completed_ts
            models.Game.recalculate_elo_since(timestamp=timestamp)
            return timestamp
