"""Explicitly gated read-only P10.3 development PostgreSQL verification."""

from __future__ import annotations

import json
import os
from pathlib import Path
import unittest

from modules import guild_configuration_storage as storage
from runtime_config import load_runtime_profile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INTEGRATION_FLAG = 'POLYBOT_P10_3_STORAGE_INTEGRATION'
SNAPSHOT_PATH = (
    PROJECT_ROOT
    / 'logs/development/guild-configuration/discord-snapshot.json'
)


@unittest.skipUnless(
    os.environ.get(INTEGRATION_FLAG) == '1',
    f'{INTEGRATION_FLAG}=1 is required for P10.3 database verification',
)
class GuildConfigurationStorageDatabaseTests(unittest.TestCase):
    def test_exact_development_import_is_read_only_verified(self):
        self.assertEqual(os.environ.get('POLYBOT_ENV'), 'development')
        profile = load_runtime_profile(
            project_root=PROJECT_ROOT,
            environ=os.environ,
            create_directories=False,
        )
        target = storage.StorageTarget(
            environment=profile.environment,
            database_name=profile.database_name,
            database_user=profile.database_user,
            expected_application_id=profile.expected_bot_id,
            background_tasks_enabled=profile.background_tasks_enabled,
            api_enabled=profile.api_enabled,
            bullet_enabled=profile.bullet_enabled,
        )
        storage.validate_target(target)
        self.assertTrue(SNAPSHOT_PATH.is_file())
        snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding='utf-8'))
        bundle = storage.build_import_bundle(
            target=target,
            server_settings=profile.server_settings,
            allowed_guild_ids=profile.allowed_guild_ids,
            discord_snapshot=snapshot,
        )

        import psycopg2

        connection = psycopg2.connect(
            dbname=profile.database_name,
            user=profile.database_user,
            password=profile.database_password,
            host=profile.database_host,
            port=profile.database_port,
        )
        try:
            connection.set_session(readonly=True, autocommit=True)
            result = storage.verify_storage(
                connection,
                target=target,
                bundle=bundle,
            )
        finally:
            connection.close()
        self.assertEqual(result.verified_guild_ids, profile.allowed_guild_ids)
        self.assertEqual(result.imported_guild_ids, ())
        self.assertEqual(result.unchanged_guild_ids, profile.allowed_guild_ids)


if __name__ == '__main__':
    unittest.main()
