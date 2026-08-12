import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HELPER_SOURCE = PROJECT_ROOT / 'scripts/setup_development_worktree.sh'

FAKE_INTERPRETER = '''#!/bin/sh
if [ "${POLYBOT_ENV:-}" != "development" ]; then
    exit 2
fi

case "${FAKE_PROFILE_MODE:-good}" in
good)
    printf '%s\\n' \\
        'environment: development' \\
        'database: polytopia_dev' \\
        'background tasks enabled: False' \\
        'HTTP API enabled: False'
    ;;
bad_environment)
    printf '%s\\n' \\
        'environment: production' \\
        'database: polytopia_dev' \\
        'background tasks enabled: False' \\
        'HTTP API enabled: False'
    ;;
bad_database)
    printf '%s\\n' \\
        'environment: development' \\
        'database: polytopia2' \\
        'background tasks enabled: False' \\
        'HTTP API enabled: False'
    ;;
bad_background)
    printf '%s\\n' \\
        'environment: development' \\
        'database: polytopia_dev' \\
        'background tasks enabled: True' \\
        'HTTP API enabled: False'
    ;;
bad_api)
    printf '%s\\n' \\
        'environment: development' \\
        'database: polytopia_dev' \\
        'background tasks enabled: False' \\
        'HTTP API enabled: True'
    ;;
*)
    exit 3
    ;;
esac
'''


class SetupDevelopmentWorktreeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.primary = self.root / 'PolyBot39-dev'
        self.production = self.root / 'PolyBot39'
        self.target = self.root / 'task-worktree'
        self.caller = self.root / 'unrelated-caller'
        self.helper = self.primary / 'scripts/setup_development_worktree.sh'

        (self.primary / 'scripts').mkdir(parents=True)
        (self.primary / '.venv/bin').mkdir(parents=True)
        self.production.mkdir()
        (self.target / 'scripts').mkdir(parents=True)
        self.caller.mkdir()

        shutil.copyfile(HELPER_SOURCE, self.helper)
        self.helper.chmod(0o755)
        (self.primary / 'config.development.ini').write_text(
            '[DEFAULT]\npsql_user = polybot_dev\n', encoding='utf-8'
        )
        (self.primary / 'server_settings_dev.py').write_text(
            'server_shortcut_ids = {}\n', encoding='utf-8'
        )
        (self.primary / 'scripts/check_runtime_config.py').write_text(
            '# placeholder; the test interpreter supplies offline output\n',
            encoding='utf-8',
        )
        (self.target / 'scripts/check_runtime_config.py').write_text(
            '# placeholder; the test interpreter supplies offline output\n',
            encoding='utf-8',
        )

    def tearDown(self):
        self.temp.cleanup()

    def install_interpreter(self, *, executable=True):
        interpreter = self.primary / '.venv/bin/python'
        interpreter.write_text(FAKE_INTERPRETER, encoding='utf-8')
        interpreter.chmod(0o755 if executable else 0o644)
        return interpreter

    def run_helper(self, target, *, cwd=None, **environment):
        child_environment = os.environ.copy()
        child_environment.pop('POLYBOT_ENV', None)
        child_environment.update({
            'HOME': str(self.root / 'wrong-home'),
            'FAKE_PROFILE_MODE': 'good',
        })
        child_environment.update(environment)
        return subprocess.run(
            [str(self.helper), str(target)],
            cwd=str(cwd or self.caller),
            env=child_environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def assert_refused(self, result, message):
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(message, result.stderr)

    def test_derives_physical_primary_and_creates_idempotent_links(self):
        interpreter = self.install_interpreter()
        target_alias = self.root / 'task-worktree-alias'
        target_alias.symlink_to(self.target, target_is_directory=True)

        first = self.run_helper(target_alias)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn(f'Shared interpreter: {interpreter}', first.stdout)

        for filename in ('config.development.ini', 'server_settings_dev.py'):
            link = self.target / filename
            self.assertTrue(link.is_symlink())
            self.assertEqual(os.readlink(link), str(self.primary / filename))

        second = self.run_helper(target_alias)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn(f'Development worktree ready: {self.target}', second.stdout)
        self.assertEqual(
            [os.readlink(self.target / name)
             for name in ('config.development.ini', 'server_settings_dev.py')],
            [str(self.primary / name)
             for name in ('config.development.ini', 'server_settings_dev.py')],
        )

    def test_wrong_existing_symlink_is_refused_without_creating_other_link(self):
        self.install_interpreter()
        wrong_source = self.root / 'wrong-config.ini'
        wrong_source.write_text('not authoritative\n', encoding='utf-8')
        (self.target / 'config.development.ini').symlink_to(wrong_source)

        result = self.run_helper(self.target)

        self.assert_refused(result, 'existing symlink has an unexpected target')
        self.assertFalse((self.target / 'server_settings_dev.py').exists())
        self.assertEqual(
            os.readlink(self.target / 'config.development.ini'),
            str(wrong_source),
        )

    def test_existing_regular_target_is_refused_without_overwrite(self):
        self.install_interpreter()
        regular_target = self.target / 'config.development.ini'
        regular_target.write_text('keep this file\n', encoding='utf-8')

        result = self.run_helper(self.target)

        self.assert_refused(result, 'refusing to overwrite existing path')
        self.assertEqual(regular_target.read_text(encoding='utf-8'), 'keep this file\n')
        self.assertFalse((self.target / 'server_settings_dev.py').exists())

    def test_production_checkout_and_physical_descendants_are_refused(self):
        self.install_interpreter()
        descendant = self.production / 'nested/worktree'
        descendant.mkdir(parents=True)
        production_alias = self.root / 'production-alias'
        production_alias.symlink_to(descendant, target_is_directory=True)

        for target in (self.production, descendant, production_alias):
            with self.subTest(target=target):
                result = self.run_helper(target)
                self.assert_refused(
                    result,
                    'production checkout and descendants are never valid targets',
                )

    def test_missing_and_non_executable_shared_interpreter_are_refused(self):
        missing = self.run_helper(self.target)
        self.assert_refused(missing, 'shared development interpreter is unavailable')

        interpreter = self.install_interpreter(executable=False)
        non_executable = self.run_helper(self.target)
        self.assert_refused(
            non_executable,
            'shared development interpreter is unavailable',
        )
        self.assertEqual(interpreter.stat().st_mode & 0o111, 0)

    def test_profile_identity_failures_remain_fail_closed(self):
        self.install_interpreter()
        cases = (
            ('bad_environment', 'runtime profile is not development'),
            ('bad_database', 'runtime database is not polytopia_dev'),
            ('bad_background', 'development background tasks are enabled'),
            ('bad_api', 'development API is enabled'),
        )
        for mode, message in cases:
            with self.subTest(mode=mode):
                result = self.run_helper(
                    self.primary,
                    FAKE_PROFILE_MODE=mode,
                )
                self.assert_refused(result, message)

        (self.primary / 'config.development.ini').write_text(
            '[DEFAULT]\npsql_user = wrong_role\n', encoding='utf-8'
        )
        wrong_role = self.run_helper(self.primary)
        self.assert_refused(wrong_role, 'runtime database role is not polybot_dev')

    def test_target_must_exist_and_helper_must_be_invoked_absolutely(self):
        self.install_interpreter()
        missing_target = self.run_helper(self.root / 'does-not-exist')
        self.assert_refused(missing_target, 'target is not a directory')

        relative_helper = os.path.relpath(self.helper, self.caller)
        relative_invocation = subprocess.run(
            [relative_helper, str(self.target)],
            cwd=str(self.caller),
            env={**os.environ, 'HOME': str(self.root / 'wrong-home')},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assert_refused(relative_invocation, 'helper must be invoked by an absolute path')


if __name__ == '__main__':
    unittest.main()
