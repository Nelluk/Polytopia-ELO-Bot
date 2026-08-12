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
    'whattotest',
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
                'import asyncio, json; '
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
        readiness_current = _section(readiness, '## Current reconciliation')
        self.assertEqual(
            tuple(re.findall(r'`([^`]+)`', readiness_current))[:len(EXPECTED_ROOTS)],
            EXPECTED_ROOTS,
        )
        self.assertIn('M1–M6 and L1 are resolved', readiness_current)

        roadmap = _read('docs/DATABASE_AND_SLASH_MODERNIZATION.md')
        self.assertIn('| P9 | In progress |', roadmap)
        self.assertIn(
            'Current active unit: **P11.8 current-accumulation Mac beta refresh',
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
        self.assertIn('Status: **Resolved by P9.19.**', review)

        cutover = _read('docs/MODERNIZATION_PRODUCTION_CUTOVER.md')
        self.assertIn(
            "application_command_all_guild_capabilities` is exactly\n"
            "  `('tools_support',)`",
            cutover,
        )
        self.assertIn(
            "every allowlisted guild has a valid `staff_help_channel`",
            cutover,
        )
        self.assertIn('Production writes no JSONL', compatibility_rows['C-007'])

        example_config = _read('config.ini-EXAMPLE')
        self.assertRegex(example_config, r'(?m)^psql_db\s*=\s*polytopia2$')
        self.assertRegex(
            example_config,
            r'(?m)^expected_bot_id\s*=\s*484067640302764042$',
        )
        self.assertRegex(example_config, r'(?m)^psql_password\s*=\s*CHANGEME$')


if __name__ == '__main__':
    unittest.main()
