"""Focused offline coverage for the public Docker Compose deployment."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest


class SimpleComposeDeploymentTests(unittest.TestCase):
    root = Path(__file__).resolve().parents[1]
    docker_assets = root / 'deploy/docker'

    def test_root_assets_expose_examples_not_operator_owned_files(self):
        for relative_path in (
            'Dockerfile',
            'compose.example.yaml',
            'compose.external-postgres.example.yaml',
            '.env.example',
            'docs/DOCKER.md',
            '.github/workflows/self-hosting-smoke.yml',
        ):
            self.assertTrue((self.root / relative_path).is_file(), relative_path)
        self.assertFalse((self.root / 'compose.yaml').exists())
        self.assertIn(
            'compose.yaml',
            (self.root / '.gitignore').read_text(encoding='utf-8').splitlines(),
        )

        compose = (self.root / 'compose.example.yaml').read_text(encoding='utf-8')
        self.assertIn('  database:', compose)
        self.assertIn('  bot:', compose)
        self.assertIn('  schema:', compose)
        self.assertIn('dockerfile: Dockerfile', compose)
        self.assertIn('  backup:', compose)
        self.assertIn('  restore:', compose)
        self.assertIn('postgres_data:', compose)
        self.assertIn('create_host_path: false', compose)
        self.assertNotIn('\nname:', compose)
        self.assertNotIn('polybot-mac-beta', compose)
        self.assertNotIn('479029527553638401', compose)
        self.assertNotIn('478571892832206869', compose)
        self.assertNotIn('ports:', compose)
        self.assertIn('POLYBOT_DATABASE_CONFIGURATION: environment', compose)
        self.assertIn('${POLYBOT_CONFIG_FILE:-./config.ini}', compose)

    def test_public_workflow_exercises_bundled_compose_installation(self):
        workflow = (
            self.root / '.github/workflows/self-hosting-smoke.yml'
        ).read_text(encoding='utf-8')

        self.assertIn('  compose-install:', workflow)
        self.assertIn('cp compose.example.yaml compose.yaml', workflow)
        self.assertIn('docker compose build bot', workflow)
        self.assertIn('docker compose up -d database', workflow)
        self.assertIn('docker compose run --rm schema --apply', workflow)
        self.assertIn('python bot.py --add_default_data --skip_tasks', workflow)
        self.assertIn('docker compose run --rm schema --verify', workflow)
        self.assertIn('docker compose run --rm backup', workflow)
        self.assertIn('docker compose down -v --remove-orphans', workflow)
        self.assertNotIn('docker compose up -d bot', workflow)

    def test_public_workflow_runs_full_offline_regression_suite(self):
        workflow = (
            self.root / '.github/workflows/self-hosting-smoke.yml'
        ).read_text(encoding='utf-8')

        self.assertIn('  test-suite:', workflow)
        self.assertIn('POLYBOT_ENV: development', workflow)
        self.assertIn('.venv/bin/python -m unittest discover -v', workflow)
        self.assertIn(
            'cp config.development.ini-EXAMPLE config.development.ini',
            workflow,
        )
        self.assertIn(
            'cp server_settings_dev-EXAMPLE.py server_settings_dev.py',
            workflow,
        )

    def test_installation_neutral_examples_default_to_database_authority(self):
        production = (self.root / 'server_settings-EXAMPLE.py').read_text(
            encoding='utf-8'
        )
        development = (
            self.root / 'server_settings_dev-EXAMPLE.py'
        ).read_text(encoding='utf-8')
        guide = (self.root / 'docs/DOCKER.md').read_text(encoding='utf-8')

        config = (self.root / 'config.ini-EXAMPLE').read_text(encoding='utf-8')

        self.assertIn('guild_configuration_source = database', config)
        self.assertIn('application_command_capabilities = {}', production)
        self.assertIn('polyelo_feedback_route = {}', production)
        self.assertIn('database-backed', production)
        self.assertIn('Discord application and permissions', guide)
        self.assertNotIn('Beta Lab Staff', development)

    def test_external_postgres_example_owns_no_database_storage(self):
        compose = (self.root / 'compose.external-postgres.example.yaml').read_text(
            encoding='utf-8'
        )
        self.assertIn('${POLYBOT_DATABASE_HOST:', compose)
        self.assertNotIn('  database:', compose)
        self.assertIn('polybot_images:', compose)
        self.assertNotIn('restore:', compose)
        self.assertNotIn('ports:', compose)

    def test_database_password_has_one_new_install_authority(self):
        compose = (self.root / 'compose.example.yaml').read_text(encoding='utf-8')
        config = (self.root / 'config.ini-EXAMPLE').read_text(encoding='utf-8')
        environment = (self.root / '.env.example').read_text(encoding='utf-8')

        self.assertIn('POLYBOT_DATABASE_PASSWORD', compose)
        self.assertIn('POLYBOT_DATABASE_PASSWORD', environment)
        self.assertNotIn('psql_password =', config)
        self.assertIn('only database-credential authority', environment)

    def test_image_is_environment_neutral_and_nonroot(self):
        dockerfile = (self.root / 'Dockerfile').read_text(encoding='utf-8')
        dockerignore = (self.root / '.dockerignore').read_text(encoding='utf-8')
        self.assertIn('uv sync --locked --no-dev --no-install-project', dockerfile)
        self.assertIn('USER ${POLYBOT_UID}:${POLYBOT_GID}', dockerfile)
        self.assertIn('CMD ["python", "bot.py"]', dockerfile)
        self.assertNotIn('POLYBOT_ENV', dockerfile)
        self.assertNotIn('run_development_beta.py', dockerfile)
        self.assertNotIn('POLYBOT_SOURCE_CHECKPOINT', dockerfile)
        self.assertNotIn('POLYBOT_IMAGE_CHECKPOINT', dockerfile)
        self.assertNotIn('/usr/local/share/polybot/image-checkpoint', dockerfile)
        for private_path in (
            '.env',
            'config.ini',
            'config.development.ini',
            'server_settings.py',
            'server_settings_dev.py',
            'spreadsheet_creds.json',
            'graph.png',
        ):
            with self.subTest(private_path=private_path):
                self.assertIn(private_path, dockerignore.splitlines())

    def test_all_compose_bot_runtimes_declare_compose_supervision(self):
        for name in (
            'compose.example.yaml',
            'compose.external-postgres.example.yaml',
        ):
            with self.subTest(name=name):
                compose = (self.root / name).read_text(encoding='utf-8')
                self.assertIn('POLYBOT_RESTART_SUPERVISOR: compose', compose)
                self.assertIn('driver: json-file', compose)
                self.assertIn('max-size: "10m"', compose)
                self.assertIn('max-file: "3"', compose)

    def test_shell_assets_are_syntactically_valid(self):
        for name in (
            'init-postgres.sh',
            'backup-postgres.sh',
            'restore-postgres.sh',
        ):
            result = subprocess.run(
                ['/bin/sh', '-n', self.docker_assets / name],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

        initializer = (self.docker_assets / 'init-postgres.sh').read_text(
            encoding='utf-8'
        )
        self.assertIn('CREATE ROLE %I LOGIN PASSWORD %L', initializer)
        self.assertIn('NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION', initializer)
        self.assertIn('CREATE DATABASE %I OWNER %I', initializer)
        self.assertNotIn('DROP ', initializer)

        backup = (self.docker_assets / 'backup-postgres.sh').read_text(
            encoding='utf-8'
        )
        self.assertIn('pg_dump --format=custom', backup)
        self.assertIn('pg_restore --list', backup)
        self.assertIn('sha256sum', backup)
        self.assertIn('mv -- "$temporary_archive" "$archive_path"', backup)

        restore = (self.docker_assets / 'restore-postgres.sh').read_text(
            encoding='utf-8'
        )
        self.assertIn('--single-transaction', restore)
        self.assertIn('--no-owner', restore)
        self.assertIn('--no-acl', restore)
        self.assertIn("[ \"$relation_count\" = 0 ]", restore)

    @staticmethod
    def _write_executable(directory: Path, name: str, source: str) -> None:
        path = directory / name
        path.write_text(source, encoding='utf-8')
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def test_backup_validates_and_publishes_archive_and_checksum(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commands = root / 'commands'
            backups = root / 'backups'
            commands.mkdir()
            backups.mkdir()
            self._write_executable(
                commands,
                'pg_dump',
                """#!/bin/sh
