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

    def test_root_assets_expose_standard_compose_entrypoints(self):
        for relative_path in (
            'Dockerfile',
            'compose.yaml',
            'compose.host-postgres.yaml',
            'compose.beta.yaml',
            '.env.example',
            '.env.host-postgres.example',
            '.env.beta.example',
            'docs/DOCKER.md',
            'docs/DEVELOPMENT_DOCKER.md',
        ):
            self.assertTrue((self.root / relative_path).is_file(), relative_path)

        compose = (self.root / 'compose.yaml').read_text(encoding='utf-8')
        self.assertIn('  database:', compose)
        self.assertIn('  bot:', compose)
        self.assertIn('  schema:', compose)
        self.assertIn('  backup:', compose)
        self.assertIn('  restore:', compose)
        self.assertIn('external: true', compose)
        self.assertIn('name: ${POSTGRES_VOLUME_NAME:', compose)
        self.assertIn('create_host_path: false', compose)
        self.assertNotIn('\nname:', compose)
        self.assertNotIn('polybot-mac-beta', compose)
        self.assertNotIn('479029527553638401', compose)
        self.assertNotIn('478571892832206869', compose)
        self.assertNotIn('ports:', compose)

    def test_beta_compose_uses_direct_standard_lifecycle(self):
        compose = (self.root / 'compose.beta.yaml').read_text(encoding='utf-8')
        environment = (self.root / '.env.beta.example').read_text(encoding='utf-8')
        guide = (
            self.root / 'docs/DEVELOPMENT_DOCKER.md'
        ).read_text(encoding='utf-8')

        self.assertIn('  bot:', compose)
        self.assertIn('  schema:', compose)
        self.assertIn('scripts/run_development_beta.py', compose)
        self.assertIn('scripts/bootstrap_development_database.py', compose)
        self.assertIn('POLYBOT_BETA_STARTUP_SYNC: disabled', compose)
        self.assertIn('${POSTGRES_SOCKET_DIR:-/var/run/postgresql}', compose)
        self.assertIn('polybot_images:/app/data/development/images', compose)
        self.assertIn('polybot_logs:/app/logs/development', compose)
        self.assertNotIn('\nname:', compose)
        self.assertNotIn('ports:', compose)
        self.assertNotIn('./polybot', compose)
        self.assertNotIn('  database:', compose)
        self.assertIn('COMPOSE_FILE=compose.beta.yaml', environment)
        self.assertIn('docker compose up -d bot', guide)
        self.assertNotIn('./polybot', guide)
        self.assertNotIn('./polybot', compose)

    def test_host_postgres_compose_owns_no_database_storage(self):
        compose = (
            self.root / 'compose.host-postgres.yaml'
        ).read_text(encoding='utf-8')
        self.assertIn('${POSTGRES_SOCKET_DIR:-/var/run/postgresql}', compose)
        self.assertIn('target: /var/run/postgresql', compose)
        self.assertIn('read_only: true', compose)
        self.assertNotIn('  database:', compose)
        self.assertNotIn('\nvolumes:', compose)
        self.assertNotIn('POSTGRES_VOLUME_NAME', compose)
        self.assertNotIn('restore:', compose)
        self.assertNotIn('ports:', compose)

    def test_image_is_environment_neutral_and_nonroot(self):
        dockerfile = (self.root / 'Dockerfile').read_text(encoding='utf-8')
        self.assertIn('uv sync --locked --no-dev --no-install-project', dockerfile)
        self.assertIn('USER ${POLYBOT_UID}:${POLYBOT_GID}', dockerfile)
        self.assertIn('CMD ["python", "bot.py"]', dockerfile)
        self.assertNotIn('POLYBOT_ENV', dockerfile)
        self.assertNotIn('run_development_beta.py', dockerfile)
        self.assertNotIn('POLYBOT_SOURCE_CHECKPOINT', dockerfile)

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
