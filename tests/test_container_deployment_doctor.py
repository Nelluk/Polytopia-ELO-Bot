from __future__ import annotations

from dataclasses import FrozenInstanceError
import io
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from modules import container_deployment_doctor as doctor
from scripts import check_container_deployment as cli


CHECKPOINT = 'a' * 40
ADMIN_PASSWORD = 'admin-only-secret'
APP_PASSWORD = 'application-only-secret'


class ContainerDeploymentDoctorTests(unittest.TestCase):
    source_root = Path(__file__).resolve().parents[1]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(
            self.source_root / 'deploy/container',
            self.root / 'deploy/container',
        )
        self._write_private(
            'deploy/container/config.development.ini',
            self._config(host='postgres'),
        )
        self._write_private(
            'deploy/container/server_settings_dev.py',
            (
                'server_shortcut_ids = {"test": 987654321012345678}\n'
                'application_command_capabilities = {}\n'
                'server_list = {"default": {}, 987654321012345678: {}}\n'
            ),
        )
        self._write_private(
            'deploy/container/secrets/postgres-admin-password.txt',
            ADMIN_PASSWORD + '\n',
        )
        self._write_private(
            'deploy/container/secrets/polybot-database-password.txt',
            APP_PASSWORD + '\n',
        )
        env = (
            self.source_root / 'deploy/container/development.env.example'
        ).read_text(encoding='utf-8').replace(
            'REPLACE_WITH_EXACT_CLEAN_GIT_HEAD', CHECKPOINT
        )
        (self.root / 'deploy/container/.env').write_text(env, encoding='utf-8')
        backup_directory = self.root / 'deploy/container/backups'
        backup_directory.mkdir()
        backup_directory.chmod(0o700)

    def _write_private(self, relative: str, value: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding='utf-8')
        path.chmod(0o600)
        return path

    @staticmethod
    def _config(*, host: str, password: str = APP_PASSWORD) -> str:
        return f'''[DEFAULT]
guild_configuration_source = database
discord_key = development-discord-token
expected_bot_id = 987654321012345679
owner_id = 987654321012345680
psql_db = polytopia_dev
psql_user = polybot_dev
psql_password = {password}
psql_host = {host}
psql_port = 5432
production_database_name = polytopia_prod
production_bot_id = 987654321012345681
production_guild_ids = 987654321012345682
background_tasks_enabled = false
api_enabled = false
bullet_enabled = false
image_root = data/development/images
log_root = logs/development
'''

    @staticmethod
    def _git(_root: Path, *, clean: bool = True) -> doctor.GitSnapshot:
        return doctor.GitSnapshot(checkpoint=CHECKPOINT, clean=clean)

    @staticmethod
    def _docker_only(name: str) -> str | None:
        return '/usr/bin/docker' if name == 'docker' else None

    def test_bundled_ready_report_is_read_only_and_never_discloses_secrets(self):
        looked_up: list[str] = []

        def which(name: str) -> str | None:
            looked_up.append(name)
            return self._docker_only(name)

        report = doctor.run_doctor(
            self.root,
            mode='bundled',
            which=which,
            git_probe=self._git,
        )

        self.assertTrue(report.ready)
        self.assertEqual(looked_up, ['docker', 'docker-compose'])
        rendered = doctor.format_report(report)
        machine = doctor.report_json(report)
        for secret in (ADMIN_PASSWORD, APP_PASSWORD, 'development-discord-token'):
            self.assertNotIn(secret, rendered)
            self.assertNotIn(secret, machine)
        self.assertIn('[WARN] compose-plugin', rendered)
        self.assertIn('--env-file deploy/container/.env', rendered)
        self.assertIn('database-provision', rendered)
        self.assertIn('recovery assets match the reviewed', rendered)
        self.assertIn('Off-volume backup directory', rendered)
        with self.assertRaises(FrozenInstanceError):
            report.mode = 'external'

    def test_missing_engine_blocks_but_still_prints_reviewed_commands(self):
        report = doctor.run_doctor(
            self.root,
            mode='bundled',
            which=lambda _name: None,
            git_probe=self._git,
        )
        self.assertFalse(report.ready)
        self.assertTrue(any(
            item.key == 'docker-cli' and item.status == doctor.BLOCK
            for item in report.findings
        ))
        self.assertTrue(report.commands)
        self.assertTrue(all(command.startswith('docker compose ') for command in report.commands))

    def test_password_mismatch_and_unsafe_secret_shape_fail_without_disclosure(self):
        app_path = self._write_private(
            'deploy/container/secrets/polybot-database-password.txt',
            'different-secret\nsecond-line\n',
        )
        app_path.chmod(0o644)
        report = doctor.run_doctor(
            self.root,
            mode='bundled',
            which=self._docker_only,
            git_probe=self._git,
        )
        self.assertFalse(report.ready)
        rendered = doctor.format_report(report)
        self.assertNotIn('different-secret', rendered)
        self.assertNotIn('second-line', rendered)
        blocked = {item.key for item in report.findings if item.status == doctor.BLOCK}
        self.assertIn('database-app-secret', blocked)
        self.assertIn('secret-agreement', blocked)

    def test_dirty_or_mismatched_checkpoint_blocks_image_provenance(self):
        env_path = self.root / 'deploy/container/.env'
        env_path.write_text(
            env_path.read_text(encoding='utf-8').replace(CHECKPOINT, 'b' * 40),
            encoding='utf-8',
        )
        report = doctor.run_doctor(
            self.root,
            mode='bundled',
            which=self._docker_only,
            git_probe=lambda root: self._git(root, clean=False),
        )
        blocked = {item.key for item in report.findings if item.status == doctor.BLOCK}
        self.assertEqual({'git-checkpoint', 'compose-env'}, blocked)

    def test_external_mode_omits_database_service_and_bundle_secrets(self):
        self._write_private(
            'deploy/container/config.development.ini',
            self._config(host='db.internal.example'),
        )
        shutil.rmtree(self.root / 'deploy/container/secrets')
        env_path = self.root / 'deploy/container/.env'
        env_path.write_text(
            '\n'.join(
                line for line in env_path.read_text(encoding='utf-8').splitlines()
                if not line.startswith(('POLYBOT_RECOVERY_UID=', 'POLYBOT_RECOVERY_GID='))
            ) + '\n',
            encoding='utf-8',
        )
        report = doctor.run_doctor(
            self.root,
            mode='external',
            which=self._docker_only,
            git_probe=self._git,
        )
        self.assertTrue(report.ready)
        keys = {item.key for item in report.findings}
        self.assertNotIn('postgres-admin-secret', keys)
        self.assertNotIn('database-app-secret', keys)
        rendered = '\n'.join(report.commands)
        self.assertNotIn('database-provision', rendered)
        self.assertNotIn('up -d postgres', rendered)

    def test_external_mode_rejects_bundle_or_host_loopback_address(self):
        for host in ('postgres', 'localhost', '127.0.0.1'):
            with self.subTest(host=host):
                self._write_private(
                    'deploy/container/config.development.ini',
                    self._config(host=host),
                )
                report = doctor.run_doctor(
                    self.root,
                    mode='external',
                    which=self._docker_only,
                    git_probe=self._git,
                )
                self.assertFalse(report.ready)
                self.assertTrue(any(
                    item.key == 'development-profile'
                    and item.status == doctor.BLOCK
                    for item in report.findings
                ))

    def test_asset_drift_and_unsupported_env_key_fail_closed(self):
        compose = self.root / 'deploy/container/compose.development.yaml'
        compose.write_text(
            compose.read_text(encoding='utf-8') + '\n# --apply\n',
            encoding='utf-8',
        )
        env_path = self.root / 'deploy/container/.env'
        env_path.write_text(
            env_path.read_text(encoding='utf-8') + 'UNREVIEWED=value\n',
            encoding='utf-8',
        )
        self._write_private(
            'deploy/container/server_settings_dev.py',
            (
                'server_shortcut_ids = {}\n'
                'application_command_capabilities = {}\n'
                'server_list = 42\n'
            ),
        )
        report = doctor.run_doctor(
            self.root,
            mode='bundled',
            which=self._docker_only,
            git_probe=self._git,
        )
        blocked = {item.key for item in report.findings if item.status == doctor.BLOCK}
        self.assertIn('repository-assets', blocked)
        self.assertIn('compose-env', blocked)
        self.assertIn('server-settings', blocked)

    def test_recovery_uid_and_gid_must_be_positive_integers(self):
        env_path = self.root / 'deploy/container/.env'
        env_path.write_text(
            env_path.read_text(encoding='utf-8').replace(
                'POLYBOT_RECOVERY_UID=1000',
                'POLYBOT_RECOVERY_UID=0',
            ).replace(
                'POLYBOT_RECOVERY_GID=1000',
                'POLYBOT_RECOVERY_GID=operator',
            ),
            encoding='utf-8',
        )
        report = doctor.run_doctor(
            self.root,
            mode='bundled',
            which=self._docker_only,
            git_probe=self._git,
        )
        self.assertFalse(report.ready)
        finding = next(item for item in report.findings if item.key == 'compose-env')
        self.assertIn('POLYBOT_RECOVERY_UID', finding.message)
        self.assertIn('POLYBOT_RECOVERY_GID', finding.message)

    def test_backup_directory_must_match_recovery_identity_and_mode(self):
        backup_directory = self.root / 'deploy/container/backups'
        backup_directory.chmod(0o755)
        report = doctor.run_doctor(
            self.root,
            mode='bundled',
            which=self._docker_only,
            git_probe=self._git,
        )
        finding = next(
            item for item in report.findings if item.key == 'backup-directory'
        )
        self.assertEqual(finding.status, doctor.BLOCK)
        self.assertNotIn(str(backup_directory.resolve()), '\n'.join(report.commands))

    def test_invalid_contract_refuses_before_any_engine_probe(self):
        contract = self.root / 'deploy/container/container-contract.toml'
        contract.write_text(
            contract.read_text(encoding='utf-8').replace(
                'environment = "development"',
                'environment = "production"',
            ),
            encoding='utf-8',
        )
        looked_up: list[str] = []
        with self.assertRaises(doctor.ContainerDoctorError):
            doctor.run_doctor(
                self.root,
                mode='bundled',
                which=lambda name: looked_up.append(name),
                git_probe=self._git,
            )
        self.assertEqual(looked_up, [])

    def test_source_and_cli_do_not_import_or_invoke_external_clients(self):
        source = (
            self.source_root / 'modules/container_deployment_doctor.py'
        ).read_text(encoding='utf-8')
        script = (
            self.source_root / 'scripts/check_container_deployment.py'
        ).read_text(encoding='utf-8')
        self.assertNotIn('import discord', source)
        self.assertNotIn('psycopg', source)
        self.assertNotIn("subprocess.run(('docker'", source)
        self.assertIn("('git', *arguments)", source)
        self.assertIn("'GIT_OPTIONAL_LOCKS': '0'", source)
        self.assertIn('Never invokes Docker', script)
        self.assertIn('sys.dont_write_bytecode = True', script)
        self.assertIn("POLYBOT_ENV') != 'development'", script)

    def test_cli_requires_exact_development_environment_before_inspection(self):
        for environment in ({}, {'POLYBOT_ENV': 'production'}):
            with self.subTest(environment=environment), mock.patch.dict(
                os.environ,
                environment,
                clear=True,
            ), mock.patch.object(cli, 'run_doctor') as run_doctor, mock.patch(
                'sys.stderr',
                new_callable=io.StringIO,
            ) as stderr:
                self.assertEqual(cli.main(['--mode', 'bundled']), 2)
                run_doctor.assert_not_called()
                self.assertIn('exactly development', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