set -eu
for argument in "$@"; do
  case "$argument" in
    --file=*) printf 'valid custom archive' >"${argument#--file=}" ;;
  esac
done
""",
            )
            self._write_executable(
                commands,
                'pg_restore',
                '#!/bin/sh\nexit 0\n',
            )
            self._write_executable(
                commands,
                'date',
                '#!/bin/sh\nprintf "20260826T120000Z\\n"\n',
            )

            script = (self.docker_assets / 'backup-postgres.sh').read_text(
                encoding='utf-8'
            ).replace('backup_root=/backups', f'backup_root={backups}')
            script_path = root / 'backup-postgres.sh'
            script_path.write_text(script, encoding='utf-8')

            result = subprocess.run(
                ['/bin/sh', script_path],
                env={
                    **os.environ,
                    'PATH': f'{commands}:/usr/bin:/bin',
                    'PGDATABASE': 'polybot',
                    'PGUSER': 'polybot',
                    'POLYBOT_BACKUP_PREFIX': 'polybot-test',
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            archive = backups / 'polybot-test-20260826T120000Z.dump'
            digest = backups / f'{archive.name}.sha256'
            self.assertEqual(archive.read_bytes(), b'valid custom archive')
            expected = hashlib.sha256(archive.read_bytes()).hexdigest()
            self.assertEqual(
                digest.read_text(encoding='utf-8'),
                f'{expected}  {archive.name}\n',
            )
            self.assertEqual(list(backups.glob('.*.partial.*')), [])

    def test_restore_requires_verified_archive_and_empty_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commands = root / 'commands'
            backups = root / 'backups'
            commands.mkdir()
            backups.mkdir()
            archive = backups / 'polybot-test.dump'
            archive.write_bytes(b'valid custom archive')
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            (backups / f'{archive.name}.sha256').write_text(
                f'{digest}  {archive.name}\n', encoding='utf-8'
            )
            marker = root / 'restore-ran'
            self._write_executable(
                commands,
                'pg_restore',
                """#!/bin/sh
