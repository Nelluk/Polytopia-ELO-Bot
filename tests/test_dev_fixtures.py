"""Offline tests for the development beta fixture safety boundary."""

from contextlib import contextmanager
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from modules import dev_fixtures
from scripts import manage_dev_fixtures


def profile(**overrides):
    values = {
        'environment': 'development',
        'database_name': 'polytopia_dev',
        'database_user': 'polybot_dev',
        'background_tasks_enabled': False,
        'api_enabled': False,
        'allowed_guild_ids': (1234,),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class RecordingDatabase:
    def __init__(self):
        self.events = []

    @contextmanager
    def connection_context(self):
        self.events.append('connect')
        try:
            yield
        finally:
            self.events.append('close')

    @contextmanager
    def atomic(self):
        self.events.append('begin')
        try:
            yield
        except Exception:
            self.events.append('rollback')
            raise
        else:
            self.events.append('commit')


class DevelopmentFixtureSafetyTests(unittest.TestCase):
    def test_scenario_metadata_supports_trade_price_fallback(self):
        class FakeGame:
            league_season = object()
            league_tier = object()

            def __init__(self):
                self.league_season = None
                self.league_tier = None
                self.save = mock.Mock()

        completed = FakeGame()
        dev_fixtures._apply_scenario_metadata(completed, 'completed')
        self.assertEqual(
            completed.league_season,
            dev_fixtures.FIXTURE_COMPLETED_LEAGUE_SEASON,
        )
        self.assertEqual(
            completed.league_tier, dev_fixtures.FIXTURE_LEAGUE_TIER
        )
        completed.save.assert_called_once()

        unconfirmed = FakeGame()
        dev_fixtures._apply_scenario_metadata(unconfirmed, 'unconfirmed')
        self.assertEqual(
            unconfirmed.league_season,
            dev_fixtures.FIXTURE_CURRENT_LEAGUE_SEASON,
        )
        self.assertEqual(
            unconfirmed.league_tier, dev_fixtures.FIXTURE_LEAGUE_TIER
        )

    def test_leaderboard_fixture_ownership_requires_id_name_and_guild(self):
        index = 4
        name = dev_fixtures._leaderboard_name(index)
        player = SimpleNamespace(
            guild_id=1234,
            name=name,
            discord_member=SimpleNamespace(
                discord_id=dev_fixtures._leaderboard_discord_id(index),
                name=name,
            ),
        )
        self.assertTrue(
            dev_fixtures.is_owned_leaderboard_player(player, 1234)
        )
        player.name = 'Real Player'
        self.assertFalse(
            dev_fixtures.is_owned_leaderboard_player(player, 1234)
        )
        game = SimpleNamespace(
            guild_id=1234,
            notes=dev_fixtures.LEADERBOARD_GAME_MARKER,
            name=dev_fixtures._leaderboard_game_name(1),
        )
        self.assertTrue(
            dev_fixtures.is_owned_leaderboard_game(game, 1234)
        )
        game.name = 'Lb2 Showcase Game 99'
        self.assertFalse(
            dev_fixtures.is_owned_leaderboard_game(game, 1234)
        )

    def test_leaderboard_pairings_produce_four_games_per_player(self):
        players = tuple(range(dev_fixtures.LEADERBOARD_FIXTURE_COUNT))
        pairs = dev_fixtures._round_robin_pairs(players)
        self.assertEqual(len(pairs), 48)
        appearances = {
            player: sum(player in pair for pair in pairs)
            for player in players
        }
        self.assertEqual(set(appearances.values()), {4})

    def test_leaderboard_cleanup_requires_confirmation_before_database(self):
        models = SimpleNamespace(db=RecordingDatabase())
        with self.assertRaises(dev_fixtures.FixtureValidationError):
            dev_fixtures.cleanup_leaderboard_fixtures(
                profile=profile(),
                models_module=models,
                guild_id=1234,
                confirmed=False,
            )
        self.assertEqual(models.db.events, [])

    def test_status_output_includes_pending_state_and_expiration(self):
        state = dev_fixtures.FixtureState(
            guild_id=1234,
            user_ids=(1, 2),
            games=(
                dev_fixtures.FixtureGame(
                    scenario='ready',
                    game_id=42,
                    name='Beta Fixture Ready',
                    is_completed=False,
                    is_confirmed=False,
                    is_ranked=True,
                    is_pending=True,
                    expiration='2026-07-30T12:00:00',
                    league_season=4,
                    league_tier=1,
                ),
            ),
        )
        output = StringIO()
        with redirect_stdout(output):
            manage_dev_fixtures._print_state(state)

        self.assertIn('pending=True', output.getvalue())
        self.assertIn('season=4', output.getvalue())
        self.assertIn('tier=1', output.getvalue())
        self.assertIn('expiration=2026-07-30T12:00:00', output.getvalue())

    def test_cli_refuses_unsafe_profile_before_importing_models(self):
        stderr = StringIO()
        with (
            mock.patch.object(
                manage_dev_fixtures,
                'get_runtime_profile',
                return_value=profile(environment='production'),
            ),
            mock.patch.dict('sys.modules', {'modules.models': None}),
            redirect_stderr(stderr),
        ):
            result = manage_dev_fixtures.main(['status'])
        self.assertEqual(result, 2)
        self.assertIn('Fixture operation refused', stderr.getvalue())

    def test_profile_gate_requires_exact_environment_database_and_role(self):
        dev_fixtures.validate_profile(profile())
        unsafe_profiles = (
            profile(environment='production'),
            profile(database_name='polytopia2'),
            profile(database_user='postgres'),
            profile(background_tasks_enabled=True),
            profile(api_enabled=True),
        )
        for unsafe_profile in unsafe_profiles:
            with self.subTest(unsafe_profile=unsafe_profile):
                with self.assertRaises(dev_fixtures.FixtureSafetyError):
                    dev_fixtures.validate_profile(unsafe_profile)

    def test_live_identity_requires_exact_database_and_role(self):
        dev_fixtures.validate_live_identity('polytopia_dev', 'polybot_dev')
        for database_name, role in (
            ('polytopia2', 'polybot_dev'),
            ('polytopia_dev', 'postgres'),
        ):
            with self.assertRaises(dev_fixtures.FixtureSafetyError):
                dev_fixtures.validate_live_identity(database_name, role)

    def test_user_ids_must_be_unique_positive_and_even_bounded(self):
        self.assertEqual(
            dev_fixtures.validate_user_ids([4, 3, 2, 1]),
            (4, 3, 2, 1),
        )
        for values in ([1], [1, 2, 3], [1, 1], [0, 2], range(10)):
            with self.subTest(values=values):
                with self.assertRaises(dev_fixtures.FixtureValidationError):
                    dev_fixtures.validate_user_ids(values)

    def test_owned_game_requires_guild_marker_and_name(self):
        owned = SimpleNamespace(
            guild_id=1234,
            notes=dev_fixtures.FIXTURE_NOTES_MARKER,
            name='Beta Fixture Ready',
        )
        self.assertTrue(dev_fixtures.is_owned_game(owned, 1234))
        self.assertFalse(dev_fixtures.is_owned_game(owned, 9999))
        self.assertFalse(dev_fixtures.is_owned_game(
            SimpleNamespace(
                guild_id=1234,
                notes='ordinary notes',
                name='Beta Fixture Ready',
            ),
            1234,
        ))
        self.assertFalse(dev_fixtures.is_owned_game(
            SimpleNamespace(
                guild_id=1234,
                notes=dev_fixtures.FIXTURE_NOTES_MARKER,
                name='Ordinary Game',
            ),
            1234,
        ))

    def test_cleanup_requires_explicit_confirmation_before_database_use(self):
        models = SimpleNamespace(db=RecordingDatabase())
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(dev_fixtures.FixtureValidationError):
                dev_fixtures.cleanup_fixtures(
                    profile=profile(),
                    models_module=models,
                    guild_id=1234,
                    manifest_path=Path(directory) / 'manifest.json',
                    confirmed=False,
                )
        self.assertEqual(models.db.events, [])

    def test_seed_rolls_back_and_does_not_write_manifest_on_failure(self):
        database = RecordingDatabase()
        models = SimpleNamespace(db=database)
        manifest_path = Path('/tmp/manifest-must-not-be-written.json')
        with (
            mock.patch.object(
                dev_fixtures,
                '_live_identity',
                return_value=('polytopia_dev', 'polybot_dev'),
            ),
            mock.patch.object(
                dev_fixtures,
                '_find_fixture_games',
                return_value=(),
            ),
            mock.patch.object(
                dev_fixtures,
                '_load_players',
                return_value=('one', 'two'),
            ),
            mock.patch.object(
                dev_fixtures,
                '_create_scenario',
                side_effect=RuntimeError('injected seed failure'),
            ),
            mock.patch.object(dev_fixtures, '_write_manifest') as write_manifest,
        ):
            with self.assertRaisesRegex(RuntimeError, 'injected seed failure'):
                dev_fixtures.seed_fixtures(
                    profile=profile(),
                    models_module=models,
                    guild_id=1234,
                    user_ids=(1, 2),
                    manifest_path=manifest_path,
                )
        self.assertEqual(
            database.events,
            ['connect', 'begin', 'rollback', 'close'],
        )
        write_manifest.assert_not_called()

    def test_in_process_prepare_audit_failure_rolls_back_complete_bundle(self):
        database = RecordingDatabase()
        game_log = SimpleNamespace(write=mock.Mock(
            side_effect=RuntimeError('injected audit failure')
        ))
        models = SimpleNamespace(db=database, GameLog=game_log)
        with (
            mock.patch.object(
                dev_fixtures,
                '_live_identity',
                return_value=('polytopia_dev', 'polybot_dev'),
            ),
            mock.patch.object(dev_fixtures, '_find_fixture_games', return_value=()),
            mock.patch.object(
                dev_fixtures, '_load_players', return_value=('one', 'two')
            ),
            mock.patch.object(dev_fixtures, '_create_scenario') as create,
        ):
            with self.assertRaisesRegex(RuntimeError, 'audit failure'):
                dev_fixtures.prepare_fixtures_in_process(
                    profile=profile(),
                    models_module=models,
                    guild_id=1234,
                    user_ids=(1, 2),
                    audit_message='owner prepared fixtures',
                )
        self.assertEqual(create.call_count, 3)
        self.assertEqual(
            database.events,
            ['connect', 'begin', 'rollback', 'close'],
        )

    def test_in_process_prepare_snapshot_failure_rolls_back_before_result(self):
        database = RecordingDatabase()
        models = SimpleNamespace(
            db=database,
            GameLog=SimpleNamespace(write=mock.Mock()),
        )
        with (
            mock.patch.object(
                dev_fixtures,
                '_live_identity',
                return_value=('polytopia_dev', 'polybot_dev'),
            ),
            mock.patch.object(dev_fixtures, '_find_fixture_games', return_value=()),
            mock.patch.object(
                dev_fixtures, '_load_players', return_value=('one', 'two')
            ),
            mock.patch.object(dev_fixtures, '_create_scenario'),
            mock.patch.object(
                dev_fixtures,
                '_state_from_open_connection',
                side_effect=RuntimeError('injected snapshot failure'),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, 'snapshot failure'):
                dev_fixtures.prepare_fixtures_in_process(
                    profile=profile(),
                    models_module=models,
                    guild_id=1234,
                    user_ids=(1, 2),
                    audit_message='owner prepared fixtures',
                )
        self.assertEqual(
            database.events,
            ['connect', 'begin', 'rollback', 'close'],
        )

    def test_in_process_reset_refuses_ambiguous_owned_rows_before_delete(self):
        database = RecordingDatabase()
        unsafe = SimpleNamespace(
            id=7,
            guild_id=1234,
            notes=dev_fixtures.FIXTURE_NOTES_MARKER,
            name='Beta Fixture Unknown',
            completed_ts=None,
            delete_game=mock.Mock(),
        )
        expected = dev_fixtures.FixtureState(1234, (1, 2), ())
        models = SimpleNamespace(db=database, GameLog=SimpleNamespace(write=mock.Mock()))
        with (
            mock.patch.object(
                dev_fixtures,
                '_live_identity',
                return_value=('polytopia_dev', 'polybot_dev'),
            ),
            mock.patch.object(
                dev_fixtures, '_find_fixture_games', return_value=(unsafe,)
            ),
            mock.patch.object(
                dev_fixtures, '_state_from_open_connection', return_value=expected
            ),
        ):
            with self.assertRaises(dev_fixtures.FixtureValidationError):
                dev_fixtures.reset_fixtures_in_process(
                    profile=profile(),
                    models_module=models,
                    guild_id=1234,
                    user_ids=(1, 2),
                    expected_state=expected,
                    audit_message='owner reset fixtures',
                )
        unsafe.delete_game.assert_not_called()
        self.assertIn('rollback', database.events)

    def test_in_process_reset_rolls_back_when_recreation_fails(self):
        database = RecordingDatabase()
        owned = SimpleNamespace(
            id=7,
            guild_id=1234,
            notes=dev_fixtures.FIXTURE_NOTES_MARKER,
            name='Beta Fixture Ready',
            completed_ts=None,
            delete_game=mock.Mock(),
        )
        expected_game = dev_fixtures.FixtureGame(
            scenario='ready',
            game_id=7,
            name='Beta Fixture Ready',
            is_completed=False,
            is_confirmed=False,
            is_ranked=True,
            is_pending=False,
            expiration=None,
            participant_ids=(1, 2),
        )
        expected = dev_fixtures.FixtureState(1234, (1, 2), (expected_game,))
        models = SimpleNamespace(db=database, GameLog=SimpleNamespace(write=mock.Mock()))
        with (
            mock.patch.object(
                dev_fixtures,
                '_live_identity',
                return_value=('polytopia_dev', 'polybot_dev'),
            ),
            mock.patch.object(
                dev_fixtures, '_find_fixture_games', return_value=(owned,)
            ),
            mock.patch.object(
                dev_fixtures, '_state_from_open_connection', return_value=expected
            ),
            mock.patch.object(
                dev_fixtures, '_user_ids_for_games', return_value=(1, 2)
            ),
            mock.patch.object(
                dev_fixtures, '_load_players', return_value=('one', 'two')
            ),
            mock.patch.object(
                dev_fixtures,
                '_create_scenario',
                side_effect=RuntimeError('injected recreation failure'),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, 'recreation failure'):
                dev_fixtures.reset_fixtures_in_process(
                    profile=profile(),
                    models_module=models,
                    guild_id=1234,
                    user_ids=(1, 2),
                    expected_state=expected,
                    audit_message='owner reset fixtures',
                )
        owned.delete_game.assert_called_once()
        self.assertEqual(
            database.events,
            ['connect', 'begin', 'rollback', 'close'],
        )

    def test_cleanup_refuses_failed_ownership_check_and_rolls_back(self):
        database = RecordingDatabase()
        models = SimpleNamespace(db=database)
        unsafe_game = SimpleNamespace(
            id=5,
            guild_id=1234,
            notes=dev_fixtures.FIXTURE_NOTES_MARKER,
            name='Ordinary Game',
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                dev_fixtures,
                '_live_identity',
                return_value=('polytopia_dev', 'polybot_dev'),
            ),
            mock.patch.object(
                dev_fixtures,
                '_find_fixture_games',
                return_value=(unsafe_game,),
            ),
        ):
            with self.assertRaises(dev_fixtures.FixtureSafetyError):
                dev_fixtures.cleanup_fixtures(
                    profile=profile(),
                    models_module=models,
                    guild_id=1234,
                    manifest_path=Path(directory) / 'manifest.json',
                    confirmed=True,
                )
        self.assertEqual(
            database.events,
            ['connect', 'begin', 'rollback', 'close'],
        )


if __name__ == '__main__':
    unittest.main()
