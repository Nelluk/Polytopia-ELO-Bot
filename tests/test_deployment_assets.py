"""Offline checks for current deployment and backup assets."""

from pathlib import Path
import subprocess
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

    def test_backup_script_is_syntactically_valid_and_atomic(self):
        script = self.root / 'scripts/backup_db.sh'
        result = subprocess.run(
            ['/usr/bin/bash', '-n', script],
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        source = script.read_text(encoding='utf-8')
        self.assertIn('/usr/bin/flock -n 9', source)
        self.assertIn('/usr/bin/mktemp', source)
        self.assertIn('/usr/bin/pg_restore --list', source)
        self.assertIn('/usr/bin/gzip -t', source)
        self.assertIn('/usr/bin/tar -tzf', source)
        self.assertIn('/usr/bin/mv -f --', source)
        self.assertIn(
            'REPORTEXPORTER=${POLYBOT_REPORT_EXPORTER:-$PROJECT_ROOT/scripts/export_reporting_duckdb.py}',
            source,
        )
        self.assertIn('--output "$REPORTTARGET"', source)
        self.assertIn(
            'REPORTLOCK=${POLYBOT_REPORT_LOCK:-$STATE_ROOT/.polybot-reporting.lock}',
            source,
        )
        self.assertIn('--lock-file "$REPORTLOCK"', source)
        self.assertIn(
            'Core backup successful, but reporting export failed.',
            source,
        )
        self.assertIn('REPORTING_PARTIAL_EXIT=20', source)
        self.assertIn('LOCK_BUSY_EXIT=75', source)
        self.assertIn('exit "$REPORTING_PARTIAL_EXIT"', source)
        self.assertIn('exit "$LOCK_BUSY_EXIT"', source)
        self.assertGreater(
            source.index('"$REPORTPYTHON" "$REPORTEXPORTER"'),
            source.index(
                '/usr/bin/mv -f -- "$IMAGETARGET_TMP" "$IMAGETARGET"'
            ),
        )
        self.assertNotIn('> "$TARGET"', source)
        self.assertNotIn('/home/nelluk', source)
        self.assertIn('DATABASE_USER=${POLYBOT_DATABASE_USER:-}', source)
        self.assertIn('DATABASE_USER_ARGS=(--username "$DATABASE_USER")', source)

        wrapper = (self.root / 'deploy/polyelo-backup').read_text(
            encoding='utf-8'
        )
        self.assertIn('POLYBOT_ROOT=/srv/polyelo/PolyBot39', wrapper)
        self.assertIn('POLYBOT_BACKUP_DIR=/srv/polyelo/backups', wrapper)
        self.assertIn(
            'exec /srv/polyelo/PolyBot39/scripts/backup_db.sh',
            wrapper,
        )
        self.assertNotIn('/home/nelluk', wrapper)


if __name__ == '__main__':
    unittest.main()
