"""Model-free checks for the current repository and command surface."""

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXPECTED_ROOTS = (
    'elo',
    'game',
    'guild',
    'house',
    'leaderboard',
    'league',
    'operator',
    'player',
    'squad',
    'staffhelp',
    'team',
)
EXPECTED_OPERATOR_CHILDREN = ('bot', 'channels', 'guild', 'player', 'tribe')
RETIRED_RELEASE_PATHS = (
    'deploy/polyelo-release',
    'deploy/sudoers/polyelo-release',
    'deploy/systemd/polyelo.service',
    'deploy/systemd/polyelo-modernization-canary.conf',
    'deploy/systemd/polytopia.service',
    'deploy/systemd/polytopia-modernization-canary.conf',
    'modules/release_candidate.py',
    'scripts/install_polyelo_release.sh',
    'scripts/manage_release_candidate.py',
    'scripts/production_release.sh',
)
RETIRED_MILESTONE_DOCUMENTS = (
    'docs/DATABASE_AND_SLASH_MODERNIZATION.md',
    'docs/DYNAMIC_GUILD_CONFIGURATION_DESIGN.md',
    'docs/DEVELOPMENT_GUILD_COMMAND_CAPABILITIES.md',
    'docs/DEVELOPMENT_GUILD_CONFIGURATION_AUTHORITY.md',
    'docs/DEVELOPMENT_GUILD_CONFIGURATION_CONTROL.md',
    'docs/DEVELOPMENT_GUILD_CONFIGURATION_DELEGATION.md',
    'docs/DEVELOPMENT_GUILD_CONFIGURATION_DRAFTS.md',
    'docs/DEVELOPMENT_GUILD_CONFIGURATION_SHADOW.md',
    'docs/DEVELOPMENT_GUILD_CONFIGURATION_STORAGE.md',
    'docs/DEVELOPMENT_GUILD_LIFECYCLE.md',
    'docs/DEVELOPMENT_GUILD_ONBOARDING.md',
    'docs/GAME_KEEP_ACTIVE_MIGRATION.md',
    'docs/PLAYER_BADGES_MIGRATION.md',
    'docs/PRODUCTION_TIMEZONE_MIGRATION.md',
)
RETIRED_INSTALLATION_DOCUMENTS = (
    'deploy/self-hosting/polybot.service.example',
    'docs/DATABASE_SETUP.md',
    'docs/PRIVACY_READINESS_CHECKLIST.md',
    'docs/PRIVILEGED_INTENT_SCREENSHOT_GUIDE.md',
    'docs/SELF_HOSTING.md',
    'docs/privileged-intent-review',
)


def _read(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding='utf-8')


