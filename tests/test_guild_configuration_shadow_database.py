"""Explicitly gated read-only P10.4 development shadow comparison."""

from __future__ import annotations

import json
import os
from pathlib import Path
import unittest

from modules import guild_configuration_shadow as shadow
from modules import guild_configuration_storage as storage
from runtime_config import load_runtime_profile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INTEGRATION_FLAG = 'POLYBOT_P10_4_SHADOW_INTEGRATION'
SNAPSHOT_ENV = 'POLYBOT_DEVELOPMENT_GUILD_CONFIGURATION_SNAPSHOT'


@unittest.skipUnless(
    os.environ.get(INTEGRATION_FLAG) == '1',
    f'{INTEGRATION_FLAG}=1 is required for P10.4 database verification',
)
class GuildConfigurationShadowDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_effective_static_and_stored_development_snapshots_match(self):
        self.assertEqual(os.environ.get('POLYBOT_ENV'), 'development')
        profile = load_runtime_profile(
            project_root=PROJECT_ROOT,
            environ=os.environ,
            create_directories=False,
        )
        target = shadow.target_from_profile(profile)
        storage.validate_target(target)
        snapshot_value = os.environ.get(SNAPSHOT_ENV, '').strip()
        self.assertTrue(snapshot_value, f'{SNAPSHOT_ENV} must name the reviewed snapshot')
        snapshot_path = Path(snapshot_value)
        self.assertTrue(snapshot_path.is_absolute())
        self.assertTrue(snapshot_path.is_file())
        snapshot = json.loads(snapshot_path.read_text(encoding='utf-8'))
        bundle = storage.build_import_bundle(
            target=target,
            server_settings=profile.server_settings,
            allowed_guild_ids=profile.allowed_guild_ids,
            discord_snapshot=snapshot,
        )
        request = shadow.request_from_profile(
            profile=profile,
            expected_bundle=bundle,
        )
        result = await shadow.run_shadow_comparison(request)
        self.assertEqual(result.status, shadow.STATUS_MATCHED)
        self.assertTrue(result.promotion_ready)
        self.assertEqual(result.matched_guild_ids, profile.allowed_guild_ids)
        self.assertEqual(result.mismatches, ())


if __name__ == '__main__':
    unittest.main()
