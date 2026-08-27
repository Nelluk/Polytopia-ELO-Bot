"""Offline checks for current deployment assets."""

from pathlib import Path
import unittest


class DeploymentAssetTests(unittest.TestCase):
    root = Path(__file__).resolve().parents[1]

    def test_generic_schema_manager_owns_known_additive_upgrades(self):
        source = (
            self.root / 'modules/schema_management.py'
        ).read_text(encoding='utf-8')

        self.assertIn('player_timezone_migration', source)
        self.assertIn('player_badges_migration', source)
        self.assertIn('game_keep_active_migration', source)
        self.assertIn('SCHEMA_ADVISORY_LOCK_KEY', source)

if __name__ == '__main__':
    unittest.main()
