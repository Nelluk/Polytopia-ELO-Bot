"""Worker-local immutable snapshots for confirmed-game publication."""

from __future__ import annotations

from dataclasses import dataclass

from peewee import fn

import settings
from modules import game_detail_workers, models, nova_graduation_workers


MAX_PUBLICATION_GUILDS = 100
MAX_PUBLICATION_SIDES = 50
MAX_PUBLICATION_PARTICIPANTS = 50
MAX_NOVA_CONTEXT_MEMBERS = 5_000


class ConfirmationPublicationSnapshotError(RuntimeError):
    """The committed-game publication snapshot could not be built safely."""


@dataclass(frozen=True)
class ConfirmationPublicationContext:
    """Primitive event-loop state needed by the confirmation worker."""

    bot_guild_ids: tuple[int, ...] = ()
    nova_guild_ids: tuple[int, ...] = ()
    nova_candidates: tuple[
        nova_graduation_workers.NovaParticipantSnapshot, ...
    ] = ()


@dataclass(frozen=True)
class ChannelPublicationTarget:
    guild_id: int
    channel_id: int


@dataclass(frozen=True)
class ExperienceRoleEffect:
    discord_id: int
    guild_ids: tuple[int, ...]
    earned_role_name: str | None
    removable_role_names: tuple[str, ...]


@dataclass(frozen=True)
class ChampionGuildEffect:
    guild_id: int
    local_champion_discord_id: int | None


@dataclass(frozen=True)
class ChampionRoleEffect:
    global_champion_discord_id: int | None
    guilds: tuple[ChampionGuildEffect, ...]


@dataclass(frozen=True)
class ConfirmationPublicationSnapshot:
    game: game_detail_workers.GameDetailSnapshot
    winner_name: str
    roster_mentions: tuple[str, ...]
    side_channel_targets: tuple[ChannelPublicationTarget, ...]
    game_channel_id: int | None
    experience_roles: tuple[ExperienceRoleEffect, ...]
    champion_roles: ChampionRoleEffect | None
    nova: nova_graduation_workers.NovaGraduationResult | None


def normalise_publication_context(
    context: ConfirmationPublicationContext,
) -> ConfirmationPublicationContext:
    bot_guild_ids = tuple(dict.fromkeys(int(value) for value in context.bot_guild_ids))
    nova_guild_ids = tuple(dict.fromkeys(int(value) for value in context.nova_guild_ids))
    if len(bot_guild_ids) > MAX_PUBLICATION_GUILDS:
        raise ConfirmationPublicationSnapshotError(
            f'Confirmation publication is limited to {MAX_PUBLICATION_GUILDS} bot guilds.'
        )
    if any(value <= 0 for value in (*bot_guild_ids, *nova_guild_ids)):
        raise ConfirmationPublicationSnapshotError(
            'Confirmation publication guild IDs must be positive.'
        )
    if len(context.nova_candidates) > MAX_NOVA_CONTEXT_MEMBERS:
        raise ConfirmationPublicationSnapshotError(
            'Confirmation publication is limited to '
            f'{MAX_NOVA_CONTEXT_MEMBERS} cached Nova candidates.'
        )
    candidates_by_id = {}
    for candidate in context.nova_candidates:
        existing = candidates_by_id.get(int(candidate.discord_id))
        if existing is not None and existing != candidate:
            raise ConfirmationPublicationSnapshotError(
                f'Nova candidate {candidate.discord_id} has conflicting cache data.'
            )
        candidates_by_id[int(candidate.discord_id)] = candidate
    nova_candidates = tuple(candidates_by_id.values())
    if any(candidate.discord_id <= 0 for candidate in nova_candidates):
        raise ConfirmationPublicationSnapshotError(
            'Confirmation publication Nova candidate IDs must be positive.'
        )
    return ConfirmationPublicationContext(
        bot_guild_ids,
        nova_guild_ids,
        nova_candidates,
    )


def _validate_game_snapshot(snapshot: game_detail_workers.GameDetailSnapshot) -> None:
    if len(snapshot.sides) > MAX_PUBLICATION_SIDES:
        raise ConfirmationPublicationSnapshotError(
            f'Confirmation publication is limited to {MAX_PUBLICATION_SIDES} sides.'
        )
    participant_count = sum(len(side.lineups) for side in snapshot.sides)
    if participant_count > MAX_PUBLICATION_PARTICIPANTS:
        raise ConfirmationPublicationSnapshotError(
            'Confirmation publication is limited to '
            f'{MAX_PUBLICATION_PARTICIPANTS} participants.'
        )


def _earned_experience_roles(discord_member) -> tuple[str | None, tuple[str, ...]]:
    completed_games = discord_member.completed_game_count(
        only_ranked=False,
        moonrise=models.is_post_moonrise(),
    )
    earned = []
    elo_max = (
        discord_member.elo_max_moonrise
        if models.is_post_moonrise()
        else discord_member.elo_max
    )
    if completed_games >= 2:
        earned.append('ELO Rookie')
    if completed_games >= 10:
        earned.append('ELO Player')
    if discord_member.elo_max >= 1200 or discord_member.elo_max_moonrise >= 1200:
        earned.append('ELO Veteran')
    if elo_max >= 1350:
        earned.append('ELO Hero')
    if elo_max >= 1500:
        earned.append('ELO Elite')
    if elo_max >= 1650:
        earned.append('ELO Master')
    if elo_max >= 1800:
        earned.append('ELO Titan')
    if not earned:
        return None, ()
    return earned[-1], tuple(earned[:-1])


