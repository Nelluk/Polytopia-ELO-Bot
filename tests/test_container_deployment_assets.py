from pathlib import Path
import subprocess
import tomllib
import unittest


class ContainerDeploymentAssetTests(unittest.TestCase):
    root = Path(__file__).resolve().parents[1]
    assets = root / 'deploy/container'

    def test_contract_pins_reviewed_development_identity_and_effects(self):
        with (self.assets / 'container-contract.toml').open('rb') as source:
            contract = tomllib.load(source)

        self.assertEqual(contract['contract_version'], 2)
        self.assertEqual(contract['environment'], 'development')
        self.assertEqual(
            contract['python_image'],
            'python:3.12.13-slim-bookworm@sha256:'
            '4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2',
        )
        self.assertEqual(
            contract['uv_image'],
            'ghcr.io/astral-sh/uv:0.11.32@sha256:'
            'df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c',
        )
        self.assertEqual(
            contract['postgres_image'],
            'postgres:18.4-bookworm@sha256:'
            '882236b897e39051d2368c5ccc6cda944904723506b2dfc97f2a8f5bc9afa382',
        )
        self.assertEqual(contract['postgres_major'], 18)
        self.assertEqual(contract['database_name'], 'polytopia_dev')
        self.assertEqual(contract['database_user'], 'polybot_dev')
        self.assertEqual(contract['bundled_database_host'], 'postgres')
        self.assertEqual(contract['backup_directory'], 'deploy/container/backups')
        self.assertEqual(contract['backup_archive_prefix'], 'polybot-polytopia_dev')
        self.assertEqual(contract['restore_database_name'], 'polytopia_restore_verify')
        self.assertEqual(contract['restore_database_host'], 'restore-postgres')
        self.assertEqual(contract['restart_exit_status'], 75)
        self.assertEqual(
            contract['restart_supervisor_environment'],
            'POLYBOT_RESTART_SUPERVISOR=compose',
        )
        self.assertEqual(contract['bot_stop_signal'], 'SIGINT')
        self.assertEqual(contract['bot_stop_grace_seconds'], 45)
        self.assertEqual(
            contract['persistent_volumes'],
            [
                'postgres_data',
                'postgres_restore_data',
                'polybot_images',
                'polybot_logs',
            ],
        )
        self.assertEqual(
            contract['runtime_policy'],
            {
                'background_tasks_enabled': False,
                'api_enabled': False,
                'bullet_enabled': False,
                'startup_schema_changes': False,
                'startup_fixture_changes': False,
                'startup_discord_sync': False,
                'global_discord_sync': False,
            },
        )

    def test_bot_image_is_locked_nonroot_and_excludes_runtime_secrets(self):
        dockerfile = (self.assets / 'Dockerfile').read_text(encoding='utf-8')
        ignore = (self.root / '.dockerignore').read_text(encoding='utf-8')

        self.assertIn(
            'ARG PYTHON_IMAGE=python:3.12.13-slim-bookworm@sha256:', dockerfile
        )
        self.assertIn(
            'ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.32@sha256:', dockerfile
        )
        self.assertIn('uv sync --locked --no-dev --no-install-project', dockerfile)
        self.assertIn('USER 10001:10001', dockerfile)
        self.assertIn('STOPSIGNAL SIGINT', dockerfile)
        self.assertIn('POLYBOT_IMAGE_CHECKPOINT=${POLYBOT_SOURCE_CHECKPOINT}', dockerfile)
        self.assertIn('org.opencontainers.image.revision=${POLYBOT_SOURCE_CHECKPOINT}', dockerfile)
        self.assertIn('["python", "bot.py", "--skip_tasks"]', dockerfile)
        self.assertNotIn('config.development.ini', dockerfile)
        for excluded in (
            '.git', '.venv', 'config.ini', 'config.development.ini',
            'server_settings.py', 'server_settings_dev.py',
            'deploy/container/secrets/*.txt',
        ):
            self.assertIn(excluded, ignore)

    def test_bundled_compose_separates_persistence_and_explicit_jobs(self):
        compose = (
            self.assets / 'compose.development.yaml'
        ).read_text(encoding='utf-8')

        self.assertIn('postgres:18.4-bookworm@sha256:', compose)
        self.assertIn('postgres_data:/var/lib/postgresql', compose)
        self.assertIn('polybot_images:/app/data/development/images', compose)
        self.assertIn('polybot_logs:/app/logs/development', compose)
        self.assertIn('condition: service_healthy', compose)
        self.assertIn('pg_isready -U postgres -d postgres', compose)
        self.assertIn('database-provision:', compose)
        self.assertIn('database-backup:', compose)
        self.assertIn('database-restore-drill:', compose)
        self.assertIn('restore-postgres:', compose)
        self.assertIn('postgres_restore_data:/var/lib/postgresql', compose)
        self.assertIn('./backups:/backups', compose)
        self.assertIn('./backups:/backups:ro', compose)
        self.assertIn('profiles: ["recovery"]', compose)
        self.assertIn('profiles: ["tools"]', compose)
        self.assertIn('bootstrap_development_database.py', compose)
        self.assertIn('read_only: true', compose)
        self.assertIn('user: "10001:10001"', compose)
        self.assertIn('cap_drop:', compose)
        self.assertIn('no-new-privileges:true', compose)
        self.assertIn('stop_signal: SIGINT', compose)
        self.assertIn('stop_grace_period: 45s', compose)
        self.assertIn('restart: "on-failure:5"', compose)
        self.assertIn('POLYBOT_RESTART_SUPERVISOR: compose', compose)
        self.assertIn('POLYBOT_SOURCE_CHECKPOINT:', compose)
        self.assertNotIn('manage_application_commands.py', compose)
        self.assertNotIn('manage_dev_fixtures.py', compose)
        self.assertNotIn('--apply', compose)
        self.assertNotIn('polytopia2', compose)

    def test_recovery_jobs_are_development_only_plan_first_and_syntactically_valid(self):
        backup = self.assets / 'backup-development-database.sh'
        restore = self.assets / 'restore-development-database.sh'
        for script in (backup, restore):
            with self.subTest(script=script.name):
                result = subprocess.run(
                    ['/bin/sh', '-n', script],
                    capture_output=True,
                    check=False,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(script.stat().st_mode & 0o100)

        backup_source = backup.read_text(encoding='utf-8')
        restore_source = restore.read_text(encoding='utf-8')
        self.assertIn('BACKUP $SOURCE_DATABASE $checkpoint', backup_source)
        self.assertIn('zero other source-database sessions', backup_source)
        self.assertGreaterEqual(backup_source.count('active_sessions'), 3)
        self.assertIn('--format=custom', backup_source)
        self.assertIn('pg_restore --list', backup_source)
        self.assertIn('sha256sum', backup_source)
        self.assertIn('source-size plus 64 MiB headroom', backup_source)
        self.assertIn('mv -- "$temporary_archive" "$archive_path"', backup_source)
        self.assertNotIn('polytopia2', backup_source)

        self.assertIn('RESTORE $RESTORE_DATABASE $archive_digest', restore_source)
        self.assertIn('RESTORE_HOST=restore-postgres', restore_source)
        self.assertIn('RESTORE_DATABASE=polytopia_restore_verify', restore_source)
        self.assertIn('--single-transaction', restore_source)
        self.assertIn('required application tables', restore_source)
        self.assertIn('winner foreign key', restore_source)
        self.assertIn('wrong_table_owners', restore_source)
        self.assertNotIn('DROP DATABASE', restore_source)
        self.assertNotIn('polytopia2', restore_source)

    def test_backup_without_confirmation_is_connection_free_plan(self):
        result = subprocess.run(
            ['/bin/sh', self.assets / 'backup-development-database.sh'],
            env={
                'PATH': '/nonexistent',
                'POLYBOT_ENV': 'development',
                'POLYBOT_SOURCE_CHECKPOINT': 'a' * 40,
                'PGHOST': 'postgres',
                'PGPORT': '5432',
                'PGDATABASE': 'postgres',
                'PGUSER': 'postgres',
            },
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Plan only', result.stdout)
        self.assertIn('BACKUP polytopia_dev ' + ('a' * 40), result.stdout)

    def test_backup_wrong_confirmation_refuses_before_filesystem_or_database(self):
        result = subprocess.run(
            ['/bin/sh', self.assets / 'backup-development-database.sh'],
            env={
                'PATH': '/nonexistent',
                'POLYBOT_ENV': 'development',
                'POLYBOT_SOURCE_CHECKPOINT': 'a' * 40,
                'POLYBOT_BACKUP_CONFIRMATION': 'BACKUP polytopia_dev wrong',
                'PGHOST': 'postgres',
                'PGPORT': '5432',
                'PGDATABASE': 'postgres',
                'PGUSER': 'postgres',
            },
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn('does not match', result.stderr)

    def test_external_database_variant_has_no_database_service_or_secret(self):
        compose = (
            self.assets / 'compose.development.external-db.yaml'
        ).read_text(encoding='utf-8')

        self.assertIn('polybot-development-external-db', compose)
        self.assertIn('services:\n  schema:', compose)
        self.assertIn('\n  bot:', compose)
        self.assertNotIn('\n  postgres:', compose)
        self.assertNotIn('postgres_data', compose)
        self.assertNotIn('postgres_admin_password', compose)
        self.assertNotIn('database-provision', compose)
        self.assertNotIn('database-backup', compose)
        self.assertNotIn('database-restore-drill', compose)
        self.assertNotIn('restore-postgres', compose)

    def test_database_provisioner_is_development_and_major_gated(self):
        script = self.assets / 'provision-development-database.sh'
        result = subprocess.run(
            ['/bin/sh', '-n', script],
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        source = script.read_text(encoding='utf-8')
        self.assertIn('POLYBOT_ENV must be development', source)
        self.assertIn('PGHOST must be the bundled postgres service', source)
        self.assertIn('BETWEEN 180000 AND 189999', source)
        self.assertIn("CREATE ROLE %I LOGIN PASSWORD %L", source)
        self.assertIn("'polybot_dev'", source)
        self.assertIn("'polytopia_dev'", source)
        self.assertIn('NOT r.rolsuper', source)
        self.assertIn('NOT r.rolcreatedb', source)
        self.assertIn('pg_advisory_lock', source)
        self.assertNotIn('DROP ', source)
        self.assertNotIn('ALTER ', source)

    def test_document_records_static_proof_and_production_boundary(self):
        runbook = (
            self.root / 'docs/CONTAINERIZED_DEVELOPMENT.md'
        ).read_text(encoding='utf-8')

        self.assertIn('development-only static proof', runbook)
        self.assertIn('no\nDocker or Podman executable', runbook)
        self.assertIn('does not replace either existing systemd service', runbook)
        self.assertIn('Normal database or bot startup never creates application schema', runbook)
        self.assertIn('exit status 75', runbook)
        self.assertIn('copying a live volume is not a\nbackup', runbook)
        self.assertIn('fresh-volume restore drill', runbook)
        self.assertIn('production migration', runbook)


if __name__ == '__main__':
    unittest.main()
