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


@dataclass(frozen=True)
class FixtureState:
    guild_id: int
    user_ids: tuple[int, ...]
    games: tuple[FixtureGame, ...]


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


def _game_view(game: Any) -> FixtureGame:
    scenario = _scenario_from_name(game.name) or 'unknown'
    return FixtureGame(
        scenario=scenario,
        game_id=int(game.id),
        name=str(game.name),
        is_completed=bool(game.is_completed),
        is_confirmed=bool(game.is_confirmed),
        is_ranked=bool(game.is_ranked),
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