def build_experience_role_effects(
    full_game,
) -> tuple[ExperienceRoleEffect, ...]:
    effects = []
    seen = set()
    for side in full_game.gamesides:
        for lineup in side.lineup:
            discord_member = lineup.player.discord_member
            discord_id = int(discord_member.discord_id)
            if discord_id in seen:
                continue
            seen.add(discord_id)
            earned, removable = _earned_experience_roles(discord_member)
            guild_ids = tuple(dict.fromkeys(
                int(player.guild_id) for player in discord_member.guildmembers
            ))
            if len(guild_ids) > MAX_PUBLICATION_GUILDS:
                raise ConfirmationPublicationSnapshotError(
                    f'Player {discord_id} has too many guild role targets.'
                )
            effects.append(ExperienceRoleEffect(
                discord_id=discord_id,
                guild_ids=guild_ids,
                earned_role_name=earned,
                removable_role_names=removable,
            ))
    return tuple(effects)


def _participant_requires_champion_refresh(full_game) -> bool:
    participants = []
    seen = set()
    for side in full_game.gamesides:
        for lineup in side.lineup:
            player = lineup.player
            discord_id = int(player.discord_member.discord_id)
            if discord_id not in seen:
                seen.add(discord_id)
                participants.append(player)
    if not participants:
        return False

    max_global_elo = models.DiscordMember.select(
        fn.Max(models.DiscordMember.elo_moonrise)
    ).scalar()
    local_maxima = {
        int(guild_id): models.Player.select(
            fn.Max(models.Player.elo_moonrise)
        ).where(models.Player.guild_id == int(guild_id)).scalar()
        for guild_id in {int(player.guild_id) for player in participants}
    }
    return any(
        (
            max_global_elo is not None
            and player.discord_member.elo_moonrise >= max_global_elo
        )
        or (
            local_maxima.get(int(player.guild_id)) is not None
            and player.elo_moonrise >= local_maxima[int(player.guild_id)]
        )
        for player in participants
    )


def _leaderboard_champion(query) -> int | None:
    champion = query.limit(1).first()
    if champion is None or int(champion.elo_field) == 1000:
        return None
    return int(champion.discord_member.discord_id)


def build_champion_role_effect(
    full_game,
    context: ConfirmationPublicationContext,
) -> ChampionRoleEffect | None:
    if not context.bot_guild_ids or not _participant_requires_champion_refresh(full_game):
        return None

    global_champion = models.DiscordMember.leaderboard(
        date_cutoff=settings.date_cutoff,
        guild_id=None,
        max_flag=False,
    ).limit(1).first()
    global_discord_id = None
    if global_champion is not None and int(global_champion.elo_field) != 1000:
        global_discord_id = int(global_champion.discord_id)

    guilds = []
    for guild_id in context.bot_guild_ids:
        local_discord_id = _leaderboard_champion(
            models.Player.leaderboard(
                date_cutoff=settings.date_cutoff,
                guild_id=guild_id,
                max_flag=False,
            )
        )
        guilds.append(ChampionGuildEffect(guild_id, local_discord_id))
    return ChampionRoleEffect(global_discord_id, tuple(guilds))


def _nova_snapshot(
    snapshot: game_detail_workers.GameDetailSnapshot,
    context: ConfirmationPublicationContext,
) -> nova_graduation_workers.NovaGraduationResult | None:
    if snapshot.guild_id not in context.nova_guild_ids:
        return None
    roster_ids = {
        lineup.discord_id for side in snapshot.sides for lineup in side.lineups
    }
    participants = tuple(
        candidate
        for candidate in context.nova_candidates
        if candidate.discord_id in roster_ids
    )
    return nova_graduation_workers.load_nova_graduation(
        nova_graduation_workers.NovaGraduationRequest(
            game_id=snapshot.game_id,
            guild_id=snapshot.guild_id,
            allowed_guild_ids=context.nova_guild_ids,
            participants=participants,
        )
    )


def build_confirmation_publication_snapshot(
    game_id: int,
    guild_id: int,
    context: ConfirmationPublicationContext,
) -> ConfirmationPublicationSnapshot:
    """Freeze every database-derived publication input on the ELO worker."""

    context = normalise_publication_context(context)
    full_game = models.Game.load_full_game(game_id)
    if int(full_game.guild_id) != int(guild_id):
        raise ConfirmationPublicationSnapshotError(
            f'Game {game_id} is associated with a different Discord server.'
        )
    if not full_game.is_completed or not full_game.is_confirmed or not full_game.winner:
        raise ConfirmationPublicationSnapshotError(
            f'Game {game_id} is not a committed confirmed result.'
        )

    snapshot = game_detail_workers.snapshot_loaded_game(
        full_game,
        request_guild_id=int(guild_id),
    )
    _validate_game_snapshot(snapshot)
    winner = next(
        (side for side in snapshot.sides if side.side_id == snapshot.winner_side_id),
        None,
    )
    if winner is None:
        raise ConfirmationPublicationSnapshotError(
            f'Game {game_id} has no winner in its publication snapshot.'
        )

    roster_ids = tuple(
        lineup.discord_id for side in snapshot.sides for lineup in side.lineups
    )
    side_targets = tuple(
        ChannelPublicationTarget(
            guild_id=side.external_guild_id or snapshot.guild_id,
            channel_id=side.channel_id,
        )
        for side in snapshot.sides
        if side.channel_id is not None
    )
    return ConfirmationPublicationSnapshot(
        game=snapshot,
        winner_name=winner.name,
        roster_mentions=tuple(f'<@{discord_id}>' for discord_id in roster_ids),
        side_channel_targets=side_targets,
        game_channel_id=snapshot.game_channel_id,
        experience_roles=build_experience_role_effects(full_game),
        champion_roles=build_champion_role_effect(full_game, context),
        nova=_nova_snapshot(snapshot, context),
    )