class RepositoryConsistencyTests(unittest.TestCase):
    def test_background_tasks_wait_for_published_guild_configuration(self):
        for relative_path in (
            'modules/administration.py',
            'modules/games.py',
            'modules/league.py',
            'modules/matchmaking.py',
            'modules/misc.py',
        ):
            with self.subTest(relative_path=relative_path):
                active_lines = (
                    line for line in _read(relative_path).splitlines()
                    if not line.lstrip().startswith('#')
                )
                source = '\n'.join(active_lines)
                self.assertNotIn(
                    'await self.bot.wait_until_ready()',
                    source,
                )

    def test_current_command_source_has_expected_roots_and_operator_groups(self):
        command_probe = subprocess.run(
            (
                sys.executable,
                '-c',
                'import asyncio, json, runtime_config; '
                'from types import SimpleNamespace as NS; '
                'guild=900000000000000001; '
                'server=NS(server_shortcut_ids={"main":guild,'
                '"polychampions":guild,"test":guild},'
                'server_list={"default":{},guild:{}},'
                'application_command_capabilities={},'
                'application_command_all_guild_capabilities=()); '
                'runtime_config._runtime_profile=NS('
                'server_settings=server,discord_token="offline",'
                'database_user="offline",database_name="offline",'
                'database_password="offline",database_host="localhost",'
                'database_port=5432,owner_id=guild,superuser_ids=(guild,),'
                'pastebin_key=None,expected_bot_id=guild,'
                'guild_configuration_source="static",allowed_guild_ids=(guild,),'
                'background_tasks_enabled=False,api_enabled=False,'
                'bullet_enabled=False,environment="development"); '
                'from scripts.manage_application_commands import '
                'load_command_source; '
                'client, commands = load_command_source(); '
                'surface={command.name:sorted('
                'child.name for child in getattr(command,"commands",())) '
                'for command in commands}; '
                'print(json.dumps(surface,sort_keys=True)); '
                'asyncio.run(client.close())',
            ),
            cwd=PROJECT_ROOT,
            env={**os.environ, 'POLYBOT_ENV': 'development'},
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        surface = json.loads(command_probe.stdout.splitlines()[0])
        self.assertEqual(tuple(sorted(surface)), EXPECTED_ROOTS)
        self.assertEqual(tuple(surface['operator']), EXPECTED_OPERATOR_CHILDREN)
        self.assertNotIn('database', surface['operator'])

    def test_current_tree_excludes_retired_release_and_milestone_paths(self):
        for relative_path in (
            RETIRED_RELEASE_PATHS
            + RETIRED_MILESTONE_DOCUMENTS
            + RETIRED_INSTALLATION_DOCUMENTS
        ):
            with self.subTest(relative_path=relative_path):
                self.assertFalse((PROJECT_ROOT / relative_path).exists())

    def test_documentation_map_points_to_current_state_authorities(self):
        documentation_map = _read('docs/README.md')
        self.assertIn('## Independent self-hosting', documentation_map)
        self.assertIn('## Upstream production operations', documentation_map)
        self.assertIn('## Upstream development operations', documentation_map)
        self.assertIn('## Policy and data operations', documentation_map)
        self.assertIn('## Git history', documentation_map)
        self.assertIn('DEVELOPMENT_GUILD_CONFIGURATION.md', documentation_map)
        self.assertIn('a226ade9', documentation_map)
        self.assertIn('e99ec18e', documentation_map)

        guild_configuration = _read('docs/DEVELOPMENT_GUILD_CONFIGURATION.md')
        self.assertIn('Status: **beta operations guide**', guild_configuration)
        self.assertIn('Production continues to', guild_configuration)
        self.assertIn('**Repair commands**', guild_configuration)
        self.assertNotIn('P10.', guild_configuration)

        readme = _read('README.md')
        self.assertIn('[documentation map](docs/README.md)', readme)

    def test_public_example_remains_installation_neutral(self):
        example_config = _read('config.ini-EXAMPLE')
        self.assertRegex(example_config, r'(?m)^psql_db\s*=\s*polybot$')
        self.assertIn('REPLACE_WITH_YOUR_BOT_USER_ID', example_config)
        self.assertRegex(example_config, r'(?m)^bullet_enabled\s*=\s*false$')
        self.assertNotIn('484067640302764042', example_config)
        self.assertNotIn('polytopia2', example_config)

    def test_production_image_excludes_nested_runtime_artifacts(self):
        dockerignore = set(_read('.dockerignore').splitlines())
        self.assertIn('**/__pycache__/', dockerignore)
        self.assertIn('**/*.py[cod]', dockerignore)
        self.assertIn('.operator-backup-release.json', dockerignore)
        self.assertIn('**/.operator-backup-release.json', dockerignore)

    def test_production_guild_runbook_matches_retained_inventory(self):
        runbook = _read('docs/PRODUCTION_GUILD_CONFIGURATION.md')
        self.assertIn('25 allowed guilds', runbook)
        self.assertIn('| Standard | 23 |', runbook)
        self.assertIn('exactly three existing global-leaderboard', runbook)
        self.assertNotIn('49 allowed guilds', runbook)
        self.assertNotIn('49 imported configurations', runbook)


if __name__ == '__main__':
    unittest.main()
