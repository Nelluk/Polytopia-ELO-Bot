import asyncio
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import FrozenInstanceError
import importlib
from io import StringIO
from pathlib import Path
import stat
from types import ModuleType, SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock
import warnings

from runtime_config import (
    LEGACY_PRODUCTION_BOT_ID,
    RuntimeConfigurationError,
    format_runtime_profile,
    load_runtime_profile,
)
from scripts import check_runtime_config


DEVELOPMENT_GUILD_ID = 900000000000000001
OTHER_GUILD_ID = 900000000000000002


@contextmanager
def import_bot_with_models_stub(models_stub):
    settings_stub = ModuleType('settings')
    settings_stub.runtime_profile = SimpleNamespace(
        background_tasks_enabled=False,
        discord_token='offline-token',
    )
    settings_stub.run_tasks = False
    stubs = {
        'logging_config': ModuleType('logging_config'),
        'settings': settings_stub,
        'modules.image_storage': ModuleType('modules.image_storage'),
        'modules.initialize_data': ModuleType('modules.initialize_data'),
        'modules.models': models_stub,
        'modules.utilities': ModuleType('modules.utilities'),
    }
    old_bot_module = sys.modules.pop('bot', None)
    modules_package = importlib.import_module('modules')
    try:
        with mock.patch.dict(sys.modules, stubs), mock.patch.object(
            modules_package,
            'models',
            models_stub,
            create=True,
        ), mock.patch.object(
            modules_package,
            'image_storage',
            stubs['modules.image_storage'],
            create=True,
        ), mock.patch.object(
            modules_package,
            'initialize_data',
            stubs['modules.initialize_data'],
            create=True,
        ), mock.patch.object(
            modules_package,
            'utilities',
            stubs['modules.utilities'],
            create=True,
        ):
            yield importlib.import_module('bot')
    finally:
        sys.modules.pop('bot', None)
        if old_bot_module is not None:
            sys.modules['bot'] = old_bot_module


class RuntimeProfileTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def write_config(self, environment, **overrides):
        values = {
            'discord_key': 'development-secret-token',
            'expected_bot_id': '900000000000000010',
            'owner_id': '900000000000000011',
            'psql_db': 'polytopia_dev',
            'psql_user': 'polybot_dev',
            'psql_password': 'development-secret-password',
            'psql_host': 'localhost',
            'psql_port': '5432',
            'production_database_name': 'polytopia2',
            'production_bot_id': str(LEGACY_PRODUCTION_BOT_ID),
            'production_guild_ids': (
                '283436219780825088,447883341463814144'
            ),
            'shared_production_guild_ids': '',
            'acknowledge_shared_production_guild_risk': 'false',
            'background_tasks_enabled': 'false',
            'api_enabled': 'false',
            'bullet_enabled': 'false',
        }
        values.update(overrides)
        filename = (
            'config.ini' if environment == 'production'
            else 'config.development.ini'
        )
        lines = ['[DEFAULT]']
        for key, value in values.items():
            if value is not None:
                lines.append(f'{key} = {value}')
        (self.root / filename).write_text(
            '\n'.join(lines) + '\n', encoding='utf-8'
        )

    def write_server_settings(
            self, environment, guild_id=DEVELOPMENT_GUILD_ID):
        module_name = (
            'server_settings' if environment == 'production'
            else 'server_settings_dev'
        )
        source = (
            f"server_shortcut_ids = {{'main': {guild_id}, "
            f"'polychampions': {guild_id}, 'test': {guild_id}}}\n"
            f"server_list = {{'default': {{}}, {guild_id}: {{}}}}\n"
        )
        (self.root / f'{module_name}.py').write_text(
            source, encoding='utf-8'
        )

    def load_development(self, *, create_directories=False):
        return load_runtime_profile(
            project_root=self.root,
            environ={'POLYBOT_ENV': 'development'},
            create_directories=create_directories,
        )

    def test_unset_environment_selects_production_and_preserves_paths(self):
        self.write_config(
            'production',
            discord_key='production-token',
            expected_bot_id=str(LEGACY_PRODUCTION_BOT_ID),
            psql_db='polytopia',
            psql_user='nelluk',
            psql_password='production-password',
            psql_host='',
            psql_port='',
            background_tasks_enabled='true',
            api_enabled='true',
            bullet_enabled='true',
        )
        self.write_server_settings('production')

        profile = load_runtime_profile(
            project_root=self.root, environ={}, create_directories=True
        )

        self.assertEqual(profile.environment, 'production')
        self.assertEqual(profile.image_root, self.root / 'data/images')
        self.assertEqual(profile.log_root, self.root / 'logs')
        self.assertTrue(profile.background_tasks_enabled)
        self.assertTrue(profile.api_enabled)
        self.assertTrue(profile.bullet_enabled)
        self.assertFalse(profile.image_root.exists())
        self.assertFalse(profile.log_root.exists())
        with self.assertRaises(FrozenInstanceError):
            profile.environment = 'development'

    def test_development_selection_creates_only_isolated_paths(self):
        self.write_config('development')
        self.write_server_settings('development')

        profile = self.load_development(create_directories=True)

        self.assertEqual(profile.environment, 'development')
        self.assertEqual(
            profile.image_root, self.root / 'data/development/images'
        )
        self.assertEqual(profile.log_root, self.root / 'logs/development')
        self.assertFalse(profile.background_tasks_enabled)
        self.assertFalse(profile.api_enabled)
        self.assertFalse(profile.bullet_enabled)
        self.assertTrue(profile.image_root.is_dir())
        self.assertTrue(profile.log_root.is_dir())
        self.assertEqual(
            stat.S_IMODE(profile.image_root.stat().st_mode), 0o750
        )
        self.assertEqual(
            stat.S_IMODE(profile.log_root.stat().st_mode), 0o750
        )
        self.assertFalse((self.root / 'data/images').exists())

    def test_unknown_environment_is_rejected(self):
        with self.assertRaisesRegex(
                RuntimeConfigurationError, 'must be exactly'):
            load_runtime_profile(
                project_root=self.root,
                environ={'POLYBOT_ENV': 'staging'},
                create_directories=False,
            )

    def test_missing_required_development_value_is_rejected(self):
        self.write_config('development', psql_password=None)
        self.write_server_settings('development')

        with self.assertRaisesRegex(
                RuntimeConfigurationError, 'psql_password'):
            self.load_development()

    def test_missing_production_resource_denylist_is_rejected(self):
        self.write_config('development', production_guild_ids=None)
        self.write_server_settings('development')

        with self.assertRaisesRegex(
                RuntimeConfigurationError, 'production_guild_ids'):
            self.load_development()

    def test_redacted_inspection_output_contains_no_secrets(self):
        self.write_config('development')
        self.write_server_settings('development')
        profile = self.load_development()

        output = StringIO()
        with redirect_stdout(output):
            result = check_runtime_config.main(profile)
        summary = output.getvalue()

        self.assertEqual(result, 0)
        self.assertEqual(summary.strip(), format_runtime_profile(profile))
        self.assertIn('polytopia_dev', summary)
        self.assertIn(str(DEVELOPMENT_GUILD_ID), summary)
        self.assertNotIn('development-secret-token', summary)
        self.assertNotIn('development-secret-password', summary)
        self.assertNotIn('database user', summary.lower())
        self.assertNotIn('development-secret-token', repr(profile))
        self.assertNotIn('development-secret-password', repr(profile))

    def test_development_database_requires_clear_marker(self):
        self.write_config('development', psql_db='polytopia')
        self.write_server_settings('development')

        with self.assertRaisesRegex(
                RuntimeConfigurationError, 'clear dev'):
            self.load_development()

    def test_configured_production_database_and_token_are_rejected(self):
        self.write_config(
            'production',
            discord_key='shared-token',
            psql_db='shared_dev',
        )
        self.write_server_settings('production', OTHER_GUILD_ID)
        self.write_config(
            'development',
            discord_key='shared-token',
            psql_db='separate_dev',
        )
        self.write_server_settings('development')
        with self.assertRaisesRegex(
                RuntimeConfigurationError, 'production token'):
            self.load_development()

        self.write_config(
            'development',
            discord_key='different-token',
            psql_db='shared_dev',
        )
        with self.assertRaisesRegex(
                RuntimeConfigurationError, 'production database'):
            self.load_development()

    def test_production_bot_id_is_rejected_in_development(self):
        self.write_config(
            'development',
            expected_bot_id=str(LEGACY_PRODUCTION_BOT_ID),
        )
        self.write_server_settings('development')

        with self.assertRaisesRegex(
                RuntimeConfigurationError, 'production bot ID'):
            self.load_development()

    def test_production_guild_is_rejected_in_development(self):
        self.write_config('development')
        self.write_server_settings(
            'development', guild_id=283436219780825088
        )

        with self.assertRaisesRegex(
                RuntimeConfigurationError, 'production guild IDs'):
            self.load_development()

    def test_exact_shared_production_guild_requires_acknowledgement(self):
        self.write_server_settings(
            'development', guild_id=283436219780825088
        )
        self.write_config(
            'development',
            shared_production_guild_ids='283436219780825088',
        )
        with self.assertRaisesRegex(
                RuntimeConfigurationError,
                'acknowledge_shared_production_guild_risk'):
            self.load_development()

        self.write_config(
            'development',
            shared_production_guild_ids='283436219780825088',
            acknowledge_shared_production_guild_risk='true',
        )
        profile = self.load_development()
        self.assertEqual(
            profile.shared_production_guild_ids,
            (283436219780825088,),
        )
        self.assertIn(
            'acknowledged shared production guild IDs: '
            '283436219780825088',
            format_runtime_profile(profile),
        )

    def test_shared_guild_exception_cannot_allow_unrelated_id(self):
        self.write_config(
            'development',
            shared_production_guild_ids=str(DEVELOPMENT_GUILD_ID),
            acknowledge_shared_production_guild_risk='true',
        )
        self.write_server_settings('development')

        with self.assertRaisesRegex(
                RuntimeConfigurationError, 'production denylist'):
            self.load_development()

    def test_production_paths_are_rejected_in_development(self):
        for key, value, expected in (
                ('image_root', 'data/images', 'production image path'),
                ('log_root', 'logs', 'production log path')):
            with self.subTest(key=key):
                self.write_config('development', **{key: value})
                self.write_server_settings('development')
                with self.assertRaisesRegex(
                        RuntimeConfigurationError, expected):
                    self.load_development()

    def test_development_policy_enablement_requires_acknowledgement(self):
        self.write_server_settings('development')
        for policy, acknowledgement in (
                (
                    'background_tasks_enabled',
                    'allow_development_background_tasks',
                ),
                ('api_enabled', 'allow_development_api')):
            with self.subTest(policy=policy):
                self.write_config('development', **{policy: 'true'})
                with self.assertRaisesRegex(
                        RuntimeConfigurationError, acknowledgement):
                    self.load_development()

    def test_expected_bot_id_validation(self):
        self.write_config('development')
        self.write_server_settings('development')
        profile = self.load_development()

        profile.validate_logged_in_bot(profile.expected_bot_id)
        with self.assertRaisesRegex(
                RuntimeConfigurationError, 'Discord application mismatch'):
            profile.validate_logged_in_bot(profile.expected_bot_id + 1)

    def test_modules_api_uses_profile_and_refuses_disabled_api(self):
        self.write_config('development')
        self.write_server_settings('development')
        profile = self.load_development()

        model_stubs = ModuleType('modules.models')
        model_stubs.ApiApplication = type('ApiApplication', (), {})
        model_stubs.DiscordMember = type('DiscordMember', (), {})
        model_stubs.Game = type('Game', (), {})
        settings_stub = ModuleType('settings')
        settings_stub.runtime_profile = profile
        old_api_module = sys.modules.pop('modules.api', None)
        try:
            with mock.patch.dict(
                    sys.modules,
                    {'modules.models': model_stubs, 'settings': settings_stub}):
                import importlib
                # The FastAPI lifespan migration belongs to the later
                # dependency-upgrade phase documented in the handoff.
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', DeprecationWarning)
                    api = importlib.import_module('modules.api')

            self.assertIs(api.runtime_profile, profile)
            self.assertFalse(hasattr(api, 'config'))

            async def run_lifespan():
                async with api.lifespan(api.server):
                    pass

            with self.assertRaisesRegex(
                    RuntimeConfigurationError, 'HTTP API is disabled'):
                asyncio.run(run_lifespan())

            enabled_profile = SimpleNamespace(
                api_enabled=True,
                environment='development',
                discord_token='central-profile-token',
            )
            api.runtime_profile = enabled_profile
            events = []

            class FakeClient:
                async def start(self, token):
                    events.append(('start', token))
                    await asyncio.Event().wait()

                async def close(self):
                    events.append(('close', None))

            with mock.patch.object(
                    api, 'create_discord_client',
                    return_value=FakeClient()):
                async def exercise_enabled_lifespan():
                    async with api.lifespan(api.server):
                        await asyncio.sleep(0)
                        self.assertIsNotNone(api.client_task)
                    self.assertIsNone(api.client)
                    self.assertIsNone(api.client_task)

                asyncio.run(exercise_enabled_lifespan())

            self.assertEqual(
                events,
                [
                    ('start', 'central-profile-token'),
                    ('close', None),
                ],
            )
        finally:
            sys.modules.pop('modules.api', None)
            if old_api_module is not None:
                sys.modules['modules.api'] = old_api_module

    def test_skip_tasks_always_forces_tasks_off(self):
        settings_stub = ModuleType('settings')
        settings_stub.runtime_profile = SimpleNamespace(
            background_tasks_enabled=True,
            discord_token='offline-token',
        )
        settings_stub.run_tasks = True
        settings_stub.owner_id = 1
        settings_stub.config = {}
        stubs = {
            'logging_config': ModuleType('logging_config'),
            'settings': settings_stub,
            'modules.image_storage': ModuleType('modules.image_storage'),
            'modules.initialize_data': ModuleType('modules.initialize_data'),
            'modules.models': ModuleType('modules.models'),
            'modules.utilities': ModuleType('modules.utilities'),
        }
        old_bot_module = sys.modules.pop('bot', None)
        try:
            with mock.patch.dict(sys.modules, stubs):
                import importlib
                bot_module = importlib.import_module('bot')
                parsed = bot_module.configure_runtime_arguments(
                    ['--skip_tasks']
                )

            self.assertTrue(parsed.skip_tasks)
            self.assertFalse(settings_stub.run_tasks)
        finally:
            sys.modules.pop('bot', None)
            if old_bot_module is not None:
                sys.modules['bot'] = old_bot_module

    def test_full_elo_recalculation_owns_cli_database_connection(self):
        events = []

        class ConnectionContext:
            def __enter__(self):
                events.append('connection-open')

            def __exit__(self, exc_type, exc_value, traceback):
                events.append('connection-close')

        models_stub = ModuleType('modules.models')
        models_stub.db = SimpleNamespace(
            connection_context=lambda: ConnectionContext()
        )
        models_stub.Game = SimpleNamespace(
            recalculate_all_elo=lambda: events.append('recalculate')
        )
        with import_bot_with_models_stub(models_stub) as bot_module:
            with self.assertRaises(SystemExit) as raised:
                bot_module.main(['--recalc_elo'])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(
            events,
            ['connection-open', 'recalculate', 'connection-close'],
        )

    def test_full_elo_recalculation_closes_connection_after_failure(self):
        events = []

        class ConnectionContext:
            def __enter__(self):
                events.append('connection-open')

            def __exit__(self, exc_type, exc_value, traceback):
                events.append('connection-close')

        def fail_recalculation():
            events.append('recalculate')
            raise RuntimeError('simulated recalculation failure')

        models_stub = ModuleType('modules.models')
        models_stub.db = SimpleNamespace(
            connection_context=lambda: ConnectionContext()
        )
        models_stub.Game = SimpleNamespace(
            recalculate_all_elo=fail_recalculation
        )
        with import_bot_with_models_stub(models_stub) as bot_module:
            with self.assertRaisesRegex(
                RuntimeError,
                'simulated recalculation failure',
            ):
                bot_module.main(['--recalc_elo'])

        self.assertEqual(
            events,
            ['connection-open', 'recalculate', 'connection-close'],
        )


class InspectionCommandFailureTests(unittest.TestCase):
    def test_inspection_command_reports_configuration_error(self):
        stderr = StringIO()
        with mock.patch(
                'scripts.check_runtime_config.get_runtime_profile',
                side_effect=RuntimeConfigurationError('safe failure')):
            with redirect_stderr(stderr):
                result = check_runtime_config.main()

        self.assertEqual(result, 2)
        self.assertIn('safe failure', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
