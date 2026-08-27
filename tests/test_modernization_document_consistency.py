"""Model-free consistency checks for current modernization authority."""

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


def _read(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding='utf-8')


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    next_heading = text.find('\n## ', start + len(heading))
    return text[start:] if next_heading == -1 else text[start:next_heading]


class ModernizationDocumentConsistencyTests(unittest.TestCase):
    def test_current_source_compatibility_and_status_records_agree(self):
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

        taxonomy = _read('docs/SLASH_COMMAND_TAXONOMY_REVIEW.md')
        taxonomy_current = _section(taxonomy, '## Current implementation alignment')
        taxonomy_inventory = taxonomy_current.split(
            'Their current first-level structure is:', 1
        )[0]
        self.assertEqual(
            tuple(re.findall(r'`([^`]+)`', taxonomy_inventory)),
            EXPECTED_ROOTS,
        )

        readiness = _read('docs/MODERNIZATION_PRODUCTION_READINESS_AUDIT.md')
        readiness_current = _section(
            readiness,
            '## Final reconciliation within this historical audit',
        )
        self.assertEqual(
            tuple(re.findall(r'`([^`]+)`', readiness_current))[:len(EXPECTED_ROOTS)],
            EXPECTED_ROOTS,
        )
        self.assertIn('M1–M6 and L1 are resolved', readiness_current)

        roadmap = _read('docs/DATABASE_AND_SLASH_MODERNIZATION.md')
        self.assertIn('| P9 | Complete |', roadmap)
        self.assertIn(
            'Current active unit: **None. The modernization release is '
            'integrated and',
            roadmap,
        )
        self.assertNotIn('command source currently loads ten roots', roadmap)
        compatibility_rows = {
            row.split(' ', 2)[1]: row
            for row in roadmap.splitlines()
            if row.startswith('| C-')
        }
        self.assertIn('stall was corrected', compatibility_rows['C-012'])
        self.assertIn('correction was validated', compatibility_rows['C-013'])
        self.assertIn('integrated/deployed at `41da49e`', compatibility_rows['C-025'])
        self.assertNotIn('pending correction', compatibility_rows['C-012'])
        self.assertNotIn('implementation pending', compatibility_rows['C-025'])
        self.assertIn(
            'Discord-triggered backup surface is retired',
            compatibility_rows['C-029'],
        )
        self.assertIn(
            'full retirement implemented/deployed at `9dd701e9`',
            compatibility_rows['C-029'],
        )
        self.assertIn(
            'The immediate modernization action at this historical checkpoint\n'
            'was P9.7a',
            roadmap,
        )

        review = _read('docs/MODERNIZATION_PRE_PRODUCTION_REVIEW.md')
        self.assertIn('| M1–M5 | Resolved |', review)
        self.assertIn('| M6 | Resolved |', review)
        self.assertIn('| M7 | In progress |', review)
        self.assertIn('| L1 | Resolved |', review)
        self.assertIn('| N1 import/startup schema DDL | High |', review)
        self.assertIn(
            '| N2 production identity/password literal fallback | Medium |',
            review,
        )
        for finding in ('N3', 'N4', 'N5', 'N6', 'N7'):
            self.assertRegex(review, rf'(?m)^\| {finding} \| Resolved \|')
        self.assertIn('Status: **Resolved by P9.19.**', review)

        cutover = _read('docs/MODERNIZATION_PRODUCTION_CUTOVER.md')
        self.assertIn(
            'application_command_all_guild_capabilities` is exactly empty',
            cutover,
        )
        self.assertIn(
            'Main and PolyChampions retain their live staff-help channels',
            cutover,
        )
        self.assertIn('Production writes no JSONL', compatibility_rows['C-007'])

        taxonomy_alignment = _section(
            taxonomy,
            '## Current implementation alignment',
        )
        self.assertIn(
            '/operator bot|channels|guild|player|tribe',
            taxonomy_alignment,
        )
        self.assertNotIn('/operator bot|channels|database', taxonomy_alignment)

        wrapper = _read('docs/PRODUCTION_RELEASE_WRAPPER.md')
        self.assertIn(
            'historical systemd-era record; do not install or invoke',
            wrapper,
        )
        self.assertIn('[PRODUCTION_DOCKER.md](PRODUCTION_DOCKER.md)', wrapper)

        readme = _read('README.md')
        documentation_map = _read('docs/README.md')
        self.assertIn('[documentation map](docs/README.md)', readme)
        self.assertIn('## Independent self-hosting', documentation_map)
        self.assertIn('## Current upstream operations', documentation_map)
        self.assertIn('## Current engineering references', documentation_map)
        self.assertIn('## Historical migrations and release evidence',
                      documentation_map)

        example_config = _read('config.ini-EXAMPLE')
        self.assertRegex(example_config, r'(?m)^psql_db\s*=\s*polybot$')
        self.assertIn('REPLACE_WITH_YOUR_BOT_USER_ID', example_config)
        self.assertRegex(example_config, r'(?m)^bullet_enabled\s*=\s*false$')
        self.assertNotIn('484067640302764042', example_config)
        self.assertNotIn('polytopia2', example_config)


if __name__ == '__main__':
    unittest.main()
