"""Offline tests for the development beta fixture safety boundary."""

from contextlib import contextmanager
from contextlib import redirect_stderr
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
