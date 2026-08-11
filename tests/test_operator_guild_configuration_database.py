"""Explicitly gated read-only P10.6a development control-plane verification."""

from __future__ import annotations

import json
import os
from pathlib import Path
import unittest

import settings
from modules import guild_configuration_runtime as runtime
from modules import guild_configuration_shadow as shadow
from modules import operator_guild_configuration_workers as workers
from runtime_config import load_runtime_profile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INTEGRATION_FLAG = 'POLYBOT_P10_6A_CONTROL_INTEGRATION'
SNAPSHOT_ENV = 'POLYBOT_DEVELOPMENT_GUILD_CONFIGURATION_SNAPSHOT'


@unittest.skipUnless(
    os.environ.get(INTEGRATION_FLAG) == '1',
    f'{INTEGRATION_FLAG}=1 is required for P10.6a database verification',
)
class OperatorGuildConfigurationDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_owner_read_surfaces_match_the_current_stored_graph(self):
        self.assertEqual(os.environ.get('POLYBOT_ENV'), 'development')
        profile = load_runtime_profile(
            project_root=PROJECT_ROOT,
            environ=os.environ,
            create_directories=False,
        )
        self.assertEqual(profile.guild_configuration_source, 'database')
        snapshot_value = os.environ.get(SNAPSHOT_ENV, '').strip()
        self.assertTrue(snapshot_value, f'{SNAPSHOT_ENV} must name the reviewed snapshot')
        snapshot_path = Path(snapshot_value)
        self.assertTrue(snapshot_path.is_absolute())
        self.assertTrue(snapshot_path.is_file())
        discord_snapshot = json.loads(snapshot_path.read_text(encoding='utf-8'))

        bundle = shadow.expected_bundle_from_snapshot(
            profile=profile,
            discord_snapshot=discord_snapshot,
        )
        stored = await shadow.run_shadow_comparison(
            shadow.request_from_profile(
                profile=profile,
                expected_bundle=bundle,
            )
        )
        published = runtime.build_runtime_snapshot(
            result=stored,
            discord_snapshot=discord_snapshot,
            allowed_guild_ids=profile.allowed_guild_ids,
        )
        guild_id = profile.allowed_guild_ids[0]
        runtime_record = published.guilds[guild_id]

        results = {}
        for operation in (
                workers.LIST,
                workers.SETTINGS,
                workers.VALIDATE,
                workers.HISTORY):
            results[operation] = await workers.run_read(
                workers.request_from_profile(
                    profile=profile,
                    requester_id=int(settings.owner_id),
                    guild_id=guild_id,
                    operation=operation,
                    runtime_record=runtime_record,
                    discord_snapshot=(
                        discord_snapshot if operation == workers.VALIDATE else None
                    ),
                )
            )

        self.assertEqual(
            tuple(record.guild_id for record in results[workers.LIST].records),
            profile.allowed_guild_ids,
        )
        self.assertEqual(
            results[workers.SETTINGS].selected.document_digest,
            runtime_record.document_digest,
        )
        self.assertTrue(
            results[workers.VALIDATE].validation.running_snapshot_current
        )
        self.assertTrue(results[workers.VALIDATE].validation.live_references_valid)
        self.assertGreaterEqual(len(results[workers.HISTORY].revisions), 1)
        self.assertGreaterEqual(len(results[workers.HISTORY].audits), 1)


if __name__ == '__main__':
    unittest.main()
