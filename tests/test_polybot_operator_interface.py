"""Focused offline coverage for the cross-platform ``./polybot`` interface."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
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
            'setup', 'deploy', 'bootstrap-guild ID', 'import-backup PATH', 'start',
            'status', 'logs [--follow]', 'restart', 'stop', 'backup',
            'verify-backup PATH',
            'beta-lab [--mode bundled|external|external-socket]',
        ):
            self.assertIn(command, result.stdout)
        self.assertIn('Development-only upstream beta stack', result.stdout)
        self.assertIn('The default mode is bundled', result.stdout)
        self.assertIn('Bundled only:', result.stdout)
        self.assertIn('deploy/container/README.md', result.stdout)
        self.assertNotIn('--profile', result.stdout)
        self.assertNotIn('--project-name', result.stdout)
        self.assertNotIn('POLYBOT_', result.stdout)

    def test_beta_lab_routes_control_and_database_into_compose_namespace(self):
        result = subprocess.run(
            ['/bin/sh', '-c', r'''
                . ./polybot
                require_exact_beta_image() { :; }
                beta_control_python() { printf 'control:%s\n' "$*"; }
                beta_database_python() { printf 'database:%s\n' "$*"; }
                command_beta_lab status
                command_beta_lab roles-reconcile --confirm TOKEN
                command_beta_lab database-reconcile --confirm TOKEN
            '''],
            cwd=self.source_root,
            env={**os.environ, 'POLYBOT_SOURCE_ONLY': '1'},
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            'control:scripts/manage_beta_lab.py --json status', result.stdout,
        )
        self.assertIn(
            'control:scripts/manage_beta_lab_personas.py --json roles-reconcile --confirm TOKEN',
            result.stdout,
        )
        self.assertIn(
            'database:scripts/manage_beta_lab_personas.py --json database-reconcile --confirm TOKEN',
            result.stdout,
        )

    def test_beta_lab_external_mode_uses_the_external_compose_file(self):
        result = subprocess.run(
            ['/bin/sh', '-c', r'''
                . ./polybot
                docker() { printf '%s\n' "$*"; }
                DEPLOYMENT_MODE=external
                beta_compose ps -q bot
            '''],
            cwd=self.source_root,
            env={**os.environ, 'POLYBOT_SOURCE_ONLY': '1'},
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('compose --project-name', result.stdout)
        self.assertIn('compose.development.external-db.yaml ps -q bot', result.stdout)

    def test_external_socket_mode_adds_only_the_reviewed_overlay(self):
        result = subprocess.run(
            ['/bin/sh', '-c', r'''
                . ./polybot
                docker() { printf '%s\n' "$*"; }
                DEPLOYMENT_MODE=external-socket
                compose ps -q bot
            '''],
            cwd=self.source_root,
            env={**os.environ, 'POLYBOT_SOURCE_ONLY': '1'},
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('compose.development.external-db.yaml', result.stdout)
        self.assertIn(
            'compose.development.external-db.local-socket.yaml', result.stdout,
        )
        self.assertNotIn('compose.development.yaml ', result.stdout)

    def test_external_mode_refuses_bundled_database_lifecycle(self):
        result = subprocess.run(
            [self.script, '--mode', 'external-socket', 'backup'],
            cwd=self.source_root,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn('only in bundled mode', result.stderr)

    def test_external_socket_setup_preserves_password_without_bundle_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assets = root / 'deploy/container'
            assets.mkdir(parents=True)
            shutil.copy2(self.script, root / 'polybot')
            shutil.copy2(
                self.source_root / 'deploy/container/development.env.example',
                assets / 'development.env.example',
            )
            (root / 'config.development.ini').write_text(
                '[DEFAULT]\n'
                'psql_host = 127.0.0.1\n'
                'psql_password = retain-this-value\n',
                encoding='utf-8',
            )
            (root / 'server_settings_dev.py').write_text(
                'server_list = {}\n', encoding='utf-8',
            )
            command = r'''
                . ./polybot
                DEPLOYMENT_MODE=external-socket
                git_checkpoint() { printf '%040d\n' 0 | tr 0 a; }
                prepare_ignored_inputs
            '''
            result = subprocess.run(
                ['/bin/sh', '-c', command],
                cwd=root,
                env={
                    **os.environ,
                    'POLYBOT_SOURCE_ONLY': '1',
                    'POLYBOT_ROOT_OVERRIDE': str(root),
                    'POLYBOT_PLATFORM_OVERRIDE': 'Linux',
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            config = (
                assets / 'config.development.ini'
            ).read_text(encoding='utf-8')
            self.assertIn('psql_host = /var/run/postgresql', config)
            self.assertIn('psql_password = retain-this-value', config)
            self.assertFalse(
                (assets / 'secrets/postgres-admin-password.txt').exists()
            )
            self.assertFalse(
                (assets / 'secrets/polybot-database-password.txt').exists()
            )

    def test_external_start_never_starts_a_postgres_service(self):
        result = subprocess.run(
            ['/bin/sh', '-c', r'''
                . ./polybot
                DEPLOYMENT_MODE=external-socket
                require_engine() { :; }
                validate_project_name() { :; }
                require_prepared_inputs() { :; }
                host_private_input_probe() { :; }
                git_checkpoint() { printf '%040d\n' 0 | tr 0 a; }
                configured_checkpoint() { printf '%040d\n' 0 | tr 0 a; }
                run_immutable_doctor() { :; }
                live_bind_probe() { :; }
                live_socket_probe() { :; }
                assert_single_writer_startable() { :; }
                command_status() { :; }
                compose() { printf 'compose:%s\n' "$*"; }
                command_start
            '''],
            cwd=self.source_root,
            env={**os.environ, 'POLYBOT_SOURCE_ONLY': '1'},
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('compose:up -d bot', result.stdout)
        self.assertNotIn('postgres', result.stdout)

    def test_deploy_runs_setup_then_start_as_one_explicit_operation(self):
        result = subprocess.run(
            ['/bin/sh', '-c', r'''
                . ./polybot
                command_setup() { printf 'setup\n'; }
                start_services() { printf 'start\n'; }
                command_deploy
            '''],
            cwd=self.source_root,
            env={**os.environ, 'POLYBOT_SOURCE_ONLY': '1'},
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['setup', 'start'])

    def test_beta_lab_refuses_checkpoint_mismatch_before_compose_effect(self):
        result = subprocess.run(
            ['/bin/sh', '-c', r'''
                . ./polybot
                require_engine() { :; }
                require_beta_inputs() { :; }
                git_checkpoint() { printf '%040d\n' 1; }
                configured_checkpoint() { printf '%040d\n' 2; }
                docker() { printf 'unexpected-docker-effect\n'; }
                command_beta_lab status
            '''],
            cwd=self.source_root,
            env={**os.environ, 'POLYBOT_SOURCE_ONLY': '1'},
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn('differs from Git HEAD', result.stderr)
        self.assertNotIn('unexpected-docker-effect', result.stdout)

    def test_other_container_writer_census_recognizes_guarded_launcher(self):
        result = subprocess.run(
            ['/bin/sh', '-c', r'''
                . ./polybot
                docker() {
                  if [ "$1" = ps ]; then
                    printf '%s\n' allowed other
                  else
                    printf '%s\n' '["python","scripts/run_development_beta.py","--skip_tasks"]'
                  fi
                }
                other_container_writers allowed
            '''],
            cwd=self.source_root,
            env={**os.environ, 'POLYBOT_SOURCE_ONLY': '1'},
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '1')

    def test_other_project_guarded_launcher_blocks_start(self):
        result = subprocess.run(
            ['/bin/sh', '-c', r'''
                . ./polybot
                bot_container_id() { printf '%s\n' current; }
                host_writer_count() { printf '%s\n' 0; }
                docker() {
                  if [ "$1" = ps ]; then
                    printf '%s\n' current other-project-bot
                  else
                    printf '%s\n' '["python","scripts/run_development_beta.py","--skip_tasks"]'
                  fi
                }
                assert_single_writer_startable
            '''],
            cwd=self.source_root,
            env={**os.environ, 'POLYBOT_SOURCE_ONLY': '1'},
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn(
            'Another guarded development-beta container is already running',
            result.stderr,
        )

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
            env={
                **os.environ,
                'POLYBOT_SOURCE_ONLY': '1',
                'POLYBOT_PLATFORM_OVERRIDE': 'Linux',
                'DOCKER_HOST': 'unix:///var/run/docker.sock',
            },
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '1')

    def test_darwin_writer_audit_never_compares_docker_vm_pids(self):
        result = subprocess.run(
            ['/bin/sh', '-c', '''
                . ./polybot
                docker() {
                  if [ "$1" = ps ]; then echo vm-container; else echo 200; fi
                }
                ps() { printf '%s\n' '200 1 python bot.py --skip_tasks'; }
                host_writer_count
            '''],
            cwd=self.source_root,
            env={
                **os.environ,
                'POLYBOT_SOURCE_ONLY': '1',
                'POLYBOT_PLATFORM_OVERRIDE': 'Darwin',
            },
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '1')

    def test_remote_linux_writer_audit_never_compares_remote_pids(self):
        result = subprocess.run(
            ['/bin/sh', '-c', '''
                . ./polybot
                docker() {
                  if [ "$1" = ps ]; then echo remote-container; else echo 200; fi
                }
                ps() { printf '%s\n' '200 1 python bot.py --skip_tasks'; }
                host_writer_count
            '''],
            cwd=self.source_root,
            env={
                **os.environ,
                'POLYBOT_SOURCE_ONLY': '1',
                'POLYBOT_PLATFORM_OVERRIDE': 'Linux',
                'DOCKER_HOST': 'ssh://remote-engine',
            },
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '1')

    def test_ambiguous_linux_docker_endpoint_counts_native_writer(self):
        environment = {
            **os.environ,
            'POLYBOT_SOURCE_ONLY': '1',
            'POLYBOT_PLATFORM_OVERRIDE': 'Linux',
        }
        environment.pop('DOCKER_HOST', None)
        result = subprocess.run(
            ['/bin/sh', '-c', '''
                . ./polybot
                docker() { return 1; }
                ps() { printf '%s\n' '200 1 python bot.py --skip_tasks'; }
                host_writer_count
            '''],
            cwd=self.source_root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '1')

    def test_custom_linux_unix_socket_is_not_pid_namespace_proof(self):
        result = subprocess.run(
            ['/bin/sh', '-c', '''
                . ./polybot
                docker() {
                  if [ "$1" = ps ]; then echo proxied-container; else echo 200; fi
                }
                ps() { printf '%s\n' '200 1 python bot.py --skip_tasks'; }
                host_writer_count
            '''],
            cwd=self.source_root,
            env={
                **os.environ,
                'POLYBOT_SOURCE_ONLY': '1',
                'POLYBOT_PLATFORM_OVERRIDE': 'Linux',
                'DOCKER_HOST': 'unix:///tmp/proxied-docker.sock',
            },
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '1')

    def _backup_process(
        self,
        directory: Path,
        *,
        bot_running: bool,
        database_running: bool,
        mode: str,
        cleanup_failure: str = '',
    ) -> subprocess.Popen:
        state = directory / 'state'
        state.mkdir()
        (state / 'bot').write_text(
            str(bot_running).lower(), encoding='utf-8'
        )
        (state / 'postgres').write_text(
            str(database_running).lower(), encoding='utf-8'
        )
        environment = {
            **os.environ,
            'POLYBOT_SOURCE_ONLY': '1',
            'BACKUP_TEST_STATE': str(state),
            'BACKUP_TEST_MARKER': str(directory / 'backup-started'),
            'BACKUP_TEST_MODE': mode,
            'BACKUP_TEST_CLEANUP_FAILURE': cleanup_failure,
        }
        command = r'''
            . ./polybot
            require_engine() { :; }
            validate_project_name() { :; }
            require_prepared_inputs() { :; }
            host_private_input_probe() { :; }
            confirm_exact() { :; }
            configured_checkpoint() { printf '%040d\n' 0 | tr 0 a; }
            bot_container_id() { echo bot; }
            postgres_container_id() { echo postgres; }
            container_running() {
              [ -f "$BACKUP_TEST_STATE/$1" ] \
                && [ "$(sed -n '1p' "$BACKUP_TEST_STATE/$1")" = true ]
            }
            docker() {
              if [ "$1" = exec ]; then printf '%040d\n' 0 | tr 0 a; fi
            }
            compose() {
              case " $* " in
                *" --no-deps "*)
                  echo 'confirmation: CONFIRM'
                  return 0
                  ;;
                *" POLYBOT_BACKUP_CONFIRMATION=CONFIRM "*)
                  echo true >"$BACKUP_TEST_STATE/postgres"
                  : >"$BACKUP_TEST_MARKER"
                  case "$BACKUP_TEST_MODE" in
                    block) while :; do sleep 1; done ;;
                    fail) return 37 ;;
                    success) return 0 ;;
                  esac
                  ;;
                *" start postgres "*)
                  echo true >"$BACKUP_TEST_STATE/postgres"
                  ;;
                *" stop postgres "*)
                  echo false >"$BACKUP_TEST_STATE/postgres"
                  ;;
                *" start bot "*)
                  if [ "$BACKUP_TEST_CLEANUP_FAILURE" = bot ]; then
                    return 55
                  fi
                  echo true >"$BACKUP_TEST_STATE/bot"
                  ;;
                *" stop bot "*)
                  echo false >"$BACKUP_TEST_STATE/bot"
                  ;;
              esac
            }
            command_backup
        '''
        return subprocess.Popen(
            ['/bin/sh', '-c', command],
            cwd=self.source_root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def test_interrupted_backup_restores_running_and_stopped_states(self):
        for bot_running, database_running in (
            (True, True),
            (False, True),
            (False, False),
        ):
            with self.subTest(
                bot_running=bot_running,
                database_running=database_running,
            ), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                process = self._backup_process(
                    root,
                    bot_running=bot_running,
                    database_running=database_running,
                    mode='block',
                )
                marker = root / 'backup-started'
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(marker.exists(), 'backup never reached blocking job')
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=10)

                self.assertEqual(process.returncode, 143, (stdout, stderr))
                self.assertEqual(
                    (root / 'state/bot').read_text(encoding='utf-8').strip(),
                    str(bot_running).lower(),
                )
                self.assertEqual(
                    (root / 'state/postgres').read_text(encoding='utf-8').strip(),
                    str(database_running).lower(),
                )

    def test_backup_preserves_failure_status_after_successful_restoration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = self._backup_process(
                root,
                bot_running=True,
                database_running=True,
                mode='fail',
            )
            stdout, stderr = process.communicate(timeout=10)

            self.assertEqual(process.returncode, 37, (stdout, stderr))
            self.assertEqual(
                (root / 'state/bot').read_text(encoding='utf-8').strip(),
                'true',
            )
            self.assertEqual(
                (root / 'state/postgres').read_text(encoding='utf-8').strip(),
                'true',
            )
            self.assertIn('prior bot and database state was restored', stderr)

    def test_backup_cleanup_failure_is_distinct_and_never_claims_restoration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = self._backup_process(
                root,
                bot_running=True,
                database_running=True,
                mode='success',
                cleanup_failure='bot',
            )
            stdout, stderr = process.communicate(timeout=10)

            self.assertEqual(process.returncode, 3, (stdout, stderr))
            self.assertIn('prior service state was not fully restored', stderr)
            self.assertNotIn('state was restored', stdout)

    def test_backup_failure_status_wins_when_cleanup_also_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = self._backup_process(
                root,
                bot_running=True,
                database_running=True,
                mode='fail',
                cleanup_failure='bot',
            )
            stdout, stderr = process.communicate(timeout=10)

            self.assertEqual(process.returncode, 37, (stdout, stderr))
            self.assertIn('prior service state was not fully restored', stderr)
            self.assertNotIn('state was restored', stdout)

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
