"""Focused offline coverage for the P11.4B1 host-development export gate."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from modules import beta_operations
from modules import container_host_database_export as export


CHECKPOINT = 'a' * 40


def profile(root: Path, **overrides):
    values = {
        'environment': 'development',
        'project_root': root,
        'log_root': root / 'logs' / 'development',
        'expected_bot_id': beta_operations.BETA_APPLICATION_ID,
        'allowed_guild_ids': (beta_operations.BETA_GUILD_ID,),
        'database_name': 'polytopia_dev',
        'database_user': 'polybot_dev',
        'database_password': 'private-test-password',
        'database_host': 'localhost',
        'database_port': 5432,
        'background_tasks_enabled': False,
        'api_enabled': False,
        'bullet_enabled': False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class ContainerHostDatabaseExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'bot.py').write_text('# fixture\n', encoding='utf-8')
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.session_counter = self.root / 'session-counter'
        self._write_clients()
        self.profile = profile(self.root)
        self.plan = export.build_plan(self.profile, CHECKPOINT)

    def _executable(self, name: str, source: str) -> None:
        path = self.bin / name
        path.write_text(source, encoding='utf-8')
        path.chmod(0o700)

    def _write_clients(self):
        self._executable(
            'psql',
            '''#!/bin/sh
case "$*" in
  *current_database*current_user*) echo polytopia_dev:polybot_dev ;;
  *server_version_num*) echo 180004 ;;
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
  *pg_database_size*) echo 4096 ;;
  *) exit 9 ;;
esac
''',
        )
        self._executable(
            'pg_dump',
            '''#!/bin/sh
target=
for argument in "$@"; do
  case "$argument" in --file=*) target=${argument#--file=} ;; esac
done
[ -n "$target" ] || exit 9
printf 'host development custom archive\n' >"$target"
''',
        )
        self._executable('pg_restore', '#!/bin/sh\nprintf "catalog\\n"\n')

    def _apply(self, **environment):
        selected = {
            **os.environ,
            'PATH': f'{self.bin}:/usr/bin:/bin',
            **environment,
        }
        with mock.patch.dict(os.environ, selected, clear=True):
            return export.export_database(
                self.profile,
                self.plan,
                confirmation=self.plan.confirmation,
                timestamp='20260812T120000Z',
            )

    def test_plan_is_exact_and_truthful_about_session_sampling(self):
        rendered = export.format_plan(self.plan)

        self.assertEqual(self.plan.confirmation, f'EXPORT polytopia_dev {CHECKPOINT}')
        self.assertIn('durable beta stopped', rendered)
        self.assertIn('sampled immediately before and after pg_dump', rendered)
        self.assertIn('do not prove that no transient session existed', rendered)
        self.assertFalse(self.plan.backup_root.exists())

    def test_plan_rejects_nonlocal_or_non_development_targets(self):
        with self.assertRaises(export.HostDevelopmentExportError):
            export.build_plan(profile(self.root, database_host='db.example'), CHECKPOINT)
        with self.assertRaises(export.HostDevelopmentExportError):
            export.build_plan(profile(self.root, database_name='polytopia2'), CHECKPOINT)
        with self.assertRaises(export.HostDevelopmentExportError):
            export.build_plan(self.profile, 'not-a-checkpoint')

    def test_apply_holds_writer_lock_and_publishes_exact_private_pair(self):
        result = self._apply()

        expected_name = f'polybot-polytopia_dev-20260812T120000Z-{CHECKPOINT}.dump'
        self.assertEqual(result.archive.name, expected_name)
        self.assertEqual(result.archive.stat().st_mode & 0o777, 0o600)
        self.assertEqual(result.digest_path.stat().st_mode & 0o777, 0o600)
        digest = hashlib.sha256(result.archive.read_bytes()).hexdigest()
        self.assertEqual(result.sha256, digest)
        self.assertEqual(
            result.digest_path.read_text(encoding='ascii'),
            f'{digest}  {expected_name}\n',
        )
        self.assertEqual((result.sessions_before, result.sessions_after), (0, 0))
        self.assertEqual(list(result.archive.parent.glob('.polybot-host-*')), [])

    def test_apply_refuses_while_beta_writer_lock_is_held(self):
        paths = beta_operations.operation_paths(self.profile, create=True)
        with beta_operations.BetaWriterLock(paths.writer_lock):
            with self.assertRaisesRegex(
                    beta_operations.BetaRuntimeInvariantError,
                    'Another development beta writer'):
                self._apply()
        self.assertEqual(list(self.plan.backup_root.glob('*.dump')), [])

    def test_apply_refuses_post_dump_session_without_publishing(self):
        with self.assertRaisesRegex(
                export.HostDevelopmentExportError,
                'present after pg_dump'):
            self._apply(FAKE_SESSION_COUNTER=str(self.session_counter))

        self.assertEqual(list(self.plan.backup_root.glob('*.dump')), [])
        self.assertEqual(list(self.plan.backup_root.glob('*.sha256')), [])
        self.assertEqual(list(self.plan.backup_root.glob('.polybot-host-*')), [])

    def test_wrong_confirmation_is_connection_and_output_free(self):
        with mock.patch.object(export, '_client') as client:
            with self.assertRaisesRegex(
                    export.HostDevelopmentExportError,
                    'does not match'):
                export.export_database(
                    self.profile,
                    self.plan,
                    confirmation='EXPORT polytopia_dev wrong',
                    timestamp='20260812T120000Z',
                )
        client.assert_not_called()
        self.assertFalse(self.plan.backup_root.exists())


if __name__ == '__main__':
    unittest.main()
