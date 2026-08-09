"""Strictly gated development-database fixtures for beta command testing."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import datetime
import json
from pathlib import Path
import tempfile
from typing import Any, Iterable, Sequence


EXPECTED_ENVIRONMENT = 'development'
EXPECTED_DATABASE = 'polytopia_dev'
EXPECTED_ROLE = 'polybot_dev'
FIXTURE_VERSION = 1
FIXTURE_NOTES_MARKER = f'polybot-dev-beta-fixture:v{FIXTURE_VERSION}'
FIXTURE_NAME_PREFIX = 'Beta Fixture'
SCENARIOS = ('ready', 'unconfirmed', 'completed')
FIXTURE_LEAGUE_TIER = 1
FIXTURE_COMPLETED_LEAGUE_SEASON = 3
FIXTURE_CURRENT_LEAGUE_SEASON = 4
LEADERBOARD_FIXTURE_COUNT = 24
LEADERBOARD_DISCORD_ID_BASE = 9_000_000_000_100_000_000
LEADERBOARD_NAME_PREFIX = 'LB2 Showcase'
LEADERBOARD_GAME_MARKER = 'polybot-dev-lb2-showcase:v1'


class FixtureSafetyError(RuntimeError):
    """The selected runtime or live database is not safe for fixtures."""


class FixtureValidationError(RuntimeError):
    """Fixture inputs or existing fixture state are invalid."""


@dataclass(frozen=True)
class FixtureGame:
    scenario: str
    game_id: int
    name: str
    is_completed: bool
    is_confirmed: bool
    is_ranked: bool
    is_pending: bool
    expiration: str | None
    league_season: int | None = None
    league_tier: int | None = None


@dataclass(frozen=True)
class FixtureState:
    guild_id: int
    user_ids: tuple[int, ...]
    games: tuple[FixtureGame, ...]


@dataclass(frozen=True)
class LeaderboardFixturePlayer:
    discord_id: int
    player_id: int
    name: str
    elo: int
    elo_max: int


@dataclass(frozen=True)
class LeaderboardFixtureState:
    guild_id: int
    players: tuple[LeaderboardFixturePlayer, ...]
    game_ids: tuple[int, ...]


def default_manifest_path(project_root: Path) -> Path:
    return project_root / 'data/development/beta_fixture_manifest.json'


def validate_profile(profile: Any) -> None:
    """Reject every runtime profile except the isolated development profile."""

    if (
        profile.environment != EXPECTED_ENVIRONMENT
        or profile.database_name != EXPECTED_DATABASE
        or profile.database_user != EXPECTED_ROLE
    ):
        raise FixtureSafetyError(
            'Beta fixtures require POLYBOT_ENV=development, database '
            f'{EXPECTED_DATABASE}, and role {EXPECTED_ROLE}.'
        )
    if profile.background_tasks_enabled or profile.api_enabled:
        raise FixtureSafetyError(
            'Beta fixtures require background tasks and the API to be '
            'disabled.'
        )


def validate_live_identity(database_name: str, database_role: str) -> None:
    if (
        database_name != EXPECTED_DATABASE
        or database_role != EXPECTED_ROLE
    ):
        raise FixtureSafetyError(
            'The live PostgreSQL session is not connected to the approved '
            f'{EXPECTED_DATABASE} database as {EXPECTED_ROLE}.'
        )


def validate_user_ids(user_ids: Iterable[int]) -> tuple[int, ...]:
    normalized = tuple(int(user_id) for user_id in user_ids)
    if len(normalized) not in (2, 4, 6, 8):
        raise FixtureValidationError(
            'Supply an even group of 2, 4, 6, or 8 existing Discord user IDs.'
        )
    if any(user_id <= 0 for user_id in normalized):
        raise FixtureValidationError('Discord user IDs must be positive.')
    if len(set(normalized)) != len(normalized):
        raise FixtureValidationError('Discord user IDs must be unique.')
    return normalized


def validate_guild_id(profile: Any, guild_id: int) -> int:
    guild_id = int(guild_id)
    if guild_id not in profile.allowed_guild_ids:
        raise FixtureSafetyError(
            f'Guild {guild_id} is not allowed by the development profile.'
        )
    return guild_id


def is_owned_game(game: Any, guild_id: int) -> bool:
    return (
        int(game.guild_id) == int(guild_id)
        and game.notes == FIXTURE_NOTES_MARKER
        and bool(game.name)
        and game.name.startswith(FIXTURE_NAME_PREFIX)
    )


def _leaderboard_name(index: int) -> str:
    return f'{LEADERBOARD_NAME_PREFIX} {index:02d}'


def _leaderboard_discord_id(index: int) -> int:
    return LEADERBOARD_DISCORD_ID_BASE + index


def _leaderboard_game_name(game_number: int) -> str:
    # Game.save() normalizes names with str.title().
    return f'{LEADERBOARD_NAME_PREFIX} Game {game_number:02d}'.title()


def _leaderboard_index(discord_id: int) -> int | None:
    index = int(discord_id) - LEADERBOARD_DISCORD_ID_BASE
    if 1 <= index <= LEADERBOARD_FIXTURE_COUNT:
        return index
    return None


def is_owned_leaderboard_player(player: Any, guild_id: int) -> bool:
    """Require every synthetic-player ownership marker to agree."""

    index = _leaderboard_index(player.discord_member.discord_id)
    return (
        index is not None
        and int(player.guild_id) == int(guild_id)
        and player.name == _leaderboard_name(index)
        and player.discord_member.name == _leaderboard_name(index)
    )


def is_owned_leaderboard_game(game: Any, guild_id: int) -> bool:
    return (
        int(game.guild_id) == int(guild_id)
        and game.notes == LEADERBOARD_GAME_MARKER
        and bool(game.name)
        and game.name in {
            _leaderboard_game_name(game_number)
            for game_number in range(
                1,
                (LEADERBOARD_FIXTURE_COUNT // 2) * 4 + 1,
            )
        }
    )


def _live_identity(models_module: Any) -> tuple[str, str]:
    row = models_module.db.execute_sql(
        'SELECT current_database(), current_user'
    ).fetchone()
    return str(row[0]), str(row[1])


def _load_players(
    models_module: Any,
    guild_id: int,
    user_ids: Sequence[int],
) -> tuple[Any, ...]:
    players = tuple(
        models_module.Player
        .select(models_module.Player, models_module.DiscordMember)
        .join(models_module.DiscordMember)
        .where(
            (models_module.Player.guild_id == guild_id)
            & (models_module.DiscordMember.discord_id.in_(user_ids))
        )
    )
    by_discord_id = {
        int(player.discord_member.discord_id): player
        for player in players
    }
    missing = [
        user_id for user_id in user_ids
        if user_id not in by_discord_id
    ]
    if missing:
        joined = ', '.join(str(user_id) for user_id in missing)
        raise FixtureValidationError(
            'These users do not have an existing Player record in guild '
            f'{guild_id}: {joined}'
        )
    return tuple(by_discord_id[user_id] for user_id in user_ids)


def _scenario_name(scenario: str) -> str:
    return f'{FIXTURE_NAME_PREFIX} {scenario.title()}'


def _find_fixture_games(models_module: Any, guild_id: int) -> tuple[Any, ...]:
    return tuple(
        models_module.Game.select().where(
            (models_module.Game.guild_id == guild_id)
            & (models_module.Game.notes == FIXTURE_NOTES_MARKER)
        ).order_by(models_module.Game.id)
    )


def _find_leaderboard_players(
    models_module: Any,
    guild_id: int,
) -> tuple[Any, ...]:
    first_id = _leaderboard_discord_id(1)
    last_id = _leaderboard_discord_id(LEADERBOARD_FIXTURE_COUNT)
    return tuple(
        models_module.Player
        .select(models_module.Player, models_module.DiscordMember)
        .join(models_module.DiscordMember)
        .where(
            (models_module.Player.guild_id == guild_id)
            & (
                models_module.DiscordMember.discord_id.between(
                    first_id,
                    last_id,
                )
            )
        )
        .order_by(models_module.DiscordMember.discord_id)
    )


def _find_leaderboard_games(
    models_module: Any,
    guild_id: int,
) -> tuple[Any, ...]:
    return tuple(
        models_module.Game.select().where(
            (models_module.Game.guild_id == guild_id)
            & (models_module.Game.notes == LEADERBOARD_GAME_MARKER)
        ).order_by(models_module.Game.id)
    )


def _leaderboard_state_from_open_connection(
    models_module: Any,
    guild_id: int,
) -> LeaderboardFixtureState:
    players = _find_leaderboard_players(models_module, guild_id)
    games = _find_leaderboard_games(models_module, guild_id)
    return LeaderboardFixtureState(
        guild_id=guild_id,
        players=tuple(
            LeaderboardFixturePlayer(
                discord_id=int(player.discord_member.discord_id),
                player_id=int(player.id),
                name=str(player.name),
                elo=int(player.elo_moonrise),
                elo_max=int(player.elo_max_moonrise),
            )
            for player in players
        ),
        game_ids=tuple(int(game.id) for game in games),
    )


def _round_robin_pairs(
    players: Sequence[Any],
    rounds: int = 4,
) -> tuple[tuple[Any, Any], ...]:
    rotation = list(players)
    pairs = []
    for _ in range(rounds):
        for index in range(len(rotation) // 2):
            pairs.append((rotation[index], rotation[-index - 1]))
        rotation = [rotation[0], rotation[-1], *rotation[1:-1]]
    return tuple(pairs)


def _create_leaderboard_game(
    models_module: Any,
    guild_id: int,
    first_player: Any,
    second_player: Any,
    game_number: int,
) -> Any:
    game = models_module.Game.create(
        guild_id=guild_id,
        host=first_player,
        name=_leaderboard_game_name(game_number),
        notes=LEADERBOARD_GAME_MARKER,
        is_pending=False,
        is_ranked=True,
        is_mobile=True,
        size=[1, 1],
    )
    first_side = models_module.GameSide.create(
        game=game,
        size=1,
        position=1,
        sidename='Showcase Gold',
    )
    second_side = models_module.GameSide.create(
        game=game,
        size=1,
        position=2,
        sidename='Showcase Blue',
    )
    models_module.Lineup.create(
        game=game,
        gameside=first_side,
        player=first_player,
    )
    models_module.Lineup.create(
        game=game,
        gameside=second_side,
        player=second_player,
    )
    first_index = _leaderboard_index(
        first_player.discord_member.discord_id
    )
    second_index = _leaderboard_index(
        second_player.discord_member.discord_id
    )
    winner = first_side if first_index < second_index else second_side
    game.declare_winner(winning_side=winner, confirm=True)
    return game


def _set_leaderboard_ratings(players: Sequence[Any]) -> None:
    for index, player in enumerate(players, start=1):
        current = 1610 - ((index - 1) * 22)
        peak = current + 35 + ((index % 4) * 8)
        all_time = current + (18 if index % 3 == 0 else -12)
        all_time_peak = max(peak, all_time + 42)
        values = {
            'elo': current,
            'elo_max': peak,
            'elo_moonrise': current,
            'elo_max_moonrise': peak,
            'elo_alltime': all_time,
            'elo_max_alltime': all_time_peak,
        }
        for field, value in values.items():
            setattr(player, field, value)
            setattr(player.discord_member, field, value)
        player.save()
        player.discord_member.save()


def _scenario_from_name(name: str) -> str | None:
    for scenario in SCENARIOS:
        if name == _scenario_name(scenario):
            return scenario
    return None


def _create_scenario(
    models_module: Any,
    guild_id: int,
    players: Sequence[Any],
    scenario: str,
) -> Any:
    side_size = len(players) // 2
    game = models_module.Game.create(
        guild_id=guild_id,
        host=players[0],
        name=_scenario_name(scenario),
        notes=FIXTURE_NOTES_MARKER,
        is_pending=False,
        is_ranked=True,
        is_mobile=True,
        size=[side_size, side_size],
    )
    _apply_scenario_metadata(game, scenario)
    first_side = models_module.GameSide.create(
        game=game,
        size=side_size,
        position=1,
        sidename='Fixture Alpha',
    )
    second_side = models_module.GameSide.create(
        game=game,
        size=side_size,
        position=2,
        sidename='Fixture Beta',
    )
    for player in players[:side_size]:
        models_module.Lineup.create(
            game=game,
            gameside=first_side,
            player=player,
        )
    for player in players[side_size:]:
        models_module.Lineup.create(
            game=game,
            gameside=second_side,
            player=player,
        )

    if scenario == 'unconfirmed':
        game.win_claimed_ts = datetime.datetime.now()
        game.save()
        game.declare_winner(winning_side=first_side, confirm=False)
    elif scenario == 'completed':
        game.declare_winner(winning_side=first_side, confirm=True)
    elif scenario != 'ready':
        raise FixtureValidationError(f'Unknown fixture scenario: {scenario}')
    return game


def _apply_scenario_metadata(game: Any, scenario: str) -> None:
    """Keep the owned result fixtures useful for league-price smoke tests."""

    if scenario == 'completed':
        league_season = FIXTURE_COMPLETED_LEAGUE_SEASON
        league_tier = FIXTURE_LEAGUE_TIER
    elif scenario == 'unconfirmed':
        league_season = FIXTURE_CURRENT_LEAGUE_SEASON
        league_tier = FIXTURE_LEAGUE_TIER
    else:
        league_season = None
        league_tier = None
    if (
        game.league_season != league_season
        or game.league_tier != league_tier
    ):
        game.league_season = league_season
        game.league_tier = league_tier
        game.save(
            only=[game.__class__.league_season, game.__class__.league_tier]
        )


def _game_view(game: Any) -> FixtureGame:
    scenario = _scenario_from_name(game.name) or 'unknown'
    return FixtureGame(
        scenario=scenario,
        game_id=int(game.id),
        name=str(game.name),
        is_completed=bool(game.is_completed),
        is_confirmed=bool(game.is_confirmed),
        is_ranked=bool(game.is_ranked),
        is_pending=bool(game.is_pending),
        expiration=(
            game.expiration.isoformat()
            if game.expiration is not None
            else None
        ),
        league_season=(
            int(game.league_season)
            if game.league_season is not None
            else None
        ),
        league_tier=(
            int(game.league_tier)
            if game.league_tier is not None
            else None
        ),
    )


def _user_ids_for_games(games: Sequence[Any]) -> tuple[int, ...]:
    user_ids = {
        int(lineup.player.discord_member.discord_id)
        for game in games
        for lineup in game.lineup
    }
    return tuple(sorted(user_ids))


def _state_from_open_connection(
    models_module: Any,
    guild_id: int,
) -> FixtureState:
    games = _find_fixture_games(models_module, guild_id)
    return FixtureState(
        guild_id=guild_id,
        user_ids=_user_ids_for_games(games),
        games=tuple(_game_view(game) for game in games),
    )


def _write_manifest(path: Path, state: FixtureState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'fixture_version': FIXTURE_VERSION,
        'guild_id': state.guild_id,
        'user_ids': list(state.user_ids),
        'games': [asdict(game) for game in state.games],
        'updated_at': datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat(),
    }
    with tempfile.NamedTemporaryFile(
        mode='w',
        encoding='utf-8',
        dir=path.parent,
        prefix=f'.{path.name}.',
        suffix='.tmp',
        delete=False,
    ) as manifest_file:
        json.dump(payload, manifest_file, indent=2, sort_keys=True)
        manifest_file.write('\n')
        temporary_path = Path(manifest_file.name)
    temporary_path.replace(path)


def _remove_manifest(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def fixture_status(
    *,
    profile: Any,
    models_module: Any,
    guild_id: int,
) -> FixtureState:
    validate_profile(profile)
    guild_id = validate_guild_id(profile, guild_id)
    with models_module.db.connection_context():
        validate_live_identity(*_live_identity(models_module))
        return _state_from_open_connection(models_module, guild_id)


def seed_fixtures(
    *,
    profile: Any,
    models_module: Any,
    guild_id: int,
    user_ids: Iterable[int],
    manifest_path: Path,
) -> FixtureState:
    validate_profile(profile)
    guild_id = validate_guild_id(profile, guild_id)
    normalized_user_ids = validate_user_ids(user_ids)

    with models_module.db.connection_context():
        validate_live_identity(*_live_identity(models_module))
        with models_module.db.atomic():
            existing_games = _find_fixture_games(models_module, guild_id)
            existing_scenarios = {
                _scenario_from_name(game.name): game
                for game in existing_games
            }
            known_scenarios = [
                _scenario_from_name(game.name)
                for game in existing_games
                if _scenario_from_name(game.name) is not None
            ]
            if len(known_scenarios) != len(set(known_scenarios)):
                raise FixtureValidationError(
                    'Duplicate owned fixture scenarios require review before '
                    'seeding.'
                )
            unknown_games = [
                game for game in existing_games
                if _scenario_from_name(game.name) is None
            ]
            if unknown_games:
                ids = ', '.join(str(game.id) for game in unknown_games)
                raise FixtureValidationError(
                    'Unknown owned fixture games require review before '
                    f'seeding: {ids}'
                )

            existing_user_ids = _user_ids_for_games(existing_games)
            if existing_games and existing_user_ids != tuple(
                sorted(normalized_user_ids)
            ):
                raise FixtureValidationError(
                    'Existing fixtures reference a different user set. Run '
                    'status and cleanup before reseeding.'
                )

            players = _load_players(
                models_module, guild_id, normalized_user_ids
            )
            for scenario in SCENARIOS:
                if scenario not in existing_scenarios:
                    _create_scenario(
                        models_module,
                        guild_id,
                        players,
                        scenario,
                    )
                else:
                    _apply_scenario_metadata(
                        existing_scenarios[scenario], scenario
                    )

        state = _state_from_open_connection(models_module, guild_id)
    _write_manifest(manifest_path, state)
    return state


def cleanup_fixtures(
    *,
    profile: Any,
    models_module: Any,
    guild_id: int,
    manifest_path: Path,
    confirmed: bool,
) -> FixtureState:
    validate_profile(profile)
    guild_id = validate_guild_id(profile, guild_id)
    if not confirmed:
        raise FixtureValidationError(
            'Cleanup requires the explicit --confirm option.'
        )

    with models_module.db.connection_context():
        validate_live_identity(*_live_identity(models_module))
        with models_module.db.atomic():
            games = _find_fixture_games(models_module, guild_id)
            if any(not is_owned_game(game, guild_id) for game in games):
                raise FixtureSafetyError(
                    'Cleanup found a game that failed the fixture ownership '
                    'check.'
                )
            for game in sorted(
                games,
                key=lambda item: item.completed_ts or datetime.datetime.min,
                reverse=True,
            ):
                game.delete_game()

        remaining = _state_from_open_connection(models_module, guild_id)
    if not remaining.games:
        _remove_manifest(manifest_path)
    return remaining


def leaderboard_fixture_status(
    *,
    profile: Any,
    models_module: Any,
    guild_id: int,
) -> LeaderboardFixtureState:
    """Inspect only the separately owned Components v2 showcase fixtures."""

    validate_profile(profile)
    guild_id = validate_guild_id(profile, guild_id)
    with models_module.db.connection_context():
        validate_live_identity(*_live_identity(models_module))
        return _leaderboard_state_from_open_connection(
            models_module,
            guild_id,
        )


def seed_leaderboard_fixtures(
    *,
    profile: Any,
    models_module: Any,
    guild_id: int,
) -> LeaderboardFixtureState:
    """Idempotently seed a multi-page player leaderboard showcase."""

    validate_profile(profile)
    guild_id = validate_guild_id(profile, guild_id)
    with models_module.db.connection_context():
        validate_live_identity(*_live_identity(models_module))
        with models_module.db.atomic():
            existing_players = _find_leaderboard_players(
                models_module,
                guild_id,
            )
            existing_games = _find_leaderboard_games(
                models_module,
                guild_id,
            )
            if existing_players or existing_games:
                if (
                    len(existing_players) != LEADERBOARD_FIXTURE_COUNT
                    or len(existing_games)
                    != (LEADERBOARD_FIXTURE_COUNT // 2) * 4
                    or any(
                        not is_owned_leaderboard_player(player, guild_id)
                        for player in existing_players
                    )
                    or any(
                        not is_owned_leaderboard_game(game, guild_id)
                        for game in existing_games
                    )
                ):
                    raise FixtureValidationError(
                        'Existing LB2 showcase fixtures are incomplete or '
                        'fail ownership validation. Inspect status and clean '
                        'them before reseeding.'
                    )
                return _leaderboard_state_from_open_connection(
                    models_module,
                    guild_id,
                )

            players = []
            for index in range(1, LEADERBOARD_FIXTURE_COUNT + 1):
                name = _leaderboard_name(index)
                discord_member = models_module.DiscordMember.create(
                    discord_id=_leaderboard_discord_id(index),
                    name=name,
                    polytopia_name=name,
                )
                players.append(models_module.Player.create(
                    discord_member=discord_member,
                    guild_id=guild_id,
                    nick=name,
                    name=name,
                ))

            for game_number, pair in enumerate(
                _round_robin_pairs(players),
                start=1,
            ):
                _create_leaderboard_game(
                    models_module,
                    guild_id,
                    pair[0],
                    pair[1],
                    game_number,
                )
            _set_leaderboard_ratings(players)

        return _leaderboard_state_from_open_connection(
            models_module,
            guild_id,
        )


def cleanup_leaderboard_fixtures(
    *,
    profile: Any,
    models_module: Any,
    guild_id: int,
    confirmed: bool,
) -> LeaderboardFixtureState:
    """Delete only fully validated Components v2 showcase fixtures."""

    validate_profile(profile)
    guild_id = validate_guild_id(profile, guild_id)
    if not confirmed:
        raise FixtureValidationError(
            'Leaderboard cleanup requires the explicit --confirm option.'
        )

    with models_module.db.connection_context():
        validate_live_identity(*_live_identity(models_module))
        with models_module.db.atomic():
            players = _find_leaderboard_players(models_module, guild_id)
            games = _find_leaderboard_games(models_module, guild_id)
            if any(
                not is_owned_leaderboard_player(player, guild_id)
                for player in players
            ):
                raise FixtureSafetyError(
                    'Leaderboard cleanup found a player that failed the '
                    'ownership check.'
                )
            if any(
                not is_owned_leaderboard_game(game, guild_id)
                for game in games
            ):
                raise FixtureSafetyError(
                    'Leaderboard cleanup found a game that failed the '
                    'ownership check.'
                )
            for game in sorted(
                games,
                key=lambda item: (
                    item.completed_ts or datetime.datetime.min,
                    item.id,
                ),
                reverse=True,
            ):
                game.delete_game()
            for player in players:
                discord_member = player.discord_member
                player.delete_instance()
                if not discord_member.guildmembers.exists():
                    discord_member.delete_instance()

        return _leaderboard_state_from_open_connection(
            models_module,
            guild_id,
        )
