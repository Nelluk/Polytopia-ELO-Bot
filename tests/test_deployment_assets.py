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

    def test_production_timezone_migration_is_additive_and_separately_gated(self):
        runbook = (
            self.root / 'docs/PRODUCTION_TIMEZONE_MIGRATION.md'
        ).read_text(encoding='utf-8')
        script = (
            self.root / 'scripts/migrate_player_timezone_production.py'
        ).read_text(encoding='utf-8')
        example_config = (
            self.root / 'config.ini-EXAMPLE'
        ).read_text(encoding='utf-8')

        self.assertIn('not standing\nauthorization', runbook)
        self.assertIn('P9-B1-PRODUCTION-TIMEZONE-APPLY', runbook)
        self.assertIn('SET\nTRANSACTION READ ONLY', runbook)
        self.assertIn('leave both harmless additive columns in place', runbook)
        self.assertIn('Do not improvise `DROP COLUMN`', runbook)
        self.assertIn("mode.add_argument('--verify'", script)
        self.assertIn("mode.add_argument('--apply'", script)
        self.assertNotIn("add_argument('--rollback'", script)
        self.assertIn('create_directories=False', script)
        self.assertIn('psql_db = polytopia2', example_config)

    def test_player_badge_migration_is_inventory_tracked_and_production_gated(self):
        badge_runbook = (
            self.root / 'docs/PLAYER_BADGES_MIGRATION.md'
        ).read_text(encoding='utf-8')
        cutover = (
            self.root / 'docs/MODERNIZATION_PRODUCTION_CUTOVER.md'
        ).read_text(encoding='utf-8')
        release = (
            self.root / 'modules/release_candidate.py'
        ).read_text(encoding='utf-8')
        self.assertIn('ARRAY[]::TEXT[]', badge_runbook)
        self.assertIn(
            'leaves this harmless additive column in place',
            badge_runbook.lower(),
        )
        production_script = (
            self.root / 'scripts/migrate_player_badges_production.py'
        ).read_text(encoding='utf-8')
        self.assertIn('P12.1-PRODUCTION-PLAYER-BADGES-APPLY', badge_runbook)
        self.assertIn('scripts/migrate_player_badges_production.py', cutover)
        self.assertIn("mode.add_argument('--verify'", production_script)
        self.assertIn("mode.add_argument('--apply'", production_script)
        self.assertNotIn("add_argument('--rollback'", production_script)
        self.assertIn("'scripts/migrate_player_badges.py'", release)
        self.assertIn(
            "'scripts/migrate_player_badges_production.py'", release
        )
        self.assertIn(
            "'modules/player_badges_production_migration.py'", release
        )
        self.assertIn("'docs/PLAYER_BADGES_MIGRATION.md'", release)

    def test_modernization_cutover_is_separate_ordered_and_fail_closed(self):
        historical = (
            self.root / 'docs/PRODUCTION_CUTOVER.md'
        ).read_text(encoding='utf-8')
        runbook = (
            self.root / 'docs/MODERNIZATION_PRODUCTION_CUTOVER.md'
        ).read_text(encoding='utf-8')
        canary = (
            self.root / 'deploy/systemd/polyelo-modernization-canary.conf'
        ).read_text(encoding='utf-8')
        example_settings = (
            self.root / 'server_settings-EXAMPLE.py'
        ).read_text(encoding='utf-8')

        self.assertIn(
            'historical completed dependency-upgrade record only', historical
        )
        self.assertIn('MODERNIZATION_PRODUCTION_CUTOVER.md', historical)
        self.assertIn('not standing production authorization', runbook)
        self.assertIn('POLYBOT_RELEASE_SHA', runbook)
        self.assertIn('POLYBOT_ROLLBACK_SHA', runbook)
        self.assertIn(
            'Native synchronization targets: Main `283436219780825088` and\n'
            '  PolyChampions `447883341463814144` only',
            runbook,
        )
        self.assertNotIn(
            'Initial native canary guild: PolyChampions, `478571892832206869`',
            runbook,
        )
        self.assertIn(
            "'polychampions': 447883341463814144",
            example_settings,
        )
        self.assertIn('unresolved adversarial-review items', runbook)
        self.assertIn(
            'application_command_all_guild_capabilities` is exactly empty',
            runbook,
        )
        self.assertIn(
            'Main and PolyChampions retain their live staff-help channels',
            runbook,
        )
        self.assertIn('pg_stat_activity', runbook)
        self.assertIn('P9-B1-PRODUCTION-TIMEZONE-APPLY', runbook)
        self.assertIn(
            'scripts/manage_production_backup_release.py',
            runbook,
        )
        self.assertIn(
            'P9-M3-PRODUCTION-BACKUP-RELEASE-APPLY',
            runbook,
        )
        self.assertIn('--validate', runbook)
        self.assertIn('.operator-backup-release.json', runbook)
        self.assertIn('--skip_tasks', runbook)
        self.assertIn('Restart=no', runbook)
        self.assertIn('--confirm-no-global-sync', runbook)
        self.assertIn('Never use `DROP COLUMN`', runbook)
        self.assertIn(
            'Announcement delivery\nis the terminal deployment action',
            runbook,
        )
        self.assertNotIn('There is no application schema migration', runbook)

        ordered_markers = (
            '### 1. Capture start state and fresh backup',
            '### 2. Stop the production writer and prove it is absent',
            '### 3. Move only to the exact reviewed release',
            '### 4. Apply and verify the additive schema',
            '### 5. Run the reviewed task-disabled process canary',
            '### 6. Cleanly stop the canary and start the canonical service',
            '### 7. Inspect and apply Main staff help plus the PolyChampions canary',
            '### 8. Finish and announce',
        )
        positions = [runbook.index(marker) for marker in ordered_markers]
        self.assertEqual(positions, sorted(positions))

        self.assertIn('[Service]', canary)
        self.assertIn('POLYBOT_ENV=production', (
            self.root / 'deploy/systemd/polyelo.service'
        ).read_text(encoding='utf-8'))
        self.assertIn(
            'ExecStart=/srv/polyelo/PolyBot39/.venv/bin/python '
            '/srv/polyelo/PolyBot39/bot.py --skip_tasks',
            canary,
        )
        self.assertIn('Restart=no', canary)

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
