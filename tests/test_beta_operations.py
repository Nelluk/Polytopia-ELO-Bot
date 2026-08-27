"""Focused offline coverage for the direct-Compose development runtime."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from modules import beta_operations


CHECKPOINT = 'a' * 40


def profile(root: Path, **overrides):
    values = {
        'environment': 'development',
        'project_root': root,
        'log_root': root / 'logs' / 'development',
        'expected_bot_id': beta_operations.BETA_APPLICATION_ID,
        'allowed_guild_ids': (beta_operations.BETA_GUILD_ID,),
        'database_name': beta_operations.BETA_DATABASE_NAME,
        'database_user': beta_operations.BETA_DATABASE_ROLE,
        'background_tasks_enabled': False,
        'api_enabled': False,
        'bullet_enabled': False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def service_environment():
    return {
        'POLYBOT_ENV': 'development',
        'POLYBOT_RESTART_SUPERVISOR': 'compose',
        'POLYBOT_BETA_STARTUP_SYNC': 'disabled',
        'POLYBOT_BETA_CHECKPOINT': CHECKPOINT,
        'POLYBOT_IMAGE_CHECKPOINT': CHECKPOINT,
        'POLYBOT_BETA_APPLICATION_ID': str(beta_operations.BETA_APPLICATION_ID),
        'POLYBOT_BETA_GUILD_ID': str(beta_operations.BETA_GUILD_ID),
        'POLYBOT_BETA_DATABASE': beta_operations.BETA_DATABASE_NAME,
        'POLYBOT_BETA_DATABASE_ROLE': beta_operations.BETA_DATABASE_ROLE,
    }


class DirectComposeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / 'bot.py').write_text('', encoding='utf-8')

    def tearDown(self):
        self.temporary.cleanup()

    def test_fixed_development_profile_is_required(self):
        beta_operations.assert_beta_profile(
            profile(self.root),
            environ=service_environment(),
            require_service_environment=True,
        )
        for field, value in (
            ('environment', 'production'),
            ('expected_bot_id', 1),
            ('allowed_guild_ids', (1,)),
            ('database_name', 'polytopia2'),
            ('database_user', 'polyelo'),
            ('background_tasks_enabled', True),
        ):
            with self.subTest(field=field), self.assertRaises(
                beta_operations.BetaRuntimeInvariantError
            ):
                beta_operations.assert_beta_profile(
                    profile(self.root, **{field: value})
                )

    def test_compose_launch_requires_exact_embedded_checkpoint(self):
        checkpoint_file = self.root / 'image-checkpoint'
        checkpoint_file.write_text(CHECKPOINT + '\n', encoding='ascii')
        with mock.patch.object(
            beta_operations,
            'COMPOSE_IMAGE_CHECKPOINT_FILE',
            checkpoint_file,
        ):
            self.assertEqual(
                beta_operations.validate_beta_launch(
                    profile(self.root),
                    ['--skip_tasks'],
                    environ=service_environment(),
                ),
                CHECKPOINT,
            )
            wrong = {**service_environment(), 'POLYBOT_IMAGE_CHECKPOINT': 'b' * 40}
            with self.assertRaises(beta_operations.BetaRuntimeInvariantError):
                beta_operations.validate_beta_launch(
                    profile(self.root),
                    ['--skip_tasks'],
                    environ=wrong,
                )

    def test_only_direct_compose_and_skip_tasks_are_supported(self):
        checkpoint_file = self.root / 'image-checkpoint'
        checkpoint_file.write_text(CHECKPOINT + '\n', encoding='ascii')
        with mock.patch.object(
            beta_operations,
            'COMPOSE_IMAGE_CHECKPOINT_FILE',
            checkpoint_file,
        ):
            with self.assertRaises(beta_operations.BetaRuntimeInvariantError):
                beta_operations.validate_beta_launch(
                    profile(self.root),
                    [],
                    environ=service_environment(),
                )
            native = {
                **service_environment(),
                'POLYBOT_RESTART_SUPERVISOR': 'systemd',
            }
            with self.assertRaises(beta_operations.BetaRuntimeInvariantError):
                beta_operations.validate_beta_launch(
                    profile(self.root),
                    ['--skip_tasks'],
                    environ=native,
                )

    def test_process_writer_lock_blocks_a_second_holder(self):
        paths = beta_operations.operation_paths(profile(self.root), create=True)
        first = beta_operations.BetaWriterLock(paths.writer_lock)
        second = beta_operations.BetaWriterLock(paths.writer_lock)
        first.acquire()
        try:
            with self.assertRaises(beta_operations.BetaRuntimeInvariantError):
                second.acquire()
        finally:
            first.release()
        second.acquire()
        second.release()

    def test_retired_control_and_fixture_surfaces_are_absent(self):
        source = Path(beta_operations.__file__).read_text(encoding='utf-8')
        self.assertNotIn('BetaReleaseControl', source)
        self.assertNotIn('send_control_request', source)
        self.assertNotIn('beta-lab', source.lower())


if __name__ == '__main__':
    unittest.main()
