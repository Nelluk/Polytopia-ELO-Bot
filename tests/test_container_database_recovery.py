from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


CHECKPOINT = 'a' * 40
ADMIN_PASSWORD = 'administrative-test-secret'
APP_PASSWORD = 'application-test-secret'
IMPORT_ARCHIVE_NAME = (
    'polybot-polytopia_dev-20260812T123355Z-'
    'd27d6c83508ad00ef4e28d4eabad5fcddcf3189f.dump'
)


class ContainerDatabaseRecoveryTests(unittest.TestCase):
    source_root = Path(__file__).resolve().parents[1]

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.backups = self.root / 'backups'
        self.secrets = self.root / 'secrets'
        self.fake_bin = self.root / 'bin'
        self.backups.mkdir()
        self.secrets.mkdir()
        self.fake_bin.mkdir()
        self.admin_secret = self.secrets / 'admin.txt'
        self.app_secret = self.secrets / 'app.txt'
        self.admin_secret.write_text(ADMIN_PASSWORD + '\n', encoding='utf-8')
        self.app_secret.write_text(APP_PASSWORD + '\n', encoding='utf-8')
        self.psql_marker = self.root / 'psql-called'
        self.restore_marker = self.root / 'restore-called'
        self.session_counter = self.root / 'session-counter'
        self._write_fake_clients()
        self.backup_script = self._copy_script(
            'backup-development-database.sh',
            {
                'BACKUP_ROOT=/backups': f'BACKUP_ROOT={self.backups}',
                'ADMIN_SECRET=/run/secrets/postgres_admin_password': (
                    f'ADMIN_SECRET={self.admin_secret}'
                ),
            },
        )
        self.restore_script = self._copy_script(
            'restore-development-database.sh',
            {
                'BACKUP_ROOT=/backups': f'BACKUP_ROOT={self.backups}',
                'ADMIN_SECRET=/run/secrets/postgres_admin_password': (
                    f'ADMIN_SECRET={self.admin_secret}'
                ),
                'APPLICATION_SECRET=/run/secrets/polybot_database_password': (
                    f'APPLICATION_SECRET={self.app_secret}'
                ),
            },
        )
        import_archive = self.backups / IMPORT_ARCHIVE_NAME
        import_archive.write_bytes(b'reviewed transferred development archive\n')
        self.import_digest = hashlib.sha256(import_archive.read_bytes()).hexdigest()
        import_archive.with_suffix('.dump.sha256').write_text(
            f'{self.import_digest}  {IMPORT_ARCHIVE_NAME}\n',
            encoding='utf-8',
        )
        self.import_script = self._copy_script(
            'import-development-database.sh',
            {
                'BACKUP_ROOT=/backups': f'BACKUP_ROOT={self.backups}',
                'ADMIN_SECRET=/run/secrets/postgres_admin_password': (
                    f'ADMIN_SECRET={self.admin_secret}'
                ),
                'APPLICATION_SECRET=/run/secrets/polybot_database_password': (
                    f'APPLICATION_SECRET={self.app_secret}'
                ),
                (
                    'EXPECTED_DIGEST='
                    'a1ab30a068a068da6ce207d41d8b840a31291d721b49ee4e1d7a9c464958aa8b'
                ): f'EXPECTED_DIGEST={self.import_digest}',
            },
        )

    def _copy_script(self, name: str, replacements: dict[str, str]) -> Path:
        source = (
            self.source_root / 'deploy/container' / name
        ).read_text(encoding='utf-8')
        for old, new in replacements.items():
            self.assertIn(old, source)
            source = source.replace(old, new, 1)
        target = self.root / name
        target.write_text(source, encoding='utf-8')
        target.chmod(0o700)
        return target

    def _write_executable(self, name: str, source: str) -> None:
        target = self.fake_bin / name
        target.write_text(source, encoding='utf-8')
        target.chmod(0o700)

    def _write_fake_clients(self) -> None:
        self._write_executable(
            'date',
            '#!/bin/sh\necho 20260812T120000Z\n',
        )
        self._write_executable(
            'pg_dump',
            '''#!/bin/sh
target=
for argument in "$@"; do
  case "$argument" in --file=*) target=${argument#--file=} ;; esac
done
[ -n "$target" ] || exit 9
printf 'reviewed fake custom archive\n' >"$target"
''',
        )
        self._write_executable(
            'pg_restore',
            '''#!/bin/sh
case " $* " in
  *' --list '*) exit 0 ;;
esac
printf 'called\n' >"$FAKE_RESTORE_MARKER"
[ "${FAKE_RESTORE_FAIL:-0}" = 0 ]
''',
        )
        self._write_executable(
            'psql',
            '''#!/bin/sh
printf 'called\n' >"$FAKE_PSQL_MARKER"
case "$*" in
  *--dbname=polytopia_dev*current_database*) echo polytopia_dev:polybot_dev ;;
  *current_database*) echo postgres:postgres ;;
  *server_version_num*) echo 180004 ;;
  *"FROM pg_roles AS role"*) echo "${FAKE_SAFE_ROLE:-1}" ;;
  *"FROM pg_database AS database"*) echo "${FAKE_SAFE_DATABASE:-1}" ;;
  *"relation.relkind IN ('r', 'p', 'S', 'v', 'm', 'f')"*) echo "${FAKE_PUBLIC_RELATIONS:-0}" ;;
  *"id BETWEEN 2286 AND 2288"*) echo '71|4|44|15|3|48|24' ;;
  *relation.relkind*) echo 0 ;;
  *pg_get_userbyid*) echo 1 ;;
  *pg_stat_activity*)
    if [ -n "${FAKE_SESSION_COUNTER:-}" ]; then
      count=$(cat "$FAKE_SESSION_COUNTER" 2>/dev/null || echo 0)
      count=$((count + 1))
      echo "$count" >"$FAKE_SESSION_COUNTER"
      if [ "$count" -eq 1 ]; then echo 0; else echo 1; fi
    else
      echo 0
    fi
    ;;
  *pg_database_size*) echo 1024 ;;
  *datistemplate*) echo "${FAKE_NONDEFAULT_DATABASES:-0}" ;;
  *pg_roles*) echo "${FAKE_CUSTOM_ROLES:-0}" ;;
  *to_regclass*) echo 0 ;;
  *pg_constraint*) echo 1 ;;
  *) exit 0 ;;
esac
''',
        )

    def _environment(self, **updates: str) -> dict[str, str]:
        environment = {
            **os.environ,
            'PATH': f'{self.fake_bin}:/usr/bin:/bin:/sbin',
            'POLYBOT_ENV': 'development',
            'POLYBOT_SOURCE_CHECKPOINT': CHECKPOINT,
            'PGHOST': 'postgres',
            'PGPORT': '5432',
            'PGDATABASE': 'postgres',
            'PGUSER': 'postgres',
            'FAKE_PSQL_MARKER': str(self.psql_marker),
            'FAKE_RESTORE_MARKER': str(self.restore_marker),
        }
        environment.update(updates)
        return environment

    def _run(self, script: Path, **environment: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ['/bin/sh', script],
            env=self._environment(**environment),
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )

    def _create_backup(self) -> tuple[Path, str]:
        result = self._run(
            self.backup_script,
            POLYBOT_BACKUP_CONFIRMATION=f'BACKUP polytopia_dev {CHECKPOINT}',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        archives = list(self.backups.glob('*T120000Z*.dump'))
        self.assertEqual(len(archives), 1)
        archive = archives[0]
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        return archive, digest

    def test_backup_apply_publishes_only_one_exact_digest_pair(self):
        archive, digest = self._create_backup()

        self.assertEqual(
            archive.name,
            f'polybot-polytopia_dev-20260812T120000Z-{CHECKPOINT}.dump',
        )
        self.assertEqual(
            archive.with_suffix('.dump.sha256').read_text(encoding='utf-8'),
            f'{digest}  {archive.name}\n',
        )
        self.assertFalse((self.backups / '.polybot-backup.lock').exists())
        self.assertEqual(list(self.backups.glob('.polybot-*.partial.*')), [])

        original = archive.read_bytes()
        repeated = self._run(
            self.backup_script,
            POLYBOT_BACKUP_CONFIRMATION=f'BACKUP polytopia_dev {CHECKPOINT}',
        )
        self.assertEqual(repeated.returncode, 2)
        self.assertEqual(archive.read_bytes(), original)

    def test_backup_refuses_if_application_session_is_present_after_dump(self):
        result = self._run(
            self.backup_script,
            POLYBOT_BACKUP_CONFIRMATION=f'BACKUP polytopia_dev {CHECKPOINT}',
            FAKE_SESSION_COUNTER=str(self.session_counter),
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn('present after pg_dump', result.stderr)
        self.assertEqual(list(self.backups.glob('*T120000Z*.dump')), [])
        self.assertEqual(list(self.backups.glob('*T120000Z*.sha256')), [])
        self.assertFalse((self.backups / '.polybot-backup.lock').exists())

    def test_backup_plan_states_the_pre_post_sampling_limitation(self):
        result = self._run(self.backup_script)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('sampled immediately before and after pg_dump', result.stdout)
        self.assertIn(
            'do not prove that no transient session existed between them',
            result.stdout,
        )

    def test_restore_is_local_plan_then_exact_fresh_target_apply(self):
        archive, digest = self._create_backup()
        self.psql_marker.unlink()
        self.restore_marker.unlink(missing_ok=True)

        plan = self._run(
            self.restore_script,
            PGHOST='restore-postgres',
            POLYBOT_BACKUP_ARCHIVE=archive.name,
        )
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertIn(
            f'RESTORE polytopia_restore_verify {digest}',
            plan.stdout,
        )
        self.assertFalse(self.psql_marker.exists())
        self.assertFalse(self.restore_marker.exists())

        applied = self._run(
            self.restore_script,
            PGHOST='restore-postgres',
            POLYBOT_BACKUP_ARCHIVE=archive.name,
            POLYBOT_RESTORE_CONFIRMATION=(
                f'RESTORE polytopia_restore_verify {digest}'
            ),
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertIn('Fresh-volume restore drill complete.', applied.stdout)
        self.assertTrue(self.psql_marker.exists())
        self.assertTrue(self.restore_marker.exists())
        self.assertNotIn(ADMIN_PASSWORD, applied.stdout + applied.stderr)
        self.assertNotIn(APP_PASSWORD, applied.stdout + applied.stderr)

        self.psql_marker.unlink()
        self.restore_marker.unlink()
        sidecar = archive.with_suffix('.dump.sha256')
        sidecar.write_text(
            sidecar.read_text(encoding='utf-8') + '\n',
            encoding='utf-8',
        )
        malformed = self._run(
            self.restore_script,
            PGHOST='restore-postgres',
            POLYBOT_BACKUP_ARCHIVE=archive.name,
        )
        self.assertEqual(malformed.returncode, 2)
        self.assertIn('unexpected shape', malformed.stderr)
        self.assertFalse(self.psql_marker.exists())
        self.assertFalse(self.restore_marker.exists())

    def test_restore_refuses_nonfresh_recovery_service_before_writes(self):
        archive, digest = self._create_backup()
        self.restore_marker.unlink(missing_ok=True)
        result = self._run(
            self.restore_script,
            PGHOST='restore-postgres',
            POLYBOT_BACKUP_ARCHIVE=archive.name,
            POLYBOT_RESTORE_CONFIRMATION=(
                f'RESTORE polytopia_restore_verify {digest}'
            ),
            FAKE_NONDEFAULT_DATABASES='1',
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn('not fresh', result.stderr)
        self.assertFalse(self.restore_marker.exists())

    def test_import_plan_is_connection_free_and_apply_is_digest_bound(self):
        self.psql_marker.unlink(missing_ok=True)
        self.restore_marker.unlink(missing_ok=True)

        plan = self._run(
            self.import_script,
            POLYBOT_BACKUP_ARCHIVE=IMPORT_ARCHIVE_NAME,
        )
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertIn(
            f'IMPORT polytopia_dev {self.import_digest}',
            plan.stdout,
        )
        self.assertIn('no PostgreSQL connection or write', plan.stdout)
        self.assertFalse(self.psql_marker.exists())
        self.assertFalse(self.restore_marker.exists())

        applied = self._run(
            self.import_script,
            POLYBOT_BACKUP_ARCHIVE=IMPORT_ARCHIVE_NAME,
            POLYBOT_IMPORT_CONFIRMATION=(
                f'IMPORT polytopia_dev {self.import_digest}'
            ),
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertIn(
            'Development bundled database import complete.',
            applied.stdout,
        )
        self.assertTrue(self.psql_marker.exists())
        self.assertTrue(self.restore_marker.exists())
        self.assertNotIn(ADMIN_PASSWORD, applied.stdout + applied.stderr)
        self.assertNotIn(APP_PASSWORD, applied.stdout + applied.stderr)

    def test_import_plan_does_not_read_secrets(self):
        self.admin_secret.unlink()
        self.app_secret.unlink()

        plan = self._run(
            self.import_script,
            POLYBOT_BACKUP_ARCHIVE=IMPORT_ARCHIVE_NAME,
        )

        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertFalse(self.psql_marker.exists())
        self.assertFalse(self.restore_marker.exists())

    def test_import_wrong_confirmation_refuses_before_database_access(self):
        self.psql_marker.unlink(missing_ok=True)
        result = self._run(
            self.import_script,
            POLYBOT_BACKUP_ARCHIVE=IMPORT_ARCHIVE_NAME,
            POLYBOT_IMPORT_CONFIRMATION='IMPORT polytopia_dev wrong',
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn('does not match', result.stderr)
        self.assertFalse(self.psql_marker.exists())
        self.assertFalse(self.restore_marker.exists())

    def test_import_refuses_session_race_before_restore(self):
        self.restore_marker.unlink(missing_ok=True)
        result = self._run(
            self.import_script,
            POLYBOT_BACKUP_ARCHIVE=IMPORT_ARCHIVE_NAME,
            POLYBOT_IMPORT_CONFIRMATION=(
                f'IMPORT polytopia_dev {self.import_digest}'
            ),
            FAKE_SESSION_COUNTER=str(self.session_counter),
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn('connected during preflight', result.stderr)
        self.assertFalse(self.restore_marker.exists())

    def test_import_requires_restricted_role_and_owned_database(self):
        for variable in ('FAKE_SAFE_ROLE', 'FAKE_SAFE_DATABASE'):
            with self.subTest(variable=variable):
                self.restore_marker.unlink(missing_ok=True)
                result = self._run(
                    self.import_script,
                    POLYBOT_BACKUP_ARCHIVE=IMPORT_ARCHIVE_NAME,
                    POLYBOT_IMPORT_CONFIRMATION=(
                        f'IMPORT polytopia_dev {self.import_digest}'
                    ),
                    **{variable: '0'},
                )
                self.assertEqual(result.returncode, 2)
                self.assertFalse(self.restore_marker.exists())

    def test_import_repeat_refuses_nonfresh_target_before_restore(self):
        self.restore_marker.unlink(missing_ok=True)
        repeated = self._run(
            self.import_script,
            POLYBOT_BACKUP_ARCHIVE=IMPORT_ARCHIVE_NAME,
            POLYBOT_IMPORT_CONFIRMATION=(
                f'IMPORT polytopia_dev {self.import_digest}'
            ),
            FAKE_PUBLIC_RELATIONS='1',
        )

        self.assertEqual(repeated.returncode, 2)
        self.assertIn('Target is not fresh', repeated.stderr)
        self.assertFalse(self.restore_marker.exists())

    def test_import_rejects_any_other_archive_before_database_access(self):
        self.psql_marker.unlink(missing_ok=True)
        result = self._run(
            self.import_script,
            POLYBOT_BACKUP_ARCHIVE='another.dump',
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn('exact reviewed archive basename', result.stderr)
        self.assertFalse(self.psql_marker.exists())


if __name__ == '__main__':
    unittest.main()
