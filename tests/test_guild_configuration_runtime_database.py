"""Explicitly gated read-only P10.5 development authority verification."""

from __future__ import annotations

import json
import os
from pathlib import Path
import unittest

from modules import guild_configuration_runtime as runtime
from modules import guild_configuration_shadow as shadow
from modules import guild_configuration_storage as storage
from runtime_config import load_runtime_profile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INTEGRATION_FLAG = 'POLYBOT_P10_5_AUTHORITY_INTEGRATION'
SNAPSHOT_ENV = 'POLYBOT_DEVELOPMENT_GUILD_CONFIGURATION_SNAPSHOT'


@unittest.skipUnless(
    os.environ.get(INTEGRATION_FLAG) == '1',
    f'{INTEGRATION_FLAG}=1 is required for P10.5 database verification',
)
class GuildConfigurationRuntimeDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_stored_graph_builds_exact_runtime_snapshot(self):
        self.assertEqual(os.environ.get('POLYBOT_ENV'), 'development')
        profile = load_runtime_profile(
            project_root=PROJECT_ROOT,
            environ=os.environ,
            create_directories=False,
        )
        target = shadow.target_from_profile(profile)
        storage.validate_target(target)
        snapshot_value = os.environ.get(SNAPSHOT_ENV, '').strip()
        self.assertTrue(
            snapshot_value,
            f'{SNAPSHOT_ENV} must name the reviewed snapshot',
        )
        snapshot_path = Path(snapshot_value)
        self.assertTrue(snapshot_path.is_absolute())
        self.assertTrue(snapshot_path.is_file())
        discord_snapshot = json.loads(
            snapshot_path.read_text(encoding='utf-8')
        )
        bundle = shadow.expected_bundle_from_snapshot(
            profile=profile,
            discord_snapshot=discord_snapshot,
        )
        result = await shadow.run_shadow_comparison(
            shadow.request_from_profile(
                profile=profile,
                expected_bundle=bundle,
            )
        )
        published = runtime.build_runtime_snapshot(
            result=result,
            discord_snapshot=discord_snapshot,
            allowed_guild_ids=profile.allowed_guild_ids,
        )

        self.assertEqual(published.source, 'database')
        self.assertEqual(
            tuple(published.guilds),
            profile.allowed_guild_ids,
        )
        self.assertEqual(
            tuple(published.legacy_config),
            profile.allowed_guild_ids,
        )
        for guild_id in profile.allowed_guild_ids:
            guild = published.guilds[guild_id]
            self.assertGreater(guild.generation, 0)
            self.assertGreater(guild.revision, 0)
            self.assertEqual(
                published.command_policy.capabilities_for_guild(guild_id),
                guild.document.command_capabilities,
            )


if __name__ == '__main__':
    unittest.main()
