"""Model-free consistency checks for the current engineering contract."""

import json
import os
from pathlib import Path
import re
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
RETIRED_HEAD_PATHS = (
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


def _read(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding='utf-8')


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    next_heading = text.find('\n## ', start + len(heading))
    return text[start:] if next_heading == -1 else text[start:next_heading]


class ModernizationDocumentConsistencyTests(unittest.TestCase):
    def test_current_source_and_engineering_contract_agree(self):
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
                'print(json.dumps(sorted(command.name for command in commands))); '
                'asyncio.run(client.close())',
            ),
            cwd=PROJECT_ROOT,
            env={**os.environ, 'POLYBOT_ENV': 'development'},
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        loaded_roots = tuple(json.loads(command_probe.stdout.splitlines()[0]))
        self.assertEqual(loaded_roots, EXPECTED_ROOTS)

        contract = _read('docs/DATABASE_AND_SLASH_MODERNIZATION.md')
        command_section = _section(contract, '## Discord command contract')
        root_inventory = command_section.split(
            'Current first-level structure:', 1
        )[0]
        documented_roots = tuple(
            re.findall(r'(?m)^- `([^`]+)`$', root_inventory)
        )
        self.assertEqual(documented_roots, EXPECTED_ROOTS)
        self.assertIn(
            '/operator bot|channels|guild|player|tribe',
            command_section,
        )
        self.assertNotIn('/operator database', command_section)
        self.assertIn('There is no active modernization unit.', contract)
        self.assertIn('pre-cleanup checkpoint\n`e99ec18e`', contract)

    def test_current_tree_excludes_retired_release_interfaces(self):
        for relative_path in RETIRED_HEAD_PATHS:
            with self.subTest(relative_path=relative_path):
                self.assertFalse((PROJECT_ROOT / relative_path).exists())

        documentation_map = _read('docs/README.md')
        self.assertIn('## Independent self-hosting', documentation_map)
        self.assertIn('## Current upstream operations', documentation_map)
        self.assertIn('## Current engineering references', documentation_map)
        self.assertIn('## Git history', documentation_map)
        self.assertIn('e99ec18e', documentation_map)

        readme = _read('README.md')
        self.assertIn('[documentation map](docs/README.md)', readme)

    def test_public_example_remains_installation_neutral(self):
        example_config = _read('config.ini-EXAMPLE')
        self.assertRegex(example_config, r'(?m)^psql_db\s*=\s*polybot$')
        self.assertIn('REPLACE_WITH_YOUR_BOT_USER_ID', example_config)
        self.assertRegex(example_config, r'(?m)^bullet_enabled\s*=\s*false$')
        self.assertNotIn('484067640302764042', example_config)
        self.assertNotIn('polytopia2', example_config)


if __name__ == '__main__':
    unittest.main()