set -eu
case " $* " in
  *" --list "*) exit 0 ;;
esac
: >"$RESTORE_MARKER"
""",
            )
            self._write_executable(
                commands,
                'psql',
                '#!/bin/sh\ncat >/dev/null\nprintf "0\\n"\n',
            )

            script = (self.docker_assets / 'restore-postgres.sh').read_text(
                encoding='utf-8'
            ).replace('/backups/', f'{backups}/')
            script_path = root / 'restore-postgres.sh'
            script_path.write_text(script, encoding='utf-8')

            result = subprocess.run(
                ['/bin/sh', script_path, archive.name],
                env={
                    **os.environ,
                    'PATH': f'{commands}:/usr/bin:/bin',
                    'PGDATABASE': 'polybot',
                    'PGUSER': 'polybot',
                    'RESTORE_MARKER': str(marker),
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(marker.is_file())

    def test_restore_rejects_nonempty_database_before_pg_restore_apply(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commands = root / 'commands'
            backups = root / 'backups'
            commands.mkdir()
            backups.mkdir()
            archive = backups / 'polybot-test.dump'
            archive.write_bytes(b'valid custom archive')
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            (backups / f'{archive.name}.sha256').write_text(
                f'{digest}  {archive.name}\n', encoding='utf-8'
            )
            marker = root / 'restore-ran'
            self._write_executable(
                commands,
                'pg_restore',
                """#!/bin/sh
set -eu
case " $* " in
  *" --list "*) exit 0 ;;
esac
: >"$RESTORE_MARKER"
""",
            )
            self._write_executable(
                commands,
                'psql',
                '#!/bin/sh\ncat >/dev/null\nprintf "1\\n"\n',
            )

            script = (self.docker_assets / 'restore-postgres.sh').read_text(
                encoding='utf-8'
            ).replace('/backups/', f'{backups}/')
            script_path = root / 'restore-postgres.sh'
            script_path.write_text(script, encoding='utf-8')

            result = subprocess.run(
                ['/bin/sh', script_path, archive.name],
                env={
                    **os.environ,
                    'PATH': f'{commands}:/usr/bin:/bin',
                    'PGDATABASE': 'polybot',
                    'PGUSER': 'polybot',
                    'RESTORE_MARKER': str(marker),
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('target database is not empty', result.stderr)
            self.assertFalse(marker.exists())


if __name__ == '__main__':
    unittest.main()
