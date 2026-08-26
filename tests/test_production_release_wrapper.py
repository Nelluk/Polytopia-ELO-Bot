"""Offline safety checks for the constrained production-release wrapper."""

from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ProductionReleaseWrapperTests(unittest.TestCase):
    def test_shell_assets_are_syntactically_valid(self):
        for relative in (
            'deploy/polyelo-release',
            'scripts/production_release.sh',
            'scripts/install_polyelo_release.sh',
        ):
            result = subprocess.run(
                ['/usr/bin/bash', '-n', str(ROOT / relative)],
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_sudoers_rule_is_exact_and_accepts_no_arguments(self):
        source = ROOT / 'deploy/sudoers/polyelo-release'
        result = subprocess.run(
            ['/usr/sbin/visudo', '-cf', str(source)],
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            tuple(
                line for line in source.read_text(encoding='utf-8').splitlines()
                if line and not line.startswith('#')
            ),
            (
                'Cmnd_Alias POLYELO_RELEASE = '
                '/srv/polyelo/bin/polyelo-release ""',
                'nelluk ALL=(root) NOPASSWD: POLYELO_RELEASE',
            ),
        )

    def test_root_wrapper_has_fixed_boundaries(self):
        source = (ROOT / 'deploy/polyelo-release').read_text(encoding='utf-8')
        self.assertIn('if (( $# != 0 ))', source)
        self.assertIn('if (( EUID != 0 ))', source)
        self.assertIn('/usr/bin/flock -n 9', source)
        self.assertIn('/usr/sbin/runuser -u nelluk', source)
        self.assertIn('symbolic-ref --quiet --short HEAD', source)
        self.assertIn('status --porcelain=v1 --untracked-files=normal', source)
        self.assertIn('/usr/bin/systemctl stop "$SERVICE"', source)
        self.assertIn('/usr/sbin/runuser -u polyelo', source)
        self.assertIn('/usr/bin/systemctl start "$SERVICE"', source)
        self.assertNotIn('eval ', source)
        self.assertNotIn('$@', source)

    def test_unprivileged_runner_owns_only_schema_and_fixed_guild_sync(self):
        source = (ROOT / 'scripts/production_release.sh').read_text(
            encoding='utf-8'
        )
        self.assertIn('[[ $(/usr/bin/id -un) == polyelo ]]', source)
        self.assertIn('migrate_player_timezone_production.py', source)
        self.assertIn('migrate_player_badges_production.py', source)
        self.assertIn('migrate_game_keep_active_production.py', source)
        self.assertIn('--mode apply', source)
        self.assertIn(
            '--guild-ids 283436219780825088,447883341463814144', source
        )
        self.assertIn('--confirm-no-global-sync', source)
        self.assertNotIn('systemctl', source)
        self.assertNotIn('sudo ', source)
        self.assertNotIn('eval ', source)
        self.assertNotIn('$@', source)

    def test_installer_uses_root_owned_modes_and_full_sudoers_validation(self):
        source = (ROOT / 'scripts/install_polyelo_release.sh').read_text(
            encoding='utf-8'
        )
        self.assertIn('-o root -g root -m 0755', source)
        self.assertIn('-o root -g root -m 0440', source)
        self.assertIn('/usr/sbin/visudo -cf /etc/sudoers', source)
        self.assertIn('/usr/bin/cmp --silent', source)


if __name__ == '__main__':
    unittest.main()
