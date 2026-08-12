"""Focused offline coverage for the cross-platform ``./polybot`` interface."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class PolybotOperatorInterfaceTests(unittest.TestCase):
    source_root = Path(__file__).resolve().parents[1]
    script = source_root / 'polybot'

    def _source(self, command: str, *, platform: str) -> subprocess.CompletedProcess:
        environment = {
            **os.environ,
            'POLYBOT_SOURCE_ONLY': '1',
            'POLYBOT_PLATFORM_OVERRIDE': platform,
        }
        return subprocess.run(
            ['/bin/sh', '-c', f'. ./polybot; {command}'],
            cwd=self.source_root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_public_help_exposes_only_the_short_operator_commands(self):
        result = subprocess.run(
            [self.script, '--help'],
            cwd=self.source_root,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        for command in (
            'setup', 'bootstrap-guild ID', 'import-backup PATH', 'start',
            'status', 'logs [--follow]', 'restart', 'stop', 'backup',
            'verify-backup PATH',
        ):
            self.assertIn(command, result.stdout)
        self.assertNotIn('--profile', result.stdout)
        self.assertNotIn('--project-name', result.stdout)
        self.assertNotIn('POLYBOT_', result.stdout)

    def test_darwin_and_linux_choose_the_reviewed_runtime_identities(self):
        darwin = self._source(
            'printf "%s|%s|%s\\n" "$(platform_name)" "$(runtime_uid)" "$(runtime_gid)"',
            platform='Darwin',
        )
        linux = self._source(
            'printf "%s|%s|%s\\n" "$(platform_name)" "$(runtime_uid)" "$(runtime_gid)"',
            platform='Linux',
        )

        self.assertEqual(darwin.returncode, 0, darwin.stderr)
        self.assertEqual(darwin.stdout.strip(), 'darwin|1000|1000')
        self.assertEqual(linux.returncode, 0, linux.stderr)
        self.assertEqual(
            linux.stdout.strip(),
            f'linux|{os.getuid()}|{os.getgid()}',
        )

    def test_exact_confirmation_accepts_only_the_displayed_value(self):
        accepted = subprocess.run(
            ['/bin/sh', '-c',
             'printf "EXACT VALUE\\n" | '
             'POLYBOT_SOURCE_ONLY=1 /bin/sh -c '
             "'. ./polybot; confirm_exact \"EXACT VALUE\"; echo accepted'"],
            cwd=self.source_root,
            capture_output=True,
            text=True,
            check=False,
        )
        refused = subprocess.run(
            ['/bin/sh', '-c',
             'printf "wrong\\n" | '
             'POLYBOT_SOURCE_ONLY=1 /bin/sh -c '
             "'. ./polybot; confirm_exact \"EXACT VALUE\"'"],
            cwd=self.source_root,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertIn('accepted', accepted.stdout)
        self.assertEqual(refused.returncode, 2)
        self.assertIn('did not match', refused.stderr)

    def test_bootstrap_rejects_invalid_guild_before_docker(self):
        result = subprocess.run(
            [self.script, 'bootstrap-guild', 'not-a-guild'],
            cwd=self.source_root,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn('positive Discord guild ID', result.stderr)
        self.assertNotIn('Docker', result.stderr)

    def test_linux_writer_audit_excludes_container_descendants(self):
        result = subprocess.run(
            ['/bin/sh', '-c', '''
                . ./polybot
                docker() {
                  if [ "$1" = ps ]; then
                    echo owned-container
                  else
                    echo 100
                  fi
                }
                ps() {
                  printf '%s\n' \
                    '100 1 /sbin/docker-init --' \
                    '101 100 python bot.py --skip_tasks' \
                    '200 1 python bot.py --skip_tasks'
                }
                host_writer_count
            '''],
            cwd=self.source_root,
            env={**os.environ, 'POLYBOT_SOURCE_ONLY': '1'},
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '1')

    def test_archive_staging_never_replaces_a_different_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy2(self.script, root / 'polybot')
            backups = root / 'deploy/container/backups'
            backups.mkdir(parents=True)
            source = root / (
                'polybot-polytopia_dev-20260812T123355Z-'
                + 'a' * 40 + '.dump'
            )
            source.write_bytes(b'archive-one')
            source.with_suffix('.dump.sha256').write_text(
                f'{"b" * 64}  {source.name}\n',
                encoding='utf-8',
            )
            environment = {
                **os.environ,
                'POLYBOT_SOURCE_ONLY': '1',
                'POLYBOT_ROOT_OVERRIDE': str(root),
            }
            first = subprocess.run(
                ['/bin/sh', '-c', f'. ./polybot; ensure_private_directories; stage_archive "{source}"'],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            source.write_bytes(b'archive-two')
            second = subprocess.run(
                ['/bin/sh', '-c', f'. ./polybot; ensure_private_directories; stage_archive "{source}"'],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(second.returncode, 2)
            self.assertIn('Refusing to replace', second.stderr)

    def test_interface_reuses_reviewed_jobs_and_contains_no_sync_path(self):
        source = self.script.read_text(encoding='utf-8')
        for service in (
            'database-provision', 'schema', 'database-import',
            'database-backup', 'database-restore-drill', 'guild-bootstrap',
        ):
            self.assertIn(service, source)
        self.assertIn('bootstrap-guild GUILD_ID', source)
        self.assertIn('guild-bootstrap snapshot', source)
        self.assertIn('guild-bootstrap apply', source)
        self.assertIn('assert_single_writer_startable', source)
        self.assertIn('No Discord application commands were synchronized', source)
        self.assertIn('VERIFIED %s %s %s', source)
        self.assertNotIn('POLYBOT_IMPORT_EXPECTED_COUNTS', source)
        self.assertIn('PROJECT_NAME=polybot-mac-beta', source)
        self.assertIn("'{{.State.Pid}}'", source)
        self.assertIn('host_private_input_probe', source)
        self.assertIn("stat -f '%u'", source)
        self.assertIn("stat -c '%u'", source)
        self.assertIn('container_roots', source)
        self.assertIn('exposure=internal-only', source)
        self.assertIn('backup_checkpoint=$(docker exec', source)
        self.assertNotIn('manage_application_commands.py', source)
        self.assertNotIn('polytopia2', source)
        self.assertNotIn('config.ini', source)


if __name__ == '__main__':
    unittest.main()
