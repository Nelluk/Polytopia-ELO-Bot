"""Worker-local immutable snapshots for ordinary win and unwin publication."""

from __future__ import annotations

from dataclasses import dataclass

from modules import confirmation_publication_workers, game_detail_workers, models


class GameResultPublicationSnapshotError(RuntimeError):
    """A committed ordinary result could not be frozen for publication."""


@dataclass(frozen=True)
class GameResultPublicationSnapshot:
    """Database-derived inputs used after an ordinary result mutation."""

    game: game_detail_workers.GameDetailSnapshot
    roster_mentions: tuple[str, ...]
    side_channel_targets: tuple[
        confirmation_publication_workers.ChannelPublicationTarget, ...
    ]
    game_channel_id: int | None
    experience_roles: tuple[
        confirmation_publication_workers.ExperienceRoleEffect, ...
    ] = ()
    champion_roles: (
        confirmation_publication_workers.ChampionRoleEffect | None
    ) = None
    confirmed_publication: (
        confirmation_publication_workers.ConfirmationPublicationSnapshot | None
    ) = None


def _validate_snapshot(
    snapshot: game_detail_workers.GameDetailSnapshot,
) -> None:
    if len(snapshot.sides) > confirmation_publication_workers.MAX_PUBLICATION_SIDES:
        raise GameResultPublicationSnapshotError(
            'Game-result publication is limited to '
            f'{confirmation_publication_workers.MAX_PUBLICATION_SIDES} sides.'
        )
    participants = sum(len(side.lineups) for side in snapshot.sides)
    if participants > confirmation_publication_workers.MAX_PUBLICATION_PARTICIPANTS:
        raise GameResultPublicationSnapshotError(
            'Game-result publication is limited to '
            f'{confirmation_publication_workers.MAX_PUBLICATION_PARTICIPANTS} '
            'participants.'
        )


def _freeze_loaded_game(full_game, guild_id: int) -> GameResultPublicationSnapshot:
    if int(full_game.guild_id) != int(guild_id):
        raise GameResultPublicationSnapshotError(
            f'Game {full_game.id} is associated with a different Discord server.'
        )
    snapshot = game_detail_workers.snapshot_loaded_game(
        full_game,
        request_guild_id=int(guild_id),
    )
    _validate_snapshot(snapshot)
    roster_ids = tuple(
        lineup.discord_id for side in snapshot.sides for lineup in side.lineups
    )
    side_targets = tuple(
        confirmation_publication_workers.ChannelPublicationTarget(
            guild_id=side.external_guild_id or snapshot.guild_id,
            channel_id=side.channel_id,
        )
        for side in snapshot.sides
        if side.channel_id is not None
    )
    return GameResultPublicationSnapshot(
        game=snapshot,
        roster_mentions=tuple(f'<@{discord_id}>' for discord_id in roster_ids),
        side_channel_targets=side_targets,
        game_channel_id=snapshot.game_channel_id,
    )


def build_win_publication_snapshot(
    game_id: int,
    guild_id: int,
    *,
    confirmed: bool,
    context: confirmation_publication_workers.ConfirmationPublicationContext,
) -> GameResultPublicationSnapshot:
    """Freeze one committed ordinary win while its coordinator claim is held."""

    if confirmed:
        publication = (
            confirmation_publication_workers
            .build_confirmation_publication_snapshot(game_id, guild_id, context)
        )
        return GameResultPublicationSnapshot(
            game=publication.game,
            roster_mentions=publication.roster_mentions,
            side_channel_targets=publication.side_channel_targets,
            game_channel_id=publication.game_channel_id,
            experience_roles=publication.experience_roles,
            champion_roles=publication.champion_roles,
            confirmed_publication=publication,
        )

    full_game = models.Game.load_full_game(game_id)
    snapshot = _freeze_loaded_game(full_game, guild_id)
    if (
        not snapshot.game.is_completed
        or snapshot.game.is_confirmed
        or snapshot.game.winner_side_id is None
    ):
        raise GameResultPublicationSnapshotError(
            f'Game {game_id} is not a committed unconfirmed result.'
        )
    return snapshot


def build_unwin_publication_snapshot(
    game_id: int,
    guild_id: int,
    *,
    previously_confirmed: bool,
    context: confirmation_publication_workers.ConfirmationPublicationContext,
) -> GameResultPublicationSnapshot:
    """Freeze one committed reset while its coordinator claim is held."""

    full_game = models.Game.load_full_game(game_id)
    snapshot = _freeze_loaded_game(full_game, guild_id)
    if (
        snapshot.game.is_completed
        or snapshot.game.is_confirmed
        or snapshot.game.winner_side_id is not None
    ):
        raise GameResultPublicationSnapshotError(
            f'Game {game_id} is not a committed incomplete reset.'
        )
    if not previously_confirmed:
        return snapshot

    normalised = (
        confirmation_publication_workers.normalise_publication_context(context)
    )
    return GameResultPublicationSnapshot(
        game=snapshot.game,
        roster_mentions=snapshot.roster_mentions,
        side_channel_targets=snapshot.side_channel_targets,
        game_channel_id=snapshot.game_channel_id,
        experience_roles=(
            confirmation_publication_workers
            .build_experience_role_effects(full_game)
        ),
        champion_roles=(
            confirmation_publication_workers
            .build_champion_role_effect(full_game, normalised)
        ),
    )
