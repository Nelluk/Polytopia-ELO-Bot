from pathlib import Path
import subprocess
import unittest


class ProductionDeploymentAssetTests(unittest.TestCase):
    root = Path(__file__).resolve().parents[1]

    def test_canonical_bot_service_selects_production_python_312_environment(self):
        service = (
            self.root / 'deploy/systemd/polytopia.service'
        ).read_text(encoding='utf-8').splitlines()

        self.assertIn('[Service]', service)
        self.assertIn('Environment=POLYBOT_ENV=production', service)
        self.assertIn(
            'ExecStart=/home/nelluk/PolyBot39/.venv/bin/python '
            '/home/nelluk/PolyBot39/bot.py',
            service,
        )
        self.assertIn('WorkingDirectory=/home/nelluk/PolyBot39', service)

    def test_cutover_runbook_preserves_legacy_rollback(self):
        runbook = (
            self.root / 'docs/PRODUCTION_CUTOVER.md'
        ).read_text(encoding='utf-8')

        self.assertIn('uv sync --locked --no-dev --python 3.12.13', runbook)
        self.assertIn('POLYBOT_ROLLBACK_COMMIT=43b3425', runbook)
        self.assertIn(
            '75b24b5e79e997477014aa979d87dc5f6d162bc5',
            runbook,
        )
        self.assertIn('final-stopped-polytopia-full-', runbook)
        self.assertIn('/home/nelluk/PolyBot39/bot.py --skip_tasks', runbook)
        self.assertIn(
            '/home/nelluk/PolyBot39/bin/python3 '
            '/home/nelluk/PolyBot39/bot.py',
            runbook,
        )
        self.assertIn('polyapi.service` remains inactive', runbook)
        self.assertNotIn('git switch master', runbook)

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
            '/home/nelluk/PolyBot39/scripts/export_reporting_duckdb.py',
            source,
        )
        self.assertIn(
            '--output "$REPORTTARGET"',
            source,
        )
        self.assertIn(
            'REPORTLOCK=/home/nelluk/.polybot-reporting.lock',
            source,
        )
        self.assertIn(
            '--lock-file "$REPORTLOCK"',
            source,
        )
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


if __name__ == '__main__':
    unittest.main()
